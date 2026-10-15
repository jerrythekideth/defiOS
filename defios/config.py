"""User configuration (defios.yaml + environment).

defios.yaml lives in the user's own agent repo, never in DeFi_os. Keys and API
tokens only come from environment variables (see .env.example).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from eth_utils import is_address, to_checksum_address

CHAINS = {"eth": 1, "mainnet": 1, "ethereum": 1, "gno": 100, "gnosis": 100, "arb1": 42161,
          "base": 8453, "oeth": 10, "optimism": 10}
SAFE_TX_SERVICE = {1: "eth", 100: "gnosis-chain", 42161: "arbitrum", 8453: "base", 10: "optimism"}


class ConfigError(ValueError):
    pass


def load_dotenv(path: Path) -> None:
    """Tiny .env loader (no python-dotenv dependency). Never overrides real env."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


@dataclass
class VaultConfig:
    name: str
    chain_id: int
    avatar: str
    roles_modifier: str
    role_key: str
    operator: str | None = None
    proposer: str | None = None
    roles_version: str = "auto"
    roles_from_block: int = 0
    protocols: list[str] | None = None
    book_vars: dict[str, str] = field(default_factory=dict)
    limits: dict = field(default_factory=dict)


@dataclass
class Config:
    root: Path
    vaults: dict[str, VaultConfig]
    catalog_dir: Path
    books_dir: Path

    def vault(self, name: str | None) -> VaultConfig:
        if name is None:
            if len(self.vaults) == 1:
                return next(iter(self.vaults.values()))
            raise ConfigError(f"several vaults configured, pick one with --vault: {sorted(self.vaults)}")
        try:
            return self.vaults[name]
        except KeyError:
            raise ConfigError(f"vault {name!r} not in defios.yaml ({sorted(self.vaults)})") from None


def _addr(v, what: str, required: bool = True) -> str | None:
    if v in (None, ""):
        if required:
            raise ConfigError(f"{what} is required")
        return None
    if not is_address(str(v)):
        raise ConfigError(f"{what}: {v!r} is not an address")
    return to_checksum_address(str(v))


def default_catalog_dir() -> Path:
    env = os.environ.get("DEFIOS_CATALOG")
    if env:
        return Path(env)
    return Path(__file__).resolve().parent.parent / "catalog"


def load_config(root: str | Path = ".", path: str | Path | None = None) -> Config:
    root = Path(root).resolve()
    load_dotenv(root / ".env")
    cfg_path = Path(path) if path else root / "defios.yaml"
    if not cfg_path.exists():
        raise ConfigError(f"{cfg_path} not found (copy defios.example.yaml from DeFi_os)")
    raw = yaml.safe_load(cfg_path.read_text()) or {}
    chain = raw.get("chain_id") or CHAINS.get(str(raw.get("chain", "eth")).lower())
    if not chain:
        raise ConfigError(f"unknown chain {raw.get('chain')!r}")
    vaults_raw = raw.get("vaults") or {}
    if not vaults_raw:
        raise ConfigError("defios.yaml has no `vaults:` section")
    vaults = {}
    for name, v in vaults_raw.items():
        v = v or {}
        vaults[name] = VaultConfig(
            name=name,
            chain_id=int(v.get("chain_id") or chain),
            avatar=_addr(v.get("avatar"), f"vaults.{name}.avatar"),
            roles_modifier=_addr(v.get("roles_modifier"), f"vaults.{name}.roles_modifier"),
            role_key=str(v.get("role_key") or ""),
            operator=_addr(v.get("operator"), f"vaults.{name}.operator", required=False),
            proposer=_addr(v.get("proposer"), f"vaults.{name}.proposer", required=False),
            roles_version=str(v.get("roles_version") or "auto"),
            roles_from_block=int(v.get("roles_from_block") or 0),
            protocols=v.get("protocols"),
            book_vars={str(k): str(x) for k, x in (v.get("book_vars") or {}).items()},
            limits=v.get("limits") or {},
        )
        if not vaults[name].role_key:
            raise ConfigError(f"vaults.{name}.role_key is required")
    catalog_dir = Path(raw["catalog"]).expanduser() if raw.get("catalog") else default_catalog_dir()
    if not catalog_dir.is_absolute():
        catalog_dir = (root / catalog_dir).resolve()
    books_dir = root / (raw.get("books_dir") or "books")
    return Config(root=root, vaults=vaults, catalog_dir=catalog_dir, books_dir=books_dir)
