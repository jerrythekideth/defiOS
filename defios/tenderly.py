"""`defios simulate --tenderly`: the same transactions as the anvil fork, run by Tenderly's Simulation API.

    export TENDERLY_ACCESS_KEY=…  TENDERLY_ACCOUNT=<account slug>  TENDERLY_PROJECT=<project slug>
    defios simulate --tenderly --batch permit.json --calls calls.json

One bundle, in order: each permission transaction from the Roles modifier's owner, then the Operator's
execTransactionWithRole call(s). Tenderly keeps the state between them, so a new permission is tried
with the call that needs it. Every simulation is saved in the project: the result carries a dashboard
link per transaction that anyone in the Tenderly project can open (and share from there).
No anvil and no RPC needed; nothing is signed or broadcast.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from eth_utils import to_checksum_address

from .catalog import Catalog
from .chain import env
from .config import VaultConfig
from .simulate import GAS, SimulateError, _fmt, _Ledger, batch_calls, operator_sends
from .tx import Call

API = "https://api.tenderly.co/api/v1/account/{account}/project/{project}/simulate-bundle"
DASHBOARD = "https://dashboard.tenderly.co/{account}/{project}/simulator/{id}"
ENV = ("TENDERLY_ACCESS_KEY", "TENDERLY_ACCOUNT", "TENDERLY_PROJECT")


def credentials() -> tuple[str, str, str]:
    vals = tuple(env(k) for k in ENV)
    missing = [k for k, v in zip(ENV, vals) if not v]
    if missing:
        raise SimulateError(f"--tenderly needs {', '.join(missing)} (dashboard.tenderly.co > Settings > "
                            f"Access Keys; account and project are the slugs in the project URL)")
    return vals  # type: ignore[return-value]


def _post(url: str, key: str, body: dict) -> dict:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST", headers={
        "X-Access-Key": key, "Content-Type": "application/json", "User-Agent": "defios"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise SimulateError(f"Tenderly answered {e.code}: {e.read()[:300].decode(errors='replace')}") from None
    except urllib.error.URLError as e:
        raise SimulateError(f"Tenderly unreachable: {e.reason}") from None


def bundle(vault: VaultConfig, *, batch: dict | None, calls: list[Call] | None, owner: str | None,
           batch_mode: str = "roles", multisend: str | None = None) -> tuple[list[dict], list[str]]:
    """(transactions in order with a `kind` / `label`, warnings). `owner` executes the batch (Roles owner)."""
    txs: list[dict] = []
    warnings: list[str] = []
    if batch:
        for i, c in enumerate(batch_calls(batch)):
            txs.append({"kind": "permission", "index": i, "label": c.label, "from": owner or vault.avatar, "call": c})
        if not owner:
            warnings.append("Roles owner not read (no RPC_URL); the batch runs as the Vault")
    if calls:
        if not vault.operator:
            raise SimulateError(f"vault {vault.name} has no operator in defios.yaml; calls run through it")
        sends, warns = operator_sends(vault, calls, batch_mode, multisend)
        warnings += warns
        for i, s in enumerate(sends):
            label = "execTransactionWithRole(" + (", ".join(c.label or c.to for c in calls) if len(sends) == 1
                                                   else calls[i].label or calls[i].to) + ")"
            txs.append({"kind": "operator", "index": i, "label": label, "from": vault.operator, "call": s})
    if not txs:
        raise SimulateError("nothing to simulate: pass a permission batch and/or calls")
    return txs, warnings


def simulate(vault: VaultConfig, *, batch: dict | None = None, calls: list[Call] | None = None,
             catalog: Catalog | None = None, owner: str | None = None, block: int | None = None,
             batch_mode: str = "roles", multisend: str | None = None, post=_post) -> dict:
    key, account, project = credentials()
    txs, warnings = bundle(vault, batch=batch, calls=calls, owner=owner, batch_mode=batch_mode, multisend=multisend)
    sims = []
    for t in txs:
        c: Call = t["call"]
        data = c.data if isinstance(c.data, str) else "0x" + bytes(c.data).hex()
        sims.append({"network_id": str(vault.chain_id), "from": to_checksum_address(t["from"]),
                     "to": to_checksum_address(c.to), "input": data, "value": str(c.value), "gas": GAS,
                     "gas_price": "0", "save": True, "save_if_fails": True, "simulation_type": "full",
                     **({"block_number": block} if block else {})})
    out = post(API.format(account=account, project=project), key, {"simulations": sims})
    results = out.get("simulation_results") or []
    if len(results) != len(txs):
        raise SimulateError(f"Tenderly returned {len(results)} results for {len(txs)} transactions")

    ledger = _Ledger(vault.avatar)
    steps, ok = [], True
    for t, r in zip(txs, results):
        tx, sim = r.get("transaction") or {}, r.get("simulation") or {}
        good = bool(tx.get("status"))
        ok = ok and good
        logs = [lg.get("raw") or {} for lg in (tx.get("transaction_info") or {}).get("logs") or []]
        ledger.add(logs)
        step = {"kind": t["kind"], "index": t["index"], "label": t["label"], "ok": good,
                "gas": tx.get("gas_used"), "tenderly": DASHBOARD.format(account=account, project=project,
                                                                        id=sim.get("id"))}
        if not good:
            step["error"] = tx.get("error_message") or (tx.get("error_info") or {}).get("error_message") or "reverted"
        steps.append(step)

    index = catalog.address_index(vault.book_vars) if catalog else {}
    deltas = {}
    for tok, raw in sorted(ledger.deltas.items()):
        if raw:
            hit = index.get(tok)
            name = f"{hit[0][0]}.{hit[0][1]}" if hit else None
            dec = catalog.contract_meta(*hit[0]).get("decimals") if hit and catalog else None
            deltas[to_checksum_address(tok)] = {"name": name, "decimals": dec, "raw": str(raw),
                                                "amount": _fmt(raw, dec)}
    approvals = [{"token": to_checksum_address(tk), "spender": to_checksum_address(sp)}
                 for tk, sp in sorted(ledger.approvals)]
    if approvals:
        warnings.append("the Vault approved spenders during the bundle; check each allowance ends at 0 "
                        "(the anvil run reads them back; Tenderly logs only show the Approval events)")
    return {"vault": vault.name, "engine": "tenderly", "fork_block": block or "latest", "forked": True,
            "steps": steps, "vault_deltas": deltas, "approvals_seen": approvals, "open_allowances": [],
            "warnings": warnings, "gas_total": sum(s.get("gas") or 0 for s in steps), "ok": ok}


def summarize(r: dict) -> str:
    lines = [f"simulate {r['vault']} on Tenderly @ block {r['fork_block']}"]
    for s in r["steps"]:
        line = f"  {'ok ' if s['ok'] else 'REVERT'} {s['kind']} {s['index']} {s['label'] or ''}".rstrip()
        if s.get("gas"):
            line += f"  gas {s['gas']:,}"
        if not s["ok"]:
            line += f"  {s.get('error')}"
        lines += [line, f"      {s['tenderly']}"]
    for tok, d in r["vault_deltas"].items():
        lines.append(f"  Vault {d['amount']} {d['name'] or tok}")
    for w in r["warnings"]:
        lines.append(f"  warning: {w}")
    lines.append("  all transactions succeeded on Tenderly" if r["ok"] else
                 "  FAILED on Tenderly: do not propose; open the link and explain the revert to the human")
    return "\n".join(lines)
