from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest


@pytest.fixture
def launcher():
    path = Path(__file__).resolve().parents[1] / "10_web_deploy" / "windows" / "run_windows_mcp_bridge.py"
    spec = importlib.util.spec_from_file_location("desktop_launcher_tests", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def install_fixture(module, tmp_path, version="version_info = 3.14\n"):
    paths = module.Paths(tmp_path)
    paths.python.parent.mkdir(parents=True)
    paths.python.write_bytes(b"test fixture, never executed")
    paths.backend.write_text("# test bootstrap fixture, never executed\n", encoding="utf-8")
    (paths.runtime / "venv" / "pyvenv.cfg").write_text(version, encoding="utf-8")
    metadata = paths.runtime / "venv" / "Lib" / "site-packages" / "windows_mcp-0.8.8.dist-info" / "METADATA"
    metadata.parent.mkdir(parents=True)
    metadata.write_text("Name: windows-mcp\nVersion: 0.8.8\n", encoding="utf-8")
    paths.token.parent.mkdir(parents=True)
    paths.token.write_text("launcher-test-token-at-least-32-characters\n", encoding="utf-8")
    return paths


def identity(pid=123, created=12.5, session=7):
    return {"pid": pid, "create_time": created, "exe": "test-python.exe", "command_hash": "test-command-hash", "session_id": session}


def test_fixed_loopback_command_whitelist_and_secret_only_in_environment(launcher, tmp_path):
    paths = launcher.Paths(tmp_path)
    command = launcher.child_command(paths)
    assert command[:6] == [str(paths.python), "-I", "-X", "utf8", str(paths.backend), "serve"]
    assert command[command.index("--host") + 1] == "127.0.0.1"
    assert command[command.index("--port") + 1] == "48888"
    assert command[command.index("--ip-allowlist") + 1] == "127.0.0.1/32"
    assert set(command[command.index("--tools") + 1].split(",")) == {"Snapshot", "Screenshot", "ControlStatus", "Click", "Type", "Shortcut", "Scroll", "App"}
    assert "--auth-key" not in command and "--allow-insecure-remote" not in command
    token = "private-test-value-must-never-be-in-argv"
    env = launcher.child_environment(token, {"PATH": "fixture-path", "SystemRoot": "fixture-windows", "OPENAI_API_KEY": "unrelated-secret", "WINDOWS_MCP_AUTH_KEY": "old-token", "WINDOWS_MCP_TOOLS": "PowerShell"})
    assert env["WINDOWS_MCP_AUTH_KEY"] == token
    assert env["ANONYMIZED_TELEMETRY"] == "false"
    assert "OPENAI_API_KEY" not in env and "WINDOWS_MCP_TOOLS" not in env
    assert token not in json.dumps(command) and token not in json.dumps(launcher.public_config(paths))


@pytest.mark.parametrize("version", ["version_info = 3.14\n", "version = 3.14.7\n"])
def test_separate_runtime_config_is_secret_free_and_does_not_use_user_config(launcher, tmp_path, version):
    paths = install_fixture(launcher, tmp_path, version)
    launcher.ensure_runtime(paths)
    config = paths.config.read_text(encoding="utf-8")
    assert config == launcher.CONFIG_TEXT
    assert "auth_key" not in config
    assert str(paths.config) in launcher.child_command(paths)
    assert paths.python != paths.root / ".venv" / "Scripts" / "python.exe"
    paths.config.write_text(config + '\n[tools]\nexclude = []\n', encoding="utf-8")
    with pytest.raises(launcher.LauncherError, match="desktop_dedicated_config_changed"):
        launcher.ensure_runtime(paths)


def test_wrong_python_runtime_refused_before_process_start(launcher, tmp_path):
    paths = install_fixture(launcher, tmp_path, "version = 3.13.7\n")
    with pytest.raises(launcher.LauncherError, match="desktop_isolated_runtime_not_ready"):
        launcher.ensure_runtime(paths)
    assert not paths.config.exists()


@pytest.mark.parametrize("value", ["short", "space inside a token of sufficient length", "token-with\nnewline-should-never-be-valid"])
def test_invalid_token_does_not_escape_exception(launcher, tmp_path, value):
    path = tmp_path / "private.token"
    path.write_text(value, encoding="utf-8")
    with pytest.raises(launcher.LauncherError) as caught:
        launcher.read_token(path)
    assert str(caught.value) == "desktop_token_invalid" and value not in str(caught.value)


def test_single_instance_lock_rejects_second_supervisor_and_releases(launcher, tmp_path):
    lock = tmp_path / "instance.lock"
    with launcher.instance_lock(lock):
        with pytest.raises(launcher.LauncherError, match="desktop_supervisor_already_running"):
            with launcher.instance_lock(lock):
                pytest.fail("A second owner acquired the lock")
    with launcher.instance_lock(lock):
        pass


@pytest.mark.parametrize("changed", [{"create_time": 99.0}, {"exe": "other.exe"}, {"command_hash": "different"}, {"session_id": 0}])
def test_pid_reuse_or_changed_identity_refuses_stop_request(launcher, tmp_path, changed):
    paths = launcher.Paths(tmp_path)
    original = identity()
    actual = {**original, **changed}
    launcher.atomic_json(paths.pidfile, {"owner": original, "generation": "test-generation", "status": "running"})
    matcher = lambda record: launcher.identity_matches(record, identity_reader=lambda pid: actual)
    assert launcher.request_stop(paths, matcher=matcher) == {"stop_requested": False, "reason": "no_verified_supervisor"}
    assert not paths.stopfile.exists()
    assert launcher.status(paths, matcher=matcher)["running"] is False


def test_verified_stop_is_generation_scoped_and_never_kills_a_pid(launcher, tmp_path):
    paths = launcher.Paths(tmp_path)
    owner = identity()
    launcher.atomic_json(paths.pidfile, {"owner": owner, "generation": "owned-generation", "status": "running"})
    result = launcher.request_stop(paths, matcher=lambda record: record == owner)
    assert result == {"stop_requested": True, "generation": "owned-generation"}
    request = launcher.read_json(paths.stopfile)
    assert request["owner"] == owner and request["generation"] == "owned-generation"


def test_status_hides_unverified_child(launcher, tmp_path):
    paths = launcher.Paths(tmp_path)
    owner, child = identity(), identity(999)
    launcher.atomic_json(paths.pidfile, {"owner": owner, "child": child, "generation": "owned", "status": "running"})
    result = launcher.status(paths, matcher=lambda record: record == owner)
    assert result["running"] and not result["backend_running"]
    assert result["child"] is None


def test_readiness_accepts_actual_interpreter_descendant_of_uv_redirector(launcher):
    connection = SimpleNamespace(status="LISTEN", laddr=SimpleNamespace(ip="127.0.0.1", port=48888))
    real = SimpleNamespace(pid=222, net_connections=lambda **kwargs: [connection])
    redirector = SimpleNamespace(pid=111, children=lambda **kwargs: [real], net_connections=lambda **kwargs: [])
    assert launcher.child_listener_pid(111, process_factory=lambda pid: redirector) == 222


@pytest.mark.parametrize("host,port", [("0.0.0.0", 48888), ("127.0.0.1", 48271)])
def test_readiness_rejects_other_bind_addresses_or_ports(launcher, host, port):
    connection = SimpleNamespace(status="LISTEN", laddr=SimpleNamespace(ip=host, port=port))
    process = SimpleNamespace(pid=111, children=lambda **kwargs: [], net_connections=lambda **kwargs: [connection])
    assert launcher.child_listener_pid(111, process_factory=lambda pid: process) is None


def test_readiness_cannot_accept_unrelated_process_holding_port(launcher):
    # Only the actual Popen process and verified descendants are queried;
    # global unrelated listeners are never used to declare this backend ready.
    process = SimpleNamespace(pid=111, children=lambda **kwargs: [], net_connections=lambda **kwargs: [])
    assert launcher.child_listener_pid(111, process_factory=lambda pid: process) is None


def test_status_reports_verified_listener_identity_separately(launcher, tmp_path):
    paths = launcher.Paths(tmp_path)
    owner, child, listener = identity(), identity(456), identity(789)
    launcher.atomic_json(paths.pidfile, {"owner": owner, "child": child, "listener": listener, "generation": "owned", "status": "running"})
    result = launcher.status(paths, matcher=lambda record: record in (owner, child, listener))
    assert result["listener_running"] is True and result["listener"] == listener
    result = launcher.status(paths, matcher=lambda record: record in (owner, child))
    assert result["listener_running"] is False and result["listener"] is None


def test_log_never_includes_upstream_secret_values_or_raw_exceptions(launcher, tmp_path):
    path = tmp_path / "events.jsonl"
    log = launcher.SafeEventLog(path)
    try:
        log("backend_start_failed", reason="desktop_backend_start_failed", exception_type="OSError", token="secret-token-value", stderr="Authorization: Bearer secret-token-value")
        log("password=secret-token-value", reason="Authorization: Bearer secret-token-value")
    finally:
        log.close()
    text = path.read_text(encoding="utf-8")
    assert "secret-token-value" not in text and "Authorization" not in text
    assert "desktop_backend_start_failed" in text
    assert all(isinstance(json.loads(line), dict) for line in text.splitlines())


def make_supervisor(module, paths, **overrides):
    events = []
    values = dict(logger=lambda event, **fields: events.append((event, fields)), session_check=lambda: 7,
                  identity_reader=lambda pid: identity(pid), matcher=lambda record: True,
                  job_factory=lambda child: SimpleNamespace(close=lambda: None), ready=lambda pid: True)
    values.update(overrides)
    supervisor = module.Supervisor(paths, **values)
    return supervisor, events


def test_session_zero_and_service_identity_are_rejected(launcher, monkeypatch):
    monkeypatch.setattr(launcher, "session_id", lambda pid: 0)
    with pytest.raises(launcher.LauncherError, match="interactive_user_session_required"):
        launcher.require_user_session()
    monkeypatch.setattr(launcher, "session_id", lambda pid: 7)
    monkeypatch.setattr(launcher, "session_active", lambda identifier: True)
    monkeypatch.setattr(launcher.psutil, "Process", lambda: SimpleNamespace(username=lambda: "NT AUTHORITY\\SYSTEM"))
    with pytest.raises(launcher.LauncherError, match="interactive_user_session_required"):
        launcher.require_user_session()


def test_port_collision_stops_without_starting_or_killing_anything(launcher, tmp_path):
    paths = install_fixture(launcher, tmp_path)
    called = []
    supervisor, events = make_supervisor(launcher, paths, free_port=lambda: False, popen=lambda *a, **k: called.append(True))
    assert supervisor.run() == 2
    assert called == []
    assert launcher.read_json(paths.pidfile)["reason"] == "desktop_port_in_use"


def test_invalid_token_fails_closed_without_child(launcher, tmp_path):
    paths = install_fixture(launcher, tmp_path)
    paths.token.write_text("invalid", encoding="utf-8")
    called = []
    supervisor, _ = make_supervisor(launcher, paths, free_port=lambda: True, popen=lambda *a, **k: called.append(True))
    assert supervisor.run() == 2 and not called
    assert launcher.read_json(paths.pidfile)["reason"] == "desktop_token_invalid"


def test_exponential_backoff_and_restart_count_are_bounded(launcher, tmp_path):
    paths = install_fixture(launcher, tmp_path)
    attempts, delays = [], []

    def fail(*args, **kwargs):
        attempts.append(1)
        raise OSError("Authorization: Bearer do-not-log-this-secret")

    supervisor, events = make_supervisor(launcher, paths, free_port=lambda: True, popen=fail)
    supervisor.wait = lambda seconds: delays.append(seconds)
    assert supervisor.run() == 1
    assert len(attempts) == launcher.MAX_RESTARTS + 1
    assert delays == [1, 2, 4, 8, 16]
    assert max(launcher.restart_delay(n) for n in range(1, 100)) == 30
    assert "do-not-log-this-secret" not in str(events)
    assert launcher.read_json(paths.pidfile)["reason"] == "desktop_restart_limit_reached"


def test_user_session_end_stops_only_owned_backend(launcher, tmp_path):
    paths = install_fixture(launcher, tmp_path)
    checked, terminated, closed = [], [], []

    def session():
        checked.append(1)
        if len(checked) >= 3:
            raise launcher.LauncherError("interactive_user_session_required")
        return 7

    child = SimpleNamespace(pid=999, poll=lambda: None, terminate=lambda: terminated.append(999), wait=lambda **kwargs: 0)
    supervisor, _ = make_supervisor(launcher, paths, session_check=session, free_port=lambda: True,
                                    popen=lambda *args, **kwargs: child,
                                    job_factory=lambda child: SimpleNamespace(close=lambda: closed.append(999)))
    assert supervisor.run() == 0
    assert terminated == [999] and closed == [999]
    assert launcher.read_json(paths.pidfile)["reason"] == "user_session_ended"


def test_changed_child_identity_is_not_terminated_by_pid(launcher, tmp_path):
    paths = launcher.Paths(tmp_path)
    terminated, job_closed = [], []
    supervisor, events = make_supervisor(launcher, paths, matcher=lambda record: False)
    supervisor.child = SimpleNamespace(poll=lambda: None, terminate=lambda: terminated.append(True))
    supervisor.record = {"child": identity(999)}
    supervisor.job = SimpleNamespace(close=lambda: job_closed.append(True))
    supervisor.stop_child()
    assert not terminated and job_closed == [True]
    assert events == [("child_identity_changed", {"reason": "termination_refused"})]


def test_fresh_popen_handle_is_cleaned_up_if_identity_inspection_failed(launcher, tmp_path):
    supervisor, _ = make_supervisor(launcher, launcher.Paths(tmp_path), matcher=lambda record: False)
    terminated = []
    supervisor.child = SimpleNamespace(poll=lambda: None, terminate=lambda: terminated.append(True), wait=lambda **kwargs: 0)
    supervisor.child_identity_pending = True
    supervisor.stop_child()
    assert terminated == [True]


def test_child_is_hidden_and_raw_output_is_discarded(launcher):
    options = launcher.hidden_process_options()
    assert options["stdout"] == options["stderr"] == launcher.subprocess.DEVNULL
    assert options["stdin"] == launcher.subprocess.DEVNULL
    if os.name == "nt":
        assert options["creationflags"] & launcher.subprocess.CREATE_NO_WINDOW
        assert options["startupinfo"].wShowWindow == 0


def test_old_stop_generation_cannot_stop_new_owner(launcher, tmp_path):
    paths = launcher.Paths(tmp_path)
    supervisor, _ = make_supervisor(launcher, paths)
    supervisor.record = {"owner": identity(os.getpid())}
    launcher.atomic_json(paths.stopfile, {"generation": "stale-generation", "owner": supervisor.record["owner"]})
    assert supervisor.should_stop() is None
    launcher.atomic_json(paths.stopfile, {"generation": supervisor.generation, "owner": supervisor.record["owner"]})
    assert supervisor.should_stop() == "stop_requested"


@pytest.fixture
def backend():
    path = Path(__file__).resolve().parents[1] / "10_web_deploy" / "windows" / "run_windows_mcp_backend.py"
    spec = importlib.util.spec_from_file_location("desktop_backend_tests", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
@pytest.mark.parametrize("code", ["desktop_locked_or_secure", "desktop_session_inactive", "interactive_session_required"])
async def test_backend_guards_each_call_before_native_control_gate(backend, code):
    changes = []
    guard = backend.make_desktop_guard(object, RuntimeError, lambda: {"available": False, "error_code": code})

    async def native_gate(context):
        changes.append("control_lease_acquired")

    for tool in ("ControlStatus", "Snapshot", "Screenshot", "Type", "Click", "Shortcut"):
        context = SimpleNamespace(message=SimpleNamespace(name=tool))
        with pytest.raises(RuntimeError) as caught:
            await guard.on_call_tool(context, native_gate)
        assert json.loads(str(caught.value))["code"] == code
    assert changes == []


@pytest.mark.asyncio
async def test_backend_checks_real_desktop_on_every_call_and_preserves_tool_inputs(backend):
    states = [{"available": True}, {"available": False, "error_code": "desktop_locked_or_secure"}]
    seen = []
    guard = backend.make_desktop_guard(object, RuntimeError, lambda: states.pop(0))
    context = SimpleNamespace(message=SimpleNamespace(name="Type", arguments={"text": "unchanged input", "loc": [10, 20]}))

    async def native_gate(value):
        seen.append(value)
        return {"original_tool_result": True}

    assert await guard.on_call_tool(context, native_gate) == {"original_tool_result": True}
    with pytest.raises(RuntimeError, match="desktop_locked_or_secure"):
        await guard.on_call_tool(context, native_gate)
    assert seen == [context]
    assert context.message.arguments == {"text": "unchanged input", "loc": [10, 20]}


@pytest.mark.asyncio
async def test_backend_probe_exception_is_closed_without_secret_diagnostics(backend):
    def broken():
        raise OSError("Authorization: Bearer never-log-this-test-value")

    guard = backend.make_desktop_guard(object, RuntimeError, broken)
    with pytest.raises(RuntimeError) as caught:
        await guard.on_call_tool(SimpleNamespace(), lambda context: None)
    assert json.loads(str(caught.value))["code"] == "desktop_session_unavailable"
    assert "never-log" not in str(caught.value)


def test_backend_factory_adds_guard_before_original_gate_without_altering_schema(backend):
    class OriginalFastMCP:
        def __init__(self, *, name, lifespan):
            self.name, self.lifespan = name, lifespan
            self.middleware = []
            self.tools = {"Type": {"inputSchema": {"text": "string"}}}

        def add_middleware(self, middleware):
            self.middleware.append(middleware)

    lifespan = object()
    upstream = SimpleNamespace(FastMCP=OriginalFastMCP, _mcp=None)
    backend.install_guarded_factory(upstream, lambda: "desktop_guard")
    server = upstream.FastMCP(name="windows-mcp", lifespan=lifespan)
    server.add_middleware("original_ControlToolGate")
    assert server.middleware == ["desktop_guard", "original_ControlToolGate"]
    assert server.name == "windows-mcp" and server.lifespan is lifespan
    assert server.tools == {"Type": {"inputSchema": {"text": "string"}}}
    original_guarded = upstream.FastMCP
    backend.install_guarded_factory(upstream, lambda: "duplicate_guard")
    assert upstream.FastMCP is original_guarded


def test_backend_refuses_guard_insertion_after_native_server_initialized(backend):
    upstream = SimpleNamespace(FastMCP=object, _mcp=object())
    with pytest.raises(RuntimeError, match="desktop_backend_already_initialized"):
        backend.install_guarded_factory(upstream, lambda: "guard")


@pytest.mark.asyncio
async def test_app_trace_is_bounded_and_never_serializes_arguments_or_locals(backend, tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(backend.faulthandler, "dump_traceback_later", lambda delay, **kwargs: calls.append(("schedule", delay, kwargs["repeat"])))
    monkeypatch.setattr(backend.faulthandler, "cancel_dump_traceback_later", lambda: calls.append(("cancel",)))
    context = SimpleNamespace(message=SimpleNamespace(name="App", arguments={"name": "private-window-secret", "token": "private-test-secret"}))

    async def original(value):
        assert value is context
        return {"original": True}

    assert await backend.trace_app_call(context, original, tmp_path) == {"original": True}
    assert calls == [("schedule", 12, False), ("cancel",)]
    metadata = next(tmp_path.glob("*.json")).read_text(encoding="utf-8")
    assert "private-window-secret" not in metadata and "private-test-secret" not in metadata
    assert json.loads(metadata)["state"] == "returned"


@pytest.mark.asyncio
async def test_other_tools_do_not_schedule_app_trace(backend, tmp_path, monkeypatch):
    monkeypatch.setattr(backend.faulthandler, "dump_traceback_later", lambda *args, **kwargs: pytest.fail("non-App tracing"))

    async def original(value):
        return "unchanged"

    result = await backend.trace_app_call(SimpleNamespace(message=SimpleNamespace(name="Snapshot")), original, tmp_path)
    assert result == "unchanged" and list(tmp_path.iterdir()) == []


def test_diagnostic_environment_path_is_fixed_and_not_a_secret(launcher, tmp_path):
    paths = launcher.Paths(tmp_path)
    env = launcher.child_environment("private-credential-for-child", {}, diagnostics_path=paths.private_diagnostics)
    assert env["WEB_AGENT_WINDOWS_MCP_APP_TRACE_DIR"] == str(paths.private_diagnostics)
    assert "private-credential-for-child" not in str(launcher.public_config(paths))