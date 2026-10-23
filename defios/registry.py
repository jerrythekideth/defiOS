"""The protocol registry: what an address is, how sure we are, and how it links to others.

Two layers share the catalog:

* the **registry** (this module) answers "what is 0x…?": every card contract with
  a concrete address is a record (chain, address, protocol, name, category,
  sources, onchain proofs, edges, trust status);
* the **cards** answer "what do I call to do X?" (catalog.py, plan.py).

Trust (see catalog/REGISTRY.md):

* ``verified``: two independent source kinds, or one source plus a passing
  onchain proof (``defios registry verify`` runs them and writes
  catalog/registry.proofs.json);
* ``unverified``: shown, never acted on;
* ``conflict``: two protocols claim the address, a proof fails, there is no code,
  or a human marked it. A human resolves it; nothing here does;
* ``retired``: history only.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from eth_abi import decode as abi_decode
from eth_abi import encode as abi_encode
from eth_utils import is_address, to_checksum_address

from .catalog import Catalog, CardError, parse_signature
from .chain import ChainError

SCHEMA = "defios.registry/1"
PROOFS_FILE = "registry.proofs.json"
INDEX_FILE = "index.json"
SELF = "$self"

CATEGORIES = {
    # entry points
    "market", "vault", "staking", "router", "bridge", "minter", "queue", "adapter", "spender", "escrow",
    # tokens
    "token", "receipt", "debt", "reward", "governance",
    # dependencies
    "oracle", "price_feed", "rate_model", "config", "proxy_admin", "timelock", "multisig", "keeper",
}

# authoring key on a contract (`edges: {underlying: X}`) -> (relation, direction).
# "in": the edge is (X, relation, this); "out": (this, relation, X).
EDGE_KEYS = {
    "underlying": ("underlying_of", "in"),
    "receipt": ("receipt_of", "in"),
    "debt": ("debt_of", "in"),
    "admin": ("admin_of", "in"),
    "owner": ("owner_of", "in"),
    "spender": ("spender_for", "in"),
    "priced_by": ("priced_by", "out"),
    "reads_from": ("reads_from", "out"),
    "bridges_to": ("bridges_to", "out"),
}
RELATIONS = {r for r, _ in EDGE_KEYS.values()} | {"upgrades"}

# Who said this address is what. Independent = different kinds. `card` is the card
# author (implicit on every record); hints are shown but never count toward `verified`.
SOURCE_KINDS = {"card", "docs", "explorer", "address-book", "defi-kit", "kpk-nav", "payload", "audit", "hypernative"}
HINT_KINDS = {"hypernative"}

STATUSES = ("verified", "unverified", "conflict", "retired")

EIP1967 = {
    "implementation": "0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc",
    "admin": "0xb53127684a568b3173ae13b9f8a6016e243e63b6e8ee1178d6a717850b5d6103",
    "beacon": "0xa3f0ad74e5423aebfd80d3ef4346578335a9a72aeaee59ff6cb3582b35133d50",
}


class RegistryError(ValueError):
    pass


def key(chain_id: int, address: str) -> str:
    return f"{int(chain_id)}:{address.lower()}"


@dataclass
class Record:
    chain_id: int
    address: str  # checksum
    protocol: str
    name: str
    category: str | None
    kind: str | None
    aliases: list[str]
    sources: list[tuple[str, str]]  # (kind, detail)
    proofs: list[dict]
    edges: dict
    status_set: str | None  # `status:` written in the card (conflict / retired)
    note: str = ""

    @property
    def key(self) -> str:
        return key(self.chain_id, self.address)

    @property
    def ref(self) -> str:
        return f"{self.protocol}.{self.name}"


@dataclass
class Proof:
    id: str
    target: str  # address the read is sent to
    sig: str
    args: list
    out_types: list[str]
    expect: str  # address that must come back
    contains: bool


@dataclass
class Status:
    status: str
    why: list[str] = field(default_factory=list)


def _card_source_kind(card) -> str:
    """The implicit source of a card's records: `defi-kit` when the card itself was built from
    defi-kit (so a defi-kit import is not counted as a second, independent source)."""
    text = " ".join(str(s) for s in card.raw.get("sources") or [])
    return "defi-kit" if "defi-kit" in text else "card"


def _parse_source(s: str, where: str) -> tuple[str, str]:
    kind, _, detail = str(s).partition(":")
    kind = kind.strip()
    if kind not in SOURCE_KINDS or not detail.strip():
        raise RegistryError(f"{where}: source {s!r} must be '<kind>: <detail>' with kind one of "
                            f"{sorted(SOURCE_KINDS)}")
    return kind, detail.strip()


class Registry:
    def __init__(self, catalog: Catalog, records: list[Record], errors: list[str]):
        self.catalog = catalog
        self.records = records
        self.errors = errors
        self.by_key: dict[str, list[Record]] = {}
        for r in records:
            self.by_key.setdefault(r.key, []).append(r)
        self.by_ref = {r.ref: r for r in records}
        self._actions: dict[str, set[str]] | None = None

    # ------------------------------------------------------------------ load
    @classmethod
    def from_catalog(cls, catalog: Catalog) -> "Registry":
        records, errors = [], []
        for proto, card in catalog.cards.items():
            card_chain = int(card.raw.get("chain_id") or 1)
            implicit = (_card_source_kind(card), f"catalog/{proto}/card.yaml")
            for name, c in card.contracts.items():
                c = c or {}
                if "ref" in c:
                    continue
                where = f"{proto}.{name}"
                try:
                    addr = catalog.resolve_contract(name, proto)
                except CardError as e:
                    errors.append(str(e))
                    continue
                if not addr:  # TODO, $book.*, read-time: not a registry record (yet)
                    continue
                cat = c.get("category")
                if cat is None:
                    errors.append(f"{where}: no `category` (one of {sorted(CATEGORIES)})")
                elif cat not in CATEGORIES:
                    errors.append(f"{where}: category {cat!r} is not one of {sorted(CATEGORIES)}")
                sources = [implicit]
                for s in c.get("sources") or []:
                    try:
                        sources.append(_parse_source(s, where))
                    except RegistryError as e:
                        errors.append(str(e))
                st = c.get("status")
                if st is not None and st not in ("conflict", "retired"):
                    errors.append(f"{where}: `status` may only be set to conflict or retired (the rest is computed)")
                edges = c.get("edges") or {}
                for k in edges:
                    if k not in EDGE_KEYS:
                        errors.append(f"{where}: edge {k!r} is not one of {sorted(EDGE_KEYS)}")
                records.append(Record(int(c.get("chain_id") or card_chain), addr, proto, name, cat, c.get("kind"),
                                      [str(a) for a in c.get("aliases") or []], sources,
                                      list(c.get("proofs") or []), edges, st, str(c.get("note") or "")))
        reg = cls(catalog, records, errors)
        for r in records:
            for k, refs in r.edges.items():
                for ref in refs if isinstance(refs, list) else [refs]:
                    if reg.resolve(ref, r) is None:
                        errors.append(f"{r.ref}: edge {k} -> {ref!r} does not resolve to a registry record")
            try:
                reg.proofs_of(r)
            except RegistryError as e:
                errors.append(str(e))
        return reg

    def resolve(self, ref: str, rec: Record) -> Record | None:
        """A contract ref (`name`, `card.name`, `$self`) seen from `rec`, as a registry record."""
        if ref == SELF:
            return rec
        try:
            a = self.catalog.resolve_contract(str(ref), rec.protocol)
        except CardError:
            return None
        if not a:
            return None
        proto, name = str(ref).split(".", 1) if "." in str(ref) else (rec.protocol, str(ref))
        meta = self.catalog.cards[proto].contracts.get(name) or {}
        while "ref" in meta:  # follow card-to-card refs to the owning record
            proto, name = meta["ref"].split(".", 1) if "." in meta["ref"] else (proto, meta["ref"])
            meta = self.catalog.cards[proto].contracts.get(name) or {}
        return self.by_ref.get(f"{proto}.{name}")

    # ---------------------------------------------------------------- proofs
    def proofs_of(self, rec: Record) -> list[Proof]:
        out = []
        for i, p in enumerate(rec.proofs):
            where = f"{rec.ref}.proofs[{i}]"
            if not isinstance(p, dict) or "read" not in p or "expect" not in p:
                raise RegistryError(f"{where}: a proof needs `read` and `expect`")
            src = self.resolve(p.get("from", SELF), rec)
            if src is None:
                raise RegistryError(f"{where}: `from: {p.get('from')}` is not a registry record")
            if src.chain_id != rec.chain_id:
                raise RegistryError(f"{where}: `from` is on chain {src.chain_id}, the record on {rec.chain_id}")
            try:
                sig = parse_signature(p["read"])
            except CardError as e:
                raise RegistryError(f"{where}: {e}") from e
            outs = [o.type for o in sig.outputs]
            exp, contains = p["expect"], False
            if isinstance(exp, dict):
                if set(exp) != {"contains"}:
                    raise RegistryError(f"{where}: expect must be a ref or {{contains: ref}}")
                exp, contains = exp["contains"], True
            if outs != (["address[]"] if contains else ["address"]):
                raise RegistryError(f"{where}: `read` must return {'(address[])' if contains else '(address)'}")
            want = self.resolve(exp, rec)
            if want is None:
                raise RegistryError(f"{where}: expect {exp!r} is not a registry record")
            args = []
            for a, prm in zip(p.get("args") or [], sig.params):
                if prm.type == "address":
                    r2 = self.resolve(a, rec) if not is_address(str(a)) else None
                    args.append(r2.address if r2 else to_checksum_address(str(a)))
                else:
                    args.append(int(a) if not isinstance(a, str) else int(a, 0))
            if len(args) != len(sig.params):
                raise RegistryError(f"{where}: {len(sig.params)} args expected")
            arg_txt = ",".join(str(a) for a in p.get("args") or [])
            out.append(Proof(f"{p.get('from', SELF)}.{sig.name}({arg_txt}) {'contains' if contains else '=='} {exp}",
                             src.address, sig.canonical, args, outs, want.address, contains))
        return out

    def _proofs_or_none(self, rec: Record) -> list[Proof]:
        """Proofs whose spec resolves; a broken spec is already in `self.errors`."""
        try:
            return self.proofs_of(rec)
        except RegistryError:
            return []

    # ---------------------------------------------------------------- status
    def status(self, rec: Record, results: dict) -> Status:
        if rec.status_set == "retired":
            return Status("retired", ["marked retired in the card"])
        why = []
        if rec.status_set == "conflict":
            why.append("marked conflict in the card")
        others = [o.ref for o in self.by_key.get(rec.key, []) if o.protocol != rec.protocol]
        if others:
            why.append(f"also claimed by {', '.join(others)}")
        res = results.get(rec.key) or {}
        if res.get("code") is False:
            why.append(f"no contract code at this address on chain {rec.chain_id}")
        proofs = self._proofs_or_none(rec)
        ran = {p["id"]: p for p in res.get("proofs") or []}
        for p in proofs:
            r = ran.get(p.id)
            if r is not None and r.get("ok") is False:
                why.append(f"proof failed: {p.id} (got {r.get('got')})")
        if why:
            return Status("conflict", why)
        kinds = sorted({k for k, _ in rec.sources if k not in HINT_KINDS})
        passed = [p.id for p in proofs if (ran.get(p.id) or {}).get("ok") is True]
        if len(kinds) >= 2:
            return Status("verified", [f"independent sources: {', '.join(kinds)}"])
        if kinds and passed:
            return Status("verified", [f"source {kinds[0]} + onchain proof: {passed[0]}"])
        missing = "no onchain proof has passed" if proofs else "no onchain proof declared"
        if proofs and not ran:
            missing = "proofs not run yet (defios registry verify)"
        return Status("unverified", [f"one source ({', '.join(kinds) or 'none'}); {missing}"])

    # ---------------------------------------------------------------- export
    def edges(self) -> list[dict]:
        out = []
        for r in self.records:
            for k, refs in r.edges.items():
                rel, direction = EDGE_KEYS.get(k, (None, None))
                if rel is None:
                    continue
                for ref in refs if isinstance(refs, list) else [refs]:
                    o = self.resolve(ref, r)
                    if o is None:
                        continue
                    a, b = (o, r) if direction == "in" else (r, o)
                    out.append({"from": a.key, "relation": rel, "to": b.key, "from_ref": a.ref, "to_ref": b.ref})
        return sorted(out, key=lambda e: (e["from"], e["relation"], e["to"]))

    def actions_touching(self, rec: Record) -> list[str]:
        if self._actions is None:
            self._actions = {}
            for (addr, _sel), uses in self.catalog.known_functions().items():
                for u in uses:
                    if u.get("action"):
                        self._actions.setdefault(addr, set()).add(f"{u['protocol']}.{u['action']}")
        return sorted(self._actions.get(rec.address.lower(), ()))

    def describe(self, rec: Record, results: dict) -> dict:
        st = self.status(rec, results)
        res = results.get(rec.key) or {}
        card = self.catalog.cards[rec.protocol].raw
        d = {
            "chain_id": rec.chain_id, "address": rec.address, "protocol": rec.protocol,
            "protocol_name": card.get("name"), "name": rec.name, "ref": rec.ref,
            "category": rec.category, "kind": rec.kind, "aliases": rec.aliases,
            "status": st.status, "why": st.why,
            "card_reviewed": bool(card.get("reviewed")),
            "sources": [f"{k}: {v}" for k, v in rec.sources],
            "proofs": [{"id": p.id, "ok": (({q["id"]: q for q in res.get("proofs") or []}.get(p.id)) or {}).get("ok")}
                       for p in self._proofs_or_none(rec)],
            "proxy": {k: res[k] for k in ("implementation", "admin", "beacon") if res.get(k)},
            "actions": self.actions_touching(rec),
        }
        if rec.note:
            d["note"] = rec.note
        return d

    def export_index(self, results: dict) -> dict:
        meta = results.get("_meta") or {}
        protocols = {}
        for proto, card in sorted(self.catalog.cards.items()):
            protocols[proto] = {"name": card.raw.get("name"), "status": card.raw.get("status"),
                                "reviewed": bool(card.raw.get("reviewed")),
                                "chains": sorted({r.chain_id for r in self.records if r.protocol == proto}),
                                "actions": sorted(card.actions)}
        deployments = {}
        for r in sorted(self.records, key=lambda r: (r.chain_id, r.address.lower(), r.protocol)):
            d = self.describe(r, results)
            d.pop("actions")
            deployments.setdefault(r.key, []).append(d)
        return {"schema": SCHEMA, "proofs_checked": meta, "protocols": protocols,
                "deployments": deployments, "edges": self.edges()}

    # ---------------------------------------------------------------- whois
    def whois(self, address: str, chain_id: int | None, results: dict) -> dict:
        if not is_address(address):
            raise RegistryError(f"{address!r} is not an address")
        a = address.lower()
        hits = [r for r in self.records if r.address.lower() == a and (chain_id is None or r.chain_id == chain_id)]
        out: dict[str, Any] = {"address": to_checksum_address(address), "chain_id": chain_id,
                               "matches": [self.describe(r, results) for r in hits]}
        keys = {r.key for r in hits}
        out["edges"] = [e for e in self.edges() if e["from"] in keys or e["to"] in keys]
        if not hits:
            out["verdict"] = "unknown: not in the registry. Do not act on it; say so."
        else:
            sts = {m["status"] for m in out["matches"]}
            out["verdict"] = ("verified" if sts == {"verified"} else
                              "conflict: a human must resolve it" if "conflict" in sts else
                              "retired" if sts == {"retired"} else "unverified: shown, never acted on")
        return out


# --------------------------------------------------------------------------- #
# Running the proofs
# --------------------------------------------------------------------------- #


def _word_to_addr(b: bytes) -> str | None:
    if len(b) < 32 or not any(b[12:32]):
        return None
    return to_checksum_address("0x" + b[12:32].hex())


def verify(reg: Registry, rpcs: dict[int, Any], chains: list[int] | None = None) -> dict:
    """Run code checks, EIP-1967 reads and every proof on the given chains. Returns the
    results map (record key -> result) with `_meta` = {chain: {block, checked}}."""
    results: dict[str, Any] = {"_meta": {}}
    for chain_id in sorted({r.chain_id for r in reg.records}):
        if chains and chain_id not in chains:
            continue
        rpc = rpcs.get(chain_id)
        if rpc is None:
            continue
        got_chain = rpc.chain_id()
        if got_chain != chain_id:
            raise ChainError(f"RPC for chain {chain_id} answers chain {got_chain}")
        block = rpc.block_number()
        results["_meta"][str(chain_id)] = {"block": block, "checked": date.today().isoformat()}
        for rec in reg.records:
            if rec.chain_id != chain_id or rec.key in results:
                continue
            res: dict[str, Any] = {"code": len(rpc.get_code(rec.address, block)) > 0, "proofs": []}
            for name, slot in EIP1967.items():
                a = _word_to_addr(rpc.get_storage_at(rec.address, slot, block))
                if a:
                    res[name] = a
            for p in reg.proofs_of(rec):
                pr: dict[str, Any] = {"id": p.id}
                try:
                    sig = parse_signature(p.sig)
                    data = bytes.fromhex(sig.selector[2:]) + abi_encode([q.type for q in sig.params], p.args)
                    raw = rpc.call_raw(p.target, data, block=block)
                    val = abi_decode(p.out_types, raw)[0]
                    if p.contains:
                        pr["ok"] = p.expect.lower() in {str(v).lower() for v in val}
                        pr["got"] = f"{len(val)} addresses"
                    else:
                        pr["ok"] = str(val).lower() == p.expect.lower()
                        pr["got"] = to_checksum_address(val)
                except ChainError as e:
                    pr["ok"], pr["got"] = False, f"reverted: {str(e)[:80]}"
                except Exception as e:  # undecodable return: the contract is not what we think
                    pr["ok"], pr["got"] = False, f"bad return: {str(e)[:80]}"
                res["proofs"].append(pr)
            results[rec.key] = res
    return results


def load_results(catalog_dir: Path) -> dict:
    p = Path(catalog_dir) / PROOFS_FILE
    return json.loads(p.read_text()) if p.exists() else {"_meta": {}}


def merge_results(old: dict, new: dict) -> dict:
    """Keep results for chains that were not re-checked; drop records that no longer exist."""
    out = {k: v for k, v in old.items() if k != "_meta" and k.split(":")[0] not in new.get("_meta", {})}
    out.update({k: v for k, v in new.items() if k != "_meta"})
    meta = {**(old.get("_meta") or {}), **(new.get("_meta") or {})}
    return {"_meta": dict(sorted(meta.items())), **dict(sorted(out.items()))}


def dump(data: dict) -> str:
    return json.dumps(data, indent=1, sort_keys=False) + "\n"


def summarize_whois(res: dict) -> str:
    if not res["matches"]:
        return f"{res['address']}: {res['verdict']}"
    lines = []
    for m in res["matches"]:
        lines.append(f"{m['address']} on chain {m['chain_id']}: {m['protocol_name']} / {m['name']} "
                     f"({m['category']}) — {m['status'].upper()}")
        for w in m["why"]:
            lines.append(f"  · {w}")
        if m["proxy"]:
            lines.append("  proxy: " + ", ".join(f"{k} {v}" for k, v in m["proxy"].items()))
        if m["actions"]:
            lines.append("  card actions that call it: " + ", ".join(m["actions"]))
        if not m["card_reviewed"]:
            lines.append("  card is a draft (reviewed: false)")
    for e in res["edges"]:
        lines.append(f"  edge: {e['from_ref']} {e['relation']} {e['to_ref']}")
    lines.append(f"verdict: {res['verdict']}")
    return "\n".join(lines)


def summarize_verify(reg: Registry, results: dict, checked: list[str]) -> str:
    counts: dict[str, int] = {}
    bad = []
    for r in reg.records:
        st = reg.status(r, results)
        counts[st.status] = counts.get(st.status, 0) + 1
        if st.status == "conflict":
            bad.append(f"  CONFLICT {r.ref} ({r.address}, chain {r.chain_id}): {'; '.join(st.why)}")
    meta = results.get("_meta") or {}
    head = ", ".join(f"chain {c} @ block {meta[c]['block']}" for c in checked if c in meta) or "no chain checked"
    tally = ", ".join(f"{counts.get(s, 0)} {s}" for s in STATUSES)
    return "\n".join([f"registry verify: {head}", f"  {len(reg.records)} records: {tally}", *bad])
