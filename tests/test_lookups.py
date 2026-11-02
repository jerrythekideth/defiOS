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


# --------------------------------------------------------------------------- any Morpho vault by address
VAULT_A, VAULT_B = to_checksum_address("0x" + "a1" * 20), to_checksum_address("0x" + "b2" * 20)
DEPOSIT = "0x6e553f65"


class VaultRpc:
    """Factories vouch for `vaults` ({address: asset}); `factory` says which factory knows them."""

    def __init__(self, catalog, vaults, factory="morpho_vaults.vault_v2_factory"):
        self.vaults, self.factory = vaults, catalog.resolve_contract(factory, "x")

    def call(self, to, sig, args, out_types):
        if sig in ("isMetaMorpho(address)", "isVaultV2(address)"):
            return to == self.factory and args[0] in self.vaults
        assert sig == "asset()"
        return self.vaults[to]


def test_any_vault_resolves(catalog):
    usdc = catalog.resolve_contract("stablecoins.USDC", "x")
    v = resolve(catalog, "morpho_vaults", catalog.cards["morpho_vaults"].actions["deposit_any"],
                {"vault_address": VAULT_A.lower()}, VaultRpc(catalog, {VAULT_A: usdc}))
    assert v["any_vault"] == VAULT_A and v["any_vault.asset"] == usdc
    assert v["_lookup_checks"]["any_vault"] == "vault_v2_factory.isVaultV2"


@pytest.mark.parametrize("case", ["not_a_vault", "unknown_asset", "bad_address"])
def test_any_vault_refuses(catalog, case):
    usdc = catalog.resolve_contract("stablecoins.USDC", "x")
    vaults, addr = {VAULT_A: usdc}, VAULT_A
    if case == "not_a_vault":
        addr = VAULT_B
    elif case == "unknown_asset":
        vaults = {VAULT_A: to_checksum_address("0x" + "cd" * 20)}
    else:
        addr = "0x1234"
    with pytest.raises(LookupFailed):
        resolve(catalog, "morpho_vaults", catalog.cards["morpho_vaults"].actions["deposit_any"],
                {"vault_address": addr}, VaultRpc(catalog, vaults))


def test_permit_any_vault_pins_target_and_spender(catalog):
    usdc = catalog.resolve_contract("stablecoins.USDC", "x")
    rpc = VaultRpc(catalog, {VAULT_A: usdc, VAULT_B: usdc}, "morpho_vaults.metamorpho_factory")
    state = RoleState("v2", 1, MOD, "R", 0, "t")
    for v in (VAULT_A, VAULT_B):
        seen = []
        orig = P.apply_ops
        P.apply_ops = lambda st, ops: seen.append(orig(st, ops)) or seen[-1]
        try:
            r = P.permit(catalog, state, RolesV2(MOD), VaultConfig("t", 1, AVATAR, MOD, "R"),
                         ["morpho_vaults.deposit_any"], params={"vault_address": v}, rpc=rpc)
        finally:
            P.apply_ops = orig
        assert r["ok"], r
        state = seen[0]
    assert state.function(VAULT_A, DEPOSIT) and state.function(VAULT_B, DEPOSIT)
    spender = state.function(usdc, "0x095ea7b3").condition.children[0]
    assert spender.operator == Operator.OR and len(spender.children) == 2


def test_any_vault_target_without_param(catalog):
    from defios.catalog import CallBuilder
    call = catalog.cards["morpho_vaults"].actions["deposit_any"]["calls"][1]
    bc = CallBuilder(catalog, AVATAR).build("morpho_vaults", "deposit_any", 1, call, {})
    assert bc.error and "vault_address" in bc.error


def test_recognize_for_propose(catalog):
    from defios.lookups import recognize
    rpc = VaultRpc(catalog, {VAULT_A: catalog.resolve_contract("stablecoins.USDC", "x")})
    assert recognize(catalog, VAULT_A, rpc) == "morpho_vaults any_vault (vault_v2_factory.isVaultV2)"
    assert recognize(catalog, VAULT_B, rpc) is None
    assert recognize(catalog, VAULT_A, None) is None
