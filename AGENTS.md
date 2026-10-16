# AGENTS.md: the DeFi_os operating manual

Read this file before doing anything with a vault. It applies to any LLM agent
(OpenClaw, Claude Code, Codex, a custom bot) that runs `defios` for a human.

## Mental model

```
You (agent) --propose--> Operator Safe --execTransactionWithRole--> Roles modifier --> Vault Safe --> protocol
                              ^ humans sign here                        ^ checks every call against the role
```

* **Vault** (a.k.a. Avatar): the Safe that holds the funds. Owns the Roles modifier.
* **Roles modifier** (Zodiac Roles v2 today): lets members of a role make the
  Vault call only what the role allows, down to individual parameters.
* **Operator**: a Safe that is a member of the role. You may be a *delegate*
  on it: you can queue transactions there; humans sign them.
* **Permission changes** are Vault transactions. You cannot make them. You hand
  the human a Safe Transaction Builder JSON; they propose it on the Vault.

## The three files you rely on

| file | where | what it answers |
|---|---|---|
| `catalog/<protocol>/card.yaml` | DeFi_os (public) | Which contract is "the gearbox pool"? Which exact calls make up "lend"? Which params must be the Vault? What to read? Which UI routes to avoid? |
| `books/<vault>/book.yaml` | your agent repo, generated | What does the vault use and hold? Which actions are allowed / missing / unsafe right now? What is queued? |
| `books/<vault>/role.lock.json` | your agent repo, generated | The full decoded role at a block. A snapshot for diffs. Never trust it on its own. |

## Rules (non-negotiable)

1. **Sync before answering.** Any question about holdings, permissions or the
   queue starts with `defios sync` (or `defios state`, which syncs). The role is
   always re-read from chain; the book is never "probably still right".
2. **No hand-written addresses or calldata.** Every address comes from a card
   (or `defios.yaml` for vault-specific instances). Every byte of calldata is
   encoded by the engine. If a card does not cover what the human wants, say so;
   do not improvise an address, a selector or an ABI.
