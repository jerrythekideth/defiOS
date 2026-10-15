"""eth_call probe path with a fake RPC that behaves like the Roles modifier."""
from eth_abi import decode, encode

from defios.catalog import Catalog
from defios.chain import CallReverted
from defios.check import run_check
from defios.config import VaultConfig
from defios.roles.conditions import Status

from .conftest import AVATAR, CREDIT_ACCOUNT, OPERATOR, ROLES, ROOT


class ChainLikeRpc:
    """Answers eth_call(execTransactionWithRole) using the offline evaluator, except that it
    pretends the live role rejects every Gearbox pool deposit (to prove live verdicts win)."""

    def __init__(self, adapter, state):
        self.adapter, self.state, self.calls = adapter, state, 0

    def call_raw(self, to, data, block="latest", sender=None, value=0):
        self.calls += 1
        assert to.lower() == ROLES.lower() and sender == OPERATOR
        tgt, val, inner, op, _rk, should_revert = decode(
            ["address", "uint256", "bytes", "uint8", "bytes32", "bool"], data[4:])
        assert should_revert is False
        if tgt.lower() == "0xa9d17f6d3285208280a1fd9b94479c62e0aaba64" and inner[:4].hex() == "6e553f65":
            raise CallReverted(bytes.fromhex("d0a9bf58") + encode(["uint8", "bytes32"], [3, b"\0" * 32]))
        v = self.adapter.check_call(self.state, tgt, val, inner, op, AVATAR)
        if v.status != Status.Ok:
            raise CallReverted(bytes.fromhex("d0a9bf58") + encode(["uint8", "bytes32"], [int(v.status), b"\0" * 32]))
        return encode(["bool"], [True])


def test_probe_overrides_offline(role_state):
    adapter, st = role_state
    vault = VaultConfig(name="t", chain_id=1, avatar=AVATAR, roles_modifier=ROLES, role_key="MANAGER_ROLE",
                        operator=OPERATOR, book_vars={"gearbox_v3.credit_account": CREDIT_ACCOUNT})
    rpc = ChainLikeRpc(adapter, st)
    res = run_check(Catalog.load(ROOT / "catalog"), st, adapter, vault, ["gearbox_v3", "avant"], rpc)
    assert res["probe"] and rpc.calls > 10
    lend = res["actions"]["gearbox_v3"]["lend"]
    dep = [c for c in lend["calls"] if c["selector"] == "0x6e553f65"][0]
    assert dep["source"] == "eth_call" and dep["status"] == "missing"
    assert "FunctionNotAllowed" in dep["reason"]
    assert any("live eth_call says missing" in n for n in dep["notes"])
    mc = res["actions"]["gearbox_v3"]["credit_account_multicall"]["calls"][0]
    assert mc["status"] == "unsafe" and mc["source"] == "eth_call"
    assert res["actions"]["avant"]["stake"]["status"] == "allowed"
