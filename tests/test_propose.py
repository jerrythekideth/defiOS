"""propose.py: SafeTxHash vector, nonce selection, delegate signing, dry-run default (offline, mocked service)."""
import json

import pytest
from click.testing import CliRunner
from eth_abi import encode as abi_encode
from eth_account import Account
from eth_account.messages import _hash_eip191_message, encode_typed_data
from eth_utils import keccak

from defios.chain import ChainError
from defios.cli import main
from defios.config import load_config
from defios.propose import (
    ProposeError,
    choose_nonce,
    propose,
    recover_signer,
    safe_tx_hash,
    sign_safe_tx_hash,
)
from defios.tx import Call, SafeTx

from .conftest import AVATAR, OPERATOR, ROLES

SAFE = "0x1111111111111111111111111111111111111111"
TO = "0x2222222222222222222222222222222222222222"
ZERO = "0x" + "00" * 20
KEY = "0x" + "11" * 32                      # throwaway test key
DELEGATE = Account.from_key(KEY).address
WETH = "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"   # catalogued (weth card)

# Known vector: chainId 1, Safe 0x1111…, to 0x2222…, value 1, data 0xdeadbeef, CALL, nonce 7, no refunds.
VECTOR = "7dfeb383629eefa0dabf8e2506d1e90fc432c9c0bc378addd417833edabced45"


def _tx():
    return SafeTx(to=TO, value=1, data=bytes.fromhex("deadbeef"), operation=0, nonce=7)


def test_safe_tx_hash_known_vector():
    assert safe_tx_hash(1, SAFE, _tx()).hex() == VECTOR


def test_safe_tx_hash_matches_eth_account_eip712():
    td = {"types": {
        "EIP712Domain": [{"name": "chainId", "type": "uint256"}, {"name": "verifyingContract", "type": "address"}],
        "SafeTx": [{"name": n, "type": t} for n, t in [
            ("to", "address"), ("value", "uint256"), ("data", "bytes"), ("operation", "uint8"),
            ("safeTxGas", "uint256"), ("baseGas", "uint256"), ("gasPrice", "uint256"), ("gasToken", "address"),
            ("refundReceiver", "address"), ("nonce", "uint256")]]},
        "primaryType": "SafeTx", "domain": {"chainId": 1, "verifyingContract": SAFE},
        "message": {"to": TO, "value": 1, "data": bytes.fromhex("deadbeef"), "operation": 0, "safeTxGas": 0,
                    "baseGas": 0, "gasPrice": 0, "gasToken": ZERO, "refundReceiver": ZERO, "nonce": 7}}
    assert _hash_eip191_message(encode_typed_data(full_message=td)).hex() == VECTOR


def test_safe_tx_hash_manual_encode():
    """Independent encode: hand-built 32-byte words instead of eth_abi."""
    w = lambda x: int(x, 16).to_bytes(32, "big") if isinstance(x, str) else int(x).to_bytes(32, "big")
    th = keccak(text="SafeTx(address to,uint256 value,bytes data,uint8 operation,uint256 safeTxGas,uint256 baseGas,"
                     "uint256 gasPrice,address gasToken,address refundReceiver,uint256 nonce)")
    struct = keccak(th + w(TO) + w(1) + keccak(bytes.fromhex("deadbeef")) + w(0) + w(0) + w(0) + w(0) + w(ZERO)
                    + w(ZERO) + w(7))
    dom = keccak(keccak(text="EIP712Domain(uint256 chainId,address verifyingContract)") + w(1) + w(SAFE))
    assert keccak(b"\x19\x01" + dom + struct).hex() == VECTOR
    assert abi_encode(["address"], [SAFE]) == w(SAFE)


def test_choose_nonce():
    assert choose_nonce(5, [])[0] == 5
    assert choose_nonce(5, [{"nonce": 5}, {"nonce": 6}, {"nonce": 6}])[0] == 7
    assert choose_nonce(5, [{"nonce": 3}])[0] == 5                         # stale entries ignored
    assert choose_nonce(5, [{"nonce": 9}])[0] == 10                        # never below the highest queued
    n, info = choose_nonce(5, [{"nonce": 5}], override=5)
    assert n == 5 and "REPLACES" in info["chosen_by"]
    with pytest.raises(ProposeError):
        choose_nonce(5, [], override=4)


def test_delegate_signature_recovers():
    h = safe_tx_hash(1, SAFE, _tx())
    signer, sig = sign_safe_tx_hash(h, KEY)
    assert signer == DELEGATE and int(sig[-2:], 16) in (31, 32)
    assert recover_signer(h, sig) == DELEGATE


