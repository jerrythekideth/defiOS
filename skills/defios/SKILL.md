---
name: defios
description: Operate a Safe vault through a Zodiac Roles modifier with the defios CLI. Use when asked what a vault holds, which DeFi actions its role allows, whether a permission is missing or unsafe, which contract "the pool" is, or to review a Roles permission batch. Reads state from chain; never writes calldata or addresses by hand.
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
SAFE_API_KEY) and `books/` (generated). DeFi_os itself holds no user data.

## Answering questions

| the human asks | run | then |
|---|---|---|
| what do we hold / what can we do | `defios --format text state` | quote block number, holdings, allowed / unsafe / missing |
| is X permitted / what's missing | `defios check --protocol <card>` | per-action status; for `unsafe` name the free parameter |
| which pool / which contract is X | read `catalog/<card>/card.yaml` aliases | give the address from the card only |
| review this permission .json | `defios decode file.json` | list each change in plain words; flag `PASS[NONE]`, unpinned receivers, delegatecall |
| something is broken | `defios doctor` | report failing checks |

Use `--vault <name>` if `defios.yaml` lists several vaults. Add `--format json`
when you need to parse the output.

## Hard rules (summary of AGENTS.md)

1. Sync (or `state`) before answering; never answer from an old book.
2. Addresses only from cards / defios.yaml; calldata only from the engine.
3. Direct path only; never UI routers or aggregators.
4. Receivers are always the Vault.
5. Permission changes = Tx Builder JSON for the human to propose on the Vault.
6. End every answer with a short plain-language summary.
