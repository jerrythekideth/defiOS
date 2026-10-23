"""`defios simulate`: run a permission batch and/or a plan on a local fork before anyone signs.

    defios simulate --calls calls.json                        # a plan, as the Operator would run it
    defios simulate --batch permissions.json                  # a Roles batch, as the Vault would run it
    defios simulate --batch permissions.json --calls calls.json   # the batch, then the plan on top

It starts `anvil --fork-url $RPC_URL` (Foundry), impersonates the Safe that
would execute (the Roles modifier's owner for a batch, the Operator for calls),
sends exactly the transaction that Safe would make, and reports:

* per transaction: success or the decoded revert (Roles ConditionViolation
  names the failing check), gas used;
* the Vault's token deltas, from the ERC-20 Transfer logs, and its ETH delta;
* every allowance the Vault still has open at the end (AGENTS.md: none should).

Nothing leaves the fork: no signature, no Safe Transaction Service, no
broadcast. Gas price is 0 on the fork, so balances move only by the calls.
"""
from __future__ import annotations

import json
import shutil
import socket
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from eth_abi import decode as abi_decode
from eth_abi import encode as abi_encode
from eth_utils import keccak, to_checksum_address

from .catalog import Catalog
from .chain import CallReverted, ChainError, Rpc
from .config import VaultConfig
from .roles.v2 import decode_revert
from .tx import Call, build_operator_tx, exec_with_role

TRANSFER = "0x" + keccak(text="Transfer(address,address,uint256)").hex()
APPROVAL = "0x" + keccak(text="Approval(address,address,uint256)").hex()
GAS = 30_000_000


class SimulateError(ValueError):
    pass


# ------------------------------------------------------------------------------ the fork
class Fork:
    """A local anvil, optionally forking `fork_url` at `block`. Use as a context manager."""

    def __init__(self, fork_url: str | None = None, block: int | None = None, anvil: str | None = None,
                 chain_id: int | None = None, startup_timeout: float = 90):
        self.fork_url, self.block, self.chain_id = fork_url, block, chain_id
        self.anvil = anvil or shutil.which("anvil")
        if not self.anvil:
            raise SimulateError("anvil not found: install Foundry (https://getfoundry.sh) or pass --anvil /path/to/anvil")
        self.timeout = startup_timeout
        self.proc: subprocess.Popen | None = None
        self.rpc: Rpc | None = None

    def __enter__(self) -> "Fork":
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        cmd = [self.anvil, "--port", str(port), "--host", "127.0.0.1", "--silent",
               "--gas-price", "0", "--block-base-fee-per-gas", "0", "--disable-code-size-limit"]
        if self.fork_url:
            cmd += ["--fork-url", self.fork_url, "--no-storage-caching"]
            if self.block:
                cmd += ["--fork-block-number", str(self.block)]
        if self.chain_id:
            cmd += ["--chain-id", str(self.chain_id)]
        self.proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        self.rpc = Rpc(f"http://127.0.0.1:{port}", timeout=120)
        deadline = time.time() + self.timeout
        while True:
            if self.proc.poll() is not None:
                err = (self.proc.stderr.read() or b"").decode(errors="replace")[-500:]
                raise SimulateError(f"anvil exited: {err.strip() or 'no output'}")
            try:
                self.rpc.request("eth_chainId", [])
                break
            except ChainError:
                if time.time() > deadline:
                    self.__exit__(None, None, None)
                    raise SimulateError("anvil did not start (fork RPC slow or unreachable?)")
                time.sleep(0.3)
        return self

    def __exit__(self, *exc) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                self.proc.kill()

    # --- helpers -------------------------------------------------------------
    def impersonate(self, addr: str) -> None:
        self.rpc.request("anvil_impersonateAccount", [to_checksum_address(addr)])

    def send(self, sender: str, to: str, data: bytes, value: int = 0) -> dict:
        """Send from an impersonated account; {ok, gas, logs} or {ok: False, error, ...}."""
        tx = {"from": to_checksum_address(sender), "to": to_checksum_address(to), "data": "0x" + data.hex(),
              "value": hex(value), "gas": hex(GAS), "gasPrice": "0x0"}
        try:  # eth_call first: same state, and it returns the revert data
            self.rpc.call_raw(tx["to"], data, sender=tx["from"], value=value)
        except CallReverted as e:
            return {"ok": False, **_revert(e.data, str(e))}
        h = self.rpc.request("eth_sendTransaction", [tx])
        rc = self.rpc.request("eth_getTransactionReceipt", [h])
        if int(rc["status"], 16) != 1:
            return {"ok": False, "error": "reverted on send (passed eth_call)", "gas": int(rc["gasUsed"], 16)}
        return {"ok": True, "gas": int(rc["gasUsed"], 16), "logs": rc.get("logs") or []}


def _revert(data: bytes, message: str) -> dict:
    if data[:4] == bytes.fromhex("08c379a0"):
        try:
            return {"error": "revert: " + abi_decode(["string"], data[4:])[0]}
        except Exception:  # noqa: BLE001
            pass
    r = decode_revert(data, message)
    out = {"error": r.error or message}
    if r.status:
        out["status"] = r.status
    if r.info:
        out["info"] = r.info
    return out


