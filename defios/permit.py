"""`defios permit`: card actions -> Roles v2 permission batch for the Vault queue.

    defios permit aave_v3.supply --token USDC dialectic.lock

The engine writes the conditions from the card (AGENTS.md rule 2), the same arg
specs `check` and `plan` build calldata from:

* `$AVATAR`, a card contract or a literal address -> EqualTo (bytes32 too)
* `one_of` and `$token` (the chosen tokens)     -> Or of EqualTo
* literal numbers / bools / bytes               -> EqualTo
* `$variables` and unset args                   -> a typed Pass (free)
* `calls_params` (multicall-style arrays)       -> refused: the card cannot scope them

Then it merges against the live role, never silently widening or narrowing it:

* already allowed (and the hostile variants still rejected) -> nothing to do
* nothing stored -> scopeTarget (if needed) + scopeFunction
* stored scope that differs in one address-set param only (e.g. approve's
  spender) -> the union, so existing spenders keep working
* anything else that differs, or a role looser than the card -> refused
  unless --replace

and verifies the result on a copy of the role: every requested call is allowed
with its hostile variants rejected, nothing allowed before breaks, and no new
high lint appears. Output is a Safe Transaction Builder JSON for the Vault (the
Roles modifier's owner) to queue; `defios decode` reads it back.
"""
from __future__ import annotations

import copy
import time
from dataclasses import dataclass, field
from typing import Any

from eth_abi import encode as abi_encode
from eth_utils import is_address, to_checksum_address

from .catalog import AVATAR, TOKEN, BuiltCall, CallBuilder, Catalog, CardError, Param, parse_signature
from .check import check_actions, check_call, detect_used_protocols, lint_role
from .config import VaultConfig
from .decode import SELECTORS
from .roles.base import FunctionState, RoleState, RolesAdapter, TargetState
from .roles.conditions import (
    AbiType,
    Clearance,
    Condition,
    ExecutionOptions,
    Operator,
    flatten,
    normalize_flat,
    options_name,
    render,
    unflatten,
)
from .roles.v2 import role_key_bytes32

WRITE_SELECTOR = {name: sel for sel, (name, _) in SELECTORS.items()}
_TYPES = {name: types for _, (name, types) in SELECTORS.items()}


class PermitError(ValueError):
    pass


# ------------------------------------------------------------------------------ card spec -> condition
def _pass(p: Param) -> Condition:
    """A Pass node carrying the param's type tree (what the Roles decoder needs to walk calldata)."""
    t = p.type
    if p.is_array:
        return Condition(AbiType.ARRAY, Operator.PASS, b"", [_pass(Param(p.name, p.base, p.components))])
    if p.is_tuple:
        return Condition(AbiType.TUPLE, Operator.PASS, b"", [_pass(c) for c in p.components])
    return Condition(AbiType.DYNAMIC if t in ("bytes", "string") else AbiType.STATIC, Operator.PASS)


def _pure_pass(c: Condition) -> bool:
    return c.operator == Operator.PASS and all(_pure_pass(k) for k in c.children)


def _eq(p: Param, value: Any) -> Condition:
    enc = abi_encode([p.type], [value])
    if p.type in ("bytes", "string"):
        return Condition(AbiType.DYNAMIC, Operator.EQUAL_TO, enc[32:])  # length word + padded data
    if p.is_array or p.is_tuple:
        raise PermitError(f"{p.name}: cannot pin a whole {p.type} to one literal")
    return Condition(AbiType.STATIC, Operator.EQUAL_TO, enc)


def _addr_eq(p: Param, addr: str) -> Condition:
    if p.type not in ("address", "bytes32"):
        raise PermitError(f"{p.name}: an address cannot pin a {p.type} param")
    return Condition(AbiType.STATIC, Operator.EQUAL_TO, bytes(12) + bytes.fromhex(to_checksum_address(addr)[2:]))


