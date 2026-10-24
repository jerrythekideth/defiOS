import json
import shutil

import pytest
from click.testing import CliRunner
from eth_abi import encode
from eth_utils import keccak

from defios.catalog import Catalog
from defios.chain import CallReverted
from defios.cli import main
from defios.registry import CATEGORIES, INDEX_FILE, Registry, dump, load_results, verify

from .conftest import ROOT

CAT = ROOT / "catalog"
AAVE_POOL = "0x87870Bca3F3fD6335C3F4ce8392D69350B4fA4E2"
SUSDE = "0x9D39A5DE30e57443BfF2A8307A4256c8797A3497"
USDE = "0x4c9EDD5852cd905f086C759E8383e09bff1E68B3"


def run(args):
    return CliRunner().invoke(main, args, catch_exceptions=False)


@pytest.fixture(scope="module")
def reg():
    return Registry.from_catalog(Catalog.load(CAT))


def test_every_addressed_contract_is_a_record_with_a_category(reg):
    assert reg.errors == []
    assert len(reg.records) >= 80
    assert all(r.category in CATEGORIES for r in reg.records)
    assert len(reg.by_key) == len(reg.records)  # no address claimed twice


def test_index_json_is_up_to_date(reg):
    """catalog/index.json must be regenerated (defios registry export) whenever a card changes."""
    assert (CAT / INDEX_FILE).read_text() == dump(reg.export_index(load_results(CAT)))


def test_edges_use_design_relations(reg):
    edges = {(e["from_ref"], e["relation"], e["to_ref"]) for e in reg.edges()}
    assert ("ethena.USDe", "underlying_of", "ethena.sUSDe") in edges
    assert ("etherfi.liquid_eth", "receipt_of", "etherfi.liquid_eth_teller") in edges
    assert ("liminal.spoke", "bridges_to", "liminal.keeper") in edges


def test_status_rules(reg):
    results = load_results(CAT)
    pool = reg.by_ref["aave_v3.pool"]
    st = reg.status(pool, results)
    assert st.status == "verified" and "independent sources" in st.why[0]
    # every record's implicit source is the card itself
    assert reg.by_ref["lido.stETH"].sources[0][0] == "card"
    # one source and no proof result: unverified
    assert reg.status(reg.by_ref["ethena.sUSDe"], {"_meta": {}}).status == "unverified"


class FakeRpc:
    """Answers code / storage / the proofs' eth_calls from a dict."""

    def __init__(self, chain_id, answers, no_code=()):
        self.cid, self.answers, self.no_code = chain_id, answers, {a.lower() for a in no_code}

    def chain_id(self):
        return self.cid

    def block_number(self):
        return 123

    def get_code(self, address, block="latest"):
        return b"" if address.lower() in self.no_code else b"\x60\x80"

    def get_storage_at(self, address, slot, block="latest"):
        return b"\0" * 32

    def call_raw(self, to, data, block="latest", sender=None, value=0):
        key = (to.lower(), data[:4].hex())
        if key not in self.answers:
            raise CallReverted(b"")
        return self.answers[key]


def _addr(a):
    return encode(["address"], [a])


def test_verify_marks_verified_and_conflict(reg):
    asset_sel = keccak(text="asset()")[:4].hex()
    reserves = {(AAVE_POOL.lower(), keccak(text="getReservesList()")[:4].hex()): encode(["address[]"], [[USDE, SUSDE]])}
    good = verify(reg, {1: FakeRpc(1, {(SUSDE.lower(), asset_sel): _addr(USDE), **reserves})}, [1])
    assert good["_meta"]["1"]["block"] == 123
    susde = reg.by_ref["ethena.sUSDe"]
    assert reg.status(susde, good).status == "verified"
    bad = verify(reg, {1: FakeRpc(1, {(SUSDE.lower(), asset_sel): _addr(AAVE_POOL), **reserves})}, [1])
    st = reg.status(susde, bad)
    assert st.status == "conflict" and "proof failed" in st.why[0]
    gone = verify(reg, {1: FakeRpc(1, {}, no_code=[USDE])}, [1])
    assert reg.status(reg.by_ref["ethena.USDe"], gone).status == "conflict"


def test_verify_refuses_wrong_chain(reg):
    from defios.chain import ChainError
    with pytest.raises(ChainError):
        verify(reg, {1: FakeRpc(999, {})})


def _tmp_catalog(tmp_path, cards):
    d = tmp_path / "catalog"
    for c in cards:
        shutil.copytree(CAT / c, d / c)
    return d


def test_two_protocols_claiming_one_address_is_a_conflict(tmp_path):
    d = _tmp_catalog(tmp_path, ["weth", "lido"])
    card = d / "lido" / "card.yaml"
    card.write_text(card.read_text().replace("0xae7ab96520DE3A18E5e111B5EaAb095312D7fE84",
                                             "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"))
    reg = Registry.from_catalog(Catalog.load(d))
    st = reg.status(reg.by_ref["weth.WETH"], {"_meta": {}})
    assert st.status == "conflict" and "lido.stETH" in st.why[0]


def test_bad_category_source_and_status_are_errors(tmp_path):
    d = _tmp_catalog(tmp_path, ["weth"])
    card = d / "weth" / "card.yaml"
    card.write_text(card.read_text().replace("category: token", "category: thing\n    status: verified\n"
                                             "    sources: [\"rumor: someone said so\"]"))
    errs = " ".join(Registry.from_catalog(Catalog.load(d)).errors)
    assert "category 'thing'" in errs and "rumor" in errs and "`status`" in errs


def test_whois_cli_known_and_unknown():
    r = run(["--format", "json", "whois", AAVE_POOL.lower()])
    assert r.exit_code == 0
    data = json.loads(r.output)
    m = data["matches"][0]
    assert (m["protocol"], m["name"], m["status"]) == ("aave_v3", "pool", "verified")
    assert "aave_v3.supply" in m["actions"]
    assert data["verdict"] == "verified"
    r = run(["--format", "text", "whois", "0x000000000000000000000000000000000000dEaD"])
    assert "unknown" in r.output and "Do not act" in r.output


def test_eoa_records_expect_no_code(reg):
    keeper = reg.by_ref["liminal.keeper"]
    assert keeper.eoa and keeper.category == "keeper"
    assert reg.status(keeper, {keeper.key: {"code": False}}).status != "conflict"
    st = reg.status(keeper, {keeper.key: {"code": True}})
    assert st.status == "conflict" and "marked eoa" in st.why[0]


def test_registry_export_stdout():
    r = run(["registry", "export", "--out", "-"])
    data = json.loads(r.output)
    assert data["schema"] == "defios.registry/1"
    assert "1:" + AAVE_POOL.lower() in data["deployments"]
