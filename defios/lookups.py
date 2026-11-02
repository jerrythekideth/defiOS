"""Card `lookups:`: values the engine reads onchain from an id the human gives (Morpho market params,
any Morpho vault by address).

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

An address lookup takes a contract the catalog does not list and proves what it is from catalogued
contracts before anything uses it:

      any_vault:
        param: vault_address             # --param vault_address=0x…
        kind: address
        check_any:                       # at least one catalogued factory must answer true
          - { target: metamorpho_factory, read: "isMetaMorpho(address) returns (bool)" }
        reads: { asset: "asset() returns (address)" }   # read on the address itself
        tokens: [asset]

Vars: `any_vault` (the address: a call target or an address arg, pinned with EqualTo) and `any_vault.asset`.
"""
from __future__ import annotations

from typing import Any

from eth_abi import encode as abi_encode
from eth_utils import is_address, keccak, to_checksum_address

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
        walk(c.get("target"))
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


def _address(v: Any, what: str) -> str:
    s = str(v).strip()
    if not is_address(s) or int(s, 16) == 0:
        raise LookupFailed(f"{what}: expected a contract address (0x + 40 hex chars), got {v!r}")
    return to_checksum_address(s)


def _read(rpc, target: str, read: str, args: list) -> list:
    sig = parse_signature(read)
    out_types = [p.type for p in sig.outputs]
    vals = rpc.call(target, f"{sig.name}({','.join(p.type for p in sig.params)})", args, out_types)
    vals = list(vals) if len(out_types) > 1 else [vals]
    return [to_checksum_address(v) if t == "address" else v for v, t in zip(vals, out_types)]


def check_address(catalog: Catalog, proto: str, spec: dict, address: str, rpc, book_vars: dict | None = None) -> str | None:
    """The first `check_any` read that answers true for `address` (e.g. 'metamorpho_factory.isMetaMorpho'), else None."""
    for chk in spec.get("check_any") or []:
        target = catalog.resolve_contract(chk["target"], proto, book_vars)
        try:
            ok = _read(rpc, target, chk["read"], [address])[0]
        except Exception:  # noqa: BLE001 - a factory that reverts does not vouch for the address
            ok = False
        if ok is True:
            return f"{chk['target']}.{parse_signature(chk['read']).name}"
    return None


def recognize(catalog: Catalog, address: str, rpc, book_vars: dict | None = None) -> str | None:
    """'<card> <lookup> (<check>)' when some card's address lookup vouches for `address` onchain."""
    if rpc is None:
        return None
    for proto in catalog.cards:
        for name, spec in card_lookups(catalog, proto).items():
            if (spec or {}).get("kind") == "address":
                hit = check_address(catalog, proto, spec, to_checksum_address(address), rpc, book_vars)
                if hit:
                    return f"{proto} {name} ({hit})"
    return None


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
            raise LookupFailed(f"{where}: needs RPC_URL to read it onchain")
        if spec.get("kind") == "address":
            addr = _address(params[pname], f"--param {pname}")
            hit = check_address(catalog, proto, spec, addr, rpc, book_vars)
            if not hit:
                raise LookupFailed(f"{where}: {addr} is not recognised by any of "
                                   + ", ".join(f"{c['target']}.{c['read'].split('(')[0]}" for c in spec["check_any"])
                                   + "; refusing")
            fields = {f: _read(rpc, addr, r, [])[0] for f, r in (spec.get("reads") or {}).items()}
            _check_tokens(catalog, where, spec, fields)
            out[name] = addr
            for k, v in fields.items():
                out[f"{name}.{k}"] = v
            out.setdefault(PINNED, set()).add(name)
            out.setdefault("_lookup_ids", {})[name] = addr
            out.setdefault("_lookup_checks", {})[name] = hit
            continue
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
        _check_tokens(catalog, where, spec, fields)
        out[name] = tuple(vals)
        for k, v in fields.items():
            out[f"{name}.{k}"] = v
        out.setdefault(PINNED, set()).add(name)
        out.setdefault("_lookup_ids", {})[name] = "0x" + ident.hex()
    return out


def _check_tokens(catalog: Catalog, where: str, spec: dict, fields: dict) -> None:
    for f in spec.get("tokens") or []:
        st = _token_status(catalog, fields[f])
        if st != "verified":
            raise LookupFailed(f"{where}: {f} {fields[f]} is {st} in the catalog; only verified tokens "
                               f"(catalog/TOKENS.md) can be used. Add the token first (catalog/ADDING.md §2)")


def token_from(action: dict, vars_: dict) -> str | None:
    tf = action.get("token_from")
    return vars_.get(str(tf)) if tf else None
