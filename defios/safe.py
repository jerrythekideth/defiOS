"""Safe reads (RPC) and Safe Transaction Service reads (queue, delegates, messages)."""
from __future__ import annotations

import os
from typing import Any

import requests
from eth_utils import to_checksum_address

from .chain import ChainError, Rpc
from .config import SAFE_TX_SERVICE

SENTINEL = "0x0000000000000000000000000000000000000001"
GUARD_SLOT = "0x4a204f620c8c5ccdca3fd54d003badd85ba500436a431f0cbda4f558c93c34c8"


def safe_info(rpc: Rpc, safe: str) -> dict[str, Any]:
    out: dict[str, Any] = {"address": to_checksum_address(safe)}
    try:
        out["version"] = rpc.call(safe, "VERSION()", [], ["string"])
    except ChainError as e:
        out["error"] = f"not a Safe? {e}"
        return out
    out["owners"] = [to_checksum_address(a) for a in rpc.call(safe, "getOwners()", [], ["address[]"])]
    out["threshold"] = rpc.call(safe, "getThreshold()", [], ["uint256"])
    out["nonce"] = rpc.call(safe, "nonce()", [], ["uint256"])
    mods, _next = rpc.call(safe, "getModulesPaginated(address,uint256)", [SENTINEL, 50], ["address[]", "address"])
    out["modules"] = [to_checksum_address(m) for m in mods]
    guard = rpc.get_storage_at(safe, GUARD_SLOT)
    g = "0x" + guard[-20:].hex() if guard else "0x" + "00" * 20
    out["guard"] = None if int(g, 16) == 0 else to_checksum_address(g)
    return out


class TxService:
    """Safe Transaction Service (api.safe.global). SAFE_API_KEY is sent as a bearer token when set."""

    def __init__(self, chain_id: int, api_key: str | None = None, base: str | None = None, timeout: int = 20):
        short = SAFE_TX_SERVICE.get(chain_id)
        if not short and not base:
            raise ChainError(f"no Safe Transaction Service known for chain {chain_id}")
        self.base = base or f"https://api.safe.global/tx-service/{short}/api"
        self.api_key = api_key if api_key is not None else os.environ.get("SAFE_API_KEY", "")
        self.timeout = timeout

    def _get(self, path: str, params: dict | None = None) -> Any:
        try:
            r = requests.get(self.base + path, params=params or {}, headers=self._headers(), timeout=self.timeout)
        except requests.RequestException as e:
            raise ChainError(f"safe tx service: {e}") from e
        if r.status_code >= 400:
            raise ChainError(f"safe tx service {r.status_code}: {r.text[:200]}")
        return r.json()

    def _headers(self) -> dict:
        h = {"Accept": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    def safe(self, safe: str) -> dict:
        """Service view of a Safe: nonce (next unused on-chain nonce), owners, threshold, version."""
        d = self._get(f"/v1/safes/{to_checksum_address(safe)}/")
        return {"nonce": int(d["nonce"]), "owners": [to_checksum_address(o) for o in d.get("owners", [])],
                "threshold": d.get("threshold"), "version": d.get("version")}

    def propose(self, safe: str, payload: dict) -> dict:
        """POST a multisig transaction proposal (signed by an owner or a delegate of `safe`)."""
        try:
            r = requests.post(f"{self.base}/v1/safes/{to_checksum_address(safe)}/multisig-transactions/",
                              json=payload, headers=self._headers(), timeout=self.timeout)
        except requests.RequestException as e:
            raise ChainError(f"safe tx service: {e}") from e
        if r.status_code not in (200, 201):
            raise ChainError(f"safe tx service {r.status_code}: {r.text[:300]}")
        return {"status": r.status_code}

    def pending(self, safe: str, current_nonce: int) -> list[dict]:
        d = self._get(f"/v1/safes/{to_checksum_address(safe)}/multisig-transactions/",
                      {"executed": "false", "nonce__gte": current_nonce, "ordering": "nonce", "limit": 100})
        out = []
        for tx in d.get("results", []):
            out.append({
                "nonce": tx.get("nonce"),
                "safeTxHash": tx.get("safeTxHash"),
                "to": tx.get("to"),
                "value": tx.get("value"),
                "method": (tx.get("dataDecoded") or {}).get("method"),
                "confirmations": len(tx.get("confirmations") or []),
                "confirmationsRequired": tx.get("confirmationsRequired"),
                "proposer": tx.get("proposer"),
                "submissionDate": tx.get("submissionDate"),
            })
        return out

    def delegates(self, safe: str) -> list[dict]:
        d = self._get("/v2/delegates/", {"safe": to_checksum_address(safe)})
        return [{"delegate": x.get("delegate"), "delegator": x.get("delegator"), "label": x.get("label")}
                for x in d.get("results", [])]

    def messages(self, safe: str) -> list[dict]:
        d = self._get(f"/v1/safes/{to_checksum_address(safe)}/messages/")
        return [{"messageHash": m.get("messageHash"), "confirmations": len(m.get("confirmations") or []),
                 "created": m.get("created")} for m in d.get("results", [])]


def queue_summary(pending: list[dict], current_nonce: int) -> dict:
    """Flags stale entries (nonce already used) and duplicates, and the next free nonce."""
    nonces = [p["nonce"] for p in pending if p.get("nonce") is not None]
    dup = sorted({n for n in nonces if nonces.count(n) > 1})
    nxt = current_nonce
    while nxt in nonces:
        nxt += 1
    return {"current_nonce": current_nonce, "next_free_nonce": nxt, "pending": pending,
            "duplicate_nonces": dup}
