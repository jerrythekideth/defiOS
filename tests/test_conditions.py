from eth_abi import encode
from eth_utils import keccak

from defios.roles.conditions import (
    AbiType,
    Condition,
    Evaluator,
    Operator,
    Status,
    constrains_param,
    flatten,
    normalize_flat,
    unflatten,
)
from defios.roles.v2 import decode_log

AVATAR = "0xa11ce0000000000000000000000000000000a11c"
ATTACKER = "0x000000000000000000000000000000000000dEaD"
WSTETH = "0x7f39C581F595B53c5cb19bD0b3f8dA6c935E2Ca0"
POOL = "0xA9d17f6D3285208280a1Fd9B94479c62e0AABa64"


def sel(sig):
    return keccak(text=sig)[:4]


def word(addr):
    return bytes.fromhex(addr[2:]).rjust(32, b"\0")


def static(op, cv=b""):
    return Condition(AbiType.STATIC, op, cv)


def calldata_root(*children):
    return Condition(AbiType.CALLDATA, Operator.MATCHES, b"", list(children))


def scope_functions(fixture_logs):
    """(target, selector) -> flat conditions, last write wins (as on chain)."""
    out = {}
    for lg in fixture_logs["logs"]:
        name, v = decode_log(lg)
        if name == "ScopeFunction":
            out[(v["targetAddress"].lower(), "0x" + v["selector"].hex())] = v["conditions"]
    return out


def test_flat_tree_roundtrip(fixture_logs):
    scopes = scope_functions(fixture_logs)
    assert len(scopes) > 20
    for flat in scopes.values():
        tree = unflatten(flat)
        assert flatten(tree) == normalize_flat(flat)


def test_weth_approve_spender_list(fixture_logs):
    flat = scope_functions(fixture_logs)[("0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2", "0x095ea7b3")]
    tree = unflatten(flat)
    assert tree.param_type == AbiType.CALLDATA and tree.operator == Operator.MATCHES
    spender = tree.children[0]
    assert spender.operator == Operator.OR
    js = tree.to_json()
    values = [c["value"] for c in js["children"][0]["children"]]
    assert "0xC92E8bdf79f0507f65a392b0ab4667716BFE0110" in values  # CoW relayer
    ev = Evaluator(AVATAR)
    data = sel("approve(address,uint256)") + encode(["address", "uint256"],
                                                    ["0xC92E8bdf79f0507f65a392b0ab4667716BFE0110", 10**18])
    assert ev.check(tree, data) == Status.Ok
    bad = sel("approve(address,uint256)") + encode(["address", "uint256"], [ATTACKER, 10**18])
    assert ev.check(tree, bad) == Status.OrViolation


def test_none_pass_node_shifts_params(fixture_logs):
    """The 062026 Gearbox redeem scope: [None Pass, EqualTo(vault), EqualTo(vault)].
    In the deployed decoder a None leaf takes no slot, so the checks land on
    (shares, receiver) instead of (receiver, owner): the legit call is blocked."""
    flat = scope_functions(fixture_logs)[(POOL.lower(), "0xba087652")]
    tree = unflatten(flat)
    assert tree.children[0].param_type == AbiType.NONE
    redeem = sel("redeem(uint256,address,address)")
    legit = redeem + encode(["uint256", "address", "address"], [10**18, AVATAR, AVATAR])
    assert Evaluator(AVATAR).check(tree, legit) == Status.ParameterNotAllowed
    # what it actually accepts: shares == uint(vault), receiver == vault, owner anything
    weird = redeem + encode(["uint256", "address", "address"], [int(AVATAR, 16), AVATAR, ATTACKER])
    assert Evaluator(AVATAR).check(tree, weird) == Status.Ok
    # under slot-consuming semantics the intended call would pass
    assert Evaluator(AVATAR, none_consumes_slot=True).check(tree, legit) == Status.Ok


def test_equal_to_avatar_and_pass():
    tree = calldata_root(static(Operator.PASS), static(Operator.EQUAL_TO_AVATAR))
    f = sel("deposit(uint256,address)")
    ev = Evaluator(AVATAR)
    assert ev.check(tree, f + encode(["uint256", "address"], [5, AVATAR])) == Status.Ok
    assert ev.check(tree, f + encode(["uint256", "address"], [5, ATTACKER])) == Status.ParameterNotAllowed
    assert constrains_param(tree, 1) and not constrains_param(tree, 0)


