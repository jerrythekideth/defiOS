"""Log sources with fake clients (no network)."""
from defios.chain import ChainError
from defios.logs import EtherscanLogs, RpcLogs


def mk(block, idx):
    return {"blockNumber": hex(block), "transactionHash": f"0x{block:064x}", "transactionIndex": "0x0",
            "logIndex": hex(idx), "topics": ["0x00"], "data": "0x"}


class FakeRpc:
    def __init__(self, logs, max_range):
        self.logs, self.max_range, self.calls = logs, max_range, []

    def block_number(self):
        return 1000

    def get_logs(self, address, topic0, a, b):
        self.calls.append((a, b))
        if b - a + 1 > self.max_range:
            raise ChainError("query returned more than 10000 results / range too large")
        return [x for x in self.logs if a <= int(x["blockNumber"], 16) <= b]


def test_rpc_logs_chunked_with_adaptive_halving():
    logs = [mk(b, 0) for b in range(0, 1000, 7)]
    rpc = FakeRpc(logs, max_range=150)
    out = RpcLogs(rpc, chunk=400).fetch("0x0", 0, 999)
    assert [x["blockNumber"] for x in out] == list(range(0, 1000, 7))
    assert any(b - a + 1 <= 150 for a, b in rpc.calls)


class FakeEtherscan:
    def __init__(self, logs):
        self.logs = logs

    def get(self, **q):
        a, b = int(q["fromBlock"]), int(q["toBlock"])
        res = [x for x in self.logs if a <= int(x["blockNumber"], 16) <= b][: int(q["offset"])]
        return {"status": "1" if res else "0", "message": "OK" if res else "No records found", "result": res}

    def block_number(self):
        return 10_000


def test_etherscan_pagination_dedupes_boundary_block():
    logs = [mk(b, i) for b in range(100, 2600) for i in range(1)]  # 2500 logs
    src = EtherscanLogs(FakeEtherscan(logs))
    src.PAGE = 1000
    out = src.fetch("0x0", 0, 10_000)
    assert len(out) == 2500
    assert out[0]["blockNumber"] == 100 and out[-1]["blockNumber"] == 2599
