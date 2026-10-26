"""Protocol cards: loading, validation, signature parsing and call building.

A card is public, generic protocol knowledge (see catalog/SCHEMA.md). This module
turns card actions into concrete calldata so the engine — never the LLM — writes
the bytes.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from eth_abi import encode as abi_encode
from eth_utils import is_address, keccak, to_checksum_address

AVATAR = "$AVATAR"
DEAD = "0x000000000000000000000000000000000000dEaD"
RECEIVER_NAMES = {"receiver", "recipient", "owner", "onbehalfof", "to", "beneficiary", "benefactor",
                  "controller", "onbehalf", "_receiver", "_recipient", "_owner", "_to", "user", "account"}
VALUE_MOVING_KINDS_SAFE = {"oracle", "view", "registry"}


TOKEN = "$token"


class CardError(ValueError):
    pass


def _addr_word(addr: str) -> bytes:
    """An address as a left-padded bytes32 (CCTP-style mintRecipient)."""
    return bytes(12) + bytes.fromhex(to_checksum_address(addr)[2:])


def _as_type(addr: str, t: str):
    return _addr_word(addr) if t == "bytes32" else addr


# --------------------------------------------------------------------------- #
# Signature parsing
# --------------------------------------------------------------------------- #


@dataclass
class Param:
    name: str
    type: str  # canonical, e.g. "address", "(address,bytes)[]"
    components: list["Param"] = field(default_factory=list)  # for tuple / tuple[]

    @property
    def is_tuple(self) -> bool:
        return self.type.startswith("(")

    @property
    def is_array(self) -> bool:
        return self.type.endswith("]")

    @property
    def base(self) -> str:
        return self.type[: self.type.rfind("[")] if self.is_array else self.type


@dataclass
class Signature:
    name: str
    params: list[Param]
    outputs: list[Param]

    @property
    def canonical(self) -> str:
        return f"{self.name}({','.join(p.type for p in self.params)})"

    @property
    def selector(self) -> str:
        return "0x" + keccak(text=self.canonical)[:4].hex()

    def param(self, name: str) -> Param | None:
        for p in self.params:
            if p.name == name:
                return p
        return None


def _split_top(s: str) -> list[str]:
    out, depth, cur = [], 0, ""
    for ch in s:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            out.append(cur.strip())
            cur = ""
        else:
            cur += ch
    if cur.strip():
        out.append(cur.strip())
    return out


_TYPE_ALIASES = {"uint": "uint256", "int": "int256"}


def _parse_param(s: str, idx: int) -> Param:
    s = s.strip()
    if s.startswith("("):
        depth = 0
        for i, ch in enumerate(s):
            depth += ch == "("
            depth -= ch == ")"
            if depth == 0:
                break
        inner, rest = s[1:i], s[i + 1:].strip()
        m = re.match(r"^((?:\[\d*\])*)\s*(?:(?:memory|calldata)\s+)?(\w*)$", rest)
        if not m:
            raise CardError(f"cannot parse param {s!r}")
        comps = [_parse_param(x, j) for j, x in enumerate(_split_top(inner))]
        t = "(" + ",".join(c.type for c in comps) + ")" + m.group(1)
        return Param(m.group(2) or f"arg{idx}", t, comps)
    parts = s.split()
    parts = [p for p in parts if p not in ("memory", "calldata", "indexed", "payable")]
    t = parts[0]
    m = re.match(r"^([a-z]+\d*)((?:\[\d*\])*)$", t)
    if not m:
        raise CardError(f"cannot parse type {t!r}")
    t = _TYPE_ALIASES.get(m.group(1), m.group(1)) + m.group(2)
    return Param(parts[1] if len(parts) > 1 else f"arg{idx}", t)


def parse_signature(sig: str) -> Signature:
    """'deposit(uint256 assets, address receiver) returns (uint256)' or 'f(uint256)(uint256)'."""
    sig = " ".join(sig.split())
    m = re.match(r"^(\w+)\(", sig)
    if not m:
        raise CardError(f"cannot parse signature {sig!r}")
    name = m.group(1)
    body = sig[len(name):]
    depth, end = 0, None
    for i, ch in enumerate(body):
        depth += ch == "("
        depth -= ch == ")"
        if depth == 0:
            end = i
            break
    if end is None:
        raise CardError(f"unbalanced parentheses in {sig!r}")
    inner = body[1:end]
    tail = re.sub(r"^returns\s*", "", body[end + 1:].strip()).strip()
    outputs: list[Param] = []
    if tail:
        if not (tail.startswith("(") and tail.endswith(")")):
            raise CardError(f"cannot parse outputs of {sig!r}")
        outputs = [_parse_param(x, j) for j, x in enumerate(_split_top(tail[1:-1]))]
    params = [_parse_param(x, j) for j, x in enumerate(_split_top(inner))] if inner.strip() else []
    return Signature(name, params, outputs)


# --------------------------------------------------------------------------- #
# Cards
# --------------------------------------------------------------------------- #


REGISTRY_FILE = "registry.yaml"


@dataclass
class Card:
    protocol: str
    path: Path
    raw: dict
    registry_path: Path | None = None              # catalog/<protocol>/registry.yaml, when present
    registry_names: set = field(default_factory=set)  # contracts that came from it

    def source_file(self, name: str) -> Path:
        """The file a contract is written in: card.yaml or registry.yaml."""
        return self.registry_path if name in self.registry_names else self.path

    @property
    def contracts(self) -> dict:
        return self.raw.get("contracts") or {}

    @property
    def actions(self) -> dict:
        return self.raw.get("actions") or {}

    @property
    def reads(self) -> list:
        return self.raw.get("reads") or []


def _merge_registry(card: Card, path: Path) -> None:
    """Merge registry.yaml's `contracts:` into the card; a name in both files is an error."""
    raw = yaml.safe_load(path.read_text()) or {}
    if not isinstance(raw, dict) or set(raw) - {"contracts"}:
        raise CardError(f"{path}: only a top-level `contracts:` map is allowed")
    extra = raw.get("contracts") or {}
    if not isinstance(extra, dict):
        raise CardError(f"{path}: `contracts` must be a map")
    contracts = card.raw.get("contracts") or {}
    both = sorted(set(contracts) & set(extra))
    if both:
        raise CardError(f"{path}: {', '.join(both)} also defined in card.yaml")
    card.raw["contracts"] = {**contracts, **extra}
    card.registry_path = path
    card.registry_names = set(extra)


