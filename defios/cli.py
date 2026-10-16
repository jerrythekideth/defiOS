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
@click.argument("batch", type=click.Path(exists=True, dir_okay=False))
@click.pass_context
def decode(ctx, batch):
    """Decode a Safe Tx Builder JSON of Roles permission changes (review a batch someone hands you)."""
    from .decode import decode_batch, summarize
    res = decode_batch(json.loads(Path(batch).read_text()))
    _emit(ctx, res, summarize(res))


if __name__ == "__main__":
    main()
