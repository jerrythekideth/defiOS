# Adding protocols, pools, markets and actions

DeFi_os maps each protocol to its contract addresses (the registry) and says
what to do with them (the card's actions). Every address you add should be
provable onchain, so an agent can trust it without asking anyone.

Checklist for a new protocol, end to end:

1. Card with its core contracts and their proofs (§1, §3).
2. Tokens it takes or returns: reuse a record if one exists (`defios whois`),
   else add one (§2).
3. Actions for the main verbs (§5), each run on a fork (§6).
4. `adding:` text so the next person (or model) can add a pool or market.
5. `defios registry verify`, `defios registry export`, `pytest` (§4).

## 1. A new protocol

1. Create `catalog/<protocol>/card.yaml` with `protocol`, `name`, `chain_id`,
   `status: draft`, `reviewed: false` and `sources` (see `SCHEMA.md`).
2. Add its **core contracts** first: the entry points (pool, router, vault,
   staking, minter, bridge) and the protocol's own registry, if it has one
   (a factory, an addresses provider, a contracts register). That registry is
   what proves everything else.
3. Give every contract a `category` (closed list in `REGISTRY.md`), `kind`
   (the interface, e.g. `erc4626`) and `aliases` (what people call it).
4. Write the `adding:` field: where this protocol's instances live and which
   onchain read proves a new one.
5. Add actions only for what you want an agent to do (see `SCHEMA.md`).
   A protocol with addresses and no actions is fine: `whois` can still name it.

Addresses that no action or read uses go in `catalog/<protocol>/registry.yaml`
(a top-level `contracts:` map, same fields), so `card.yaml` stays short and
hand-written. See `REGISTRY.md`.

## 2. A pool, market or vault of an existing protocol

Use the protocol's `adding:` field and write the record in its
`registry.yaml`. In general:

| field | value |
|---|---|
| key | the onchain `symbol()` (e.g. `kpkwstETH`, `sUSDe`) |
| `name` | the onchain `name()` |
| `category` | `vault` / `market` / `staking` / `receipt` … |
| `kind` | `erc4626` for share vaults, otherwise the interface |
| `decimals` | the onchain `decimals()` |
| `edges` | `underlying: <asset>` at least; `owner` / `admin` for its admin Safes |
| `proofs` | at least one: see below |

**Curated vaults** (Morpho vaults, Gearbox pools run by a curator, Veda
vaults …) go in the **curator's card** (for example `catalog/kpk`), not in the
protocol card. The protocol card keeps its core contracts and actions, plus a
local `{ ref: <curator>.<symbol> }` when an action needs the pool. To make a new
vault usable, add its key to the `tokens:` list of the matching action (e.g.
`morpho_vaults.deposit_usdc`).

**Morpho Blue markets** are not added to a card. Whitelist one by its id:
`defios permit morpho_markets.supply morpho_markets.withdraw --param market_id=0x…`.
The engine reads the market params onchain, checks them against the id, and
refuses unless both tokens are verified catalog tokens (add a missing token
first, below). Check the oracle, LLTV and liquidity before you whitelist: the
role fixes which market, not whether it is a good one.

**Tokens.** A token belongs to the card of the protocol that issues it (stETH
in `lido`, sUSDe in `ethena`, GHO in `stablecoins`). A token with no card of its
own (WBTC, cbBTC, LINK …) goes in `catalog/tokens`, proved by a market that
lists it (`getReservesList()` of an Aave pool, a Curve pool's `coins(i)` …).
Another card uses it through a local `{ ref: tokens.WBTC }` so `--token WBTC`
resolves there.

**A market of a lending protocol** (a new Aave-style pool, a new SparkLend-like
fork) gets its own card (`aave_v3_prime`, `aave_v3_etherfi`) that refs the pool,
gateway and receipt tokens and copies the action set of the closest existing
card. Fill each `tokens:` list from the live reserve flags, not from docs:
supply / enable_collateral only for active, unfrozen, unpaused reserves with
room under the supply cap; withdraw / disable_collateral for every listed
reserve (frozen ones too, so positions can exit); borrow only where borrowing
is enabled; repay for every borrowable reserve.

## 3. Proofs that work

| proof | example | what it shows |
|---|---|---|
| the protocol's registry lists it | `{ from: aave_v3.pool, read: "getReservesList() returns (address[])", expect: { contains: $self } }` | the protocol itself recognises the address |
| the protocol points at it | `{ from: gearbox_v3.credit_manager, read: "pool() returns (address)", expect: $self }` | another known contract uses it |
| it reports its counterpart | `{ read: "asset() returns (address)", expect: weth.WETH }` | a typo or a lookalike would not answer this |
| its admin | `{ from: <vault>, read: "owner() returns (address)", expect: $self }` on the Safe's record | who can change the vault |

Prefer the first two: they tie the address to the protocol, not just to itself.

## 4. Check it

```bash
defios catalog                                   # card errors
RPC_URL=… defios registry verify                 # proofs, onchain name()/symbol(), proxy slots
defios whois <address>                           # what an agent will see
defios registry export                           # index.json + ACTIONS.md
pytest                                           # both must be up to date
```

`verify` exits 3 on a conflict (a failed proof, no code, the address claimed
by two protocols) and lists tokens whose onchain symbol is missing from their
key and aliases. Commit `catalog/registry.proofs.json`, `catalog/index.json` and
`catalog/ACTIONS.md` with the card.

## 5. Actions

An action is the exact call sequence for one verb, sent by the Vault. Write it
in the card's `actions:` (format in `SCHEMA.md`). Rules:

* **One verb per action**, named plainly (`supply`, `withdraw`, `stake`,
  `claim_rewards`, `bridge_to_arbitrum`). Extend a family with a suffix
  (`supply_other`, `supply_eth`) rather than changing an action that is
  already in use: a vault's role was built from it.
* **The protocol's own contracts only**: its pool, vault, staking or bridge
  contract, or its own router / gateway / teller when that is how the protocol
  is entered. Record such a router in the protocol's card (`category: router`)
  with a proof that ties it to the protocol (it reads the vault, or the vault
  lists it). Never a third-party router, aggregator, zap or referral wrapper,
  and never a contract that runs arbitrary calldata. List the routes the UI
  uses instead in `ui_routes_to_avoid`.
* **Signatures from verified source**, with parameter names. If you could not
  confirm one, set `verified_sig: false`.
* **Every receiver is `$AVATAR`** (`receiver`, `to`, `onBehalfOf`, `owner`,
  `recipient`, `mintRecipient` …). The tests reject a free one.
* **Pin what you can**: referral codes, rate modes, a pool address that a
  gateway takes as an argument, a chain or domain id (one action per
  destination is better than a free `$domain`). Leave free only what the agent
  must choose: `$amount`, `$token`, a minimum-out, a reward token. A free
  variable without `defaults:` must be passed with `--param`, and appears in
  the `--param` column of `ACTIONS.md`.
* **Approve first, exactly `$amount`**, to the contract that pulls. `plan`
  appends the `approve(spender, 0)` itself. Use `approvals: keep` only when the
  spender pulls after the transaction.
* **`input:`** is the token the amount is written in; **`tokens:`** is the set
  `--token` picks from (the reserve, or the vault when `input:` is set). Every
  token needs `decimals`.
* **Amounts are exact**: say in a `note:` when `type(uint256).max` means "all"
  and what the agent should read first (a cooldown, a claimable balance).
* Add `reads:` for what an agent must check before acting (health factor,
  cooldown end, claimable rewards).

## 6. Prove an action works

1. `defios catalog`: no card errors.
2. `defios check --protocol <card>` against a vault, or `defios permit
   <card>.<action>` to see the conditions the card produces: no `unsafe`, no
   high lint.
3. Run it on a fork. With `RPC_URL` set and Foundry's `anvil` on PATH, `defios
   permit` and `defios plan` simulate by themselves; `defios simulate --batch
   permit.json --calls calls.json` runs the permission batch and the plan
   together and prints each call's result, the Vault's token deltas and any
   allowance left open. Run a full cycle: enter, then exit (supply then
   withdraw, stake then unstake, deposit then redeem).
4. When an action cannot run on a fork (a cooldown, an oracle round, a signed
   order, a capped market), say why in its `note:`.