class Catalog:
    def __init__(self, cards: dict[str, Card]):
        self.cards = cards

    @classmethod
    def load(cls, directory: str | Path) -> "Catalog":
        d = Path(directory)
        if not d.is_dir():
            raise CardError(f"catalog directory {d} not found")
        cards = {}
        for p in sorted(d.glob("*/card.yaml")):
            raw = yaml.safe_load(p.read_text()) or {}
            proto = raw.get("protocol") or p.parent.name
            card = Card(proto, p, raw)
            reg = p.parent / REGISTRY_FILE
            if reg.is_file():
                _merge_registry(card, reg)
            cards[proto] = card
        for reg in sorted(d.glob(f"*/{REGISTRY_FILE}")):
            if not (reg.parent / "card.yaml").is_file():
                raise CardError(f"{reg}: no card.yaml next to it")
        return cls(cards)

    # ---- resolution ------------------------------------------------------
    def resolve_contract(self, ref: str, protocol: str, book_vars: dict | None = None,
                         _seen: set | None = None) -> str | None:
        """Return checksum address for 'name' (in `protocol`) or 'card.name', or None when it
        needs a book variable that is not set. Raises CardError on bad refs."""
        _seen = _seen or set()
        if (protocol, ref) in _seen:
            raise CardError(f"circular contract ref {protocol}.{ref}")
        _seen.add((protocol, ref))
        if is_address(ref):
            return to_checksum_address(ref)
        if ref.startswith("$book."):
            key = f"{protocol}.{ref[6:]}"
            v = (book_vars or {}).get(key)
            return to_checksum_address(v) if v and is_address(v) else None
        proto, name = protocol, ref
        if "." in ref:
            proto, name = ref.split(".", 1)
        card = self.cards.get(proto)
        if not card or name not in card.contracts:
            raise CardError(f"unknown contract ref {ref!r} (from {protocol})")
        c = card.contracts[name] or {}
        if "ref" in c:
            return self.resolve_contract(c["ref"], proto, book_vars, _seen)
        addr = c.get("address")
        if addr in (None, "TODO") or str(addr).upper().startswith("TODO"):
            return None
        if isinstance(addr, str) and addr.startswith("$book."):
            return self.resolve_contract(addr, proto, book_vars, _seen)
        if isinstance(addr, dict):
            return None  # read-time address (e.g. {read: "savETH.silo()"})
        if not is_address(str(addr)):
            raise CardError(f"{proto}.{name}: bad address {addr!r}")
        return to_checksum_address(str(addr))

    def is_contract_ref(self, s: str, protocol: str) -> bool:
        if not isinstance(s, str) or s.startswith("$") or is_address(s):
            return False
        proto, name = (s.split(".", 1) if "." in s else (protocol, s))
        card = self.cards.get(proto)
        return bool(card and name in card.contracts)

    def address_index(self, book_vars: dict | None = None) -> dict[str, list[tuple[str, str]]]:
        """lowercased address -> [(protocol, contract name)] for every resolvable contract."""
        idx: dict[str, list[tuple[str, str]]] = {}
        for proto, card in self.cards.items():
            for name, c in card.contracts.items():
                if c and "ref" in c:
                    continue
                try:
                    a = self.resolve_contract(name, proto, book_vars)
                except CardError:
                    a = None
                if a:
                    idx.setdefault(a.lower(), []).append((proto, name))
        return idx

    def contract_meta(self, protocol: str, name: str) -> dict:
        c = self.cards[protocol].contracts.get(name) or {}
        if "ref" in c:
            p2, n2 = c["ref"].split(".", 1) if "." in c["ref"] else (protocol, c["ref"])
            return self.contract_meta(p2, n2)
        return c

    def action_tokens(self, protocol: str, action: dict, book_vars: dict | None = None) -> list[str]:
        """Addresses of an action's `tokens:` list (the tokens `$token` may be), unresolved ones skipped."""
        out = []
        for ref in action.get("tokens") or []:
            a = self.resolve_contract(ref, protocol, book_vars)
            if a:
                out.append(a)
        return out

    def known_functions(self, book_vars: dict | None = None) -> dict[tuple[str, str], list[dict]]:
        """(target lower, selector) -> [{protocol, action, sig, dead, why}] across all cards."""
        out: dict[tuple[str, str], list[dict]] = {}
        for proto, card in self.cards.items():
            for aname, action in card.actions.items():
                for call in (action or {}).get("calls") or []:
                    if "sig" not in call or "target" not in call:
                        continue
                    try:
                        sig = parse_signature(call["sig"])
                        if call["target"] == TOKEN:
                            addrs = self.action_tokens(proto, action or {}, book_vars)
                        else:
                            addrs = [self.resolve_contract(call["target"], proto, book_vars)]
                    except CardError:
                        continue
                    for addr in addrs:
                        if addr:
                            out.setdefault((addr.lower(), sig.selector), []).append(
                                {"protocol": proto, "action": aname, "sig": sig, "call": call, "dead": False})
            for dead in card.raw.get("dead_permissions") or []:
                try:
                    addr = self.resolve_contract(dead["target"], proto, book_vars)
                except CardError:
                    continue
                sel = None
                sig = None
                if dead.get("sig") and not str(dead["sig"]).startswith("TODO"):
                    sig = parse_signature(dead["sig"])
                    sel = sig.selector
                elif dead.get("selector") and str(dead["selector"]).startswith("0x"):
                    sel = dead["selector"].lower()
                if addr and sel:
                    out.setdefault((addr.lower(), sel), []).append(
                        {"protocol": proto, "action": None, "sig": sig, "dead": True, "why": dead.get("why", "")})
        return out


