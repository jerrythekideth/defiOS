"""check against the anonymized fixture role must reproduce the 14 Oct review gaps."""
import pytest

from defios.catalog import Catalog
from defios.check import run_check
from defios.config import VaultConfig

from .conftest import AVATAR, CREDIT_ACCOUNT, OPERATOR, ROLES, ROOT


@pytest.fixture(scope="module")
def result(role_state):
    adapter, st = role_state
    vault = VaultConfig(name="t", chain_id=1, avatar=AVATAR, roles_modifier=ROLES, role_key="MANAGER_ROLE",
                        operator=OPERATOR, book_vars={"gearbox_v3.credit_account": CREDIT_ACCOUNT,
                                                       "cowswap.sell_token": "weth.WETH"})
    return run_check(Catalog.load(ROOT / "catalog"), st, adapter, vault)


def status(result, proto, action):
    return result["actions"][proto][action]["status"]


def lint_codes(result, code):
    return [l for l in result["lints"] if l["code"] == code]


def test_detects_used_protocols(result):
    assert {"gearbox_v3", "avant", "lido", "etherfi", "origin_arm", "yo"} <= set(result["protocols"])


def test_gearbox_pool_deposit_any_receiver_is_unsafe(result):
    assert status(result, "gearbox_v3", "lend") == "unsafe"
    dep = [c for c in result["actions"]["gearbox_v3"]["lend"]["calls"] if c["selector"] == "0x6e553f65"][0]
    assert dep["status"] == "unsafe" and "receiver" in dep["reason"]
    assert any(l.get("function") == "deposit(uint256,address)" for l in lint_codes(result, "unpinned_receiver"))


def test_gearbox_credit_account_calls_unscoped(result):
    for a in ("open_credit_account", "credit_account_multicall", "close_credit_account"):
        assert status(result, "gearbox_v3", a) == "unsafe"
    assert len(lint_codes(result, "unscoped_calls")) == 3


def test_avant(result):
    assert status(result, "avant", "stake") == "allowed"
    assert status(result, "avant", "cooldown") == "allowed"  # cooldownAssets is optional
    assert status(result, "avant", "unstake") == "allowed"
    redeem = result["actions"]["avant"]["redeem"]
    assert redeem["status"] == "missing"  # avETH.approve(minting) not in the role
    assert redeem["calls"][0]["call"] == "avETH.approve"


def test_none_pass_scopes_show_as_missing(result):
    # dead scopes from the paramType-None Pass bug
    assert status(result, "gearbox_v3", "withdraw") == "missing"
    assert status(result, "lido", "request_withdrawal") == "missing"


def test_role_lints(result):
    assert any(l.get("target_name") == "lido.stETH" for l in lint_codes(result, "delegatecall_not_needed"))
    dead = lint_codes(result, "known_dead")
    assert any("rescueTokens" in (l.get("function") or "") for l in dead)
    assert any(l.get("spender", "").lower().startswith("0x5dbf760b") for l in
               lint_codes(result, "approve_uncatalogued_spender"))
    assert lint_codes(result, "uncatalogued")  # the stand-in Avant mint selector
