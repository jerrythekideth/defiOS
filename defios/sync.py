"""`defios sync`: rebuild a vault's book from chain.

Writes, into the operator's own agent repo:
    books/<vault>/role.lock.json   full decoded role at block N (snapshot for diffs)
    books/<vault>/book.yaml        what the vault uses/holds/can do, per-action status
"""
from __future__ import annotations

import json
import time
from typing import Any

import yaml
from eth_abi import decode as abi_decode
from eth_abi import encode as abi_encode
from eth_utils import is_address

from .catalog import Catalog, CardError, parse_signature
from .chain import ChainError, Rpc
from .check import detect_used_protocols, run_check
from .config import Config, VaultConfig
from .engine import read_role
from .safe import TxService, queue_summary, safe_info


def _read_arg(spec: Any, catalog: Catalog, proto: str, vault: VaultConfig) -> Any:
    if spec == "$AVATAR":
        return vault.avatar
    if isinstance(spec, int):
        return spec
    if isinstance(spec, str):
        if spec.isdigit():
            return int(spec)
        if is_address(spec) or catalog.is_contract_ref(spec, proto) or spec.startswith("$book."):
            a = catalog.resolve_contract(spec, proto, vault.book_vars)
            if not a:
                raise CardError(f"{spec} unresolved")
            return a
    raise CardError(f"unsupported read arg {spec!r}")


def run_reads(catalog: Catalog, protocols: list[str], vault: VaultConfig, rpc: Rpc | None,
              block: int | str = "latest") -> dict:
    out: dict[str, Any] = {}
    for proto in protocols:
        card = catalog.cards.get(proto)
        if not card:
            continue
        res: dict[str, Any] = {}
        for r in card.reads:
            rid = r.get("id") or r.get("sig")
            try:
                target = catalog.resolve_contract(r["target"], proto, vault.book_vars)
                if not target:
                    res[rid] = {"skipped": "target address unresolved (TODO or book_vars)"}
                    continue
                sig = parse_signature(r["sig"])
                if rpc is None:
                    res[rid] = {"skipped": "no RPC_URL"}
                    continue
                args = [_read_arg(a, catalog, proto, vault) for a in (r.get("args") or [])]
                data = bytes.fromhex(sig.selector[2:]) + abi_encode([p.type for p in sig.params], args)
                raw = rpc.call_raw(target, data, block=block)
                vals = abi_decode([o.type for o in sig.outputs], raw) if sig.outputs else []
                vals = [v.hex() if isinstance(v, bytes) else v for v in vals]
                entry: dict[str, Any] = {"raw": vals[0] if len(vals) == 1 else list(vals)}
                dec = r.get("decimals")
                if dec is not None and len(vals) == 1 and isinstance(vals[0], int):
                    entry["value"] = vals[0] / 10**dec
                res[rid] = entry
            except (ChainError, CardError, ValueError, KeyError) as e:
                res[rid] = {"error": str(e)[:200]}
        out[proto] = res
    return out


def diff_locks(old: dict | None, new: dict) -> dict:
    if not old:
        return {"first_sync": True}
    def fmap(lock):
        m = {}
        for t, e in (lock.get("targets") or {}).items():
            m[t] = json.dumps({k: v for k, v in e.items() if k not in ("functions", "changed_at")}, sort_keys=True)
            for sel, f in (e.get("functions") or {}).items():
                m[f"{t}:{sel}"] = json.dumps({k: v for k, v in f.items() if k != "changed_at"}, sort_keys=True)
        return m
    a, b = fmap(old), fmap(new)
    return {
        "since_block": old.get("block"),
        "added": sorted(set(b) - set(a)),
        "removed": sorted(set(a) - set(b)),
        "changed": sorted(k for k in set(a) & set(b) if a[k] != b[k]),
        "members_added": sorted(set(new.get("members", [])) - set(old.get("members", []))),
        "members_removed": sorted(set(old.get("members", [])) - set(new.get("members", []))),
    }


def fetch_queues(vault: VaultConfig, rpc: Rpc | None) -> dict:
    out: dict[str, Any] = {}
    if rpc is None:
        return {"skipped": "no RPC_URL"}
    try:
        svc = TxService(vault.chain_id)
    except ChainError as e:
        return {"skipped": str(e)}
    for label, addr in (("vault", vault.avatar), ("operator", vault.operator)):
        if not addr:
            continue
        try:
            nonce = safe_info(rpc, addr).get("nonce", 0)
            out[label] = queue_summary(svc.pending(addr, nonce), nonce)
        except ChainError as e:
            out[label] = {"error": str(e)[:200]}
    return out