# --------------------------------------------------------------------------- #
# Building calls from card actions
# --------------------------------------------------------------------------- #


@dataclass
class Pin:
    path: str  # e.g. "receiver", "order.receiver"
    expected: list[str]  # allowed addresses (checksum)
    kind: str  # "avatar" | "contract" | "set" | "book" | "literal"


@dataclass
class BuiltCall:
    protocol: str
    action: str
    index: int
    target_ref: str
    target: str | None
    sig: Signature | None
    data: bytes | None
    value: int
    operation: int
    pins: list[Pin]
    calls_params: list[str]
    optional: bool
    alt: bool
    error: str | None = None
    # (description, calldata, severity): severity "unsafe" for receivers/spenders/calls[],
    # "loose" for token-set pins (one_of) where a wrong value is not a direct theft path
    hostile: list[tuple[str, bytes, str]] = field(default_factory=list)
    raw: dict = field(default_factory=dict)

    @property
    def label(self) -> str:
        sig = self.sig.name if self.sig else self.raw.get("sig", "?")
        return f"{self.target_ref}.{sig}"


def _sample(t: str, comps: list[Param]) -> Any:
    if t.endswith("]"):
        base = t[: t.rfind("[")]
        inner_comps = comps
        if base.startswith("(") or base in ("bytes", "string"):
            return []
        return [_sample(base, inner_comps)]
    if t.startswith("("):
        return tuple(_sample(c.type, c.components) for c in comps)
    if t == "address":
        return to_checksum_address("0x" + "00" * 19 + "01")
    if t == "bool":
        return False
    if t == "string":
        return ""
    if t == "bytes":
        return b""
    if t.startswith("bytes"):
        return b"\0" * int(t[5:])
    if t.startswith("uint") or t.startswith("int"):
        bits = int(t[4:] if t.startswith("uint") else t[3:] or 256) if t not in ("uint", "int") else 256
        return 10**18 if bits >= 64 else 1
    raise CardError(f"no sample for type {t}")


