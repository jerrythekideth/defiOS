"""`defios resolve` link parsing, `@param` refs and recipe errors (offline)."""
import pytest

from defios.catalog import Catalog
from defios.permit import PermitError, split_ref
from defios.recipes import RecipeError, expand
from defios.resolve import parse, resolve

from .conftest import ROOT

ID = "0x" + "e3" * 32
ADDR = "0x" + "d4" * 20


@pytest.fixture(scope="module")
def catalog():
    return Catalog.load(ROOT / "catalog")


@pytest.mark.parametrize("url,hint,value,chain", [
    (f"https://app.morpho.org/ethereum/market/{ID}/usd3-usdc", "morpho market", ID, 1),
    (f"https://app.morpho.org/base/variable/{ID}", "morpho market", ID, 8453),
    (f"https://app.morpho.org/market?id={ID}&network=mainnet", "morpho market", ID, None),
    (f"https://app.morpho.org/ethereum/vault/{ADDR}/kpk-usdc", "morpho vault", ADDR, 1),
    (f"https://app.pendle.finance/trade/markets/{ADDR}/swap?view=pt&chain=ethereum", "pendle market", ADDR, 1),
    (ADDR, None, ADDR, None),
])
def test_parse_links(url, hint, value, chain):
    p = parse(url)
    assert (p["hint"], p["value"], p["chain_id"]) == (hint, value, chain)


def test_link_for_another_chain_is_refused(catalog):
    r = resolve(catalog, f"https://app.morpho.org/base/market/{ID}")
    assert not r["covered"] and "chain 8453" in r["why"]


def test_morpho_vault_link_holding_an_id_is_flagged(catalog):
    r = resolve(catalog, f"https://app.morpho.org/ethereum/vault/{ID}")
    assert any("the link says morpho vault, but the value is an id" in w for w in r["warnings"])


def test_offline_resolve_says_lookups_did_not_run(catalog):
    r = resolve(catalog, ID)
    assert not r["covered"] and any("no RPC_URL" in w for w in r["warnings"])


def test_split_ref():
    assert split_ref("morpho_markets.borrow") == ("morpho_markets.borrow", {})
    assert split_ref(f"morpho_markets.borrow@market_id={ID}") == ("morpho_markets.borrow", {"market_id": ID})
    assert split_ref("x.y@a=1,b=2") == ("x.y", {"a": "1", "b": "2"})
    with pytest.raises(PermitError):
        split_ref("x.y@nonsense")


@pytest.mark.parametrize("spec,match", [("nope@x=1", "unknown recipe"), ("morpho_loop", "needs @market_id"),
                                        ("morpho_loop@market_id=0x12", "morpho_loop")])
def test_recipe_errors(catalog, spec, match):
    with pytest.raises(RecipeError, match=match):
        expand(catalog, spec, None)


def test_cli_resolve_reads_the_agent_repos_env(tmp_path, monkeypatch):
    """Tom's bug: RPC_URL only in the agent repo's .env; resolve ran without it and said NOT COVERED."""
    from click.testing import CliRunner

    import defios.engine
    from defios.cli import main
    seen = {}
    monkeypatch.delenv("RPC_URL", raising=False)
    monkeypatch.delenv("ETH_RPC_URL", raising=False)
    monkeypatch.setattr(defios.engine, "make_rpc", lambda: seen.setdefault("rpc", __import__("os").environ.get("RPC_URL")) and None)
    (tmp_path / ".env").write_text("RPC_URL=http://from-dotenv\n")
    r = CliRunner().invoke(main, ["--root", str(tmp_path), "--format", "text", "resolve", ID])
    assert seen["rpc"] == "http://from-dotenv"
    assert r.exit_code == 2 and "NOT CHECKED" in r.output and "NOT COVERED" not in r.output
