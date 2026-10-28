---
name: defios
description: Operate a Safe vault through a Zodiac Roles modifier with the defios CLI. Use when asked what a vault holds, which DeFi actions its role allows, whether a permission is missing or unsafe, which contract "the pool" is, to plan and queue a transaction (lend, stake, wrap, unwrap) for humans to sign, to build the Roles permission batch for an action ("add the permission to do X"), or to review a Roles permission batch. Reads state from chain; never writes calldata or addresses by hand.
---

# defios skill

DeFi_os gives you protocol cards (public facts about each protocol) and a
`defios` CLI that reads the vault's role and positions from chain.

**Before using this skill, read `AGENTS.md` at the root of the DeFi_os repo.**
Its rules override anything here.

## Setup check

```bash
defios --version          # installed? if not: pip install -e <path to DeFi_os>
defios doctor             # run from the agent repo that holds defios.yaml and .env
```

The agent repo (yours, private) holds `defios.yaml` (vault, roles modifier,
role key, operator, proposer), `.env` (RPC_URL, ETHERSCAN_API_KEY,
SAFE_API_KEY, PROPOSER_PRIVATE_KEY for a delegate key) and `books/` (generated). DeFi_os itself holds no user data.

## Answering questions

| the human asks | run | then |
|---|---|---|
| what do we hold / what can we do | `defios --format text state` | quote block number, holdings, allowed / unsafe / missing |
| is X permitted / what's missing | `defios check --protocol <card>` | per-action status; for `unsafe` name the free parameter |
| which pool / which contract is X | read `catalog/<card>/card.yaml` aliases | give the address from the card only |
| what is 0x… / is this address ours / an alert names an address | `defios --format text whois 0x… [--chain N]` | quote protocol, contract and status; act only on `verified`; `unknown` = not in the registry, never guess |
| review this permission .json | `defios decode file.json` | list each change in plain words; flag `PASS[NONE]`, unpinned receivers, delegatecall |
| something is broken | `defios doctor` | report failing checks |
| a token: address, decimals, is it real | grep `catalog/TOKENS.md`; details in `skills/defios-tokens/SKILL.md` | only the Verified section goes into a plan |
| which action does X | grep `catalog/ACTIONS.md` (card, action, input, tokens, `--param`s), then read that card's action and notes | never invent an action the list does not have |
| do X (lend, stake, wrap, …) | `defios plan <card or alias>.<action>=<amount> [more steps] [--token T]` | show each call's `line`, the warnings; if REFUSED, explain each `why` and stop |
| add the permission to do X | `defios permit <card>.<action> [--token T]` | show each `+` change with its condition and why; the JSON goes to the human for the Vault queue (never propose it); if REFUSED, explain each line and stop |
| will this work? / simulate it | `defios simulate --calls calls.json` (or `--batch file.json`, both to try a permission batch then a plan) | report each revert in plain words, the Vault's token deltas and any allowance left open; never propose something that reverted. Nothing is signed unsimulated: `skills/defios-simulate/SKILL.md` |
| rewards to claim (Merkl) | `skills/defios-merkl/SKILL.md` (API -> `merkl.claim` per token) | only verified reward tokens; claim pays the Vault |
| queue this transaction | `defios propose --file calls.json` (dry run) | show calls, nonce, SafeTxHash, warnings/problems; wait for the human's yes |
| (after the human's yes) | `defios propose --file calls.json --send` | give the Safe app link + nonce; humans sign there |

Use `--vault <name>` if `defios.yaml` lists several vaults. Add `--format json`
when you need to parse the output.

## Hard rules (summary of AGENTS.md)

1. Sync (or `state`) before answering; never answer from an old book.
2. Addresses only from cards / defios.yaml; calldata only from the engine.
3. Only the contracts a card action targets (a protocol's own router or
   gateway counts); never third-party routers, aggregators or UI routes.
4. Receivers are always the Vault.
5. Permission changes = Tx Builder JSON for the human to propose on the Vault.
6. End every answer with a short plain-language summary.
7. Transactions: `plan` → human reads the summary → `propose` dry run →
   `propose --send` only after the human agrees. Never sign as an owner, never
   execute. `calls.json` comes from `defios plan`, never from you; if `plan`
   refuses, do not work around it.
8. No open approvals: `plan` approves the exact amount and sets it back to 0
   in the same transaction. Don't use `--keep-approvals` without the human's
   OK; for orders filled later (CoW, Avant), revoke once filled.
9. Any address that did not come from `plan` goes through `defios whois`
   first; act only on `verified` records.