def _any_of(p: Param, addrs: list[str]) -> Condition:
    leaves = [_addr_eq(p, a) for a in dict.fromkeys(to_checksum_address(a) for a in addrs)]
    if not leaves:
        raise PermitError(f"{p.name}: no address to pin it to")
    return leaves[0] if len(leaves) == 1 else Condition(AbiType.NONE, Operator.OR, b"", leaves)


def _pinned(p: Param, value: Any, path: str) -> Condition:
    """EqualTo on every leaf of a looked-up value (a whole tuple, e.g. Morpho MarketParams)."""
    if p.is_tuple and not p.is_array:
        return Condition(AbiType.TUPLE, Operator.MATCHES, b"",
                         [_pinned(c, v, f"{path}.{c.name}") for c, v in zip(p.components, value)])
    if p.type == "address":
        return _addr_eq(p, value)
    if p.is_array:
        raise PermitError(f"{path}: cannot pin an array from a lookup")
    return _eq(p, value)


class ConditionBuilder:
    def __init__(self, catalog: Catalog, avatar: str, book_vars: dict | None = None):
        self.catalog = catalog
        self.avatar = to_checksum_address(avatar)
        self.book_vars = book_vars or {}

    def _resolve(self, ref: str, proto: str, path: str) -> str:
        try:
            a = self.catalog.resolve_contract(ref, proto, self.book_vars)
        except CardError as e:
            raise PermitError(f"{path}: {e}") from None
        if not a:
            raise PermitError(f"{path}: {ref} has no address (TODO, read-time, or a book variable not set "
                              f"in defios.yaml book_vars)")
        return a

    def param(self, spec: Any, p: Param, proto: str, path: str, tokens: list[str],
              vars_: dict | None = None) -> Condition:
        t = p.type
        vars_ = vars_ or {}
        if spec is None:
            return _pass(p)
        if isinstance(spec, bool) or isinstance(spec, int):
            return _eq(p, spec)
        if isinstance(spec, str):
            if spec == AVATAR:
                return _addr_eq(p, self.avatar)
            if spec == TOKEN:
                if not tokens:
                    raise PermitError(f"{path}: $token but the action has no `tokens:` list")
                return _any_of(p, tokens)
            if spec.startswith("$book."):
                return _addr_eq(p, self._resolve(spec, proto, path))
            if spec.startswith("$") and spec[1:] in vars_.get("_pinned", ()):
                return _pinned(p, vars_[spec[1:]], path)  # a looked-up value (Morpho market): pinned field by field
            if spec.startswith("$"):
                if spec[1:].split(".", 1)[0] in (self.catalog.cards[proto].raw.get("lookups") or {}):
                    lk = self.catalog.cards[proto].raw["lookups"][spec[1:].split(".", 1)[0]]
                    raise PermitError(f"{path}: {spec} must be pinned; pass --param {lk.get('param')}=<id>")
                return _pass(p)  # free variable: the agent picks it per transaction
            if t in ("address", "bytes32") and (is_address(spec) or self.catalog.is_contract_ref(spec, proto)):
                return _addr_eq(p, self._resolve(spec, proto, path))
            if t.startswith("bytes") and spec.startswith("0x"):
                b = bytes.fromhex(spec[2:])
                return _eq(p, b if t == "bytes" else b.ljust(int(t[5:]), b"\0"))
            if t.startswith(("uint", "int")):
                return _eq(p, int(spec, 0))
            if t == "string":
                return _eq(p, spec)
            raise PermitError(f"{path}: cannot turn {spec!r} into a condition on {t}")
        if isinstance(spec, dict) and "one_of" in spec:
            addrs = [self.avatar if r == AVATAR else self._resolve(r, proto, path) for r in spec["one_of"]]
            return _any_of(p, addrs)
        if isinstance(spec, dict) and p.is_tuple and not p.is_array:
            kids = [self.param(spec.get(c.name), c, proto, f"{path}.{c.name}", tokens, vars_) for c in p.components]
            if all(_pure_pass(k) for k in kids):
                return _pass(p)
            return Condition(AbiType.TUPLE, Operator.MATCHES, b"", kids)
        if isinstance(spec, list) and p.is_array:
            ep = Param(p.name, p.base, p.components)
            kids = [self.param(s, ep, proto, f"{path}[{i}]", tokens, vars_) for i, s in enumerate(spec)]
            if all(_pure_pass(k) for k in kids):
                return _pass(p)
            return Condition(AbiType.ARRAY, Operator.MATCHES, b"", kids)  # exact length, element by element
        raise PermitError(f"{path}: unsupported arg spec {spec!r} for type {t}")

    def call(self, proto: str, aname: str, i: int, call: dict, tokens: list[str],
             vars_: dict | None = None) -> tuple[Condition | None, int]:
        """(root condition or None for allowFunction, execution options) for one card call."""
        where = f"{proto}.{aname}[{i}]"
        if call.get("calls_params"):
            raise PermitError(f"{where}: {', '.join(call['calls_params'])} carries arbitrary calls; a role built from "
                              f"this card could not restrict them")
        sig = parse_signature(call["sig"])
        args = call.get("args") or {}
        kids = [self.param(args.get(p.name), p, proto, f"{where}.{p.name}", tokens, vars_) for p in sig.params]
        while kids and _pure_pass(kids[-1]):
            kids.pop()  # trailing free params need no node (zodiac-roles-sdk does the same)
        opts = ExecutionOptions.NONE
        if call.get("send"):
            opts |= ExecutionOptions.SEND
        if call.get("operation") == "delegatecall":
            opts |= ExecutionOptions.DELEGATE_CALL
        root = Condition(AbiType.CALLDATA, Operator.MATCHES, b"", kids) if kids else None
        return root, int(opts)


