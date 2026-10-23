"""`defios simulate` on a plain local anvil (no fork): mock contracts set with anvil_setCode.

Skipped when anvil is not installed (set ANVIL=/path/to/anvil or put it on PATH).
"""
import json
import os
import shutil

import pytest
from eth_abi import decode as abi_decode
from eth_utils import keccak, to_checksum_address

from defios.config import VaultConfig
from defios.decode import decode_batch
from defios.simulate import APPROVAL, TRANSFER, Fork, batch_calls, simulate
from defios.tx import Call

from .conftest import AVATAR, OPERATOR, ROLES
from .test_cli import run

ANVIL = os.environ.get("ANVIL") or shutil.which("anvil")
needs_anvil = pytest.mark.skipif(not ANVIL, reason="anvil not installed")
SPENDER = "0x000000000000000000000000000000000000beef"
DEAD = "0x000000000000000000000000000000000000dead"
AMOUNT = 5 * 10**6


def _w(x) -> bytes:
    return (x if isinstance(x, bytes) else bytes.fromhex(x[2:])).rjust(32, b"\0")


def _mock_roles() -> str:
    """Any call: emit Transfer(Vault -> 0xdead, 5e6) and Approval(Vault, 0xbeef, 5e6), return the Vault's address
    (so owner() is the Vault and allowance() is non-zero)."""
    code = b""
    code += b"\x7f" + AMOUNT.to_bytes(32, "big") + b"\x60\x00\x52"  # mstore(0, amount)
    code += b"\x7f" + _w(DEAD) + b"\x7f" + _w(AVATAR) + b"\x7f" + _w(TRANSFER) + b"\x60\x20\x60\x00\xa3"
    code += b"\x7f" + _w(SPENDER) + b"\x7f" + _w(AVATAR) + b"\x7f" + _w(APPROVAL) + b"\x60\x20\x60\x00\xa3"
    code += b"\x7f" + _w(AVATAR) + b"\x60\x00\x52\x60\x20\x60\x00\xf3"  # return the Vault
    return "0x" + code.hex()


REVERT = "0x60006000fd"


def _vault():
    return VaultConfig("testvault", 1, to_checksum_address(AVATAR), to_checksum_address(ROLES), "MANAGER_ROLE",
                       operator=to_checksum_address(OPERATOR))


def _batch(n=2):
    return {"version": "1.0", "chainId": "1", "meta": {"createdFromSafeAddress": to_checksum_address(AVATAR)},
            "transactions": [{"to": to_checksum_address(ROLES), "value": "0", "data": "0x12345678" + "00" * 32,
                              "contractMethod": None} for _ in range(n)]}


@pytest.fixture
def fork():
    with Fork(None, anvil=ANVIL, chain_id=1) as f:
        f.rpc.request("anvil_setCode", [to_checksum_address(ROLES), _mock_roles()])
        yield f


def test_contract_method_batches_are_encoded():
    rk = "0x" + b"MANAGER_ROLE".hex().ljust(64, "0")
    tx = {"to": to_checksum_address(ROLES), "value": "0", "data": None,
          "contractMethod": {"name": "scopeTarget", "payable": False,
                             "inputs": [{"name": "roleKey", "type": "bytes32"}, {"name": "targetAddress", "type": "address"}]},
          "contractInputsValues": {"roleKey": rk, "targetAddress": SPENDER}}
    (c,) = batch_calls({"transactions": [tx]})
    assert c.data[:4] == keccak(text="scopeTarget(bytes32,address)")[:4]
    assert to_checksum_address(abi_decode(["bytes32", "address"], c.data[4:])[1]) == to_checksum_address(SPENDER)
    tx2 = dict(tx, data="0x" + c.data.hex(), contractMethod=None)
    assert decode_batch({"transactions": [tx2]})["transactions"][0]["function"] == "scopeTarget"


@needs_anvil
def test_batch_then_calls(fork):
    r = simulate(_vault(), fork, batch=_batch(2), calls=[Call(SPENDER, 0, "0xabcdef01")])
    assert r["ok"], r
    assert [s["kind"] for s in r["steps"]] == ["permission", "permission", "operator"]
    assert all(s["gas"] > 21000 for s in r["steps"])
    (d,) = r["vault_deltas"].values()
    assert d["raw"] == str(-3 * AMOUNT)
    (a,) = r["open_allowances"]
    assert a["spender"] == to_checksum_address(SPENDER) and any("still open" in w for w in r["warnings"])


@needs_anvil
def test_revert_stops_the_batch(fork):
    fork.rpc.request("anvil_setCode", [to_checksum_address(ROLES), REVERT])
    r = simulate(_vault(), fork, batch=_batch(2), calls=[Call(SPENDER, 0, "0x")])
    assert not r["ok"]
    assert r["steps"][0]["ok"] is False and r["steps"][1]["ok"] is None and r["steps"][2]["ok"] is None


@needs_anvil
def test_cli_simulate(agent_dir):
    (agent_dir / "b.json").write_text(json.dumps(_batch(1)))
    # no mock code: the call to an empty address succeeds, nothing moves
    r = run(["--root", str(agent_dir), "--format", "json", "--offline", "simulate", "--batch",
             str(agent_dir / "b.json"), "--no-fork", "--anvil", ANVIL])
    assert r.exit_code == 0, r.output
    data = json.loads(r.output)
    assert data["ok"] and data["vault_deltas"] == {} and data["open_allowances"] == []


@needs_anvil
def test_permit_is_simulated_on_a_fork(agent_dir, monkeypatch):
    """RPC_URL points at a local anvil (standing in for mainnet); permit forks it and runs its batch."""
    from .conftest import FIXTURE
    monkeypatch.setenv("PATH", os.path.dirname(ANVIL) + os.pathsep + os.environ.get("PATH", ""))
    with Fork(None, anvil=ANVIL, chain_id=1) as upstream:
        upstream.rpc.request("anvil_setCode", [to_checksum_address(ROLES), _mock_roles()])
        monkeypatch.setenv("RPC_URL", upstream.rpc.url)
        r = run(["--root", str(agent_dir), "--format", "json", "--logs-file", str(FIXTURE), "permit", "avant.redeem",
                 "--out", "p.json"])
        assert r.exit_code == 0, r.output
        data = json.loads(r.output)
        assert data["simulation"]["ok"] and data["simulation"]["forked"]
        assert [s["kind"] for s in data["simulation"]["steps"]] == ["permission"]
        upstream.rpc.request("anvil_setCode", [to_checksum_address(ROLES), REVERT])
        r = run(["--root", str(agent_dir), "--format", "json", "--logs-file", str(FIXTURE), "permit", "avant.redeem",
                 "--out", "p2.json"])
        assert r.exit_code == 3 and not (agent_dir / "p2.json").exists()