# ------------------------------------------------------------------------------ Tx Builder batches
def _parse_input(t: str, v: Any) -> Any:
    if isinstance(v, str) and (t.endswith("]") or t.startswith("(")):
        v = json.loads(v)
    if t.endswith("]"):
        base = t[: t.rfind("[")]
        return [_parse_input(base, x) for x in v]
    if t.startswith("("):
        from .catalog import _split_top
        return tuple(_parse_input(ct, x) for ct, x in zip(_split_top(t[1:-1]), v))
    if t == "address":
        return to_checksum_address(v)
    if t == "bool":
        return v if isinstance(v, bool) else str(v).lower() == "true"
    if t.startswith(("uint", "int")):
        return int(v, 0) if isinstance(v, str) else int(v)
    if t.startswith("bytes"):
        b = bytes.fromhex(v[2:] if str(v).startswith("0x") else v)
        return b if t == "bytes" else b.ljust(int(t[5:]), b"\0")
    return v


def _input_type(i: dict) -> str:
    t = i["type"]
    if t.startswith("tuple"):
        return "(" + ",".join(_input_type(c) for c in i.get("components") or []) + ")" + t[5:]
    return t


def batch_calls(batch: dict) -> list[Call]:
    """Safe Tx Builder JSON -> calls (data hex, or contractMethod + contractInputsValues)."""
    out = []
    for i, tx in enumerate(batch.get("transactions") or []):
        data = tx.get("data")
        cm = tx.get("contractMethod")
        if (not data or data == "0x") and cm:
            ins = cm.get("inputs") or []
            types = [_input_type(x) for x in ins]
            vals = tx.get("contractInputsValues") or {}
            try:
                args = [_parse_input(t, vals[x["name"]]) for t, x in zip(types, ins)]
            except (KeyError, ValueError, json.JSONDecodeError) as e:
                raise SimulateError(f"transaction {i}: cannot encode {cm.get('name')}: {e}") from None
            data = keccak(text=f"{cm['name']}({','.join(types)})")[:4] + abi_encode(types, args)
        out.append(Call(to=tx["to"], value=int(tx.get("value") or 0), data=data or b"",
                        label=(cm or {}).get("name") or (data[:10] if isinstance(data, str) else None)))
    if not out:
        raise SimulateError("batch has no transactions")
    return out


# ------------------------------------------------------------------------------ analysis
@dataclass
class _Ledger:
    vault: str
    deltas: dict[str, int] = field(default_factory=dict)
    approvals: set[tuple[str, str]] = field(default_factory=set)

    def add(self, logs: list[dict]) -> None:
        v = self.vault.lower()
        for lg in logs:
            topics = [t.lower() for t in lg.get("topics") or []]
            if len(topics) != 3 or not lg.get("data") or len(lg["data"]) < 66:
                continue  # ERC-721 Transfer has 4 topics; skip it
            src, dst = "0x" + topics[1][-40:], "0x" + topics[2][-40:]
            amount = int(lg["data"][2:66], 16)
            tok = lg["address"].lower()
            if topics[0] == TRANSFER:
                if dst == v:
                    self.deltas[tok] = self.deltas.get(tok, 0) + amount
                if src == v:
                    self.deltas[tok] = self.deltas.get(tok, 0) - amount
            elif topics[0] == APPROVAL and src == v:
                self.approvals.add((tok, dst))


def _read(rpc: Rpc, to: str, sig: str, args: list, out: list[str]) -> Any:
    """eth_call that returns None on revert or an undecodable answer (non-standard tokens, empty accounts)."""
    try:
        return rpc.call(to, sig, args, out)
    except Exception:  # noqa: BLE001
        return None


def _token_info(fork: Fork, catalog: Catalog | None, index: dict, addr: str) -> dict:
    name = None
    hits = index.get(addr.lower())
    dec = None
    if hits:
        name = f"{hits[0][0]}.{hits[0][1]}"
        dec = catalog.contract_meta(*hits[0]).get("decimals") if catalog else None
    if dec is None:
        dec = _read(fork.rpc, addr, "decimals()", [], ["uint8"])
    return {"name": name, "decimals": dec}


def _fmt(raw: int, dec: int | None) -> str:
    if dec is None:
        return f"{raw} (raw)"
    s = f"{abs(raw) / 10**dec:,.{min(dec, 8)}f}".rstrip("0").rstrip(".")
    return ("-" if raw < 0 else "+") + s