# ------------------------------------------------------------------------------ comparing / merging
def _key(c: Condition, avatar_word: bytes) -> Any:
    """Canonical form: Or children unordered, one-child Or = the child, EqualToAvatar = EqualTo(avatar)."""
    if c.operator == Operator.EQUAL_TO_AVATAR:
        return ("eq", AbiType.STATIC, avatar_word)
    if c.operator == Operator.EQUAL_TO and c.param_type == AbiType.STATIC:
        return ("eq", AbiType.STATIC, c.comp_value.rjust(32, b"\0"))
    if c.operator == Operator.OR:
        ks = frozenset(_key(k, avatar_word) for k in c.children)
        return next(iter(ks)) if len(ks) == 1 else ("or", ks)
    if c.operator == Operator.PASS:
        if c.param_type == AbiType.NONE:
            return ("pass-none",)
        return ("pass", int(c.param_type), tuple(_key(k, avatar_word) for k in c.children))
    return (int(c.operator), int(c.param_type), c.comp_value, tuple(_key(k, avatar_word) for k in c.children))


def _pass_like(c: Condition) -> Condition:
    if c.operator in (Operator.AND, Operator.OR, Operator.NOR):
        return _pass_like(c.children[0]) if c.children else Condition(AbiType.NONE, Operator.PASS)
    kids = c.children[:1] if c.param_type == AbiType.ARRAY else c.children
    return Condition(c.param_type, Operator.PASS, b"", [_pass_like(k) for k in kids])


def _addr_leaves(c: Condition, avatar_word: bytes) -> list[bytes] | None:
    """The 32-byte words of an address set (EqualTo / EqualToAvatar / Or of them), else None."""
    if c.operator == Operator.EQUAL_TO_AVATAR:
        return [avatar_word]
    if c.operator == Operator.EQUAL_TO and c.param_type == AbiType.STATIC and len(c.comp_value) == 32:
        return [c.comp_value]
    if c.operator == Operator.OR and c.children:
        out = []
        for k in c.children:
            sub = _addr_leaves(k, avatar_word)
            if sub is None:
                return None
            out += sub
        return out
    return None


