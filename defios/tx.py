"""Transaction building for the Operator queue (M2).

    inner call(s)  ->  [MultiSendCallOnly batch]  ->  Roles.execTransactionWithRole(...)  ->  Safe tx on the Operator

* One inner call is passed to the Roles modifier as is.
* Several inner calls are packed into one `multiSend(bytes)` on Safe's
  MultiSendCallOnly 1.4.1 and executed by the Vault with DELEGATECALL
  (`batch="roles"`, the default). The Roles modifier only accepts that if the
  role has the MultiSend unwrapper configured for this address + selector
  (`setTransactionUnwrapper(MultiSendCallOnly, 0x8d80ff0a, MultiSendUnwrapper)`);
  every inner call is then checked against the role on its own.
* `batch="operator"` instead batches on the Operator side: the Operator Safe
  delegatecalls MultiSendCallOnly, which calls `execTransactionWithRole` once per
  inner call. No unwrapper needed; each call is a separate Roles check.

Nothing here signs, sends or reads the chain. Addresses come from the caller
(defios.yaml / cards); calldata comes from the engine (see AGENTS.md rule 2).
"""
from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from eth_abi import encode as abi_encode
from eth_utils import is_address, keccak, to_checksum_address

from .roles.v2 import role_key_bytes32

ZERO = "0x0000000000000000000000000000000000000000"
CALL, DELEGATECALL = 0, 1

EXEC_WITH_ROLE_SIG = "execTransactionWithRole(address,uint256,bytes,uint8,bytes32,bool)"
EXEC_WITH_ROLE_SELECTOR = "0xc6fe8747"
MULTISEND_SIG = "multiSend(bytes)"
MULTISEND_SELECTOR = "0x8d80ff0a"

# Safe MultiSendCallOnly v1.4.1 (canonical deployment). Mainnet address checked against
# gnosisguild/zodiac-modifier-roles (packages/app/components/ApplyUpdate/const.ts MULTISEND_CALLONLY_141
# and docs/general/unwrapping.mdx). TODO: cross-check other chains against safe-global/safe-deployments
# (not available offline); until then they must be set in defios.yaml (`multisend:`).
MULTISEND_CALL_ONLY_141 = {1: "0x9641d764fc13c8B624c04430C7356C1C7C8102e2"}
# Zodiac MultiSendUnwrapper adapter (same source); only used for messages / doctor-style checks.
MULTISEND_UNWRAPPER = "0xB4Cd4bb764C089f20DA18700CE8bc5e49F369efD"


class TxError(ValueError):
    pass


def _bytes(data: Any) -> bytes:
    if isinstance(data, (bytes, bytearray)):
        return bytes(data)
    if data in (None, "", "0x"):
        return b""
    s = str(data)
    if not s.startswith("0x"):
        raise TxError(f"data must be 0x-prefixed hex, got {s[:12]!r}")
    try:
        return bytes.fromhex(s[2:])
    except ValueError as e:
        raise TxError(f"data is not valid hex: {e}") from None


@dataclass
class Call:
    """One call the Vault (avatar) makes through the Roles modifier."""
    to: str
    value: int = 0
    data: bytes = b""
    operation: int = CALL
    label: str | None = None          # e.g. "gearbox_v3.lend[1] deposit" (from the engine)

    def __post_init__(self):
        if not is_address(self.to):
            raise TxError(f"to: {self.to!r} is not an address")
        self.to = to_checksum_address(self.to)
        self.value = int(self.value)
        if self.value < 0:
            raise TxError("value must be >= 0")
        self.data = _bytes(self.data)
        self.operation = int(self.operation)
        if self.operation not in (CALL, DELEGATECALL):
            raise TxError(f"operation must be 0 (call) or 1 (delegatecall), got {self.operation}")

    @classmethod
    def from_json(cls, d: dict) -> Call:
        unknown = set(d) - {"to", "value", "data", "operation", "label"}
        if unknown:
            raise TxError(f"unknown call fields: {sorted(unknown)}")
        op = d.get("operation", 0)
        if isinstance(op, str):
            op = {"call": 0, "delegatecall": 1}.get(op.lower(), op)
        return cls(to=d["to"], value=int(d.get("value", 0) or 0), data=d.get("data") or b"",
                   operation=int(op), label=d.get("label"))

    def to_json(self) -> dict:
        out = {"to": self.to, "value": str(self.value), "data": "0x" + self.data.hex(), "operation": self.operation}
        if self.label:
            out["label"] = self.label
        return out

    @property
    def selector(self) -> str | None:
        return "0x" + self.data[:4].hex() if len(self.data) >= 4 else None


@dataclass
class SafeTx:
    """A Safe transaction (fields of the EIP-712 SafeTx struct)."""
    to: str
    value: int
    data: bytes
    operation: int
    nonce: int | None = None
    safe_tx_gas: int = 0
    base_gas: int = 0
    gas_price: int = 0
    gas_token: str = ZERO
    refund_receiver: str = ZERO

    def to_json(self) -> dict:
        d = asdict(self)
        d["data"] = "0x" + self.data.hex()
        return d


# ------------------------------------------------------------------------------ encoders
def encode_multisend_payload(calls: Iterable[Call]) -> bytes:
    """Packed transactions for MultiSend: operation (1) | to (20) | value (32) | data length (32) | data."""
    out = b""
    for c in calls:
        out += (c.operation.to_bytes(1, "big") + bytes.fromhex(c.to[2:]) + c.value.to_bytes(32, "big")
                + len(c.data).to_bytes(32, "big") + c.data)
    return out


def encode_multisend(calls: Iterable[Call]) -> bytes:
    """`multiSend(bytes transactions)` calldata."""
    return keccak(text=MULTISEND_SIG)[:4] + abi_encode(["bytes"], [encode_multisend_payload(calls)])


