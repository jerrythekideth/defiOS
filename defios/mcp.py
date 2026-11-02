"""MCP server exposing the same functions as the CLI (stub, optional dependency).

    pip install "defios[mcp]"
    DEFIOS_ROOT=/path/to/your-agent-repo python -m defios.mcp

Tools return the same JSON the CLI prints. Nothing here signs as an owner or posts anything:
`defios_propose` is a dry run only (queueing stays `defios propose --send`, after the human's yes).
`defios_plan` writes calls.json and `defios_permit` writes the Tx Builder JSON only when given `out`.
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


def tool_whois(address: str, chain_id: int | None = None) -> dict[str, Any]:
    """Needs no defios.yaml: reads the catalog and its proof results only."""
    from .catalog import Catalog
    from .config import default_catalog_dir
    from .registry import Registry, load_results

    d = default_catalog_dir()
    return Registry.from_catalog(Catalog.load(d)).whois(address, chain_id, load_results(d))


def _write(cfg, out: str, data: dict) -> str:
    import json
    from pathlib import Path

    path = Path(out) if Path(out).is_absolute() else cfg.root / out
    path.write_text(json.dumps(data, indent=2) + "\n")
    return str(path)


def tool_permit(actions: list[str] | None = None, recipes: list[str] | None = None, tokens: list[str] | None = None,
                params: dict | None = None, with_optional: bool = False, vault: str | None = None,
                out: str | None = None) -> dict[str, Any]:
    """actions: ["aave_v3.supply", "morpho_markets.borrow@market_id=0x…"]; recipes: ["morpho_loop@market_id=0x…"].
    Builds the Roles batch for the Vault queue from the cards, merged with the live role. Writes the Safe Tx
    Builder JSON (relative to DEFIOS_ROOT) only when `out` is given and the batch is not refused. Never applied."""
    from .permit import permit
    from .plan import role_for_plan
    from .recipes import expand

    cfg, v, cat, rpc = _ctx(vault)
    refs, notes = list(actions or []), []
    for spec in recipes or []:
        ex = expand(cat, spec, rpc, v.book_vars)
        refs += [x for x in ex["refs"] if x not in refs]
        notes += [f"recipe {ex['recipe']}: {n}" for n in ex["notes"]]
    if not refs:
        return {"ok": False, "error": "nothing to permit: give actions or recipes"}
    state, adapter, source = role_for_plan(cfg, v, None, rpc)
    res = permit(cat, state, adapter, v, refs, tokens=tokens, with_optional=with_optional, role_source=source,
                 params=params, rpc=rpc)
    res["notes"] = notes + list(res.get("notes") or [])
    if out and res["ok"] and res.get("batch"):
        res["written"] = _write(cfg, out, res["batch"])
    return res


def tool_simulate(calls_file: str | None = None, batch_file: str | None = None, block: int | None = None,
                  vault: str | None = None) -> dict[str, Any]:
    """Run a permission batch and/or calls.json (paths relative to DEFIOS_ROOT) on a local anvil fork of RPC_URL.
    Nothing is signed or broadcast."""
    from pathlib import Path

    from .chain import env
    from .simulate import Fork, load_batch, simulate, summarize
    from .tx import load_calls_file

    cfg, v, cat, _ = _ctx(vault)
    p = lambda f: Path(f) if Path(f).is_absolute() else cfg.root / f  # noqa: E731
    if not env("RPC_URL"):
        return {"ok": False, "error": "no RPC to fork: set RPC_URL"}
    calls = load_calls_file(p(calls_file))[0] if calls_file else None
    batch = load_batch(p(batch_file)) if batch_file else None
    with Fork(env("RPC_URL"), block) as fork:
        res = simulate(v, fork, batch=batch, calls=calls, catalog=cat, multisend=getattr(v, "multisend", None))
    res["summary"] = summarize(res)
    return res


def tool_propose(calls_file: str = "calls.json", vault: str | None = None) -> dict[str, Any]:
    """Dry run of the Operator proposal for calls.json: nonce, SafeTxHash, decoded calls, warnings, problems.
    Never posts: queueing is `defios propose --send`, run after the human says yes."""
    from pathlib import Path

    from .propose import propose
    from .tx import load_calls_file

    cfg, v, cat, rpc = _ctx(vault)
    path = Path(calls_file) if Path(calls_file).is_absolute() else cfg.root / calls_file
    calls, meta = load_calls_file(path)
    if meta.get("chain_id") and int(meta["chain_id"]) != v.chain_id:
        return {"ok": False, "error": f"calls.json is for chain {meta['chain_id']}, vault is on {v.chain_id}"}
    return propose(v, calls, send=False, catalog=cat, rpc=rpc, books_dir=cfg.books_dir,
                   description=meta.get("description"), lookups=meta.get("lookups"))


def _catalog_rpc():
    from .catalog import Catalog
    from .config import default_catalog_dir
    from .engine import make_rpc

    return Catalog.load(default_catalog_dir()), make_rpc()


def tool_resolve(thing: str) -> dict[str, Any]:
    """An app URL, an address, a 32-byte id or a symbol -> what the catalog knows and which card actions take it
    (`--param`s included). `covered: false`: tell the human it is not covered; do not guess."""
    from .resolve import resolve

    cat, rpc = _catalog_rpc()
    return resolve(cat, thing, rpc)


def tool_recipe(spec: str | None = None) -> dict[str, Any]:
    """No spec: list the recipes. 'morpho_loop@market_id=0x…' / 'pendle_pt@market_address=0x…': the card actions
    the recipe expands to, plus notes on what no card covers. Feed `refs` (or the spec) to defios_permit."""
    from .recipes import RECIPES, expand

    if not spec:
        return {k: {"param": r["param"], "summary": r["summary"]} for k, r in RECIPES.items()}
    cat, rpc = _catalog_rpc()
    return expand(cat, spec, rpc)


def tool_actions(query: str) -> dict[str, Any]:
    """Search catalog/ACTIONS.md (card, action, input, tokens, --params, what it does) for every word in `query`."""
    from .config import default_catalog_dir

    words = query.lower().split()
    card, hits = None, []
    for line in (default_catalog_dir() / "ACTIONS.md").read_text().splitlines():
        if line.startswith("## "):
            card = line[3:]
        elif line.startswith("| `") and card and all(w in f"{card} {line}".lower() for w in words):
            cells = [c.strip().strip("`") for c in line.strip("|").split("|")]
            hits.append({"card": card.split(":")[0], "action": cells[0], "input": cells[1], "tokens": cells[2],
                         "params": cells[3], "does": cells[4]})
    return {"query": query, "matches": hits[:50], "count": len(hits)}


TOOLS = {
    "defios_whois": (tool_whois, "What is this address? Protocol, contract, trust status (act only on verified)."),
    "defios_doctor": (tool_doctor, "Check Vault / Roles / Operator / proposer wiring."),
    "defios_sync": (tool_sync, "Rebuild the vault book from chain (role.lock.json, book.yaml)."),
    "defios_state": (tool_state, "Sync, then summarize holdings and allowed/missing/unsafe actions."),
    "defios_check": (tool_check, "Mark every card action allowed/missing/unsafe and lint the role."),
    "defios_plan": (tool_plan, "Plan card actions into engine-built, role-checked calls (refuses missing/unsafe)."),
    "defios_permit": (tool_permit, "Card actions / recipes -> Roles permission batch (Tx Builder JSON) for the Vault "
                                   "queue, merged with the live role. Never hand-diff permissions: use this."),
    "defios_simulate": (tool_simulate, "Run a permission batch and/or calls.json on an anvil fork: reverts, deltas, "
                                       "open allowances."),
    "defios_propose": (tool_propose, "Dry run of the Operator proposal for calls.json (never posts)."),
    "defios_resolve": (tool_resolve, "What is this URL / address / id / symbol, and which card action + --param "
                                     "takes it? Use before mapping any link to an action."),
    "defios_recipe": (tool_recipe, "Expand a strategy (morpho_loop, pendle_pt) into card actions + notes."),
    "defios_actions": (tool_actions, "Search the action catalog (catalog/ACTIONS.md)."),
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
