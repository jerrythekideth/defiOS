---
name: defios-morpho
description: Use any Morpho vault (MetaMorpho v1 / v1.1, Vault V2) by its address, or any Morpho Blue market by its id, from a Safe vault with defios. Use when asked to deposit into / withdraw from a Morpho vault, to supply, borrow, repay or post collateral on a Morpho market, or to whitelist a Morpho vault or market in the role.
---

# defios Morpho: any vault, any market

**Read `AGENTS.md` first.** Its rules override anything here. The cards are
`catalog/morpho_vaults/card.yaml` (vaults) and `catalog/morpho_markets/card.yaml`
(Blue markets).

## You bring the vault or the market; defios brings the rest

defios does not keep a list of "good" Morpho vaults or markets. The human (or
the agent, following the human's own policy) picks one by its **vault address**
or **market id**. Choosing it is their call: curator, oracle, LLTV,
liquidity, caps, timelocks. defios knows the ABIs, the Morpho core contracts and
the exact calls. It proves onchain that the address or id is really Morpho,
and it pins exactly that instance in the role.

| you have | actions | param |
|---|---|---|
| a Morpho vault address | `morpho_vaults.deposit_any`, `withdraw_any`, `redeem_any` | `--param vault_address=0x…` |
| a Morpho Blue market id | `morpho_markets.supply`, `withdraw`, `borrow`, `repay`, `supply_collateral`, `withdraw_collateral` | `--param market_id=0x…` (bytes32) |
| a vault already in a card (kpk vaults) | `morpho_vaults.deposit_usdc` / `_weth` / `_usdt` / `_eurc`, `withdraw_*`, `redeem` | `--token <vault key>` |

## What the engine checks (it refuses otherwise)

* **Vault address**: one of the catalogued factories answers true:
  `isMetaMorpho(vault)` on the MetaMorpho v1 / v1.1 factories, or
  `isVaultV2(vault)` on the VaultV2Factory. Then `asset()` must be a
  **verified** token (`catalog/TOKENS.md`). A lookalike contract fails here.
* **Market id**: `idToMarketParams(id)` on Morpho Blue; keccak256 of the params
  must equal the id; loan and collateral tokens must be verified.
* The role pins that vault (target + approve spender) or that market (all
  five params). Permitting another vault or market adds it; the ones already
  allowed keep working.
* `propose` re-checks the vault onchain before it accepts an address that is not
  in a card.

## Flows

```bash
# whitelist a vault (Tx Builder JSON for the Vault Safe, as always)
defios permit morpho_vaults.deposit_any morpho_vaults.withdraw_any morpho_vaults.redeem_any \
  --param vault_address=0x…
# use it: amount in the vault's asset
defios plan morpho_vaults.deposit_any=1000 --param vault_address=0x…
defios plan morpho_vaults.withdraw_any=400 --param vault_address=0x…
# full exit: raw shares = vault.balanceOf(Vault)
defios plan morpho_vaults.redeem_any --param vault_address=0x… --param shares_raw=<raw shares>

# a Blue market
defios permit morpho_markets.supply morpho_markets.withdraw --param market_id=0x…
defios plan morpho_markets.supply=1000 --param market_id=0x…
```

Then simulate (`skills/defios-simulate/SKILL.md`), propose a dry run, and
`--send` only after the human says yes.

## Where ids and addresses come from

The human gives them: from the Morpho app URL (`/vault/<address>`,
`/market/<id>`), from their own research, or from a curator they trust.
Never pick one yourself because its APY is high. Before you whitelist, show
the human what the engine read: the vault's asset and which factory vouched
for it, or the market's tokens, oracle, IRM and LLTV (the `notes` of `permit`
and the `lookups` of `plan`).

## Pitfalls

* Withdraw by assets can leave dust shares; exit with `redeem_any` and the full
  share balance.
* A vault's liquidity is whatever its markets have idle: a large withdraw can
  revert on the fork. Withdraw less or wait; the simulation tells you.
* Vault V2 has gates (`receiveSharesGate`, `sendAssetsGate`…) that can refuse
  the Vault. A revert on the fork with no Roles error usually means a gate or a cap.
* Never use the Morpho app bundler or a "zap": they take arbitrary calls (see
  the cards' `ui_routes_to_avoid`).
