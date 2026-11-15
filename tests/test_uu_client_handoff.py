from pathlib import Path
import ast
import sys
from types import SimpleNamespace

import pytest

from web_tools import uu_client_handoff as module


@pytest.fixture
def receiver(tmp_path, monkeypatch):
    launcher = tmp_path / "GameViewer.exe"
    launcher.touch()
    gui = tmp_path / "bin" / "GameViewer.exe"
    gui.parent.mkdir()
    gui.touch()
    state = {"processes": {
        11: {"exe": str(gui), "name": "GameViewer.exe"},
        98: {"exe": str(gui.parent / "GameViewerServer.exe"), "name": "GameViewerServer.exe"},
        99: {"exe": str(launcher.parent / "GameViewerService.exe"), "name": "GameViewerService.exe"}},
        "terminated": [], "callbacks": [], "windows": []}

    class NoSuchProcess(OSError):
        pass

    class Process:
        def __init__(self, pid):
            self.pid = pid

        @property
        def info(self):
            if self.pid not in state["processes"]:
                raise NoSuchProcess()
            return {"pid": self.pid, **state["processes"][self.pid]}

        def terminate(self):
            state["terminated"].append(self.pid)
            pytest.fail("A shared UU receiver must never be terminated")

        def kill(self):
            pytest.fail("A shared UU receiver must never be killed")

    monkeypatch.setitem(sys.modules, "psutil", SimpleNamespace(
        process_iter=lambda fields: [Process(pid) for pid in state["processes"]],
        Error=OSError, NoSuchProcess=NoSuchProcess))

    def forbidden_status():
        state["callbacks"].append("status")
        pytest.fail("A connection query cannot authorize replacing the receiver")

    def forbidden_windows():
        state["callbacks"].append("windows")
        pytest.fail("Window placement cannot authorize replacing the receiver")

    def prepare(timeout=2, executable=None):
        return module.prepare_idle_client(launcher if executable is None else executable,
            forbidden_status, forbidden_windows, timeout=timeout)

    return launcher, gui, state, prepare


def assert_preserved(result, state, pids):
    assert result["ok"] is False
    assert result["ready_for_private_launch"] is False
    assert result["handoff_performed"] is False
    assert result["closed_pids"] == []
    assert result["preserved_gui_pids"] == pids
    assert result["preserved_service"] is True
    assert result["service_processes_touched"] is False
    assert result["host_input_used"] is False
    assert state["terminated"] == []
    assert state["callbacks"] == []


@pytest.mark.parametrize("mode", ["idle", "active", "unknown", "foreground", "tray"])
def test_every_receiver_state_is_preserved_without_handoff(receiver, mode):
    _, _, state, prepare = receiver
    state["receiver_state"] = mode
    state["windows"] = [{"hwnd": 101, "pid": 11, "title": "网易UU远程",
        "class_name": "Qt51519QWindowIcon", "visible": mode != "tray",
        "owner": 0, "desktop": "Default"}]
    result = prepare()
    assert result["error_code"] == "uu_shared_receiver_preserved"
    assert_preserved(result, state, [11])
    assert set(state["processes"]) == {11, 98, 99}


def test_real_default_homepage_and_hidden_utilities_do_not_make_receiver_disposable(receiver):
    _, _, state, prepare = receiver
    state["windows"] = [
        {"hwnd": 201364, "pid": 11, "title": "GameViewer", "class_name": "Qt51519QWindowToolTipSaveBits", "visible": False, "owner": 0},
        {"hwnd": 1376488, "pid": 11, "title": "CA4BDE8A-7139-44FE-96B2-9CF79E381B20", "class_name": "Static", "visible": False, "owner": 0},
        {"hwnd": 1312292, "pid": 11, "title": "网易UU远程", "class_name": "Qt51519QWindowIcon", "visible": True, "owner": 0},
        {"hwnd": 29560766, "pid": 11, "title": "MSCTFIME UI", "class_name": "MSCTFIME UI", "visible": False, "owner": 1180416},
        {"hwnd": 1180416, "pid": 11, "title": "Default IME", "class_name": "IME", "visible": False, "owner": 1312292}]
    result = prepare()
    assert result["error_code"] == "uu_shared_receiver_preserved"
    assert_preserved(result, state, [11])


def test_root_and_bin_clients_are_both_shared_receivers(receiver):
    launcher, _, state, prepare = receiver
    state["processes"][12] = {"exe": str(launcher), "name": "GameViewer.exe"}
    result = prepare()
    assert result["error_code"] == "uu_shared_receiver_preserved"
    assert_preserved(result, state, [11, 12])


def test_unreadable_gameviewer_identity_is_preserved(receiver):
    _, _, state, prepare = receiver
    state["processes"][11] = {"exe": None, "name": "GameViewer.exe"}
    result = prepare()
    assert result["error_code"] == "uu_shared_receiver_preserved"
    assert result["identity_verified"] is False
    assert_preserved(result, state, [11])


@pytest.mark.parametrize("keep_services", [True, False])
def test_absent_gui_never_authorizes_a_private_replacement(receiver, keep_services):
    _, _, state, prepare = receiver
    state["processes"].pop(11)
    if not keep_services:
        state["processes"].clear()
    result = prepare()
    assert result["error_code"] == "uu_receiver_isolation_required"
    assert_preserved(result, state, [])


def test_another_installation_is_not_claimed_and_private_route_still_refuses(receiver, tmp_path):
    _, _, state, prepare = receiver
    state["processes"][11] = {"exe": str(tmp_path / "OtherUU" / "GameViewer.exe"), "name": "GameViewer.exe"}
    result = prepare()
    assert result["error_code"] == "uu_receiver_isolation_required"
    assert_preserved(result, state, [])


@pytest.mark.parametrize("timeout", [0, -1, True, None, 26, float("nan"), float("inf"), "10"])
def test_invalid_timeout_never_changes_receiver(receiver, timeout):
    _, _, state, prepare = receiver
    result = prepare(timeout=timeout)
    assert result["error_code"] == "uu_handoff_timeout_invalid"
    assert_preserved(result, state, [])


@pytest.mark.parametrize("kind", ["relative", "wrong_name", "missing_gui", "missing_launcher"])
def test_strict_installed_executable_validation_is_retained(receiver, tmp_path, kind):
    launcher, gui, state, prepare = receiver
    executable = launcher
    if kind == "relative":
        executable = Path("GameViewer.exe")
    elif kind == "wrong_name":
        executable = launcher.with_name("Other.exe")
        executable.touch()
    elif kind == "missing_gui":
        gui.unlink()
    else:
        launcher.unlink()
    result = prepare(executable=executable)
    assert result["error_code"] == "uu_handoff_executable_invalid"
    assert_preserved(result, state, [])


def test_inventory_failure_is_safe_and_does_not_expose_raw_error(receiver, monkeypatch):
    _, _, state, prepare = receiver
    def failing(_):
        raise OSError("private-token-must-not-escape")
    monkeypatch.setattr(sys.modules["psutil"], "process_iter", failing)
    result = prepare()
    assert result["error_code"] == "uu_receiver_state_unknown"
    assert "private-token" not in result["message"]
    assert_preserved(result, state, [])


def test_legacy_handoff_contains_no_process_or_window_shutdown_calls():
    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    forbidden = {"terminate", "kill", "PostMessage", "PostMessageW", "SendMessage", "SendMessageW", "SetForegroundWindow", "SwitchDesktop"}
    calls = {node.func.attr for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)}
    assert not calls.intersection(forbidden)
