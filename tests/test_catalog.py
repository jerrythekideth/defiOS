import json
import re

import pytest
from eth_utils import is_address, to_checksum_address

from defios.catalog import Catalog, CallBuilder, parse_signature, validate_card

from .conftest import AVATAR, CREDIT_ACCOUNT, ROOT

EXPECTED = {"lido", "gearbox_v3", "morpho_vaults", "cowswap", "weth", "avant", "origin_arm", "etherfi",
            "yo", "makina"}


@pytest.fixture(scope="module")
def catalog():
    return Catalog.load(ROOT / "catalog")


def test_all_cards_present_and_valid(catalog):
    assert EXPECTED <= set(catalog.cards)
    for card in catalog.cards.values():
        assert validate_card(catalog, card) == [], card.protocol


def test_reviewed_cards_have_only_verified_records(catalog):
    """`reviewed: true` means a maintainer checked every address: none may be left unverified."""
    idx = json.loads((ROOT / "catalog" / "index.json").read_text())
    loose = {(r["protocol"], r["ref"]) for rs in idx["deployments"].values() for r in rs if r["status"] != "verified"}
    for proto, card in catalog.cards.items():
        if card.raw["reviewed"]:
            assert not [ref for p, ref in loose if p == proto], proto


def test_addresses_are_checksummed_or_todo(catalog):
    for card in catalog.cards.values():
        text = card.path.read_text() + (card.registry_path.read_text() if card.registry_path else "")
        for a in re.findall(r"0x[0-9a-fA-F]{40}", text):
            assert a == to_checksum_address(a), (card.protocol, a)


def test_signature_parser():
    s = parse_signature("multicall(address creditAccount, (address target, bytes callData)[] calls)")
    assert s.canonical == "multicall(address,(address,bytes)[])"
    assert s.selector == "0xebe4107c"
    s2 = parse_signature("cooldowns(address account) returns (uint104 cooldownEnd, uint152 underlyingAmount)")
    assert [o.type for o in s2.outputs] == ["uint104", "uint152"]


def test_card_selectors_match_live_permissions(catalog):
    """Selectors that appear in the (anonymized) live role must be produced by the cards' signatures."""
    known = catalog.known_functions({"gearbox_v3.credit_account": CREDIT_ACCOUNT})
    must = {
        ("0xa9d17f6d3285208280a1fd9b94479c62e0aaba64", "0xb3d45433"),  # depositWithReferral
        ("0xa9d17f6d3285208280a1fd9b94479c62e0aaba64", "0xba087652"),  # redeem
        ("0x9515ab9bb73a9642f1a93ba7c2790e9d08227f9a", "0x92beab1d"),  # openCreditAccount
        ("0x23da9ade38e4477b23770ded512fd37b12381fab", "0x569d3489"),  # signOrder
        ("0xf1eee0957267b1a474323ff9cff7719e964969fa", "0x82b78ba7"),  # yo gateway deposit
        ("0x3a43aec53490cb9fa922847385d82fe25d0e9de7", "0x7d41c86e"),  # yoETH requestRedeem
        ("0x0d2df071207e18ca8638b4f04e98c53155ec2ce0", "0x6bb3b476"),  # requestOnChainWithdraw
        ("0x0d2df071207e18ca8638b4f04e98c53155ec2ce0", "0x0bf6cab7"),  # rescueTokens (dead)
        ("0x85b78aca6deae198fbf201c82daf6ca21942acc6", "0xd04bf024"),  # claimLidoWithdrawals (dead)
        ("0xda06ee2dacf9245aa80072a4407debdea0d7e341", "0xf2888dbb"),  # savETH unstake
    }
    assert must <= set(known)


def test_call_builder_pins_and_hostile_variants(catalog):
    b = CallBuilder(catalog, AVATAR, {"gearbox_v3.credit_account": CREDIT_ACCOUNT})
    lend = catalog.cards["gearbox_v3"].actions["lend"]["calls"][1]
    bc = b.build("gearbox_v3", "lend", 1, lend)
    assert bc.error is None
    assert [p.path for p in bc.pins] == ["receiver"]
    assert len(bc.hostile) == 1 and bc.hostile[0][2] == "unsafe"
    mc = catalog.cards["gearbox_v3"].actions["credit_account_multicall"]["calls"][0]
    bc2 = b.build("gearbox_v3", "credit_account_multicall", 0, mc)
    assert bc2.pins[0].expected == [to_checksum_address(CREDIT_ACCOUNT)]
    assert any("calls[]" in h[0] for h in bc2.hostile)
    cow = catalog.cards["cowswap"].actions["swap"]["calls"][1]
    bc3 = b.build("cowswap", "swap", 1, cow)
    assert bc3.operation == 1 and [p.path for p in bc3.pins] == ["order.receiver"]


def test_missing_book_var_is_reported(catalog):
    b = CallBuilder(catalog, AVATAR, {})
    mc = catalog.cards["gearbox_v3"].actions["credit_account_multicall"]["calls"][0]
    assert "book variable" in b.build("gearbox_v3", "x", 0, mc).error


def test_repo_holds_no_user_config():
    """DeFi_os ships placeholders only: the example config must not carry real-looking addresses."""
    import yaml

    ex = yaml.safe_load((ROOT / "defios.example.yaml").read_text())
    for v in ex["vaults"].values():
        for k in ("avatar", "roles_modifier", "operator", "proposer"):
            a = v[k]
            assert is_address(a) and int(a, 16) < 2**16, (k, a)


TOKEN_CATEGORIES = {"token", "receipt", "debt", "reward", "governance"}


def test_every_token_record_has_decimals(catalog):
    """A token record without `decimals` is left out of TOKENS.md, so agents grep for it, miss it and
    call a catalogued token unknown. Vault shares (erc4626*) are tokens too."""
    missing = []
    for card in catalog.cards.values():
        for name, c in card.contracts.items():
            if not isinstance(c, dict) or "ref" in c or not str(c.get("address", "")).startswith("0x"):
                continue
            kind = str(c.get("kind", ""))
            tokenish = c.get("category") in TOKEN_CATEGORIES or kind.startswith(("erc20", "erc4626", "atoken"))
            if tokenish and "decimals" not in c and kind != "merkl_distributor":
                missing.append(f"{card.protocol}.{name}")
    assert not missing, f"add `decimals:` (read decimals() onchain) to {missing}"
