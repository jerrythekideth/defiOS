"""USD-vault cards (Aave v3/v4, Ethena, Strata, infiniFi, Flying Tulip, ...): selectors, per-token actions,
bytes32 pins, and a check against a role rebuilt from one executed (anonymized) permission batch."""
import json

import pytest
from eth_abi import decode as abi_decode
from eth_utils import to_checksum_address

from defios.catalog import Catalog, CallBuilder
from defios.check import run_check
from defios.config import VaultConfig
from defios.logs import StaticLogs
from defios.plan import PlanError, Step, plan
from defios.roles.v2 import RolesV2

from .conftest import AVATAR, ROOT

BATCH = ROOT / "tests" / "fixtures" / "usd_batch_logs.json"
USD_CARDS = ["stablecoins", "ethena", "aave_v3", "aave_v4", "strata", "dialectic", "infinifi", "tokemak", "upshift",
             "noon", "re", "flying_tulip", "threejane", "cctp", "liminal"]
USDC = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"


@pytest.fixture(scope="module")
def catalog():
    return Catalog.load(ROOT / "catalog")


@pytest.fixture(scope="module")
def batch_role():
    d = json.loads(BATCH.read_text())
    adapter = RolesV2(d["roles_modifier"])
    st = adapter.read_role("MANAGER_ROLE", StaticLogs(d["logs"], d["latest_block"]))
    vault = VaultConfig("usd", 1, to_checksum_address(d["avatar"]), to_checksum_address(d["roles_modifier"]),
                        "MANAGER_ROLE", operator=to_checksum_address(d["operator"]))
    return adapter, st, vault


def test_usd_cards_present(catalog):
    assert set(USD_CARDS) <= set(catalog.cards)


def test_usd_card_selectors_match_live_permissions(catalog):
    """(target, selector) pairs from a live USD vault role: each must come out of a card signature."""
    known = catalog.known_functions({})
    must = {
        ("0x87870bca3f3fd6335c3f4ce8392d69350b4fa4e2", "0x617ba037"),  # aave v3 supply
        ("0x87870bca3f3fd6335c3f4ce8392d69350b4fa4e2", "0xa415bcad"),  # borrow
        ("0x87870bca3f3fd6335c3f4ce8392d69350b4fa4e2", "0x5a3b74b9"),  # setUserUseReserveAsCollateral
        ("0x94e7a5dcbe816e498b89ab752661904e2f56c485", "0x852a56a5"),  # aave v4 supply(reserveId,…)
        ("0x94e7a5dcbe816e498b89ab752661904e2f56c485", "0x9e35c533"),  # setUsingAsCollateral
        ("0x9d39a5de30e57443bff2a8307a4256c8797a3497", "0x9343d9e1"),  # sUSDe cooldownShares
        ("0x7a6ad3753b51cb0161405e93645b5e1f95b6e50f", "0x59fbfafd"),  # strata TrancheDepositor deposit
        ("0xfc807058a352b61aeef6a38e2d0fc3990225e772", "0xfea53be1"),  # jrNUSD redeem(token,…)
        ("0xb8431779dd9d5753b5a7f66afa3c65abfd8c3fe1", "0xcf27cfc4"),  # smDUSD lock
        ("0x3f04b65ddbd87f9ce0a2e7eb24d80e7fb87625b5", "0x2b83cccd"),  # infiniFi redeem
        ("0x39ff6d21204b919441d17bef61d19181870835a2", "0x4be47418"),  # tokemak router redeem
        ("0x39ff6d21204b919441d17bef61d19181870835a2", "0x20e8c565"),  # router deposit (dead)
        ("0xac771209ff2b71eecff6e85a9ad01db8ff2618b0", "0xa2752766"),  # upshift deposit
        ("0xe24a3dc889621612422a64e6388927901608b91d", "0xb460af94"),  # sUSN withdraw
        ("0x8aeb9453ef22cb38abc7a3af9c208f65c1bfe31e", "0xfbc3b807"),  # re redeemInstant
        ("0xaa48ecbc843cf7e9a29155d112b8cb27902bd23c", "0xa647e8ec"),  # flying tulip mint
        ("0xaa48ecbc843cf7e9a29155d112b8cb27902bd23c", "0xec00ee4a"),  # redeemExact
        ("0x68102ff5406475881462880a8da3c9bc9181ad6c", "0xa64833a0"),  # curve FT/ftUSD exchange
        ("0x2a66f992bf227d2e50ef19edd21503c3c4f3f682", "0x83df6747"),  # 3jane deposit
        ("0x28b5a0e9c621a5badaa536219b3a228c8168cf5d", "0x8e0250ee"),  # cctp depositForBurn
        ("0xaeec321a209964aabfff539807ae882917e7b4db", "0xac06a012"),  # liminal deposit
        ("0x4de413a26fc24c3fc27cc983be70aa9c5c299387", "0x0efe6a8b"),  # liquidUSD teller deposit
        ("0x0000000f2eb9f69274678c76222b35eec7588a65", "0x7d41c86e"),  # yoUSD requestRedeem
        ("0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48", "0x095ea7b3"),  # USDC approve (aave supply, $token)
    }
    assert must <= set(known), sorted(must - set(known))