class FakeService:
    def __init__(self, nonce=10, queued=(10, 11), owners=("0x" + "aa" * 20,), delegates=(DELEGATE,), fail=False):
        self.n, self.q, self.owners, self.dels, self.fail = nonce, list(queued), list(owners), list(delegates), fail
        self.posted = []

    def safe(self, safe):
        if self.fail:
            raise ChainError("down")
        return {"nonce": self.n, "owners": self.owners, "threshold": 1}

    def pending(self, safe, nonce):
        if self.fail:
            raise ChainError("down")
        return [{"nonce": q} for q in self.q]

    def delegates(self, safe):
        return [{"delegate": d} for d in self.dels]

    def propose(self, safe, payload):
        self.posted.append((safe, payload))
        return {"status": 201}


@pytest.fixture
def vault(agent_dir):
    return load_config(agent_dir).vault(None)


@pytest.fixture
def catalog():
    from defios.catalog import Catalog
    from defios.config import default_catalog_dir
    return Catalog.load(default_catalog_dir())


def test_dry_run_is_default_and_posts_nothing(vault, catalog):
    svc = FakeService()
    r = propose(vault, [Call(WETH, 0, "0xd0e30db0")], catalog=catalog, service=svc, private_key=KEY)
    assert r["mode"] == "dry-run" and r["posted"] is False and svc.posted == []
    assert r["nonce"]["chosen"] == 12                        # max(10, 11 + 1): no collision with the queue
    assert r["operator_tx"]["to"].lower() == ROLES.lower()
    assert r["payload"]["data"].startswith("0xc6fe8747")
    tx = SafeTx(to=r["operator_tx"]["to"], value=0, data=bytes.fromhex(r["operator_tx"]["data"][2:]), operation=0,
                nonce=12)
    assert r["safe_tx_hash"] == "0x" + safe_tx_hash(1, OPERATOR, tx).hex()
    assert recover_signer(bytes.fromhex(r["safe_tx_hash"][2:]), r["signature"]) == DELEGATE
    assert r["calls"][0]["contract"] == "weth.WETH"


def test_send_posts_once(vault, catalog):
    svc = FakeService()
    r = propose(vault, [Call(WETH, 0, "0xd0e30db0")], catalog=catalog, service=svc, private_key=KEY, send=True)
    assert r["posted"] and len(svc.posted) == 1
    safe, payload = svc.posted[0]
    assert safe == vault.operator and payload["nonce"] == "12" and payload["sender"] == DELEGATE
    assert payload["origin"] == "defios"


def test_never_signs_as_owner(vault, catalog):
    svc = FakeService(owners=(DELEGATE,))
    r = propose(vault, [Call(WETH, 0, "0xd0e30db0")], catalog=catalog, service=svc, private_key=KEY)
    assert r["signature"] is None and any("OWNER" in p for p in r["problems"])
    with pytest.raises(ProposeError):
        propose(vault, [Call(WETH, 0, "0xd0e30db0")], catalog=catalog, service=svc, private_key=KEY, send=True)
    assert svc.posted == []


def test_send_refuses_uncatalogued_target_and_non_delegate(vault, catalog):
    with pytest.raises(ProposeError, match="uncatalogued"):
        propose(vault, [Call(TO, 0, "0x")], catalog=catalog, service=FakeService(), private_key=KEY, send=True)
    with pytest.raises(ProposeError, match="not a delegate"):
        propose(vault, [Call(WETH, 0, "0xd0e30db0")], catalog=catalog, service=FakeService(delegates=()),
                private_key=KEY, send=True)
    with pytest.raises(ProposeError, match="PROPOSER_PRIVATE_KEY"):
        propose(vault, [Call(WETH, 0, "0xd0e30db0")], catalog=catalog, service=FakeService(), private_key="",
                send=True)


def test_service_down_needs_explicit_nonce(vault, catalog):
    with pytest.raises(ProposeError):
        propose(vault, [Call(WETH)], catalog=catalog, service=FakeService(fail=True), private_key="")
    r = propose(vault, [Call(WETH)], catalog=catalog, service=FakeService(fail=True), private_key="", nonce=3)
    assert r["nonce"]["chosen"] == 3 and r["problems"]


def test_cli_propose_dry_run_default(agent_dir, monkeypatch):
    import defios.propose as P
    svc = FakeService()
    monkeypatch.setattr(P, "TxService", lambda chain_id: svc)
    monkeypatch.delenv("PROPOSER_PRIVATE_KEY", raising=False)
    f = agent_dir / "calls.json"
    f.write_text(json.dumps({"description": "wrap 1 ETH", "chain_id": 1,
                             "calls": [{"to": WETH, "value": "1000000000000000000", "data": "0xd0e30db0"}]}))
    r = CliRunner().invoke(main, ["--root", str(agent_dir), "--format", "json", "propose", "--file", str(f)],
                           catch_exceptions=False)
    assert r.exit_code == 0, r.output
    out = json.loads(r.output)
    assert out["mode"] == "dry-run" and not out["posted"] and svc.posted == []
    assert out["avatar"].lower() == AVATAR.lower() and out["nonce"]["chosen"] == 12
    r2 = CliRunner().invoke(main, ["--root", str(agent_dir), "--format", "text", "propose", "--file", str(f), "--send"])
    assert r2.exit_code == 2 and svc.posted == []            # no key -> refused
