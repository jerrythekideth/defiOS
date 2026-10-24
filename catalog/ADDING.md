# Adding protocols, pools and markets

DeFi_os maps each protocol to its contract addresses (the registry) and says
what to do with them (the card's actions). Every address you add should be
provable onchain, so an agent can trust it without asking anyone.

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

## 2. A pool, market or vault of an existing protocol

Use the protocol's `adding:` field. In general:

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
local `{ ref: <curator>.<symbol> }` when an action needs the pool.

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
pytest                                           # index.json must be up to date
```

`verify` exits 3 on a conflict (a failed proof, no code, the address claimed
by two protocols) and lists tokens whose onchain symbol is missing from their
key and aliases. Commit `catalog/registry.proofs.json` and `catalog/index.json`
with the card.
