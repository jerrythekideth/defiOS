"""`defios permit`: card -> conditions, merge with the stored role, verification, Tx Builder output."""
import json

import pytest
from eth_utils import to_checksum_address

from defios.catalog import Catalog
from defios.cli import main
from defios.config import VaultConfig
from defios.decode import decode_batch
from defios.logs import StaticLogs
from defios.permit import ConditionBuilder, Op, PermitError, apply_ops, merge, permit
from defios.roles.conditions import AbiType, Condition, Operator
from defios.roles.v2 import RolesV2

from .conftest import AVATAR, FIXTURE, ROOT
from .test_cli import run

BATCH = ROOT / "tests" / "fixtures" / "usd_batch_logs.json"
USDC = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
POOL = "0x87870Bca3F3fD6335C3F4ce8392D69350B4fA4E2"
APPROVE = "0x095ea7b3"


@pytest.fixture(scope="module")
def catalog():
    return Catalog.load(ROOT / "catalog")


@pytest.fixture(scope="module")
def usd():
    d = json.loads(BATCH.read_text())
    adapter = RolesV2(d["roles_modifier"])
    st = adapter.read_role("MANAGER_ROLE", StaticLogs(d["logs"], d["latest_block"]))
    vault = VaultConfig("usd", 1, to_checksum_address(d["avatar"]), to_checksum_address(d["roles_modifier"]),
                        "MANAGER_ROLE", operator=to_checksum_address(d["operator"]))
    return adapter, st, vault


def _addr(c: Condition) -> str:
    return to_checksum_address(c.comp_value[12:])


def test_condition_from_card(catalog):
    cb = ConditionBuilder(catalog, AVATAR)
    call = catalog.cards["aave_v3"].actions["supply"]["calls"][1]
    root, opts = cb.call("aave_v3", "supply", 1, call, [USDC])
    assert (root.param_type, root.operator, opts) == (AbiType.CALLDATA, Operator.MATCHES, 0)
    asset, amount, on_behalf, referral = root.children
    assert _addr(asset) == USDC and amount.operator == Operator.PASS
    assert _addr(on_behalf) == to_checksum_address(AVATAR)
    assert referral.operator == Operator.EQUAL_TO and int.from_bytes(referral.comp_value, "big") == 0
    # several tokens -> Or; a tuple param -> Matches on the tuple with typed children
    root2, _ = cb.call("aave_v3", "supply", 1, call, [USDC, "0xdAC17F958D2ee523a2206206994597C13D831ec7"])
    assert root2.children[0].operator == Operator.OR and len(root2.children[0].children) == 2
    strata = catalog.cards["strata"].actions["deposit_srusde"]["calls"][-1]
    root3, _ = cb.call("strata", "deposit_srusde", 1, strata, [])
    assert root3.children[-1].param_type == AbiType.TUPLE and root3.children[-1].operator == Operator.MATCHES
    # every param free -> no condition (allowFunction)
    emode = catalog.cards["aave_v3"].actions["set_emode"]["calls"][0]
    assert cb.call("aave_v3", "set_emode", 0, emode, [])[0] is None


def test_calls_params_refused(catalog):
    cb = ConditionBuilder(catalog, AVATAR, {"gearbox_v3.credit_account": "0xcafe00000000000000000000000000000000cafe"})
    for proto, card in catalog.cards.items():
        for aname, action in card.actions.items():
            for i, call in enumerate((action or {}).get("calls") or []):
                if call.get("calls_params"):
                    with pytest.raises(PermitError, match="arbitrary calls"):
                        cb.call(proto, aname, i, call, [])
                    return
    pytest.skip("no card uses calls_params")


def test_merge_unions_one_address_param_only():
    word = lambda a: bytes(12) + bytes.fromhex(a[2:])  # noqa: E731
    eq = lambda a: Condition(AbiType.STATIC, Operator.EQUAL_TO, word(a))  # noqa: E731
    p = Condition(AbiType.STATIC, Operator.PASS)
    a, b, c = "0x" + "11" * 20, "0x" + "22" * 20, "0x" + "33" * 20
    old = Condition(AbiType.CALLDATA, Operator.MATCHES, b"", [Condition(AbiType.NONE, Operator.OR, b"", [eq(a), eq(b)])])
    new = Condition(AbiType.CALLDATA, Operator.MATCHES, b"", [eq(c), p])
    m, why = merge(old, new, AVATAR)
    assert [x.comp_value for x in m.children[0].children] == [word(a), word(b), word(c)] and len(m.children) == 1
    assert merge(old, Condition(AbiType.CALLDATA, Operator.MATCHES, b"", [eq(b)]), AVATAR)[1] == "same"
    two = Condition(AbiType.CALLDATA, Operator.MATCHES, b"", [eq(c), eq(a)])
    assert merge(old, two, AVATAR)[0] is None


def test_already_allowed_is_a_no_op(catalog, usd):
    adapter, st, vault = usd
    r = permit(catalog, st, adapter, vault, ["aave_v3.supply", "aave_v3.withdraw"])
    assert r["ok"] and r["ops"] == [] and r["batch"] is None
    assert len(r["already_allowed"]) == 10  # 8 token approves + supply + withdraw


