"""scripts/defikit_map.py: the TS-subset evaluator and the card `defi_kit:` check (offline, committed JSON)."""
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("defikit_map", ROOT / "scripts" / "defikit_map.py")
dkm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dkm)

MAP = json.loads((ROOT / "catalog" / "defi_kit.json").read_text())


def _module(tmp_path, files: dict) -> "dkm.Module":
    for name, src in files.items():
        (tmp_path / name).write_text(src)
    dkm.Module.cache.clear()
    return dkm.Module.load(tmp_path / "schema.ts")


def test_evaluator_on_defi_kit_style_schema(tmp_path):
    m = _module(tmp_path, {
        "_info.ts": '''// generated
export default [
  { id: "0xAa00000000000000000000000000000000000001", name: "Pool A", tokens: [{ address: "0x01", symbol: "WETH" }] },
  { id: "0xaa00000000000000000000000000000000000002", name: "Pool B", tokens: [{ address: "0x02", symbol: "wstETH" },
    { address: "0x01", symbol: "WETH" }] },
] as const
''',
        "schema.ts": '''import { z } from "zod"
import pools from "./_info"
import { zx } from "../../zx"
const tokens = [...new Set(pools.flatMap((p) => p.tokens as readonly T[]).flatMap((t) => [t.address, t.symbol]))]
const zPool = z.enum([...pools.map((pool) => pool.name), ...pools.map((pool) => pool.id)] as [string, ...string[]])
const make = (native: "ETH" | "XDAI") => {
  return z.object({ sell: zx.address().or(z.literal(native)).array(), fee: z.number().int().min(0).max(10000).optional() })
}
const zAddr = z.string().regex(/^0x[a-fA-F0-9]{40}$/, "bad // not a comment")
export const eth = {
  deposit: z.object({ targets: zPool.array(), tokens: z.enum(tokens as [string, ...string[]]).array().optional(),
                      market: z.enum(["Core", "Prime"]).optional().default("Core"), to: zAddr }),
  // swap: z.object({}),
  swap: make("ETH"),
} satisfies Schemas
''',
    })
    eth = m.get("eth")
    assert set(eth) == {"deposit", "swap"}                 # commented-out action ignored
    dep = {k: v.describe() for k, v in eth["deposit"].kw["shape"].items()}
    assert dep["targets"]["type"] == "enum[]" and dep["targets"]["options_count"] == 4
    assert dep["tokens"]["options"] == ["0x01", "WETH", "0x02", "wstETH"] and dep["tokens"]["optional"]
    assert dep["market"]["default"] == "Core" and dep["to"]["type"] == "string"
    swap = {k: v.describe() for k, v in eth["swap"].kw["shape"].items()}
    assert swap["sell"]["type"] == '(address | "ETH")[]' and "options" not in swap["sell"]
    assert swap["fee"]["type"] == "int 0..10000" and swap["fee"]["optional"]


def test_committed_map_header_and_coverage():
    assert MAP["defi_kit"]["version"] and len(MAP["defi_kit"]["commit"]) == 40
    keys = {p["key"] for p in MAP["protocols"] if p["kind"] == "protocol"}
    assert {"lido", "gearbox", "morphoVaults", "cowswap", "aave_v3"} <= keys
    for p in MAP["protocols"]:
        for a in p["actions"]:
            assert not a.get("unresolved"), (p["key"], a["action"])
    gear = next(p for p in MAP["protocols"] if p["key"] == "gearbox")
    opts = {o.lower() for o in gear["actions"][0]["params"]["targets"]["options"]}
    assert "0xa9d17f6d3285208280a1fd9b94479c62e0aaba64" in opts


@pytest.mark.parametrize("ref,ok", [
    ("lido/deposit", True),
    ("gearbox/deposit targets=[0xA9d17f6D3285208280a1Fd9B94479c62e0AABa64]", True),
    ("gearbox/deposit targets=[0x0000000000000000000000000000000000000001]", False),
    ("morpho_vaults/deposit targets=[…]", False),
    ("morphoVaults/deposit targets=[<vault address | name | symbol>]", True),
    ("cowswap/swap sell=[…] feeAmountBP=0", False),
    ("cowswap/swap sell=[…] feeAmountBp=0 (note)", True),
    ("lido/stake", False),
])
def test_check_ref(ref, ok):
    assert (not dkm.check_ref(MAP, ref)) == ok


def test_cards_defi_kit_fields_match_the_map():
    bad = [c for c in dkm.check_cards(MAP, ROOT / "catalog") if not c["ok"]]
    assert bad == []
