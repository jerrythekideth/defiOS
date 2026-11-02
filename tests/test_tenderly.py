"""`defios simulate --tenderly`: the bundle sent to Tenderly and how its answer is read (API mocked)."""
import pytest
from eth_utils import to_checksum_address

from defios import tenderly as td
from defios.config import VaultConfig
from defios.simulate import APPROVAL, SimulateError, TRANSFER
from defios.tx import Call

from .conftest import AVATAR, OPERATOR, ROLES

USDC = to_checksum_address("0x" + "c0" * 20)
DEAD = "0x" + "00" * 18 + "dead"
BATCH = {"transactions": [{"to": ROLES, "value": "0", "data": "0x12345678"}]}


@pytest.fixture
def keys(monkeypatch):
    for k, v in zip(td.ENV, ("secret", "acct", "proj")):
        monkeypatch.setenv(k, v)


def _vault():
    return VaultConfig("t", 1, AVATAR, ROLES, "MANAGER_ROLE", operator=OPERATOR)


def _topic(a):
    return "0x" + a[2:].lower().rjust(64, "0")


def _answer(status=True):
    sent = {}

    def post(url, key, body):
        sent.update(url=url, key=key, body=body)
        transfer = {"raw": {"address": USDC, "topics": [TRANSFER, _topic(AVATAR), _topic(DEAD)],
                            "data": "0x" + (5 * 10**6).to_bytes(32, "big").hex()}}
        approval = {"raw": {"address": USDC, "topics": [APPROVAL, _topic(AVATAR), _topic(DEAD)], "data": "0x" + "00" * 32}}
        res = [{"transaction": {"status": True, "gas_used": 50_000}, "simulation": {"id": "sim-1"}},
               {"transaction": {"status": status, "gas_used": 90_000, "error_message": None if status else "ConditionViolation",
                                "transaction_info": {"logs": [transfer, approval]}}, "simulation": {"id": "sim-2"}}]
        return {"simulation_results": res[: len(body["simulations"])]}
    return post, sent


def test_bundle_runs_batch_then_operator(keys):
    post, sent = _answer()
    calls = [Call(USDC, 0, bytes.fromhex("a9059cbb") + bytes(64), label="transfer")]
    r = td.simulate(_vault(), batch=BATCH, calls=calls, owner=AVATAR, block=123, post=post)
    sims = sent["body"]["simulations"]
    assert sent["key"] == "secret" and "/account/acct/project/proj/simulate-bundle" in sent["url"]
    assert [s["from"] for s in sims] == [to_checksum_address(AVATAR), to_checksum_address(OPERATOR)]
    assert sims[1]["to"] == to_checksum_address(ROLES) and sims[1]["input"].startswith("0x")
    assert all(s["save"] and s["block_number"] == 123 and s["network_id"] == "1" for s in sims)
    assert r["ok"] and r["steps"][1]["tenderly"].endswith("/acct/proj/simulator/sim-2")
    assert r["vault_deltas"][USDC]["raw"] == str(-5 * 10**6)
    assert r["approvals_seen"] and any("approved" in w for w in r["warnings"])


def test_revert_is_reported(keys):
    post, _ = _answer(status=False)
    r = td.simulate(_vault(), batch=BATCH, calls=[Call(USDC, 0, b"\x01\x02\x03\x04")], owner=AVATAR, post=post)
    assert not r["ok"] and r["steps"][1]["error"] == "ConditionViolation"
    assert "FAILED" in td.summarize(r)


def test_missing_keys_refused(monkeypatch):
    for k in td.ENV:
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(SimulateError, match="TENDERLY_ACCESS_KEY"):
        td.simulate(_vault(), batch=BATCH, post=lambda *a: {})
