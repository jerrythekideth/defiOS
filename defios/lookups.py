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

An address lookup may also read a multi-output signature (one field per named output), `derived:
{field: {from: <field>, read: …}}` (read on an address it read), `derived_tokens:` (fields, or `$self`, that the
proof vouches for as tokens: decimals and symbol read onchain), `uncatalogued_code:` (address fields whose code
the proof does not fix: a warning) and `warn:`. Every address field is pinned like the lookup address.
`plan` records each address lookup (`_lookup_proofs` -> calls.json `lookups`); `reprove` runs them again
for `propose`.
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


def _hex_len(v: Any) -> int:
    s = str(v).strip().lower()
    s = s[2:] if s.startswith("0x") else s
    try:
        bytes.fromhex(s)
    except ValueError:
        return -1
    return len(s)


def shape_hint(catalog: Catalog | None, v: Any) -> str:
    """' (that looks like a <what>: use <card>.* with --param <p>)' when `v` fits another card's lookup.
    Catches the classic mix-up: a Morpho market id (bytes32) given as a vault address, or the reverse."""
    if catalog is None:
        return ""
    n = _hex_len(v)
    want = "address" if n == 40 else "id" if n == 64 else None
    if want is None:
        return ""
    hits = []
    for proto in catalog.cards:
        for name, spec in card_lookups(catalog, proto).items():
            is_addr = (spec or {}).get("kind") == "address"
            if (want == "address") == is_addr:
                hits.append(f"{proto}.* with --param {spec['param']}=")
    what = "contract address (20 bytes)" if want == "address" else "32-byte id, e.g. a Morpho Blue market id"
    return f" (that looks like a {what}: use {' or '.join(dict.fromkeys(hits))})" if hits else ""


def _bytes32(v: Any, what: str, catalog: Catalog | None = None) -> bytes:
    s = str(v).strip().lower()
    if s.startswith("0x"):
        s = s[2:]
    if len(s) != 64:
        raise LookupFailed(f"{what}: expected a 32-byte hex id (0x + 64 hex chars), got {v!r}"
                           + shape_hint(catalog, v))
    try:
        return bytes.fromhex(s)
    except ValueError:
        raise LookupFailed(f"{what}: not hex: {v!r}") from None


def _address(v: Any, what: str, catalog: Catalog | None = None) -> str:
    s = str(v).strip()
    if not is_address(s) or int(s, 16) == 0:
        raise LookupFailed(f"{what}: expected a contract address (0x + 40 hex chars), got {v!r}"
                           + shape_hint(catalog, v))
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
            addr = _address(params[pname], f"--param {pname}", catalog)
            hit = check_address(catalog, proto, spec, addr, rpc, book_vars)
            if not hit:
                raise LookupFailed(f"{where}: {addr} is not recognised by any of "
                                   + ", ".join(f"{c['target']}.{c['read'].split('(')[0]}" for c in spec["check_any"])
                                   + "; refusing")
            fields = _address_fields(rpc, addr, spec, where)
            _check_tokens(catalog, where, spec, fields)
            out[name] = addr
            pinned = out.setdefault(PINNED, set())
            pinned.add(name)
            for k, v in fields.items():
                out[f"{name}.{k}"] = v
                if isinstance(v, str) and is_address(v):
                    pinned.add(f"{name}.{k}")  # read from a proved address: a target / arg pinned with EqualTo
            for f in spec.get("derived_tokens") or []:
                a = addr if f == SELF else fields[f]
                out.setdefault("_lookup_tokens", {})[a.lower()] = _token_meta(rpc, a, f"{proto}.{name}.{'lp' if f == SELF else f}", where)
            for f in spec.get("uncatalogued_code") or []:
                a = fields[f]
                if not catalog.address_index(book_vars).get(a.lower()):
                    out.setdefault("_lookup_warnings", []).append(
                        f"{proto} {name}: {f} {a} is not in any card; its code is not fixed by "
                        f"{hit.split('.')[0]}. {spec.get('warn') or 'Check it before sending funds to it.'}")
            out.setdefault("_lookup_ids", {})[name] = addr
            out.setdefault("_lookup_checks", {})[name] = hit
            out.setdefault("_lookup_proofs", []).append({"card": proto, "lookup": name, "param": pname,
                                                          "value": addr})
            continue
        ident = _bytes32(params[pname], f"--param {pname}", catalog)
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


SELF = "$self"


def _address_fields(rpc, addr: str, spec: dict, where: str) -> dict:
    """`reads:` on the address (a multi-output read gives one field per named output), then `derived:`
    reads on an address field ({from: sy, read: "yieldToken() returns (address)"})."""
    fields: dict[str, Any] = {}
    try:
        for f, r in (spec.get("reads") or {}).items():
            sig, vals = parse_signature(r), _read(rpc, addr, r, [])
            if len(sig.outputs) > 1:
                fields.update({o.name or f"{f}{i}": v for i, (o, v) in enumerate(zip(sig.outputs, vals))})
            else:
                fields[f] = vals[0]
        for f, d in (spec.get("derived") or {}).items():
            fields[f] = _read(rpc, fields[d["from"]], d["read"], [])[0]
    except LookupFailed:
        raise
    except Exception as e:  # noqa: BLE001 - a read that reverts: not the contract the card expects
        raise LookupFailed(f"{where}: reading {addr} failed ({e}); not the contract this lookup expects") from None
    return fields


def _token_meta(rpc, address: str, ref: str, where: str) -> dict:
    """{symbol, address, decimals, ref} of a token the lookup proved (a Pendle PT), read onchain."""
    try:
        dec = int(rpc.call(address, "decimals()", [], ["uint8"]))
    except Exception as e:  # noqa: BLE001
        raise LookupFailed(f"{where}: {ref} {address} has no decimals() ({e})") from None
    try:
        sym = str(rpc.call(address, "symbol()", [], ["string"]))
    except Exception:  # noqa: BLE001 - a symbol is only for display
        sym = ref
    return {"symbol": sym, "address": to_checksum_address(address), "decimals": dec, "ref": ref}


def reprove(catalog: Catalog, proofs: list[dict] | None, rpc, book_vars: dict | None = None) -> dict[str, str]:
    """Re-run the address lookups a plan recorded (calls.json `lookups`) and return every address they
    prove ({address_lower: 'pendle any_market 0x… sy'}), so propose can accept derived targets (a market's
    SY / PT). Raises LookupFailed when a lookup no longer proves out."""
    out: dict[str, str] = {}
    for pr in proofs or []:
        proto, name = pr.get("card"), pr.get("lookup")
        spec = card_lookups(catalog, proto).get(name)
        if not spec or spec.get("kind") != "address":
            raise LookupFailed(f"calls.json lookup {proto}.{name}: no such address lookup in the catalog")
        action = {"calls": [{"target": f"${name}"}]}
        v = resolve(catalog, proto, action, {spec["param"]: pr.get("value")}, rpc, book_vars)
        for k, a in v.items():
            if not k.startswith("_") and isinstance(a, str) and is_address(a):
                out[a.lower()] = f"{proto} {name} {v[name]}" + (f" {k.split('.', 1)[1]}" if "." in k else "")
        for a in v.get("_lookup_tokens") or {}:
            out.setdefault(a, f"{proto} {name} {v[name]} token")
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