def test_new_permission_batch_round_trips(catalog, usd):
    adapter, st, vault = usd
    r = permit(catalog, st, adapter, vault, ["dialectic.lock", "aave_v3.enable_collateral"], tokens=["USDC"])
    assert r["ok"], r
    assert [o["fn"] for o in r["ops"]] == ["scopeTarget", "scopeFunction", "scopeTarget", "scopeFunction",
                                           "scopeFunction"]
    assert r["regressions"] == [] and r["new_high_lints"] == []
    assert r["after"]["dialectic.lock"] == "allowed"
    b = r["batch"]
    assert b["meta"]["createdFromSafeAddress"] == vault.avatar
    assert {t["to"] for t in b["transactions"]} == {vault.roles_modifier}
    dec = decode_batch(b)["transactions"]
    assert [t["function"] for t in dec] == [o["fn"] for o in r["ops"]]
    assert all(t["role"] == "MANAGER_ROLE" for t in dec)
    lock = dec[3]
    assert lock["selector"] == "0xcf27cfc4" and to_checksum_address(AVATAR) in lock["condition_text"]


def test_approve_spender_is_added_not_replaced(catalog, usd):
    adapter, st, vault = usd
    old = st.function(USDC, APPROVE).condition
    spenders = [_addr(c) for c in old.children[0].children]
    # a role whose USDC approve lacks the Aave pool
    without = Condition(AbiType.CALLDATA, Operator.MATCHES, b"", [Condition(
        AbiType.NONE, Operator.OR, b"", [c for c in old.children[0].children if _addr(c) != POOL])])
    st2 = apply_ops(st, [Op("scopeFunction", USDC, APPROVE, without, 0)])
    r = permit(catalog, st2, adapter, vault, ["aave_v3.supply"], tokens=["USDC"])
    assert r["ok"], r
    (op,) = r["ops"]
    assert op["fn"] == "scopeFunction" and "merged" in op["why"]
    after = apply_ops(st2, [Op("scopeFunction", USDC, APPROVE,
                               merge(without, ConditionBuilder(catalog, vault.avatar).call(
                                   "aave_v3", "supply", 0, catalog.cards["aave_v3"].actions["supply"]["calls"][0],
                                   [USDC])[0], vault.avatar)[0], 0)])
    got = [_addr(c) for c in after.function(USDC, APPROVE).condition.children[0].children]
    assert sorted(got) == sorted(spenders)


def test_looser_role_refused_unless_replace(catalog, usd):
    adapter, st, vault = usd
    st2 = apply_ops(st, [Op("allowFunction", USDC, APPROVE, None, 0)])
    r = permit(catalog, st2, adapter, vault, ["aave_v3.supply"], tokens=["USDC"])
    assert not r["ok"] and "looser than the card" in r["refused"][0]["why"]
    r2 = permit(catalog, st2, adapter, vault, ["aave_v3.supply"], tokens=["USDC"], replace=True)
    assert r2["ok"] and r2["ops"][0]["fn"] == "scopeFunction"
    assert r2["regressions"]  # other USDC spenders stop working, and the result says so
    assert any("--replace" in w for w in r2["warnings"])


def test_new_high_lint_blocks(catalog, usd):
    adapter, st, vault = usd
    r = permit(catalog, st, adapter, vault, ["liminal.deposit"])
    assert not r["ok"] and r["new_high_lints"][0]["code"] == "unscoped_calls"
    assert "batch" not in r


def test_dormant_scope_revoked_before_scope_target(catalog, usd):
    adapter, st, vault = usd
    tgt = "0xb8431779dd9d5753b5a7F66afa3C65ABFD8c3fe1"  # dialectic smDUSD: not in this role
    st2 = apply_ops(st, [Op("allowFunction", tgt, "0x12345678", None, 0)])  # stored, but target not scoped
    r = permit(catalog, st2, adapter, vault, ["dialectic.lock"])
    fns = [(o["fn"], o.get("selector")) for o in r["ops"] if o["target"].lower() == tgt.lower()]
    assert fns[0] == ("revokeFunction", "0x12345678") and fns[1][0] == "scopeTarget"


def test_cli_permit(agent_dir):
    r = run(["--root", str(agent_dir), "--format", "json", "--logs-file", str(FIXTURE), "--offline",
             "permit", "gearbox_v3.lend"])
    assert r.exit_code == 3 and json.loads(r.output)["refused"]
    r = run(["--root", str(agent_dir), "--format", "json", "--logs-file", str(FIXTURE), "--offline",
             "permit", "avant.redeem", "--out", "p.json"])
    assert r.exit_code == 0, r.output
    batch = json.loads((agent_dir / "p.json").read_text())
    dec = decode_batch(batch)["transactions"]
    assert [t["function"] for t in dec] == ["scopeFunction"] and dec[0]["selector"] == APPROVE
