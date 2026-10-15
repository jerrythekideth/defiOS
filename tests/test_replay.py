from eth_abi import encode
from eth_utils import keccak

from defios.logs import StaticLogs
from defios.roles.conditions import Clearance, ExecutionOptions, Status
from defios.roles.v2 import EVENTS, RolesV2, decode_log, event_signature, role_key_bytes32

from .conftest import AVATAR, OPERATOR, ROLES

RK = bytes.fromhex(role_key_bytes32("MANAGER_ROLE")[2:])
OTHER = bytes.fromhex(role_key_bytes32("OTHER_ROLE")[2:])
T = "0x1111111111111111111111111111111111111111"


def log(name, args, block, idx=0, indexed=False):
    spec = EVENTS[name]
    topics = ["0x" + keccak(text=event_signature(name)).hex()]
    if indexed:
        for (n, t, ix), a in zip(spec, args):
            if ix:
                topics.append("0x" + encode([t], [a]).hex())
        data_args = [(t, a) for (n, t, ix), a in zip(spec, args) if not ix]
    else:
        data_args = [(t, a) for (_, t, _), a in zip(spec, args)]
    return {"address": ROLES.lower(), "blockNumber": block, "transactionHash": f"0x{block:064x}",
            "transactionIndex": 0, "logIndex": idx, "topics": topics,
            "data": "0x" + encode([t for t, _ in data_args], [a for _, a in data_args]).hex()}


def test_role_key_encoding():
    assert role_key_bytes32("MANAGER_ROLE") == "0x4d414e414745525f524f4c450000000000000000000000000000000000000000"
    assert role_key_bytes32("0x01") == "0x01" + "0" * 62


def test_replay_fixture(role_state):
    adapter, st = role_state
    assert st.members == [OPERATOR]
    assert st.events_replayed > 100
    weth = st.target("0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2")
    assert weth.clearance == Clearance.FUNCTION
    pool = "0xA9d17f6D3285208280a1Fd9B94479c62e0AABa64"
    assert st.target(pool).clearance == Clearance.FUNCTION
    dep = st.function(pool, "0x6e553f65")
    assert dep.wildcarded  # allowFunction: any receiver
    redeem = st.function(pool, "0xba087652")
    assert not redeem.wildcarded and redeem.condition is not None
    # CoW signOrder was scoped twice; the second (options NONE) wins
    signer = st.function("0x23dA9AdE38E4477b23770DeD512fD37b12381FAB", "0x569d3489")
    assert signer.options == ExecutionOptions.NONE
    lock = st.to_lock_json()
    assert lock["schema"] == "defios.role.lock/1"
    assert pool.lower() in lock["targets"]
    assert "0x6e553f65" in lock["targets"][pool.lower()]["functions"]


def test_replay_semantics_revoke_and_dormant():
    logs = [
        log("AssignRoles", [OPERATOR, [RK, OTHER], [True, True]], 1),
        log("ScopeTarget", [RK, T], 2),
        log("AllowFunction", [RK, T, bytes.fromhex("aabbccdd"), 0], 3),
        log("ScopeFunction", [RK, T, bytes.fromhex("11223344"), [(0, 5, 5, b""), (0, 1, 15, b"")], 1], 4),
        log("AllowFunction", [OTHER, T, bytes.fromhex("deadbeef"), 0], 5),  # other role: ignored
        log("RevokeFunction", [RK, T, bytes.fromhex("aabbccdd")], 6),
        log("AllowTarget", [RK, T, 2], 7),  # functions stay stored but go dormant
        log("AssignRoles", [OPERATOR, [OTHER], [False]], 8),
    ]
    st = RolesV2(ROLES).read_role("MANAGER_ROLE", StaticLogs(logs))
    assert st.members == [OPERATOR]
    assert st.target(T).clearance == Clearance.TARGET
    assert st.target(T).options == ExecutionOptions.DELEGATE_CALL
    assert st.function(T, "0xaabbccdd") is None
    assert st.function(T, "0xdeadbeef") is None
    assert [f.selector for f in st.dormant_functions()] == ["0x11223344"]
    assert st.active_functions() == []
    assert set(st.roles_seen) == {"0x" + RK.hex(), "0x" + OTHER.hex()}
    # replay up to block 5 only: function still allowed, target still scoped
    st5 = RolesV2(ROLES).read_role("MANAGER_ROLE", StaticLogs(logs), to_block=5)
    assert st5.function(T, "0xaabbccdd").wildcarded
    assert st5.target(T).clearance == Clearance.FUNCTION


def test_decode_log_indexed_and_unindexed_layouts():
    owner = "0x2222222222222222222222222222222222222222"
    args = [owner, owner, AVATAR, AVATAR]
    a = decode_log(log("RolesModSetup", args, 1, indexed=True))
    b = decode_log(log("RolesModSetup", args, 1, indexed=False))
    assert a[0] == b[0] == "RolesModSetup"
    assert a[1]["avatar"].lower() == b[1]["avatar"].lower() == AVATAR


def test_check_call_offline(role_state):
    adapter, st = role_state
    pool = "0xA9d17f6D3285208280a1Fd9B94479c62e0AABa64"
    data = keccak(text="deposit(uint256,address)")[:4] + encode(["uint256", "address"], [1, T])
    assert adapter.check_call(st, pool, 0, data, 0, AVATAR).status == Status.Ok
    assert adapter.check_call(st, pool, 1, data, 0, AVATAR).status == Status.SendNotAllowed
    assert adapter.check_call(st, pool, 0, data, 1, AVATAR).status == Status.DelegateCallNotAllowed
    assert adapter.check_call(st, T, 0, data, 0, AVATAR).status == Status.TargetAddressNotAllowed
    other = keccak(text="transfer(address,uint256)")[:4] + encode(["address", "uint256"], [T, 1])
    assert adapter.check_call(st, pool, 0, other, 0, AVATAR).status == Status.FunctionNotAllowed


def test_exec_with_role_calldata_and_revert_decoding():
    from defios.roles.v2 import decode_revert

    cd = RolesV2(ROLES).exec_with_role_calldata("MANAGER_ROLE", T, 0, b"\x12\x34", 0)
    assert cd[:4] == keccak(text="execTransactionWithRole(address,uint256,bytes,uint8,bytes32,bool)")[:4]
    rev = bytes.fromhex("d0a9bf58") + encode(["uint8", "bytes32"], [7, b"\0" * 32])
    r = decode_revert(rev)
    assert not r.ok and r.error == "ConditionViolation" and r.status == "ParameterNotAllowed"
    assert decode_revert(bytes.fromhex("fd8e9f28")).error == "NoMembership"
