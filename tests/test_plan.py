"""defios plan: intent -> engine-built calls -> role check -> calls.json (offline)."""
import json

import pytest
from click.testing import CliRunner
from eth_abi import decode as abi_decode

from defios.catalog import Catalog
from defios.cli import main
from defios.config import VaultConfig
from defios.plan import (
    PlanError,
    Step,
    load_intent,
    parse_step,
    plan,
    resolve_action,
    resolve_token,
    to_raw,
)
from defios.propose import propose
from defios.roles.base import FunctionState, RoleState, TargetState
from defios.roles.conditions import (
    AbiType,
    Clearance,
    Condition,
    ExecutionOptions,
    Operator,
    flatten,
)
from defios.roles.v2 import RolesV2, role_key_bytes32
from defios.tx import MULTISEND_CALL_ONLY_141, MULTISEND_SELECTOR, load_calls_file

from .conftest import AVATAR, FIXTURE, OPERATOR, ROLES, ROOT

WETH = "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"
STETH = "0xae7ab96520DE3A18E5e111B5EaAb095312D7fE84"
WSTETH = "0x7f39C581F595B53c5cb19bD0b3f8dA6c935E2Ca0"
POOL = "0xA9d17f6D3285208280a1Fd9B94479c62e0AABa64"
E18 = 10**18


def _word(addr: str) -> bytes:
    return bytes(12) + bytes.fromhex(addr[2:])


def _scoped(target, sel, children, options=ExecutionOptions.NONE):
    root = Condition(AbiType.CALLDATA, Operator.MATCHES, b"", children)
    return FunctionState(target, sel, False, int(options), flat=flatten(root), condition=root)


def _pass():
    return Condition(AbiType.STATIC, Operator.PASS)


def good_role() -> RoleState:
    """A role that allows exactly the WETH -> ETH -> stETH -> wstETH -> Gearbox path, receivers pinned."""
    st = RoleState("v2", 1, ROLES, role_key_bytes32("MANAGER_ROLE"), 123, "test", members=[OPERATOR])
    fns = [
        FunctionState(WETH, "0x2e1a7d4d", True, int(ExecutionOptions.NONE)),                 # withdraw
        FunctionState(STETH, "0xa1903eab", True, int(ExecutionOptions.SEND)),                # submit
        _scoped(STETH, "0x095ea7b3", [Condition(AbiType.STATIC, Operator.EQUAL_TO, _word(WSTETH)), _pass()]),
        FunctionState(WSTETH, "0xea598cb0", True, int(ExecutionOptions.NONE)),               # wrap
        _scoped(WSTETH, "0x095ea7b3", [Condition(AbiType.STATIC, Operator.EQUAL_TO, _word(POOL)), _pass()]),
        _scoped(POOL, "0x6e553f65", [_pass(), Condition(AbiType.STATIC, Operator.EQUAL_TO_AVATAR)]),  # deposit
    ]
    for f in fns:
        st.targets[f.target.lower()] = TargetState(f.target, Clearance.FUNCTION, ExecutionOptions.NONE)
        st.functions[(f.target.lower(), f.selector)] = f
    st.unwrappers[f"{MULTISEND_CALL_ONLY_141[1].lower()}:{MULTISEND_SELECTOR}"] = "0x" + "11" * 20
    return st


@pytest.fixture(scope="module")
def catalog():
    return Catalog.load(ROOT / "catalog")


@pytest.fixture
def vault():
    return VaultConfig(name="testvault", chain_id=1, avatar=AVATAR, roles_modifier=ROLES, role_key="MANAGER_ROLE",
                       operator=OPERATOR)


def run(args):
    return CliRunner().invoke(main, args, catch_exceptions=False)


def write_lock(agent_dir, st: RoleState):
    d = agent_dir / "books" / "testvault"
    d.mkdir(parents=True, exist_ok=True)
    (d / "role.lock.json").write_text(json.dumps(st.to_lock_json(), default=str))