def simulate(vault: VaultConfig, fork: Fork, *, batch: dict | None = None, calls: list[Call] | None = None,
             catalog: Catalog | None = None, batch_mode: str = "roles", multisend: str | None = None) -> dict:
    if not batch and not calls:
        raise SimulateError("nothing to simulate: pass a permission batch and/or calls")
    rpc = fork.rpc
    index = catalog.address_index(vault.book_vars) if catalog else {}
    res: dict[str, Any] = {"vault": vault.name, "fork_block": int(rpc.request("eth_blockNumber", []), 16),
                           "forked": bool(fork.fork_url), "steps": [], "warnings": []}
    ledger = _Ledger(vault.avatar)
    eth_before = rpc.get_balance(vault.avatar)
    ok = True

    if batch:
        btxs = batch_calls(batch)
        owner = _read(rpc, vault.roles_modifier, "owner()", [], ["address"])
        if owner is None:
            owner = vault.avatar
            res["warnings"].append("could not read the Roles modifier's owner(); executing the batch as the Vault")
        bsafe = (batch.get("meta") or {}).get("createdFromSafeAddress")
        if bsafe and bsafe.lower() != str(owner).lower():
            res["warnings"].append(f"batch was created for {bsafe}, but the Roles owner is {owner}")
        fork.impersonate(owner)
        for i, c in enumerate(btxs):
            if not ok:
                res["steps"].append({"kind": "permission", "index": i, "label": c.label, "ok": None,
                                     "error": "not run (an earlier transaction reverted; a Safe batch is atomic)"})
                continue
            r = fork.send(owner, c.to, c.data, c.value)
            ok = ok and r["ok"]
            ledger.add(r.pop("logs", []))
            res["steps"].append({"kind": "permission", "index": i, "label": c.label, "to": c.to, **r})

    if calls and ok:
        if not vault.operator:
            raise SimulateError(f"vault {vault.name} has no operator in defios.yaml; calls run through it")
        op = build_operator_tx(calls, vault.roles_modifier, vault.role_key, vault.chain_id, batch_mode, multisend)
        res["warnings"] += op.warnings
        fork.impersonate(vault.operator)
        sends = [op.safe_tx] if op.safe_tx.operation == 0 else [
            Call(vault.roles_modifier, 0, exec_with_role(c, vault.role_key, True)) for c in calls]
        for i, s in enumerate(sends):
            if not ok:
                res["steps"].append({"kind": "operator", "index": i, "ok": None, "error": "not run"})
                continue
            r = fork.send(vault.operator, s.to, s.data, s.value)
            ok = ok and r["ok"]
            ledger.add(r.pop("logs", []))
            label = "execTransactionWithRole(" + (", ".join(c.label or c.to for c in calls) if len(sends) == 1
                                                   else calls[i].label or calls[i].to) + ")"
            res["steps"].append({"kind": "operator", "index": i, "label": label, **r})
    elif calls:
        res["steps"].append({"kind": "operator", "ok": None, "error": "not run (the permission batch reverted)"})

    deltas = {}
    for tok, raw in sorted(ledger.deltas.items()):
        if raw == 0:
            continue
        info = _token_info(fork, catalog, index, tok)
        deltas[to_checksum_address(tok)] = {**info, "raw": str(raw), "amount": _fmt(raw, info["decimals"])}
    res["vault_deltas"] = deltas
    eth = rpc.get_balance(vault.avatar) - eth_before
    if eth:
        res["eth_delta"] = _fmt(eth, 18)
    open_ = []
    for tok, sp in sorted(ledger.approvals):
        left = _read(rpc, tok, "allowance(address,address)", [to_checksum_address(vault.avatar),
                                                              to_checksum_address(sp)], ["uint256"])
        if left:
            info = _token_info(fork, catalog, index, tok)
            spn = index.get(sp)
            open_.append({"token": to_checksum_address(tok), "token_name": info["name"],
                          "spender": to_checksum_address(sp), "spender_name": f"{spn[0][0]}.{spn[0][1]}" if spn else None,
                          "allowance": str(left)})
    res["open_allowances"] = open_
    if open_:
        res["warnings"].append(f"{len(open_)} allowance(s) still open after the transaction")
    res["gas_total"] = sum(s.get("gas") or 0 for s in res["steps"])
    res["ok"] = ok
    return res


def summarize(r: dict) -> str:
    lines = [f"simulate {r['vault']} on {'a fork' if r['forked'] else 'a local chain'} @ block {r['fork_block']}"]
    for s in r["steps"]:
        mark = "ok " if s.get("ok") else ("-- " if s.get("ok") is None else "REVERT")
        line = f"  {mark} {s['kind']} {s.get('index', '')} {s.get('label') or ''}".rstrip()
        if s.get("gas"):
            line += f"  gas {s['gas']:,}"
        if not s.get("ok"):
            line += f"  {s.get('error')}" + (f" ({s['status']})" if s.get("status") else "")
        lines.append(line)
    for tok, d in r["vault_deltas"].items():
        lines.append(f"  Vault {d['amount']} {d['name'] or tok}")
    if r.get("eth_delta"):
        lines.append(f"  Vault {r['eth_delta']} ETH")
    for a in r["open_allowances"]:
        lines.append(f"  open allowance: {a['token_name'] or a['token']} -> {a['spender_name'] or a['spender']} "
                     f"{a['allowance']}")
    for w in r["warnings"]:
        lines.append(f"  warning: {w}")
    lines.append("  all transactions succeeded on the fork" if r["ok"] else
                 "  FAILED on the fork: do not propose; explain the revert to the human")
    return "\n".join(lines)


def load_batch(path: str | Path) -> dict:
    return json.loads(Path(path).read_text())
