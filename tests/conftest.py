import json
import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "role_v2_logs.json"

AVATAR = "0xa11ce0000000000000000000000000000000a11c"
ROLES = "0x00000000000000000000000000000000000000F0"
OPERATOR = "0x000000000000000000000000000000000000b0b0"
CREDIT_ACCOUNT = "0xcafe00000000000000000000000000000000cafe"


@pytest.fixture(autouse=True)
def _no_network_env(monkeypatch):
    for k in ("RPC_URL", "ETHERSCAN_API_KEY", "SAFE_API_KEY", "LOG_SOURCE"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("DEFIOS_CATALOG", str(ROOT / "catalog"))


@pytest.fixture(scope="session")
def fixture_logs():
    return json.loads(FIXTURE.read_text())


@pytest.fixture
def agent_dir(tmp_path):
    (tmp_path / "defios.yaml").write_text(f"""
chain: eth
vaults:
  testvault:
    avatar: "{AVATAR}"
    roles_modifier: "{ROLES}"
    roles_version: v2
    role_key: MANAGER_ROLE
    operator: "{OPERATOR}"
    book_vars:
      gearbox_v3.credit_account: "{CREDIT_ACCOUNT}"
      cowswap.sell_token: weth.WETH
      cowswap.buy_token: makina.DETH
""")
    return tmp_path


@pytest.fixture(scope="session")
def role_state(fixture_logs):
    from defios.logs import StaticLogs
    from defios.roles.v2 import RolesV2

    adapter = RolesV2(ROLES)
    src = StaticLogs(fixture_logs["logs"], fixture_logs["latest_block"])
    return adapter, adapter.read_role("MANAGER_ROLE", src)


os.environ.setdefault("DEFIOS_CATALOG", str(ROOT / "catalog"))
