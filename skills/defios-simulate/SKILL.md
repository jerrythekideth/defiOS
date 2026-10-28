---
name: defios-simulate
description: Simulate every defios transaction before anyone signs it: a plan (calls.json) on the Operator, a permission batch on the Vault, or both in order. Use before proposing, before the humans sign, when a plan or permit summary says "not simulated", when asked "will this work?", or when a transaction reverted and you need to know why.
---

# defios simulate

**Read `AGENTS.md` first.** Its rules override anything here.

**Rule: nothing gets signed unsimulated.** Every plan and every permission
batch runs on a fork first. If it cannot be simulated, say so in plain words
next to the Safe link. Never write "should work".

## What runs where

| step | how | needs |
|---|---|---|
| automatic | `defios plan` and `defios permit` run their result on a local anvil fork and refuse if anything reverts | `RPC_URL` + Foundry's `anvil` (`defios doctor` reports both) |
| by hand | `defios simulate --calls calls.json`, `--batch permit.json`, or both (the batch first, then the plan, so a new permission is tried with the call that needs it) | same |
| at signing | in the Safe app, every signer presses **Simulate** on the queued transaction (Tenderly, built into the Safe app, no key needed) | nothing |

Run `curl -L https://foundry.paradigm.xyz | bash && foundryup` to get anvil
when `doctor` warns that it is missing.

## Reading the result

* **ok per transaction.** Show any revert decoded (`InvalidProof`,
  `insufficient collateral`, `ERC20: transfer amount exceeds balance`) and
  stop. Never propose something that reverted.
* **Vault token deltas.** Check them against the intent: the amounts in and
  out, and nothing else moving. An unexpected token leaving the Vault is a stop.
* **Allowances left open.** These should be none, unless the action is
  `approvals: keep` (CoW, signed orders). Name every one.
* **Gas** per transaction.

## Limits to tell the human

* The fork is at the **current block**. Prices, caps, liquidity and Merkle
  roots can change before execution. For a transaction that waits in the queue
  for hours, re-simulate (or press Simulate in the Safe app) right before the
  last signature.
* A multi-step plan with fixed amounts can pass on the fork and still fail
  later if a step yields less (stETH rounding, slippage). Leave a margin.
* A simulation proves the transaction executes, not that it is a good idea.
  It does not replace reading the plan summary.
