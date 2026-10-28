"""Card `lookups:` (Morpho market ids): resolve onchain, refuse bad ids / unverified tokens, pin and merge in the role."""
import pytest
from eth_abi import encode as abi_encode
from eth_utils import keccak, to_checksum_address

import defios.permit as P
from defios.catalog import Catalog
from defios.config import VaultConfig
from defios.lookups import LookupFailed, resolve
from defios.roles.base import RoleState
from defios.roles.conditions import AbiType, Operator
from defios.roles.v2 import RolesV2

from .conftest import AVATAR, ROOT

MOD = "0x" + "77" * 20
TYPES = ["address", "address", "address", "address", "uint256"]
ORACLE, IRM = to_checksum_address("0x" + "aa" * 20), to_checksum_address("0x" + "bb" * 20)
SUPPLY = "0xa99aad89"


@pytest.fixture(scope="module")
def catalog():
    return Catalog.load(ROOT / "catalog")


def _market(catalog, loan, col, lltv=860000000000000000):
    vals = [catalog.resolve_contract(loan, "x"), catalog.resolve_contract(col, "x") if "." in col else col,
            ORACLE, IRM, lltv]
    return "0x" + keccak(abi_encode(TYPES, vals)).hex(), tuple(vals)


class FakeRpc:
    def __init__(self, markets):
        self.markets = markets

    def call(self, to, sig, args, out_types):
        assert sig == "idToMarketParams(bytes32)"
        return self.markets.get("0x" + args[0].hex(), ("0x" + "00" * 20,) * 4 + (0,))


def _action(catalog, name):
    return catalog.cards["morpho_markets"].actions[name]


def test_resolve_pins_market_and_token(catalog):
    mid, mp = _market(catalog, "stablecoins.USDC", "rocket_pool.rETH")
    v = resolve(catalog, "morpho_markets", _action(catalog, "supply"), {"market_id": mid}, FakeRpc({mid: mp}))
    assert v["market"] == mp and v["market.loanToken"] == mp[0] and v["_pinned"] == {"market"}
    assert v["_lookup_ids"]["market"] == mid


@pytest.mark.parametrize("case", ["no_param", "no_rpc", "zeros", "bad_hash", "unknown_token"])
def test_resolve_refuses(catalog, case):
    mid, mp = _market(catalog, "stablecoins.USDC", "rocket_pool.rETH")
    params, rpc = {"market_id": mid}, FakeRpc({mid: mp})
    if case == "no_param":
        params = {}
    elif case == "no_rpc":
        rpc = None
    elif case == "zeros":
        rpc = FakeRpc({})
    elif case == "bad_hash":  # the node answers params that do not hash to the id
        rpc = FakeRpc({mid: mp[:4] + (mp[4] + 1,)})
    elif case == "unknown_token":
        mid, mp = _market(catalog, "stablecoins.USDC", "0x" + "cd" * 20)
        params, rpc = {"market_id": mid}, FakeRpc({mid: mp})
    with pytest.raises(LookupFailed):
        resolve(catalog, "morpho_markets", _action(catalog, "supply"), params, rpc)


def _permit(catalog, state, mid, rpc):
    seen = []
    orig = P.apply_ops
    P.apply_ops = lambda st, ops: seen.append(orig(st, ops)) or seen[-1]
    try:
        r = P.permit(catalog, state, RolesV2(MOD), VaultConfig("t", 1, AVATAR, MOD, "R"), ["morpho_markets.supply"],
                     params={"market_id": mid}, rpc=rpc)
    finally:
        P.apply_ops = orig
    return r, seen[0]


def test_permit_whitelists_markets_one_by_one(catalog):
    a, ma = _market(catalog, "stablecoins.USDC", "rocket_pool.rETH")
    b, mb = _market(catalog, "stablecoins.USDC", "lido.wstETH")
    rpc = FakeRpc({a: ma, b: mb})
    state = RoleState("v2", 1, MOD, "R", 0, "t")
    r, state = _permit(catalog, state, a, rpc)
    assert r["ok"], r
    mp = state.function(catalog.resolve_contract("morpho_vaults.morpho_blue", "x"), SUPPLY).condition.children[0]
    assert (mp.param_type, mp.operator) == (AbiType.TUPLE, Operator.MATCHES)
    assert all(k.operator == Operator.EQUAL_TO for k in mp.children)
    r, state = _permit(catalog, state, b, rpc)
    assert r["ok"] and any("added 1 tuple" in o["why"] for o in r["ops"]), r
    mp = state.function(catalog.resolve_contract("morpho_vaults.morpho_blue", "x"), SUPPLY).condition.children[0]
    assert mp.operator == Operator.OR and len(mp.children) == 2


def test_permit_without_market_id_refused(catalog):
    with pytest.raises(P.PermitError, match="market_id"):
        P.permit(catalog, RoleState("v2", 1, MOD, "R", 0, "t"), RolesV2(MOD), VaultConfig("t", 1, AVATAR, MOD, "R"),
                 ["morpho_markets.supply"], rpc=FakeRpc({}))


def test_list_params_and_bytes32_proofs(catalog):
    from defios.catalog import _fixed_bytes
    from defios.plan import _param_value
    proof = ["0x" + "ab" * 32, "0x" + "01" * 32]
    v = _param_value(catalog, "merkl", proof, {})
    assert _fixed_bytes(v, "bytes32[]") == [bytes.fromhex("ab" * 32), bytes.fromhex("01" * 32)]
    assert _fixed_bytes([v], "bytes32[][]")[0][1] == bytes.fromhex("01" * 32)
    with pytest.raises(Exception):
        _param_value(catalog, "merkl", ["0x" + "12" * 20], {})  # an uncatalogued address