def _hostile_element(base: str, comps: list[Param]) -> Any:
    if base.startswith("("):
        return tuple(_hostile_element(c.type, c.components) for c in comps)
    if base == "address":
        return to_checksum_address(DEAD)
    if base == "bytes":
        return bytes.fromhex("deadbeef") + b"\0" * 32
    return _sample(base, comps)


class CallBuilder:
    def __init__(self, catalog: Catalog, avatar: str, book_vars: dict | None = None):
        self.catalog = catalog
        self.avatar = to_checksum_address(avatar)
        self.book_vars = book_vars or {}

    def _value(self, spec: Any, p: Param, protocol: str, path: str, pins: list[Pin], vars_: dict) -> Any:
        """Turn an arg spec into a python value for eth_abi; collect pins."""
        t = p.type
        if spec is None:
            return _sample(t, p.components)
        if isinstance(spec, str):
            if spec == AVATAR:
                pins.append(Pin(path, [self.avatar], "avatar"))
                return _as_type(self.avatar, t)
            if spec == TOKEN and vars_.get("_tokens") and t == "address":
                pins.append(Pin(path, list(vars_["_tokens"]), "set"))
                if "token" not in vars_:
                    raise CardError("$token not chosen")
                return vars_["token"]
            if spec.startswith("$book."):
                a = self.catalog.resolve_contract(spec, protocol, self.book_vars)
                if not a:
                    raise CardError(f"book variable {protocol}.{spec[6:]} not set in defios.yaml book_vars")
                pins.append(Pin(path, [a], "book"))
                return a
            if spec.startswith("$"):
                if spec[1:] in vars_:
                    return vars_[spec[1:]]
                bv = self.book_vars.get(f"{protocol}.{spec[1:]}")
                if bv is not None:  # sample value for checks, e.g. cowswap.sell_token
                    if t == "address":
                        a = self.catalog.resolve_contract(bv, protocol, self.book_vars)
                        if a:
                            return a
                    elif t.startswith(("uint", "int")):
                        return int(bv, 0)
                return _sample(t, p.components)
            if t in ("address", "bytes32") and (is_address(spec) or self.catalog.is_contract_ref(spec, protocol)):
                a = self.catalog.resolve_contract(spec, protocol, self.book_vars)
                if not a:
                    raise CardError(f"contract {spec} has no address yet (TODO)")
                pins.append(Pin(path, [a], "contract" if not is_address(spec) else "literal"))
                return _as_type(a, t)
            if t.startswith("bytes") and spec.startswith("0x"):
                b = bytes.fromhex(spec[2:])
                return b if t == "bytes" else b.ljust(int(t[5:]), b"\0")
            if t.startswith(("uint", "int")):
                return int(spec, 0)
            return spec
        if isinstance(spec, bool):
            return spec
        if isinstance(spec, int):
            return spec
        if isinstance(spec, dict) and "one_of" in spec:
            addrs = []
            for r in spec["one_of"]:
                a = self.catalog.resolve_contract(r, protocol, self.book_vars) if r != AVATAR else self.avatar
                if a:
                    addrs.append(a)
            if not addrs:
                raise CardError(f"{path}: one_of resolved to nothing")
            pins.append(Pin(path, addrs, "set"))
            return addrs[0]
        if isinstance(spec, dict) and p.is_tuple and not p.is_array:
            out = []
            for c in p.components:
                out.append(self._value(spec.get(c.name), c, protocol, f"{path}.{c.name}", pins, vars_))
            return tuple(out)
        if isinstance(spec, list) and p.is_array:
            ep = Param(p.name, p.base, p.components)
            return [self._value(s, ep, protocol, f"{path}[{i}]", pins, vars_) for i, s in enumerate(spec)]
        raise CardError(f"{path}: unsupported arg spec {spec!r} for type {t}")

    def build(self, protocol: str, action: str, index: int, call: dict, vars_: dict | None = None) -> BuiltCall:
        vars_ = vars_ or {}
        bc = BuiltCall(protocol, action, index, call.get("target", "?"), None, None, None,
                       0, 1 if call.get("operation") == "delegatecall" else 0, [],
                       list(call.get("calls_params") or []), bool(call.get("optional")), bool(call.get("alt")),
                       raw=call)
        try:
            if call["target"] == TOKEN:
                bc.target = vars_.get("token")
                if not bc.target:
                    bc.error = "target $token: no token chosen (the action's `tokens:` list, or --token)"
                    return bc
            else:
                bc.target = self.catalog.resolve_contract(call["target"], protocol, self.book_vars)
            if not bc.target:
                bc.error = f"target {call['target']} has no address (TODO or book variable missing)"
                return bc
            bc.sig = parse_signature(call["sig"])
            args = call.get("args") or {}
            unknown = set(args) - {p.name for p in bc.sig.params}
            if unknown:
                raise CardError(f"args {sorted(unknown)} not in signature {bc.sig.canonical}")
            values = [self._value(args.get(p.name), p, protocol, p.name, bc.pins, vars_) for p in bc.sig.params]
            types = [p.type for p in bc.sig.params]
            sel = bytes.fromhex(bc.sig.selector[2:])
            bc.data = sel + abi_encode(types, values)
            if call.get("send"):
                bc.value = int(vars_.get("value", 10**18))
            # hostile variants: each pinned path -> DEAD; each calls[] param -> one hostile element
            for pin in bc.pins:
                dead = _as_type(DEAD, _type_at(bc.sig.params, pin.path) or "address")
                hv = _replace_path(values, bc.sig.params, pin.path, dead)
                if hv is not None:
                    bc.hostile.append((f"{pin.path} = 0x…dEaD (expected {pin.kind})", sel + abi_encode(types, hv),
                                       "loose" if pin.kind == "set" else "unsafe"))
            for name in bc.calls_params:
                p = bc.sig.param(name)
                if not p or not p.is_array:
                    raise CardError(f"calls_params {name!r} is not an array param of {bc.sig.canonical}")
                i = bc.sig.params.index(p)
                hv = list(values)
                hv[i] = [_hostile_element(p.base, p.components)]
                bc.hostile.append((f"{name}[] contains an arbitrary call (0x…dEaD, 0xdeadbeef)",
                                   sel + abi_encode(types, hv), "unsafe"))
        except CardError as e:
            bc.error = str(e)
        return bc


