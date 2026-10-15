"""Zodiac Roles Modifier v2 adapter.

Roles v2 stores conditions in packed bytecode with no read function, so the
modifier's own events are the only complete source of a role's permissions.
`read_role` replays every event the modifier ever emitted, in order, up to the
requested block. Event definitions come from gnosisguild/zodiac-modifier-roles
(packages/evm/contracts: PermissionBuilder.sol, Roles.sol, _Periphery.sol,
AllowanceTracker.sol) plus the Zodiac Modifier base events.

Ported and generalized from scytherxyz/permissions main.py (load_roles_cache),
which first handled the deployed modifier emitting events with no indexed
params (all fields in `data`).
"""
from __future__ import annotations

from eth_abi import decode as abi_decode
from eth_abi import encode as abi_encode
from eth_utils import keccak, to_checksum_address

from ..chain import CallReverted, ChainError
from .base import CallVerdict, FunctionState, ProbeResult, RoleState, RolesAdapter, TargetState
from .conditions import (
    Clearance,
    Evaluator,
    ExecutionOptions,
    Status,
    normalize_flat,
    unflatten,
)

CONDITION_FLAT = "(uint8,uint8,uint8,bytes)[]"

# name -> list of (arg name, type, indexed)
EVENTS: dict[str, list[tuple[str, str, bool]]] = {
    "AllowTarget": [("roleKey", "bytes32", False), ("targetAddress", "address", False), ("options", "uint8", False)],
    "RevokeTarget": [("roleKey", "bytes32", False), ("targetAddress", "address", False)],
    "ScopeTarget": [("roleKey", "bytes32", False), ("targetAddress", "address", False)],
    "AllowFunction": [("roleKey", "bytes32", False), ("targetAddress", "address", False),
                      ("selector", "bytes4", False), ("options", "uint8", False)],
    "RevokeFunction": [("roleKey", "bytes32", False), ("targetAddress", "address", False),
                       ("selector", "bytes4", False)],
    "ScopeFunction": [("roleKey", "bytes32", False), ("targetAddress", "address", False),
                      ("selector", "bytes4", False), ("conditions", CONDITION_FLAT, False),
                      ("options", "uint8", False)],
    "SetAllowance": [("allowanceKey", "bytes32", False), ("balance", "uint128", False),
                     ("maxRefill", "uint128", False), ("refill", "uint128", False),
                     ("period", "uint64", False), ("timestamp", "uint64", False)],
    "ConsumeAllowance": [("allowanceKey", "bytes32", False), ("consumed", "uint128", False),
                         ("newBalance", "uint128", False)],
    "AssignRoles": [("module", "address", False), ("roleKeys", "bytes32[]", False), ("memberOf", "bool[]", False)],
    "SetDefaultRole": [("module", "address", False), ("defaultRoleKey", "bytes32", False)],
    "SetUnwrapAdapter": [("to", "address", False), ("selector", "bytes4", False), ("adapter", "address", False)],
    "RolesModSetup": [("initiator", "address", True), ("owner", "address", True), ("avatar", "address", True),
                      ("target", "address", False)],
    "AvatarSet": [("previousAvatar", "address", True), ("newAvatar", "address", True)],
    "TargetSet": [("previousTarget", "address", True), ("newTarget", "address", True)],
    "OwnershipTransferred": [("previousOwner", "address", True), ("newOwner", "address", True)],
    "EnabledModule": [("module", "address", False)],
    "DisabledModule": [("module", "address", False)],
}


def event_signature(name: str) -> str:
    return f"{name}({','.join(t for _, t, _ in EVENTS[name])})"


TOPIC0 = {"0x" + keccak(text=event_signature(n)).hex(): n for n in EVENTS}

EXEC_WITH_ROLE_SIG = "execTransactionWithRole(address,uint256,bytes,uint8,bytes32,bool)"
ERRORS = {
    "d0a9bf58": "ConditionViolation",
    "fd8e9f28": "NoMembership",
    "4684c122": "FunctionSignatureTooShort",
    "15e649e9": "MalformedMultiEntrypoint",
    "d27b44a9": "ModuleTransactionFailed",
}


