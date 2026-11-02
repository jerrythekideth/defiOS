---
name: defios-pendle
description: Use any Pendle V2 market by its address from a Safe vault with defios. Use when asked to buy or sell a Pendle PT, to mint or redeem SY, to redeem PT at maturity, to add or remove Pendle liquidity, to "get fixed yield on X", or when the human pastes an app.pendle.finance link.
---

# defios Pendle: any market, by address

**Read `AGENTS.md` first.** Its rules override anything here. The card is
`catalog/pendle/card.yaml`; the generic actions are the `pendle.any_*` ones.

## The pieces

A Pendle market (the LP token) has three tokens:

* **SY**: wraps the yield token (siUSD, sUSDe, …). `deposit` turns the yield token into SY.
* **PT**: principal. It trades at a discount and redeems 1:1 into SY at expiry. Buying PT locks a fixed yield.
* **YT**: the yield. defios does not trade it.

The human brings the **market address**: the 0x… in
`app.pendle.finance/trade/markets/<address>/…` (also `/trade/pools/<address>`).
It is not a Morpho market id (32 bytes) and not the PT, SY or YT address.

```bash
defios resolve https://app.pendle.finance/trade/markets/0x…/swap
```

`resolve` shows what the engine reads: which factory vouched for the market,
the SY, PT and YT (with symbols), the expiry, and the yield token. Show these
to the human before you whitelist anything.

## What the engine checks (it refuses otherwise)

* `isValidMarket(market)` answers true on a catalogued Pendle market factory.
* `readTokens()` on the market gives SY, PT and YT, and `SY.yieldToken()` gives
  the yield token. The yield token must be a **verified** catalog token.
* The role pins that market, its SY, PT and YT (call targets, approve spenders
  and the `market` arg), so a lookalike market fails.
* **An SY that no card lists is refused.** Anyone can create an SY, and the
  market factory does not fix the SY's code: a market built on a hostile SY
  passes `isValidMarket`. `plan`, `permit` and recipes refuse it until the
  human has checked that the market is listed on app.pendle.finance and that
  its SY is Pendle's, and then passes that exact SY address:
  `--param accept_sy=0x…` (`resolve` prints the line to use). Never add it
  yourself.
* `propose` proves the market again onchain before it accepts the SY, PT or YT
  as a call target (calls.json carries the `lookups` record from `plan`).

## Actions (`--param market_address=0x…` on every one)

| action | input | does |
|---|---|---|
| `pendle.any_mint_sy` | yield token | yield token -> SY (`$min_sy`) |
| `pendle.any_buy_pt` | SY | SY -> PT through the Pendle router (`$min_pt`) |
| `pendle.any_sell_pt` | PT | PT -> SY before expiry |
| `pendle.any_redeem_pt` | PT | PT -> SY after expiry |
| `pendle.any_redeem_sy` | SY | SY -> yield token (`$min_out`) |
| `pendle.any_add_liquidity` / `any_remove_liquidity` | SY / LP | single-sided SY liquidity in, SY + PT out |

## Flows

```bash
# whitelist one market: the recipe adds the core actions plus the yield token card's actions
defios recipe pendle_pt@market_address=0x…,accept_sy=0x…          # read-only: what it expands to
defios permit --recipe pendle_pt@market_address=0x…,accept_sy=0x…  # Tx Builder JSON for the Vault queue

# siUSD -> SY -> PT in one plan (amounts in each step's input token; leave a margin)
defios plan pendle.any_mint_sy=1000 pendle.any_buy_pt=999 \
  --param market_address=0x… --param accept_sy=0x… --param min_sy=<raw> --param min_pt=<raw>
```

The human sets the minimums from a quote they trust. defios does not quote
prices, so never pass a minimum of 0 unless the human says so in writing.
Then simulate, run the propose dry run, and `--send` only after the human says yes.

## Pitfalls

* SY to PT is not 1:1: the PT amount depends on the market's implied rate.
  Simulate it.
* After expiry, `any_buy_pt` and `any_sell_pt` revert. Use `any_redeem_pt`.
* The yield token itself (siUSD here) comes from its own card (`infinifi`).
  If no card covers getting into or out of it, say so. Never route through
  Pendle's aggregator swaps (`swapExactTokenForPt` with a swap payload): they
  carry arbitrary calls.