def decode_multisend(data: bytes) -> list[Call]:
    """Inverse of encode_multisend (for summaries and tests)."""
    from eth_abi import decode as abi_decode
    if data[:4] != keccak(text=MULTISEND_SIG)[:4]:
        raise TxError("not a multiSend(bytes) call")
    (payload,) = abi_decode(["bytes"], data[4:])
    calls, i = [], 0
    while i < len(payload):
        op = payload[i]
        to = "0x" + payload[i + 1:i + 21].hex()
        value = int.from_bytes(payload[i + 21:i + 53], "big")
        ln = int.from_bytes(payload[i + 53:i + 85], "big")
        calls.append(Call(to=to, value=value, data=payload[i + 85:i + 85 + ln], operation=op))
        i += 85 + ln
    return calls


def exec_with_role(call: Call, role_key: str, should_revert: bool = True) -> bytes:
    """`execTransactionWithRole(to, value, data, operation, roleKey, shouldRevert)` calldata (0xc6fe8747).
    shouldRevert = true: the Operator's Safe tx fails if the Roles check or the inner call fails."""
    sel = keccak(text=EXEC_WITH_ROLE_SIG)[:4]
    return sel + abi_encode(["address", "uint256", "bytes", "uint8", "bytes32", "bool"],
                            [call.to, call.value, call.data, call.operation,
                             bytes.fromhex(role_key_bytes32(role_key)[2:]), should_revert])


def multisend_address(chain_id: int, override: str | None = None) -> str:
    if override:
        if not is_address(override):
            raise TxError(f"multisend: {override!r} is not an address")
        return to_checksum_address(override)
    try:
        return MULTISEND_CALL_ONLY_141[chain_id]
    except KeyError:
        raise TxError(f"no MultiSendCallOnly 1.4.1 address known for chain {chain_id}; "
                      f"set `multisend:` for the vault in defios.yaml") from None


def build_inner(calls: list[Call], chain_id: int = 1, multisend: str | None = None) -> Call:
    """What the Vault executes: the call itself, or a MultiSendCallOnly batch (delegatecall)."""
    if not calls:
        raise TxError("no calls")
    if len(calls) == 1:
        return calls[0]
    bad = [i for i, c in enumerate(calls) if c.operation != CALL]
    if bad:
        raise TxError(f"calls {bad} are delegatecalls; MultiSendCallOnly reverts on them. Propose them on their "
                      f"own, or use batch='operator'")
    return Call(to=multisend_address(chain_id, multisend), value=0, data=encode_multisend(calls),
                operation=DELEGATECALL, label=f"multiSend({len(calls)} calls)")


@dataclass
class OperatorTx:
    """The Safe tx for the Operator plus what it is made of (for the human summary)."""
    safe_tx: SafeTx
    calls: list[Call]
    batch: str
    inner: list[Call]            # what reaches execTransactionWithRole (1 call, or 1 per call for batch=operator)
    roles_modifier: str
    role_key: str
    warnings: list[str] = field(default_factory=list)


def build_operator_tx(calls: list[Call], roles_modifier: str, role_key: str, chain_id: int = 1,
                      batch: str = "roles", multisend: str | None = None) -> OperatorTx:
    roles_modifier = to_checksum_address(roles_modifier)
    warnings: list[str] = []
    if batch not in ("roles", "operator"):
        raise TxError("batch must be 'roles' or 'operator'")
    if any(c.operation == DELEGATECALL for c in calls):
        warnings.append("an inner call is a DELEGATECALL from the Vault: only expected for CoW's order signer")
    if len(calls) == 1 or batch == "roles":
        inner = build_inner(calls, chain_id, multisend)
        data = exec_with_role(inner, role_key, True)
        tx = SafeTx(to=roles_modifier, value=0, data=data, operation=CALL)
        if len(calls) > 1:
            warnings.append(f"batched through MultiSendCallOnly {inner.to}: the role needs the MultiSend unwrapper "
                            f"(setTransactionUnwrapper({inner.to}, {MULTISEND_SELECTOR}, adapter)) or the whole "
                            f"batch is rejected; check `unwrappers` in role.lock.json, or use --batch operator")
        return OperatorTx(tx, calls, "single" if len(calls) == 1 else "roles", [inner], roles_modifier,
                          role_key, warnings)
    wrapped = [Call(to=roles_modifier, value=0, data=exec_with_role(c, role_key, True), operation=CALL,
                    label=f"execTransactionWithRole({c.label or c.to})") for c in calls]
    if any(c.value for c in calls):
        warnings.append("inner calls send ETH from the Vault; the Operator-side batch itself sends none")
    ms = multisend_address(chain_id, multisend)
    tx = SafeTx(to=ms, value=0, data=encode_multisend(wrapped), operation=DELEGATECALL)
    return OperatorTx(tx, calls, "operator", list(calls), roles_modifier, role_key, warnings)


# ------------------------------------------------------------------------------ calls.json
def load_calls_file(path: str | Path) -> tuple[list[Call], dict]:
    """calls.json: {"description": "...", "chain_id": 1, "vault": "name", "calls": [{to, value, data, operation}]}.
    Produced by `defios plan`, not written by hand. A bare list of calls is accepted too."""
    raw = json.loads(Path(path).read_text())
    meta: dict = {}
    if isinstance(raw, dict):
        meta = {k: v for k, v in raw.items() if k != "calls"}
        raw = raw.get("calls")
    if not isinstance(raw, list) or not raw:
        raise TxError(f"{path}: expected a non-empty `calls` list")
    return [Call.from_json(c) for c in raw], meta