def role_key_bytes32(key: str) -> str:
    """'MANAGER_ROLE' -> utf8 right-padded bytes32 (zodiac-roles app convention);
    '0x…' (<=32 bytes) -> left as is, right-padded to 32 bytes if shorter."""
    if key.startswith("0x"):
        h = key[2:].lower()
        if len(h) > 64:
            raise ValueError("role key longer than 32 bytes")
        return "0x" + h.ljust(64, "0")
    b = key.encode()
    if len(b) > 32:
        raise ValueError("role key string longer than 32 bytes")
    return "0x" + b.hex().ljust(64, "0")


def role_key_label(key_hex: str) -> str:
    raw = bytes.fromhex(key_hex[2:]).rstrip(b"\0")
    try:
        s = raw.decode()
        if s and s.isprintable():
            return s
    except UnicodeDecodeError:
        pass
    return key_hex


def decode_log(log: dict) -> tuple[str, dict] | None:
    """Decode one Roles/Modifier log. Handles both layouts: params in topics
    (indexed as declared) or everything in data (topics == [topic0])."""
    topics = [t.lower() for t in log.get("topics") or []]
    if not topics:
        return None
    name = TOPIC0.get(topics[0])
    if not name:
        return None
    spec = EVENTS[name]
    data_hex = log.get("data") or "0x"
    data = bytes.fromhex(data_hex[2:]) if len(data_hex) > 2 else b""
    n_indexed = sum(1 for _, _, ix in spec if ix)
    values: dict = {}
    if n_indexed and len(topics) == 1 + n_indexed:
        it = iter(topics[1:])
        rest = [(n, t) for n, t, ix in spec if not ix]
        for n, t, ix in spec:
            if ix:
                values[n] = abi_decode([t], bytes.fromhex(next(it)[2:]))[0]
        if rest:
            dec = abi_decode([t for _, t in rest], data)
            values.update({n: v for (n, _), v in zip(rest, dec)})
    elif len(topics) == 1:
        dec = abi_decode([t for _, t, _ in spec], data)
        values = {n: v for (n, _, _), v in zip(spec, dec)}
    else:
        # Unexpected layout: try "all non-indexed in data", topics as leading params.
        rest = spec[len(topics) - 1:]
        dec = abi_decode([t for _, t, _ in rest], data)
        for (n, t, _), tp in zip(spec, topics[1:]):
            values[n] = abi_decode([t], bytes.fromhex(tp[2:]))[0]
        values.update({n: v for (n, _, _), v in zip(rest, dec)})
    return name, values


def _hx(b: bytes) -> str:
    return "0x" + bytes(b).hex()


def _addr(a: str) -> str:
    return a.lower()


