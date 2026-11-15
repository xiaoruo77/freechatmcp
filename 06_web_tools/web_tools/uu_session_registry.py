"""Persist ownership of temporary UU terminal sessions across local restarts.

This module never runs UU or deletes a remote session. An orphan candidate is
only a record whose original local owner is proven gone or whose PID was
reused. The caller must recheck ``is_orphan`` before its fixed UU cleanup and
call ``forget`` only after UU confirms that exact terminal is absent.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import tempfile
import time
from typing import Iterator


_NAME = re.compile(r"xru_input_[0-9a-f]{16}\Z")
_DEVICE = re.compile(r"[A-Za-z0-9._:-]{1,512}\Z")
_UTC = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z\Z")
_FIELDS = frozenset({"device_id", "terminal_name", "owner_pid", "owner_create_time", "created_utc"})
_MAX_RECORDS = 200
_MAX_BYTES = 256 * 1024


class SessionRegistryError(RuntimeError):
    """A fixed, non-secret registry diagnostic."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class SessionRecord:
    device_id: str
    terminal_name: str
    owner_pid: int
    owner_create_time: float
    created_utc: str


def _positive_time(value) -> bool:
    return type(value) in {float, int} and math.isfinite(value) and value > 0


def _device_id(value: str) -> str:
    if not isinstance(value, str) or not _DEVICE.fullmatch(value):
        raise SessionRegistryError("uu_session_device_invalid", "Use one exact bounded UU device identifier")
    return value


def _json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise SessionRegistryError("uu_session_registry_invalid", "The session registry contains ambiguous duplicate JSON keys")
        result[key] = value
    return result


def _validated_record(raw) -> SessionRecord:
    if not isinstance(raw, dict) or set(raw) != _FIELDS:
        raise SessionRegistryError("uu_session_registry_invalid", "The session registry contains an invalid record schema")
    if (not isinstance(raw["device_id"], str) or not _DEVICE.fullmatch(raw["device_id"])
            or not isinstance(raw["terminal_name"], str) or not _NAME.fullmatch(raw["terminal_name"])
            or type(raw["owner_pid"]) is not int or not 0 < raw["owner_pid"] <= 2147483647
            or not _positive_time(raw["owner_create_time"])
            or not isinstance(raw["created_utc"], str) or not _UTC.fullmatch(raw["created_utc"])):
        raise SessionRegistryError("uu_session_registry_invalid", "The session registry contains an invalid ownership record")
    try:
        datetime.strptime(raw["created_utc"], "%Y-%m-%dT%H:%M:%S.%fZ")
    except ValueError:
        raise SessionRegistryError("uu_session_registry_invalid", "The session registry contains an invalid creation date") from None
    return SessionRecord(**{**raw, "owner_create_time": float(raw["owner_create_time"])})


def _process_create_time(pid: int) -> float:
    import psutil
    return psutil.Process(pid).create_time()


def _current_identity() -> tuple[int, float]:
    pid = os.getpid()
    try:
        created = _process_create_time(pid)
    except Exception:
        raise SessionRegistryError("uu_session_owner_unknown", "The current local process identity could not be verified") from None
    if not _positive_time(created):
        raise SessionRegistryError("uu_session_owner_unknown", "The current local process creation time is unavailable")
    return pid, float(created)


def _owner_state(record: SessionRecord) -> str:
    import psutil
    try:
        actual = _process_create_time(record.owner_pid)
    except psutil.NoSuchProcess:
        return "orphan"
    except Exception:
        # AccessDenied and unreadable process state never authorize cleanup.
        return "unknown"
    if not _positive_time(actual):
        return "unknown"
    return "live" if float(actual) == record.owner_create_time else "orphan"


