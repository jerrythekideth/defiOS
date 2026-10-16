from defios.config import apply_env_aliases


def test_aliases_fill_unset(monkeypatch):
    monkeypatch.delenv("RPC_URL", raising=False)
    monkeypatch.delenv("PROPOSER_PRIVATE_KEY", raising=False)
    monkeypatch.setenv("ETH_RPC_URL", "https://rpc.example")
    monkeypatch.setenv("ETH_PRIVATE_KEY", "0xabc")
    apply_env_aliases()
    import os
    assert os.environ["RPC_URL"] == "https://rpc.example"
    assert os.environ["PROPOSER_PRIVATE_KEY"] == "0xabc"


def test_aliases_never_override(monkeypatch):
    monkeypatch.setenv("RPC_URL", "https://mine")
    monkeypatch.setenv("ETH_RPC_URL", "https://other")
    apply_env_aliases()
    import os
    assert os.environ["RPC_URL"] == "https://mine"
