"""MCP server exposing the same functions as the CLI (stub, optional dependency).

    pip install "defios[mcp]"
    DEFIOS_ROOT=/path/to/your-agent-repo python -m defios.mcp

Tools return the same JSON the CLI prints. Nothing here signs or proposes anything
(`defios_plan` may write calls.json when asked to).
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


def tool_plan(steps: list[dict], vault: str | None = None, out: str | None = None) -> dict[str, Any]:
    """steps: [{action: "gearbox_v3.lend", amount: "1", token: "wstETH", params: {}}]. Writes calls.json
    (relative to DEFIOS_ROOT) only when `out` is given and the plan is not refused."""
    import json
    from pathlib import Path

    from .plan import calls_file, load_intent, plan, role_for_plan

    cfg, v, cat, rpc = _ctx(vault)
    st_list, desc = load_intent(json.dumps({"steps": steps}))
    state, adapter, source = role_for_plan(cfg, v, None, rpc)
    res = plan(cat, state, adapter, v, st_list, rpc=rpc, description=desc, role_source=source)
    if out and res["ok"]:
        path = Path(out) if Path(out).is_absolute() else cfg.root / out
        path.write_text(json.dumps(calls_file(res), indent=2) + "\n")
        res["written"] = str(path)
    return res


TOOLS = {
    "defios_doctor": (tool_doctor, "Check Vault / Roles / Operator / proposer wiring."),
    "defios_sync": (tool_sync, "Rebuild the vault book from chain (role.lock.json, book.yaml)."),
    "defios_state": (tool_state, "Sync, then summarize holdings and allowed/missing/unsafe actions."),
    "defios_check": (tool_check, "Mark every card action allowed/missing/unsafe and lint the role."),
    "defios_plan": (tool_plan, "Plan card actions into engine-built, role-checked calls (refuses missing/unsafe)."),
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