3. **Only card actions, only the direct path.** Call the permitted contract
   directly. Never route through a UI router, aggregator, zap or referral
   contract (see each card's `ui_routes_to_avoid`). Never use contracts that run
   opaque bytecode (e.g. the Enso router).
4. **Check, then simulate, then propose.** Run `defios check` for the action
   first. If it says `missing` or `unsafe`, stop and explain. (M2: `simulate`
   must pass and balance deltas must match the plan before `propose`.)
5. **Receivers are the Vault.** Every receiver / recipient / owner /
   onBehalfOf / to parameter is the Vault. No exceptions without the human's
   explicit, written OK in the conversation.
6. **Permission changes come out as Tx Builder JSON** for the human to import
   in the Safe Transaction Builder and propose on the Vault. Summarize every
   change in plain language next to the file. Never present a permission batch
   as already applied.
7. **Plain-language summary every time.** After any command, tell the human
   in 1–5 short lines what you found or did: block number, what is allowed,
   what is missing or unsafe and why, what you need from them.
8. **Never print secrets.** Keys live in env vars. `defios doctor` reports
   only whether they are set.
9. **Unreviewed cards are drafts.** Cards marked `reviewed: false` are good
   enough to read state and spot gaps; mention it when you rely on one for a
   transaction.

## Commands (M1 read-only, plus M2 `propose`)

| command | use it when |
|---|---|
| `defios doctor` | setting up, or something fails: checks modules, Roles version/owner/avatar/target, role membership, Operator delegates, keys present |
| `defios sync` | before answering anything; writes `books/<vault>/book.yaml` and `role.lock.json` |
| `defios state` | "what do I hold / what can I do?" (syncs first) |
| `defios check [--protocol X]` | "is lend permitted?", "what's missing?", "is this role safe?" |
| `defios decode batch.json` | someone hands you a permission batch: decode it before the human proposes it |
| `defios catalog` | list cards and validate them |
| `defios propose --file calls.json` | M2: dry run of an Operator proposal (see "Proposing"); `--send` only after the human said yes |

All commands print JSON on stdout and a short summary on stderr
(`--format text` for the summary only). Run them from your agent repo, or pass
`--root path/to/agent-repo`. Pass `--vault <name>` when several vaults are configured.

### Reading `check`

* `allowed`: the exact call passes the role, and hostile variants (receiver =
  someone else, arbitrary inner call) are rejected.
* `unsafe`: the call passes, **and** so does a hostile variant. Tell the human
  which parameter is free. Do not use the action until it is fixed (unless the
  human explicitly accepts the risk for this one transaction).
* `missing`: the call does not pass. Often a missing approve, or a permission
  whose conditions point at the wrong parameter (see "None-type Pass nodes").
* `needs_signature`: calls are allowed but the action also needs an off-chain
  order signed by the Vault owners (RFQ protocols like Avant).
* `unresolved`: the card needs an address that is still `TODO` or a
  `book_vars` entry that is not set in `defios.yaml`.
* `loose` on a call: a token-set restriction from the card is not enforced by
  the role; not a theft path, but worth tightening.
* `source: eth_call` means the verdict was confirmed against the live modifier
  from the Operator (`RPC_URL` set); `offline` means the engine's own evaluator.

### Lints to raise with the human

`unpinned_receiver`, `unscoped_calls`, `approve_any_spender`,
`wildcard_target`, `delegatecall_not_needed`, `operator_not_member` are high:
report them in your summary. `known_dead` and `uncatalogued` are clean-up items.

## Flows

* **"Lend 1 wstETH on gearbox"** (M2 for the proposing part): `sync` →
  resolve "gearbox" to a card contract via aliases → `check --protocol gearbox_v3`
  → if `allowed`: plan → tx → simulate → propose, then tell the human the
  Operator queue link and nonce. If `unsafe`/`missing`: stop, explain, offer a
  permission batch.
* **"Add permission for X"** (M3): build the batch (defi-kit bundle or
  `kit/` module), diff it against the live role, lint it, write the Tx Builder
  JSON into the human's permissions folder, summarize. The human proposes it.
* **After anything executes:** `sync` again; note in your journal what changed
  (`role.changes_since_last_sync` in the book).

## Proposing (M2, first part)

`defios propose --file calls.json` turns inner calls into one Operator Safe
transaction: a single call goes straight into
`Roles.execTransactionWithRole(to, value, data, operation, roleKey, shouldRevert=true)`;
several calls are packed into Safe's MultiSendCallOnly 1.4.1 first (the role
then needs the MultiSend unwrapper; `--batch operator` batches on the Operator
side instead). It picks the Operator nonce as max(on-chain nonce, highest queued
nonce + 1) so it never lands on top of a transaction the humans have not signed
yet, computes the SafeTxHash and signs it with `PROPOSER_PRIVATE_KEY` **as a
delegate**.

1. **Dry run first, always.** Without `--send` nothing is posted. Show the
   human the summary: each call (card contract + function), value, batch mode,
   nonce and why, SafeTxHash, warnings and problems.
2. **Only then `--send`**, and only if `problems` is empty (the CLI refuses
   otherwise: uncatalogued target, Roles pre-flight failed, key is an owner,
   key is not a delegate, service unreachable).
3. **Hand over the link** (`links.tx`) and the nonce. The humans review and sign
   in the Safe app. You never confirm, never execute, never sign with an owner key.
4. After it executes: `defios sync`.

`calls.json` (`{"description", "chain_id", "calls": [{to, value, data, operation}]}`)
is **produced by the engine from card actions** (planned M2 `defios tx --action
<card>.<action> --param …`). Until that lands, do not write calldata into it by
hand (rule 2): if no engine command builds the call, say so and stop. `propose`
also refuses to send to any address that is not in a card or `defios.yaml`.

## None-type Pass nodes (known Roles v2 pitfall)

A condition node `{paramType: 0 (None), operator: Pass}` takes **no calldata
slot** in the deployed Roles v2 decoder, so every later sibling is checked
against the previous parameter. Batches built with that pattern often produce
permissions that can never pass (`missing`) or that check the wrong field. Pass
nodes must carry their real type (`Static` for value types, `Dynamic` for
bytes/string, …). `defios check` models this; `defios decode` shows `PASS[NONE]`
so you can spot it in a batch before it is proposed.
