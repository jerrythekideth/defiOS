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
4. **Plan, then propose.** Every transaction starts with `defios plan`: it
   builds the calls from the card and checks each one against the role. If it
   refuses (`missing` / `unsafe`), stop and explain its `why` lines. Show the
   human the plan summary, then `propose` (dry run), then `--send` only after
   the human says yes. With `RPC_URL` set and Foundry's `anvil` installed,
   `plan` and `permit` also run the result on a local fork and refuse if
   anything reverts; show the human the Vault deltas and any open allowance.
   If the summary says "not simulated", run `defios simulate` before anyone
   signs, or say plainly that it was not simulated.
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
10. **No open approvals.** Approve the exact amount, act, and set the allowance
    back to 0 in the same transaction. `defios plan` does this by itself; never
    pass `--keep-approvals` without the human's OK. When the spender pulls later
    (a CoW order, a relayer filling a signed order), the approval stays open
    until the fill: tell the human, and revoke it once the order is filled or
    expired.

11. **Know the address before you touch it.** Any address you did not get from
    `defios plan` (an alert, a user's message, a UI, an explorer) goes through
    `defios whois` first. Act only on `verified` records. `unverified`: say so
    and stop. `conflict`: hand it to the human. `unknown`: it is not in the
    registry; never guess which protocol it belongs to.

## Commands

| command | use it when |
|---|---|
| `defios doctor` | setting up, or something fails: checks modules, Roles version/owner/avatar/target, role membership, Operator delegates, keys present |
| `defios sync` | before answering anything; writes `books/<vault>/book.yaml` and `role.lock.json` |
| `defios state` | "what do I hold / what can I do?" (syncs first) |
| `defios check [--protocol X]` | "is lend permitted?", "what's missing?", "is this role safe?" |
| `defios decode batch.json` | someone hands you a permission batch: decode it before the human proposes it |
| `defios catalog` | list cards and validate them |
| `defios whois 0x… [--chain N]` | "what is this address?": protocol, contract, category, trust status, proxy, edges, card actions (see rule 11) |
| `defios plan <card>.<action>=<amount> …` | M2: turn an intent into engine-built, role-checked calls; writes `calls.json` (see "Planning") |
| `defios permit <card>.<action> … [--token T]` | "add the permission to do X": builds the Roles batch from the card, merged with the live role and verified; writes a Tx Builder JSON for the Vault queue (see "Permissions") |
| `defios simulate [--batch b.json] [--calls calls.json]` | run a permission batch and/or a plan on an anvil fork: per-tx success or decoded revert, gas, Vault token deltas, allowances left open |
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
* `partial` (actions with a token list): some tokens pass, others don't; see
  `tokens: {USDC: allowed, GHO: missing, …}` and plan only with an allowed one.
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

* **"Lend 1 wstETH on gearbox"**: `sync` → `defios plan "gearbox pool.lend"
  --amount 1 --token wstETH` → show the human the plan summary (one line per
  call) → `defios propose --file calls.json` (dry run) → show nonce, SafeTxHash,
  warnings → only after the human's yes, `propose --file calls.json --send` →
  give the Operator queue link and nonce. If `plan` refuses: stop, explain,
  offer a permission batch.
* **"Add permission for X"**: `sync` → `defios permit <card>.<action> [--token T]`
  → show the human every `+` line (function, condition tree, why) and every
  `note` → they import the JSON into the Safe Transaction Builder **on the
  Vault** and sign there; you never propose on the Vault. If no card covers X,
  say so (rule 2); do not write the condition yourself.
* **After anything executes:** `sync` again; note in your journal what changed
  (`role.changes_since_last_sync` in the book).

## Permissions

`defios permit` writes the conditions from the card's arg specs: `$AVATAR`,
card contracts and literals become `EqualTo`, `one_of` / `$token` an `Or`,
`$variables` a typed `Pass`. Against the live role it:

* skips calls that are already `allowed` (`=` lines);
* adds what is missing, keeping what is there: a stored approve gets the new
  spender added to its list, never replaced;
* **refuses** (exit 3, nothing written) when the stored scope differs in some
  other way, when the role is looser than the card (`unsafe`), when a card call
  carries arbitrary calls (`calls_params`), when anything that works today
  would stop working, or when the batch adds a high lint (e.g. a free `bytes`
  param). Explain the refusal; `--replace` overwrites stored scopes and lists
  what stops working, so use it only when the human asks for exactly that.

Actions with a token list permit every listed token unless `--token` narrows
it. Optional / alternative calls need `--with-optional`. Signature and
third-party steps (Avant orders, CoW fills) are listed as notes: no role
covers them.

## Planning (M2)

The flow is **plan → (human reads the summary) → propose dry run → propose
`--send` after the human agrees**. You choose the intent; the engine writes
every byte.

```bash
defios plan gearbox_v3.lend --amount 1 --token wstETH
defios plan weth.unwrap=1 lido.stake=1 lido.wrap=0.999 kpkwstETH.lend=0.8   # one plan, steps in order
defios plan --intent '{"steps": [{"action": "gearbox_v3.lend", "amount": "1", "token": "wstETH"}]}'
```

* Steps are `<card>.<action>` or `<alias>.<action>` (`kpkwstETH.lend`,
  `"gearbox pool.lend"`), with `=<amount>` or `--amount` in the action's input
  token (`input:` in the card; `--token` must match it). Amounts are exact
  human numbers; there is no "max".
* The engine fills `$AVATAR` (the Vault) and `$book.*` (defios.yaml), converts
  amounts with the token's `decimals`, adds the approves the card lists, and
  refuses any other free variable you did not give with `--param name=value`.
* After the last step the plan sets every allowance it made back to 0
  (`approve(spender, 0)`, one per token + spender, checked against the role
  like every other call). Actions whose card says `approvals: keep` are
  skipped and the plan warns; `--keep-approvals` turns revokes off. If the
  role cannot revoke (its approve scope pins the amount), the plan refuses.
* Every call is checked against the role (live replay, `--logs-file`, or
  `books/<vault>/role.lock.json` when no log source is available — the plan
  then warns that it used a snapshot) and, with `RPC_URL`, an `eth_call` probe.
  Any `missing` / `unsafe` call refuses the **whole** plan: exit code 3,
  nothing written, a plain `why` per refused call.
* Output: the plan JSON (steps, each call with to/value/data/operation, its
  check, and a plain-language `line`), the Operator Safe tx preview
  (execTransactionWithRole, MultiSend when several calls), and `calls.json`
  (`--out`, default `calls.json` in the agent repo; `--out -` writes nothing).
* There is no separate `defios tx`: `plan` is the only producer of
  `calls.json`; `propose` picks the nonce, hashes and signs.
* Multi-step plans use fixed amounts; the fork run catches a step that comes
  up short, but only at the fork's block: if a step
  yields less than the next spends (stETH rounds 1-2 wei down; wstETH < stETH),
  the transaction reverts. Leave a margin and tell the human.

## Proposing (M2)

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
is **produced by `defios plan`** from card actions. Never write or edit calldata
in it by hand (rule 2): if `plan` cannot build the call, say so and stop.
`propose` also refuses to send to any address that is not in a card or `defios.yaml`.

## None-type Pass nodes (known Roles v2 pitfall)

A condition node `{paramType: 0 (None), operator: Pass}` takes **no calldata
slot** in the deployed Roles v2 decoder, so every later sibling is checked
against the previous parameter. Batches built with that pattern often produce
permissions that can never pass (`missing`) or that check the wrong field. Pass
nodes must carry their real type (`Static` for value types, `Dynamic` for
bytes/string, …). `defios check` models this; `defios decode` shows `PASS[NONE]`
so you can spot it in a batch before it is proposed.
