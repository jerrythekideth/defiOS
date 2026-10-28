"""`defios plan`: intent -> engine-built calls -> role check -> plan JSON + calls.json (M2).

The LLM only chooses an intent: card actions, human amounts, optionally a token.

    defios plan gearbox_v3.lend --amount 1 --token wstETH
    defios plan weth.unwrap=1 lido.stake=1 lido.wrap=0.999 kpkwstETH.lend=0.8
    defios plan --intent intent.json

The engine then

1. resolves each step (`<card>.<action>` or `<alias>.<action>`, e.g. `kpkwstETH.lend`, `"gearbox pool.lend"`)
   and the optional token through card contract names and aliases;
2. converts human amounts with the input token's decimals (card `decimals`; checked with
   `decimals()` over RPC when RPC_URL is set);
3. builds every call of the card action (approves included) with CallBuilder: `$AVATAR` is the
   Vault, `$book.*` comes from defios.yaml `book_vars`, other `$vars` must be given (or have a card
   `defaults:` entry) - they are never filled with samples;
4. checks every call against the role (chain replay, `--logs-file`, or `books/<vault>/role.lock.json`
   when no log source is available) and, with RPC_URL and an Operator, an eth_call probe of
   execTransactionWithRole. A call that is `missing` or `unsafe` refuses the whole plan;
5. appends `approve(spender, 0)` for every approval the plan made, after the last step, so the
   transaction leaves no open allowance (one revoke per token + spender, checked against the role
   like any other call). Actions whose spender pulls later (a CoW order, a relayer) set
   `approvals: keep` in their card and get no revoke; `keep_approvals=True` turns revokes off;
6. returns the plan (steps, calls with plain-language lines, Operator Safe tx preview built by tx.py)
   and the calls in the `calls.json` format that `defios propose --file` reads.

Nothing here signs, posts or simulates balances.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from eth_abi import decode as abi_decode
from eth_abi import encode as abi_encode
from eth_utils import is_address, to_checksum_address

from . import lookups
from .catalog import AVATAR, BuiltCall, CallBuilder, CardError, Catalog
from .check import check_call
from .config import VaultConfig
from .roles.base import RolesAdapter, RoleState
from .tx import MULTISEND_SELECTOR, Call, TxError, build_operator_tx, multisend_address

NATIVE = "ETH"
NATIVE_DECIMALS = 18


APPROVE_SIG = "approve(address,uint256)"
APPROVE_SELECTOR = "0x095ea7b3"


class PlanError(ValueError):
    pass


@dataclass
class Step:
    ref: str                                # "gearbox_v3.lend", "kpkwstETH.lend", "gearbox pool.lend"
    amount: str | None = None               # human amount in the input token, e.g. "1.5"
    token: str | None = None                # token name / alias / card ref / address, or "ETH"
    params: dict[str, Any] = field(default_factory=dict)   # extra $vars, e.g. {"referral": "0x…"}


# ------------------------------------------------------------------------------ intent parsing
def parse_step(s: str) -> Step:
    """'card.action' or 'card.action=1.5'."""
    ref, _, amount = s.partition("=")
    if not ref.strip() or "." not in ref:
        raise PlanError(f"step {s!r}: expected <card>.<action>[=<amount>], e.g. gearbox_v3.lend=1")
    return Step(ref.strip(), amount.strip() or None)


def load_intent(src: str) -> tuple[list[Step], str | None]:
    """JSON intent, inline or a file path:
    {"description": "...", "steps": [{"action": "gearbox_v3.lend", "amount": "1", "token": "wstETH",
    "params": {...}}]} - or one step object, or a bare list of steps."""
    p = Path(src)
    try:
        raw = json.loads(p.read_text() if not src.lstrip().startswith(("{", "[")) and p.exists() else src)
    except (OSError, ValueError) as e:
        raise PlanError(f"intent: not valid JSON ({e})") from None
    desc = None
    if isinstance(raw, dict) and "steps" in raw:
        desc, raw = raw.get("description"), raw["steps"]
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list) or not raw:
        raise PlanError("intent: expected a step object or a non-empty `steps` list")
    steps = []
    for i, d in enumerate(raw):
        if isinstance(d, str):
            steps.append(parse_step(d))
            continue
        unknown = set(d) - {"action", "amount", "token", "params"}
        if unknown or "action" not in d:
            raise PlanError(f"intent step {i}: needs `action`; allowed keys action, amount, token, params"
                            + (f" (unknown {sorted(unknown)})" if unknown else ""))
        steps.append(Step(str(d["action"]), None if d.get("amount") is None else str(d["amount"]),
                          d.get("token"), dict(d.get("params") or {})))
    return steps, desc


# ------------------------------------------------------------------------------ name resolution
def find_contracts(catalog: Catalog, name: str, book_vars: dict | None = None) -> list[dict]:
    """Contracts whose `card.name`, name or alias matches (case-insensitive), deduplicated by address."""
    n = name.strip().lower()
    hits: dict[str, dict] = {}
    for proto, card in catalog.cards.items():
        for cname, c in card.contracts.items():
            c = c or {}
            names = {cname.lower(), f"{proto}.{cname}".lower()} | {str(a).lower() for a in c.get("aliases") or []}
            if n not in names:
                continue
            try:
                addr = catalog.resolve_contract(cname, proto, book_vars)
            except CardError:
                addr = None
            if not addr:
                continue
            origin = (proto, cname)
            if "ref" in c:  # report the contract where it is defined
                r = c["ref"]
                origin = tuple(r.split(".", 1)) if "." in r else (proto, r)
            h = hits.setdefault(addr.lower(), {"address": addr, "protocol": origin[0], "name": origin[1],
                                               "matched_in": []})
            h["matched_in"].append(f"{proto}.{cname}")
    return list(hits.values())


def resolve_token(catalog: Catalog, token: str, book_vars: dict | None = None) -> dict:
    if token.strip().upper() == NATIVE:
        return {"symbol": NATIVE, "address": None, "decimals": NATIVE_DECIMALS, "ref": NATIVE}
    if is_address(token):
        idx = catalog.address_index(book_vars).get(token.lower())
        if not idx:
            raise PlanError(f"token {token} is not in any card; every address must come from a card (AGENTS.md rule 2)")
        hits = [{"address": to_checksum_address(token), "protocol": idx[0][0], "name": idx[0][1]}]
    else:
        hits = find_contracts(catalog, token, book_vars)
    if not hits:
        raise PlanError(f"token {token!r} matches no card contract name or alias")
    if len(hits) > 1:
        raise PlanError(f"token {token!r} is ambiguous: " + ", ".join(f"{h['protocol']}.{h['name']} {h['address']}"
                                                                    for h in hits) + "; use <card>.<contract>")
    h = hits[0]
    meta = catalog.contract_meta(h["protocol"], h["name"])
    return {"symbol": h["name"], "address": h["address"], "decimals": meta.get("decimals"),
            "ref": f"{h['protocol']}.{h['name']}"}


def _call_contracts(catalog: Catalog, proto: str, action: dict, book_vars: dict) -> set[str]:
    """Addresses an action touches: call targets and contract-valued args."""
    out: set[str] = set()

    def add(ref):
        try:
            a = catalog.resolve_contract(ref, proto, book_vars)
        except CardError:
            return
        if a:
            out.add(a.lower())

    def walk(spec):
        if isinstance(spec, str) and catalog.is_contract_ref(spec, proto):
            add(spec)
        elif isinstance(spec, dict):
            for v in (spec.get("one_of") or spec.values()):
                walk(v)
        elif isinstance(spec, list):
            for v in spec:
                walk(v)

    for c in action.get("calls") or []:
        if "target" in c and c["target"] != "$token":
            add(c["target"])
        walk(c.get("args") or {})
    for t in action.get("tokens") or []:
        add(t)
    return out


def resolve_action(catalog: Catalog, ref: str, book_vars: dict | None = None) -> tuple[str, str, str | None]:
    """'<card>.<action>' or '<contract name | alias>.<action>' -> (protocol, action, alias used)."""
    head, _, aname = ref.rpartition(".")
    if not head or not aname:
        raise PlanError(f"{ref!r}: expected <card>.<action>")
    if head in catalog.cards:
        if aname not in catalog.cards[head].actions:
            raise PlanError(f"card {head} has no action {aname!r} (has: {', '.join(catalog.cards[head].actions)})")
        return head, aname, None
    hits = find_contracts(catalog, head, book_vars)
    if not hits:
        raise PlanError(f"{head!r} is neither a card nor a contract name/alias in any card")
    cands = []
    for proto, card in catalog.cards.items():
        if aname not in card.actions:
            continue
        used = _call_contracts(catalog, proto, card.actions[aname] or {}, book_vars or {})
        if any(h["address"].lower() in used for h in hits):
            cands.append(proto)
    if not cands:
        raise PlanError(f"{head!r} is " + ", ".join(f"{h['protocol']}.{h['name']}" for h in hits)
                        + f", but no card action {aname!r} uses it")
    if len(cands) > 1:
        raise PlanError(f"{ref!r} is ambiguous: {', '.join(c + '.' + aname for c in cands)}")
    return cands[0], aname, head


# ------------------------------------------------------------------------------ amounts
def to_raw(amount: str, decimals: int, what: str) -> int:
    try:
        d = Decimal(str(amount).replace("_", ""))
    except InvalidOperation:
        raise PlanError(f"{what}: amount {amount!r} is not a number (no 'max' / 'all': give the exact amount)") from None
    if not d.is_finite() or d <= 0:
        raise PlanError(f"{what}: amount must be > 0, got {amount}")
    raw = d * (Decimal(10) ** decimals)
    if raw != raw.to_integral_value():
        raise PlanError(f"{what}: {amount} has more than {decimals} decimals")
    return int(raw)


def fmt_amount(raw: int, decimals: int) -> str:
    return format((Decimal(raw) / (Decimal(10) ** decimals)).normalize(), "f")


def _onchain_decimals(rpc, address: str) -> int | None:
    from .chain import ChainError
    try:
        return int(rpc.call(address, "decimals()", [], ["uint8"]))
    except (ChainError, ValueError, TypeError):
        return None


# ------------------------------------------------------------------------------ vars
def _free_vars(spec: Any, out: set[str]) -> set[str]:
    if isinstance(spec, str) and spec.startswith("$") and spec != AVATAR and not spec.startswith("$book."):
        out.add(spec[1:])
    elif isinstance(spec, dict):
        for v in (spec.get("one_of") or []) if "one_of" in spec else spec.values():
            _free_vars(v, out)
    elif isinstance(spec, list):
        for v in spec:
            _free_vars(v, out)
    return out


def _param_value(catalog: Catalog, proto: str, v: Any, book_vars: dict) -> Any:
    """--param values: ints, addresses from cards, or card contract refs (or lists of them, from an
    --intent JSON). Never a free-form address. Hex that is not an address is an int; a bytesN param
    gets it back as bytes when the call is encoded (Merkl proofs)."""
    if isinstance(v, list):
        return [_param_value(catalog, proto, x, book_vars) for x in v]
    if isinstance(v, bool):
        raise PlanError(f"param value {v!r} not supported")
    if isinstance(v, int):
        return v
    s = str(v).strip()
    if s.lstrip("-").isdigit():
        return int(s)
    if s.startswith("0x") and not is_address(s):
        return int(s, 16)
    if is_address(s):
        a = to_checksum_address(s)
        if int(a, 16) != 0 and not catalog.address_index(book_vars).get(a.lower()):
            raise PlanError(f"param address {a} is not in any card (AGENTS.md rule 2)")
        return a
    try:
        a = catalog.resolve_contract(s, proto, book_vars)
    except CardError:
        a = None
    if a:
        return a
    raise PlanError(f"param value {s!r} is not a number, a catalogued address or a card contract")


# ------------------------------------------------------------------------------ plain words
def _names(catalog: Catalog, vault: VaultConfig) -> dict[str, str]:
    idx = catalog.address_index(vault.book_vars)
    out = {a: f"{v[0][0]}.{v[0][1]}" for a, v in idx.items()}
    out[vault.avatar.lower()] = "the Vault"
    return out


def describe_call(bc: BuiltCall, names: dict[str, str], amounts: dict[int, str]) -> tuple[str, dict]:
    """('lido.wstETH.approve(spender=kpk.kpkwstETH, amount=0.8 wstETH)', {decoded args})."""
    assert bc.sig is not None and bc.data is not None and bc.target is not None
    vals = abi_decode([p.type for p in bc.sig.params], bc.data[4:]) if bc.sig.params else ()
    args, parts = {}, []

    def render(v: Any) -> str:
        if isinstance(v, str) and is_address(v):
            return names.get(v.lower(), to_checksum_address(v))
        if isinstance(v, int) and not isinstance(v, bool):
            return amounts.get(v, str(v))
        if isinstance(v, (bytes, bytearray)):
            return "0x" + bytes(v).hex()
        if isinstance(v, (list, tuple)):
            return "[" + ", ".join(render(x) for x in v) + "]"
        return str(v)

    for p, v in zip(bc.sig.params, vals):
        args[p.name] = (to_checksum_address(v) if isinstance(v, str) and is_address(v) else
                        "0x" + bytes(v).hex() if isinstance(v, (bytes, bytearray)) else
                        str(v) if isinstance(v, int) and not isinstance(v, bool) else v)
        parts.append(f"{p.name}={render(v)}")
    line = f"{names.get(bc.target.lower(), bc.target)}.{bc.sig.name}({', '.join(parts)})"
    if bc.value:
        line += f", sending {fmt_amount(bc.value, NATIVE_DECIMALS)} ETH"
    return line, args


def _plain_refusal(c: dict, line: str) -> str:
    if c["status"] == "missing":
        return f"the role does not allow {line}: {c.get('reason')}"
    if c["status"] == "unsafe":
        return (f"the role allows {line}, but it also allows a hostile version of it ({c.get('reason')}); "
                f"fix the permission before using it")
    return f"{line}: {c['status']} ({c.get('reason')})"


# ------------------------------------------------------------------------------ planning
def plan(catalog: Catalog, state: RoleState, adapter: RolesAdapter, vault: VaultConfig, steps: list[Step], *,
         rpc=None, probe: bool = True, batch: str = "roles", description: str | None = None,
         role_source: str | None = None, keep_approvals: bool = False) -> dict:
    """Build and check every call of every step. Returns the plan; `refused` is non-empty when any call is
    missing / unsafe / unresolved (then `calls` must not be proposed)."""
    if not steps:
        raise PlanError("nothing to plan: give at least one <card>.<action> step")
    builder = CallBuilder(catalog, vault.avatar, vault.book_vars)
    names = _names(catalog, vault)
    probe_rpc = rpc if probe else None
    out_steps, refused, warnings, all_calls = [], [], [], []
    revokes: dict[tuple[str, str], tuple[BuiltCall, str]] = {}  # (token, spender) -> (approve call, action)
    kept: list[str] = []

    for si, step in enumerate(steps):
        proto, aname, via = resolve_action(catalog, step.ref, vault.book_vars)
        action = catalog.cards[proto].actions[aname] or {}
        where = f"step {si + 1} ({proto}.{aname})"
        for c in action.get("calls") or []:
            if "sig" not in c:
                kind = "an off-chain signature" if "sign" in c else "a step by a third party"
                raise PlanError(f"{where} needs {kind} ({c.get('sign') or c.get('external')}); defios plan only "
                                f"builds on-chain calls, so this action cannot be planned")

        # input token and amount
        input_ref = action.get("input")
        input_tok = resolve_token(catalog, input_ref if input_ref == NATIVE or "." in str(input_ref)
                                  else f"{proto}.{input_ref}", vault.book_vars) if input_ref else None
        tok = resolve_token(catalog, step.token, vault.book_vars) if step.token else None
        # `tokens:` + `input:` (e.g. a list of vaults that take USDC): --token picks from the list,
        # the amount is in the input token
        pick_from_list = bool(action.get("tokens")) and bool(input_tok) and input_tok["address"] is not None
        if tok and input_tok and not pick_from_list and \
                (tok["address"] or "").lower() != (input_tok["address"] or "").lower():
            raise PlanError(f"{where} takes {input_tok['ref']}, not {tok['ref']}")
        if not pick_from_list:
            tok = tok or input_tok
        amt_tok = input_tok if pick_from_list else tok
        vars_: dict[str, Any] = {}
        # card lookups (a Morpho market from --param market_id): read onchain, checked, pinned
        try:
            vars_.update(lookups.resolve(catalog, proto, action, step.params, rpc, vault.book_vars))
        except lookups.LookupFailed as e:
            raise PlanError(f"{where}: {e}") from None
        lk_tok = lookups.token_from(action, vars_)
        if lk_tok:
            ltok = resolve_token(catalog, lk_tok, vault.book_vars)
            if tok and (tok["address"] or "").lower() != lk_tok.lower():
                raise PlanError(f"{where}: the market's {action['token_from'].split('.')[-1]} is {ltok['ref']}, "
                                f"not {tok['ref']}")
            tok = amt_tok = ltok
        if action.get("tokens"):  # per-token action: $token is the chosen token, which must be in the list
            allowed = catalog.action_tokens(proto, action, vault.book_vars)
            names_ = ", ".join(str(r) for r in action["tokens"])
            if tok is None and len(allowed) == 1:
                tok = resolve_token(catalog, allowed[0], vault.book_vars)
            if tok is None:
                raise PlanError(f"{where} works on one of {names_}: pass --token")
            if (tok["address"] or "").lower() not in {a.lower() for a in allowed}:
                raise PlanError(f"{where} takes one of {names_}, not {tok['ref']}")
            vars_["_tokens"] = allowed
        raw = None
        if step.amount is not None:
            if not amt_tok:
                raise PlanError(f"{where}: the card does not say which token the amount is in; pass --token")
            dec = amt_tok["decimals"]
            if amt_tok["address"] and rpc is not None:
                live = _onchain_decimals(rpc, amt_tok["address"])
                if live is not None and dec is not None and live != int(dec):
                    raise PlanError(f"{amt_tok['ref']}: card says {dec} decimals, chain says {live}; fix the card")
                dec = live if dec is None else dec
            if dec is None:
                raise PlanError(f"{amt_tok['ref']} has no `decimals` in its card and no RPC_URL to read it")
            raw = to_raw(step.amount, int(dec), where)
            vars_.update(amount=raw, shares=raw)
        if tok and tok["address"]:
            vars_["token"] = tok["address"]
        for k, v in (action.get("defaults") or {}).items():
            vars_.setdefault(k, _param_value(catalog, proto, v, vault.book_vars))
        lookup_params = {lk.get("param") for lk in lookups.card_lookups(catalog, proto).values()}
        for k, v in step.params.items():
            if k not in lookup_params:
                vars_[k] = _param_value(catalog, proto, v, vault.book_vars)
        if raw is not None and any(c.get("send") for c in action.get("calls") or []):
            vars_["value"] = raw
        amounts = {raw: f"{step.amount} {amt_tok['symbol']}"} if raw is not None and amt_tok else {}

        # calls: each required call, or the first allowed alt after it; optional calls skipped
        groups: list[list[tuple[int, dict]]] = []
        skipped = []
        for i, c in enumerate(action.get("calls") or []):
            if c.get("alt") and groups:
                groups[-1].append((i, c))
            elif c.get("optional"):
                skipped.append(f"{c['target']}.{c['sig'].split('(')[0]} (optional)")
            else:
                groups.append([(i, c)])
        step_calls = []
        for group in groups:
            primary_vars = _free_vars(group[0][1].get("args") or {}, set())
            tried = []
            chosen = None
            for gi, (i, c) in enumerate(group):
                need = _free_vars(c.get("args") or {}, set())
                if gi and need != primary_vars:
                    continue  # an alt with other inputs (e.g. assets vs shares) is not a drop-in replacement
                missing_vars = sorted(v for v in need if v not in vars_)
                if c.get("send") and "value" not in vars_:
                    missing_vars.append("amount (ETH to send)")
                if missing_vars:
                    tried.append({"call": f"{c['target']}.{c['sig'].split('(')[0]}", "status": "unresolved",
                                  "reason": "needs " + ", ".join(missing_vars)
                                            + " (--amount / --param name=value)"})
                    continue
                bc = builder.build(proto, aname, i, c, vars_)
                unset = sorted({p.name for p in bc.sig.params} - set(c.get("args") or {})) if bc.sig else []
                if unset:
                    tried.append({"call": bc.label, "status": "unresolved",
                                  "reason": f"card leaves {unset} unset; refusing to fill them with samples"})
                    continue
                res = check_call(adapter, state, vault, bc, probe_rpc)
                res["_bc"] = bc
                tried.append(res)
                if res["status"] == "allowed":
                    chosen = res
                    break
            if chosen is None:
                first = tried[0] if tried else {"call": group[0][1].get("target"), "status": "unresolved",
                                                "reason": "no call could be built"}
                bc0 = first.get("_bc")
                line = describe_call(bc0, names, amounts)[0] if bc0 and bc0.data else first["call"]
                why = _plain_refusal(first, line)
                if len(tried) > 1:
                    why += "; alternatives: " + "; ".join(f"{t['call']} {t['status']}" for t in tried[1:])
                refused.append({"step": si + 1, "action": f"{proto}.{aname}", "call": first["call"],
                                "status": first["status"], "why": why})
                continue
            bc = chosen.pop("_bc")
            line, args = describe_call(bc, names, amounts)
            call = Call(to=bc.target, value=bc.value, data=bc.data, operation=bc.operation,
                        label=f"{proto}.{aname}[{bc.index}] {bc.sig.name}")
            entry = {**call.to_json(), "contract": names.get(bc.target.lower()), "function": bc.sig.canonical,
                     "args": args, "line": line,
                     "check": {k: chosen[k] for k in ("status", "source", "reason", "notes", "loose") if k in chosen}}
            if bc.alt:
                entry["alt"] = True
            step_calls.append(entry)
            all_calls.append(call)
            if bc.sig.canonical == APPROVE_SIG and bc.data:
                spender = to_checksum_address(abi_decode(["address", "uint256"], bc.data[4:])[0])
                if action.get("approvals") == "keep":
                    kept.append(f"{line} (pulled later by {names.get(spender.lower(), spender)})")
                elif not keep_approvals:
                    revokes[(bc.target.lower(), spender.lower())] = (bc, f"{proto}.{aname}")
        s_out = {"step": si + 1, "action": f"{proto}.{aname}", "summary": action.get("summary"),
                 "card_reviewed": bool(catalog.cards[proto].raw.get("reviewed")), "calls": step_calls}
        if via:
            s_out["resolved_from"] = step.ref
        for lname, lid in (vars_.get("_lookup_ids") or {}).items():
            s_out.setdefault("lookups", {})[lname] = {"id": lid, **{k.split(".", 1)[1]: (names.get(v.lower(), v) if
                                                      isinstance(v, str) else v) for k, v in vars_.items()
                                                      if k.startswith(lname + ".")}}
        if raw is not None and amt_tok:
            s_out["amount"] = {"human": step.amount, "raw": str(raw), "token": amt_tok["ref"],
                               "decimals": int(amt_tok["decimals"] if amt_tok["decimals"] is not None else NATIVE_DECIMALS)}
        if skipped:
            s_out["skipped"] = skipped
        out_steps.append(s_out)

    # revoke every approval at the end of the batch, so no allowance stays open
    revoke_calls = []
    for (_tok, _sp), (bc, act) in revokes.items():
        spender = to_checksum_address(abi_decode(["address", "uint256"], bc.data[4:])[0])
        rbc = replace(bc, data=bytes.fromhex(APPROVE_SELECTOR[2:]) + abi_encode(["address", "uint256"], [spender, 0]),
                      value=0)
        res = check_call(adapter, state, vault, rbc, probe_rpc)
        line = describe_call(rbc, names, {})[0]
        if res["status"] != "allowed":
            refused.append({"step": None, "action": f"revoke after {act}", "call": res.get("call", rbc.label),
                            "status": res["status"], "why": _plain_refusal(res, line)})
            continue
        call = Call(to=rbc.target, value=0, data=rbc.data, operation=rbc.operation, label=f"revoke {act} approve")
        revoke_calls.append({**call.to_json(), "contract": names.get(rbc.target.lower()), "function": APPROVE_SIG,
                             "args": {"spender": spender, "amount": "0"}, "line": line,
                             "check": {k: res[k] for k in ("status", "source", "reason", "notes", "loose") if k in res}})
        all_calls.append(call)
    if revoke_calls:
        out_steps.append({"step": len(out_steps) + 1, "action": "revoke_approvals",
                          "summary": "set every allowance this transaction made back to 0", "card_reviewed": True,
                          "calls": revoke_calls})
    if kept:
        warnings.append("approval left open on purpose (the spender pulls later, so it can't be revoked in the "
                        "same transaction): " + "; ".join(kept) + ". Revoke it once the order is filled")
    if keep_approvals and any(c.get("function") == APPROVE_SIG for s in out_steps for c in s["calls"]):
        warnings.append("--keep-approvals: allowances made here stay open after the transaction")

    # plain-language warnings
    if sum(1 for s in out_steps if s["action"] != "revoke_approvals") > 1:
        warnings.append("steps run in order in one transaction with fixed amounts: if a step produces less than the "
                        "next one spends (stETH rounds down 1-2 wei; wrap gives fewer wstETH than stETH), the whole "
                        "transaction reverts. Not simulated: check balances before proposing")
    if any(not s["card_reviewed"] for s in out_steps):
        warnings.append("uses draft cards (reviewed: false): tell the human")
    if state.log_source.startswith("role.lock.json"):
        warnings.append(f"checked against the role.lock.json snapshot at block {state.block}, not the live role; "
                        f"run `defios sync` (or set RPC_URL / ETHERSCAN_API_KEY) to re-read it")
    probed = bool(probe_rpc is not None and vault.operator)
    if not probed:
        warnings.append("not probed with eth_call (no RPC_URL or no Operator): offline evaluator only")

    operator_tx = None
    if all_calls and not refused:
        try:
            otx = build_operator_tx(all_calls, vault.roles_modifier, vault.role_key, vault.chain_id, batch,
                                    vault.multisend)
            warnings.extend(w for w in otx.warnings if "unwrapper" not in w)
            if otx.batch == "roles":
                key = f"{multisend_address(vault.chain_id, vault.multisend).lower()}:{MULTISEND_SELECTOR}"
                if key not in state.unwrappers:
                    warnings.append("the role has no MultiSend unwrapper: a batch of several calls is rejected by the "
                                    "Roles modifier; propose with --batch operator, or ask for the unwrapper")
            operator_tx = {"batch": otx.batch, "to": otx.safe_tx.to, "value": str(otx.safe_tx.value),
                           "data": "0x" + otx.safe_tx.data.hex(), "operation": otx.safe_tx.operation,
                           "note": "nonce and SafeTxHash are chosen by `defios propose`"}
        except TxError as e:
            refused.append({"step": None, "action": None, "call": None, "status": "tx", "why": str(e)})

    desc = description or " -> ".join(f"{s['action']}" + (f" {s['amount']['human']} {s['amount']['token']}"
                                                          if s.get("amount") else "") for s in out_steps)
    return {
        "vault": vault.name,
        "chain_id": vault.chain_id,
        "avatar": vault.avatar,
        "description": desc,
        "role": {"block": state.block, "source": role_source or state.log_source, "version": state.version,
                 "probe": probed},
        "ok": not refused,
        "steps": out_steps,
        "refused": refused,
        "warnings": warnings,
        "calls": [c.to_json() for c in all_calls] if not refused else [],
        "operator_tx": operator_tx,
    }


def calls_file(p: dict) -> dict:
    """The calls.json `defios propose --file` reads."""
    return {"description": p["description"], "chain_id": p["chain_id"], "vault": p["vault"],
            "generated_by": "defios plan", "role_block": p["role"]["block"], "calls": p["calls"]}


def summarize(p: dict) -> str:
    head = "PLAN" if p["ok"] else "REFUSED"
    lines = [(f"{head}: {p['description']} (vault {p['vault']}, role at block {p['role']['block']}, "
              f"{'eth_call probe' if p['role']['probe'] else 'offline check'})")]
    for s in p["steps"]:
        lines.append(f"  {s['step']}. {s['action']}: {s.get('summary') or ''}")
        for c in s["calls"]:
            lines.append(f"     - {c['line']}  [{c['check']['status']}]")
    for r in p["refused"]:
        lines.append(f"  REFUSED step {r['step']}: {r['why']}")
    for w in p["warnings"]:
        lines.append(f"  warning: {w}")
    if p.get("written"):
        lines.append(f"  wrote {p['written']}: next `defios propose --file {p['written']}` (dry run), "
                     f"then --send only after the human agrees")
    elif not p["ok"]:
        lines.append("  nothing written; do not propose. Explain the refusal to the human.")
    return "\n".join(lines)


def role_for_plan(cfg, vault: VaultConfig, logs_file: str | None, rpc) -> tuple[RoleState, RolesAdapter, str]:
    """The role to check against: --logs-file replay, else the live chain (Etherscan / RPC), else
    books/<vault>/role.lock.json (a snapshot; the plan says so)."""
    from .chain import ChainError, env
    from .engine import read_role
    from .roles import get_adapter
    if logs_file:
        st, ad, _ = read_role(vault, logs_file=logs_file, rpc=rpc)
        return st, ad, f"--logs-file {logs_file}"
    lock_path = Path(cfg.books_dir) / vault.name / "role.lock.json"
    if rpc is not None or env("ETHERSCAN_API_KEY"):
        try:
            st, ad, _ = read_role(vault, rpc=rpc)
            return st, ad, f"chain ({st.log_source})"
        except ChainError as e:
            if not lock_path.exists():
                raise
            from .engine import warn
            warn(f"live role read failed ({e}); falling back to {lock_path}")
    if not lock_path.exists():
        raise PlanError(f"no role to check against: run `defios sync` first (writes {lock_path}), "
                        f"set ETHERSCAN_API_KEY / RPC_URL, or pass --logs-file")
    lock = json.loads(lock_path.read_text())
    from .roles.v2 import role_key_bytes32
    if (str(lock.get("roles_modifier", "")).lower() != vault.roles_modifier.lower()
            or str(lock.get("role_key", "")).lower() != role_key_bytes32(vault.role_key).lower()):
        raise PlanError(f"{lock_path} is for {lock.get('roles_modifier')} / {lock.get('role_key')}, not this vault's "
                        f"{vault.roles_modifier} / {vault.role_key}; re-run `defios sync`")
    st = RoleState.from_lock_json(lock)
    return st, get_adapter(st.version, vault.roles_modifier, vault.chain_id), f"role.lock.json @ block {st.block}"