def _type_at(params: list[Param], path: str) -> str | None:
    """Solidity type of the value at `path` ("order.receiver", "_amounts[0]")."""
    parts = re.findall(r"[^.\[\]]+|\[\d+\]", path)
    plist, t = params, None
    for part in parts:
        if part.startswith("["):
            if t is None or not t.endswith("]"):
                return None
            t = t[: t.rfind("[")]
            continue
        p = next((x for x in plist if x.name == part), None)
        if p is None:
            return None
        t, plist = p.type, p.components
    return t


def _replace_path(values: list, params: list[Param], path: str, new: Any) -> list | None:
    parts = re.findall(r"[^.\[\]]+|\[\d+\]", path)
    out = list(values)

    def rec(container, plist: list[Param], parts_: list[str]):
        head = parts_[0]
        idx = next((i for i, p in enumerate(plist) if p.name == head), None)
        if idx is None:
            return None
        cont = list(container)
        p = plist[idx]
        rest = parts_[1:]
        if not rest:
            cont[idx] = new
            return cont
        if rest[0].startswith("["):
            j = int(rest[0][1:-1])
            arr = list(cont[idx])
            if len(rest) == 1:
                arr[j] = new
            else:
                sub = rec(arr[j], p.components, rest[1:])
                if sub is None:
                    return None
                arr[j] = tuple(sub)
            cont[idx] = arr
            return cont
        sub = rec(cont[idx], p.components, rest)
        if sub is None:
            return None
        cont[idx] = tuple(sub)
        return cont

    return rec(out, params, parts)


def receiver_like(p: Param) -> bool:
    return p.type == "address" and p.name.lower().lstrip("_") in {n.lstrip("_") for n in RECEIVER_NAMES}