def sync(cfg: Config, vault: VaultConfig, catalog: Catalog, rpc: Rpc | None = None,
         logs_file: str | None = None, probe: bool = True, write: bool = True,
         with_queues: bool = True) -> dict:
    state, adapter, detection = read_role(vault, logs_file=logs_file, rpc=rpc)
    lock = state.to_lock_json()
    if detection:
        lock["version_detection"] = detection
    protocols = vault.protocols or detect_used_protocols(catalog, state, vault.book_vars)
    check = run_check(catalog, state, adapter, vault, protocols, rpc if (probe and rpc) else None)
    reads = run_reads(catalog, protocols, vault, None if logs_file else rpc, block=state.block)
    queues = fetch_queues(vault, rpc) if (with_queues and not logs_file) else {"skipped": "offline"}
    if rpc is not None and not logs_file:
        from .balances import vault_balances
        balances: dict[str, Any] = vault_balances(catalog, protocols, vault, rpc, block=state.block)
    else:
        balances = {"skipped": "no RPC_URL" if rpc is None else "offline (--logs-file)"}

    book_dir = cfg.books_dir / vault.name
    lock_path = book_dir / "role.lock.json"
    old = json.loads(lock_path.read_text()) if lock_path.exists() else None
    changes = diff_locks(old, lock)

    permissions = {}
    for proto, acts in check["actions"].items():
        if not isinstance(acts, dict):
            continue
        for aname, e in acts.items():
            if isinstance(e, dict) and "status" in e:
                reason = next((c.get("reason") for c in e.get("calls", []) if c.get("status") == e["status"]), None)
                permissions[f"{proto}.{aname}"] = {"status": e["status"], **({"why": reason} if reason else {})}

    index = catalog.address_index(vault.book_vars)
    uses: dict[str, Any] = {}
    for proto in protocols:
        card = catalog.cards.get(proto)
        if not card:
            continue
        contracts = {}
        for name in card.contracts:
            try:
                a = catalog.resolve_contract(name, proto, vault.book_vars)
            except CardError:
                a = None
            if a and state.target(a) is not None:
                contracts[name] = a
        uses[proto] = {"contracts_in_role": contracts, "reads": reads.get(proto, {})}

    book = {
        "schema": "defios.book/1",
        "generated_by": "defios sync (do not edit by hand; re-run sync)",
        "vault": vault.name,
        "chain_id": vault.chain_id,
        "synced_at": {"block": state.block, "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                      "log_source": state.log_source},
        "avatar": vault.avatar,
        "roles_modifier": vault.roles_modifier,
        "roles_version": state.version,
        "role_key": state.role_key,
        "operator": vault.operator,
        "role": {
            "members": state.members,
            "targets": sum(1 for t in state.targets.values() if t.clearance),
            "functions": len(state.active_functions()),
            "last_change": state.last_change,
            "changes_since_last_sync": changes,
        },
        "uses": uses,
        "balances": balances,
        "permissions": permissions,
        "lints": [{k: l[k] for k in ("severity", "code", "message", "target_name", "target", "function", "selector")
                   if k in l} for l in check["lints"] if l["severity"] in ("high", "medium", "low")],
        "queue": queues,
        "uncatalogued_targets": sorted(
            t.address for a, t in state.targets.items() if t.clearance and a not in index),
    }
    if state.module.get("avatar") and state.module["avatar"].lower() != vault.avatar.lower():
        book["warnings"] = [f"Roles avatar {state.module['avatar']} != configured avatar {vault.avatar}"]
    if write:
        book_dir.mkdir(parents=True, exist_ok=True)
        lock_path.write_text(json.dumps(lock, indent=2, default=str) + "\n")
        (book_dir / "book.yaml").write_text(
            "# Generated by `defios sync`. Do not edit; every command re-reads the chain.\n"
            + yaml.safe_dump(book, sort_keys=False, default_flow_style=False, width=120))
    return {"book": book, "lock": lock, "check": check, "paths": {
        "book": str(book_dir / "book.yaml"), "lock": str(lock_path)} if write else {}}


def summarize(result: dict) -> str:
    b = result["book"]
    lines = [f"synced {b['vault']} at block {b['synced_at']['block']} ({b['roles_version']}, "
             f"{b['role']['targets']} targets, {b['role']['functions']} functions, "
             f"members: {', '.join(b['role']['members']) or 'none'})"]
    ch = b["role"]["changes_since_last_sync"]
    if not ch.get("first_sync"):
        n = len(ch["added"]) + len(ch["removed"]) + len(ch["changed"])
        lines.append(f"  role changes since block {ch['since_block']}: {n}")
    for proto, u in b["uses"].items():
        vals = [f"{k}={v.get('value', v.get('raw'))}" for k, v in u["reads"].items()
                if isinstance(v, dict) and ("value" in v or "raw" in v)]
        if vals:
            lines.append(f"  {proto}: " + ", ".join(vals))
    bad = {k: v["status"] for k, v in b["permissions"].items() if v["status"] != "allowed"}
    if bad:
        lines.append("  not allowed: " + ", ".join(f"{k}={v}" for k, v in bad.items()))
    if result.get("paths"):
        lines.append(f"  wrote {result['paths']['book']}")
    return "\n".join(lines)
