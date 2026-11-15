from dataclasses import asdict, replace
import json
import os
from pathlib import Path
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

import psutil
import pytest

from web_tools import uu_session_registry as module


NAME = "xru_input_0123456789abcdef"
OTHER_NAME = "xru_input_fedcba9876543210"


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "_current_identity", lambda: (1000, 123.25))
    state = {1000: 123.25}
    def probe(pid):
        value = state.get(pid)
        if value is None:
            raise psutil.NoSuchProcess(pid)
        if isinstance(value, BaseException):
            raise value
        return value
    monkeypatch.setattr(module, "_process_create_time", probe)
    return module.UUSessionRegistry(tmp_path), state


def test_register_persists_only_ownership_and_is_idempotent(ledger):
    registry, _ = ledger
    record = registry.register("device1", NAME)
    assert registry.register("device1", NAME) == record
    assert registry.records("device1") == [record]
    raw = json.loads(registry.path.read_text())
    assert set(raw) == {"version", "sessions"}
    assert set(raw["sessions"][0]) == module._FIELDS
    assert raw["sessions"][0] == asdict(record)
    assert not any(word in registry.path.read_text().lower() for word in ("nonce", "token", "input_text"))


@pytest.mark.parametrize("name", ["user-session", "xru_input_"+"a"*15, "xru_input_"+"a"*17,
                                   "xru_input_"+"A"*16, "../"+NAME, NAME+"\n"])
def test_unowned_names_are_rejected_before_writing(ledger, name):
    registry, _ = ledger
    with pytest.raises(module.SessionRegistryError, match="owned"):
        registry.register("device1", name)
    assert not registry.path.exists()


@pytest.mark.parametrize("device", ["", "device id", "../device", "device\n", None, "a"*513])
def test_invalid_device_identifiers_cannot_pollute_registry(ledger, device):
    registry, _ = ledger
    with pytest.raises(module.SessionRegistryError):
        registry.register(device, NAME)


@pytest.mark.parametrize("pid,created", [(1001, 123.25), (True, 123.25), (1000, 123.0),
                                        (1000, float("nan")), (1000, True)])
def test_registration_cannot_assign_another_owner(ledger, pid, created):
    registry, _ = ledger
    with pytest.raises(module.SessionRegistryError) as error:
        registry.register("device1", NAME, owner_pid=pid, owner_create_time=created)
    assert error.value.code == "uu_session_owner_mismatch"


def test_live_owner_is_never_an_orphan_and_exact_device_is_required(ledger):
    registry, state = ledger
    own = registry.register("device1", NAME)
    other = registry.register("device2", OTHER_NAME)
    assert registry.orphan_candidates("device1") == []
    state.clear()
    assert registry.orphan_candidates("device1") == [own]
    assert registry.orphan_candidates("device2") == [other]


def test_pid_reuse_is_orphan_but_access_denied_and_unknown_are_preserved(ledger):
    registry, state = ledger
    record = registry.register("device1", NAME)
    state[1000] = 124.25
    assert registry.is_orphan(record)
    for unknown in (psutil.AccessDenied(1000), RuntimeError("unreadable"), float("nan"), False):
        state[1000] = unknown
        assert registry.orphan_candidates("device1") == []
        assert registry.is_orphan(record) is False


def test_forget_requires_the_unchanged_full_record(ledger):
    registry, state = ledger
    record = registry.register("device1", NAME)
    assert registry.forget(replace(record, owner_create_time=record.owner_create_time+1)) is False
    assert registry.forget(asdict(record)) is False
    assert registry.records() == [record]
    assert registry.forget(record) is True
    assert registry.forget(record) is False
    state.clear()
    assert registry.is_orphan(record) is False


def test_orphan_candidate_is_revalidated_against_updated_ledger(ledger):
    registry, state = ledger
    old = registry.register("device1", NAME)
    state.clear()
    assert registry.orphan_candidates("device1") == [old]
    assert registry.forget(old)
    state[1000] = 123.25
    new = registry.register("device1", NAME)
    assert new != old
    assert registry.is_orphan(old) is False
    assert registry.forget(old) is False


def test_forget_cannot_erase_a_different_live_owner_receipt(ledger, monkeypatch):
    registry, state = ledger
    record = registry.register("device1", NAME)
    monkeypatch.setattr(module, "_current_identity", lambda: (2000, 456.5))
    assert registry.forget(record) is False
    assert registry.records() == [record]
    state[1000] = psutil.AccessDenied(1000)
    assert registry.forget(record) is False
    state.clear()
    assert registry.forget(record) is True


