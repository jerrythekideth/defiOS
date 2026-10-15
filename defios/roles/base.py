"""Roles adapter interface.

Every Roles version (v2 today, v3 later) implements `RolesAdapter`. The rest of
DeFi_os (sync, check, doctor, later tx/permit) only talks to this interface and
to `RoleState`, so a v3 modifier can sit next to v2 during a migration.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from .conditions import Clearance, Condition, ExecutionOptions, Status, flat_to_json, options_name


@dataclass
class FunctionState:
    target: str
    selector: str  # 0x-prefixed 4 bytes
    wildcarded: bool
    options: int
    flat: list[dict] = field(default_factory=list)
    condition: Condition | None = None
    changed_at: dict | None = None

    def to_json(self) -> dict:
        d: dict[str, Any] = {
            "wildcarded": self.wildcarded,
            "options": options_name(self.options),
        }
        if not self.wildcarded:
            d["conditions_flat"] = flat_to_json(self.flat)
            d["condition"] = self.condition.to_json() if self.condition else None
        if self.changed_at:
            d["changed_at"] = self.changed_at
        return d


@dataclass
class TargetState:
    address: str
    clearance: int
    options: int
    changed_at: dict | None = None

    def to_json(self) -> dict:
        return {"clearance": Clearance(self.clearance).name, "options": options_name(self.options),
                "changed_at": self.changed_at}


@dataclass
class RoleState:
    version: str
    chain_id: int
    roles_modifier: str
    role_key: str
    block: int
    log_source: str
    members: list[str] = field(default_factory=list)
    targets: dict[str, TargetState] = field(default_factory=dict)  # lowercased address
    functions: dict[tuple[str, str], FunctionState] = field(default_factory=dict)
    allowances: dict[str, dict] = field(default_factory=dict)
    unwrappers: dict[str, str] = field(default_factory=dict)
    default_roles: dict[str, str] = field(default_factory=dict)
    module: dict[str, Any] = field(default_factory=dict)
    roles_seen: list[str] = field(default_factory=list)
    events_replayed: int = 0
    last_change: dict | None = None
    warnings: list[str] = field(default_factory=list)

    # ---- queries ---------------------------------------------------------
    def target(self, address: str) -> TargetState | None:
        return self.targets.get(address.lower())

    def function(self, address: str, sel: str) -> FunctionState | None:
        return self.functions.get((address.lower(), sel.lower()))

    def active_functions(self) -> list[FunctionState]:
        """Functions that are actually reachable (target clearance == Function)."""
        out = []
        for (addr, _), fs in sorted(self.functions.items()):
            t = self.targets.get(addr)
            if t and t.clearance == Clearance.FUNCTION:
                out.append(fs)
        return out

    def dormant_functions(self) -> list[FunctionState]:
        """Stored function scopes whose target is not function-scoped: inert today,
        but they come back to life if someone calls scopeTarget again."""
        out = []
        for (addr, _), fs in sorted(self.functions.items()):
            t = self.targets.get(addr)
            if not t or t.clearance != Clearance.FUNCTION:
                out.append(fs)
        return out

    def to_lock_json(self) -> dict:
        targets: dict[str, Any] = {}
        for addr, t in sorted(self.targets.items()):
            if t.clearance == Clearance.NONE:
                continue
            entry = t.to_json()
            if t.clearance == Clearance.FUNCTION:
                entry["functions"] = {
                    fs.selector: fs.to_json()
                    for (a, _), fs in sorted(self.functions.items()) if a == addr
                }
            targets[addr] = entry
        return {
            "schema": "defios.role.lock/1",
            "note": "Snapshot for diffs and history only. Every defios command re-reads the chain.",
            "roles_version": self.version,
            "chain_id": self.chain_id,
            "roles_modifier": self.roles_modifier,
            "role_key": self.role_key,
            "block": self.block,
            "log_source": self.log_source,
            "events_replayed": self.events_replayed,
            "last_change": self.last_change,
            "module": self.module,
            "members": sorted(self.members),
            "targets": targets,
            "dormant_functions": [
                {"target": f.target, "selector": f.selector, **f.to_json()} for f in self.dormant_functions()
            ],
            "allowances": self.allowances,
            "unwrappers": self.unwrappers,
            "default_roles": self.default_roles,
            "roles_seen": sorted(self.roles_seen),
            "warnings": self.warnings,
        }


@dataclass
class CallVerdict:
    status: Status
    detail: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == Status.Ok


@dataclass
class ProbeResult:
    ok: bool
    error: str = ""
    status: str | None = None
    info: str | None = None


class RolesAdapter(ABC):
    version: str = "?"

    def __init__(self, address: str, chain_id: int = 1):
        self.address = address
        self.chain_id = chain_id

    @classmethod
    @abstractmethod
    def detect(cls, rpc) -> dict:
        """Return {'match': bool, ...evidence} for the modifier at self.address."""

    @abstractmethod
    def read_role(self, role_key: str, log_source, to_block: int | None = None,
                  from_block: int = 0) -> RoleState:
        """Rebuild a role's full state from chain at `to_block` (default latest)."""

    @abstractmethod
    def check_call(self, state: RoleState, to: str, value: int, data: bytes,
                   operation: int = 0, avatar: str | None = None) -> CallVerdict:
        """Offline evaluation of one call against a decoded role."""

    @abstractmethod
    def probe(self, rpc, sender: str, role_key: str, to: str, value: int, data: bytes,
              operation: int = 0, block: str | int = "latest") -> ProbeResult:
        """eth_call execTransactionWithRole (shouldRevert=false) from `sender`."""


ExecutionOptions = ExecutionOptions  # re-export
