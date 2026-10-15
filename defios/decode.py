"""Decode Safe Transaction Builder batches of Roles v2 permission changes.

Use it to review a batch before proposing it on the Vault (e.g. a .json a
teammate hands you because they are not a proposer there).
"""
from __future__ import annotations

from typing import Any

from eth_abi import decode as abi_decode
from eth_utils import keccak, to_checksum_address

from .roles.conditions import ExecutionOptions, render, unflatten
from .roles.v2 import CONDITION_FLAT, role_key_label

WRITE_FUNCTIONS = {
    "allowTarget": ["bytes32", "address", "uint8"],
    "revokeTarget": ["bytes32", "address"],
    "scopeTarget": ["bytes32", "address"],
    "allowFunction": ["bytes32", "address", "bytes4", "uint8"],
    "revokeFunction": ["bytes32", "address", "bytes4"],
    "scopeFunction": ["bytes32", "address", "bytes4", CONDITION_FLAT, "uint8"],
    "assignRoles": ["address", "bytes32[]", "bool[]"],
    "setDefaultRole": ["address", "bytes32"],
    "setAllowance": ["bytes32", "uint128", "uint128", "uint128", "uint64", "uint64"],
    "setTransactionUnwrapper": ["address", "bytes4", "address"],
}
SELECTORS = {
    "0x" + keccak(text=f"{n}({','.join(t)})")[:4].hex(): (n, t) for n, t in WRITE_FUNCTIONS.items()
}


def _fmt(v: Any) -> Any:
    if isinstance(v, bytes):
        return "0x" + v.hex()
    if isinstance(v, (list, tuple)):
        return [_fmt(x) for x in v]
    return v


def decode_tx(tx: dict) -> dict:
    out: dict[str, Any] = {"to": tx.get("to"), "value": tx.get("value")}
    cm = tx.get("contractMethod")
    if cm:
        vals = tx.get("contractInputsValues") or {}
        out["function"] = cm.get("name")
        out["args"] = vals
        if vals.get("targetAddress"):
            out["target"] = to_checksum_address(vals["targetAddress"])
        if vals.get("selector"):
            out["selector"] = vals["selector"]
        if "roleKey" in vals:
            out["role"] = role_key_label(vals["roleKey"])
        if "options" in vals:
            out["options"] = ExecutionOptions(int(vals["options"])).name
        return out
    data = tx.get("data") or "0x"
    sel = data[:10].lower()
    if sel not in SELECTORS:
        out["function"] = f"unknown {sel}"
        return out
    name, types = SELECTORS[sel]
    vals = abi_decode(types, bytes.fromhex(data[10:]))
    out["function"] = name
    if name in ("allowTarget", "revokeTarget", "scopeTarget", "allowFunction", "revokeFunction", "scopeFunction"):
        out["role"] = role_key_label("0x" + vals[0].hex())
        out["target"] = to_checksum_address(vals[1])
        if name.endswith("Function"):
            out["selector"] = "0x" + vals[2].hex()
        if name == "scopeFunction":
            tree = unflatten(vals[3])
            out["condition"] = tree.to_json()
            out["condition_text"] = render(tree)
            out["options"] = ExecutionOptions(vals[4]).name
        elif name in ("allowTarget", "allowFunction"):
            out["options"] = ExecutionOptions(vals[-1]).name
    else:
        out["args"] = _fmt(list(vals))
    return out


def decode_batch(batch: dict) -> dict:
    txs = batch.get("transactions") or []
    return {"meta": batch.get("meta"), "chainId": batch.get("chainId"),
            "transactions": [decode_tx(t) for t in txs]}


def summarize(res: dict) -> str:
    lines = [f"{len(res['transactions'])} transactions"]
    for i, t in enumerate(res["transactions"]):
        s = f"  {i:2} {t.get('function')}"
        if t.get("target"):
            s += f" {t['target']}"
        if t.get("selector"):
            s += f" {t['selector']}"
        if t.get("options") and t["options"] != "NONE":
            s += f" options={t['options']}"
        lines.append(s)
        if t.get("condition_text"):
            lines.extend("       " + ln for ln in t["condition_text"].splitlines())
    return "\n".join(lines)
