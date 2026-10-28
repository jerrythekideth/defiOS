"""Card `lookups:`: values the engine reads onchain from an id the human gives (Morpho market params).

    lookups:
      market:
        param: market_id                 # --param market_id=0x… (bytes32)
        target: morpho_blue
        read: "idToMarketParams(bytes32 id) returns (address loanToken, address collateralToken, …)"
        id: keccak                       # keccak256(abi.encode(result)) must equal the id
        tokens: [loanToken, collateralToken]   # must be verified tokens of the catalog

Resolved at plan / permit time over RPC into vars: `market` (the tuple, for a tuple param) and
`market.<field>` (each field). `market` is a *pinned* var: `check` tests hostile variants of its
address fields and `permit` pins every field with EqualTo, so the role allows that market only.
An action's `token_from: market.loanToken` makes that field the action's `$token` (approve target,
decimals of the amount, end-of-batch revoke).
"""
from __future__ import annotations

from typing import Any

from eth_abi import encode as abi_encode
from eth_utils import keccak, to_checksum_address

from .catalog import AVATAR, Catalog, CardError, parse_signature

PINNED = "_pinned"


class LookupFailed(ValueError):
    pass


def card_lookups(catalog: Catalog, proto: str) -> dict:
    return (catalog.cards[proto].raw.get("lookups") or {}) if proto in catalog.cards else {}


def used_lookups(catalog: Catalog, proto: str, action: dict) -> list[str]:
    """Names of the card's lookups that an action's calls or `token_from` use."""
    lk = card_lookups(catalog, proto)
    used: set[str] = set()

    def walk(spec):
        if isinstance(spec, str) and spec.startswith("$") and spec != AVATAR:
            name = spec[1:].split(".", 1)[0]
            if name in lk:
                used.add(name)
        elif isinstance(spec, dict):
            for v in spec.values():
                walk(v)
        elif isinstance(spec, list):
            for v in spec:
                walk(v)

    for c in action.get("calls") or []:
        walk(c.get("args") or {})
    tf = action.get("token_from")
    if tf and str(tf).split(".", 1)[0] in lk:
        used.add(str(tf).split(".", 1)[0])
    return sorted(used)


def _bytes32(v: Any, what: str) -> bytes:
    s = str(v).strip().lower()
    if s.startswith("0x"):
        s = s[2:]
    if len(s) != 64:
        raise LookupFailed(f"{what}: expected a 32-byte hex id (0x + 64 hex chars), got {v!r}")
    try:
        return bytes.fromhex(s)
    except ValueError:
        raise LookupFailed(f"{what}: not hex: {v!r}") from None


def _token_status(catalog: Catalog, address: str) -> str:
    """'verified' / 'unverified' / 'conflict' / 'unknown' for a token address, from the registry."""
    from .registry import Registry, load_results
    any_card = next(iter(catalog.cards.values()), None)
    if any_card is None:
        return "unknown"
    reg = Registry.from_catalog(catalog)
    w = reg.whois(address, 1, load_results(any_card.path.parent.parent))
    if not w["matches"]:
        return "unknown"
    return w["verdict"].split(":")[0]


def resolve(catalog: Catalog, proto: str, action: dict, params: dict, rpc, book_vars: dict | None = None) -> dict:
    """vars for every lookup the action uses: {name: tuple, 'name.field': value, '_pinned': {name}}."""
    names = used_lookups(catalog, proto, action)
    out: dict[str, Any] = {}
    if not names:
        return out
    lk = card_lookups(catalog, proto)
    for name in names:
        spec = lk[name]
        pname = spec["param"]
        where = f"{proto} lookup {name}"
        if params.get(pname) is None:
            raise LookupFailed(f"{where}: pass --param {pname}=<id> (the {name} is pinned in the role by its id)")
        if rpc is None:
            raise LookupFailed(f"{where}: needs RPC_URL to read {spec['read'].split('(')[0]} onchain")
        ident = _bytes32(params[pname], f"--param {pname}")
        try:
            target = catalog.resolve_contract(spec["target"], proto, book_vars)
        except CardError as e:
            raise LookupFailed(f"{where}: {e}") from None
        sig = parse_signature(spec["read"])
        in_sig = f"{sig.name}({','.join(p.type for p in sig.params)})"
        out_types = [p.type for p in sig.outputs]
        vals = rpc.call(target, in_sig, [ident], out_types)
        vals = list(vals) if len(out_types) > 1 else [vals]
        vals = [to_checksum_address(v) if t == "address" else v for v, t in zip(vals, out_types)]
        if all(not v or (isinstance(v, str) and int(v, 16) == 0) for v in vals):
            raise LookupFailed(f"{where}: {pname} {params[pname]} does not exist onchain ({sig.name} returned zeros)")
        if spec.get("id") == "keccak" and keccak(abi_encode(out_types, vals)) != ident:
            raise LookupFailed(f"{where}: keccak256 of the returned params does not match {pname}; refusing")
        fields = {p.name: v for p, v in zip(sig.outputs, vals)}
        for f in spec.get("tokens") or []:
            st = _token_status(catalog, fields[f])
            if st != "verified":
                raise LookupFailed(f"{where}: {f} {fields[f]} is {st} in the catalog; only verified tokens "
                                   f"(catalog/TOKENS.md) can be used. Add the token first (catalog/ADDING.md §2)")
        out[name] = tuple(vals)
        for k, v in fields.items():
            out[f"{name}.{k}"] = v
        out.setdefault(PINNED, set()).add(name)
        out.setdefault("_lookup_ids", {})[name] = "0x" + ident.hex()
    return out


def token_from(action: dict, vars_: dict) -> str | None:
    tf = action.get("token_from")
    return vars_.get(str(tf)) if tf else None
