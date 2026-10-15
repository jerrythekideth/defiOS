"""Log sources for event replay: Etherscan v2 getLogs or plain JSON-RPC eth_getLogs.

Both return normalized logs:
    {"blockNumber": int, "transactionHash": str, "transactionIndex": int,
     "logIndex": int, "topics": [str], "data": str}
sorted by (blockNumber, transactionIndex, logIndex).
"""
from __future__ import annotations

import sys
from typing import Protocol

from .chain import ChainError, Etherscan, Rpc


def _int(v) -> int:
    if isinstance(v, int):
        return v
    if v in (None, "", "0x"):
        return 0
    return int(v, 16) if str(v).startswith("0x") else int(v)


def normalize(log: dict) -> dict:
    return {
        "address": (log.get("address") or "").lower(),
        "blockNumber": _int(log.get("blockNumber")),
        "transactionHash": log.get("transactionHash"),
        "transactionIndex": _int(log.get("transactionIndex")),
        "logIndex": _int(log.get("logIndex")),
        "topics": list(log.get("topics") or []),
        "data": log.get("data") or "0x",
    }


def sort_logs(logs: list[dict]) -> list[dict]:
    seen, out = set(), []
    for lg in logs:
        k = (lg["transactionHash"], lg["logIndex"])
        if k in seen:
            continue
        seen.add(k)
        out.append(lg)
    return sorted(out, key=lambda x: (x["blockNumber"], x["transactionIndex"], x["logIndex"]))


class LogSource(Protocol):
    name: str

    def latest_block(self) -> int: ...

    def fetch(self, address: str, from_block: int, to_block: int) -> list[dict]: ...


class EtherscanLogs:
    name = "etherscan"
    PAGE = 1000

    def __init__(self, client: Etherscan):
        self.client = client

    def latest_block(self) -> int:
        return self.client.block_number()

    def fetch(self, address: str, from_block: int, to_block: int) -> list[dict]:
        out: list[dict] = []
        cur = from_block
        while True:
            d = self.client.get(module="logs", action="getLogs", address=address,
                                fromBlock=str(cur), toBlock=str(to_block),
                                page="1", offset=str(self.PAGE))
            res = d.get("result")
            if d.get("status") != "1":
                if isinstance(res, list) and not res:
                    break
                if "no records" in str(d.get("message", "")).lower():
                    break
                raise ChainError(f"etherscan getLogs: {d.get('message')} {res}")
            batch = [normalize(x) for x in res]
            out.extend(batch)
            if len(batch) < self.PAGE:
                break
            last = max(b["blockNumber"] for b in batch)
            if last == cur:  # >1000 logs in one block: give up loudly
                raise ChainError(f"more than {self.PAGE} logs in block {cur}; use RPC source")
            cur = last  # re-fetch last block; duplicates removed by sort_logs
        return sort_logs(out)


class RpcLogs:
    name = "rpc"

    def __init__(self, rpc: Rpc, chunk: int = 10_000):
        self.rpc = rpc
        self.chunk = chunk

    def latest_block(self) -> int:
        return self.rpc.block_number()

    def find_deploy_block(self, address: str, latest: int) -> int:
        """Binary search the first block with code at `address` (needs an archive node)."""
        lo, hi = 0, latest
        if not self.rpc.get_code(address, latest):
            raise ChainError(f"no code at {address}")
        try:
            while lo < hi:
                mid = (lo + hi) // 2
                if self.rpc.get_code(address, mid):
                    hi = mid
                else:
                    lo = mid + 1
            return lo
        except ChainError as e:
            print(f"[defios] deploy-block search failed ({e}); scanning from 0", file=sys.stderr)
            return 0

    def fetch(self, address: str, from_block: int, to_block: int) -> list[dict]:
        out: list[dict] = []
        start, size = from_block, self.chunk
        while start <= to_block:
            end = min(start + size - 1, to_block)
            try:
                batch = self.rpc.get_logs(address, None, start, end)
            except ChainError as e:
                if size > 100:
                    size //= 2
                    continue
                raise ChainError(f"eth_getLogs {start}-{end}: {e}") from e
            out.extend(normalize(x) for x in batch)
            start = end + 1
            if size < self.chunk:
                size = min(self.chunk, size * 2)
        return sort_logs(out)


class StaticLogs:
    """Fixture source used by tests and `--logs-file`."""

    name = "file"

    def __init__(self, logs: list[dict], latest: int | None = None):
        self.logs = sort_logs([normalize(x) for x in logs])
        self._latest = latest if latest is not None else (self.logs[-1]["blockNumber"] if self.logs else 0)

    def latest_block(self) -> int:
        return self._latest

    def fetch(self, address: str, from_block: int, to_block: int) -> list[dict]:
        a = address.lower()
        return [x for x in self.logs
                if from_block <= x["blockNumber"] <= to_block and (not x["address"] or x["address"] == a)]
