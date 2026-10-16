"""Vault balances over plain eth_call: ERC-20 balanceOf / decimals / symbol, ERC-4626 convertToAssets.

Which tokens: every ERC-20 / ERC-4626 contract of the cards the vault uses
(`kind: erc20*` / `erc4626*`), plus `tokens:` from defios.yaml (card refs such
as `weth.WETH`, or addresses), plus native ETH. Nothing is hard-coded here.
"""
from __future__ import annotations

from typing import Any

from eth_abi import decode as abi_decode
from eth_abi import encode as abi_encode
from eth_utils import is_address, keccak, to_checksum_address

from .catalog import CardError, Catalog
from .chain import ChainError, Rpc
from .config import VaultConfig


def _sel(sig: str) -> bytes:
    return keccak(text=sig)[:4]


def _call_uint(rpc: Rpc, to: str, sig: str, types: list[str] | None = None, args: list | None = None,
               block: int | str = "latest") -> int:
    data = _sel(sig) + (abi_encode(types, args) if types else b"")
    raw = rpc.call_raw(to, data, block=block)
    if len(raw) < 32:
        raise ChainError(f"{sig} on {to}: empty return (not a token?)")
    return abi_decode(["uint256"], raw[:32])[0]


def erc20_balance(rpc: Rpc, token: str, holder: str, block: int | str = "latest") -> int:
    return _call_uint(rpc, token, "balanceOf(address)", ["address"], [to_checksum_address(holder)], block)


def erc20_decimals(rpc: Rpc, token: str, block: int | str = "latest") -> int | None:
    try:
        return _call_uint(rpc, token, "decimals()", block=block)
    except ChainError:
        return None


def erc20_symbol(rpc: Rpc, token: str, block: int | str = "latest") -> str | None:
    try:
        raw = rpc.call_raw(token, _sel("symbol()"), block=block)
    except ChainError:
        return None
    try:
        return abi_decode(["string"], raw)[0]
    except Exception:  # noqa: BLE001  (some tokens return bytes32)
        s = raw[:32].rstrip(b"\0")
        return s.decode(errors="ignore") or None


def erc4626_convert_to_assets(rpc: Rpc, vault: str, shares: int, block: int | str = "latest") -> int:
    return _call_uint(rpc, vault, "convertToAssets(uint256)", ["uint256"], [shares], block)


def erc4626_asset(rpc: Rpc, vault: str, block: int | str = "latest") -> str | None:
    try:
        raw = rpc.call_raw(vault, _sel("asset()"), block=block)
        return to_checksum_address(abi_decode(["address"], raw)[0])
    except (ChainError, Exception):  # noqa: BLE001
        return None


def token_list(catalog: Catalog, protocols: list[str], vault: VaultConfig) -> list[dict]:
    """[{name, address, kind}] from the used cards' token contracts and defios.yaml `tokens:`; deduped."""
    out: dict[str, dict] = {}
    for proto in protocols:
        card = catalog.cards.get(proto)
        if not card:
            continue
        for name in card.contracts:
            try:
                meta = catalog.contract_meta(proto, name)
                addr = catalog.resolve_contract(name, proto, vault.book_vars)
            except (CardError, KeyError):
                continue
            kind = str(meta.get("kind") or "")
            raw = card.contracts.get(name) or {}
            label = raw["ref"] if isinstance(raw, dict) and "ref" in raw else f"{proto}.{name}"
            if addr and (kind.startswith("erc20") or kind.startswith("erc4626")):
                out.setdefault(addr.lower(), {"name": label, "address": addr, "kind": kind})
    for t in vault.tokens:
        if is_address(t):
            addr, name, kind = to_checksum_address(t), t, "erc20"
        else:
            proto, _, cname = t.partition(".")
            try:
                addr = catalog.resolve_contract(t, proto, vault.book_vars)
                kind = str(catalog.contract_meta(proto, cname).get("kind") or "erc20")
            except (CardError, KeyError):
                addr = None
            name = t
        if addr:
            out.setdefault(addr.lower(), {"name": name, "address": addr, "kind": kind})
    return list(out.values())


def vault_balances(catalog: Catalog, protocols: list[str], vault: VaultConfig, rpc: Rpc,
                   block: int | str = "latest") -> dict[str, Any]:
    index = catalog.address_index(vault.book_vars)
    res: dict[str, Any] = {"holder": vault.avatar, "block": block, "tokens": []}
    try:
        wei = rpc.get_balance(vault.avatar, block)
        res["native"] = {"symbol": "ETH" if vault.chain_id != 100 else "xDAI", "raw": wei, "value": wei / 1e18}
    except ChainError as e:
        res["native"] = {"error": str(e)[:200]}
    for t in token_list(catalog, protocols, vault):
        e: dict[str, Any] = dict(t)
        try:
            bal = erc20_balance(rpc, t["address"], vault.avatar, block)
        except ChainError as err:
            e["error"] = str(err)[:200]
            res["tokens"].append(e)
            continue
        dec = erc20_decimals(rpc, t["address"], block)
        e.update(raw=bal, decimals=dec, symbol=erc20_symbol(rpc, t["address"], block))
        e["value"] = bal / 10 ** (dec if dec is not None else 18)
        if dec is None:
            e["note"] = "decimals() failed; shown with 18"
        if bal and t["kind"].startswith("erc4626"):
            try:
                assets = erc4626_convert_to_assets(rpc, t["address"], bal, block)
                asset = erc4626_asset(rpc, t["address"], block)
                adec = erc20_decimals(rpc, asset, block) if asset else None
                names = index.get(asset.lower(), []) if asset else []
                e["assets"] = {"raw": assets, "asset": asset,
                               "asset_name": ".".join(names[0]) if names else None,
                               "value": assets / 10 ** (adec if adec is not None else 18)}
            except ChainError as err:
                e["assets"] = {"error": str(err)[:200]}
        res["tokens"].append(e)
    return res


def plain_balances(b: dict) -> list[str]:
    lines = []
    nat = b.get("native") or {}
    if nat.get("raw"):
        lines.append(f"  {nat['symbol']}: {nat['value']:.6g}")
    for t in b.get("tokens") or []:
        if not t.get("raw"):
            continue
        s = f"  {t.get('symbol') or t['name']} ({t['name']}): {t['value']:.6g}"
        a = t.get("assets") or {}
        if "value" in a:
            s += f"  = {a['value']:.6g} {a.get('asset_name') or a.get('asset')}"
        lines.append(s)
    return lines
