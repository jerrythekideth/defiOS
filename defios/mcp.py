"""MCP server exposing the same functions as the CLI (stub, optional dependency).

    pip install "defios[mcp]"
    DEFIOS_ROOT=/path/to/your-agent-repo python -m defios.mcp

Tools return the same JSON the CLI prints. Read-only in M1: nothing here signs
or proposes anything.
"""
from __future__ import annotations

import os
from typing import Any


def _ctx(vault: str | None):
    from .catalog import Catalog
    from .config import load_config
    from .engine import make_rpc

    cfg = load_config(os.environ.get("DEFIOS_ROOT", "."))
    v = cfg.vault(vault)
    return cfg, v, Catalog.load(cfg.catalog_dir), make_rpc()


def tool_doctor(vault: str | None = None) -> dict[str, Any]:
    from .doctor import doctor

    _, v, _, rpc = _ctx(vault)
    return doctor(v, rpc)


def tool_sync(vault: str | None = None) -> dict[str, Any]:
    from .sync import sync

    cfg, v, cat, rpc = _ctx(vault)
    res = sync(cfg, v, cat, rpc)
    return {"book": res["book"], "paths": res["paths"]}


def tool_state(vault: str | None = None) -> dict[str, Any]:
    from .state import plain_state
    from .sync import sync

    cfg, v, cat, rpc = _ctx(vault)
    book = sync(cfg, v, cat, rpc)["book"]
    return {"summary": plain_state(book), "book": book}


def tool_check(vault: str | None = None, protocols: list[str] | None = None) -> dict[str, Any]:
    from .check import run_check
    from .engine import read_role

    _, v, cat, rpc = _ctx(vault)
    st, adapter, _ = read_role(v, rpc=rpc)
    return run_check(cat, st, adapter, v, protocols, rpc)


TOOLS = {
    "defios_doctor": (tool_doctor, "Check Vault / Roles / Operator / proposer wiring."),
    "defios_sync": (tool_sync, "Rebuild the vault book from chain (role.lock.json, book.yaml)."),
    "defios_state": (tool_state, "Sync, then summarize holdings and allowed/missing/unsafe actions."),
    "defios_check": (tool_check, "Mark every card action allowed/missing/unsafe and lint the role."),
}


def serve() -> None:
    try:
        from mcp.server.fastmcp import FastMCP  # type: ignore
    except ImportError as e:  # pragma: no cover
        raise SystemExit("MCP support needs the optional dependency: pip install 'defios[mcp]'") from e
    app = FastMCP("defios")
    for name, (fn, desc) in TOOLS.items():
        app.tool(name=name, description=desc)(fn)
    app.run()


if __name__ == "__main__":  # pragma: no cover
    serve()
