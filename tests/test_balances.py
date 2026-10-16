"""balances.py with a fake RPC: ERC-20 balance/decimals, ERC-4626 convertToAssets, token list from cards."""
from eth_abi import decode, encode
from eth_utils import keccak

from defios.balances import plain_balances, token_list, vault_balances
from defios.catalog import Catalog
from defios.chain import CallReverted
from defios.config import default_catalog_dir, load_config

WSTETH = "0x7f39C581F595B53c5cb19bD0b3f8dA6c935E2Ca0"
POOL = "0xA9d17f6D3285208280a1Fd9B94479c62e0AABa64"


def sel(s):
    return keccak(text=s)[:4]


class FakeRpc:
    def __init__(self, balances):
        self.balances = {k.lower(): v for k, v in balances.items()}

    def get_balance(self, address, block="latest"):
        return 2 * 10**18

    def call_raw(self, to, data, block="latest", sender=None, value=0):
        t = to.lower()
        if data[:4] == sel("balanceOf(address)"):
            return encode(["uint256"], [self.balances.get(t, 0)])
        if data[:4] == sel("decimals()"):
            return encode(["uint256"], [18])
        if data[:4] == sel("symbol()"):
            return encode(["string"], [{WSTETH.lower(): "wstETH", POOL.lower(): "kpkwstETH"}.get(t, "TKN")])
        if data[:4] == sel("convertToAssets(uint256)") and t == POOL.lower():
            (shares,) = decode(["uint256"], data[4:])
            return encode(["uint256"], [shares * 11 // 10])
        if data[:4] == sel("asset()") and t == POOL.lower():
            return encode(["address"], [WSTETH])
        raise CallReverted(b"")


def test_token_list_from_cards_and_config(agent_dir):
    vault = load_config(agent_dir).vault(None)
    vault.tokens = ["weth.WETH", "0x3333333333333333333333333333333333333333"]
    cat = Catalog.load(default_catalog_dir())
    names = {t["name"] for t in token_list(cat, ["lido", "gearbox_v3"], vault)}
    assert {"lido.stETH", "lido.wstETH", "gearbox_v3.kpkwstETH_pool", "weth.WETH"} <= names
    assert "lido.unstETH" not in names                   # erc721 queue, not a token balance
    assert "0x3333333333333333333333333333333333333333" in names


def test_vault_balances_erc20_and_erc4626(agent_dir):
    vault = load_config(agent_dir).vault(None)
    cat = Catalog.load(default_catalog_dir())
    rpc = FakeRpc({WSTETH: 3 * 10**18, POOL: 10**18})
    b = vault_balances(cat, ["lido", "gearbox_v3"], vault, rpc)
    by = {t["name"]: t for t in b["tokens"]}
    assert by["lido.wstETH"]["value"] == 3.0 and by["lido.wstETH"]["symbol"] == "wstETH"
    pool = by["gearbox_v3.kpkwstETH_pool"]
    assert pool["value"] == 1.0 and pool["assets"]["value"] == 1.1 and pool["assets"]["asset_name"] == "lido.wstETH"
    assert b["native"]["value"] == 2.0
    lines = plain_balances(b)
    assert any("kpkwstETH" in line and "1.1" in line for line in lines)
    assert not any("stETH (lido.stETH)" in line for line in lines)   # zero balances are not listed
