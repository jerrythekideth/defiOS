"""`defios resolve <thing>`: turn what a human pastes (an app URL, an address, a 32-byte id, a symbol) into what
the catalog knows about it and the card actions + `--param`s that take it. Read-only.

    app.morpho.org/ethereum/market/<id>/…      -> morpho_markets.* --param market_id=<id>
    app.morpho.org/ethereum/vault/<addr>/…     -> morpho_vaults.*_any --param vault_address=<addr>
    app.pendle.finance/trade/markets/<addr>/…  -> pendle.any_* --param market_address=<addr>
    0x… (20 bytes)  -> whois, else an address lookup that proves it (any Morpho vault, any Pendle market)
    0x… (32 bytes)  -> an id lookup (a Morpho Blue market)
    a symbol        -> catalog contracts by name / alias

Nothing found is an answer: `covered: false`, say so to the human (AGENTS.md rule 2)."""
from __future__ import annotations

import re
from typing import Any

from eth_utils import is_address, to_checksum_address

from . import lookups
from .catalog import Catalog

CHAINS = {"ethereum": 1, "mainnet": 1, "eth": 1, "base": 8453, "arbitrum": 42161, "optimism": 10,
          "polygon": 137, "unichain": 130, "katana": 747474}
HEX = r"0x[0-9a-fA-F]{40}(?:[0-9a-fA-F]{24})?"
# (pattern, the kind the app means by it); the chain is group "chain" when the URL carries one
URLS = [
    (rf"morpho\.org/(?P<chain>[a-z0-9-]+)/(?:market|variable|fixed)/(?P<v>{HEX})", "morpho market"),
    (rf"morpho\.org/(?P<chain>[a-z0-9-]+)/vault/(?P<v>{HEX})", "morpho vault"),
    (rf"morpho\.org/market\?id=(?P<v>{HEX})", "morpho market"),
    (rf"morpho\.org/vault\?vault=(?P<v>{HEX})", "morpho vault"),
    (rf"pendle\.finance/trade/(?:markets|pools)/(?P<v>{HEX})", "pendle market"),
]


def parse(thing: str) -> dict:
    """{value, hint, chain} from a URL, or the raw value."""
    s = thing.strip()
    for pat, hint in URLS:
        m = re.search(pat, s)
        if m:
            chain = m.groupdict().get("chain")
            cm = re.search(r"chain(?:Id)?=(\d+)|chain=([a-z]+)", s)
            cid = CHAINS.get(chain) if chain else (int(cm.group(1)) if cm and cm.group(1) else
                                                   CHAINS.get(cm.group(2)) if cm else None)
            return {"value": m.group("v"), "hint": hint, "chain_id": cid, "url": s}
    if s.startswith("http"):
        hexes = re.findall(HEX, s)
        return {"value": hexes[0] if hexes else s, "hint": None, "chain_id": None, "url": s}
    return {"value": s, "hint": None, "chain_id": None}


def _actions_for(catalog: Catalog, proto: str, lname: str) -> list[str]:
    return [f"{proto}.{a}" for a, act in catalog.cards[proto].actions.items()
            if lname in lookups.used_lookups(catalog, proto, act or {})]


def _try_lookup(catalog: Catalog, proto: str, lname: str, value: str, rpc, book_vars) -> dict:
    spec = lookups.card_lookups(catalog, proto)[lname]
    try:
        v = lookups.resolve(catalog, proto, {"calls": [{"target": f"${lname}"}]}, {spec["param"]: value}, rpc,
                            book_vars, read_only=True)
    except lookups.LookupFailed as e:
        return {"card": proto, "lookup": lname, "ok": False, "why": str(e)}
    fields = {k.split(".", 1)[1]: (to_checksum_address(x) if isinstance(x, str) and is_address(x) else x)
              for k, x in v.items() if k.startswith(lname + ".")}
    names = {a: m["symbol"] for a, m in (v.get("_lookup_tokens") or {}).items()}
    idx = catalog.address_index(book_vars)
    for k, x in list(fields.items()):
        if isinstance(x, str) and is_address(x):
            n = names.get(x.lower()) or ", ".join(f"{p}.{c}" for p, c in idx.get(x.lower(), [])[:2])
            if n:
                fields[k] = f"{x} ({n})"
    return {"card": proto, "lookup": lname, "ok": True, "param": spec["param"], "value": v[lname] if isinstance(
            v[lname], str) else (v.get("_lookup_ids") or {}).get(lname), "fields": fields,
            "proved_by": (v.get("_lookup_checks") or {}).get(lname), "warnings": v.get("_lookup_warnings") or [],
            "actions": _actions_for(catalog, proto, lname),
            "use": f"--param {spec['param']}={value}"}


def _token_onchain(rpc, address: str) -> dict | None:
    out: dict[str, Any] = {}
    for fn, t in (("symbol()", "string"), ("name()", "string"), ("decimals()", "uint8")):
        try:
            out[fn[:-2]] = rpc.call(address, fn, [], [t])
        except Exception:  # noqa: BLE001
            pass
    return out or None


