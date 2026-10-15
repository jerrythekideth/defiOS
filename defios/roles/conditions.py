"""Zodiac Roles v2 condition model: enums, flat <-> tree, pretty printing,
and an offline evaluator that mirrors the on-chain PermissionChecker.

The evaluator is a best-effort port used by `defios check` to decide whether a
concrete call (built from a protocol card) would pass the decoded role. It is
never the final word: when RPC_URL is set, `check` confirms with an eth_call
probe of execTransactionWithRole against the live Roles modifier.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any

from eth_utils import keccak, to_checksum_address


class AbiType(IntEnum):
    """`ParameterType` in v2.0 naming, `AbiType` in the contracts repo."""

    NONE = 0
    STATIC = 1
    DYNAMIC = 2
    TUPLE = 3
    ARRAY = 4
    CALLDATA = 5
    ABI_ENCODED = 6


class Operator(IntEnum):
    PASS = 0
    AND = 1
    OR = 2
    NOR = 3
    MATCHES = 5
    ARRAY_SOME = 6
    ARRAY_EVERY = 7
    ARRAY_SUBSET = 8
    EQUAL_TO_AVATAR = 15
    EQUAL_TO = 16
    GREATER_THAN = 17
    LESS_THAN = 18
    SIGNED_INT_GREATER_THAN = 19
    SIGNED_INT_LESS_THAN = 20
    BITMASK = 21
    CUSTOM = 22
    WITHIN_ALLOWANCE = 28
    ETHER_WITHIN_ALLOWANCE = 29
    CALL_WITHIN_ALLOWANCE = 30


class ExecutionOptions(IntEnum):
    NONE = 0
    SEND = 1
    DELEGATE_CALL = 2
    BOTH = 3


class Clearance(IntEnum):
    NONE = 0
    TARGET = 1
    FUNCTION = 2


class Status(IntEnum):
    """PermissionChecker.Status (index = value in ConditionViolation(uint8,bytes32))."""

    Ok = 0
    DelegateCallNotAllowed = 1
    TargetAddressNotAllowed = 2
    FunctionNotAllowed = 3
    SendNotAllowed = 4
    OrViolation = 5
    NorViolation = 6
    ParameterNotAllowed = 7
    ParameterLessThanAllowed = 8
    ParameterGreaterThanAllowed = 9
    ParameterNotAMatch = 10
    NotEveryArrayElementPasses = 11
    NoArrayElementPasses = 12
    ParameterNotSubsetOfAllowed = 13
    BitmaskOverflow = 14
    BitmaskNotAllowed = 15
    CustomConditionViolation = 16
    AllowanceExceeded = 17
    CallAllowanceExceeded = 18
    EtherAllowanceExceeded = 19


def _name(enum_cls, value: int) -> str:
    try:
        return enum_cls(value).name
    except ValueError:
        return f"UNKNOWN_{value}"


def op_name(v: int) -> str:
    return _name(Operator, v)


def type_name(v: int) -> str:
    return _name(AbiType, v)


def options_name(v: int) -> str:
    return _name(ExecutionOptions, v)


LOGICAL = (Operator.AND, Operator.OR, Operator.NOR)


# --------------------------------------------------------------------------- #
# Flat <-> tree
# --------------------------------------------------------------------------- #


@dataclass
class Condition:
    param_type: int
    operator: int
    comp_value: bytes = b""
    children: list["Condition"] = field(default_factory=list)

    def to_json(self, sol_type: str | None = None) -> dict:
        d: dict[str, Any] = {
            "paramType": type_name(self.param_type),
            "operator": op_name(self.operator),
        }
        if self.comp_value:
            d["compValue"] = "0x" + self.comp_value.hex()
            readable = describe_comp_value(self)
            if readable is not None:
                d["value"] = readable
        if self.children:
            d["children"] = [c.to_json() for c in self.children]
        return d


def normalize_flat(flat: list) -> list[dict]:
    """Accept tuples from eth_abi or dicts and return dicts with bytes compValue."""
    out = []
    for c in flat:
        if isinstance(c, dict):
            cv = c.get("compValue", b"")
            if isinstance(cv, str):
                cv = bytes.fromhex(cv[2:] if cv.startswith("0x") else cv)
            out.append({"parent": int(c["parent"]), "paramType": int(c["paramType"]),
                        "operator": int(c["operator"]), "compValue": bytes(cv)})
        else:
            p, t, o, cv = c
            out.append({"parent": int(p), "paramType": int(t), "operator": int(o),
                        "compValue": bytes(cv)})
    return out


def flat_to_json(flat: list) -> list[dict]:
    return [
        {"parent": c["parent"], "paramType": c["paramType"], "operator": c["operator"],
         "compValue": "0x" + c["compValue"].hex()}
        for c in normalize_flat(flat)
    ]


def unflatten(flat: list) -> Condition:
    """Rebuild the condition tree from the BFS-ordered flat array emitted by ScopeFunction."""
    nodes = normalize_flat(flat)
    if not nodes:
        raise ValueError("empty condition array")
    if nodes[0]["parent"] != 0:
        raise ValueError("root node must be its own parent")
    objs = [Condition(n["paramType"], n["operator"], n["compValue"]) for n in nodes]
    for i in range(1, len(nodes)):
        p = nodes[i]["parent"]
        if p >= i:
            raise ValueError(f"condition {i} has parent {p}: not BFS ordered")
        objs[p].children.append(objs[i])
    return objs[0]


def flatten(root: Condition) -> list[dict]:
    """Inverse of unflatten (BFS order)."""
    out: list[dict] = []
    queue: list[tuple[Condition, int]] = [(root, 0)]
    while queue:
        node, parent = queue.pop(0)
        idx = len(out)
        out.append({"parent": parent, "paramType": int(node.param_type),
                    "operator": int(node.operator), "compValue": node.comp_value})
        for c in node.children:
            queue.append((c, idx))
    return out


def describe_comp_value(c: Condition) -> Any:
    """Human-friendly value for common compValues (addresses, uints)."""
    cv = c.comp_value
    if c.operator == Operator.EQUAL_TO and c.param_type == AbiType.STATIC and len(cv) == 32:
        if cv[:12] == b"\x00" * 12 and cv[12:] != b"\x00" * 20:
            n = int.from_bytes(cv, "big")
            if n > 2**64:
                return to_checksum_address(cv[12:])
        return int.from_bytes(cv, "big")
    if c.operator in (Operator.GREATER_THAN, Operator.LESS_THAN, Operator.WITHIN_ALLOWANCE) and len(cv) == 32:
        if c.operator == Operator.WITHIN_ALLOWANCE:
            return "allowance:0x" + cv.hex()
        return int.from_bytes(cv, "big")
    if c.operator == Operator.CUSTOM and len(cv) == 32:
        return {"adapter": to_checksum_address(cv[:20]), "extra": "0x" + cv[20:].hex()}
    return None


def render(c: Condition, names: list[str] | None = None, indent: int = 0) -> str:
    """One-line-per-node text rendering for summaries."""
    pad = "  " * indent
    v = describe_comp_value(c)
    head = f"{pad}{op_name(c.operator)}[{type_name(c.param_type)}]"
    if v is not None:
        head += f" {v}"
    lines = [head]
    for i, ch in enumerate(c.children):
        label = ""
        if names and indent == 0 and i < len(names):
            label = f"{names[i]}: "
        sub = render(ch, None, indent + 1)
        if label:
            sub = sub.replace(pad + "  ", pad + "  " + label, 1)
        lines.append(sub)
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Evaluator (port of AbiDecoder + PermissionChecker, Roles v2.1 semantics)
# --------------------------------------------------------------------------- #


@dataclass
class _TypeNode:
    t: int
    fields: list["_TypeNode"]


@dataclass
class _Payload:
    location: int
    size: int
    children: list["_Payload"]


class _OutOfBounds(Exception):
    pass


def _type_tree(c: Condition) -> _TypeNode:
    if c.operator in LOGICAL:
        if not c.children:
            return _TypeNode(AbiType.NONE, [])
        return _type_tree(c.children[0])
    kids = c.children[:1] if c.param_type == AbiType.ARRAY else c.children
    return _TypeNode(c.param_type, [_type_tree(k) for k in kids])


def _word(data: bytes, loc: int) -> int:
    if loc + 32 > len(data):
        raise _OutOfBounds()
    return int.from_bytes(data[loc:loc + 32], "big")


def _ceil32(n: int) -> int:
    return ((n + 31) // 32) * 32


class Evaluator:
    """Evaluate a condition tree against calldata.

    none_consumes_slot: how a child node with paramType None (a `Pass` written
    as None by some tools, or an allowance node) is decoded. In the deployed
    Roles v2.1 mastercopy (0x9646fDAD06d3e24444381f44362a3B0eB343D337) and on
    zodiac-modifier-roles main, a None leaf is "inline" with size 0: it takes
    NO calldata slot, so every later sibling is checked one word early. That
    turns e.g. `[None Pass, EqualTo(avatar)]` on `f(uint256, address)` into
    "param 0 == avatar". Default False (deployed behaviour).
    """

    def __init__(self, avatar: str, none_consumes_slot: bool = False):
        self.avatar = avatar
        self.none_consumes_slot = none_consumes_slot
        self.notes: list[str] = []

    # --- decoding ---------------------------------------------------------
    def _is_inline(self, t: _TypeNode) -> bool:
        if t.t == AbiType.STATIC:
            return True
        if t.t == AbiType.NONE:
            return not self.none_consumes_slot
        if t.t in (AbiType.DYNAMIC, AbiType.ARRAY, AbiType.CALLDATA, AbiType.ABI_ENCODED):
            return False
        return all(self._is_inline(f) for f in t.fields)

    def _walk(self, data: bytes, loc: int, t: _TypeNode) -> _Payload:
        p = _Payload(loc, 0, [])
        if t.t == AbiType.STATIC:
            p.size = 32
        elif t.t == AbiType.DYNAMIC:
            p.size = 32 + _ceil32(_word(data, loc))
        elif t.t == AbiType.TUPLE:
            self._block(data, loc, t, len(t.fields), p)
        elif t.t == AbiType.ARRAY:
            self._block(data, loc + 32, t, _word(data, loc), p)
            p.size += 32
        elif t.t in (AbiType.CALLDATA, AbiType.ABI_ENCODED):
            self._block(data, loc + 32 + (4 if t.t == AbiType.CALLDATA else 0), t, len(t.fields), p)
            p.size = 32 + _ceil32(_word(data, loc))
        # NONE: size 0, never read
        return p

    def _block(self, data: bytes, loc: int, t: _TypeNode, n: int, p: _Payload) -> None:
        offset = 0
        for i in range(n):
            f = t.fields[0] if t.t == AbiType.ARRAY else t.fields[i]
            if f.t == AbiType.NONE:
                child = _Payload(loc + offset, 0, [])
                p.children.append(child)
                if self.none_consumes_slot:
                    offset += 32
                    p.size += 32
                continue
            inline = self._is_inline(f)
            cloc = loc + offset if inline else loc + _word(data, loc + offset)
            child = self._walk(data, cloc, f)
            p.children.append(child)
            p.size += child.size + (0 if inline else 32)
            offset += child.size if inline else 32

    def inspect_calldata(self, root: Condition, data: bytes) -> _Payload:
        t = _type_tree(root)
        if t.t not in (AbiType.CALLDATA, AbiType.ABI_ENCODED):
            raise ValueError("root must be Calldata or AbiEncoded")
        p = _Payload(0, len(data), [])
        self._block(data, 4 if t.t == AbiType.CALLDATA else 0, t, len(t.fields), p)
        p.size = len(data)
        return p

    # --- checking ---------------------------------------------------------
    def check(self, root: Condition, data: bytes, value: int = 0) -> Status:
        try:
            payload = self.inspect_calldata(root, data)
            return self._eval(data, root, payload, value)
        except _OutOfBounds:
            return Status.ParameterNotAMatch

    def _eval(self, data: bytes, c: Condition, p: _Payload, value: int) -> Status:
        op = c.operator
        if op == Operator.PASS:
            return Status.Ok
        if op == Operator.MATCHES:
            if len(c.children) != len(p.children):
                return Status.ParameterNotAMatch
            for cc, pc in zip(c.children, p.children):
                s = self._eval(data, cc, pc, value)
                if s != Status.Ok:
                    return s
            return Status.Ok
        if op == Operator.AND:
            for cc in c.children:
                s = self._eval(data, cc, p, value)
                if s != Status.Ok:
                    return s
            return Status.Ok
        if op == Operator.OR:
            for cc in c.children:
                if self._eval(data, cc, p, value) == Status.Ok:
                    return Status.Ok
            return Status.OrViolation
        if op == Operator.NOR:
            for cc in c.children:
                if self._eval(data, cc, p, value) == Status.Ok:
                    return Status.NorViolation
            return Status.Ok
        if op == Operator.ARRAY_SOME:
            for pc in p.children:
                if self._eval(data, c.children[0], pc, value) == Status.Ok:
                    return Status.Ok
            return Status.NoArrayElementPasses
        if op == Operator.ARRAY_EVERY:
            for pc in p.children:
                if self._eval(data, c.children[0], pc, value) != Status.Ok:
                    return Status.NotEveryArrayElementPasses
            return Status.Ok
        if op == Operator.ARRAY_SUBSET:
            if not p.children or len(p.children) > len(c.children):
                return Status.ParameterNotSubsetOfAllowed
            taken = set()
            for pc in p.children:
                for j, cc in enumerate(c.children):
                    if j in taken:
                        continue
                    if self._eval(data, cc, pc, value) == Status.Ok:
                        taken.add(j)
                        break
                else:
                    return Status.ParameterNotSubsetOfAllowed
            return Status.Ok
        if op == Operator.EQUAL_TO_AVATAR:
            w = _word(data, p.location)
            return Status.Ok if w == int(self.avatar, 16) else Status.ParameterNotAllowed
        if op == Operator.EQUAL_TO:
            sl = data[p.location:p.location + p.size]
            if c.param_type == AbiType.STATIC:
                return Status.Ok if sl == c.comp_value[:32].rjust(32, b"\0") else Status.ParameterNotAllowed
            return Status.Ok if keccak(sl) == keccak(c.comp_value) else Status.ParameterNotAllowed
        if op in (Operator.GREATER_THAN, Operator.LESS_THAN):
            w = _word(data, p.location)
            cv = int.from_bytes(c.comp_value, "big")
            if op == Operator.GREATER_THAN and w <= cv:
                return Status.ParameterLessThanAllowed
            if op == Operator.LESS_THAN and w >= cv:
                return Status.ParameterGreaterThanAllowed
            return Status.Ok
        if op in (Operator.SIGNED_INT_GREATER_THAN, Operator.SIGNED_INT_LESS_THAN):
            def s(x: int) -> int:
                return x - 2**256 if x >= 2**255 else x
            w = s(_word(data, p.location))
            cv = s(int.from_bytes(c.comp_value, "big"))
            if op == Operator.SIGNED_INT_GREATER_THAN and w <= cv:
                return Status.ParameterLessThanAllowed
            if op == Operator.SIGNED_INT_LESS_THAN and w >= cv:
                return Status.ParameterGreaterThanAllowed
            return Status.Ok
        if op == Operator.BITMASK:
            cv = c.comp_value.rjust(32, b"\0")
            inline = c.param_type == AbiType.STATIC
            val = data[p.location + (0 if inline else 32): p.location + p.size]
            shift = int.from_bytes(cv[:2], "big")
            if shift >= len(val):
                return Status.BitmaskOverflow
            mask = cv[2:17]
            expected = cv[17:32]
            sl = val[shift:shift + 15].ljust(15, b"\0")
            ok = all((a & m) == e for a, m, e in zip(sl, mask, expected))
            return Status.Ok if ok else Status.BitmaskNotAllowed
        if op == Operator.CUSTOM:
            self.notes.append("custom condition not evaluated offline (assumed pass)")
            return Status.Ok
        if op in (Operator.WITHIN_ALLOWANCE, Operator.ETHER_WITHIN_ALLOWANCE, Operator.CALL_WITHIN_ALLOWANCE):
            self.notes.append("allowance condition not evaluated offline (assumed pass)")
            return Status.Ok
        self.notes.append(f"unknown operator {op} (treated as fail)")
        return Status.ParameterNotAllowed


def constrains_param(root: Condition, index: int) -> bool:
    """True when the condition for top-level param `index` is anything but Pass."""
    if index >= len(root.children):
        return False
    return _non_pass(root.children[index])


def _non_pass(c: Condition) -> bool:
    if c.operator == Operator.PASS:
        return any(_non_pass(ch) for ch in c.children)
    if c.operator == Operator.MATCHES:
        return any(_non_pass(ch) for ch in c.children)
    return True
