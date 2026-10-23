"""`defios check`: cards x decoded role -> allowed / missing / unsafe, plus role lints.

For every action of every card the vault uses, the engine builds the exact
calldata (CallBuilder), evaluates it against the decoded role, and evaluates
hostile variants (pinned params set to 0x…dEaD, calls[] arrays given an
arbitrary call). With an RPC and an Operator it confirms each verdict with an
eth_call probe of execTransactionWithRole from the Operator.
"""
from __future__ import annotations

from typing import Any

from eth_utils import to_checksum_address

from .catalog import (
    BuiltCall,
    CallBuilder,
    Catalog,
    CardError,
    has_free_calls,
    receiver_like,
)
from .config import VaultConfig
from .roles.base import RoleState, RolesAdapter
from .roles.conditions import Clearance, ExecutionOptions, Operator, AbiType, constrains_param

APPROVE = "0x095ea7b3"
SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2, "info": 3}
NOT_VALUE_MOVING = {"oracle", "view", "registry"}


def detect_used_protocols(catalog: Catalog, state: RoleState, book_vars: dict) -> list[str]:
    index = catalog.address_index(book_vars)
    used = set()
    for addr, t in state.targets.items():
        if t.clearance == Clearance.NONE:
            continue
        for proto, _ in index.get(addr, []):
            used.add(proto)
    return sorted(used)


def _probe(adapter: RolesAdapter, rpc, vault: VaultConfig, bc: BuiltCall, data: bytes) -> dict:
    r = adapter.probe(rpc, vault.operator, vault.role_key, bc.target, bc.value, data, bc.operation)
    d: dict[str, Any] = {"ok": r.ok}
    if not r.ok:
        d["error"] = r.error
        if r.status:
            d["status"] = r.status
    return d


def check_call(adapter: RolesAdapter, state: RoleState, vault: VaultConfig, bc: BuiltCall,
               rpc=None) -> dict:
    out: dict[str, Any] = {"call": bc.label, "target": bc.target, "optional": bc.optional, "alt": bc.alt}
    if bc.sig:
        out["function"] = bc.sig.canonical
        out["selector"] = bc.sig.selector
    if bc.operation:
        out["operation"] = "delegatecall"
    if bc.error:
        out.update(status="unresolved", reason=bc.error)
        return out
    pins = [{"param": p.path, "must_be": p.expected if len(p.expected) > 1 else p.expected[0], "kind": p.kind}
            for p in bc.pins]
    if pins:
        out["pins"] = pins
    v = adapter.check_call(state, bc.target, bc.value, bc.data, bc.operation, vault.avatar)
    notes = list(v.notes)
    if not v.ok:
        out.update(status="missing", reason=f"{v.status.name}: {v.detail}")
    else:
        holes, loose = [], []
        for desc, data, sev in bc.hostile:
            hv = adapter.check_call(state, bc.target, bc.value, data, bc.operation, vault.avatar)
            if hv.ok:
                (holes if sev == "unsafe" else loose).append(desc)
        if holes:
            out.update(status="unsafe", reason="role also accepts: " + "; ".join(holes), holes=holes)
        else:
            out.update(status="allowed", reason=v.detail)
        if loose:
            out["loose"] = loose
    out["source"] = "offline"
    if rpc is not None and vault.operator:
        probe = {"intended": _probe(adapter, rpc, vault, bc, bc.data)}
        probe["hostile"] = [{"variant": d, "severity": sev, **_probe(adapter, rpc, vault, bc, data)}
                            for d, data, sev in bc.hostile]
        out["probe"] = probe
        live_ok = probe["intended"]["ok"]
        live_holes = [h["variant"] for h in probe["hostile"] if h["ok"] and h["severity"] == "unsafe"]
        live_status = "missing" if not live_ok else ("unsafe" if live_holes else "allowed")
        if probe["intended"].get("error", "").startswith("rpc:"):
            notes.append("probe failed (RPC); offline verdict kept")
        else:
            if live_status != out["status"]:
                notes.append(f"offline said {out['status']}, live eth_call says {live_status}; using live")
            out["status"] = live_status
            out["source"] = "eth_call"
            if live_status == "unsafe":
                out["holes"] = live_holes
                out["reason"] = "live role accepts: " + "; ".join(live_holes)
            elif live_status == "missing":
                out["reason"] = f"live: {probe['intended'].get('error')} {probe['intended'].get('status', '')}".strip()
    if notes:
        out["notes"] = sorted(set(notes))
    return out


