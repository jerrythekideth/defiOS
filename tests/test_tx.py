"""tx.py: MultiSend packing, execTransactionWithRole encoding, Operator tx shapes (offline)."""
import pytest
from eth_abi import decode
from eth_utils import keccak

from defios.tx import (
    CALL,
    DELEGATECALL,
    MULTISEND_CALL_ONLY_141,
    Call,
    TxError,
    build_inner,
    build_operator_tx,
    decode_multisend,
    encode_multisend,
    encode_multisend_payload,
    exec_with_role,
)

from .conftest import ROLES

A = "0x1111111111111111111111111111111111111111"
B = "0x2222222222222222222222222222222222222222"
ROLE = "0x" + b"MANAGER_ROLE".hex().ljust(64, "0")


def test_selectors():
    assert keccak(text="execTransactionWithRole(address,uint256,bytes,uint8,bytes32,bool)")[:4].hex() == "c6fe8747"
    assert keccak(text="multiSend(bytes)")[:4].hex() == "8d80ff0a"
    assert MULTISEND_CALL_ONLY_141[1] == "0x9641d764fc13c8B624c04430C7356C1C7C8102e2"


def test_multisend_packing_by_hand():
    calls = [Call(A, 0, "0x095ea7b3"), Call(B, 5, b"")]
    packed = encode_multisend_payload(calls)
    # operation(1) | to(20) | value(32) | len(32) | data
    expected = (b"\x00" + bytes.fromhex(A[2:]) + (0).to_bytes(32, "big") + (4).to_bytes(32, "big")
                + bytes.fromhex("095ea7b3")
                + b"\x00" + bytes.fromhex(B[2:]) + (5).to_bytes(32, "big") + (0).to_bytes(32, "big"))
    assert packed == expected
    data = encode_multisend(calls)
    assert data[:4].hex() == "8d80ff0a"
    assert decode(["bytes"], data[4:])[0] == expected
    back = decode_multisend(data)
    assert [(c.to, c.value, c.data, c.operation) for c in back] == [(c.to, c.value, c.data, c.operation) for c in calls]


def test_exec_with_role_encoding():
    data = exec_with_role(Call(A, 3, "0xabcdef", 0), "MANAGER_ROLE")
    assert data[:4].hex() == "c6fe8747"
    to, value, inner, op, role, should_revert = decode(
        ["address", "uint256", "bytes", "uint8", "bytes32", "bool"], data[4:])
    assert to.lower() == A and value == 3 and inner == bytes.fromhex("abcdef") and op == 0
    assert role.hex() == ROLE[2:] and should_revert is True
    # 0x role keys pass through
    data2 = exec_with_role(Call(A, 0, b""), ROLE)
    assert decode(["address", "uint256", "bytes", "uint8", "bytes32", "bool"], data2[4:])[4].hex() == ROLE[2:]


def test_single_call_goes_straight_to_roles():
    otx = build_operator_tx([Call(A, 0, "0x12345678")], ROLES, "MANAGER_ROLE")
    assert otx.batch == "single" and otx.safe_tx.to.lower() == ROLES.lower()
    assert otx.safe_tx.operation == CALL and otx.safe_tx.value == 0
    inner = decode(["address", "uint256", "bytes", "uint8", "bytes32", "bool"], otx.safe_tx.data[4:])
    assert inner[0].lower() == A and inner[2] == bytes.fromhex("12345678")


def test_several_calls_multisend_inside_roles():
    calls = [Call(A, 0, "0x01020304"), Call(B, 0, "0x05060708")]
    otx = build_operator_tx(calls, ROLES, "MANAGER_ROLE")
    to, value, inner, op, _role, sr = decode(
        ["address", "uint256", "bytes", "uint8", "bytes32", "bool"], otx.safe_tx.data[4:])
    assert to.lower() == MULTISEND_CALL_ONLY_141[1].lower() and op == DELEGATECALL and sr
    assert [c.to.lower() for c in decode_multisend(inner)] == [A, B]
    assert any("unwrapper" in w for w in otx.warnings)


def test_operator_side_batch():
    calls = [Call(A, 0, "0x01020304"), Call(B, 0, "0x05060708")]
    otx = build_operator_tx(calls, ROLES, "MANAGER_ROLE", batch="operator")
    assert otx.safe_tx.to == MULTISEND_CALL_ONLY_141[1] and otx.safe_tx.operation == DELEGATECALL
    wrapped = decode_multisend(otx.safe_tx.data)
    assert all(w.to.lower() == ROLES.lower() and w.data[:4].hex() == "c6fe8747" for w in wrapped)


def test_delegatecall_cannot_go_in_multisend_call_only():
    with pytest.raises(TxError):
        build_inner([Call(A, 0, b"", 1), Call(B, 0, b"")])
    # but a single delegatecall (CoW order signer) is fine
    assert build_inner([Call(A, 0, b"", 1)]).operation == DELEGATECALL


def test_unknown_chain_needs_override():
    with pytest.raises(TxError):
        build_inner([Call(A), Call(B)], chain_id=12345)
    assert build_inner([Call(A), Call(B)], chain_id=12345, multisend=B).to.lower() == B


def test_call_validation():
    with pytest.raises(TxError):
        Call("0x123")
    with pytest.raises(TxError):
        Call(A, operation=2)
    with pytest.raises(TxError):
        Call.from_json({"to": A, "data": "0x", "extra": 1})
