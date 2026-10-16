#!/usr/bin/env python3
"""Generate catalog/DEFI_KIT.md and catalog/defi_kit.json from a local defi-kit clone.

    python scripts/defikit_map.py --defi-kit ../ext/karpatkey_defi-kit
    python scripts/defikit_map.py --check        # only check card `defi_kit:` fields
                                                 # against the committed catalog/defi_kit.json

defi-kit (karpatkey/defi-kit) declares every protocol action as a zod schema in
TypeScript (`sdk/src/protocols/**/schema.ts`), with option lists in generated
`_*.ts` data files. This script reads them with a small TypeScript-subset
evaluator (literals, imports, const, arrow functions, map/flatMap, Set/Map and a
zod stand-in). No node, no network. Anything it cannot evaluate is reported as
`unresolved` in the output instead of being guessed.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
CHAINS = {"eth": 1, "gno": 100, "arb1": 42161, "oeth": 10, "base": 8453}

# --------------------------------------------------------------------------------------
# DeFi_os <-> defi-kit mapping (hand-maintained; the generator checks it against the source)
# --------------------------------------------------------------------------------------
CARD_MAP: dict[tuple[str, str], dict] = {
    ("lido", "deposit"): {
        "card": "lido",
        "actions": ["stake", "wrap", "unwrap", "request_withdrawal", "request_withdrawal_wsteth",
                    "claim_withdrawal"],
        "note": "one bundle: stETH/wstETH approvals to wstETH and unstETH, submit (send), wrap, unwrap, "
                "requestWithdrawals[WstETH](_owner = avatar), claimWithdrawal(s)",
    },
    ("gearbox", "deposit"): {
        "card": "gearbox_v3",
        "actions": ["lend", "withdraw"],
        "note": "lending pools only (approve asset, deposit/depositWithReferral, withdraw/redeem pinned to the "
                "avatar). Credit accounts (open/multicall/close) are not in defi-kit.",
    },
    ("morphoVaults", "deposit"): {
        "card": "morpho_vaults",
        "actions": ["deposit", "withdraw"],
        "note": "approve asset + ERC-4626 deposit/withdraw/redeem pinned to the avatar, per listed vault",
    },
    ("cowswap", "swap"): {
        "card": "cowswap",
        "actions": ["swap", "cancel"],
        "note": "sell/buy token sets, receiver = avatar, feeAmountBP <= feeAmountBp (default 0), optional appData "
                "pin; sell=ETH also allows WETH.deposit (weth.wrap). twap=true adds ComposableCoW (not carded).",
    },
}

# DeFi_os cards defi-kit does not cover (or covers only partially), with what to use instead.
NOT_COVERED = {
    "avant": "avETH / savETH (RFQ minting, cooldown staking): not in defi-kit; card built from verified source",
    "origin_arm": "Origin ARM LP vaults (Lido / EtherFi / multi-asset): not in defi-kit",
    "etherfi": "ether.fi weETH adapters, WithdrawRequestNFT and Veda Liquid ETH: not in defi-kit "
               "(weETH only appears as a token inside aave_v3 / balancer / aura lists)",
    "yo": "Yo yoETH vault and gateway: not in defi-kit",
    "makina": "Makina DETH: not in defi-kit (reachable as a cowswap sell/buy token)",
    "gearbox_v3 (credit accounts)": "Gearbox credit facade open/multicall/close: defi-kit `gearbox` covers "
                                    "only lending-pool deposits",
    "weth": "no standalone defi-kit protocol; WETH.deposit is added by cowswap/swap when sell includes ETH",
}


# --------------------------------------------------------------------------------------
# TypeScript-subset tokenizer
# --------------------------------------------------------------------------------------
class TsError(Exception):
    pass


PUNCT = ["...", "===", "!==", "=>", "==", "!=", "&&", "||", "??", "?.", "<=", ">=",
         "{", "}", "(", ")", "[", "]", ",", ":", ";", ".", "=", "<", ">", "|", "&", "!", "?", "+", "-",
         "*", "/", "%", "@"]
_REGEX_PREV = {"(", ",", "=", ":", "[", "!", "&", "|", "?", "{", "}", ";", "=>", "&&", "||", "??", None}


def tokenize(src: str) -> list[tuple[str, Any, int]]:
    out: list[tuple[str, Any, int]] = []
    i, n, line = 0, len(src), 1
    prev: Any = None
    while i < n:
        c = src[i]
        if c == "\n":
            line += 1
            i += 1
            continue
        if c.isspace():
            i += 1
            continue
        if src.startswith("//", i):
            j = src.find("\n", i)
            i = n if j < 0 else j
            continue
        if src.startswith("/*", i):
            j = src.find("*/", i + 2)
            j = n if j < 0 else j + 2
            line += src.count("\n", i, j)
            i = j
            continue
        if c in "\"'`":
            j, buf = i + 1, []
            while j < n and src[j] != c:
                if src[j] == "\\" and j + 1 < n:
                    esc = src[j + 1]
                    buf.append({"n": "\n", "t": "\t"}.get(esc, esc))
                    j += 2
                    continue
                if src[j] == "\n":
                    line += 1
                buf.append(src[j])
                j += 1
            kind = "tmpl" if c == "`" else "str"
            out.append((kind, "".join(buf), line))
            prev = "str"
            i = j + 1
            continue
        if c == "/" and prev in _REGEX_PREV:
            j, in_cls = i + 1, False
            while j < n:
                ch = src[j]
                if ch == "\\":
                    j += 2
                    continue
                if ch == "[":
                    in_cls = True
                elif ch == "]":
                    in_cls = False
                elif ch == "/" and not in_cls:
                    break
                j += 1
            j += 1
            while j < n and src[j].isalpha():
                j += 1
            out.append(("regex", src[i:j], line))
            prev = "regex"
            i = j
            continue
        if c.isdigit():
            m = re.match(r"0x[0-9a-fA-F_]+|\d[\d_]*(\.\d+)?([eE][+-]?\d+)?n?", src[i:])
            txt = m.group(0).replace("_", "").rstrip("n")
            out.append(("num", int(txt, 16) if txt.startswith("0x") else (float(txt) if "." in txt or "e" in txt.lower() else int(txt)), line))
            prev = "num"
            i += len(m.group(0))
            continue
        if c.isalpha() or c in "_$":
            m = re.match(r"[A-Za-z_$][\w$]*", src[i:])
            out.append(("ident", m.group(0), line))
            prev = "ident"
            i += len(m.group(0))
            continue
        for p in PUNCT:
            if src.startswith(p, i):
                out.append(("p", p, line))
                prev = p
                i += len(p)
                break
        else:
            raise TsError(f"line {line}: unexpected character {c!r}")
    return out


# --------------------------------------------------------------------------------------
# Values: zod stand-in, JS collections, namespaces
# --------------------------------------------------------------------------------------
class Z:
    """A zod schema description (not a validator)."""

    def __init__(self, kind: str, **kw):
        self.kind = kind
        self.kw = kw

    def _with(self, **kw) -> Z:
        z = Z(self.kind, **self.kw)
        z.kw.update(kw)
        return z

    def attr(self, name: str):
        if name == "array":
            return lambda: Z("array", inner=self)
        if name == "optional":
            return lambda: self._with(optional=True)
        if name == "default":
            return lambda v: self._with(optional=True, default=v)
        if name == "or":
            return lambda other: Z("union", options=[self, other])
        if name in ("min", "max"):
            return lambda v, *_: self._with(**{name: v})
        if name in ("int", "nonempty", "nullable"):
            return lambda *_: self._with(**{name: True})
        if name in ("regex", "transform", "refine", "describe", "openapi", "length"):
            return lambda *a: self
        raise TsError(f"zod: unsupported method .{name}()")

    # ---- description for the map
    def describe(self) -> dict:
        d: dict[str, Any] = {"type": self.type_str()}
        if self.kw.get("optional"):
            d["optional"] = True
        if "default" in self.kw:
            d["default"] = self.kw["default"]
        opts = self.options()
        if opts is not None:
            d["options_count"] = len(opts)
            d["options"] = opts
        return d

    def type_str(self) -> str:
        k = self.kind
        if k == "array":
            inner = self.kw["inner"]
            s = inner.type_str()
            return f"({s})[]" if inner.kind == "union" else f"{s}[]"
        if k == "union":
            return " | ".join(o.type_str() for o in self.kw["options"])
        if k == "literal":
            return json.dumps(self.kw["value"])
        if k == "number":
            s = "int" if self.kw.get("int") else "number"
            if "min" in self.kw or "max" in self.kw:
                s += f" {self.kw.get('min', '')}..{self.kw.get('max', '')}"
            return s
        if k == "object":
            return "object"
        return k

    def options(self) -> list | None:
        if self.kind == "enum":
            seen, out = set(), []
            for v in self.kw["values"]:
                key = str(v).lower()
                if key not in seen:
                    seen.add(key)
                    out.append(v)
            return out
        if self.kind == "array":
            return self.kw["inner"].options()
        if self.kind == "union":
            # a closed list only when every member is closed (a free `address` member opens it)
            vals: list = []
            for o in self.kw["options"]:
                if o.kind == "literal":
                    vals.append(o.kw["value"])
                elif o.options() is not None:
                    vals.extend(o.options())
                else:
                    return None
            return vals or None
        return None


def _zod_builtin() -> dict:
    return {
        "object": lambda shape=None, *a: Z("object", shape=shape or {}),
        "enum": lambda values, *a: Z("enum", values=list(values)),
        "string": lambda *a: Z("string"),
        "number": lambda *a: Z("number"),
        "boolean": lambda *a: Z("bool"),
        "bigint": lambda *a: Z("bigint"),
        "literal": lambda v, *a: Z("literal", value=v),
        "array": lambda inner, *a: Z("array", inner=inner),
        "union": lambda opts, *a: Z("union", options=list(opts)),
        "any": lambda *a: Z("any"),
        "NEVER": None,
    }


ZX = {"address": lambda *a: Z("address"), "bytes32": lambda *a: Z("bytes32")}


class JsSet(list):
    pass


class JsMap(dict):
    pass


class Namespace:
    def __init__(self, module: Module):
        self.module = module


class Opaque:
    """A value from an import we do not evaluate (ethers, zodiac-roles-sdk, ...)."""

    def __init__(self, what: str):
        self.what = what


def _new(ctor: Any, args: list) -> Any:
    if ctor == "Set":
        seen, out = set(), JsSet()
        for v in (args[0] if args else []):
            k = json.dumps(v, sort_keys=True, default=str)
            if k not in seen:
                seen.add(k)
                out.append(v)
        return out
    if ctor == "Map":
        m = JsMap()
        for pair in (args[0] if args else []):
            m[_hashable(pair[0])] = pair[1]
        return m
    raise TsError(f"new {ctor}: unsupported")


def _hashable(v):
    return json.dumps(v, sort_keys=True, default=str) if isinstance(v, (list, dict)) else v


def _member(obj: Any, name: Any) -> Any:
    if isinstance(obj, Opaque):
        raise TsError(f"opaque import {obj.what} used")
    if isinstance(obj, Namespace):
        return obj.module.get(name)
    if isinstance(obj, Z):
        return obj.attr(name)
    if isinstance(obj, JsMap):
        if name == "values":
            return lambda: list(obj.values())
        if name == "keys":
            return lambda: list(obj.keys())
        if name == "get":
            return lambda k: obj.get(_hashable(k))
    if isinstance(obj, dict):
        if name in obj:
            return obj[name]
        raise TsError(f"no property {name!r}")
    if isinstance(obj, list):
        if isinstance(name, int) or (isinstance(name, float) and name.is_integer()):
            return obj[int(name)]
        if name == "length":
            return len(obj)
        if name == "map":
            return lambda f: [f(v, i) for i, v in enumerate(obj)]
        if name == "flatMap":
            def fm(f):
                out = []
                for i, v in enumerate(obj):
                    r = f(v, i)
                    out.extend(r if isinstance(r, list) else [r])
                return out
            return fm
        if name == "filter":
            return lambda f: [v for i, v in enumerate(obj) if f(v, i)]
        if name == "concat":
            return lambda *xs: obj + [y for x in xs for y in (x if isinstance(x, list) else [x])]
        if name == "includes":
            return lambda x: x in obj
        if name == "slice":
            return lambda a=0, b=None: obj[a:b]
        if name == "values":
            return lambda: list(obj)
    if isinstance(obj, str):
        if name == "toLowerCase":
            return lambda: obj.lower()
        if name == "toUpperCase":
            return lambda: obj.upper()
        if name == "length":
            return len(obj)
    raise TsError(f"unsupported member .{name} on {type(obj).__name__}")


BUILTINS = {
    "Array": {"from": lambda x, f=None: [f(v, i) for i, v in enumerate(x)] if f else list(x),
              "isArray": lambda x: isinstance(x, list)},
    "Object": {"values": lambda d: list(d.values()), "keys": lambda d: list(d.keys()),
               "entries": lambda d: [[k, v] for k, v in d.items()]},
    "undefined": None,
    "true": True,
    "false": False,
    "null": None,
}


# --------------------------------------------------------------------------------------
# Parser (to a tiny AST) and evaluator
# --------------------------------------------------------------------------------------
class Parser:
    def __init__(self, toks: list, pos: int = 0):
        self.t = toks
        self.i = pos

    # helpers
    def peek(self, k: int = 0):
        j = self.i + k
        return self.t[j] if j < len(self.t) else ("eof", None, 0)

    def at(self, v: Any, kind: str | None = None, k: int = 0) -> bool:
        tk = self.peek(k)
        return tk[1] == v and (kind is None or tk[0] == kind) and tk[0] in ("p", "ident")

    def eat(self, v: Any) -> None:
        tk = self.peek()
        if tk[1] != v:
            raise TsError(f"line {tk[2]}: expected {v!r}, got {tk[1]!r}")
        self.i += 1

    def skip_balanced(self) -> None:
        """skip one token, or a whole bracketed group if it opens one"""
        pairs = {"(": ")", "[": "]", "{": "}", "<": ">"}
        tk = self.peek()
        if tk[0] == "p" and tk[1] in pairs:
            depth, opener, closer = 0, tk[1], pairs[tk[1]]
            while True:
                tk = self.peek()
                if tk[0] == "eof":
                    raise TsError("unbalanced brackets")
                if tk[0] == "p" and tk[1] == opener:
                    depth += 1
                elif tk[0] == "p" and tk[1] == closer:
                    depth -= 1
                    if depth == 0:
                        self.i += 1
                        return
                elif opener == "<" and tk[0] == "p" and tk[1] == "=>":
                    pass
                self.i += 1
        self.i += 1

    def skip_type(self) -> None:
        while True:
            while self.peek()[0] == "ident" and self.peek()[1] in ("readonly", "keyof", "typeof", "const", "unique"):
                self.i += 1
                if self.t[self.i - 1][1] == "const":
                    break
            tk = self.peek()
            if tk[0] == "p" and tk[1] in ("[", "(") or tk[0] == "p" and tk[1] == "{":
                self.skip_balanced()
            elif tk[0] in ("str", "tmpl", "num"):
                self.i += 1
            elif tk[0] == "ident":
                if self.t[self.i - 1][1] == "const" and self.t[self.i - 1][0] == "ident":
                    pass
                else:
                    self.i += 1
                    while self.at(".") and self.peek(1)[0] == "ident":
                        self.i += 2
                    if self.at("<"):
                        self.skip_balanced()
            while self.at("[") :
                self.skip_balanced()
            if self.at("|") or self.at("&"):
                self.i += 1
                continue
            return

    # grammar
    def expr(self):
        node = self.arrow_or_unary()
        while True:
            tk = self.peek()
            if tk[0] == "p" and tk[1] in ("||", "&&", "??", "===", "!==", "==", "!=", "+"):
                self.i += 1
                node = ("bin", tk[1], node, self.arrow_or_unary())
            elif tk[0] == "p" and tk[1] == "?" :
                self.i += 1
                a = self.expr()
                self.eat(":")
                node = ("cond", node, a, self.expr())
            else:
                return node

    def _is_arrow(self) -> bool:
        if self.peek()[0] == "ident" and self.at("=>", k=1):
            return True
        if self.peek()[0] == "ident" and self.peek()[1] == "async":
            return True
        if not self.at("("):
            return False
        j, depth = self.i, 0
        while j < len(self.t):
            v = self.t[j]
            if v[0] == "p" and v[1] in ("(", "[", "{"):
                depth += 1
            elif v[0] == "p" and v[1] in (")", "]", "}"):
                depth -= 1
                if depth == 0:
                    nxt = self.t[j + 1] if j + 1 < len(self.t) else ("eof", None, 0)
                    return nxt[1] in ("=>", ":") and nxt[0] == "p"
            j += 1
        return False

    def arrow_or_unary(self):
        if self._is_arrow():
            return self.arrow()
        if self.at("!"):
            self.i += 1
            return ("not", self.arrow_or_unary())
        if self.at("-"):
            self.i += 1
            return ("neg", self.arrow_or_unary())
        return self.postfix()

    def arrow(self):
        if self.peek()[1] == "async":
            self.i += 1
        params: list[str] = []
        if self.peek()[0] == "ident":
            params.append(self.peek()[1])
            self.i += 1
        else:
            self.eat("(")
            while not self.at(")"):
                if self.at("{"):  # destructuring
                    start = self.i
                    self.skip_balanced()
                    names = [t[1] for t in self.t[start:self.i] if t[0] == "ident"]
                    params.append(("destructure", names))
                else:
                    if self.at("..."):
                        self.i += 1
                    params.append(self.peek()[1])
                    self.i += 1
                if self.at("?"):
                    self.i += 1
                if self.at(":"):
                    self.i += 1
                    self.skip_type()
                if self.at("="):
                    self.i += 1
                    self.expr()
                if self.at(","):
                    self.i += 1
            self.eat(")")
            if self.at(":"):
                self.i += 1
                self.skip_type()
        self.eat("=>")
        if self.at("{"):
            start = self.i
            self.skip_balanced()
            end = self.i
            j, depth = start, 0
            while j < end:
                v = self.t[j]
                if v[0] == "p" and v[1] in ("{", "(", "["):
                    depth += 1
                elif v[0] == "p" and v[1] in ("}", ")", "]"):
                    depth -= 1
                elif v[0] == "ident" and v[1] == "return" and depth == 1:
                    body = Parser(self.t, j + 1).expr()
                    return ("arrow", params, body)
                j += 1
            return ("arrow", params, ("lit", None))
        return ("arrow", params, self.expr())

    def postfix(self):
        node = self.primary()
        while True:
            tk = self.peek()
            if tk[0] == "p" and tk[1] in (".", "?."):
                self.i += 1
                name = self.peek()
                if name[0] != "ident":
                    raise TsError(f"line {name[2]}: bad member")
                self.i += 1
                node = ("member", node, ("lit", name[1]))
            elif tk[0] == "p" and tk[1] == "[":
                self.i += 1
                idx = self.expr()
                self.eat("]")
                node = ("member", node, idx)
            elif tk[0] == "p" and tk[1] == "(":
                node = ("call", node, self.args())
            elif tk[0] == "p" and tk[1] == "!":
                self.i += 1
            elif tk[0] == "ident" and tk[1] in ("as", "satisfies"):
                self.i += 1
                self.skip_type()
            elif tk[0] == "p" and tk[1] == "<" and node[0] in ("ident", "member"):
                # generic call f<T>(...)
                save = self.i
                try:
                    self.skip_balanced()
                    if not self.at("("):
                        raise TsError("not a generic call")
                except TsError:
                    self.i = save
                    return node
            else:
                return node

    def args(self) -> list:
        self.eat("(")
        out = []
        while not self.at(")"):
            if self.at("..."):
                self.i += 1
                out.append(("spread", self.expr()))
            else:
                out.append(self.expr())
            if self.at(","):
                self.i += 1
        self.eat(")")
        return out

    def primary(self):
        tk = self.peek()
        kind, v = tk[0], tk[1]
        if kind in ("str", "num", "tmpl"):
            self.i += 1
            return ("lit", v)
        if kind == "regex":
            self.i += 1
            return ("lit", v)
        if kind == "ident":
            if v == "new":
                self.i += 1
                callee = self.peek()[1]
                self.i += 1
                if self.at("<"):
                    self.skip_balanced()
                return ("new", callee, self.args() if self.at("(") else [])
            if v == "typeof":
                self.i += 1
                return ("lit", None)
            self.i += 1
            return ("ident", v)
        if kind == "p" and v == "(":
            self.i += 1
            e = self.expr()
            self.eat(")")
            return e
        if kind == "p" and v == "[":
            self.i += 1
            items = []
            while not self.at("]"):
                if self.at("..."):
                    self.i += 1
                    items.append(("spread", self.expr()))
                else:
                    items.append(self.expr())
                if self.at(","):
                    self.i += 1
            self.eat("]")
            return ("array", items)
        if kind == "p" and v == "{":
            self.i += 1
            props = []
            while not self.at("}"):
                if self.at("..."):
                    self.i += 1
                    props.append(("spread", self.expr()))
                else:
                    k = self.peek()
                    if k[0] == "p" and k[1] == "[":
                        self.i += 1
                        key = self.expr()
                        self.eat("]")
                    else:
                        self.i += 1
                        key = ("lit", k[1])
                    if self.at(":"):
                        self.i += 1
                        props.append(("prop", key, self.expr()))
                    elif self.at("("):  # method shorthand: skip body
                        self.skip_balanced()
                        if self.at(":"):
                            self.i += 1
                            self.skip_type()
                        self.skip_balanced()
                        props.append(("prop", key, ("lit", None)))
                    else:
                        props.append(("prop", key, ("ident", k[1])))
                if self.at(","):
                    self.i += 1
            self.eat("}")
            return ("object", props)
        raise TsError(f"line {tk[2]}: unexpected {v!r}")


def evaluate(node, env: Callable[[str], Any]) -> Any:
    op = node[0]
    if op == "lit":
        return node[1]
    if op == "ident":
        return env(node[1])
    if op == "array":
        out: list = []
        for it in node[1]:
            if it[0] == "spread":
                out.extend(evaluate(it[1], env))
            else:
                out.append(evaluate(it, env))
        return out
    if op == "object":
        d: dict = {}
        for p in node[1]:
            if p[0] == "spread":
                d.update(evaluate(p[1], env))
            else:
                key = evaluate(p[1], env)
                try:
                    d[key] = evaluate(p[2], env)
                except (TsError, TypeError, KeyError, IndexError, AttributeError, ValueError) as e:
                    d[key] = Unresolved(f"{type(e).__name__}: {e}")
        return d
    if op == "member":
        obj = evaluate(node[1], env)
        if isinstance(obj, Unresolved):
            raise TsError(obj.why)
        return _member(obj, evaluate(node[2], env))
    if op == "call":
        fn = evaluate(node[1], env)
        args: list = []
        for a in node[2]:
            if a[0] == "spread":
                args.extend(evaluate(a[1], env))
            else:
                args.append(evaluate(a, env))
        if not callable(fn):
            raise TsError(f"not callable: {node[1]}")
        return fn(*args)
    if op == "new":
        args = [evaluate(a, env) for a in node[2]]
        return _new(node[1], args)
    if op == "arrow":
        params, body = node[1], node[2]

        def fn(*args):
            local: dict = {}
            for i, p in enumerate(params):
                val = args[i] if i < len(args) else None
                if isinstance(p, tuple):
                    for nm in p[1]:
                        local[nm] = (val or {}).get(nm) if isinstance(val, dict) else None
                else:
                    local[p] = val
            return evaluate(body, lambda n: local[n] if n in local else env(n))
        return fn
    if op == "bin":
        a = evaluate(node[2], env)
        o = node[1]
        if o == "||":
            return a or evaluate(node[3], env)
        if o == "&&":
            return a and evaluate(node[3], env)
        if o == "??":
            return a if a is not None else evaluate(node[3], env)
        b = evaluate(node[3], env)
        if o == "+":
            if isinstance(a, str) or isinstance(b, str):
                return f"{'' if a is None else a}{'' if b is None else b}"
            return a + b
        return {"===": a == b, "==": a == b, "!==": a != b, "!=": a != b}[o]
    if op == "cond":
        return evaluate(node[2] if evaluate(node[1], env) else node[3], env)
    if op == "not":
        return not evaluate(node[1], env)
    if op == "neg":
        return -evaluate(node[1], env)
    raise TsError(f"cannot evaluate {op}")


class Unresolved:
    def __init__(self, why: str):
        self.why = why

    def __repr__(self):
        return f"Unresolved({self.why})"


# --------------------------------------------------------------------------------------
# Modules
# --------------------------------------------------------------------------------------
class Module:
    cache: dict[Path, Module] = {}

    def __init__(self, path: Path):
        self.path = path
        self.decls: dict[str, Any] = {}       # name -> AST | ("import", ...) | Unresolved
        self.values: dict[str, Any] = {}
        self.exports: set[str] = set()
        self._parse()

    @classmethod
    def load(cls, path: Path) -> Module:
        path = path.resolve()
        if path not in cls.cache:
            cls.cache[path] = Module(path)
        return cls.cache[path]

    def _resolve_import(self, spec: str):
        if spec == "zod":
            return {"z": _zod_builtin()}
        if spec.endswith("/zx"):
            return {"zx": ZX}
        if not spec.startswith("."):
            return None
        base = (self.path.parent / spec)
        for cand in (base.with_suffix(".ts") if base.suffix != ".ts" else base, base / "index.ts"):
            if cand.exists():
                return cand
        return None

    def _parse(self) -> None:
        toks = tokenize(self.path.read_text())
        p = Parser(toks)
        n = len(toks)

        def statement_start(j: int) -> bool:
            return j == 0 or toks[j][2] != toks[j - 1][2]

        while p.i < n:
            tk = p.peek()
            if tk[0] == "ident" and tk[1] == "import" and statement_start(p.i):
                p.i += 1
                if p.peek()[0] == "str":  # side-effect import
                    p.i += 1
                    continue
                if p.peek()[1] == "type":
                    p.i += 1
                bindings: list[tuple[str, str]] = []  # (local, imported | "*" | "default")
                while not (p.peek()[0] == "ident" and p.peek()[1] == "from"):
                    t = p.peek()
                    if t[1] == "*":
                        p.i += 1
                        p.eat("as")
                        bindings.append((p.peek()[1], "*"))
                        p.i += 1
                    elif t[1] == "{":
                        p.i += 1
                        while not p.at("}"):
                            if p.peek()[1] == "type":
                                p.i += 1
                            imported = p.peek()[1]
                            p.i += 1
                            local = imported
                            if p.peek()[1] == "as":
                                p.i += 1
                                local = p.peek()[1]
                                p.i += 1
                            bindings.append((local, imported))
                            if p.at(","):
                                p.i += 1
                        p.eat("}")
                    elif t[0] == "ident":
                        bindings.append((t[1], "default"))
                        p.i += 1
                    elif t[1] == ",":
                        p.i += 1
                    else:
                        raise TsError(f"{self.path}: bad import at line {t[2]}")
                p.i += 1  # from
                spec = p.peek()[1]
                p.i += 1
                for local, imported in bindings:
                    self.decls[local] = ("import", spec, imported)
                continue
            if tk[0] == "ident" and tk[1] == "export" and statement_start(p.i):
                p.i += 1
                if p.peek()[1] == "default":
                    p.i += 1
                    self._decl(p, "default")
                    self.exports.add("default")
                    continue
                if p.peek()[1] in ("const", "let", "var"):
                    p.i += 1
                    name = p.peek()[1]
                    p.i += 1
                    self.exports.add(name)
                    self._decl_after_name(p, name)
                    continue
                continue
            if tk[0] == "ident" and tk[1] in ("const", "let", "var") and statement_start(p.i):
                p.i += 1
                name = p.peek()[1]
                p.i += 1
                self._decl_after_name(p, name)
                continue
            p.i += 1

    def _decl_after_name(self, p: Parser, name: str) -> None:
        if p.at(":"):
            p.i += 1
            p.skip_type()
        if not p.at("="):
            return
        p.i += 1
        self._decl(p, name)

    def _decl(self, p: Parser, name: str) -> None:
        start = p.i
        try:
            self.decls[name] = p.expr()
        except TsError as e:
            self.decls[name] = Unresolved(f"{self.path.name}: {e}")
            p.i = start + 1

    def get(self, name: str) -> Any:
        if name in self.values:
            return self.values[name]
        if name in BUILTINS:
            return BUILTINS[name]
        if name not in self.decls:
            raise TsError(f"{self.path.name}: unknown name {name!r}")
        d = self.decls[name]
        if isinstance(d, Unresolved):
            raise TsError(d.why)
        if isinstance(d, tuple) and d and d[0] == "import":
            _, spec, imported = d
            target = self._resolve_import(spec)
            if target is None:
                val: Any = Opaque(spec)
            elif isinstance(target, dict):
                val = target.get(imported, Opaque(f"{spec}.{imported}"))
            else:
                mod = Module.load(target)
                val = Namespace(mod) if imported == "*" else mod.get(imported)
        else:
            try:
                val = evaluate(d, self.get)
            except TsError:
                raise
            except (TypeError, KeyError, IndexError, AttributeError, ValueError) as e:
                raise TsError(f"{self.path.name}: {name}: {type(e).__name__}: {e}") from e
        self.values[name] = val
        return val


# --------------------------------------------------------------------------------------
# Build the map
# --------------------------------------------------------------------------------------
def _git(dk: Path, *args: str) -> str:
    try:
        return subprocess.run(["git", "-C", str(dk), *args], capture_output=True, text=True,
                              check=True).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "unknown"


def _registry_sources(path: Path) -> dict[str, str]:
    """protocol key -> source folder, from `import * as X from "./a/b/schema"` and `key: X.chain`."""
    txt = path.read_text()
    imports = dict(re.findall(r'import \* as (\w+) from "\./([^"]+)/schema"', txt))
    out = {}
    for key, alias in re.findall(r"^\s+(\w+): (\w+)\.\w+,", txt, re.MULTILINE):
        if alias in imports:
            out[key] = f"{path.parent.relative_to(path.parents[2])}/{imports[alias]}"
    return out


def _describe_actions(actions: Any) -> dict:
    out = {}
    if not isinstance(actions, dict):
        return {"_unresolved": {"error": repr(actions)}}
    for aname, schema in actions.items():
        if isinstance(schema, Unresolved):
            out[aname] = {"unresolved": schema.why}
            continue
        if not isinstance(schema, Z) or schema.kind != "object":
            out[aname] = {"unresolved": f"not a z.object: {schema!r}"[:200]}
            continue
        params = {}
        for pname, pz in (schema.kw.get("shape") or {}).items():
            params[pname] = pz.describe() if isinstance(pz, Z) else {"unresolved": repr(pz)[:200]}
        out[aname] = {"params": params}
    return out


def build_map(dk: Path) -> dict:
    sdk = dk / "sdk"
    pkg = json.loads((sdk / "package.json").read_text())
    Module.cache.clear()
    groups = [("protocol", sdk / "src/protocols/schema.ts"), ("bridge", sdk / "src/bridges/schema.ts"),
              ("repertoire", sdk / "src/repertoire/schema.ts")]
    entries: dict[tuple[str, str], dict] = {}
    for kind, path in groups:
        mod = Module.load(path)
        sources = _registry_sources(path)
        for chain in CHAINS:
            try:
                reg = mod.get(chain)
            except TsError:
                continue
            for key, actions in reg.items():
                e = entries.setdefault((kind, key), {"key": key, "kind": kind, "source": sources.get(key),
                                                     "chains": [], "actions": {}})
                e["chains"].append(chain)
                for aname, desc in _describe_actions(actions).items():
                    a = e["actions"].setdefault(aname, {"action": aname, "chains": [], "params": None})
                    a["chains"].append(chain)
                    if a["params"] is None or chain == "eth":
                        a["params"] = desc.get("params")
                        if "unresolved" in desc:
                            a["unresolved"] = desc["unresolved"]
                        a["params_from"] = chain
    protocols = []
    for (kind, key), e in sorted(entries.items(), key=lambda kv: ({"protocol": 0, "bridge": 1, "repertoire": 2}[kv[0][0]], kv[0][1])):
        acts = []
        for aname, a in e["actions"].items():
            if kind == "protocol":
                m = CARD_MAP.get((key, aname))
                a["defios"] = dict(m) if m else None
            # option lists are kept for Ethereum mainnet only
            if a.get("params_from") != "eth":
                for p in (a.get("params") or {}).values():
                    p.pop("options", None)
            acts.append(a)
        e["actions"] = acts
        cards = sorted({a["defios"]["card"] for a in acts if a.get("defios")})
        e["defios_cards"] = cards
        protocols.append(e)
    try:
        gearbox_pools = Module.load(sdk / "src/protocols/gearbox/_ethInfo.ts").get("default")
        gearbox_pools = [{k: gp.get(k) for k in ("id", "name", "symbol", "curator", "asset")} for gp in gearbox_pools]
    except (TsError, OSError):
        gearbox_pools = []
    return {
        "schema": "defios.defi_kit_map/1",
        "generated_by": "scripts/defikit_map.py (do not edit by hand; re-run the script)",
        "defi_kit": {
            "repo": "https://github.com/karpatkey/defi-kit",
            "package": pkg.get("name"),
            "version": pkg.get("version"),
            "commit": _git(dk, "rev-parse", "HEAD"),
            "commit_date": _git(dk, "log", "-1", "--format=%cI"),
        },
        "chains": CHAINS,
        "protocols": protocols,
        "not_covered": NOT_COVERED,
        "gearbox_pools": gearbox_pools,
    }


def scan_card_addresses(dk: Path, catalog_dir: Path) -> dict:
    """For each card contract address: which defi-kit source files mention it (case-insensitive)."""
    import yaml
    files = [p for p in (dk / "sdk/src").rglob("*.ts") if "test" not in p.name] + [dk / "sdk/eth-sdk/config.ts"]
    texts = {p: p.read_text().lower() for p in files if p.exists()}
    out: dict[str, dict] = {}
    for card_path in sorted(catalog_dir.glob("*/card.yaml")):
        card = yaml.safe_load(card_path.read_text())
        res = {}
        for name, c in (card.get("contracts") or {}).items():
            addr = c.get("address") if isinstance(c, dict) else None
            if not isinstance(addr, str) or not re.fullmatch(r"0x[0-9a-fA-F]{40}", addr):
                continue
            hits = sorted(str(p.relative_to(dk)) for p, t in texts.items() if addr.lower() in t)
            res[name] = {"address": addr, "in_defi_kit": hits[:6], "more": max(0, len(hits) - 6)}
        out[card["protocol"]] = res
    return out


# --------------------------------------------------------------------------------------
# Card `defi_kit:` checks (work from the committed JSON; no defi-kit clone needed)
# --------------------------------------------------------------------------------------
_REF = re.compile(r"^(\w+)/(\w+)((?:\s+\w+=(?:\[[^\]]*\]|\S+))*)\s*(\(.*\))?\s*$")


def parse_ref(s: str) -> dict | None:
    m = _REF.match(s.strip())
    if not m:
        return None
    params = {}
    for k, v in re.findall(r"(\w+)=(\[[^\]]*\]|\S+)", m.group(3) or ""):
        vals = [x.strip() for x in v.strip("[]").split(",")] if v.startswith("[") else [v]
        params[k] = [x for x in vals if x]
    return {"protocol": m.group(1), "action": m.group(2), "params": params, "note": m.group(4)}


def _placeholder(v: str) -> bool:
    return any(ch in v for ch in "…<>") or v in ("...", "*")


def check_ref(dk_map: dict, ref: str, chain: str = "eth") -> list[str]:
    r = parse_ref(ref)
    if not r:
        return [f"cannot parse {ref!r} (expected '<protocol>/<action> [param=[v,…]]')"]
    proto = next((p for p in dk_map["protocols"] if p["key"] == r["protocol"] and p["kind"] == "protocol"), None)
    if not proto:
        keys = sorted(p["key"] for p in dk_map["protocols"] if p["kind"] == "protocol")
        return [f"defi-kit has no protocol {r['protocol']!r} (known: {', '.join(keys)})"]
    if chain not in proto["chains"]:
        return [f"{r['protocol']} is not available on {chain}"]
    act = next((a for a in proto["actions"] if a["action"] == r["action"]), None)
    if not act:
        return [f"{r['protocol']} has no action {r['action']!r} (has: {', '.join(a['action'] for a in proto['actions'])})"]
    errs = []
    params = act.get("params") or {}
    for k, vals in r["params"].items():
        if k not in params:
            errs.append(f"{r['protocol']}/{r['action']} has no parameter {k!r} (has: {', '.join(params)})")
            continue
        opts = params[k].get("options")
        if opts is None:
            continue
        low = {str(o).lower() for o in opts}
        for v in vals:
            if not _placeholder(v) and v.lower() not in low:
                errs.append(f"{r['protocol']}/{r['action']} {k}={v} is not in defi-kit's option list")
    return errs


def check_cards(dk_map: dict, catalog_dir: Path) -> list[dict]:
    import yaml
    out = []
    for card_path in sorted(catalog_dir.glob("*/card.yaml")):
        card = yaml.safe_load(card_path.read_text())
        refs = []
        if card.get("defi_kit"):
            refs.append(("defi_kit", card["defi_kit"]))
        for name, c in (card.get("contracts") or {}).items():
            if isinstance(c, dict) and c.get("defi_kit"):
                refs.append((f"contracts.{name}.defi_kit", c["defi_kit"]))
        for field, ref in refs:
            chain = {v: k for k, v in CHAINS.items()}.get(card.get("chain_id", 1), "eth")
            errs = check_ref(dk_map, str(ref), chain)
            out.append({"card": card["protocol"], "field": field, "value": ref, "ok": not errs, "problems": errs})
        # a carded protocol that defi-kit covers should say so
        mapped = sorted({f"{k[0]}/{k[1]}" for k, m in CARD_MAP.items() if m["card"] == card["protocol"]})
        if mapped and not card.get("defi_kit"):
            out.append({"card": card["protocol"], "field": "defi_kit", "value": None, "ok": False,
                        "problems": [f"defi-kit covers it ({', '.join(mapped)}); add a `defi_kit:` field"]})
    return out


# --------------------------------------------------------------------------------------
# Markdown
# --------------------------------------------------------------------------------------
def _short(v: Any) -> str:
    s = str(v)
    return s[:6] + "…" + s[-4:] if re.fullmatch(r"0x[0-9a-fA-F]{40}", s) else s


def _cell(s: Any) -> str:
    return str(s).replace("|", "\\|")


def _param_md(name: str, p: dict) -> str:
    if "unresolved" in p:
        return f"`{name}`: unresolved"
    s = f"`{name}`{'?' if p.get('optional') else ''}: {p['type']}"
    if "default" in p:
        s += f" = {json.dumps(p['default'])}"
    if "options_count" in p:
        named = [o for o in p.get("options", []) if not re.fullmatch(r"0x[0-9a-fA-F]+", str(o))]
        ex = ", ".join(_short(x) for x in (named or p.get("options", []))[:4])
        s += f" ({p['options_count']} options{': ' + ex + ', …' if ex else ''})"
    return s


def render_md(m: dict) -> str:
    dk = m["defi_kit"]
    L = [
        "# defi-kit coverage map",
        "",
        f"Generated by `scripts/defikit_map.py` from [karpatkey/defi-kit]({dk['repo']}) "
        f"**v{dk['version']}** at commit `{dk['commit']}` ({dk['commit_date']}). Do not edit by hand.",
        "Full option lists (every pool / vault / token defi-kit accepts on Ethereum mainnet) are in "
        "[`defi_kit.json`](defi_kit.json).",
        "",
        "How to read it: a defi-kit *action* is a permission bundle (`allow.<protocol>.<action>(params)`) that "
        "returns Roles permissions. `params` are the bundle's options; `?` = optional. The DeFi_os column names "
        "the card (and card actions) that describe the same calls, or *no card yet*.",
        "",
        "Chains: " + ", ".join(f"`{k}` ({v})" for k, v in m["chains"].items()) + ".",
        "",
        "## Protocols on Ethereum mainnet",
        "",
        "| protocol | action | chains | params (mainnet) | DeFi_os card |",
        "|---|---|---|---|---|",
    ]
    others = []
    for p in m["protocols"]:
        if p["kind"] != "protocol":
            continue
        if "eth" not in p["chains"]:
            others.append(p)
            continue
        for a in p["actions"]:
            params = a.get("params") or {}
            ptxt = "<br>".join(_cell(_param_md(k, v)) for k, v in params.items()) or "none"
            if a.get("unresolved"):
                ptxt = "unresolved: " + a["unresolved"]
            d = a.get("defios")
            card = f"**{d['card']}**: {', '.join(d['actions'])}" if d else "no card yet"
            L.append(f"| `{p['key']}` | `{a['action']}` | {', '.join(a['chains'])} | {ptxt} | {card} |")
    if others:
        L += ["", "## Protocols not on Ethereum mainnet", "", "| protocol | actions | chains |", "|---|---|---|"]
        for p in others:
            L.append(f"| `{p['key']}` | {', '.join(a['action'] for a in p['actions'])} | {', '.join(p['chains'])} |")
    L += ["", "## Bridges and repertoire", "",
          "Bridges are permission bundles like protocols. The *repertoire* builds plain transactions "
          "(not permissions) for common steps.", "",
          "| kind | key | action | chains | params (mainnet) |", "|---|---|---|---|---|"]
    for p in m["protocols"]:
        if p["kind"] == "protocol":
            continue
        for a in p["actions"]:
            ptxt = "<br>".join(_cell(_param_md(k, v)) for k, v in (a.get("params") or {}).items()) or "none"
            L.append(f"| {p['kind']} | `{p['key']}` | `{a['action']}` | {', '.join(a['chains'])} | {ptxt} |")

    L += ["", "## DeFi_os cards mapped to defi-kit", ""]
    for (proto, action), mm in CARD_MAP.items():
        L.append(f"* **{mm['card']}** ← `{proto}/{action}`: {mm['note']}")
    pools = m.get("gearbox_pools") or []
    if pools:
        L += ["", "### Gearbox pools accepted by `gearbox/deposit` (mainnet)", "",
              "| pool (target) | name | symbol | asset | curator |", "|---|---|---|---|---|"]
        for gp in pools:
            L.append(f"| `{gp.get('id')}` | {_cell(gp.get('name'))} | {_cell(gp.get('symbol'))} | "
                     f"{_cell((gp.get('asset') or {}).get('symbol'))} | {_cell(gp.get('curator', ''))} |")

    L += ["", "## DeFi_os protocols defi-kit does not cover", "", "| DeFi_os card | status |", "|---|---|"]
    for k, v in m["not_covered"].items():
        L.append(f"| {k} | {v} |")

    addr = m.get("card_addresses") or {}
    if addr:
        L += ["", "## Card addresses seen in defi-kit sources", "",
              "A hit means the address appears somewhere in defi-kit's `sdk/src` or `eth-sdk/config.ts` "
              "(as a contract, token or pool); it is a second source, not a review.", "",
              "| card | contract | address | defi-kit files |", "|---|---|---|---|"]
        for card, cs in addr.items():
            for name, c in cs.items():
                files = ", ".join(f"`{f}`" for f in c["in_defi_kit"]) or "none"
                if c.get("more"):
                    files += f" (+{c['more']})"
                L.append(f"| {card} | {name} | `{c['address']}` | {files} |")

    checks = m.get("card_checks") or []
    if checks:
        L += ["", "## Card `defi_kit:` field check", "", "| card | field | value | result |", "|---|---|---|---|"]
        for c in checks:
            res = "ok" if c["ok"] else _cell("; ".join(c["problems"]))
            L.append(f"| {c['card']} | `{c['field']}` | `{_cell(c['value'])}` | {res} |")
    L.append("")
    return "\n".join(L)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--defi-kit", default=str(REPO.parent / "ext" / "karpatkey_defi-kit"),
                    help="path to a karpatkey/defi-kit clone")
    ap.add_argument("--catalog", default=str(REPO / "catalog"))
    ap.add_argument("--check", action="store_true",
                    help="only check card defi_kit fields against the committed catalog/defi_kit.json")
    args = ap.parse_args(argv)
    catalog = Path(args.catalog)
    if args.check:
        m = json.loads((catalog / "defi_kit.json").read_text())
        checks = check_cards(m, catalog)
        bad = [c for c in checks if not c["ok"]]
        for c in checks:
            print(f"{'ok ' if c['ok'] else 'BAD'} {c['card']}.{c['field']}: {c['value']}"
                  + ("" if c["ok"] else "  -> " + "; ".join(c["problems"])))
        return 1 if bad else 0
    dk = Path(args.defi_kit)
    if not (dk / "sdk" / "package.json").exists():
        print(f"no defi-kit clone at {dk}", file=sys.stderr)
        return 2
    m = build_map(dk)
    m["card_addresses"] = scan_card_addresses(dk, catalog)
    m["card_checks"] = check_cards(m, catalog)
    (catalog / "defi_kit.json").write_text(json.dumps(m, indent=1, default=str) + "\n")
    (catalog / "DEFI_KIT.md").write_text(render_md(m))
    n_act = sum(len(p["actions"]) for p in m["protocols"])
    unresolved = [f"{p['key']}/{a['action']}" for p in m["protocols"] for a in p["actions"] if a.get("unresolved")
                  or any("unresolved" in v for v in (a.get("params") or {}).values())]
    bad = [c for c in m["card_checks"] if not c["ok"]]
    print(f"defi-kit v{m['defi_kit']['version']} @ {m['defi_kit']['commit'][:10]}: {len(m['protocols'])} entries, "
          f"{n_act} actions; unresolved: {unresolved or 'none'}; card check problems: {len(bad)}")
    for c in bad:
        print(f"  {c['card']}.{c['field']}: {'; '.join(c['problems'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