# ------------------------------------------------------------------------------ end to end
def test_e2e_unwrap_stake_wrap_lend_from_lock(agent_dir):
    """'unwrap 1 WETH, stake in Lido, wrap to wstETH, lend on Gearbox kpkwstETH pool', checked against
    role.lock.json (no log source), written to calls.json."""
    write_lock(agent_dir, good_role())
    r = run(["--root", str(agent_dir), "--format", "json", "plan",
             "weth.unwrap=1", "lido.stake=1", "wstETH.wrap=0.999", "kpkwstETH.lend=0.8"])
    assert r.exit_code == 0, r.output
    p = json.loads(r.output)
    assert p["ok"] and not p["refused"]
    assert p["role"]["source"].startswith("role.lock.json") and p["role"]["block"] == 123
    assert [s["action"] for s in p["steps"]] == ["weth.unwrap", "lido.stake", "lido.wrap", "gearbox_v3.lend",
                                                 "revoke_approvals"]
    assert p["steps"][3]["resolved_from"] == "kpkwstETH.lend"
    assert any("role.lock.json snapshot" in w for w in p["warnings"])
    assert not any("unwrapper" in w for w in p["warnings"])

    calls, meta = load_calls_file(agent_dir / "calls.json")
    assert meta["chain_id"] == 1 and meta["vault"] == "testvault"
    got = [(c.to, c.selector, c.value, c.operation) for c in calls]
    assert got == [
        (WETH, "0x2e1a7d4d", 0, 0),        # WETH.withdraw(wad)
        (STETH, "0xa1903eab", E18, 0),     # stETH.submit(referral) sending 1 ETH
        (STETH, "0x095ea7b3", 0, 0),       # stETH.approve(wstETH, amount)
        (WSTETH, "0xea598cb0", 0, 0),      # wstETH.wrap(amount)
        (WSTETH, "0x095ea7b3", 0, 0),      # wstETH.approve(pool, amount)
        (POOL, "0x6e553f65", 0, 0),        # pool.deposit(assets, receiver)
        (STETH, "0x095ea7b3", 0, 0),       # stETH.approve(wstETH, 0): no allowance left open
        (WSTETH, "0x095ea7b3", 0, 0),      # wstETH.approve(pool, 0)
    ]
    args = lambda c, types: abi_decode(types, c.data[4:])
    assert args(calls[0], ["uint256"]) == (E18,)
    assert args(calls[1], ["address"]) == ("0x" + "00" * 20,)
    assert args(calls[2], ["address", "uint256"]) == (WSTETH.lower(), 999 * 10**15)
    assert args(calls[3], ["uint256"]) == (999 * 10**15,)
    assert args(calls[4], ["address", "uint256"]) == (POOL.lower(), 8 * 10**17)
    assert args(calls[5], ["uint256", "address"]) == (8 * 10**17, AVATAR)
    assert args(calls[6], ["address", "uint256"]) == (WSTETH.lower(), 0)
    assert args(calls[7], ["address", "uint256"]) == (POOL.lower(), 0)

    lines = [c["line"] for s in p["steps"][:-1] for c in s["calls"]]
    assert lines[-1] == "gearbox_v3.kpkwstETH_pool.deposit(assets=0.8 wstETH, receiver=the Vault)"
    assert lines[1].endswith("sending 1 ETH")

    # the Operator tx preview is execTransactionWithRole(MultiSend(all eight calls))
    assert p["operator_tx"]["batch"] == "roles" and p["operator_tx"]["to"] == ROLES
    assert p["operator_tx"]["data"].startswith("0xc6fe8747")

    # and `propose` accepts the file as is (dry run, mocked Safe service)
    from defios.config import load_config

    from .test_propose import FakeService
    v = load_config(agent_dir).vault(None)
    rep = propose(v, calls, catalog=Catalog.load(ROOT / "catalog"), service=FakeService(), private_key="")
    assert rep["mode"] == "dry-run" and not rep["posted"]
    assert [d["to"] for d in rep["multisend"]["decoded"]] == [c.to for c in calls]
    assert not [x for x in rep["problems"] if "uncatalogued" in x]


def test_refuses_missing_and_unsafe_against_fixture_role(agent_dir):
    """Fixture role: WETH.withdraw is not allowed, pool.deposit takes any receiver -> refuse, write nothing."""
    r = run(["--root", str(agent_dir), "--format", "json", "--logs-file", str(FIXTURE), "plan",
             "weth.unwrap=1", "lido.stake=1", "lido.wrap=0.999", "gearbox_v3.lend=0.8"])
    assert r.exit_code == 3, r.output
    p = json.loads(r.output)
    assert not p["ok"] and p["calls"] == [] and p["operator_tx"] is None
    by_step = {x["step"]: x for x in p["refused"]}
    assert set(by_step) == {1, 4}
    assert by_step[1]["status"] == "missing" and "does not allow weth.WETH.withdraw" in by_step[1]["why"]
    assert by_step[4]["status"] == "unsafe" and "receiver" in by_step[4]["why"]
    assert not (agent_dir / "calls.json").exists()