def _aggregate(calls: list[dict]) -> str:
    st = [c["status"] for c in calls if "status" in c]
    required = [c for c in calls if "status" in c and not c["optional"] and not c["alt"]]
    if any(c.get("status") == "unsafe" for c in calls):
        return "unsafe"
    if any(c["status"] == "unresolved" for c in required):
        return "unresolved"
    # a required call is satisfied if it, or a following alt, is allowed
    missing = False
    for i, c in enumerate(calls):
        if "status" not in c or c["optional"] or c["alt"]:
            continue
        alts = []
        for nxt in calls[i + 1:]:
            if not nxt.get("alt"):
                break
            alts.append(nxt)
        if c["status"] != "allowed" and not any(a.get("status") == "allowed" for a in alts):
            missing = True
    if missing:
        return "missing"
    return "allowed" if st else "n/a"


def _token_name(catalog: Catalog, proto: str, action: dict, addr: str, book_vars: dict) -> str:
    for ref in action.get("tokens") or []:
        try:
            if (catalog.resolve_contract(ref, proto, book_vars) or "").lower() == addr.lower():
                return str(ref).split(".")[-1]
        except CardError:
            continue
    return addr


def check_actions(catalog: Catalog, state: RoleState, adapter: RolesAdapter, vault: VaultConfig,
                  protocols: list[str], rpc=None) -> dict:
    builder = CallBuilder(catalog, vault.avatar, vault.book_vars)
    out: dict[str, Any] = {}
    for proto in protocols:
        card = catalog.cards.get(proto)
        if not card:
            out[proto] = {"error": "no card in catalog"}
            continue
        pres: dict[str, Any] = {}
        for aname, action in card.actions.items():
            action = action or {}
            calls_out = []
            steps = []
            # an action with a `tokens:` list is checked once per token ($token = that token)
            tokens = catalog.action_tokens(proto, action, vault.book_vars) if action.get("tokens") else [None]
            per_token: dict[str, str] = {}
            for tok in tokens:
                vars_ = {"token": tok, "_tokens": tokens} if tok else {}
                tok_calls = []
                for i, call in enumerate(action.get("calls") or []):
                    if "sig" not in call:
                        if tok == tokens[0]:
                            steps.append({k: call[k] for k in ("sign", "external", "signer", "note") if k in call})
                        continue
                    bc = builder.build(proto, aname, i, call, vars_)
                    c = check_call(adapter, state, vault, bc, rpc)
                    if tok:
                        c["token"] = _token_name(catalog, proto, action, tok, vault.book_vars)
                    tok_calls.append(c)
                if tok:
                    per_token[_token_name(catalog, proto, action, tok, vault.book_vars)] = _aggregate(tok_calls)
                calls_out += tok_calls
            if per_token:
                sts = set(per_token.values())
                status = ("unsafe" if "unsafe" in sts else sts.pop() if len(sts) == 1
                          else "partial" if "allowed" in sts else "missing")
            else:
                status = _aggregate(calls_out)
            entry: dict[str, Any] = {"status": status, "calls": calls_out}
            if per_token:
                entry["tokens"] = per_token
            if any(c.get("loose") for c in calls_out):
                entry["loose"] = True
            if steps:
                entry["offchain_steps"] = steps
                entry["note"] = "also needs steps the role cannot cover (signatures / third-party execution)"
                if entry["status"] == "allowed" and any("sign" in s_ for s_ in steps):
                    entry["status"] = "needs_signature"
            if action.get("summary"):
                entry["summary"] = action["summary"]
            pres[aname] = entry
        out[proto] = pres
    return out