class RolesV2(RolesAdapter):
    version = "v2"

    # ------------------------------------------------------------------ detect
    @classmethod
    def detect(cls, rpc, address: str | None = None) -> dict:  # type: ignore[override]
        """v2 exposes allowances(bytes32) and unwrappers(bytes32) getters; v1 does not."""
        ev: dict = {"match": False}
        try:
            code = rpc.get_code(address)
        except ChainError as e:
            return {"match": False, "error": str(e)}
        ev["has_code"] = bool(code)
        if len(code) == 45 and code[:10] == bytes.fromhex("363d3d373d3d3d363d73"):
            ev["mastercopy"] = to_checksum_address(code[10:30])
        try:
            rpc.call(address, "allowances(bytes32)", [b"\0" * 32], ["uint128", "uint128", "uint64", "uint128", "uint64"])
            rpc.call(address, "unwrappers(bytes32)", [b"\0" * 32], ["address"])
            ev["match"] = True
        except ChainError as e:
            ev["error"] = str(e)
        return ev

    # ---------------------------------------------------------------- replay
    def read_role(self, role_key: str, log_source, to_block: int | None = None,
                  from_block: int = 0) -> RoleState:
        key = role_key_bytes32(role_key)
        latest = log_source.latest_block() if to_block is None else to_block
        logs = log_source.fetch(self.address, from_block, latest)
        return self.replay(key, logs, latest, getattr(log_source, "name", "?"))

    def replay(self, key: str, logs: list[dict], block: int, source: str = "?") -> RoleState:
        st = RoleState(version=self.version, chain_id=self.chain_id,
                       roles_modifier=to_checksum_address(self.address), role_key=key,
                       block=block, log_source=source)
        st.module["role_key_label"] = role_key_label(key)
        members: dict[str, bool] = {}
        roles_seen: set[str] = set()
        all_allowances: dict[str, dict] = {}
        n = 0
        for lg in logs:
            try:
                dec = decode_log(lg)
            except Exception as e:  # noqa: BLE001
                st.warnings.append(f"could not decode log {lg.get('transactionHash')}#{lg.get('logIndex')}: {e}")
                continue
            if not dec:
                continue
            name, v = dec
            n += 1
            where = {"block": lg["blockNumber"], "tx": lg.get("transactionHash"), "event": name}
            rk = _hx(v["roleKey"]) if "roleKey" in v else None
            if rk:
                roles_seen.add(rk)
            if name == "AssignRoles":
                for k, m in zip(v["roleKeys"], v["memberOf"]):
                    roles_seen.add(_hx(k))
                    if _hx(k) == key:
                        members[_addr(v["module"])] = bool(m)
                        st.last_change = where
                continue
            if name == "SetDefaultRole":
                st.default_roles[to_checksum_address(v["module"])] = _hx(v["defaultRoleKey"])
                continue
            if name == "SetAllowance":
                all_allowances[_hx(v["allowanceKey"])] = {
                    "balance": v["balance"], "maxRefill": v["maxRefill"], "refill": v["refill"],
                    "period": v["period"], "timestamp": v["timestamp"], "set_at": where}
                continue
            if name == "ConsumeAllowance":
                a = all_allowances.setdefault(_hx(v["allowanceKey"]), {})
                a["balance"] = v["newBalance"]
                continue
            if name == "SetUnwrapAdapter":
                st.unwrappers[f"{_addr(v['to'])}:{_hx(v['selector'])}"] = to_checksum_address(v["adapter"])
                continue
            if name == "RolesModSetup":
                st.module.update({"owner": to_checksum_address(v["owner"]),
                                  "avatar": to_checksum_address(v["avatar"]),
                                  "target": to_checksum_address(v["target"]), "setup_block": lg["blockNumber"]})
                continue
            if name == "AvatarSet":
                st.module["avatar"] = to_checksum_address(v["newAvatar"])
                continue
            if name == "TargetSet":
                st.module["target"] = to_checksum_address(v["newTarget"])
                continue
            if name in ("EnabledModule", "DisabledModule"):
                mods = st.module.setdefault("enabled_modules", [])
                m = to_checksum_address(v["module"])
                if name == "EnabledModule" and m not in mods:
                    mods.append(m)
                elif name == "DisabledModule" and m in mods:
                    mods.remove(m)
                continue
            if name == "OwnershipTransferred":
                st.module["owner"] = to_checksum_address(v["newOwner"])
                continue
            if rk != key:
                continue
            tgt = _addr(v["targetAddress"])
            st.last_change = where
            if name == "AllowTarget":
                st.targets[tgt] = TargetState(to_checksum_address(tgt), Clearance.TARGET, int(v["options"]), where)
            elif name == "RevokeTarget":
                st.targets[tgt] = TargetState(to_checksum_address(tgt), Clearance.NONE, ExecutionOptions.NONE, where)
            elif name == "ScopeTarget":
                st.targets[tgt] = TargetState(to_checksum_address(tgt), Clearance.FUNCTION, ExecutionOptions.NONE, where)
            else:
                sel = _hx(v["selector"])
                fkey = (tgt, sel)
                if name == "RevokeFunction":
                    st.functions.pop(fkey, None)
                elif name == "AllowFunction":
                    st.functions[fkey] = FunctionState(to_checksum_address(tgt), sel, True, int(v["options"]),
                                                       changed_at=where)
                elif name == "ScopeFunction":
                    flat = normalize_flat(v["conditions"])
                    try:
                        tree = unflatten(flat)
                    except ValueError as e:
                        st.warnings.append(f"{tgt}:{sel} bad condition tree: {e}")
                        tree = None
                    st.functions[fkey] = FunctionState(to_checksum_address(tgt), sel, False, int(v["options"]),
                                                       flat=flat, condition=tree, changed_at=where)
        st.members = sorted(to_checksum_address(m) for m, on in members.items() if on)
        st.roles_seen = sorted(roles_seen)
        st.events_replayed = n
        # keep allowances referenced by this role's conditions, plus all for context
        referenced = set()
        for fs in st.functions.values():
            for c in fs.flat:
                if c["operator"] in (28, 29, 30):
                    referenced.add("0x" + c["compValue"].hex())
        st.allowances = {k: {**v, "referenced_by_role": k in referenced} for k, v in all_allowances.items()}
        for k in referenced - set(all_allowances):
            st.allowances[k] = {"referenced_by_role": True, "never_set": True}
        return st

    # ----------------------------------------------------------- evaluation
    def check_call(self, state: RoleState, to: str, value: int, data: bytes,
                   operation: int = 0, avatar: str | None = None) -> CallVerdict:
        t = state.target(to)
        if t is None or t.clearance == Clearance.NONE:
            return CallVerdict(Status.TargetAddressNotAllowed, "target not allowed")
        if t.clearance == Clearance.TARGET:
            s = _exec_options(value, operation, t.options)
            return CallVerdict(s, "target fully allowed (allowTarget)" if s == Status.Ok else "execution options")
        if len(data) < 4:
            return CallVerdict(Status.FunctionNotAllowed, "calldata shorter than a selector")
        sel = "0x" + data[:4].hex()
        notes = []
        if f"{to.lower()}:{sel}" in state.unwrappers:
            notes.append("target+selector has an unwrap adapter (e.g. MultiSend); inner calls checked separately")
        fs = state.function(to, sel)
        if fs is None:
            return CallVerdict(Status.FunctionNotAllowed, f"function {sel} not allowed", notes)
        s = _exec_options(value, operation, fs.options)
        if s != Status.Ok:
            return CallVerdict(s, "execution options", notes)
        if fs.wildcarded:
            return CallVerdict(Status.Ok, "function allowed with no conditions (allowFunction)", notes)
        if fs.condition is None:
            return CallVerdict(Status.ParameterNotAllowed, "condition tree could not be decoded", notes)
        av = avatar or state.module.get("avatar")
        if not av:
            notes.append("avatar unknown; EqualToAvatar evaluated against zero address")
            av = "0x" + "00" * 20
        ev = Evaluator(av)
        status = ev.check(fs.condition, data, value)
        return CallVerdict(status, "scoped function", notes + ev.notes)

    # ---------------------------------------------------------------- probe
    def exec_with_role_calldata(self, role_key: str, to: str, value: int, data: bytes,
                                operation: int = 0, should_revert: bool = False) -> bytes:
        sel = keccak(text=EXEC_WITH_ROLE_SIG)[:4]
        return sel + abi_encode(["address", "uint256", "bytes", "uint8", "bytes32", "bool"],
                                [to_checksum_address(to), value, data, operation,
                                 bytes.fromhex(role_key_bytes32(role_key)[2:]), should_revert])

    def probe(self, rpc, sender: str, role_key: str, to: str, value: int, data: bytes,
              operation: int = 0, block: str | int = "latest") -> ProbeResult:
        cd = self.exec_with_role_calldata(role_key, to, value, data, operation, False)
        try:
            rpc.call_raw(self.address, cd, block=block, sender=sender)
            return ProbeResult(ok=True)
        except CallReverted as e:
            return decode_revert(e.data, str(e))
        except ChainError as e:
            return ProbeResult(ok=False, error=f"rpc: {e}")


def decode_revert(data: bytes, message: str = "") -> ProbeResult:
    if len(data) >= 4:
        name = ERRORS.get(data[:4].hex())
        if name == "ConditionViolation" and len(data) >= 68:
            st, info = abi_decode(["uint8", "bytes32"], data[4:68])
            try:
                sname = Status(st).name
            except ValueError:
                sname = f"Status{st}"
            return ProbeResult(False, "ConditionViolation", sname, "0x" + info.hex())
        if name:
            return ProbeResult(False, name)
        return ProbeResult(False, f"revert 0x{data[:4].hex()}")
    return ProbeResult(False, message or "reverted")


def _exec_options(value: int, operation: int, options: int) -> Status:
    if value > 0 and options not in (ExecutionOptions.SEND, ExecutionOptions.BOTH):
        return Status.SendNotAllowed
    if operation == 1 and options not in (ExecutionOptions.DELEGATE_CALL, ExecutionOptions.BOTH):
        return Status.DelegateCallNotAllowed
    return Status.Ok