def has_free_calls(p: Param) -> bool:
    """bytes params and arrays of tuples containing bytes (multicall-style)."""
    if p.type == "bytes":
        return True
    if p.is_array and p.base.startswith("("):
        return any(c.type == "bytes" for c in p.components)
    if p.is_array and p.base == "bytes":
        return True
    return False


def validate_card(catalog: Catalog, card: Card) -> list[str]:
    """Static checks used by tests and `defios catalog lint` (future)."""
    errs = []
    raw = card.raw
    for k in ("protocol", "chain_id", "status", "reviewed", "contracts", "actions"):
        if k not in raw:
            errs.append(f"{card.protocol}: missing `{k}`")
    if raw.get("status") != "draft" or raw.get("reviewed") is not False:
        errs.append(f"{card.protocol}: must be status: draft, reviewed: false until reviewed")
    for name, c in card.contracts.items():
        c = c or {}
        if "ref" in c:
            try:
                catalog.resolve_contract(c["ref"], card.protocol)
            except CardError as e:
                errs.append(str(e))
            continue
        a = c.get("address")
        if a is None:
            errs.append(f"{card.protocol}.{name}: no address")
        elif isinstance(a, str) and not (is_address(a) or a.startswith("TODO") or a.startswith("$book.")):
            errs.append(f"{card.protocol}.{name}: bad address {a!r}")
        elif isinstance(a, str) and is_address(a) and a != to_checksum_address(a):
            errs.append(f"{card.protocol}.{name}: address not checksummed")
        d = c.get("decimals")
        if d is not None and (not isinstance(d, int) or isinstance(d, bool) or not 0 <= d <= 77):
            errs.append(f"{card.protocol}.{name}: decimals must be an integer 0..77, got {d!r}")
    for aname, action in card.actions.items():
        action = action or {}
        inp = action.get("input")
        if inp is not None and inp != "ETH":
            try:
                if not catalog.is_contract_ref(str(inp), card.protocol):
                    errs.append(f"{card.protocol}.{aname}: input {inp!r} is not a contract of a card (or ETH)")
            except CardError as e:
                errs.append(f"{card.protocol}.{aname}: {e}")
        toks = action.get("tokens")
        if toks is not None:
            if not isinstance(toks, list) or not toks:
                errs.append(f"{card.protocol}.{aname}: `tokens` must be a non-empty list of card contracts")
            for ref in toks or []:
                if not catalog.is_contract_ref(str(ref), card.protocol):
                    errs.append(f"{card.protocol}.{aname}: token {ref!r} is not a contract of a card")
        if any(c.get("target") == TOKEN for c in action.get("calls") or []) and not toks:
            errs.append(f"{card.protocol}.{aname}: target $token needs a `tokens:` list")
        used_vars: set[str] = set()

        def _vars(spec):
            if isinstance(spec, str) and spec.startswith("$") and spec != AVATAR and not spec.startswith("$book."):
                used_vars.add(spec[1:])
            elif isinstance(spec, dict):
                for v in spec.values():
                    _vars(v)
            elif isinstance(spec, list):
                for v in spec:
                    _vars(v)

        for call in action.get("calls") or []:
            _vars(call.get("args") or {})
        for k in action.get("defaults") or {}:
            if k not in used_vars:
                errs.append(f"{card.protocol}.{aname}: default {k!r} is not a $variable of the action's calls")
        for i, call in enumerate(action.get("calls") or []):
            if "sig" not in call:
                if not any(k in call for k in ("sign", "external", "note")):
                    errs.append(f"{card.protocol}.{aname}[{i}]: call needs sig/target or sign/external")
                continue
            try:
                sig = parse_signature(call["sig"])
            except CardError as e:
                errs.append(f"{card.protocol}.{aname}[{i}]: {e}")
                continue
            for pname in (call.get("args") or {}):
                if not sig.param(pname):
                    errs.append(f"{card.protocol}.{aname}[{i}]: arg {pname} not in {sig.canonical}")
            for p in sig.params:
                if receiver_like(p) and (call.get("args") or {}).get(p.name) is None and p.name not in (call.get("free") or []):
                    errs.append(f"{card.protocol}.{aname}[{i}]: receiver-like param {p.name!r} not pinned "
                                f"(pin it or list it under `free:` with a reason)")
            if call["target"] == TOKEN:
                continue
            try:
                catalog.resolve_contract(call["target"], card.protocol)
            except CardError as e:
                errs.append(f"{card.protocol}.{aname}[{i}]: {e}")
    return errs