def test_allowed_part_of_fixture_role_plans(agent_dir):
    r = run(["--root", str(agent_dir), "--format", "text", "--logs-file", str(FIXTURE), "plan",
             "lido.stake", "--amount", "2"])
    assert r.exit_code == 0, r.output
    assert "PLAN" in r.output
    calls, _ = load_calls_file(agent_dir / "calls.json")
    assert len(calls) == 1 and calls[0].value == 2 * E18


def test_no_role_source_is_an_error(agent_dir):
    r = run(["--root", str(agent_dir), "--format", "json", "plan", "lido.stake=1"])
    assert r.exit_code == 2 and "defios sync" in r.output


# ------------------------------------------------------------------------------ units
def test_resolve_aliases(catalog):
    assert resolve_action(catalog, "gearbox_v3.lend")[:2] == ("gearbox_v3", "lend")
    assert resolve_action(catalog, "gearbox pool.lend")[:2] == ("gearbox_v3", "lend")
    assert resolve_action(catalog, "kpkwstETH.lend")[:2] == ("gearbox_v3", "lend")
    assert resolve_action(catalog, "wsteth.wrap")[:2] == ("lido", "wrap")
    with pytest.raises(PlanError, match="no action"):
        resolve_action(catalog, "lido.lend")
    with pytest.raises(PlanError, match="no card action"):
        resolve_action(catalog, "kpkwstETH.stake")
    assert resolve_token(catalog, "kpkwstETH")["address"] == POOL
    assert resolve_token(catalog, "wstETH")["decimals"] == 18
    with pytest.raises(PlanError, match="not in any card"):
        resolve_token(catalog, "0x" + "12" * 20)


def test_amounts():
    assert to_raw("1", 18, "x") == E18
    assert to_raw("0.000001", 6, "x") == 1
    for bad in ("0", "-1", "max", "0.0000001"):
        with pytest.raises(PlanError):
            to_raw(bad, 6, "x")


def test_token_must_match_input(catalog, vault):
    adapter = RolesV2(ROLES)
    with pytest.raises(PlanError, match="takes lido.wstETH, not weth.WETH"):
        plan(catalog, good_role(), adapter, vault, [Step("gearbox_v3.lend", "1", "WETH")])
    p = plan(catalog, good_role(), adapter, vault, [Step("gearbox_v3.lend", "1", "wstETH")])
    assert p["ok"] and len(p["calls"]) == 3


def test_approvals_are_revoked_in_the_same_tx(catalog, vault):
    """approve exact -> action -> approve(spender, 0); one revoke per token + spender; opt-out and keep."""
    adapter = RolesV2(ROLES)
    p = plan(catalog, good_role(), adapter, vault, [Step("gearbox_v3.lend", "1", "wstETH")])
    sel = [c["data"][:10] for c in p["calls"]]
    assert sel == ["0x095ea7b3", "0x6e553f65", "0x095ea7b3"]
    assert abi_decode(["address", "uint256"], bytes.fromhex(p["calls"][2]["data"][10:])) == (POOL.lower(), 0)
    assert p["steps"][-1]["action"] == "revoke_approvals"
    # two lends to the same pool -> still one revoke
    p2 = plan(catalog, good_role(), adapter, vault, [Step("gearbox_v3.lend", "1", "wstETH"),
                                                     Step("gearbox_v3.lend", "2", "wstETH")])
    assert [c["data"][:10] for c in p2["calls"]].count("0x095ea7b3") == 3
    # opt-out
    p3 = plan(catalog, good_role(), adapter, vault, [Step("gearbox_v3.lend", "1", "wstETH")], keep_approvals=True)
    assert len(p3["calls"]) == 2 and any("--keep-approvals" in w for w in p3["warnings"])
    # a role that only allows approve(pool, exact) cannot revoke -> refuse the plan
    st = good_role()
    st.functions[(WSTETH.lower(), "0x095ea7b3")] = _scoped(
        WSTETH, "0x095ea7b3", [Condition(AbiType.STATIC, Operator.EQUAL_TO, _word(POOL)),
                               Condition(AbiType.STATIC, Operator.GREATER_THAN, (0).to_bytes(32, "big"))])
    p4 = plan(catalog, st, adapter, vault, [Step("gearbox_v3.lend", "1", "wstETH")])
    assert not p4["ok"] and p4["refused"][0]["action"] == "revoke after gearbox_v3.lend"
    # cards that are pulled later keep their approval
    assert catalog.cards["cowswap"].actions["swap"]["approvals"] == "keep"


def test_free_vars_are_never_sampled(catalog, vault):
    p = plan(catalog, good_role(), RolesV2(ROLES), vault, [Step("lido.claim_withdrawal")])
    assert not p["ok"] and p["refused"][0]["status"] == "unresolved"
    assert "request_id" in p["refused"][0]["why"]