def _pinned_leaves(c: Condition) -> bool:
    """Every leaf is an EqualTo (nothing free inside)."""
    if c.operator == Operator.MATCHES:
        return bool(c.children) and all(_pinned_leaves(k) for k in c.children)
    return c.operator in (Operator.EQUAL_TO, Operator.EQUAL_TO_AVATAR)


def _tuple_alts(c: Condition) -> list[Condition] | None:
    """A fully pinned tuple, or an Or of them, as a list of alternatives; None otherwise."""
    alts = c.children if c.operator == Operator.OR else [c]
    if alts and all(a.param_type == AbiType.TUPLE and a.operator == Operator.MATCHES and _pinned_leaves(a)
                    for a in alts):
        return list(alts)
    return None


def same(a: Condition | None, b: Condition | None, avatar: str) -> bool:
    if a is None or b is None:
        return a is b
    aw = bytes(12) + bytes.fromhex(to_checksum_address(avatar)[2:])
    ka, kb = list(a.children), list(b.children)
    ka += [_pass_like(x) for x in kb[len(ka):]]
    kb += [_pass_like(x) for x in ka[len(kb):]]
    return (a.param_type, a.operator) == (b.param_type, b.operator) and \
        [_key(x, aw) for x in ka] == [_key(x, aw) for x in kb]


def merge(old: Condition, new: Condition, avatar: str) -> tuple[Condition | None, str]:
    """Union of two scopes that differ in one top-level address-set param; (None, why) otherwise."""
    aw = bytes(12) + bytes.fromhex(to_checksum_address(avatar)[2:])
    if (old.param_type, old.operator) != (AbiType.CALLDATA, Operator.MATCHES) or \
            (new.param_type, new.operator) != (AbiType.CALLDATA, Operator.MATCHES):
        return None, "stored condition is not a plain calldata match"
    ko, kn = list(old.children), list(new.children)
    ko += [_pass_like(x) for x in kn[len(ko):]]
    kn += [_pass_like(x) for x in ko[len(kn):]]
    diff = [i for i, (x, y) in enumerate(zip(ko, kn)) if _key(x, aw) != _key(y, aw)]
    if not diff:
        return old, "same"
    if len(diff) > 1:
        return None, f"params {diff} differ"
    i = diff[0]
    to, tn = _tuple_alts(ko[i]), _tuple_alts(kn[i])
    if to is not None and tn is not None:  # whitelists of fully pinned tuples (Morpho markets): union
        have = {_key(x, aw) for x in to}
        add = [x for x in tn if _key(x, aw) not in have]
        if not add:
            return old, "same"
        alts = to + add
        ko[i] = Condition(AbiType.NONE, Operator.OR, b"", alts)
        while ko and _pure_pass(ko[-1]):
            ko.pop()
        return Condition(AbiType.CALLDATA, Operator.MATCHES, b"", ko), f"param {i}: added {len(add)} tuple(s)"
    lo, ln = _addr_leaves(ko[i], aw), _addr_leaves(kn[i], aw)
    if lo is None or ln is None:
        return None, f"param {i} differs and is not an address set"
    if set(ln) <= set(lo):
        return old, "same"
    words = list(dict.fromkeys(lo + ln))
    leaves = [Condition(AbiType.STATIC, Operator.EQUAL_TO, w) for w in words]
    ko[i] = leaves[0] if len(leaves) == 1 else Condition(AbiType.NONE, Operator.OR, b"", leaves)
    while ko and _pure_pass(ko[-1]):
        ko.pop()
    return Condition(AbiType.CALLDATA, Operator.MATCHES, b"", ko), f"param {i}: added {len(words) - len(set(lo))}"


