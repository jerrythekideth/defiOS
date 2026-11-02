"""pendle.any_* : any Pendle market by address, proved by a catalogued market factory, SY / PT / YT read from
the market, decimals read onchain; permit pins every derived address; propose re-proves them."""
import pytest
from eth_utils import keccak, to_checksum_address

import defios.permit as P
from defios.catalog import Catalog
from defios.chain import ChainError
from defios.config import VaultConfig
from defios.lookups import LookupFailed, reprove, resolve
from defios.plan import Step, calls_file, plan
from defios.roles.base import RoleState
from defios.roles.v2 import RolesV2

from .conftest import AVATAR, ROOT

MOD = "0x" + "77" * 20
MARKET = to_checksum_address("0x" + "d4" * 20)
SY, PT, YT = (to_checksum_address("0x" + b * 20) for b in ("51", "52", "53"))
DEPOSIT = "0x" + keccak(text="deposit(address,address,uint256,uint256)")[:4].hex()
SWAP_SY_PT = "0x" + keccak(text="swapExactSyForPtSimple(address,address,uint256,uint256)")[:4].hex()


@pytest.fixture(scope="module")
def catalog():
    return Catalog.load(ROOT / "catalog")


class PendleRpc:
    """market_factory_v6 vouches for MARKET; MARKET -> (SY, PT, YT); SY.yieldToken = `yield_token`."""

    def __init__(self, catalog, yield_token="infinifi.siUSD", valid=True):
        self.factory = catalog.resolve_contract("pendle.market_factory_v6", "x")
        self.yt = catalog.resolve_contract(yield_token, "x") if "." in yield_token else yield_token
        self.valid = valid

    def call(self, to, sig, args, out_types):
        if sig == "isValidMarket(address)":
            return self.valid and to == self.factory and args[0] == MARKET
        if sig == "readTokens()" and to == MARKET:
            return (SY, PT, YT)
        if sig == "expiry()" and to == MARKET:
            return 1799280000
        if sig == "yieldToken()" and to == SY:
            return self.yt
        if sig == "decimals()":
            return 18
        if sig == "symbol()":
            return {SY: "SY-siUSD", PT: "PT-siUSD-7JAN2027", YT: "YT-siUSD-7JAN2027", MARKET: "PLP-siUSD"}.get(to, "X")
        raise ChainError(f"unexpected {sig} on {to}")

    def request(self, method, params):
        raise ChainError("no eth_call in this fake")

    def call_raw(self, *a, **k):
        raise ChainError("no eth_call in this fake")


def test_any_market_resolves_tokens_and_pins_them(catalog):
    v = resolve(catalog, "pendle", catalog.cards["pendle"].actions["any_buy_pt"],
                {"market_address": MARKET.lower()}, PendleRpc(catalog))
    assert v["any_market"] == MARKET and v["any_market.sy"] == SY and v["any_market.pt"] == PT
    assert {"any_market", "any_market.sy", "any_market.pt", "any_market.yt"} <= v["_pinned"]
    assert v["_lookup_tokens"][PT.lower()]["symbol"] == "PT-siUSD-7JAN2027"
    assert v["_lookup_checks"]["any_market"] == "market_factory_v6.isValidMarket"
    assert any("not in any card" in w for w in v["_lookup_warnings"])  # the SY's code is not fixed by the factory


@pytest.mark.parametrize("case", ["not_a_market", "unverified_yield_token", "market_id_instead"])
def test_any_market_refuses(catalog, case):
    params, rpc = {"market_address": MARKET}, PendleRpc(catalog)
    if case == "not_a_market":
        rpc = PendleRpc(catalog, valid=False)
    if case == "unverified_yield_token":
        rpc = PendleRpc(catalog, yield_token="0x" + "99" * 20)
    if case == "market_id_instead":
        params = {"market_address": "0x" + "e3" * 32}
    with pytest.raises(LookupFailed, match={"not_a_market": "not recognised", "unverified_yield_token": "verified",
                                            "market_id_instead": "morpho_markets"}[case]):
        resolve(catalog, "pendle", catalog.cards["pendle"].actions["any_mint_sy"], params, rpc)


def _permit(catalog, state, refs, rpc):
    seen = []
    orig = P.apply_ops
    P.apply_ops = lambda st, ops: seen.append(orig(st, ops)) or seen[-1]
    try:
        r = P.permit(catalog, state, RolesV2(MOD), VaultConfig("t", 1, AVATAR, MOD, "R"), refs,
                     params={"market_address": MARKET}, rpc=rpc)
    finally:
        P.apply_ops = orig
    assert r["ok"], r
    return r, seen[0]


def test_permit_then_plan_siusd_into_pt(catalog):
    """Tom's third leg: siUSD -> SY -> PT on one market, permitted from the card, then planned."""
    rpc = PendleRpc(catalog)
    r, state = _permit(catalog, RoleState("v2", 1, MOD, "R", 0, "t"), ["pendle.any_mint_sy", "pendle.any_buy_pt"], rpc)
    router = catalog.resolve_contract("pendle.router", "x")
    assert state.function(SY, DEPOSIT) is not None and state.function(router, SWAP_SY_PT) is not None
    vault = VaultConfig("t", 1, AVATAR, MOD, "R")
    p = plan(catalog, state, RolesV2(MOD), vault,
             [Step("pendle.any_mint_sy", "100", None, {"market_address": MARKET, "min_sy": "1"}),
              Step("pendle.any_buy_pt", "99", None, {"market_address": MARKET, "min_pt": "1"})],
             rpc=rpc)
    assert p["ok"], p["refused"]
    assert p["lookups"] == [{"card": "pendle", "lookup": "any_market", "param": "market_address", "value": MARKET}]
    assert calls_file(p)["lookups"] == p["lookups"]
    assert any(c["to"] == SY for c in p["calls"])
    assert "SY-siUSD" in " ".join(c["line"] for s in p["steps"] for c in s["calls"])
    # a hostile market (any other address) is not allowed by that role
    other = plan(catalog, RoleState("v2", 1, MOD, "R", 0, "t"), RolesV2(MOD), vault,
                 [Step("pendle.any_buy_pt", "1", None, {"market_address": MARKET, "min_pt": "1"})], rpc=rpc)
    assert not other["ok"]


def test_propose_accepts_derived_targets_only_with_the_recorded_lookup(catalog):
    from defios.propose import propose
    from defios.tx import Call
    rpc = PendleRpc(catalog)
    assert reprove(catalog, [{"card": "pendle", "lookup": "any_market", "value": MARKET}], rpc)[SY.lower()].endswith("sy")
    vault = VaultConfig("t", 1, AVATAR, MOD, "R", operator="0x" + "0e" * 20)
    calls = [Call(SY, 0, "0x")]
    bare = propose(vault, calls, catalog=catalog, rpc=rpc, nonce=1, private_key="")
    assert any("uncatalogued" in x for x in bare["problems"])
    ok = propose(vault, calls, catalog=catalog, rpc=rpc, nonce=1, private_key="",
                 lookups=[{"card": "pendle", "lookup": "any_market", "param": "market_address", "value": MARKET}])
    assert not any("uncatalogued" in x for x in ok["problems"])
    assert any("proves it onchain" in w for w in ok["warnings"])
