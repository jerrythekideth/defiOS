# Gearbox v3: risks in plain words

* **Pool deposit receiver.** `pool.deposit(assets, receiver)` mints shares to
  `receiver`. If the role allows `deposit` without pinning `receiver` to the
  Avatar, a member can deposit the Vault's wstETH and send the shares anywhere.
  defi-kit's `gearbox/deposit` bundle pins it (`c.avatar`).
* **Credit account `calls[]`.** `openCreditAccount`, `multicall` and
  `closeCreditAccount` take an array of `(target, callData)` that the facade
  executes for the account. Pinning the account address is not enough; every
  element must be scoped. Dangerous inner calls include
  `withdrawCollateral(token, amount, to)` with `to` not the Avatar and
  `setBotPermissions` (hands control of the account to a bot).
* **Referral routes.** The Gearbox app may route through referral contracts.
  Call the pool directly.
