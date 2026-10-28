---
name: defios-merkl
description: Find and claim Merkl rewards for a Safe vault with defios. Use when asked "do we have rewards to claim?", "claim our MORPHO / Merkl rewards", or when a vault earns incentives distributed through Merkl.
---

# defios merkl rewards

**Read `AGENTS.md` first.** Its rules override anything here. The card is
`catalog/merkl/card.yaml`.

## How Merkl pays

One Distributor contract per chain holds every Merkl reward. Each few hours a
new Merkle root is posted. A claim gives, per token, the Vault's **cumulative**
amount ever earned and a Merkle proof. The contract pays the difference with
what was already claimed. There is no "claim everything" call without
arguments: each token needs its own amount and proof.

## Steps

1. **List what is claimable** (read-only API, no key):
   `GET https://api.merkl.xyz/v4/users/<Vault>/rewards?chainId=1`
   For each `rewards[]` entry, `amount - claimed` is what a claim pays now
   (raw units; use `token.decimals`). Report each token with a non-zero
   difference. Skip dust unless the human asks.
2. **Check each token** with `defios whois <token.address>`. Only `verified`
   tokens can be claimed through `plan` (`skills/defios-tokens/SKILL.md`). For
   any other token, tell the human and stop.
3. **Plan the claim** with an intent, one step per token, with values copied
   from the API response, never typed:
   ```bash
   defios plan --intent '{"steps": [{"action": "merkl.claim", "params": {
     "reward_token": "<token.address>", "claimable": "<amount>", "proof": ["0x…", "0x…"]}}]}'
   ```
   `claimable` is the cumulative `amount`, not `amount - claimed`.
4. Simulate (`skills/defios-simulate/SKILL.md`): the Vault delta must equal
   `amount - claimed` of that token. Then propose, dry run first, as usual.

## The permission

`defios permit merkl.claim` pins `users` to `[Vault]` (one element) and leaves
`tokens`, `amounts` and `proofs` free. That is safe: `claim` only pays the
user. One permission covers every reward token.

## Pitfalls

* `InvalidProof` on simulate or execution means the root moved since the
  proof was fetched. Fetch again, plan again.
* If `claimRecipient(Vault, token)` on the distributor is not the zero
  address, rewards go there and the simulated Vault delta is 0. Tell the human. The role never allows
  `setClaimRecipient`.
* Never use `claimWithRecipient`, `toggleOperator` or the Merkl app's "claim
  and swap": they send rewards elsewhere or run callbacks.
* Swapping the reward afterwards goes through CoW (`cowswap` card), as for
  any other token.
