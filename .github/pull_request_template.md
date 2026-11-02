<!-- Adding a protocol, pool or action? Read CONTRIBUTING.md and catalog/ADDING.md first. -->

## Before

<!-- What an agent could not do, or what was wrong. -->

## After

<!-- What it can do now. Paste the new or changed lines of catalog/ACTIONS.md. -->

## Addresses

<!-- One line per new or changed record: card.name, address, the proof, and the public source (docs / verified explorer page).
     A record with no proof: say why. It stays unverified and agents will not act on it. -->

| record | address | proof | source |
|---|---|---|---|
|  |  |  |  |

## Fork run

<!-- Output of `defios simulate --batch permit.json --calls calls.json` for enter + exit:
     per-call status, Vault token deltas, allowances left open. Or "not run" and why. -->

```
```

## Checklist

- [ ] `defios catalog`: no errors
- [ ] `registry verify --write`: 0 conflict; no expected value changed to make a proof pass
- [ ] `registry export` run; `index.json`, `ACTIONS.md`, `TOKENS.md`, `registry.proofs.json` committed
- [ ] `pytest -q` green
- [ ] Every receiver / recipient / owner / onBehalfOf / to is `$AVATAR`
- [ ] Only the protocol's own contracts; UI routes through other routers listed in `ui_routes_to_avoid`
- [ ] Approves are exactly `$amount` to the contract that pulls
- [ ] `defios permit` shows no `unsafe` and no high lint
- [ ] Card stays `status: draft`, `reviewed: false`
