"""`defios doctor`: check the wiring between Vault, Roles modifier, Operator and proposer."""
from __future__ import annotations

import os
from typing import Any

from .chain import ChainError, Rpc
from .config import VaultConfig
from .engine import read_role
from .roles import detect_version
from .safe import TxService, safe_info

ENV_KEYS = ["RPC_URL", "ETHERSCAN_API_KEY", "SAFE_API_KEY", "PROPOSER_PRIVATE_KEY"]


def _ok(cond: bool, ok_msg: str, bad_msg: str, severity: str = "error") -> dict:
    return {"ok": bool(cond), "severity": "ok" if cond else severity, "message": ok_msg if cond else bad_msg}


def doctor(vault: VaultConfig, rpc: Rpc | None, logs_file: str | None = None) -> dict:
    checks: list[dict] = []
    out: dict[str, Any] = {"vault": vault.name, "checks": checks}
    out["env"] = {k: bool(os.environ.get(k)) for k in ENV_KEYS}  # presence only, never values
    if not out["env"]["RPC_URL"]:
        checks.append(_ok(False, "", "RPC_URL not set: doctor can only check config", "error"))
    if not (out["env"]["ETHERSCAN_API_KEY"] or out["env"]["RPC_URL"]):
        checks.append(_ok(False, "", "no log source: set ETHERSCAN_API_KEY or RPC_URL"))
    if rpc is None:
        return out

    try:
        cid = rpc.chain_id()
        checks.append(_ok(cid == vault.chain_id, f"RPC chain id {cid}", f"RPC chain id {cid} != configured {vault.chain_id}"))
    except ChainError as e:
        checks.append(_ok(False, "", f"RPC unreachable: {e}"))
        return out

    # Vault Safe
    try:
        v = safe_info(rpc, vault.avatar)
        out["vault_safe"] = v
        mods = [m.lower() for m in v.get("modules", [])]
        checks.append(_ok(vault.roles_modifier.lower() in mods,
                          "Roles modifier is an enabled module on the Vault",
                          "Roles modifier is NOT enabled on the Vault"))
        others = [m for m in v.get("modules", []) if m.lower() != vault.roles_modifier.lower()]
        if others:
            checks.append(_ok(False, "", f"Vault has other modules enabled: {others}", "warning"))
        if v.get("guard"):
            checks.append(_ok(False, "", f"Vault has a guard set: {v['guard']} (check it allows module txs)", "warning"))
    except ChainError as e:
        checks.append(_ok(False, "", f"Vault Safe read failed: {e}"))

    # Roles modifier
    det = detect_version(rpc, vault.roles_modifier)
    out["roles_version"] = det
    checks.append(_ok(bool(det["version"]), f"Roles modifier version {det['version']}",
                      "could not detect Roles version"))
    if det["version"] and vault.roles_version not in ("auto", det["version"]):
        checks.append(_ok(False, "", f"defios.yaml says {vault.roles_version}, chain says {det['version']}"))
    try:
        owner = rpc.call(vault.roles_modifier, "owner()", [], ["address"])
        avatar = rpc.call(vault.roles_modifier, "avatar()", [], ["address"])
        target = rpc.call(vault.roles_modifier, "target()", [], ["address"])
        out["roles_modifier"] = {"owner": owner, "avatar": avatar, "target": target}
        checks.append(_ok(avatar.lower() == vault.avatar.lower(), "Roles avatar is the Vault",
                          f"Roles avatar {avatar} is not the Vault"))
        checks.append(_ok(target.lower() == vault.avatar.lower(), "Roles target is the Vault",
                          f"Roles target {target} is not the Vault"))
        checks.append(_ok(owner.lower() == vault.avatar.lower(),
                          "Vault owns the Roles modifier (permission changes = Vault txs)",
                          f"Roles owner is {owner}, not the Vault", "warning"))
        if vault.operator:
            enabled = rpc.call(vault.roles_modifier, "isModuleEnabled(address)", [vault.operator], ["bool"])
            checks.append(_ok(enabled, "Operator is an enabled module on the Roles modifier",
                              "Operator is NOT an enabled module on the Roles modifier"))
    except ChainError as e:
        checks.append(_ok(False, "", f"Roles modifier read failed: {e}"))

    # role membership (from events: v2 has no members getter)
    if vault.operator:
        try:
            state, _, _ = read_role(vault, logs_file=logs_file, rpc=rpc)
            out["role"] = {"block": state.block, "members": state.members,
                           "targets": sum(1 for t in state.targets.values() if t.clearance)}
            checks.append(_ok(vault.operator in state.members, "Operator is a member of the role",
                              f"Operator is not a member of {state.module.get('role_key_label')}"))
        except ChainError as e:
            checks.append(_ok(False, "", f"role replay failed: {e}"))

    # Operator Safe + proposer delegate
    if vault.operator:
        try:
            o = safe_info(rpc, vault.operator)
            out["operator_safe"] = o
            if vault.proposer:
                is_owner = vault.proposer.lower() in [x.lower() for x in o.get("owners", [])]
                if is_owner:
                    checks.append(_ok(False, "", "proposer is an OWNER of the Operator; it should only be a delegate",
                                      "warning"))
        except ChainError as e:
            checks.append(_ok(False, "", f"Operator Safe read failed: {e}"))
        if vault.proposer:
            try:
                dels = TxService(vault.chain_id).delegates(vault.operator)
                out["operator_delegates"] = dels
                checks.append(_ok(any((d.get("delegate") or "").lower() == vault.proposer.lower() for d in dels),
                                  "proposer is a delegate on the Operator Safe",
                                  "proposer is NOT a delegate on the Operator Safe (cannot queue txs)"))
            except ChainError as e:
                checks.append(_ok(False, "", f"Safe Tx Service delegates read failed: {e}", "warning"))
            try:
                vdels = TxService(vault.chain_id).delegates(vault.avatar)
                if any((d.get("delegate") or "").lower() == vault.proposer.lower() for d in vdels):
                    checks.append(_ok(False, "", "proposer is also a delegate on the Vault: permission batches "
                                                  "could be proposed directly (optional)", "info"))
            except ChainError:
                pass
    return out


def summarize(result: dict) -> str:
    lines = [f"doctor {result['vault']}"]
    for k, present in result.get("env", {}).items():
        lines.append(f"  env {k}: {'set' if present else 'missing'}")
    for c in result["checks"]:
        mark = {"ok": "ok  ", "error": "FAIL", "warning": "warn", "info": "info"}[c["severity"]]
        lines.append(f"  [{mark}] {c['message']}")
    return "\n".join(lines)
