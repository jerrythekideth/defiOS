"""`defios state`: "what do I hold and what can I do", in plain words, from the book."""
from __future__ import annotations

from pathlib import Path

import yaml


def load_book(path: Path) -> dict:
    if not path.exists():
        raise FileNotFoundError(f"{path} not found; run `defios sync` first")
    return yaml.safe_load(path.read_text())


def plain_state(book: dict) -> str:
    lines = [f"{book['vault']} at block {book['synced_at']['block']} "
             f"(synced {book['synced_at']['time']}, roles {book['roles_version']})"]
    lines.append("Holds:")
    any_hold = False
    bal = book.get("balances") or {}
    if "tokens" in bal:
        from .balances import plain_balances
        bl = plain_balances(bal)
        lines.extend(bl)
        any_hold = bool(bl)
    for proto, u in (book.get("uses") or {}).items():
        for rid, v in (u.get("reads") or {}).items():
            if isinstance(v, dict) and ("value" in v or "raw" in v):
                val = v.get("value", v.get("raw"))
                if val in (0, 0.0, [], None):
                    continue
                any_hold = True
                lines.append(f"  {proto}.{rid}: {val}")
    if not any_hold:
        lines.append("  (no non-zero reads; reads need RPC_URL)")
    perms = book.get("permissions") or {}
    groups: dict[str, list[str]] = {}
    for k, v in perms.items():
        groups.setdefault(v["status"], []).append(k)
    lines.append("Can do (allowed): " + (", ".join(sorted(groups.get("allowed", []))) or "nothing"))
    for st in ("unsafe", "missing", "needs_signature", "unresolved"):
        if groups.get(st):
            lines.append(f"{st.capitalize()}: " + ", ".join(sorted(groups[st])))
    q = book.get("queue") or {}
    for label in ("vault", "operator"):
        qq = q.get(label)
        if isinstance(qq, dict) and "pending" in qq:
            lines.append(f"{label} queue: {len(qq['pending'])} pending, next free nonce {qq['next_free_nonce']}")
    return "\n".join(lines)