# ------------------------------------------------------------------------------ ops
@dataclass
class Op:
    fn: str  # scopeTarget | scopeFunction | allowFunction | revokeFunction
    target: str
    selector: str | None = None
    condition: Condition | None = None
    options: int = 0
    why: str = ""
    function: str | None = None

    def flat(self) -> list[dict]:
        return flatten(self.condition) if self.condition is not None else []

    def calldata(self, role_key: str) -> bytes:
        rk = bytes.fromhex(role_key_bytes32(role_key)[2:])
        tgt = to_checksum_address(self.target)
        args: list[Any] = [rk, tgt]
        if self.fn != "scopeTarget":
            args.append(bytes.fromhex(self.selector[2:]))
        if self.fn == "scopeFunction":
            args.append([(n["parent"], n["paramType"], n["operator"], n["compValue"]) for n in self.flat()])
        if self.fn in ("scopeFunction", "allowFunction"):
            args.append(self.options)
        return bytes.fromhex(WRITE_SELECTOR[self.fn][2:]) + abi_encode(_TYPES[self.fn], args)

    def to_json(self) -> dict:
        d: dict[str, Any] = {"fn": self.fn, "target": to_checksum_address(self.target)}
        if self.selector:
            d["selector"] = self.selector
        if self.function:
            d["function"] = self.function
        if self.fn in ("scopeFunction", "allowFunction"):
            d["options"] = options_name(self.options)
        if self.condition is not None:
            d["condition"] = render(self.condition)
        d["why"] = self.why
        return d


def apply_ops(state: RoleState, ops: list[Op]) -> RoleState:
    """The role after the batch executes (same semantics as replaying the emitted events)."""
    st = copy.deepcopy(state)
    for op in ops:
        a = op.target.lower()
        if op.fn == "scopeTarget":
            st.targets[a] = TargetState(to_checksum_address(a), Clearance.FUNCTION, ExecutionOptions.NONE)
        elif op.fn == "revokeFunction":
            st.functions.pop((a, op.selector), None)
        elif op.fn == "allowFunction":
            st.functions[(a, op.selector)] = FunctionState(to_checksum_address(a), op.selector, True, op.options)
        elif op.fn == "scopeFunction":
            flat = normalize_flat(op.flat())
            st.functions[(a, op.selector)] = FunctionState(to_checksum_address(a), op.selector, False, op.options,
                                                           flat=flat, condition=unflatten(flat))
    return st


# ------------------------------------------------------------------------------ the command
@dataclass
class _Want:
    target: str
    selector: str
    function: str
    condition: Condition | None
    options: int
    labels: list[str] = field(default_factory=list)
    calls: list[BuiltCall] = field(default_factory=list)


def _selected_tokens(catalog: Catalog, proto: str, aname: str, action: dict, tokens: list[str] | None,
                     book_vars: dict) -> list[str]:
    allowed = catalog.action_tokens(proto, action, book_vars)
    if not action.get("tokens"):
        return []
    if not tokens:
        return allowed
    from .plan import PlanError, resolve_token
    out = []
    for t in tokens:
        try:
            a = resolve_token(catalog, t, book_vars)["address"]
        except PlanError as e:
            raise PermitError(str(e)) from None
        if a and a.lower() in {x.lower() for x in allowed}:
            out.append(to_checksum_address(a))
    if not out:
        raise PermitError(f"{proto}.{aname} takes one of {', '.join(str(r) for r in action['tokens'])}; "
                          f"none of --token {', '.join(tokens)} is in that list")
    return out


def _statuses(res: dict) -> dict[tuple[str, str], str]:
    out = {}
    for proto, acts in res.items():
        if not isinstance(acts, dict):
            continue
        for a, e in acts.items():
            if isinstance(e, dict) and "status" in e:
                out[(proto, a)] = e["status"]
                for tok, s in (e.get("tokens") or {}).items():
                    out[(proto, f"{a}[{tok}]")] = s
    return out


def split_ref(ref: str) -> tuple[str, dict]:
    """'morpho_markets.supply@market_id=0x…' -> ('morpho_markets.supply', {'market_id': '0x…'}): lookup params
    for one action, so one batch can permit the same action on two markets."""
    ref, at, rest = str(ref).partition("@")
    out = {}
    for kv in filter(None, rest.split(",")) if at else []:
        k, eq, v = kv.partition("=")
        if not eq or not k.strip():
            raise PermitError(f"{ref}@{rest}: expected name=value after @")
        out[k.strip()] = v.strip()
    return ref.strip(), out


