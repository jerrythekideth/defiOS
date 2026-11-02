"""Recipes: named strategies that expand to card actions, for `defios permit --recipe` / `defios recipe`.

    morpho_loop@market_id=0x…        supply_collateral, borrow, repay, withdraw_collateral on that market,
                                     plus the collateral card's actions that take the loan token (acquire)
                                     or the collateral (unwind)
    pendle_pt@market_address=0x…     mint_sy, buy_pt, sell_pt, redeem_pt, redeem_sy on that Pendle market,
                                     plus the yield token card's actions that take the yield token

A recipe only picks actions; every condition still comes from the cards. What no card covers (savUSD has no
mint on Ethereum) comes back as a note, never as an improvised call."""
from __future__ import annotations

from . import lookups
from .catalog import Catalog, CardError

RECIPES = {
    "morpho_loop": {
        "param": "market_id", "card": "morpho_markets", "lookup": "market",
        "core": ["supply_collateral", "borrow", "repay", "withdraw_collateral"],
        "token": "collateralToken", "base": "loanToken",
        "summary": "leverage loop on one Morpho Blue market: post collateral, borrow the loan token, swap it back "
                   "into collateral, repeat; unwind in reverse",
    },
    "pendle_pt": {
        "param": "market_address", "card": "pendle", "lookup": "any_market",
        "core": ["any_mint_sy", "any_buy_pt", "any_sell_pt", "any_redeem_pt", "any_redeem_sy"],
        "token": "yield_token", "base": None,
        "summary": "fixed yield on one Pendle market: yield token -> SY -> PT; exit by selling PT before expiry or "
                   "redeeming it after",
    },
}


class RecipeError(ValueError):
    pass


def _token_card(catalog: Catalog, address: str) -> str | None:
    hits = catalog.address_index().get(address.lower()) or []
    return hits[0][0] if hits else None


def _input_address(catalog: Catalog, proto: str, action: dict) -> str | None:
    inp = action.get("input")
    if not inp or inp == "ETH":
        return None
    try:
        return catalog.resolve_contract(str(inp), proto)
    except CardError:
        return None


def expand(catalog: Catalog, spec: str, rpc, book_vars: dict | None = None) -> dict:
    """'morpho_loop@market_id=0x…' -> {recipe, refs: ['morpho_markets.borrow@market_id=0x…', …], notes, lookup}."""
    from .permit import PermitError, split_ref
    try:
        name, params = split_ref(spec)
    except PermitError as e:
        raise RecipeError(str(e)) from None
    r = RECIPES.get(name)
    if r is None:
        raise RecipeError(f"unknown recipe {name!r}; known: {', '.join(RECIPES)}")
    if r["param"] not in params:
        raise RecipeError(f"recipe {name} needs @{r['param']}=… (e.g. {name}@{r['param']}=0x…)")
    pv = params[r["param"]]
    try:
        v = lookups.resolve(catalog, r["card"], {"calls": [{"target": f"${r['lookup']}"}]}, params, rpc,
                            book_vars)
    except lookups.LookupFailed as e:
        raise RecipeError(f"recipe {name}: {e}") from None
    at = "@" + ",".join(f"{k}={x}" for k, x in params.items())
    refs = [f"{r['card']}.{a}{at}" for a in r["core"]]
    notes: list[str] = list(v.get("_lookup_warnings") or [])
    tok = v[f"{r['lookup']}.{r['token']}"]
    base = v.get(f"{r['lookup']}.{r['base']}") if r["base"] else None
    tcard = _token_card(catalog, tok)
    tname = next((f"{p}.{c}" for p, c in catalog.address_index().get(tok.lower(), [])), tok)
    related, others = [], []
    if tcard:
        for aname, act in catalog.cards[tcard].actions.items():
            a_in = _input_address(catalog, tcard, act or {})
            if a_in and a_in.lower() == tok.lower():
                related.append(f"{tcard}.{aname}")  # takes the collateral / yield token: unwind
            elif base and a_in and a_in.lower() == base.lower():
                related.append(f"{tcard}.{aname}")  # takes the loan token: acquire the collateral
            elif a_in:
                others.append(f"{tcard}.{aname} (takes {act.get('input')})")
    refs += related
    if not related:
        notes.append(f"no card action takes {tname}" + (" or the loan token" if base else "") +
                     f" in {tcard or 'any card'}: getting into / out of {tname} is not covered by a card (bridge, "
                     f"DEX or issuer flow). Say so; do not improvise a route")
    if others:
        notes.append(f"other {tcard} actions, not added (their output is not declared): " + ", ".join(others))
    return {"recipe": name, "summary": r["summary"], "param": {r["param"]: pv}, "refs": refs,
            "lookup": {k.split(".", 1)[1]: x for k, x in v.items() if k.startswith(r["lookup"] + ".")},
            "notes": notes}
