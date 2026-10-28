# The protocol registry

The catalog has two layers:

* **Registry**: what an address *is*. Every card contract with a concrete
  address is a record: chain, address, protocol, name, category, sources,
  onchain proofs, edges and a trust status.
* **Cards**: what to *do* with it (`actions:`, see `SCHEMA.md`).

Agents ask the registry first (`defios whois 0x…`). Monitoring tools and
non-Python agents load the static export `catalog/index.json`.

## Where records live

Records are contracts with a fixed address, written in either of two files
of a protocol folder:

* `catalog/<protocol>/card.yaml` `contracts:`: the hand-written contracts the
  card's actions and reads use.
* `catalog/<protocol>/registry.yaml` (optional): the rest of the protocol's
  addresses (markets, vaults, tokens, admin Safes …). Its only top-level key is
  `contracts:`, with the same fields as below. The engine merges it into the
  card, so card refs (`aave_v3.aWETH`), `whois`, `verify` and `export` see one
  map. A name defined in both files is an error, and a `registry.yaml` needs a
  `card.yaml` next to it. A protocol with addresses and no actions has a
  minimal card: `protocol`, `name`, `chain_id`, `status: draft`,
  `reviewed: false`, `adding:`, `contracts: {}`, `actions: {}`.

## Record fields (on a card contract)

```yaml
contracts:
  sUSDe:
    address: "0x9D39A5DE30e57443BfF2A8307A4256c8797A3497"
    name: "Staked USDe"          # optional: the contract's onchain name()
    category: staking            # closed list below; required
    chain_id: 1                  # optional; defaults to the card's chain_id
    kind: erc4626_cooldown       # interface hint the engine uses (open list, unchanged)
    sources:                     # optional extra sources; the card itself is always one
      - "docs: https://docs.ethena.fi/…"
    edges: { underlying: USDe }  # relations to other records (below)
    proofs:                      # onchain checks `defios registry verify` runs
      - { read: "asset() returns (address)", expect: USDe }
      - { from: aave_v3.pool, read: "getReservesList() returns (address[])", expect: { contains: $self } }
    status: retired              # only `retired` or `conflict` may be written; the rest is computed
    eoa: true                    # a key-held account (a keeper): no code is expected, code is a conflict
```

Records with `address: TODO`, `$book.*` or a read-time address are not
registry records until they have a fixed address.

### `category` (closed)

| group | values |
|---|---|
| entry points | `market`, `vault`, `staking`, `router`, `bridge`, `minter`, `queue`, `adapter`, `spender`, `escrow` |
| tokens | `token` (a protocol's own asset: stablecoin, LST, wrapper), `receipt` (share token that is not itself the vault), `debt`, `reward`, `governance` |
| dependencies | `oracle`, `price_feed`, `rate_model`, `config`, `proxy_admin`, `timelock`, `multisig`, `keeper` |

An ERC-4626 vault is `vault` (or `staking`), not `receipt`, because the
vault contract is its own share token. Shared infrastructure (Chainlink, Safe,
CCTP, Merkl) goes in its own card, not inside the protocols that use it.

### `edges`

Written from the record's point of view; exported as triples with the
relation names from the design:

| key on record R | exported edge |
|---|---|
| `underlying: X` | X `underlying_of` R |
| `receipt: X` | X `receipt_of` R |
| `debt: X` | X `debt_of` R |
| `admin: X` / `owner: X` | X `admin_of` / `owner_of` R |
| `spender: X` | X `spender_for` R |
| `priced_by: X` | R `priced_by` X |
| `reads_from: X` | R `reads_from` X |
| `bridges_to: X` | R `bridges_to` X |

`upgrades` (the EIP-1967 implementation) is never written by hand:
`registry verify` records `implementation`, `admin` and `beacon` from the
proxy slots.

### `sources`

`"<kind>: <detail>"` with kind one of `card`, `docs`, `explorer`,
`address-book`, `payload`, `audit`.

Every record already has one implicit source: the card itself. Sources are
public only; anything private stays in the operator's own repo.

### Onchain names

`registry verify` also reads each contract's `name()` and `symbol()` and stores
them with the proof results; `whois` shows them. A token or vault is keyed by
its onchain `symbol()` (or lists it in `aliases`), and its `name:` field holds
the onchain `name()`. `verify` lists every token-like record whose symbol is
missing from its key and aliases.

### `proofs`

A proof is an `eth_call` whose answer must be another record's address:

* `read`: a full signature with `returns (address)`, or `returns (address[])`
  together with `expect: { contains: … }`;
* `from`: the record to call (default `$self`), on the same chain;
* `args`: optional, card refs or literals;
* `expect`: a card ref, or `$self`.

Good proofs tie the record to something already known: the protocol's own
registry (`PoolAddressesProvider.getPool()`, `getReservesList()`), or a
self-report (`asset()`, `stETH()`, `creditManager()`). A proof catches a
typo or a swapped address, because a wrong address will not answer with the
right counterpart.

## Trust status (computed, never typed)

| status | rule | agents |
|---|---|---|
| `verified` | 2 independent source kinds, **or** 1 source + a passing proof | may act |
| `unverified` | anything else | show it, never act on it |
| `conflict` | two protocols claim the address, a proof fails, no code on that chain, or `status: conflict` | a human resolves it; nothing auto-resolves |
| `retired` | `status: retired` | history only |

`defios registry verify` runs every proof at the latest block on each chain
(`RPC_URL` for chain 1, `RPC_URL_<chain id>` for the others, e.g.
`RPC_URL_999` for HyperEVM), writes `catalog/registry.proofs.json` (results
with block numbers) and regenerates `catalog/index.json`. It exits 3 when any
record is in conflict. `defios registry export` regenerates `index.json`
offline; the tests fail when it is stale.

The card-level `status: draft` / `reviewed: false` is separate: it says a human
has not yet signed off on the card's **actions**. `whois` shows both.

## Queries

```bash
defios whois 0x9D39A5DE30e57443BfF2A8307A4256c8797A3497      # any chain
defios whois 0x28dA642f406bB5234A779Ee0143c317D4d55Fa27 --chain 999
defios registry verify [--chain 1] [--no-write]
defios registry export [--out -]
```

Not yet built: `exposure <addr>` against a vault book, and the watchlist walk
(admin Safes, oracles and dependencies reached through edges).