def permit(catalog: Catalog, state: RoleState, adapter: RolesAdapter, vault: VaultConfig, refs: list[str], *,
           tokens: list[str] | None = None, with_optional: bool = False, replace: bool = False,
           description: str | None = None, role_source: str | None = None, params: dict | None = None,
           rpc=None) -> dict:
    from . import lookups
    from .plan import PlanError, resolve_action
    cb = ConditionBuilder(catalog, vault.avatar, vault.book_vars)
    builder = CallBuilder(catalog, vault.avatar, vault.book_vars)
    wants: dict[tuple[str, str], _Want] = {}
    notes: list[str] = []
    protos: list[str] = []
    for ref in refs:
        ref, ref_params = split_ref(ref)
        try:
            proto, aname, _ = resolve_action(catalog, ref, vault.book_vars)
        except PlanError as e:
            raise PermitError(str(e)) from None
        if proto not in protos:
            protos.append(proto)
        action = catalog.cards[proto].actions[aname] or {}
        toks = _selected_tokens(catalog, proto, aname, action, tokens, vault.book_vars)
        try:
            lk_vars = lookups.resolve(catalog, proto, action, {**(params or {}), **ref_params}, rpc, vault.book_vars)
        except lookups.LookupFailed as e:
            raise PermitError(f"{proto}.{aname}: {e}") from None
        if lookups.token_from(action, lk_vars):
            toks = [lookups.token_from(action, lk_vars)]
        for lname, lid in (lk_vars.get("_lookup_ids") or {}).items():
            chk = (lk_vars.get("_lookup_checks") or {}).get(lname)
            notes.append(f"{proto}.{aname}: {lname} {lid} "
                         + (f"proved by {chk}, pinned as target / address: " if chk else "pinned field by field: ")
                         + ", ".join(f"{k.split('.', 1)[1]}={v}" for k, v in lk_vars.items()
                                     if k.startswith(lname + ".")))
        for i, call in enumerate(action.get("calls") or []):
            if "sig" not in call:
                notes.append(f"{proto}.{aname}[{i}]: {call.get('sign') or call.get('external') or call.get('note')} "
                             f"happens outside the role (signature / third party); not a permission")
                continue
            if (call.get("optional") or call.get("alt")) and not with_optional:
                notes.append(f"{proto}.{aname}[{i}] {call['sig'].split('(')[0]} is optional; skipped "
                             f"(--with-optional adds it)")
                continue
            cond, opts = cb.call(proto, aname, i, call, toks, lk_vars)
            sig = parse_signature(call["sig"])
            label = f"{proto}.{aname}[{i}]" + "".join(f"@{str(x)[:10]}…" for x in
                                                       (lk_vars.get("_lookup_ids") or {}).values())
            for tok in (toks or [None]):
                bc = builder.build(proto, aname, i, call, {**lk_vars, **({"token": tok, "_tokens": toks} if tok else {})})
                if bc.error:
                    raise PermitError(f"{label}: {bc.error}")
                key = (bc.target.lower(), sig.selector)
                w = wants.get(key)
                if w is None:
                    wants[key] = _Want(bc.target, sig.selector, sig.canonical, cond, opts, [label], [bc])
                    continue
                if label not in w.labels:
                    if w.condition is None or cond is None:
                        w.condition = None if (w.condition is None and cond is None) else (w.condition or cond)
                        if w.condition is not None:
                            raise PermitError(f"{w.function} on {w.target}: {', '.join(w.labels)} leave it free but "
                                              f"{label} restricts it; split the request")
                    elif not same(w.condition, cond, vault.avatar):
                        m, why = merge(w.condition, cond, vault.avatar)
                        if m is None:
                            raise PermitError(f"{w.function} on {w.target}: {', '.join(w.labels)} and {label} need "
                                              f"different conditions ({why}); request them separately")
                        w.condition = m
                    w.labels.append(label)
                    w.options |= opts
                w.calls.append(bc)

    ops: list[Op] = []
    skipped: list[dict] = []
    refused: list[dict] = []
    scoped_targets: set[str] = set()
    for (addr, sel), w in wants.items():
        verdicts = [check_call(adapter, state, vault, bc) for bc in w.calls]
        sts = {v["status"] for v in verdicts}
        item = {"target": w.target, "selector": sel, "function": w.function, "for": w.labels}
        if sts == {"allowed"}:
            skipped.append({**item, "why": "already allowed"})
            continue
        t = state.target(addr)
        fs = state.function(addr, sel)
        if "unsafe" in sts and not replace:
            holes = sorted({h for v in verdicts for h in v.get("holes", [])})
            refused.append({**item, "why": "the role is looser than the card (accepts " + "; ".join(holes)
                            + "); --replace rescopes it to the card"})
            continue
        if t is not None and t.clearance == Clearance.TARGET:
            if not replace:
                refused.append({**item, "why": "target is fully allowed (allowTarget) but rejects this call "
                                "(execution options); --replace would rescope the whole target"})
                continue
        if (t is None or t.clearance != Clearance.FUNCTION) and addr not in scoped_targets:
            for (a2, s2), dfs in sorted(state.functions.items()):
                if a2 == addr and s2 not in {k[1] for k in wants if k[0] == addr}:
                    ops.append(Op("revokeFunction", w.target, s2, why="dormant scope that scopeTarget would revive"))
            ops.append(Op("scopeTarget", w.target, why="target not in the role yet"))
            scoped_targets.add(addr)
        if t is None or t.clearance != Clearance.FUNCTION:
            fs = None  # a dormant scope is not merged with: scopeFunction overwrites it
        cond, opts, why = w.condition, w.options, "not in the role yet"
        if fs is not None:
            opts |= fs.options
            if "unsafe" in sts:
                why = "rescoped to the card (--replace): the stored scope accepted hostile variants"
            elif fs.wildcarded:
                cond, why = None, "allowed with no conditions; widening execution options to " + options_name(opts)
            elif cond is None:
                why = "card leaves every param free (replaces the stored scope)"
                if not replace:
                    refused.append({**item, "why": "card leaves every param free but the role restricts it; "
                                    "--replace would drop the stored conditions"})
                    continue
            elif fs.condition is None:
                refused.append({**item, "why": "stored condition cannot be decoded; refusing to touch it"})
                continue
            else:
                m, mwhy = merge(fs.condition, cond, vault.avatar)
                if m is not None:
                    cond, why = m, ("execution options widened" if mwhy == "same" else f"merged with the stored scope "
                                    f"({mwhy})")
                elif replace:
                    why = f"replaces the stored scope (--replace; {mwhy})"
                else:
                    refused.append({**item, "why": f"stored scope differs ({mwhy}); --replace would overwrite it",
                                    "stored": render(fs.condition), "card": render(cond)})
                    continue
        if cond is None:
            ops.append(Op("allowFunction", w.target, sel, None, opts, why, w.function))
        else:
            ops.append(Op("scopeFunction", w.target, sel, cond, opts, why, w.function))

    result: dict[str, Any] = {
        "vault": vault.name, "block": state.block, "roles_modifier": vault.roles_modifier,
        "role_key": vault.role_key, "role_source": role_source or state.log_source,
        "requested": refs, "tokens": tokens or [], "ops": [o.to_json() for o in ops],
        "already_allowed": skipped, "refused": refused, "notes": notes, "warnings": [],
    }
    owner = state.module.get("owner")
    if owner and owner.lower() != vault.avatar.lower():
        result["warnings"].append(f"the Roles modifier's owner is {owner}, not the Vault; this batch must be "
                                  f"queued on the owner instead")
    if refused:
        result["ok"] = False
        return result
    if not ops:
        result.update(ok=True, batch=None)
        return result

    after = apply_ops(state, ops)
    problems = []
    for w in wants.values():
        for bc in w.calls:
            v = check_call(adapter, after, vault, bc)
            if v["status"] != "allowed":
                problems.append(f"{bc.label} for {', '.join(w.labels)} is {v['status']} after the batch: "
                                f"{v.get('reason')}")
    used = sorted(set(detect_used_protocols(catalog, state, vault.book_vars)) | set(protos))
    before_s = _statuses(check_actions(catalog, state, adapter, vault, used))
    after_s = _statuses(check_actions(catalog, after, adapter, vault, used))
    works = {"allowed": 2, "needs_signature": 2, "unsafe": 2, "partial": 1}  # unsafe still runs today
    regressions = [f"{p}.{a}: {s} -> {after_s.get((p, a))}" for (p, a), s in sorted(before_s.items())
                   if works.get(after_s.get((p, a)), 0) < works.get(s, 0)]
    lk = lambda l: (l["code"], l.get("target", ""), l.get("selector", ""), l.get("param", ""))  # noqa: E731
    old_high = {lk(l) for l in lint_role(catalog, state, vault) if l["severity"] == "high"}
    new_high = [l for l in lint_role(catalog, after, vault) if l["severity"] == "high" and lk(l) not in old_high]
    result["regressions"] = regressions
    result["new_high_lints"] = new_high
    result["after"] = {f"{p}.{a}": after_s[(p, a)] for (p, a) in sorted(after_s)
                       if p in protos and "[" not in a}
    if regressions and not replace:
        problems.append("the batch would break what works today: " + "; ".join(regressions))
    if new_high:
        problems += [f"new high lint {l['code']}: {l.get('target_name') or l.get('target')} "
                     f"{l.get('function') or l.get('selector')}: {l['message']}" for l in new_high]
    if problems:
        result.update(ok=False, problems=problems)
        return result
    if regressions:
        result["warnings"].append("--replace: these stop working: " + "; ".join(regressions))
    result["ok"] = True
    result["batch"] = tx_builder_json(vault, ops, description or f"defios permit {' '.join(refs)}")
    return result


