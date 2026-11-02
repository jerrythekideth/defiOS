"""Regression eval: the scyUSD request an agent ("Tom") got wrong in the wild (Nov 2026).

    "loop USD3 on Morpho market 0xe3df…, loop savUSD on market 0xe07d…, use siUSD to buy the Pendle PT at
     market 0xd44f…; what permissions payload would you need, given what you already have?"

What went wrong: the market id was fed to a Morpho *vault* action, savUSD was not known to be Avant, Pendle was
not usable. Each assertion below is one of those failure modes, run offline against the real ids / addresses
(chain state frozen in a fake RPC, values read from mainnet on 2026-11-02)."""
import pytest
from eth_abi import encode as abi_encode
from eth_utils import keccak, to_checksum_address

import defios.permit as P
from defios.catalog import Catalog
from defios.chain import ChainError
from defios.config import VaultConfig
from defios.lookups import LookupFailed, resolve as lookup
from defios.recipes import expand
from defios.resolve import resolve
from defios.roles.base import RoleState
from defios.roles.conditions import Operator
from defios.roles.v2 import RolesV2

from .conftest import AVATAR, ROOT

MOD = "0x" + "77" * 20
USD3_MARKET = "0xe3df58f9d3011b7481ff36b939fa5f8da642f34ea5792d25d3958dbf1efa26d7"
SAVUSD_MARKET = "0xe07d416323a1afbfe0bf2fe27ffb549ff565cf5c86d21b79fc60664038e597c9"
PENDLE_MARKET = to_checksum_address("0xd44f2630260ff3805d7cb317d4f6740b0044c408")
IRM = "0x870aC11D48B15DB9a138Cf899d20F13F79Ba00BC"
LLTV = 915000000000000000
USDC = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
MARKETS = {
    USD3_MARKET: (USDC, "0x056B269Eb1f75477a8666ae8C7fE01b64dD55eCc", "0x68b4c2B2b2e245AB54a3bD55DfD5A9d84f029C06", IRM, LLTV),
    SAVUSD_MARKET: (USDC, "0xb8D89678E75a973E74698c976716308abB8a46A4", "0x839940de5043e7c6eDaf063714AFe7F20De5ff12", IRM, LLTV),
}
SY, PT, YT = ("0x9F30507C264Cc6EB5bE35b18ff9AD7B4539Aa920", "0x76543ac61Ba84C6FEeeE76C6a918127Bf0cA5Fd1",
              "0xd42F23Aa6d2b886524006c6AecB1AD80579e736c")
SIUSD = "0xDBDC1Ef57537E34680B898E1FEBD3D68c7389bCB"
TOM = (f"loop USD3 on https://app.morpho.org/ethereum/market/{USD3_MARKET}/usd3-usdc, loop savUSD on market "
       f"{SAVUSD_MARKET}, use siUSD to buy the PT at https://app.pendle.finance/trade/markets/{PENDLE_MARKET.lower()}/swap")


@pytest.fixture(scope="module")
def catalog():
    return Catalog.load(ROOT / "catalog")


class Mainnet:
    """The reads the eval needs, frozen."""

    def __init__(self, catalog):
        self.v6 = catalog.resolve_contract("pendle.market_factory_v6", "x")

    def call(self, to, sig, args, out_types):
        if sig == "idToMarketParams(bytes32)":
            return MARKETS.get("0x" + args[0].hex(), ("0x" + "00" * 20,) * 4 + (0,))
        if sig in ("isMetaMorpho(address)", "isVaultV2(address)"):
            return False
        if sig == "isValidMarket(address)":
            return to == self.v6 and args[0] == PENDLE_MARKET
        if (to, sig) == (PENDLE_MARKET, "readTokens()"):
            return (SY, PT, YT)
        if (to, sig) == (PENDLE_MARKET, "expiry()"):
            return 1799280000
        if (to, sig) == (SY, "yieldToken()"):
            return SIUSD
        if sig == "decimals()":
            return 18
        if sig == "symbol()":
            return {SY: "SY-siUSD", PT: "PT-siUSD-7JAN2027", YT: "YT-siUSD-7JAN2027"}.get(to, "PLP-siUSD-7JAN2027")
        raise ChainError(f"{sig} on {to}")


def test_frozen_market_params_hash_to_the_real_ids():
    types = ["address", "address", "address", "address", "uint256"]
    for mid, mp in MARKETS.items():
        assert "0x" + keccak(abi_encode(types, list(mp))).hex() == mid


def test_resolve_reads_each_link_as_the_right_kind_of_thing(catalog):
    rpc = Mainnet(catalog)
    words = TOM.replace(",", " ").split()
    links = [w for w in words if w.startswith(("http", "0x"))]
    got = {x: resolve(catalog, x, rpc) for x in links}
    usd3, savusd, pendle = got.values()
    assert usd3["matches"][0]["lookup"] == "market" and "threejane.USD3" in usd3["matches"][0]["fields"]["collateralToken"]
    assert savusd["matches"][0]["card"] == "morpho_markets"
    assert "avant.savUSD" in savusd["matches"][0]["fields"]["collateralToken"]         # savUSD is Avant
    assert pendle["matches"][0]["card"] == "pendle" and "PT-siUSD" in pendle["matches"][0]["fields"]["pt"]
    assert all(r["covered"] for r in got.values())
    assert resolve(catalog, "savUSD")["matches"][0]["card"] == "avant"
    assert not resolve(catalog, "scyUSD")["covered"]                                    # not covered: say so


def test_a_market_id_is_never_taken_as_a_vault(catalog):
    with pytest.raises(LookupFailed, match="morpho_markets"):
        lookup(catalog, "morpho_vaults", catalog.cards["morpho_vaults"].actions["deposit_any"],
               {"vault_address": USD3_MARKET}, Mainnet(catalog))


def test_one_permission_batch_for_the_whole_request(catalog):
    rpc = Mainnet(catalog)
    ex = [expand(catalog, s, rpc) for s in (f"morpho_loop@market_id={USD3_MARKET}",
                                            f"morpho_loop@market_id={SAVUSD_MARKET}",
                                            f"pendle_pt@market_address={PENDLE_MARKET},accept_sy={SY}")]
    assert "threejane.deposit" in ex[0]["refs"] and "threejane.redeem" in ex[0]["refs"]
    assert any("avant.savUSD" in n and "not covered" in n for n in ex[1]["notes"])     # no savUSD route: a note
    assert "infinifi.unstake" in ex[2]["refs"]
    refs = list(dict.fromkeys(r for e in ex for r in e["refs"]))
    seen = []
    orig = P.apply_ops
    P.apply_ops = lambda st, ops: seen.append(orig(st, ops)) or seen[-1]
    try:
        r = P.permit(catalog, RoleState("v2", 1, MOD, "R", 0, "t"), RolesV2(MOD), VaultConfig("t", 1, AVATAR, MOD, "R"),
                     refs, rpc=rpc)
    finally:
        P.apply_ops = orig
    assert r["ok"], r.get("problems") or r.get("refused")
    state = seen[0]
    blue = catalog.resolve_contract("morpho_markets.morpho_blue", "x")
    borrow = "0x" + keccak(text="borrow((address,address,address,address,uint256),uint256,uint256,address,address)")[:4].hex()
    cond = state.function(blue, borrow).condition
    markets = cond.children[0]
    assert markets.operator == Operator.OR and len(markets.children) == 2              # both markets, nothing else
    assert state.function(SY, "0x" + keccak(text="deposit(address,address,uint256,uint256)")[:4].hex())