def _addr_name(index: dict, addr: str) -> str | None:
    hits = index.get(addr.lower())
    return f"{hits[0][0]}.{hits[0][1]}" if hits else None


def _static_addresses(root) -> list[str]:
    out = []

    def rec(c):
        if c.operator == Operator.EQUAL_TO and c.param_type == AbiType.STATIC and len(c.comp_value) == 32:
            cv = c.comp_value
            if cv[:12] == b"\0" * 12 and int.from_bytes(cv, "big") > 2**80:
                out.append(to_checksum_address(cv[12:]))
        for ch in c.children:
            rec(ch)

    if root is not None:
        rec(root)
    return out


def lint_role(catalog: Catalog, state: RoleState, vault: VaultConfig) -> list[dict]:
    index = catalog.address_index(vault.book_vars)
    known = catalog.known_functions(vault.book_vars)
    lints: list[dict] = []
    avatar = vault.avatar.lower()

    def add(sev, code, msg, target=None, selector=None, **kw):
        d = {"severity": sev, "code": code, "message": msg}
        if target:
            d["target"] = to_checksum_address(target)
            n = _addr_name(index, target)
            if n:
                d["target_name"] = n
        if selector:
            d["selector"] = selector
        d.update(kw)
        lints.append(d)

    # membership
    if vault.operator and vault.operator not in state.members:
        add("high", "operator_not_member", f"Operator {vault.operator} is not a member of the role")

    # wildcarded targets
    for addr, t in sorted(state.targets.items()):
        if t.clearance != Clearance.TARGET:
            continue
        kinds = {catalog.contract_meta(p, n).get("kind") for p, n in index.get(addr, [])}
        sev = "medium" if kinds and kinds <= NOT_VALUE_MOVING else "high"
        add(sev, "wildcard_target", "every function on this target is allowed (allowTarget)", addr,
            options=ExecutionOptions(t.options).name)
        if t.options in (ExecutionOptions.DELEGATE_CALL, ExecutionOptions.BOTH):
            add("high", "delegatecall_target", "target allows delegatecall to any function", addr)

    needs_delegate = set()
    needs_send = set()
    for (a, s), entries in known.items():
        for e in entries:
            call = e.get("call") or {}
            if call.get("operation") == "delegatecall":
                needs_delegate.add((a, s))
            if call.get("send"):
                needs_send.add((a, s))

    referenced: set[str] = set()
    for fs in state.active_functions():
        a, s = fs.target.lower(), fs.selector
        entries = known.get((a, s), [])
        live = [e for e in entries if not e["dead"]]
        dead = [e for e in entries if e["dead"]]
        sig = next((e["sig"] for e in entries if e.get("sig")), None)
        fname = sig.canonical if sig else None
        if dead and not live:
            add("low", "known_dead", f"{fname or s}: {dead[0].get('why', '')}".strip(), a, s, function=fname)
        if not entries:
            add("info", "uncatalogued", "permission not described by any card (unknown function or contract)",
                a, s)
        # execution options
        if fs.options in (ExecutionOptions.DELEGATE_CALL, ExecutionOptions.BOTH) and (a, s) not in needs_delegate:
            add("high", "delegatecall_not_needed", "function allows delegatecall but no card needs it", a, s,
                function=fname)
        if fs.options in (ExecutionOptions.SEND, ExecutionOptions.BOTH) and (a, s) not in needs_send:
            add("medium", "send_not_needed", "function allows sending ETH but no card needs it", a, s, function=fname)
        if fs.condition is not None:
            for addr in _static_addresses(fs.condition):
                referenced.add(addr)
        # param-level lints need the signature
        if s == APPROVE:
            if fs.wildcarded or not constrains_param(fs.condition, 0):
                add("high", "approve_any_spender", "approve() with no spender restriction", a, s,
                    function="approve(address,uint256)")
            else:
                for sp in _static_addresses(fs.condition.children[0]):
                    if sp.lower() not in index and sp.lower() != avatar:
                        add("medium", "approve_uncatalogued_spender",
                            f"approve() allows spender {sp}, which no card describes", a, s, spender=sp)
            continue
        if not sig:
            continue
        for i, p in enumerate(sig.params):
            free = fs.wildcarded or fs.condition is None or not constrains_param(fs.condition, i)
            if not free:
                continue
            if receiver_like(p) and not dead:
                add("high", "unpinned_receiver",
                    f"{p.name} ({fname}) is not restricted; pin it to the Avatar", a, s, function=fname, param=p.name)
            if has_free_calls(p) and not dead:
                add("high", "unscoped_calls",
                    f"{p.name} ({fname}) is an unscoped bytes/calls[] param; scope it element by element", a, s,
                    function=fname, param=p.name)
        if fs.wildcarded and live and not dead:
            pinned = [pin for e in live for pin in (e.get("call") or {}).get("args", {}).values()
                      if isinstance(pin, str) and pin == "$AVATAR"]
            if pinned and not any(l["code"] == "unpinned_receiver" and l.get("selector") == s and
                                  l.get("target", "").lower() == a for l in lints):
                add("high", "unpinned_receiver", f"{fname} allowed with no conditions; card pins params to the Avatar",
                    a, s, function=fname)

    for fs in state.dormant_functions():
        add("info", "dormant_function",
            "function scope stored but target is not function-scoped; it revives if scopeTarget is called again",
            fs.target, fs.selector)

    for addr in sorted(referenced):
        if addr.lower() not in index and addr.lower() != avatar and not any(
                l.get("spender") == addr for l in lints):
            add("info", "uncatalogued_address", f"conditions reference {addr}, which no card describes", addr)

    for k, a in state.allowances.items():
        if a.get("referenced_by_role") and a.get("never_set"):
            add("medium", "allowance_never_set", f"conditions use allowance {k} which was never set")

    lints.sort(key=lambda d: (SEVERITY_ORDER[d["severity"]], d["code"], d.get("target", ""), d.get("selector", "")))
    return lints


