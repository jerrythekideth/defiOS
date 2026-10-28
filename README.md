# defiOS

An agent-agnostic toolkit that lets any LLM agent operate a Safe ("Vault")
through a [Zodiac Roles](https://github.com/gnosisguild/zodiac-modifier-roles)
modifier, safely:

* **know** every protocol it may touch: protocol cards with contracts, aliases,
  exact calls, which parameters must be the Vault, reads, UI routes to avoid;
* **read** the vault's real state from chain: the role is rebuilt from the
  Roles modifier's own events on every command;
* **find** missing and unsafe permissions: every card action is encoded by the
  engine, evaluated against the decoded role, and attacked with hostile
  variants (receiver = someone else, arbitrary inner call);
* **plan** transactions: an intent (`gearbox_v3.lend --amount 1`) becomes
  engine-built calls, each checked against the role; refused if anything is
  missing or unsafe;
* **propose** them to the Operator Safe for humans to sign; later milestones:
  simulate, and produce permission batches for humans to propose.

The LLM chooses intents and parameters. The engine writes every address and
every byte. Humans sign.

> Status: **M1 read-only** (`doctor`, `sync`, `state`, `check`, `decode`) plus the
> **M2 so far**: `plan` (intent → role-checked calls.json), `propose` (dry run by
> default) and balance reads.
> All cards are `status: draft`, `reviewed: false`.

## Map for agents: where to find what

Read [`AGENTS.md`](AGENTS.md) first (rules and flows). Then:

| you need | look in |
|---|---|
| the action for a request ("supply USDC on Aave", "bridge USDC to Arbitrum") | [`catalog/ACTIONS.md`](catalog/ACTIONS.md): every card action on one page, with its input token, token list, required `--param`s and a one-line summary. Grep it, then open the card |
| the exact calls of an action, its notes, what to read | `catalog/<card>/card.yaml` → `actions:` / `reads:` |
| an address, or what an address is | `defios whois 0x…`; records live in `catalog/<card>/card.yaml` `contracts:` and `catalog/<card>/registry.yaml`; all of them in [`catalog/index.json`](catalog/index.json) |
| a token (decimals, aliases) | the card that owns it: `stablecoins`, `weth`, `lido`, `ethena`, `etherfi`, … and [`catalog/tokens`](catalog/tokens/card.yaml) for the rest (WBTC, cbBTC, LINK, …) |
| a curated vault or pool (kpkUSDC, kpkwstETH …) | the curator's card ([`catalog/kpk`](catalog/kpk)); the protocol card's actions take it with `--token` |
| the card format | [`catalog/SCHEMA.md`](catalog/SCHEMA.md) (cards, actions), [`catalog/REGISTRY.md`](catalog/REGISTRY.md) (records, proofs, categories) |
| how to add a protocol, market, vault or action | [`catalog/ADDING.md`](catalog/ADDING.md), then the card's own `adding:` field |

Naming conventions in cards:

* **One card per market** when a protocol has several: `aave_v3` (Core),
  `aave_v3_prime`, `aave_v3_etherfi`, `spark` (SparkLend + Spark savings),
  `aave_v4`. Protocol-wide contracts (governance, staking, GHO) sit in `aave`.
* **Verb names** are plain: `supply`, `withdraw`, `borrow`, `repay`, `stake`,
  `unstake`, `claim_rewards`, `deposit_<thing>`, `bridge_to_<chain>`.
  `<verb>_eth` is the native-ETH route (WETH gateway); `<verb>_other` is the
  same verb for the market's other tokens when `<verb>` holds the stablecoins.
* **`--token` picks from `tokens:`**: a reserve on a lending market, or a vault
  when the action also has `input:` (`morpho_vaults.deposit_usdc=1000 --token
  KPK_USDC_Prime`: the amount is USDC, the vault is KPK_USDC_Prime).
* **Exit-only cards** list only withdraw / repay actions (e.g. `aave_v3_etherfi`,
  whose caps are closed).

## Quickstart

```bash
git clone <this repo> DeFi_os
pip install -e DeFi_os            # Python >= 3.10
# optional, for fork simulation: Foundry's anvil on PATH (https://getfoundry.sh, or the
# foundry_stable_linux_amd64.tar.gz release binary; only `anvil` is needed)

# in YOUR agent repo (private), not in DeFi_os:
cp ../DeFi_os/defios.example.yaml defios.yaml   # fill in your addresses
cp ../DeFi_os/.env.example .env                 # RPC_URL, ETHERSCAN_API_KEY, SAFE_API_KEY
defios doctor
defios --format text state
defios --format text check
```

Point your agent at `AGENTS.md` (the manual and rules) and, for OpenClaw or
Claude Code, at `skills/defios/SKILL.md`.

Optional MCP server exposing the same functions as tools:
`pip install -e "DeFi_os[mcp]"` then `DEFIOS_ROOT=/path/to/agent-repo python -m defios.mcp`.

## Three repos

```
DeFi_os/                    public · this repo · no user data
├── AGENTS.md               the agent manual and rules
├── skills/defios/          AgentSkills-format skill (OpenClaw, Claude Code)
├── catalog/<protocol>/     card.yaml (+ NOTES.md): public protocol facts
├── defios/                 Python engine: roles/ (v2 adapter, v3 slot), sync, check, doctor, cli, mcp,
│                           plan (intent -> calls.json), tx (MultiSend + execTransactionWithRole),
│                           propose (Safe queue), balances
└── tests/                  offline tests (anonymized Roles v2 event fixture)

your-agent/                 private · one per operator/agent
├── defios.yaml · .env      vault, roles modifier, role key, operator, proposer; keys in env only
├── books/<vault>/          generated by `defios sync`; commit it so history is diffable
└── memory/ · skills/       the agent's own; one line points to DeFi_os/AGENTS.md

your-permissions/           public or private audit trail of permission batches
└── <vault>/<MMYYYY>/*.json Safe Tx Builder JSON, proposed on the Vault by a human
```

## Safety model

* **Role state is always from chain.** Roles v2 keeps conditions in packed
  bytecode with no read function, so `defios` replays the modifier's own events
  (Etherscan v2 `getLogs` or plain JSON-RPC `eth_getLogs`, chunked) up to the
  latest block on every command. `role.lock.json` is only a snapshot for diffs.
* **Live confirmation.** With `RPC_URL` and an Operator configured, `check`
  confirms each verdict by `eth_call`ing `execTransactionWithRole` from the
  Operator (shouldRevert = false) and decoding `ConditionViolation` (0xd0a9bf58).
* **Receivers pinned.** Cards mark every receiver/recipient/owner/onBehalfOf/to
  parameter as `$AVATAR`; the test suite rejects a card that leaves one free.
* **Arrays of calls** (Gearbox `calls[]`, multicalls) must be scoped element by
  element; `check` tries an arbitrary inner call and flags the action `unsafe` if
  it passes.
* **Execution options** are plain calls unless the card says otherwise (CoW's
  order signer is the one delegatecall); extra Send/DelegateCall is linted.
* **Two queues.** The agent proposes executions to the Operator Safe. Permission
  changes are Vault transactions: the agent hands the human a Tx Builder JSON.
* **Roles versions** sit behind one adapter interface (`defios/roles/base.py`);
  `v2.py` today, `v3.py` when Roles v3 ships. `doctor` detects the version.

## Commands

| command | does |
|---|---|
| `defios doctor` | Vault modules + guard, Roles version / owner / avatar / target, Operator enabled + role member, proposer is an Operator delegate, keys present |
| `defios sync` | replays the role, runs card reads, checks every action, pulls both Safe queues, writes `books/<vault>/book.yaml` and `role.lock.json` (with a diff against the previous lock) |
| `defios state` | sync, then a plain summary of holdings and allowed / missing / unsafe actions |
| `defios check` | per-action `allowed` / `missing` / `unsafe` / `needs_signature` plus role lints |
| `defios decode <batch.json>` | decodes a Tx Builder batch of Roles changes, conditions as a tree |
| `defios plan <card>.<action>[=amount] … [--token T] [--intent json]` | M2: resolves card/alias, converts amounts with token decimals, builds every call (approves included, receivers = Vault), checks each against the role (+ eth_call probe with RPC); refuses missing/unsafe (exit 3), else writes `calls.json` and prints the Operator Safe tx preview. Flow: plan → human reads → propose dry run → `--send` |
| `defios permit <card>.<action> … [--token T] [--replace]` | builds the Roles permission batch for card actions: conditions from the card, merged with the live role (existing approve spenders kept), verified on a copy (calls allowed, hostile variants rejected, nothing that works today broken, no new high lint); writes a Safe Tx Builder JSON for the Vault queue, or refuses (exit 3) |
| `defios simulate [--batch b.json] [--calls calls.json] [--block N]` | starts `anvil --fork-url $RPC_URL`, impersonates the Roles owner (batch) and the Operator (calls), sends exactly what those Safes would send; reports per-tx success or the decoded revert (Roles `ConditionViolation` names the check), gas, the Vault's token deltas from Transfer logs and every allowance still open. `plan` and `permit` do this by themselves when `RPC_URL` and `anvil` are available (`--no-simulate` skips) |
| `defios propose --file calls.json [--send]` | M2: wraps engine-built calls in `execTransactionWithRole` (MultiSend when several), picks a non-colliding Operator nonce, computes the SafeTxHash, signs as a delegate; prints everything (dry run) unless `--send` posts it to the Safe Transaction Service. Never signs as an owner, never executes |
| `defios catalog` | lists and validates cards |
| `defios whois 0x…` | what an address is, and whether it is verified (see `catalog/REGISTRY.md`) |
| `defios registry verify` / `export` | re-run onchain proofs; write `catalog/index.json` (for non-Python consumers) and `catalog/ACTIONS.md` |

Global options: `--root <agent repo>`, `--vault <name>`, `--format both|json|text`,
`--logs-file <json>` (offline replay), `--offline` (ignore `RPC_URL`).

## Cards

See [`catalog/SCHEMA.md`](catalog/SCHEMA.md); the full action list is
[`catalog/ACTIONS.md`](catalog/ACTIONS.md) and `defios catalog` lists cards.
Ethereum mainnet:

* Lending: aave_v3 (Core), aave_v3_prime, aave_v3_etherfi, aave_v4, spark,
  compound_v3, fluid, gearbox_v3, morpho_vaults.
* ETH staking and restaking: lido, rocket_pool, stader, stakewise_v3, ankr,
  etherfi (incl. Veda Liquid ETH and Liquid USD), weth.
* Stables and savings: stablecoins, sky (USDS, sUSDS, sDAI, stUSDS, rewards),
  ethena, aave (stkAAVE, sGho), avant, strata, dialectic, infinifi, tokemak,
  upshift, noon, re, flying_tulip, threejane, yo, makina, origin_arm, liminal.
* DEX, LP and yield trading: cowswap, uniswap_v3, curve, convex, balancer, balancer_v2,
  balancer_v3, aura, pendle, permit2.
* Bridges: cctp (Circle USDC).
* Tokens with no protocol card of their own: tokens.
* Curators: kpk (Gearbox v3 pools, Morpho Vault V2 vaults, their admin Safes).

Every address is checked onchain where possible (`defios registry verify`):
the protocol's own registry, or the contract reporting its counterpart
(`asset()`, `pool()`, `owner()` …). Unknown ones are `TODO`. How to add a
protocol, pool, market or action: [`catalog/ADDING.md`](catalog/ADDING.md).

## Development

```bash
pip install -e ".[dev]"
pytest
```

Tests are offline (the Safe Transaction Service and RPC are mocked). `tests/fixtures/role_v2_logs.json` is a synthetic Roles v2
event log built by `tests/build_fixture.py` from anonymized permission batches.

## License

MIT
