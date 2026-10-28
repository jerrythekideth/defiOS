"""defios command line.

Every command prints JSON (for agents) on stdout and a short plain summary (for
humans) on stderr. `--format text` prints only the summary, on stdout.
Every command re-reads the role from chain; nothing trusts a stored lock.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import click

from . import __version__
from .catalog import Catalog, validate_card
from .chain import ChainError
from .config import ConfigError, load_config


def _emit(ctx: click.Context, data: dict, summary: str) -> None:
    fmt = ctx.obj["format"]
    if fmt in ("json", "both"):
        click.echo(json.dumps(data, indent=2, default=str))
    if fmt == "both":
        click.echo(summary, err=True)
    elif fmt == "text":
        click.echo(summary)


def _setup(ctx: click.Context):
    o = ctx.obj
    cfg = load_config(o["root"], o["config"])
    vault = cfg.vault(o["vault"])
    catalog = Catalog.load(cfg.catalog_dir)
    from .engine import make_rpc
    rpc = None if o["offline"] else make_rpc()
    return cfg, vault, catalog, rpc


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(__version__, prog_name="defios")
@click.option("--root", default=".", show_default=True, type=click.Path(file_okay=False),
              help="Your agent repo (holds defios.yaml, .env and books/).")
@click.option("--config", default=None, type=click.Path(dir_okay=False), help="Path to defios.yaml.")
@click.option("--vault", default=None, help="Vault name from defios.yaml (needed when several are configured).")
@click.option("--format", "fmt", type=click.Choice(["both", "json", "text"]), default="both", show_default=True,
              help="both = JSON on stdout + summary on stderr.")
@click.option("--logs-file", default=None, type=click.Path(dir_okay=False),
              help="Replay Roles events from a JSON file instead of Etherscan/RPC (offline; testing).")
@click.option("--offline", is_flag=True, help="Do not use RPC_URL (no reads, no probes).")
@click.pass_context
def main(ctx, root, config, vault, fmt, logs_file, offline):
    """DeFi_os: operate a Safe through a Zodiac Roles modifier, safely.

    \b
    defios doctor   check Vault / Roles / Operator / proposer wiring
    defios sync     rebuild books/<vault>/ (role.lock.json, book.yaml) from chain
    defios state    sync, then say what the vault holds and can do
    defios check    allowed / missing / unsafe per card action, plus role lints
    defios plan     intent -> engine-built, role-checked calls -> calls.json
    defios permit   card actions -> Roles permission batch for the Vault (Safe Tx Builder JSON)
    defios simulate run a permission batch and/or calls.json on a local anvil fork
    defios propose  dry-run (default) or --send an Operator Safe tx for humans to sign
    """
    ctx.ensure_object(dict)
    ctx.obj.update(root=root, config=config, vault=vault, format=fmt, logs_file=logs_file, offline=offline)


def _run(fn):
    try:
        fn()
    except (ConfigError, ChainError, FileNotFoundError) as e:
        click.echo(json.dumps({"error": str(e)}), err=False)
        click.echo(f"error: {e}", err=True)
        sys.exit(2)


def _auto_simulate(ctx, vault, catalog, res: dict, *, batch: dict | None = None, calls: list | None = None,
                   mode: str = "roles", skip: bool = False) -> None:
    """Run the result on an anvil fork when RPC_URL and anvil are available; a revert turns ok off."""
    import shutil

    from .chain import env
    if skip:
        res.setdefault("warnings", []).append("not simulated (--no-simulate)")
        return
    url = env("RPC_URL")
    if ctx.obj["offline"] or not url or not shutil.which("anvil"):
        why = "offline" if ctx.obj["offline"] else ("RPC_URL not set" if not url else "anvil not installed")
        res.setdefault("warnings", []).append(f"not simulated ({why}); run `defios simulate` before anyone signs")
        return
    from .simulate import Fork, SimulateError, simulate, summarize
    from .tx import Call
    try:
        with Fork(url) as fork:
            sim = simulate(vault, fork, batch=batch, calls=[Call.from_json(c) for c in calls] if calls else None,
                           catalog=catalog, batch_mode=mode, multisend=getattr(vault, "multisend", None))
    except SimulateError as e:
        res.setdefault("warnings", []).append(f"simulation could not run: {e}")
        return
    res["simulation"] = sim
    res["simulation_summary"] = summarize(sim)
    if not sim["ok"]:
        res["ok"] = False


@main.command()
@click.pass_context
def doctor(ctx):
    """Check the wiring: modules, Roles version/owner/avatar/target, membership, delegates, keys."""
    def go():
        from .doctor import doctor as run, summarize
        cfg, vault, _catalog, rpc = _setup(ctx)
        res = run(vault, rpc, ctx.obj["logs_file"])
        _emit(ctx, res, summarize(res))
    _run(go)


@main.command()
@click.option("--no-probe", is_flag=True, help="Skip eth_call probes even when RPC_URL is set.")
@click.option("--no-queues", is_flag=True, help="Skip Safe Transaction Service queue reads.")
@click.pass_context
def sync(ctx, no_probe, no_queues):
    """Rebuild the vault book from chain at the latest block."""
    def go():
        from .sync import summarize, sync as run
        cfg, vault, catalog, rpc = _setup(ctx)
        res = run(cfg, vault, catalog, rpc, logs_file=ctx.obj["logs_file"], probe=not no_probe,
                  with_queues=not no_queues)
        _emit(ctx, {"book": res["book"], "paths": res["paths"]}, summarize(res))
    _run(go)


@main.command()
@click.option("--no-sync", is_flag=True, help="Read the existing book without re-syncing (not recommended).")
@click.pass_context
def state(ctx, no_sync):
    """What the vault holds and can do, in plain words (syncs first)."""
    def go():
        from .state import load_book, plain_state
        cfg, vault, catalog, rpc = _setup(ctx)
        if no_sync:
            book = load_book(cfg.books_dir / vault.name / "book.yaml")
        else:
            from .sync import sync as run
            book = run(cfg, vault, catalog, rpc, logs_file=ctx.obj["logs_file"])["book"]
        _emit(ctx, book, plain_state(book))
    _run(go)


@main.command()
@click.option("--protocol", "protocols", multiple=True, help="Limit to these card ids (default: detected from the role).")
@click.option("--no-probe", is_flag=True, help="Offline evaluation only, even when RPC_URL is set.")
@click.pass_context
def check(ctx, protocols, no_probe):
    """Mark every card action allowed / missing / unsafe against the live role and lint the role."""
    def go():
        from .check import run_check, summarize
        from .engine import read_role
        cfg, vault, catalog, rpc = _setup(ctx)
        st, adapter, _ = read_role(vault, logs_file=ctx.obj["logs_file"], rpc=rpc)
        res = run_check(catalog, st, adapter, vault, list(protocols) or None, None if no_probe else rpc)
        _emit(ctx, res, summarize(res))
    _run(go)


@main.command("catalog")
@click.option("--dir", "directory", default=None, type=click.Path(file_okay=False), help="Catalog directory.")
@click.pass_context
def catalog_cmd(ctx, directory):
    """List protocol cards and validate them (no defios.yaml needed)."""
    from .config import default_catalog_dir
    cat = Catalog.load(directory or default_catalog_dir())
    data, lines = {}, []
    for proto, card in cat.cards.items():
        errs = validate_card(cat, card)
        data[proto] = {"name": card.raw.get("name"), "status": card.raw.get("status"),
                       "reviewed": card.raw.get("reviewed"), "contracts": list(card.contracts),
                       "actions": list(card.actions), "errors": errs}
        lines.append(f"{proto:14} {card.raw.get('status')}/{'reviewed' if card.raw.get('reviewed') else 'unreviewed'}"
                     f"  {len(card.actions)} actions{'  ERRORS: ' + str(len(errs)) if errs else ''}")
    _emit(ctx, data, "\n".join(lines))


def _registry(directory):
    from .config import default_catalog_dir
    from .registry import Registry, RegistryError, load_results
    d = Path(directory) if directory else default_catalog_dir()
    reg = Registry.from_catalog(Catalog.load(d))
    if reg.errors:
        raise RegistryError("registry errors:\n  " + "\n  ".join(reg.errors))
    return d, reg, load_results(d)


@main.command()
@click.argument("address")
@click.option("--chain", "chain_id", type=int, default=None, help="Chain id (default: every chain).")
@click.option("--dir", "directory", default=None, type=click.Path(file_okay=False), help="Catalog directory.")
@click.pass_context
def whois(ctx, address, chain_id, directory):
    """What is ADDRESS? Protocol, contract, category, trust status, proxy, edges, card actions.

    \b
    Offline: reads the catalog and catalog/registry.proofs.json (no defios.yaml needed).
    Only `verified` records may be acted on; `unknown` means do not touch it.
    """
    from .registry import RegistryError, summarize_whois
    try:
        _d, reg, results = _registry(directory)
        res = reg.whois(address, chain_id, results)
    except RegistryError as e:
        click.echo(json.dumps({"error": str(e)}))
        click.echo(f"error: {e}", err=True)
        sys.exit(2)
    _emit(ctx, res, summarize_whois(res))


@main.group()
def registry():
    """The protocol registry: verify proofs, export index.json (see catalog/REGISTRY.md)."""


@registry.command("verify")
@click.option("--chain", "chains", type=int, multiple=True, help="Only these chain ids (default: every chain "
              "with an RPC: RPC_URL for 1, RPC_URL_<chain id> for the others).")
@click.option("--write/--no-write", default=True, show_default=True,
              help="Update catalog/registry.proofs.json, catalog/index.json and catalog/ACTIONS.md.")
@click.option("--dir", "directory", default=None, type=click.Path(file_okay=False), help="Catalog directory.")
@click.pass_context
def registry_verify(ctx, chains, write, directory):
    """Re-run every onchain proof: code present, EIP-1967 slots, each card `proofs:` read.

    Exit 3 when any record is in conflict (a failed proof, no code, two protocols claim it).
    """
    def go():
        from .chain import Rpc, env
        from .registry import (ACTIONS_FILE, INDEX_FILE, PROOFS_FILE, TOKENS_FILE, RegistryError, dump,
                               merge_results, summarize_verify, verify)
        try:
            d, reg, old = _registry(directory)
        except RegistryError as e:
            raise ConfigError(str(e)) from e
        rpcs = {}
        for cid in sorted({r.chain_id for r in reg.records}):
            url = env("RPC_URL") if cid == 1 else env(f"RPC_URL_{cid}")
            if url and (not chains or cid in chains):
                rpcs[cid] = Rpc(url)
        if not rpcs:
            raise ConfigError("no RPC for any registry chain: set RPC_URL (chain 1) or RPC_URL_<chain id>")
        results = merge_results(old, verify(reg, rpcs, list(chains) or None))
        if write:
            (d / PROOFS_FILE).write_text(dump(results))
            (d / INDEX_FILE).write_text(dump(reg.export_index(results)))
            (d / ACTIONS_FILE).write_text(reg.actions_md())
            (d / TOKENS_FILE).write_text(reg.tokens_md(results))
        checked = [str(c) for c in rpcs]
        res = {"checked": checked, "statuses": {r.ref: reg.status(r, results).status for r in reg.records},
               "written": [str(d / PROOFS_FILE), str(d / INDEX_FILE)] if write else []}
        _emit(ctx, res, summarize_verify(reg, results, checked))
        if "conflict" in res["statuses"].values():
            sys.exit(3)
    _run(go)


@registry.command("export")
@click.option("--out", default=None, help="Where to write (default catalog/index.json; '-' = stdout).")
@click.option("--dir", "directory", default=None, type=click.Path(file_okay=False), help="Catalog directory.")
def registry_export(out, directory):
    """Write the static index.json (deployments, status, edges), ACTIONS.md and TOKENS.md. Offline."""
    from .registry import ACTIONS_FILE, INDEX_FILE, TOKENS_FILE, RegistryError, dump
    try:
        d, reg, results = _registry(directory)
    except RegistryError as e:
        click.echo(f"error: {e}", err=True)
        sys.exit(2)
    text = dump(reg.export_index(results))
    if out == "-":
        click.echo(text, nl=False)
        return
    path = Path(out) if out else d / INDEX_FILE
    path.write_text(text)
    click.echo(f"wrote {path}", err=True)
    if not out:
        (d / ACTIONS_FILE).write_text(reg.actions_md())
        (d / TOKENS_FILE).write_text(reg.tokens_md(results))
        click.echo(f"wrote {d / ACTIONS_FILE} and {d / TOKENS_FILE}", err=True)


@main.command()
@click.option("--file", "calls_file", required=True, type=click.Path(exists=True, dir_okay=False),
              help="calls.json: inner calls {to, value, data, operation} produced by the engine/cards.")
@click.option("--send", is_flag=True, help="Actually POST to the Safe Transaction Service (default: dry run).")
@click.option("--nonce", type=int, default=None,
              help="Explicit Operator nonce (only to deliberately replace a queued tx). Default: max(on-chain, queued+1).")
@click.option("--batch", type=click.Choice(["roles", "operator"]), default="roles", show_default=True,
              help="Several calls: MultiSend inside execTransactionWithRole (needs the role's MultiSend unwrapper) "
                   "or one execTransactionWithRole per call batched by the Operator.")
@click.pass_context
def propose(ctx, calls_file, send, nonce, batch):
    """Build, hash and sign (as a delegate) an Operator Safe tx; with --send, queue it for humans to sign.

    \b
    Dry run by default: prints the decoded calls, nonce, SafeTxHash and signer.
    Never signs as an owner, never confirms, never executes.
    """
    def go():
        from .propose import ProposeError, propose as run, summarize
        from .tx import TxError, load_calls_file
        cfg, vault, catalog, rpc = _setup(ctx)
        try:
            calls, meta = load_calls_file(calls_file)
            if meta.get("chain_id") and int(meta["chain_id"]) != vault.chain_id:
                raise TxError(f"calls.json is for chain {meta['chain_id']}, vault {vault.name} is on {vault.chain_id}")
            res = run(vault, calls, send=send, nonce=nonce, batch=batch, catalog=catalog, rpc=rpc,
                      books_dir=cfg.books_dir, description=meta.get("description"))
        except (ProposeError, TxError) as e:
            raise ConfigError(str(e)) from e
        _emit(ctx, res, summarize(res))
    _run(go)


@main.command()
@click.argument("steps", nargs=-1)
@click.option("--intent", default=None, help="JSON intent (inline or a file): {description, steps: [{action, amount, "
              "token, params}]}. Instead of / in addition to STEPS.")
@click.option("--amount", default=None, help="Human amount for steps without `=amount` (in the action's input token).")
@click.option("--token", default=None, help="Token for steps without one (name, alias, card.contract or ETH); must "
              "match the action's input token.")
@click.option("--param", "params", multiple=True, help="Extra card variable, name=value (number or card contract).")
@click.option("--out", default="calls.json", show_default=True,
              help="Where to write calls.json for `defios propose --file` (relative to --root); '-' = do not write.")
@click.option("--batch", type=click.Choice(["roles", "operator"]), default="roles", show_default=True,
              help="Batch mode for the Operator tx preview (same meaning as in propose).")
@click.option("--no-probe", is_flag=True, help="Offline evaluation only, even when RPC_URL is set.")
@click.option("--keep-approvals", is_flag=True,
              help="Do not append approve(spender, 0) after the steps (by default no allowance stays open).")
@click.option("--no-simulate", is_flag=True, help="Skip the anvil fork run (done by default when RPC_URL and anvil "
              "are available).")
@click.pass_context
def plan(ctx, steps, intent, amount, token, params, out, batch, no_probe, keep_approvals, no_simulate):
    """Turn an intent into engine-built calls, check them against the role, write calls.json.

    \b
    STEPS are <card>.<action>[=<amount>] or <alias>.<action>[=<amount>], run in order:
      defios plan gearbox_v3.lend --amount 1 --token wstETH
      defios plan weth.unwrap=1 lido.stake=1 lido.wrap=0.999 kpkwstETH.lend=0.8
    Refuses (exit 3, nothing written) if any call is missing / unsafe in the role.
    Every approval the plan makes is set back to 0 at the end of the same transaction
    (unless the card says the spender pulls later, or --keep-approvals).
    There is no separate `defios tx`: plan writes calls.json and prints the Operator Safe tx preview;
    `defios propose --file calls.json` picks the nonce, hashes and signs.
    """
    def go():
        from .catalog import CardError
        from .plan import PlanError, calls_file, load_intent, parse_step, plan as run, role_for_plan, summarize
        cfg, vault, catalog, rpc = _setup(ctx)
        try:
            all_steps, desc = [], None
            if intent:
                all_steps, desc = load_intent(intent)
            all_steps += [parse_step(s) for s in steps]
            extra = {}
            for kv in params:
                k, eq, v = kv.partition("=")
                if not eq or not k.strip():
                    raise PlanError(f"--param {kv!r}: expected name=value")
                extra[k.strip()] = v.strip()
            for st in all_steps:
                if st.amount is None and amount is not None:
                    st.amount = amount
                if st.token is None and token is not None:
                    st.token = token
                st.params = {**extra, **st.params}
            state, adapter, source = role_for_plan(cfg, vault, ctx.obj["logs_file"], rpc)
            res = run(catalog, state, adapter, vault, all_steps, rpc=rpc, probe=not no_probe, batch=batch,
                      description=desc, role_source=source, keep_approvals=keep_approvals)
        except (PlanError, CardError) as e:
            raise ConfigError(str(e)) from e
        if res["ok"]:
            _auto_simulate(ctx, vault, catalog, res, calls=res["calls"], mode=batch, skip=no_simulate)
        if res["ok"] and out != "-":
            path = Path(out)
            if not path.is_absolute():
                path = cfg.root / path
            path.write_text(json.dumps(calls_file(res), indent=2) + "\n")
            res["written"] = str(path)
        _emit(ctx, res, summarize(res) + ("\n" + res["simulation_summary"] if res.get("simulation_summary") else ""))
        if not res["ok"]:
            sys.exit(3)
    _run(go)


@main.command()
@click.argument("actions", nargs=-1, required=True)
@click.option("--token", "tokens", multiple=True, help="Limit actions with a `tokens:` list to these tokens (repeatable). "
              "Default: every token the card lists.")
@click.option("--with-optional", is_flag=True, help="Also permit the actions' optional / alternative calls.")
@click.option("--param", "params", multiple=True, help="Lookup id, name=value (e.g. market_id=0x… for a Morpho "
              "market): the looked-up value is pinned in the role.")
@click.option("--replace", is_flag=True, help="Overwrite stored scopes that differ from the card (or are looser "
              "than it) instead of refusing. Whatever stops working is listed.")
@click.option("--description", default=None, help="Batch description shown in the Safe Transaction Builder.")
@click.option("--out", default=None, help="Where to write the Safe Tx Builder JSON (relative to --root). Default "
              "permissions_<vault>_<YYYYMMDD>.json; '-' = do not write.")
@click.option("--no-simulate", is_flag=True, help="Skip the anvil fork run (done by default when RPC_URL and anvil "
              "are available).")
@click.pass_context
def permit(ctx, actions, tokens, with_optional, params, replace, description, out, no_simulate):
    """Build the Roles permission batch that lets the Operator run card ACTIONS.

    \b
    ACTIONS are <card>.<action> or <alias>.<action>:
      defios permit aave_v3.supply aave_v3.withdraw --token USDC --token USDT
      defios permit morpho_markets.supply morpho_markets.withdraw --param market_id=0x…
    Conditions come from the card; the batch is merged with the live role (existing
    approve spenders are kept) and checked on a copy of it: every call allowed, hostile
    variants rejected, nothing that works today broken, no new high lint.
    Refuses (exit 3, nothing written) otherwise. The output is for the Vault queue
    (the Roles modifier's owner), not the Operator: humans import, review and sign it.
    """
    def go():
        from datetime import date

        from .catalog import CardError
        from .permit import PermitError, permit as run, summarize
        from .plan import PlanError, role_for_plan
        cfg, vault, catalog, rpc = _setup(ctx)
        try:
            extra = {}
            for kv in params:
                k, eq, v = kv.partition("=")
                if not eq or not k.strip():
                    raise PermitError(f"--param {kv!r}: expected name=value")
                extra[k.strip()] = v.strip()
            state, adapter, source = role_for_plan(cfg, vault, ctx.obj["logs_file"], rpc)
            res = run(catalog, state, adapter, vault, list(actions), tokens=list(tokens) or None,
                      with_optional=with_optional, replace=replace, description=description, role_source=source,
                      params=extra, rpc=rpc)
        except (PermitError, PlanError, CardError) as e:
            raise ConfigError(str(e)) from e
        if res["ok"] and res.get("batch"):
            _auto_simulate(ctx, vault, catalog, res, batch=res["batch"], skip=no_simulate)
        if res["ok"] and res.get("batch") and out != "-":
            path = Path(out or f"permissions_{vault.name}_{date.today():%Y%m%d}.json")
            if not path.is_absolute():
                path = cfg.root / path
            path.write_text(json.dumps(res["batch"], indent=2) + "\n")
            res["written"] = str(path)
        _emit(ctx, res, summarize(res) + ("\n" + res["simulation_summary"] if res.get("simulation_summary") else "")
              + (f"\n  wrote {res['written']}" if res.get("written") else ""))
        if not res["ok"]:
            sys.exit(3)
    _run(go)


@main.command()
@click.option("--calls", "calls_file", default=None, type=click.Path(exists=True, dir_okay=False),
              help="calls.json from `defios plan`: run as the Operator would.")
@click.option("--batch", "batch_file", default=None, type=click.Path(exists=True, dir_okay=False),
              help="Safe Tx Builder JSON of Roles changes (e.g. from `defios permit`): run as the Roles owner would, "
                   "before --calls.")
@click.option("--fork-url", default=None, help="RPC to fork (default RPC_URL).")
@click.option("--block", type=int, default=None, help="Fork at this block (default latest).")
@click.option("--anvil", "anvil_path", default=None, help="Path to anvil (default: on PATH).")
@click.option("--batch-mode", type=click.Choice(["roles", "operator"]), default="roles", show_default=True,
              help="How several calls are batched (same meaning as in propose).")
@click.option("--no-fork", is_flag=True, hidden=True, help="Plain local anvil (tests).")
@click.pass_context
def simulate(ctx, calls_file, batch_file, fork_url, block, anvil_path, batch_mode, no_fork):
    """Run a permission batch and/or a plan on a local anvil fork; report reverts, Vault deltas, open allowances.

    \b
      defios simulate --calls calls.json
      defios simulate --batch permissions_<vault>_<date>.json --calls calls.json
    Nothing leaves the fork. Exit 3 if any transaction reverts.
    """
    def go():
        from .chain import env
        from .simulate import Fork, SimulateError, load_batch, simulate as run, summarize
        from .tx import TxError, load_calls_file
        cfg, vault, catalog, _rpc = _setup(ctx)
        try:
            url = None if no_fork else (fork_url or env("RPC_URL"))
            if not no_fork and not url:
                raise SimulateError("no RPC to fork: set RPC_URL or pass --fork-url")
            calls = load_calls_file(calls_file)[0] if calls_file else None
            batch = load_batch(batch_file) if batch_file else None
            with Fork(url, block, anvil_path, chain_id=vault.chain_id if no_fork else None) as fork:
                res = run(vault, fork, batch=batch, calls=calls, catalog=catalog, batch_mode=batch_mode,
                          multisend=getattr(vault, "multisend", None))
        except (SimulateError, TxError) as e:
            raise ConfigError(str(e)) from e
        _emit(ctx, res, summarize(res))
        if not res["ok"]:
            sys.exit(3)
    _run(go)


@main.command()
@click.argument("batch", type=click.Path(exists=True, dir_okay=False))
@click.pass_context
def decode(ctx, batch):
    """Decode a Safe Tx Builder JSON of Roles permission changes (review a batch someone hands you)."""
    from .decode import decode_batch, summarize
    res = decode_batch(json.loads(Path(batch).read_text()))
    _emit(ctx, res, summarize(res))


if __name__ == "__main__":
    main()
