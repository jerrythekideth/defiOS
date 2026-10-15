"""Build tests/fixtures/role_v2_logs.json from a folder of Safe Tx Builder batches.

    python tests/build_fixture.py <batch folder> \
        --replace <vault address>=0xa11ce0000000000000000000000000000000a11c \
        --replace <credit account>=0xcafe00000000000000000000000000000000cafe \
        --replace <roles modifier>=0x00000000000000000000000000000000000000f0

Each Roles write call in the batches is turned into the event the Roles v2
modifier would emit for it, encoded the way the deployed modifier emits them
(no indexed params, everything in `data`). Every vault-specific address must be
replaced with a placeholder via --replace so the fixture holds no user data
(the real addresses are passed on the command line, never stored here).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from eth_abi import encode
from eth_utils import keccak

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from defios.decode import SELECTORS  # noqa: E402
from defios.roles.v2 import EVENTS, event_signature  # noqa: E402

PLACEHOLDERS: dict[str, str] = {}  # filled from --replace
ROLES = "0x00000000000000000000000000000000000000F0"
AVATAR = "0xa11ce0000000000000000000000000000000a11c"
OPERATOR = "0x000000000000000000000000000000000000B0B0"
# Batches executed before the 14 Oct review (later folders are proposals, not chain state).
HISTORIC = {"052026", "062026", "072026"}
CALL_TO_EVENT = {
    "allowTarget": "AllowTarget", "revokeTarget": "RevokeTarget", "scopeTarget": "ScopeTarget",
    "allowFunction": "AllowFunction", "revokeFunction": "RevokeFunction", "scopeFunction": "ScopeFunction",
    "assignRoles": "AssignRoles", "setDefaultRole": "SetDefaultRole", "setAllowance": "SetAllowance",
}


def anonymize(s: str) -> str:
    low = s.lower()
    for k, v in PLACEHOLDERS.items():
        low = low.replace(k, v)
    return low


def method_calldata(tx: dict) -> str:
    """Return 0x calldata for a Tx Builder tx (encoding contractMethod ones)."""
    if tx.get("data"):
        return tx["data"]
    cm = tx["contractMethod"]
    vals = tx["contractInputsValues"]
    types = [i["type"] for i in cm["inputs"]]
    args = []
    for i in cm["inputs"]:
        v = vals[i["name"]]
        t = i["type"]
        if t.startswith("bytes") and t != "bytes":
            args.append(bytes.fromhex(v[2:]).ljust(int(t[5:]), b"\0"))
        elif t.startswith("uint"):
            args.append(int(v))
        else:
            args.append(v)
    sel = keccak(text=f"{cm['name']}({','.join(types)})")[:4]
    return "0x" + (sel + encode(types, args)).hex()


def event_log(name: str, args: list, block: int, idx: int) -> dict:
    types = [t for _, t, _ in EVENTS[name]]
    return {
        "address": ROLES.lower(),
        "blockNumber": hex(block),
        "transactionHash": "0x" + keccak(text=f"{block}:{idx}").hex(),
        "transactionIndex": "0x0",
        "logIndex": hex(idx),
        "topics": ["0x" + keccak(text=event_signature(name)).hex()],
        "data": "0x" + encode(types, args).hex(),
    }


def logs_from_batches(paths: list[Path], start_block: int = 20_000_000) -> list[dict]:
    logs = []
    block = start_block
    # setup + membership first
    logs.append(event_log("AssignRoles", [OPERATOR, [b"MANAGER_ROLE".ljust(32, b"\0")], [True]], block, 0))
    for p in paths:
        block += 1000
        batch = json.loads(p.read_text())
        for i, tx in enumerate(batch.get("transactions", [])):
            if tx.get("contractMethod") and tx["contractMethod"]["name"] == "deployModule":
                continue
            data = anonymize(method_calldata(tx))
            sel = data[:10]
            if sel not in SELECTORS:
                continue
            fn, types = SELECTORS[sel]
            from eth_abi import decode
            vals = list(decode(types, bytes.fromhex(data[10:])))
            logs.append(event_log(CALL_TO_EVENT[fn], vals, block, i))
    return logs


def review_extras(block: int) -> list[dict]:
    """Permissions seen on-chain in the 14 Oct review that are not in the batch folder
    (reconstructed, anonymized): Avant stake/cooldown and the Gearbox deposit allowFunction."""
    rk = b"MANAGER_ROLE".ljust(32, b"\0")
    av = bytes.fromhex(AVATAR[2:]).rjust(32, b"\0")
    aveth = "0x9469470C9878bf3d6d0604831d9A3A366156f7EE"
    saveth = "0xDA06eE2dACF9245Aa80072a4407deBDea0D7e341"
    minting = "0x09becF6E5e297825D19aA14eD6081A03524532D7"
    pool = "0xA9d17f6D3285208280a1Fd9B94479c62e0AABa64"
    s32 = lambda a: bytes.fromhex(a[2:]).rjust(32, b"\0")  # noqa: E731
    weth = "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"
    out = [
        ("ScopeTarget", [rk, weth]),  # WETH was scoped before the batches in the folder
        ("ScopeTarget", [rk, aveth]),
        ("ScopeFunction", [rk, aveth, bytes.fromhex("095ea7b3"),
                           [(0, 5, 5, b""), (0, 1, 16, s32(saveth)), (0, 1, 0, b"")], 0]),
        ("ScopeTarget", [rk, saveth]),
        ("ScopeFunction", [rk, saveth, bytes.fromhex("6e553f65"),
                           [(0, 5, 5, b""), (0, 1, 0, b""), (0, 1, 16, av)], 0]),
        ("AllowFunction", [rk, saveth, bytes.fromhex("9343d9e1"), 0]),  # cooldownShares
        ("ScopeTarget", [rk, minting]),
        ("AllowFunction", [rk, minting, bytes.fromhex("12345678"), 0]),  # stand-in for the dead mint()
        ("AllowFunction", [rk, pool, bytes.fromhex("6e553f65"), 0]),  # pool.deposit, any receiver
    ]
    return [event_log(n, a, block, i) for i, (n, a) in enumerate(out)]


def main() -> None:
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("--replace", action="append", default=[], help="0xreal=0xplaceholder")
    a = ap.parse_args()
    for r in a.replace:
        old, new = r.split("=")
        PLACEHOLDERS[old.lower().removeprefix("0x")] = new.lower().removeprefix("0x")
    src = Path(a.src)
    files = sorted(p for p in src.rglob("*.json")
                   if p.name != "gnosis_roles_init.json" and p.relative_to(src).parts[0] in HISTORIC)
    # chronological by folder name MMYYYY
    files.sort(key=lambda p: (p.relative_to(src).parts[0][2:] + p.relative_to(src).parts[0][:2], str(p)))
    logs = logs_from_batches(files)
    logs += review_extras(int(logs[-1]["blockNumber"], 16) + 1000)
    out = {"note": "synthetic Roles v2 logs built from anonymized permission batches; see build_fixture.py",
           "roles_modifier": ROLES, "avatar": AVATAR, "operator": OPERATOR,
           "latest_block": int(logs[-1]["blockNumber"], 16) + 10,
           "batches": len(files), "logs": logs}
    dest = Path(__file__).parent / "fixtures" / "role_v2_logs.json"
    dest.write_text(json.dumps(out, indent=1) + "\n")
    print(f"wrote {len(logs)} logs to {dest}")


if __name__ == "__main__":
    main()
