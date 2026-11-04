# Yield Basis: notes

Read 2026-11-04 from the verified sources (Blockscout) and the chain at block
26342121.

## Why deposits go through the LTDepositZap

`LT.deposit(assets, debt, min_shares, receiver)` (`0xfad3cc4b` on yb-WETH)
makes the caller choose `debt`: how much crvUSD the LEVAMM borrows to pair
with the deposited WETH in the Curve pool. A debt off the LEVAMM book's
ratio is a fee-free trade against the other holders, so the WETH pool's
policy (`YBOraclizedPolicy`, `POLICY()` of the pool) checks every LT deposit
in the pool callback:

```
|added_debt - added_lp * amm.get_debt() / amm.collateral_amount()|
    <= proportional * ratio_tolerance          # 0.01%
```

and reverts with `"LT deviation"` otherwise. The ratio moves with every swap
in the pool, so a debt quoted when the plan is built is stale by the time the
Safe owners sign and execute. A direct deposit from the Vault will almost
always revert.

`LTDepositZap` (`0xF3a2…6E6A`) solves the debt inside the transaction (two
Newton steps on `calc_token_amount`) and then calls `LT.deposit(..., receiver)`
for the Vault, or `LT.deposit(..., self)` + `gauge.deposit(lt_shares,
receiver)` when `stake = true`. It is stateless and has no admin: nothing is
left in it after the call. `min_shares` (from `preview_deposit` on the zap)
is the slippage guard.

Zap selectors, all on the same contract:

| selector | signature | in the card |
|---|---|---|
| `0x8b6099db` | `deposit(address lt, uint256 assets, uint256 min_shares, address receiver)` | `deposit` |
| `0x0efe6a8b` | `deposit(address lt, uint256 assets, uint256 min_shares)` | `deposit` alt: no receiver param, shares go to `msg.sender` = the Vault, so it is safe to allow |
| `0x1144126a` | `deposit(address lt, uint256 assets, uint256 min_shares, address receiver, bool stake)` | `deposit_and_stake`, `stake` pinned to `true` |

`lt` is pinned to yb-WETH in every call: an unpinned `lt` would let the
caller pick any contract as the "market", and the zap reads the asset to pull
from it.

The zap is **not on the docs address page**. It is a Yield Basis contract by
its source (`@author Yield Basis`) and by its deployer
`0xa39E4d6bb25A8E55552D6D9ab1f5f8889DDdC80d`, which also deployed the
Factory, the GaugeController, the StakeZap and the LTSwapZap. No getter of a
known contract returns it, so the record has no onchain proof: its trust rests
on the explorer source. Ask Yield Basis to list it.

## Exits are direct

There is no gate on exits (the policy returns early when LP supply falls).

* `withdraw(shares, min_assets, receiver)` `0x0ad58d2f` on the LT. Burns the
  caller's shares, pays WETH to `receiver`. Reverts while the market is killed.
* `emergency_withdraw(shares, receiver, owner)` `0xd5569431` /
  `(shares, receiver)` `0xb78be802` on the LT. Works while killed. Pays the
  share of WETH, and crvUSD when the pool share holds more crvUSD than the debt
  share; otherwise pulls the shortfall in crvUSD from the caller
  (`emergency_withdraw_repay`). While not killed, `owner` must be `msg.sender`.
* Staked shares: `redeem(shares, receiver, owner)` on the gauge first, then
  withdraw. The StakeZap's `withdraw_and_unstake` would need a standing
  allowance on the gauge shares, so it is not used.

## Staked or not

The gauge (`g(yb-WETH)`, ERC-4626 over yb-WETH) earns YB emissions. Staked
yb-WETH gives up part of the trading fees to the unstaked holders (docs:
"Staked and unstaked economics"). The choice is a strategy call; the card
offers both (`deposit` and `deposit_and_stake`, `stake` / `unstake`).
`claim(reward, user)` pays `user`; `reward` is pinned to YB (the gauge's only
reward token on 2026-11-04).

## Other markets

`Factory.markets(i)` lists 11 markets. Active (docs): 7 WBTC, 8 cbBTC,
9 tBTC, 10 WETH. 0-6 are deprecated; market 6 is the legacy WETH market
(registry.yaml, `retired`). The BTC markets use `YBTwocryptoPolicy`; check
whether it carries the same LT gate before adding them.