@contextmanager
def _file_lock(path: Path, timeout: float) -> Iterator[None]:
    if path.is_symlink():
        raise SessionRegistryError("uu_session_registry_unsafe", "The registry lock must be a regular local file")
    deadline = time.monotonic() + timeout
    # Never delete or replace this file: every cooperating process locks the
    # same first byte while the JSON manifest is replaced atomically.
    with path.open("a+b") as handle:
        if os.fstat(handle.fileno()).st_size == 0:
            handle.write(b"\0")
            handle.flush()
        acquired = False
        try:
            while not acquired:
                try:
                    handle.seek(0)
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                except OSError:
                    if time.monotonic() >= deadline:
                        raise SessionRegistryError("uu_session_registry_busy", "Another process still owns the session registry lock") from None
                    time.sleep(min(.02, max(0, deadline - time.monotonic())))
            yield
        finally:
            if acquired:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class UUSessionRegistry:
    """A bounded ownership ledger; it contains no credentials or GUI input.

    ``root`` is the caller's local data directory. Registration is restricted
    to the current process identity; records cannot be transferred to another
    process. All read/modify/write operations take the cross-process lock.
    """

    def __init__(self, root: Path, *, lock_timeout: float = 5):
        if type(lock_timeout) not in {int, float} or not math.isfinite(lock_timeout) or not 0 < lock_timeout <= 30:
            raise ValueError("lock_timeout must be between zero and 30 seconds")
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "uu-session-registry.json"
        self.lock_path = self.root / "uu-session-registry.lock"
        self.lock_timeout = float(lock_timeout)

    def _read(self) -> list[SessionRecord]:
        if self.path.is_symlink():
            raise SessionRegistryError("uu_session_registry_unsafe", "The session registry must be a regular local file")
        if not self.path.exists():
            return []
        try:
            if not self.path.is_file() or self.path.stat().st_size > _MAX_BYTES:
                raise SessionRegistryError("uu_session_registry_invalid", "The session registry exceeds its bounded file size")
            payload = json.loads(self.path.read_text(encoding="utf-8"), object_pairs_hook=_json_object)
        except (OSError, UnicodeError, ValueError):
            raise SessionRegistryError("uu_session_registry_invalid", "The session registry could not be read as valid JSON") from None
        if not isinstance(payload, dict) or set(payload) != {"version", "sessions"} or type(payload["version"]) is not int or payload["version"] != 1:
            raise SessionRegistryError("uu_session_registry_invalid", "The session registry has an unsupported manifest schema")
        raw = payload["sessions"]
        if not isinstance(raw, list) or len(raw) > _MAX_RECORDS:
            raise SessionRegistryError("uu_session_registry_invalid", "The session registry has an invalid record count")
        records = [_validated_record(item) for item in raw]
        keys = {(item.device_id, item.terminal_name) for item in records}
        if len(keys) != len(records):
            raise SessionRegistryError("uu_session_registry_invalid", "The session registry contains ambiguous duplicate sessions")
        return records

    def _write(self, records: list[SessionRecord]) -> None:
        payload = {"version": 1, "sessions": [asdict(record) for record in records]}
        encoded = (json.dumps(payload, ensure_ascii=True, separators=(",", ":")) + "\n").encode("utf-8")
        if len(records) > _MAX_RECORDS or len(encoded) > _MAX_BYTES:
            raise SessionRegistryError("uu_session_registry_full", "The session ownership registry is full; confirm stale sessions absent before removing their records")
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="wb", dir=self.root, prefix=".uu-session-", suffix=".tmp", delete=False) as handle:
                temporary = Path(handle.name)
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            if temporary is not None and temporary.exists():
                temporary.unlink()

    def register(self, device_id: str, terminal_name: str, *, owner_pid: int | None = None,
                 owner_create_time: float | None = None) -> SessionRecord:
        """Record this process's intent before creating a new remote terminal."""
        device_id = _device_id(device_id)
        if not isinstance(terminal_name, str) or not _NAME.fullmatch(terminal_name):
            raise SessionRegistryError("uu_session_name_invalid", "Only an owned xru_input terminal name may be registered")
        pid, created = _current_identity()
        requested_pid = pid if owner_pid is None else owner_pid
        requested_created = created if owner_create_time is None else owner_create_time
        if type(requested_pid) is not int or requested_pid != pid or not _positive_time(requested_created) or float(requested_created) != created:
            raise SessionRegistryError("uu_session_owner_mismatch", "A session can only be registered to the verified current process")
        with _file_lock(self.lock_path, self.lock_timeout):
            records = self._read()
            for record in records:
                if (record.device_id, record.terminal_name) == (device_id, terminal_name):
                    if (record.owner_pid, record.owner_create_time) != (pid, created):
                        raise SessionRegistryError("uu_session_already_owned", "The exact terminal is already registered to a different process identity")
                    return record
            record = SessionRecord(device_id, terminal_name, pid, created,
                                   datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"))
            records.append(record)
            self._write(records)
            return record

    def records(self, device_id: str | None = None) -> list[SessionRecord]:
        if device_id is not None:
            device_id = _device_id(device_id)
        with _file_lock(self.lock_path, self.lock_timeout):
            records = self._read()
        return [record for record in records if device_id is None or record.device_id == device_id]

    def orphan_candidates(self, device_id: str) -> list[SessionRecord]:
        """List candidates only for this exact device, preserving unknown owners."""
        device_id = _device_id(device_id)
        with _file_lock(self.lock_path, self.lock_timeout):
            return [record for record in self._read()
                    if record.device_id == device_id and _owner_state(record) == "orphan"]

    def is_orphan(self, record: SessionRecord) -> bool:
        """Revalidate exact ledger identity and liveness before remote cleanup."""
        if not isinstance(record, SessionRecord):
            return False
        _validated_record(asdict(record))
        with _file_lock(self.lock_path, self.lock_timeout):
            records = self._read()
            return record in records and _owner_state(record) == "orphan"

    def forget(self, record: SessionRecord) -> bool:
        """Remove one unchanged record, after the caller confirmed remote absence."""
        if not isinstance(record, SessionRecord):
            return False
        _validated_record(asdict(record))
        with _file_lock(self.lock_path, self.lock_timeout):
            records = self._read()
            if record not in records:
                return False
            # Only this process's own receipt, or a proven orphan receipt,
            # can be forgotten. A different still-live owner retains its row.
            try:
                own = _current_identity() == (record.owner_pid, record.owner_create_time)
            except SessionRegistryError:
                own = False
            if not own and _owner_state(record) != "orphan":
                return False
            self._write([candidate for candidate in records if candidate != record])
            return True


SessionRegistry = UUSessionRegistry
