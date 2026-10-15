import json

import yaml
from click.testing import CliRunner

from defios.cli import main

from .conftest import FIXTURE


def run(args):
    return CliRunner().invoke(main, args, catch_exceptions=False)


def test_help():
    r = run(["--help"])
    assert r.exit_code == 0
    for cmd in ("doctor", "sync", "state", "check"):
        assert cmd in r.output


def test_catalog_command():
    r = run(["--format", "json", "catalog"])
    assert r.exit_code == 0
    data = json.loads(r.output)
    assert all(not v["errors"] for v in data.values())


def test_check_offline(agent_dir):
    r = run(["--root", str(agent_dir), "--format", "json", "--logs-file", str(FIXTURE), "check"])
    assert r.exit_code == 0, r.output
    data = json.loads(r.output)
    assert data["actions"]["gearbox_v3"]["lend"]["status"] == "unsafe"
    assert data["probe"] is False


def test_sync_writes_book_and_lock_then_state(agent_dir):
    r = run(["--root", str(agent_dir), "--format", "json", "--logs-file", str(FIXTURE), "sync"])
    assert r.exit_code == 0, r.output
    book_path = agent_dir / "books" / "testvault" / "book.yaml"
    lock_path = agent_dir / "books" / "testvault" / "role.lock.json"
    book = yaml.safe_load(book_path.read_text())
    lock = json.loads(lock_path.read_text())
    assert book["synced_at"]["block"] == lock["block"]
    assert book["permissions"]["gearbox_v3.lend"]["status"] == "unsafe"
    assert book["permissions"]["avant.redeem"]["status"] == "missing"
    assert book["role"]["changes_since_last_sync"] == {"first_sync": True}
    # second sync: no role changes
    r2 = run(["--root", str(agent_dir), "--format", "json", "--logs-file", str(FIXTURE), "sync"])
    book2 = json.loads(r2.output)["book"]
    ch = book2["role"]["changes_since_last_sync"]
    assert ch["added"] == ch["removed"] == ch["changed"] == []
    r3 = run(["--root", str(agent_dir), "--format", "text", "--logs-file", str(FIXTURE), "state"])
    assert r3.exit_code == 0 and "Unsafe:" in r3.output and "gearbox_v3.lend" in r3.output


def test_decode_batch(tmp_path):
    from eth_abi import encode
    from eth_utils import keccak

    sel = keccak(text="scopeFunction(bytes32,address,bytes4,(uint8,uint8,uint8,bytes)[],uint8)")[:4]
    data = sel + encode(["bytes32", "address", "bytes4", "(uint8,uint8,uint8,bytes)[]", "uint8"],
                        [b"MANAGER_ROLE".ljust(32, b"\0"), "0x1111111111111111111111111111111111111111",
                         bytes.fromhex("6e553f65"), [(0, 5, 5, b""), (0, 1, 0, b""), (0, 1, 15, b"")], 0])
    batch = {"version": "1.0", "chainId": "1", "meta": {"name": "x"}, "transactions": [
        {"to": "0x00000000000000000000000000000000000000F0", "value": "0", "data": "0x" + data.hex(),
         "contractMethod": None, "contractInputsValues": None}]}
    p = tmp_path / "b.json"
    p.write_text(json.dumps(batch))
    r = run(["--format", "json", "decode", str(p)])
    assert r.exit_code == 0
    tx = json.loads(r.output)["transactions"][0]
    assert tx["function"] == "scopeFunction" and tx["role"] == "MANAGER_ROLE"
    assert tx["condition"]["children"][1]["operator"] == "EQUAL_TO_AVATAR"


def test_missing_config_is_a_clean_error(tmp_path):
    r = CliRunner().invoke(main, ["--root", str(tmp_path), "check"])
    assert r.exit_code == 2


def test_doctor_without_rpc_reports_missing_keys(agent_dir):
    r = run(["--root", str(agent_dir), "--format", "json", "doctor"])
    assert r.exit_code == 0
    data = json.loads(r.output)
    assert data["env"]["RPC_URL"] is False
    assert any(not c["ok"] for c in data["checks"])