@pytest.mark.parametrize("change", [
    lambda x: x.update(token="must not be accepted"),
    lambda x: x.update(owner_pid=True),
    lambda x: x.update(owner_create_time=float("nan")),
    lambda x: x.update(terminal_name="user-session"),
    lambda x: x.update(created_utc="2026-99-99T00:00:00.000000Z"),
])
def test_schema_pollution_never_authorizes_cleanup_or_overwrites_file(ledger, change):
    registry, _ = ledger
    record = registry.register("device1", NAME)
    raw = asdict(record)
    change(raw)
    registry.path.write_text(json.dumps({"version": 1, "sessions": [raw]}))
    before = registry.path.read_bytes()
    with pytest.raises(module.SessionRegistryError):
        registry.orphan_candidates("device1")
    with pytest.raises(module.SessionRegistryError):
        registry.register("device1", OTHER_NAME)
    assert registry.path.read_bytes() == before


@pytest.mark.parametrize("payload", [[], {}, {"version": True, "sessions": []},
                                     {"version": 1, "sessions": [], "extra": 1},
                                     {"version": 2, "sessions": []}])
def test_manifest_schema_is_strict(ledger, payload):
    registry, _ = ledger
    registry.path.write_text(json.dumps(payload))
    with pytest.raises(module.SessionRegistryError):
        registry.records()


def test_duplicate_ownership_rows_are_rejected(ledger):
    registry, _ = ledger
    raw = asdict(registry.register("device1", NAME))
    registry.path.write_text(json.dumps({"version": 1, "sessions": [raw, raw]}))
    with pytest.raises(module.SessionRegistryError):
        registry.records()


@pytest.mark.parametrize("payload", [
    '{"version":1,"version":1,"sessions":[]}',
    '{"version":1,"sessions":[],"sessions":[]}',
    '{"version":1,"sessions":[{"device_id":"device1","device_id":"device2"}]}',
])
def test_duplicate_json_keys_are_not_silently_resolved(ledger, payload):
    registry, _ = ledger
    registry.path.write_text(payload)
    with pytest.raises(module.SessionRegistryError) as error:
        registry.records()
    assert error.value.code == "uu_session_registry_invalid"


def test_full_registry_preserves_all_records(ledger):
    registry, _ = ledger
    for index in range(200):
        registry.register("device1", "xru_input_"+format(index, "016x"))
    before = registry.path.read_bytes()
    with pytest.raises(module.SessionRegistryError) as error:
        registry.register("device1", "xru_input_"+format(200, "016x"))
    assert error.value.code == "uu_session_registry_full"
    assert registry.path.read_bytes() == before
    assert len(registry.records()) == 200


def test_atomic_replace_failure_preserves_manifest_and_removes_only_temporary(ledger, monkeypatch):
    registry, _ = ledger
    first = registry.register("device1", NAME)
    before = registry.path.read_bytes()
    monkeypatch.setattr(module.os, "replace", lambda *args: (_ for _ in ()).throw(OSError("test failure")))
    with pytest.raises(OSError):
        registry.register("device1", OTHER_NAME)
    assert registry.path.read_bytes() == before
    assert registry.records() == [first]
    assert not list(registry.root.glob(".uu-session-*.tmp"))


def test_separate_instances_serialize_thread_updates(tmp_path):
    def register(index):
        return module.UUSessionRegistry(tmp_path).register("device1", "xru_input_"+format(index, "016x"))
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert len(list(pool.map(register, range(40)))) == 40
    assert len(module.UUSessionRegistry(tmp_path).records()) == 40


def test_process_lock_serializes_real_independent_writers(tmp_path):
    source = Path(module.__file__).resolve().parents[1]
    code = (
        "import sys;from pathlib import Path;sys.path.insert(0,sys.argv[1]);"
        "from web_tools.uu_session_registry import UUSessionRegistry;"
        "r=UUSessionRegistry(Path(sys.argv[2]));"
        "[r.register('device1','xru_input_'+format(int(sys.argv[3])*10+i,'016x')) for i in range(10)]"
    )
    children = [subprocess.Popen([sys.executable, "-c", code, str(source), str(tmp_path), str(index)],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)) for index in range(4)]
    for child in children:
        _, error = child.communicate(timeout=20)
        assert child.returncode == 0, error.decode(errors="replace")
    registry = module.UUSessionRegistry(tmp_path)
    assert len(registry.records()) == 40
    assert len(registry.orphan_candidates("device1")) == 40


def test_lock_timeout_does_not_read_or_change_manifest(ledger):
    registry, _ = ledger
    registry.register("device1", NAME)
    before = registry.path.read_bytes()
    other = module.UUSessionRegistry(registry.root, lock_timeout=.05)
    with module._file_lock(registry.lock_path, 1):
        with pytest.raises(module.SessionRegistryError) as error:
            other.records()
    assert error.value.code == "uu_session_registry_busy"
    assert registry.path.read_bytes() == before
