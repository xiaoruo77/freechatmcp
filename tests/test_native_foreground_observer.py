"""Foreground evidence boundaries and actual hook cleanup, without UI actions."""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import sys
import threading
import time

import pytest


PATH = Path(__file__).resolve().parents[1] / "10_web_deploy" / "native-foreground-observer.py"


def load_module():
    spec = importlib.util.spec_from_file_location("foreground_observer_test", PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_import_and_snapshot_do_not_require_windows(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    module = load_module()
    observer = module.ForegroundEventObserver(123)
    assert observer.available is False
    assert observer.snapshot()["started"] is False
    with pytest.raises(RuntimeError, match="requires_windows"):
        observer.start()
    assert observer.stop()["hook_installed"] is False


def test_event_clock_remains_relative_across_dword_wrap():
    module = load_module()
    start = (1 << 32) - 20
    # An event 25 ms after start, delivered another 10 ms later, crosses the
    # DWORD boundary even though the observer's GetTickCount64 does not wrap.
    assert module._relative_event_ms(5, (1 << 32) + 15, start) == 25
    assert module._relative_event_ms(0xFFFFFFF6, (1 << 32) + 15, start) == 10
    # An unexpectedly old queued event cannot disclose an absolute uptime.
    assert module._relative_event_ms(1, 5000, 4900) == 0


@pytest.mark.parametrize("seconds", [0, 0.099, 120.001, float("inf"), float("nan")])
def test_observer_duration_must_be_bounded(seconds):
    module = load_module()
    with pytest.raises(ValueError):
        module.ForegroundEventObserver(123, max_duration_seconds=seconds)


@pytest.mark.skipif(sys.platform != "win32", reason="real WinEvent lifecycle requires Windows")
def test_context_exception_unhooks_and_evidence_has_no_identifiers():
    module = load_module()
    observer = module.ForegroundEventObserver(os.getpid(), max_duration_seconds=1, max_marks=1)
    with pytest.raises(ValueError, match="action_failed"):
        with observer:
            first = observer.mark("action_start")
            second = observer.mark("action_end")
            assert first["timestamp_ms"] <= second["timestamp_ms"]
            raise ValueError("action_failed")
    evidence = observer.snapshot()
    assert evidence["hook_installed"] and evidence["unhook_succeeded"]
    assert evidence["stopped"] and not evidence["running"]
    assert evidence["error"] is None
    assert evidence["mark_count"] == 2 and evidence["marks_dropped"] == 1
    assert len(evidence["marks"]) == 1
    assert isinstance(evidence["initial_final_foreground_equal"], bool)
    assert observer.stop() == evidence
    # Only equality flags and relative timing may cross the evidence boundary.
    for record in [evidence, *evidence["events"], *evidence["marks"]]:
        assert not any(part in key for key in record for part in ("hwnd", "pid", "title", "input", "key_content"))


@pytest.mark.skipif(sys.platform != "win32", reason="real WinEvent lifecycle requires Windows")
def test_deadline_releases_hook_without_caller_stop():
    module = load_module()
    observer = module.ForegroundEventObserver(os.getpid(), max_duration_seconds=0.1)
    try:
        observer.start()
        deadline = time.monotonic() + 2
        while observer.snapshot()["running"] and time.monotonic() < deadline:
            threading.Event().wait(0.02)
        evidence = observer.snapshot()
        assert evidence["stopped"] and evidence["stop_reason"] == "deadline"
        assert evidence["unhook_succeeded"] and evidence["error"] is None
        assert 90 <= evidence["elapsed_ms"] < 1500
    finally:
        observer.stop()