def test_comparisons_and_nor():
    tree = calldata_root(
        Condition(AbiType.NONE, Operator.AND, b"", [
            static(Operator.GREATER_THAN, (10).to_bytes(32, "big")),
            static(Operator.LESS_THAN, (100).to_bytes(32, "big")),
        ]),
        Condition(AbiType.NONE, Operator.NOR, b"", [static(Operator.EQUAL_TO, word(ATTACKER))]),
    )
    f = sel("f(uint256,address)")
    ev = Evaluator(AVATAR)
    assert ev.check(tree, f + encode(["uint256", "address"], [50, AVATAR])) == Status.Ok
    assert ev.check(tree, f + encode(["uint256", "address"], [5, AVATAR])) == Status.ParameterLessThanAllowed
    assert ev.check(tree, f + encode(["uint256", "address"], [500, AVATAR])) == Status.ParameterGreaterThanAllowed
    assert ev.check(tree, f + encode(["uint256", "address"], [50, ATTACKER])) == Status.NorViolation


def test_array_of_calls_every():
    """Gearbox-style calls[]: every element's target must be the facade."""
    facade = "0x9515AB9BB73A9642F1a93Ba7C2790e9d08227f9a"
    elem = Condition(AbiType.TUPLE, Operator.MATCHES, b"", [
        static(Operator.EQUAL_TO, word(facade)),
        Condition(AbiType.DYNAMIC, Operator.PASS),
    ])
    tree = calldata_root(static(Operator.EQUAL_TO_AVATAR),
                         Condition(AbiType.ARRAY, Operator.ARRAY_EVERY, b"", [elem]))
    f = sel("multicall(address,(address,bytes)[])")
    ev = Evaluator(AVATAR)
    ok = f + encode(["address", "(address,bytes)[]"], [AVATAR, [(facade, b"\x01\x02"), (facade, b"")]])
    bad = f + encode(["address", "(address,bytes)[]"], [AVATAR, [(facade, b""), (ATTACKER, b"\xde\xad")]])
    assert ev.check(tree, ok) == Status.Ok
    assert ev.check(tree, bad) == Status.NotEveryArrayElementPasses


def test_dynamic_equal_to_and_bitmask():
    payload = b"hello world"
    cv = encode(["bytes"], [payload])[32:]  # compValue = abi encoding without the head offset
    tree = calldata_root(Condition(AbiType.DYNAMIC, Operator.EQUAL_TO, cv))
    f = sel("g(bytes)")
    ev = Evaluator(AVATAR)
    assert ev.check(tree, f + encode(["bytes"], [payload])) == Status.Ok
    assert ev.check(tree, f + encode(["bytes"], [b"hello there"])) == Status.ParameterNotAllowed
    # bitmask on a bytes4 static: shift 0, mask ffffffff, expected 095ea7b3
    mask = (0).to_bytes(2, "big") + bytes.fromhex("ffffffff").ljust(15, b"\0") + bytes.fromhex("095ea7b3").ljust(15, b"\0")
    tree2 = calldata_root(Condition(AbiType.STATIC, Operator.BITMASK, mask))
    h = sel("h(bytes4)")
    assert ev.check(tree2, h + encode(["bytes4"], [bytes.fromhex("095ea7b3")])) == Status.Ok
    assert ev.check(tree2, h + encode(["bytes4"], [bytes.fromhex("a9059cbb")])) == Status.BitmaskNotAllowed


def test_tuple_field_scoping_cow_order(fixture_logs):
    """CoW signOrder scope from the weETH batch: order tuple with an Or on its 3rd child."""
    flat = scope_functions(fixture_logs)[("0x23da9ade38e4477b23770ded512fd37b12381fab", "0x569d3489")]
    tree = unflatten(flat)
    order = tree.children[0]
    assert order.param_type == AbiType.TUPLE and order.operator == Operator.MATCHES
    assert len(order.children) == 12