def test_token_actions_build_one_call_per_token(catalog):
    b = CallBuilder(catalog, AVATAR)
    action = catalog.cards["aave_v3"].actions["supply"]
    tokens = catalog.action_tokens("aave_v3", action)
    assert len(tokens) == 8 and to_checksum_address(USDC) in tokens
    approve, supply = action["calls"]
    bc = b.build("aave_v3", "supply", 0, approve, {"token": USDC, "_tokens": tokens})
    assert bc.error is None and bc.target == USDC
    bc2 = b.build("aave_v3", "supply", 1, supply, {"token": USDC, "_tokens": tokens})
    assert {p.path: p.kind for p in bc2.pins} == {"asset": "set", "onBehalfOf": "avatar"}
    assert "no token chosen" in b.build("aave_v3", "supply", 0, approve, {}).error


def test_bytes32_avatar_pin_and_hostile_variant(catalog):
    b = CallBuilder(catalog, AVATAR)
    call = catalog.cards["cctp"].actions["bridge"]["calls"][1]
    bc = b.build("cctp", "bridge", 1, call, {"amount": 5, "domain": 19, "max_fee": 0, "finality": 2000})
    assert bc.error is None
    vals = abi_decode([p.type for p in bc.sig.params], bc.data[4:])
    assert vals[2] == bytes(12) + bytes.fromhex(AVATAR[2:])  # mintRecipient = Vault, left-padded
    pins = {p.path: p.kind for p in bc.pins}
    assert pins["mintRecipient"] == "avatar" and pins["burnToken"] == "contract"
    hv = abi_decode([p.type for p in bc.sig.params], bc.hostile[0][1][4:])
    assert hv[2][:12] == bytes(12) and hv[2][12:] != bytes.fromhex(AVATAR[2:])


def test_check_against_executed_batch(catalog, batch_role):
    adapter, st, vault = batch_role
    r = run_check(catalog, st, adapter, vault, USD_CARDS)
    acts = r["actions"]
    for proto, names in {
        "aave_v3": ["supply", "withdraw", "borrow", "repay", "repay_with_atokens"],
        "aave_v4": ["supply", "withdraw", "borrow", "repay", "enable_collateral", "update_risk_premium"],
        "flying_tulip": ["stake_stable", "unstake_stable", "sell_ft_curve", "claim_rewards"],
        "strata": ["redeem_srusde", "redeem_jrusde", "redeem_srnusd", "redeem_jrnusd"],
        "threejane": ["deposit"],
        "noon": ["request_withdraw"],
        "ethena": ["unstake"],
        "tokemak": ["redeem"],
        "infinifi": ["claim_redemption"],
    }.items():
        for a in names:
            assert acts[proto][a]["status"] == "allowed", (proto, a, acts[proto][a])
    assert set(acts["aave_v3"]["supply"]["tokens"].values()) == {"allowed"}
    unsafe = [(p, a) for p, aa in acts.items() for a, e in aa.items() if e.get("status") == "unsafe"]
    assert unsafe == []
    assert not [l for l in r["lints"] if l["severity"] == "high"]


def test_plan_token_action(catalog, batch_role):
    adapter, st, vault = batch_role
    p = plan(catalog, st, adapter, vault, [Step("aave_v3.supply", "100", "USDC")], probe=False)
    assert p["ok"], p["refused"]
    fns = [c["function"] for s in p["steps"] for c in s["calls"]]
    assert fns == ["approve(address,uint256)", "supply(address,uint256,address,uint16)", "approve(address,uint256)"]
    assert p["steps"][0]["calls"][1]["args"]["amount"] == str(100 * 10**6)
    with pytest.raises(PlanError, match="pass --token"):
        plan(catalog, st, adapter, vault, [Step("aave_v3.supply", "1")], probe=False)
    with pytest.raises(PlanError, match="takes one of"):
        plan(catalog, st, adapter, vault, [Step("aave_v3.borrow", "1", "ethena.sUSDe")], probe=False)
