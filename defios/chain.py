"""Thin chain access: JSON-RPC (eth_call, eth_getLogs, ...) and the Etherscan v2 API.

Kept dependency-light on purpose (requests + eth_abi); web3 is only used for
checksumming via eth_utils.
"""
from __future__ import annotations

import os
import time
from typing import Any, Iterable

import requests
from eth_abi import decode as abi_decode
from eth_abi import encode as abi_encode
from eth_utils import keccak, to_checksum_address

ETHERSCAN_API = "https://api.etherscan.io/v2/api"


class ChainError(RuntimeError):
    pass


class CallReverted(ChainError):
    def __init__(self, data: bytes, message: str = ""):
        super().__init__(message or f"execution reverted: 0x{data.hex()}")
        self.data = data


def selector(sig: str) -> bytes:
    return keccak(text=sig)[:4]


class Rpc:
    """Minimal JSON-RPC client."""

    def __init__(self, url: str, timeout: int = 30):
        if not url:
            raise ChainError("RPC_URL is not set")
        self.url = url
        self.timeout = timeout
        self._id = 0

    def request(self, method: str, params: list) -> Any:
        self._id += 1
        body = {"jsonrpc": "2.0", "id": self._id, "method": method, "params": params}
        for attempt in range(3):
            try:
                r = requests.post(self.url, json=body, timeout=self.timeout)
                r.raise_for_status()
                out = r.json()
                break
            except (requests.RequestException, ValueError) as e:
                if attempt == 2:
                    raise ChainError(f"{method}: {e}") from e
                time.sleep(1 + attempt)
        if "error" in out:
            err = out["error"]
            data = err.get("data")
            if isinstance(data, dict):
                data = data.get("data")
            if method == "eth_call" and (data or "revert" in str(err.get("message", "")).lower()):
                raw = bytes.fromhex(data[2:]) if isinstance(data, str) and data.startswith("0x") else b""
                raise CallReverted(raw, str(err.get("message")))
            raise ChainError(f"{method}: {err}")
        return out["result"]

    # --- helpers -----------------------------------------------------------
    def block_number(self) -> int:
        return int(self.request("eth_blockNumber", []), 16)

    def chain_id(self) -> int:
        return int(self.request("eth_chainId", []), 16)

    def get_code(self, address: str, block: str | int = "latest") -> bytes:
        b = hex(block) if isinstance(block, int) else block
        return bytes.fromhex(self.request("eth_getCode", [address, b])[2:])

    def get_storage_at(self, address: str, slot: str, block: str | int = "latest") -> bytes:
        b = hex(block) if isinstance(block, int) else block
        return bytes.fromhex(self.request("eth_getStorageAt", [address, slot, b])[2:])

    def call_raw(self, to: str, data: bytes, block: str | int = "latest", sender: str | None = None,
                 value: int = 0) -> bytes:
        b = hex(block) if isinstance(block, int) else block
        tx: dict[str, Any] = {"to": to, "data": "0x" + data.hex()}
        if sender:
            tx["from"] = sender
        if value:
            tx["value"] = hex(value)
        return bytes.fromhex(self.request("eth_call", [tx, b])[2:])

    def call(self, to: str, sig: str, args: Iterable = (), out_types: list[str] | None = None,
             block: str | int = "latest", sender: str | None = None) -> Any:
        """sig like 'balanceOf(address)'; out_types like ['uint256']."""
        name, _, rest = sig.partition("(")
        in_types = _split_types(rest[:-1]) if rest[:-1] else []
        data = selector(sig) + abi_encode(in_types, list(args))
        raw = self.call_raw(to, data, block=block, sender=sender)
        if not out_types:
            return raw
        vals = abi_decode(out_types, raw)
        return vals[0] if len(vals) == 1 else vals

    def get_logs(self, address: str, topic0: str | None, from_block: int, to_block: int) -> list[dict]:
        flt: dict[str, Any] = {"address": address, "fromBlock": hex(from_block), "toBlock": hex(to_block)}
        if topic0:
            flt["topics"] = [topic0]
        return self.request("eth_getLogs", [flt])


def _split_types(s: str) -> list[str]:
    out, depth, cur = [], 0, ""
    for ch in s:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            out.append(cur)
            cur = ""
        else:
            cur += ch
    if cur:
        out.append(cur)
    return out


class Etherscan:
    """Etherscan v2 (multichain) API."""

    def __init__(self, api_key: str, chain_id: int = 1, timeout: int = 30):
        if not api_key:
            raise ChainError("ETHERSCAN_API_KEY is not set")
        self.api_key = api_key
        self.chain_id = chain_id
        self.timeout = timeout

    def get(self, **params) -> dict:
        q = {"chainid": str(self.chain_id), "apikey": self.api_key, **params}
        for attempt in range(4):
            try:
                r = requests.get(ETHERSCAN_API, params=q, timeout=self.timeout)
                d = r.json()
            except (requests.RequestException, ValueError) as e:
                if attempt == 3:
                    raise ChainError(f"etherscan: {e}") from e
                time.sleep(1 + attempt)
                continue
            msg = str(d.get("result", ""))
            if d.get("status") == "0" and "rate limit" in msg.lower():
                time.sleep(1.5)
                continue
            return d
        raise ChainError("etherscan: rate limited")

    def block_number(self) -> int:
        d = self.get(module="proxy", action="eth_blockNumber")
        return int(d["result"], 16)


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def checksum(a: str) -> str:
    return to_checksum_address(a)
