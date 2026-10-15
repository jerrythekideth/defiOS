"""Roles v3 adapter: placeholder.

Roles v3 (scoped typed-message signing, which fixes RFQ protocols like Avant)
is not released yet. When it is, implement this class against its events and
error types; nothing else in DeFi_os should need to change. Migration plan:
deploy v3 next to v2, re-grant the role there from role.lock.json with
`defios permit`, move the Operator over, then disable v2.
"""
from __future__ import annotations

from .base import RolesAdapter


class RolesV3(RolesAdapter):
    version = "v3"

    @classmethod
    def detect(cls, rpc, address: str | None = None) -> dict:  # type: ignore[override]
        return {"match": False, "note": "Roles v3 detection not implemented (v3 unreleased)"}

    def read_role(self, role_key, log_source, to_block=None, from_block=0):
        raise NotImplementedError("Roles v3 adapter not implemented yet")

    def check_call(self, state, to, value, data, operation=0, avatar=None):
        raise NotImplementedError("Roles v3 adapter not implemented yet")

    def probe(self, rpc, sender, role_key, to, value, data, operation=0, block="latest"):
        raise NotImplementedError("Roles v3 adapter not implemented yet")
