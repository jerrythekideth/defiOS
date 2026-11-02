"""Propose a transaction to the Operator Safe queue (M2).

    calls -> tx.build_operator_tx -> nonce -> SafeTxHash (EIP-712, Safe >= 1.3 / 1.4.1)
          -> sign as a DELEGATE with PROPOSER_PRIVATE_KEY -> POST to the Safe Transaction Service

Dry run is the default: everything is built, hashed and (if a key is present)
signed, then printed. Nothing is posted unless `send=True` (`--send`).

Never signs as an owner (an owner's signature would count as a confirmation),
never confirms, never executes. Humans sign in the Safe app.

Nonce: max(on-chain nonce, highest queued nonce + 1). The queue may already hold
transactions the humans have not signed yet; proposing at the on-chain nonce
would put the new transaction on top of one of them (the bug in the original
proposal script). An explicit `nonce=` is only for deliberate replacements.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from eth_abi import encode as abi_encode
from eth_utils import keccak, to_checksum_address

from .chain import ChainError, Rpc
from .config import VaultConfig
from .safe import TxService, safe_info
from .tx import (
    MULTISEND_SELECTOR,
    Call,
    OperatorTx,
    SafeTx,
    build_operator_tx,
    decode_multisend,
)

SAFE_TX_TYPEHASH = keccak(text="SafeTx(address to,uint256 value,bytes data,uint8 operation,uint256 safeTxGas,"
                               "uint256 baseGas,uint256 gasPrice,address gasToken,address refundReceiver,"
                               "uint256 nonce)")
DOMAIN_TYPEHASH = keccak(text="EIP712Domain(uint256 chainId,address verifyingContract)")
APP_PREFIX = {1: "eth", 100: "gno", 42161: "arb1", 8453: "base", 10: "oeth"}
ORIGIN = "defios"


class ProposeError(RuntimeError):
    pass


# ------------------------------------------------------------------------------ hashing
def domain_separator(chain_id: int, safe: str) -> bytes:
    return keccak(abi_encode(["bytes32", "uint256", "address"], [DOMAIN_TYPEHASH, chain_id, to_checksum_address(safe)]))


def safe_tx_struct_hash(tx: SafeTx) -> bytes:
    if tx.nonce is None:
        raise ProposeError("nonce not set")
    return keccak(abi_encode(
        ["bytes32", "address", "uint256", "bytes32", "uint8", "uint256", "uint256", "uint256", "address", "address",
         "uint256"],
        [SAFE_TX_TYPEHASH, to_checksum_address(tx.to), tx.value, keccak(tx.data), tx.operation, tx.safe_tx_gas,
         tx.base_gas, tx.gas_price, to_checksum_address(tx.gas_token), to_checksum_address(tx.refund_receiver),
         tx.nonce]))


def safe_tx_hash(chain_id: int, safe: str, tx: SafeTx) -> bytes:
    """EIP-712 SafeTxHash as computed by Safe >= 1.3.0 (incl. 1.4.1): domain = (chainId, verifyingContract)."""
    return keccak(b"\x19\x01" + domain_separator(chain_id, safe) + safe_tx_struct_hash(tx))


# ------------------------------------------------------------------------------ nonce
def choose_nonce(onchain_nonce: int, pending: list[dict], override: int | None = None) -> tuple[int, dict]:
    """Next nonce that does not collide with the queue: max(on-chain, highest queued + 1)."""
    queued = sorted({int(p["nonce"]) for p in pending if p.get("nonce") is not None and int(p["nonce"]) >= onchain_nonce})
    auto = max(onchain_nonce, (queued[-1] + 1) if queued else onchain_nonce)
    info = {"onchain_nonce": onchain_nonce, "queued_nonces": queued, "auto": auto}
    if override is None:
        info["chosen_by"] = "max(on-chain nonce, highest queued + 1)"
        return auto, info
    if override < onchain_nonce:
        raise ProposeError(f"nonce {override} is already used on chain (current {onchain_nonce})")
    info["chosen_by"] = "explicit --nonce" + (" (REPLACES a queued transaction)" if override in queued else "")
    return override, info


# ------------------------------------------------------------------------------ signing
def sign_safe_tx_hash(h: bytes, private_key: str) -> tuple[str, str]:
    """Sign the SafeTxHash the way the Safe SDK signs for a delegate proposal: eth_sign over the 32-byte hash,
    v + 4 (31/32) so the service/contract recover it with the "\\x19Ethereum Signed Message" prefix.
    Returns (signer address, 0x signature)."""
    from eth_account import Account
    from eth_account.messages import encode_defunct
    acct = Account.from_key(private_key)
    signed = acct.sign_message(encode_defunct(primitive=h))
    sig = signed.r.to_bytes(32, "big") + signed.s.to_bytes(32, "big") + bytes([signed.v + 4])
    return acct.address, "0x" + sig.hex()


def recover_signer(h: bytes, signature: str) -> str:
    """Recover the signer of a Safe signature over a SafeTxHash (v 27/28 = raw hash, 31/32 = eth_sign)."""
    from eth_account import Account
    from eth_account.messages import encode_defunct
    sig = bytes.fromhex(signature[2:])
    v = sig[64]
    if v > 30:
        return Account.recover_message(encode_defunct(primitive=h), signature=sig[:64] + bytes([v - 4]))
    return Account._recover_hash(h, signature=sig)


def safe_links(chain_id: int, safe: str, h: str | None = None) -> dict:
    prefix = APP_PREFIX.get(chain_id, str(chain_id))
    safe = to_checksum_address(safe)
    out = {"queue": f"https://app.safe.global/transactions/queue?safe={prefix}:{safe}"}
    if h:
        out["tx"] = f"https://app.safe.global/transactions/tx?safe={prefix}:{safe}&id=multisig_{safe}_{h}"
    return out


# ------------------------------------------------------------------------------ propose
def _uncatalogued(calls: list[Call], catalog, vault: VaultConfig) -> list[str]:
    if catalog is None:
        return []
    index = catalog.address_index(vault.book_vars)
    return [c.to for c in calls if c.to.lower() not in index]


def _lock_unwrappers(books_dir: Path | None, vault: VaultConfig) -> dict | None:
    if not books_dir:
        return None
    p = Path(books_dir) / vault.name / "role.lock.json"
    if not p.exists():
        return None
    try:
        return {k.lower(): v for k, v in (json.loads(p.read_text()).get("unwrappers") or {}).items()}
    except (OSError, ValueError):
        return None


def _probe(vault: VaultConfig, rpc: Rpc, otx: OperatorTx) -> list[dict]:
    from .roles import get_adapter
    version = vault.roles_version if vault.roles_version != "auto" else "v2"
    adapter = get_adapter(version, vault.roles_modifier, vault.chain_id)
    out = []
    for c in otx.inner:
        r = adapter.probe(rpc, vault.operator, vault.role_key, c.to, c.value, c.data, c.operation)
        out.append({"to": c.to, "label": c.label, "ok": r.ok,
                    **({"error": r.error or r.status or r.info} if not r.ok else {})})
    return out


def describe_calls(otx: OperatorTx, catalog=None, vault: VaultConfig | None = None) -> list[dict]:
    """Inner calls with catalog names (contract + function) where known, for the human summary."""
    names = catalog.address_index(vault.book_vars if vault else {}) if catalog else {}
    funcs = catalog.known_functions(vault.book_vars if vault else {}) if catalog else {}
    out = []
    for c in otx.calls:
        d = c.to_json()
        hit = names.get(c.to.lower())
        if hit:
            d["contract"] = ", ".join(f"{p}.{n}" for p, n in hit)
        if c.selector:
            f = funcs.get((c.to.lower(), c.selector))
            if f and f[0].get("sig") is not None:
                where = f"{f[0]['protocol']}.{f[0]['action']}: " if f[0].get("action") else ""
                d["function"] = where + f[0]["sig"].canonical
        out.append(d)
    return out


def propose(vault: VaultConfig, calls: list[Call], *, send: bool = False, nonce: int | None = None,
            batch: str = "roles", catalog=None, rpc: Rpc | None = None, service: Any = None,
            private_key: str | None = None, books_dir: Path | None = None, description: str | None = None,
            origin: str = ORIGIN) -> dict:
    """Build (and with send=True, post) a proposal on the Operator Safe. Returns a JSON-able report.

    Blocking problems (anything in `problems`) stop a send; a dry run reports them."""
    if not vault.operator:
        raise ProposeError("vaults.<name>.operator is not set in defios.yaml")
    otx = build_operator_tx(calls, vault.roles_modifier, vault.role_key, vault.chain_id, batch, vault.multisend)
    problems: list[str] = []
    warnings = list(otx.warnings)
    op = vault.operator

    unknown = _uncatalogued(calls, catalog, vault)
    if unknown and catalog is not None:  # a looked-up address (any Morpho vault): re-proved onchain now
        from .lookups import recognize
        for a in list(unknown):
            hit = recognize(catalog, a, rpc, vault.book_vars)
            if hit:
                unknown.remove(a)
                warnings.append(f"{a} is not in a card; accepted because {hit} vouches for it onchain")
    if unknown:
        problems.append(f"uncatalogued call target(s) {unknown}: every address must come from a card or defios.yaml")
    if otx.batch == "roles":
        unw = _lock_unwrappers(books_dir, vault)
        key = f"{otx.inner[0].to.lower()}:{MULTISEND_SELECTOR}"
        if unw is not None and key not in unw:
            warnings.append("role.lock.json shows no MultiSend unwrapper for this batch; the Roles modifier will "
                            "reject it (re-sync to confirm, or use --batch operator)")

    # --- Operator state: nonce, owners, queue
    svc = service
    if svc is None:
        try:
            svc = TxService(vault.chain_id)
        except ChainError as e:
            problems.append(str(e))
    onchain, owners, pending, source = None, None, [], None
    if rpc is not None:
        try:
            info = safe_info(rpc, op)
            onchain, owners, source = info["nonce"], info["owners"], "rpc"
        except (ChainError, KeyError) as e:
            warnings.append(f"Operator read over RPC failed: {e}")
    if svc is not None:
        try:
            if onchain is None:
                si = svc.safe(op)
                onchain, owners, source = si["nonce"], si["owners"], "safe tx service"
            pending = svc.pending(op, onchain)
        except ChainError as e:
            problems.append(f"Safe Transaction Service unreachable: {e}")
    if onchain is None:
        if nonce is None:
            raise ProposeError("cannot read the Operator nonce (no RPC_URL and the Safe Transaction Service failed); "
                               "pass --nonce for a dry run")
        onchain, nonce_info = nonce, {"chosen_by": "explicit --nonce (queue not read)", "onchain_nonce": None}
        chosen = nonce
    else:
        chosen, nonce_info = choose_nonce(onchain, pending, nonce)
    nonce_info["source"] = source
    otx.safe_tx.nonce = chosen
    h = safe_tx_hash(vault.chain_id, op, otx.safe_tx)
    hhex = "0x" + h.hex()

    # --- signer: a delegate, never an owner
    key = private_key if private_key is not None else os.environ.get("PROPOSER_PRIVATE_KEY", "").strip()
    signer = signature = None
    if key:
        from eth_account import Account
        signer = Account.from_key(key).address
        if vault.proposer and signer.lower() != vault.proposer.lower():
            problems.append(f"PROPOSER_PRIVATE_KEY is {signer}, defios.yaml proposer is {vault.proposer}")
        if owners is not None and signer.lower() in {o.lower() for o in owners}:
            problems.append(f"refusing to sign: {signer} is an OWNER of the Operator; its signature would count as a "
                            f"confirmation. Use a delegate key.")
        else:
            signer, signature = sign_safe_tx_hash(h, key)
        if svc is not None and send:
            try:
                dels = {d["delegate"].lower() for d in svc.delegates(op) if d.get("delegate")}
                if signer.lower() not in dels:
                    problems.append(f"{signer} is not a delegate of the Operator {op} in the Safe Transaction Service")
            except ChainError as e:
                problems.append(f"cannot read Operator delegates: {e}")
    elif send:
        problems.append("PROPOSER_PRIVATE_KEY is not set")

    # --- pre-flight: eth_call execTransactionWithRole from the Operator (shouldRevert = false)
    probes = None
    if rpc is not None:
        try:
            probes = _probe(vault, rpc, otx)
            for p in probes:
                if not p["ok"]:
                    problems.append(f"Roles pre-flight failed for {p['label'] or p['to']}: {p.get('error')}")
        except ChainError as e:
            warnings.append(f"pre-flight eth_call failed: {e}")
    else:
        warnings.append("not pre-flighted (no RPC_URL): run `defios check` first")

    payload = {
        "to": otx.safe_tx.to, "value": str(otx.safe_tx.value), "data": "0x" + otx.safe_tx.data.hex(),
        "operation": otx.safe_tx.operation, "safeTxGas": "0", "baseGas": "0", "gasPrice": "0",
        "gasToken": otx.safe_tx.gas_token, "refundReceiver": otx.safe_tx.refund_receiver, "nonce": str(chosen),
        "contractTransactionHash": hhex, "sender": signer, "signature": signature, "origin": origin,
    }
    report: dict[str, Any] = {
        "mode": "send" if send else "dry-run",
        "posted": False,
        "description": description,
        "chain_id": vault.chain_id,
        "vault": vault.name,
        "avatar": vault.avatar,
        "operator": op,
        "roles_modifier": otx.roles_modifier,
        "role_key": vault.role_key,
        "batch": otx.batch,
        "calls": describe_calls(otx, catalog, vault),
        "operator_tx": otx.safe_tx.to_json(),
        "nonce": nonce_info | {"chosen": chosen},
        "safe_tx_hash": hhex,
        "signer": signer,
        "signature": signature,
        "preflight": probes,
        "warnings": warnings,
        "problems": problems,
        "links": safe_links(vault.chain_id, op, hhex),
        "payload": payload,
    }
    if otx.batch == "roles" and len(calls) > 1:
        report["multisend"] = {"address": otx.inner[0].to,
                               "decoded": [c.to_json() for c in decode_multisend(otx.inner[0].data)]}
    if not send:
        return report
    if problems:
        raise ProposeError("not sent: " + "; ".join(problems))
    svc.propose(op, payload)
    report["posted"] = True
    return report


def summarize(r: dict) -> str:
    lines = [f"{'PROPOSED' if r['posted'] else 'DRY RUN (nothing posted; add --send to propose)'}: "
             f"{len(r['calls'])} call(s) via Roles {r['roles_modifier']} as {r['role_key']}, batch={r['batch']}"]
    if r.get("description"):
        lines.append(f"  {r['description']}")
    for c in r["calls"]:
        what = c.get("function") or (c["data"][:10] if c["data"] != "0x" else "(no data)")
        lines.append(f"  - {c.get('contract') or c['to']}: {what} value={c['value']} op={c['operation']}")
    n = r["nonce"]
    lines.append(f"  Operator {r['operator']} nonce {n['chosen']} ({n['chosen_by']}; on-chain {n.get('onchain_nonce')}, "
                 f"queued {n.get('queued_nonces', '?')})")
    lines.append(f"  SafeTxHash {r['safe_tx_hash']}  signer {r['signer'] or '(no PROPOSER_PRIVATE_KEY)'}")
    for w in r["warnings"]:
        lines.append(f"  warning: {w}")
    for p in r["problems"]:
        lines.append(f"  PROBLEM: {p}")
    lines.append(f"  {'review and sign' if r['posted'] else 'queue'}: {r['links'].get('tx') if r['posted'] else r['links']['queue']}")
    return "\n".join(lines)