def resolve(catalog: Catalog, thing: str, rpc=None, book_vars: dict | None = None, chain_id: int = 1) -> dict:
    from .plan import find_contracts
    from .registry import Registry, load_results
    p = parse(thing)
    v, hint = p["value"], p["hint"]
    out: dict[str, Any] = {"input": thing, "value": v, "app_says": hint, "covered": False, "matches": [],
                           "next": []}
    if p.get("chain_id") and p["chain_id"] != chain_id:
        out["why"] = f"that link is for chain {p['chain_id']}; this vault is on chain {chain_id}"
        return out
    n = lookups._hex_len(v) if str(v).startswith("0x") else -1
    shape = "address" if n == 40 else "id" if n == 64 else None
    if shape is None:
        hits = find_contracts(catalog, str(v), book_vars)
        out["kind"] = "name"
        for h in hits:
            meta = catalog.contract_meta(h["protocol"], h["name"])
            out["matches"].append({"card": h["protocol"], "contract": h["name"], "address": h["address"],
                                   "category": meta.get("category"), "decimals": meta.get("decimals")})
        out["covered"] = bool(hits)
        if not hits:
            out["why"] = (f"{v!r} matches no card contract name or alias: the catalog does not cover it. Say so; "
                          f"do not guess an address (AGENTS.md rule 2). Ask for the address or an app link")
        return out
    out["kind"] = shape
    if hint and ("id" if hint == "morpho market" else "address") != shape:
        out.setdefault("warnings", []).append(f"the link says {hint}, but the value is {'an id' if shape == 'id' else 'an address'}")
    if shape == "address":
        reg = Registry.from_catalog(catalog)
        w = reg.whois(v, chain_id, load_results(next(iter(catalog.cards.values())).path.parent.parent))
        if w["matches"]:
            out["whois"] = {"verdict": w["verdict"], "matches": [
                {k: m.get(k) for k in ("protocol", "name", "ref", "category", "status", "actions")
                 if m.get(k) is not None} for m in w["matches"]]}
            out["covered"] = w["verdict"] == "verified"
    if rpc is None:
        out.setdefault("warnings", []).append("no RPC_URL: lookups that prove the value onchain were not run")
    else:
        want_addr = shape == "address"
        for proto in catalog.cards:
            for lname, spec in lookups.card_lookups(catalog, proto).items():
                if ((spec or {}).get("kind") == "address") != want_addr:
                    continue
                r = _try_lookup(catalog, proto, lname, v, rpc, book_vars)
                if r["ok"]:
                    out["matches"].append(r)
                    out["covered"] = True
                    out["next"] += [f"{a} {r['use']}" for a in r["actions"]]
                elif "not recognised" not in r["why"] and "does not exist" not in r["why"]:
                    out.setdefault("rejected", []).append(r)  # it IS that kind of thing, but refused (unverified token…)
        if want_addr and not out["covered"] and "whois" not in out:
            tok = _token_onchain(rpc, to_checksum_address(v))
            if tok:
                out["onchain"] = tok
    if not out["covered"]:
        out["why"] = out.get("why") or (
            "not covered: no verified catalog record, and no card lookup proves it onchain"
            + (" (see `rejected` for the lookup that recognised it but refused)" if out.get("rejected") else "")
            + ". Say so to the human; adding it is a catalog change (catalog/ADDING.md)")
    return out


def summarize(r: dict) -> str:
    lines = [f"RESOLVE {r['input']}" + (f"  (the app calls it a {r['app_says']})" if r.get("app_says") else "")]
    if r.get("whois"):
        for m in r["whois"]["matches"]:
            lines.append(f"  catalog: {m.get('ref') or m.get('name')} [{m.get('status')}]")
    for m in r["matches"]:
        if "lookup" in m:
            lines.append(f"  {m['card']} {m['lookup']}: proved by {m['proved_by'] or 'its id (keccak)'}")
            for k, v in m["fields"].items():
                if k == "expiry" and isinstance(v, int):
                    from datetime import datetime, timezone
                    v = f"{v} ({datetime.fromtimestamp(v, timezone.utc):%Y-%m-%d %H:%M} UTC)"
                lines.append(f"     {k} = {v}")
            for w in m["warnings"]:
                lines.append(f"     warning: {w}")
        else:
            lines.append(f"  {m['card']}.{m['contract']} {m['address']} ({m.get('category')}, "
                         f"decimals {m.get('decimals')})")
    for x in r.get("rejected") or []:
        lines.append(f"  {x['card']} {x['lookup']} refused: {x['why']}")
    if r.get("onchain"):
        lines.append(f"  onchain: {r['onchain']} (not in the catalog)")
    for n in r["next"]:
        lines.append(f"  use: {n}")
    for w in r.get("warnings") or []:
        lines.append(f"  warning: {w}")
    if not r["covered"]:
        lines.append(f"  NOT COVERED: {r['why']}")
    return "\n".join(lines)