def tx_builder_json(vault: VaultConfig, ops: list[Op], description: str) -> dict:
    return {
        "version": "1.0",
        "chainId": str(vault.chain_id),
        "createdAt": int(time.time() * 1000),
        "meta": {"name": "Transactions Batch", "description": description, "txBuilderVersion": "1.16.5",
                 "createdFromSafeAddress": to_checksum_address(vault.avatar), "createdFromOwnerAddress": ""},
        "transactions": [{"to": to_checksum_address(vault.roles_modifier), "value": "0",
                          "data": "0x" + op.calldata(vault.role_key).hex(), "contractMethod": None,
                          "contractInputsValues": None} for op in ops],
    }


def summarize(r: dict) -> str:
    lines = [f"permit {' '.join(r['requested'])} for {r['vault']} @ block {r['block']} ({r['role_source']})"]
    for s in r["already_allowed"]:
        lines.append(f"  = {s['function']} on {s['target']}: already allowed ({', '.join(s['for'])})")
    for o in r["ops"]:
        lines.append(f"  + {o['fn']} {o['target']} {o.get('function') or ''} {o['why']}".rstrip())
        if o.get("condition"):
            lines.extend("      " + ln for ln in o["condition"].splitlines())
    for x in r["refused"]:
        lines.append(f"  ! refused {x['function']} on {x['target']}: {x['why']}")
    for p in r.get("problems", []):
        lines.append(f"  ! {p}")
    for n in r["notes"]:
        lines.append(f"  note: {n}")
    for w in r["warnings"]:
        lines.append(f"  warning: {w}")
    if r["ok"] and r.get("batch"):
        lines.append(f"  {len(r['ops'])} Roles txs for the Vault queue; review with `defios decode`, simulate, "
                     f"then the Vault owners sign")
    elif r["ok"]:
        lines.append("  nothing to change: the role already allows all of it")
    else:
        lines.append("  nothing written. Explain the refusal to the human.")
    return "\n".join(lines)
