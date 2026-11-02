# Contributing to DeFi_os

The most useful contribution is a **protocol**: its contracts, the proofs that
those addresses are really the protocol's, and the actions an agent may run on
it. This page is the path from "I want protocol X in DeFi_os" to a merged PR.

Two ways in:

| you | do this |
|---|---|
| want a protocol, contract or action added, but won't write the card | open an issue with the **New protocol / action request** form. Give the protocol's docs, the contracts and the actions you need. Someone else writes the card |
| will write it yourself | follow the steps below and open a PR |

Everything an agent does comes from the catalog, so a PR is reviewed for
**safety first**: wrong addresses, free receivers or a route through someone
else's router can lose a vault's funds. Expect questions on every address.

## 1. Set up

```bash
git clone https://github.com/jerrythekideth/defiOS DeFi_os && cd DeFi_os
python -m venv .venv && .venv/bin/pip install -e ".[dev]"   # Python >= 3.10
.venv/bin/python -m pytest -q                               # green before you start
git checkout -b <you>/add-<protocol>
```

You need an Ethereum RPC for the proofs (`RPC_URL`; a public one works for
reads). Foundry's `anvil` on PATH is needed only for the fork run (step 5).

One branch and one PR per protocol (or per batch of markets of one protocol).
Do not mix catalog changes with engine changes.

## 2. Know the files

| file | what you write there |
|---|---|
| [`catalog/ADDING.md`](catalog/ADDING.md) | **the how-to**: new protocol, new pool / market / vault, tokens, proofs, action rules, fork run. Read it first |
| [`catalog/SCHEMA.md`](catalog/SCHEMA.md) | the card format: contracts, reads, actions, calls, arg specs, lookups |
| [`catalog/REGISTRY.md`](catalog/REGISTRY.md) | record fields: `category` (closed list), `edges`, `sources`, `proofs`, trust status |
| `catalog/<protocol>/card.yaml` | contracts that actions or reads use, plus `actions:`, `reads:`, `ui_routes_to_avoid:`, `adding:` |
| `catalog/<protocol>/registry.yaml` | other addresses of the protocol (markets, receipts, admin contracts), same fields |
| `catalog/<protocol>/NOTES.md` | optional: risks and quirks too long for `notes:` |

Copy the closest existing card: `catalog/lido` (staking), `catalog/aave_v3`
(lending market), `catalog/morpho_vaults` (ERC-4626 vaults), `catalog/cctp`
(bridge).

## 3. Write the card

The full checklist is at the top of [`catalog/ADDING.md`](catalog/ADDING.md).
The parts reviewers check hardest:

* **Addresses are checksummed and come from the chain or the protocol's own
  docs or verified source.** Never from a UI, a chat or a third-party list
  alone. An unknown address is `TODO` with a comment, never a guess.
* **Every address has a proof** (ADDING §3): the protocol's own registry or
  factory lists it, or another known contract of the protocol points at it.
  If no getter proves it, say so in the PR. The record stays `unverified`
  and agents will not act on it.
* **`sources:`** lists public links only: the docs page, the verified source on
  a block explorer, the audit.
* **Actions call only the protocol's own contracts.** No aggregator, zap,
  third-party router or referral wrapper, nothing that runs arbitrary
  calldata. The routes the official UI uses instead go in
  `ui_routes_to_avoid`.
* **Every receiver is `$AVATAR`** (`receiver`, `to`, `onBehalfOf`, `owner`,
  `recipient`, `beneficiary` …). The tests fail on a free one.
* **Signatures come from verified source**, with parameter names; otherwise
  `verified_sig: false`.
* **Approve exactly `$amount`** to the contract that pulls. The engine adds
  the reset to 0 itself.
* **`status: draft`, `reviewed: false`.** Only a maintainer flips `reviewed`,
  after checking each address.

## 4. Check it

```bash
.venv/bin/python -m defios catalog                                   # card errors
RPC_URL=<rpc> .venv/bin/python -m defios --format text registry verify --write
.venv/bin/python -m defios whois <address>                           # what an agent will see
.venv/bin/python -m defios registry export                           # index.json, ACTIONS.md, TOKENS.md
.venv/bin/python -m pytest -q
```

`registry verify` must end with **0 conflict**. Never change an expected
value to make a proof pass: a failed proof means the address or the proof is
wrong, so find out which. Commit the generated files with your card:
`catalog/registry.proofs.json`, `catalog/index.json`, `catalog/ACTIONS.md`,
`catalog/TOKENS.md`. The tests fail when they are stale.

## 5. Run the actions on a fork

ADDING §6. You need a `defios.yaml` pointing at a Safe with a Zodiac Roles
modifier (your own test Safe is fine; on a fork nothing is sent and no key is
used). Then:

```bash
defios permit <protocol>.<action> --out permit.json        # the conditions the card produces
defios plan <protocol>.<action>=<amount> [--token T] [--param k=v]
defios simulate --batch permit.json --calls calls.json     # add --tenderly for shareable links
```

Run a full cycle (enter, then exit) and paste the result in the PR: the
per-call status, the Vault's token deltas, and "no allowance left open".
If an action cannot run on a fork (a cooldown, an oracle round, a signed
order), say why in the action's `note:` and in the PR.

No Safe to test with? Say so in the PR. A maintainer will run it.

## 6. Open the PR

```bash
git add catalog/<protocol>/ catalog/index.json catalog/ACTIONS.md catalog/TOKENS.md catalog/registry.proofs.json
git commit -m "Add <protocol>: <contracts>, <actions>"
git push -u origin <you>/add-<protocol>
gh pr create --fill     # or open it on GitHub; the template fills the body
```

The [PR template](.github/pull_request_template.md) asks for:

* **Before / After:** what an agent could not do before, and what it can do now
  (the new `ACTIONS.md` lines);
* **Addresses:** each new record with its proof, or why it has none;
* **Fork run:** the output of step 5;
* **the checklist** below.

### Review checklist (what a maintainer checks)

- [ ] Each address matches the explorer's verified contract and its proof passes
- [ ] No receiver left free; no third-party router; `ui_routes_to_avoid` filled
- [ ] `defios permit` shows no `unsafe` and no high lint
- [ ] The fork run covers enter and exit, and leaves no open allowance
- [ ] Generated files are up to date; `pytest` is green
- [ ] `adding:` explains how to add the next pool or market of this protocol

After merge, a maintainer may flip `reviewed: true` in a separate PR.

## Changes to the engine

Engine changes (`defios/`) go in their own PR, with tests in `tests/`. Keep
them offline: mock RPC and HTTP the way `tests/test_tenderly.py` does. Any
change to how calls are encoded, checked or proposed needs a test that shows
a hostile variant is still rejected.
