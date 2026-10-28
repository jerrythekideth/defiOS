---
name: defios-tokens
description: Work with ERC-20 tokens held or used by a Safe vault through defios: find the verified address and decimals of a token by symbol, tell a real token from a lookalike, read balances, convert human amounts, handle approvals, wrap / unwrap, and vault shares vs assets. Use when a request names a token ("100 USDC", "our stETH", "what is 0x… token"), or before any amount goes into `defios plan`.
---

# defios tokens

**Read `AGENTS.md` first.** Its rules override anything here.

## The whitelist

`catalog/TOKENS.md` (generated) lists every ERC-20 the catalog knows: plain
tokens, receipt tokens (aTokens, LP tokens) and vault shares, with address,
decimals, card, kind and underlying.

* **Verified** section: proved onchain (`defios registry verify`). Only these
  may go into a plan.
* **Not verified** section: for reading only. Say so and stop if a request needs one.
* Not listed: the token is unknown. Run `defios whois 0x…`; if it says
  `unknown`, tell the human and do not guess. Adding it is a card change
  (`catalog/ADDING.md` §2), not something you improvise.

Symbols are not unique: a scam token can copy any symbol. Always resolve a
token by its catalog key (`USDC`, `lido.wstETH`), never by an onchain
`symbol()` or an address someone pasted.

## Common tasks

| need | do |
|---|---|
| address / decimals of a symbol | grep `catalog/TOKENS.md`, then read the record in its card |
| what is this token address | `defios whois 0x…` |
| what does the vault hold | `defios --format text state` (syncs first) |
| amount in an action | give the human number (`=1000`, `--amount 0.5`); `plan` converts it with the card's `decimals` and refuses if the chain disagrees |
| which actions take this token | grep `catalog/ACTIONS.md` for the symbol (`input` and `tokens` columns) |
| ETH <-> WETH | `weth.wrap` / `weth.unwrap` |
| vault shares vs assets | ERC-4626 vaults (`kind: erc4626`): `deposit` / `withdraw` take assets, `redeem` takes shares; preview with the vault's `convertToAssets` / `convertToShares` |

## Approvals

* Never approve on its own. Approvals come from the card action: `plan` adds
  `approve(spender, exact amount)` before the call that pulls, and
  `approve(spender, 0)` at the end of the same transaction.
* Spenders are always card contracts. An approval to anything else is a red flag.
* Orders filled later (CoW, signed RFQ orders) keep their approval open until
  the fill: tell the human, and revoke it once filled or expired.

## Token quirks to tell the human about

* **Rebasing** (`kind: erc20_rebasing`, e.g. stETH): the balance grows by
  itself and transfers can arrive 1-2 wei short. Leave a margin in multi-step
  plans, or use the wrapped form (wstETH).
* **USDT** reverts when an allowance goes from non-zero to non-zero. `plan`
  always ends at 0, so the next approve works. Never keep a USDT approval open.
* **Different decimals**: USDC / USDT / EURC / PYUSD have 6, WBTC / cbBTC have 8,
  most others 18. Never copy a raw amount from one token to another.
* **Receipt tokens** (aTokens, Compound/Fluid shares, LP tokens) stand for a
  position. Moving them moves the position, so only card actions touch them.