def test_signature_actions_refused(catalog, vault):
    signed = [(proto, a) for proto, c in catalog.cards.items() for a, act in c.actions.items()
              if any("sign" in x for x in (act or {}).get("calls") or [])]
    if not signed:
        pytest.skip("no card action needs a signature")
    proto, a = signed[0]
    with pytest.raises(PlanError, match="off-chain signature"):
        plan(catalog, good_role(), RolesV2(ROLES), vault, [Step(f"{proto}.{a}", "1")])


def test_intent_json_and_step_parsing(tmp_path):
    f = tmp_path / "intent.json"
    f.write_text(json.dumps({"description": "lend", "steps": [
        {"action": "gearbox_v3.lend", "amount": "1", "token": "wstETH"}, "lido.stake=2"]}))
    steps, desc = load_intent(str(f))
    assert desc == "lend" and steps[0].token == "wstETH" and steps[1].amount == "2"
    steps, _ = load_intent('{"action": "weth.unwrap", "amount": 1}')
    assert steps[0].amount == "1"
    assert parse_step("gearbox pool.lend=0.5").ref == "gearbox pool.lend"
    with pytest.raises(PlanError):
        parse_step("lend")
    with pytest.raises(PlanError):
        load_intent('{"action": "x.y", "to": "0xdead"}')


def test_lock_roundtrip_gives_same_check(role_state):
    from defios.check import run_check
    adapter, st = role_state
    st2 = RoleState.from_lock_json(json.loads(json.dumps(st.to_lock_json(), default=str)))
    v = VaultConfig(name="t", chain_id=1, avatar=AVATAR, roles_modifier=ROLES, role_key="MANAGER_ROLE",
                    operator=OPERATOR)
    cat = Catalog.load(ROOT / "catalog")
    a, b = run_check(cat, st, adapter, v), run_check(cat, st2, adapter, v)
    assert a["actions"] == b["actions"] and a["lints"] == b["lints"]


def test_probe_refusal_and_onchain_decimals(catalog, vault):
    from .test_probe import ChainLikeRpc

    class Rpc(ChainLikeRpc):
        dec = 18

        def call(self, to, sig, args=(), out_types=None, **kw):
            assert sig == "decimals()"
            return self.dec

    st, adapter = good_role(), RolesV2(ROLES)
    rpc = Rpc(adapter, st)
    p = plan(catalog, st, adapter, vault, [Step("gearbox_v3.lend", "1")], rpc=rpc)
    assert p["role"]["probe"] and not p["ok"]
    assert p["refused"][0]["status"] == "missing" and "live" in p["refused"][0]["why"]
    p = plan(catalog, st, adapter, vault, [Step("lido.wrap", "1")], rpc=rpc)
    assert p["ok"] and all(c["check"]["source"] == "eth_call" for c in p["steps"][0]["calls"])
    rpc.dec = 6
    with pytest.raises(PlanError, match="card says 18 decimals, chain says 6"):
        plan(catalog, st, adapter, vault, [Step("lido.wrap", "1")], rpc=rpc)


def test_card_plan_fields_validate(catalog):
    from defios.catalog import Card, validate_card
    for ref in ("weth.WETH", "lido.stETH", "lido.wstETH", "gearbox_v3.kpkwstETH_pool"):
        p, n = ref.split(".")
        assert catalog.contract_meta(p, n)["decimals"] == 18
    for proto, a in (("weth", "unwrap"), ("lido", "stake"), ("lido", "wrap"), ("gearbox_v3", "lend")):
        assert catalog.cards[proto].actions[a]["input"]
    raw = {"protocol": "x", "name": "x", "chain_id": 1, "status": "draft", "reviewed": False,
           "contracts": {"T": {"address": WETH, "decimals": "18"}},
           "actions": {"a": {"input": "nope", "defaults": {"zzz": 1},
                             "calls": [{"target": "T", "sig": "withdraw(uint256 wad)", "args": {"wad": "$amount"}}]}}}
    errs = " ".join(validate_card(catalog, Card("x", ROOT, raw)))
    assert "decimals must be an integer" in errs and "input 'nope'" in errs and "default 'zzz'" in errs


def test_mcp_plan_tool(agent_dir, monkeypatch):
    from defios.mcp import tool_plan
    write_lock(agent_dir, good_role())
    monkeypatch.setenv("DEFIOS_ROOT", str(agent_dir))
    res = tool_plan([{"action": "gearbox pool.lend", "amount": "1"}], out="calls.json")
    assert res["ok"] and res["written"] and len(load_calls_file(agent_dir / "calls.json")[0]) == 3
