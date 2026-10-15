"""Roles adapters. `get_adapter` picks by version string; `detect_version` asks
each adapter (newest first) whether it recognises the modifier."""
from __future__ import annotations

from .base import RoleState, RolesAdapter
from .v2 import RolesV2
from .v3 import RolesV3

ADAPTERS: dict[str, type[RolesAdapter]] = {"v2": RolesV2, "v3": RolesV3}


def get_adapter(version: str, address: str, chain_id: int = 1) -> RolesAdapter:
    try:
        cls = ADAPTERS[version]
    except KeyError:
        raise ValueError(f"unknown Roles version {version!r}; known: {sorted(ADAPTERS)}") from None
    return cls(address, chain_id)


def detect_version(rpc, address: str) -> dict:
    evidence = {}
    for v in ("v3", "v2"):
        ev = ADAPTERS[v].detect(rpc, address)
        evidence[v] = ev
        if ev.get("match"):
            return {"version": v, "evidence": evidence}
    return {"version": None, "evidence": evidence}


__all__ = ["ADAPTERS", "RoleState", "RolesAdapter", "RolesV2", "RolesV3", "detect_version", "get_adapter"]