def run_check(catalog: Catalog, state: RoleState, adapter: RolesAdapter, vault: VaultConfig,
              protocols: list[str] | None = None, rpc=None) -> dict:
    used = protocols or vault.protocols or detect_used_protocols(catalog, state, vault.book_vars)
    try:
        actions = check_actions(catalog, state, adapter, vault, used, rpc)
    except CardError as e:
        actions = {"error": str(e)}
    lints = lint_role(catalog, state, vault)
    counts: dict[str, int] = {}
    for proto in actions.values():
        if not isinstance(proto, dict):
            continue
        for a in proto.values():
            if isinstance(a, dict) and "status" in a:
                counts[a["status"]] = counts.get(a["status"], 0) + 1
    return {
        "vault": vault.name,
        "block": state.block,
        "roles_version": state.version,
        "probe": bool(rpc is not None and vault.operator),
        "protocols": used,
        "actions": actions,
        "lints": lints,
        "counts": counts,
        "lint_counts": {s: sum(1 for l in lints if l["severity"] == s) for s in SEVERITY_ORDER},
    }


def summarize(result: dict) -> str:
    lines = [f"check {result['vault']} @ block {result['block']} (roles {result['roles_version']}, "
             f"{'eth_call probe' if result['probe'] else 'offline evaluation'})"]
    for proto, acts in result["actions"].items():
        if not isinstance(acts, dict):
            continue
        parts = []
        for a, e in acts.items():
            if isinstance(e, dict) and "status" in e:
                parts.append(f"{a}={e['status']}")
        lines.append(f"  {proto}: " + ", ".join(parts))
    lc = result["lint_counts"]
    lines.append(f"  lints: {lc['high']} high, {lc['medium']} medium, {lc['low']} low, {lc['info']} info")
    for l in result["lints"]:
        if l["severity"] in ("high", "medium"):
            who = l.get("target_name") or l.get("target") or ""
            lines.append(f"   [{l['severity']}] {l['code']} {who} {l.get('function') or l.get('selector') or ''}: "
                         f"{l['message']}")
    return "\n".join(lines)
