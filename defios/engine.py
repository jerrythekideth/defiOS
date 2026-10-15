"""Wiring shared by the commands: RPC, log source, Roles adapter, and the
always-from-chain role read."""
from __future__ import annotations

import json
import sys
from pathlib import Path

from .chain import ChainError, Etherscan, Rpc, env
from .config import VaultConfig
from .logs import EtherscanLogs, RpcLogs, StaticLogs
from .roles import detect_version, get_adapter
from .roles.base import RoleState, RolesAdapter


def warn(msg: str) -> None:
    print(f"[defios] {msg}", file=sys.stderr)


def make_rpc() -> Rpc | None:
    url = env("RPC_URL")
    return Rpc(url) if url else None


def make_log_source(vault: VaultConfig, rpc: Rpc | None, logs_file: str | None = None):
    if logs_file:
        data = json.loads(Path(logs_file).read_text())
        logs = data["logs"] if isinstance(data, dict) else data
        latest = data.get("latest_block") if isinstance(data, dict) else None
        return StaticLogs(logs, latest)
    choice = env("LOG_SOURCE", "auto").lower()
    key = env("ETHERSCAN_API_KEY")
    if choice == "etherscan" or (choice == "auto" and key):
        return EtherscanLogs(Etherscan(key, vault.chain_id))
    if choice in ("rpc", "auto") and rpc is not None:
        return RpcLogs(rpc, int(env("LOGS_CHUNK", "10000")))
    raise ChainError("no log source: set ETHERSCAN_API_KEY or RPC_URL (or pass --logs-file)")


def resolve_version(vault: VaultConfig, rpc: Rpc | None) -> tuple[str, dict | None]:
    if vault.roles_version != "auto":
        return vault.roles_version, None
    if rpc is None:
        return "v2", {"note": "no RPC_URL: assumed v2 (set roles_version or RPC_URL to detect)"}
    det = detect_version(rpc, vault.roles_modifier)
    if not det["version"]:
        raise ChainError(f"could not detect the Roles version at {vault.roles_modifier}: {det['evidence']}")
    return det["version"], det


def read_role(vault: VaultConfig, logs_file: str | None = None, rpc: Rpc | None = None,
              to_block: int | None = None) -> tuple[RoleState, RolesAdapter, dict | None]:
    """Rebuild the role from chain at the latest block (or `to_block`). Never reads a cached lock."""
    version, detection = resolve_version(vault, None if logs_file else rpc)
    adapter = get_adapter(version, vault.roles_modifier, vault.chain_id)
    src = make_log_source(vault, rpc, logs_file)
    from_block = vault.roles_from_block
    if isinstance(src, RpcLogs) and not from_block:
        latest = src.latest_block()
        from_block = src.find_deploy_block(vault.roles_modifier, latest)
        warn(f"roles_from_block not set; found deployment at block {from_block}. "
             f"Add `roles_from_block: {from_block}` to defios.yaml to skip this search.")
        to_block = to_block or latest
    state = adapter.read_role(vault.role_key, src, to_block=to_block, from_block=from_block)
    if state.module.get("avatar") is None:
        state.module["avatar_from_config"] = vault.avatar
    return state, adapter, detection
