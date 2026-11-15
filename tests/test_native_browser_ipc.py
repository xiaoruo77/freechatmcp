"""Pure local fakes: these tests never attach to a real browser."""
import asyncio
import ctypes
import json
import os
import time
from types import SimpleNamespace

import pytest

from web_tools import native_browser_ipc as ipc


TOKEN = "a" * 64
PIPE = r"\\.\pipe\xiaoruo-browser-123-00000000-0000-0000-0000-000000000001"


@pytest.fixture
def discovery(tmp_path):
    executable = tmp_path / "install" / "xiaoruo-browser.exe"
    executable.parent.mkdir()
    executable.touch()
    root = tmp_path / "sessions"
    root.mkdir()
    path = root / "browser-123-456.json"
    data = {"schema": "xiaoruo-browser-control-discovery/1", "product": "xiaoruo-browser",
            "pid": 123, "instanceId": "b" * 32, "pipeName": PIPE, "authToken": TOKEN,
            "protocol": ipc.PROTOCOL, "mcpProtocol": ipc.MCP_PROTOCOL,
            "permissions": ["browser.control"], "executable": str(executable)}
    process = SimpleNamespace(pid=123, exe=lambda: str(executable),
        create_time=lambda: time.time() - 60, is_running=lambda: True,
        cmdline=lambda: [str(executable)])
    # Creation time is stable for a single process lifetime.
    created = time.time() - 60
    process.create_time = lambda: created
    system = SimpleNamespace(Process=lambda pid: process)
    path.write_text(json.dumps(data), encoding="utf-8")
    service = ipc.NativeBrowserDiscovery(root, executable, psutil_api=system)
    return SimpleNamespace(root=root, path=path, data=data, process=process,
                           service=service, executable=executable)


def rewrite(fixture, **updates):
    fixture.data.update(updates)
    fixture.path.write_text(json.dumps(fixture.data), encoding="utf-8")


class Lease:
    def __init__(self, _key):
        self.closed = False
    def acquire(self, deadline):
        ipc._remaining(deadline)
    def close(self):
        self.closed = True


class Transport:
    def __init__(self, instance, deadline, *, states=None, failure=None, result_mode="structured"):
        self.instance, self.deadline = instance, deadline
        self.requests, self.queue = [], []
        self.closed = self.cancelled = False
        self.states = list(states or ["accepted", "running", "succeeded"])
        self.failure, self.result_mode = failure, result_mode

    def tool_result(self, value, images=False):
        content = [{"type": "text", "text": json.dumps(value)}]
        if images:
            content.append({"type": "image", "mimeType": "image/png", "data": "iVBORw0KGgo="})
        result = {"content": content}
        if self.result_mode == "structured":
            result["structuredContent"] = value
        return result

    def write(self, raw, deadline):
        ipc._remaining(deadline)
        request = json.loads(raw)
        self.requests.append(request)
        method = request["method"]
        if "id" not in request:
            return
        if method == "xiaoruo/auth":
            result = {"authenticated": True, "protocol": ipc.PROTOCOL}
        elif method == "initialize":
            result = {"protocolVersion": ipc.MCP_PROTOCOL}
        elif method == "tools/list":
            result = {"tools": [{"name": "example"}]}
        elif method == "tools/call":
            tool = request["params"]["name"]
            if tool in {"xiaoruo_browser_start_task", "xiaoruo_browser_task_status"}:
                state = self.states.pop(0) if len(self.states) > 1 else self.states[0]
                task = {"id": "xr-123-1", "state": state, "errorCode": "", "error": ""}
                if state == "failed":
                    task.update(errorCode="unsupported", error="Explicitly unsupported", retryable=True)
                elif state == "timed_out":
                    task.update(errorCode="task_timeout", error="Native task deadline expired")
                elif state == "cancelled":
                    task.update(errorCode="task_cancelled", error="Native task was cancelled")
                result = self.tool_result({"task": task}, images=state == "succeeded")
            else:
                result = self.tool_result({"tabs": [], "authToken": TOKEN, "pipeName": PIPE,
                                           "message": TOKEN + " " + PIPE})
        else:
            raise AssertionError(method)
        self.queue.append(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}).encode())

    def readline(self, deadline):
        ipc._remaining(deadline)
        if self.failure and self.requests[-1]["method"] == "tools/call":
            raise self.failure
        return self.queue.pop(0)

    def cancel(self):
        self.cancelled = True

    def close(self):
        self.closed = True


def fake_client(discovery, *, transport_class=Transport, **options):
    instance = discovery.service.discover()[0]
    opened = []
    def factory(instance, deadline):
        transport = transport_class(instance, deadline, **options)
        opened.append(transport)
        return transport
    return ipc.NativeBrowserClient(instance, transport_factory=factory, lease_factory=Lease), opened


def test_discovery_public_and_repr_are_secret_free(discovery):
    instances = discovery.service.discover()
    assert len(instances) == 1
    public = instances[0].public_descriptor()
    assert public["manual_instance"] and public["process_id"] == 123
    assert public["browser_id"].startswith("native_")
    for text in [repr(instances[0]), json.dumps(public)]:
        assert TOKEN not in text and PIPE not in text
        assert "authToken" not in text and "pipeName" not in text
    assert public["connection_generation"] == instances[0].public_descriptor()["connection_generation"]


@pytest.mark.parametrize("updates", [
    {"schema": "wrong"}, {"product": "wrong"}, {"protocol": "wrong"}, {"mcpProtocol": "wrong"},
    {"pid": True}, {"pid": 124}, {"authToken": "short"}, {"instanceId": "bad"},
    {"permissions": "browser.control"}, {"pipeName": r"\\remote\pipe\xiaoruo-browser-123-test"},
    {"pipeName": PIPE.replace("123-", "124-")}, {"pipeName": PIPE + "\\child"},
    {"executable": "relative.exe"}, {"pipeName": None},
])
def test_discovery_rejects_invalid_or_remote_identity(discovery, updates):
    rewrite(discovery, **updates)
    assert discovery.service.discover() == []


def test_discovery_uses_dynamic_localappdata_and_explicit_configuration(discovery, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(discovery.root))
    default = ipc.NativeBrowserDiscovery(psutil_api=discovery.service._process_api)
    assert default.root == discovery.root / "xiaoruo-browser/ipc/sessions"
    monkeypatch.setenv("WEB_AGENT_XIAORUO_IPC_DISCOVERY_ROOT", str(discovery.root))
    monkeypatch.setenv("WEB_AGENT_XIAORUO_BROWSER_EXECUTABLE", str(discovery.executable))
    assert len(ipc.NativeBrowserDiscovery(psutil_api=discovery.service._process_api).discover()) == 1


def test_discovery_rejects_untrusted_or_stale_process_and_stdio(discovery):
    discovery.process.exe = lambda: str(discovery.root / "xiaoruo-browser.exe")
    assert discovery.service.discover() == []
    discovery.process.exe = lambda: str(discovery.executable)
    discovery.process.is_running = lambda: False
    assert discovery.service.discover() == []
    discovery.process.is_running = lambda: True
    discovery.process.cmdline = lambda: [str(discovery.executable), "--mcp-stdio"]
    assert discovery.service.discover() == []
    discovery.process.cmdline = lambda: []
    discovery.process.create_time = lambda: time.time() + 100
    assert discovery.service.discover() == []


def test_discovery_rejects_duplicate_keys_and_oversized_json(discovery):
    discovery.path.write_text('{"authToken":"never-print-me","authToken":"different"}', encoding="utf-8")
    assert discovery.service.discover() == []
    discovery.path.write_bytes(b" " * 65537)
    assert discovery.service.discover() == []


def test_discovery_rejects_reparse_metadata(discovery, monkeypatch):
    original = ipc.Path.lstat
    def lstat(path, *args, **kwargs):
        value = original(path, *args, **kwargs)
        if path == discovery.path:
            return SimpleNamespace(st_mode=value.st_mode, st_file_attributes=0x400)
        return value
    monkeypatch.setattr(ipc.Path, "lstat", lstat)
    assert discovery.service.discover() == []


@pytest.mark.skipif(os.name != "nt", reason="Windows discovery sharing semantics")
def test_discovery_reads_while_publisher_has_write_handle(discovery):
    api = ipc._kernel()
    # A cooperative publisher allows reads but already owns write access. A
    # reader that only grants FILE_SHARE_READ cannot coexist with this handle.
    writer = api.CreateFileW(str(discovery.path), 0x40000000, 7, None, 3, 0, None)
    assert writer != ctypes.c_void_p(-1).value
    try:
        reader = api.CreateFileW(str(discovery.path), 0x80000000, 1, None, 3, 0x00200000, None)
        if reader != ctypes.c_void_p(-1).value:
            api.CloseHandle(reader)
            pytest.fail("A read-only sharing mode unexpectedly coexisted with a writer")
        assert ctypes.get_last_error() == 32
        assert len(discovery.service.discover()) == 1
        assert discovery.service.last_diagnostics == []
    finally:
        api.CloseHandle(writer)


def test_discovery_retries_changed_snapshot_and_clears_old_diagnostics(discovery, monkeypatch):
    original = ipc._read_discovery
    calls = []
    def publishing(path):
        calls.append(path)
        if len(calls) == 1:
            raise ipc._DiscoveryReadError("discovery_changed", retryable=True)
        return original(path)
    monkeypatch.setattr(ipc, "_read_discovery", publishing)
    discovery.service.last_diagnostics = [{"code": "previous_rejection"}]
    assert len(discovery.service.discover()) == 1
    assert len(calls) == 2 and discovery.service.last_diagnostics == []


def test_discovery_retries_partial_json_without_skipping_identity_checks(discovery, monkeypatch):
    original = ipc._read_discovery
    complete = discovery.path.read_bytes()
    calls = []
    def publishing(path):
        calls.append(path)
        path.write_bytes(b'{"schema":' if len(calls) == 1 else complete)
        return original(path)
    monkeypatch.setattr(ipc, "_read_discovery", publishing)
    discovery.process.exe = lambda: str(discovery.root / "wrong.exe")
    assert discovery.service.discover() == []
    assert len(calls) == 2
    assert discovery.service.last_diagnostics == [
        {"code": "discovery_executable_mismatch", "process_id": 123}]


def test_discovery_transient_failure_is_bounded_and_secret_free(discovery, monkeypatch):
    calls = []
    def busy(path):
        calls.append(path)
        raise ipc._DiscoveryReadError("discovery_sharing_violation", retryable=True)
    monkeypatch.setattr(ipc, "_read_discovery", busy)
    assert discovery.service.discover() == []
    assert len(calls) == 3
    assert discovery.service.last_diagnostics == [{"code": "discovery_sharing_violation"}]
    def denied(path):
        calls.append(path)
        raise PermissionError(TOKEN + PIPE)
    calls.clear()
    monkeypatch.setattr(ipc, "_read_discovery", denied)
    assert discovery.service.discover() == [] and len(calls) == 1
    assert discovery.service.last_diagnostics == [{"code": "discovery_access_denied"}]
    assert TOKEN not in repr(discovery.service.last_diagnostics)
    assert PIPE not in repr(discovery.service.last_diagnostics)


def test_discovery_retry_budget_is_shared_across_files(discovery, monkeypatch):
    for pid in range(124, 144):
        (discovery.root / f"browser-{pid}-456.json").touch()
    clock, calls, sleeps = [0.0], [], []
    def busy(path):
        calls.append(path)
        raise ipc._DiscoveryReadError("discovery_changed", retryable=True)
    def sleep(delay):
        sleeps.append(delay)
        clock[0] += delay
    monkeypatch.setattr(ipc, "_read_discovery", busy)
    monkeypatch.setattr(ipc.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(ipc.time, "sleep", sleep)
    assert discovery.service.discover() == []
    assert sum(sleeps) <= ipc._DISCOVERY_RETRY_BUDGET
    assert len(calls) <= 21 + 8


def test_discovery_keeps_process_creation_gate_and_reports_reason(discovery):
    discovery.process.create_time = lambda: time.time() + 100
    assert discovery.service.discover() == []
    assert discovery.service.last_diagnostics == [
        {"code": "discovery_predates_process", "process_id": 123}]


def test_discovery_reports_service_user_path_difference_without_cross_user_scan(discovery, monkeypatch):
    monkeypatch.delenv("WEB_AGENT_XIAORUO_IPC_DISCOVERY_ROOT", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(discovery.root / "system-profile"))
    service = ipc.NativeBrowserDiscovery(trusted_executable=discovery.executable,
                                        psutil_api=discovery.service._process_api)
    assert service.discover() == []
    assert service.last_diagnostics == [{"code": "discovery_root_missing"}]
    assert len(discovery.service.discover()) == 1


async def test_auth_initialize_notification_order_and_short_lived_pipe(discovery):
    client, opened = fake_client(discovery)
    result = await client.tabs()
    transport = opened[0]
    assert [item["method"] for item in transport.requests] == [
        "xiaoruo/auth", "initialize", "notifications/initialized", "tools/call"]
    assert transport.requests[0]["params"] == {"token": TOKEN}
    assert "id" not in transport.requests[2]
    assert result["tabs"] == [] and transport.closed
    assert TOKEN not in json.dumps(result) and PIPE not in json.dumps(result)
    assert "authToken" not in result and "pipeName" not in result


async def test_tools_list_and_text_only_sdk_results(discovery):
    client, opened = fake_client(discovery, result_mode="text")
    assert (await client.tools())["tools"][0]["name"] == "example"
    assert (await client.tabs())["tabs"] == []
    assert len(opened) == 2 and all(item.closed for item in opened)


async def test_json_text_content_redacts_escaped_credentials_and_pipe_names(discovery):
    client, _opened = fake_client(discovery, result_mode="text")
    result = await client.call_tool("xiaoruo_browser_tabs")
    text = json.loads(result["texts"][0])
    assert "authToken" not in text and "pipeName" not in text
    assert TOKEN not in json.dumps(result) and PIPE not in json.dumps(result)


async def test_discovery_file_or_process_lifetime_change_fails_before_connection(discovery):
    client, opened = fake_client(discovery)
    rewrite(discovery, authToken="c" * 64)
    with pytest.raises(ipc.NativeIPCError, match="identity changed") as error:
        await client.tabs()
    assert error.value.code == "native_instance_stale" and not opened
    client, opened = fake_client(discovery)
    discovery.process.create_time = lambda: time.time() + 100
    with pytest.raises(ipc.NativeIPCError):
        await client.tabs()
    assert not opened


async def test_task_acceptance_is_polled_to_terminal_on_one_connection(discovery):
    client, opened = fake_client(discovery)
    result = await client.execute("screenshot", {"tab_id": 2})
    assert result["ok"] and result["status"] == "succeeded"
    assert result["task_id"] == "xr-123-1" and result["images"][0]["mimeType"] == "image/png"
    assert len(opened) == 1 and opened[0].closed
    calls = [item["params"] for item in opened[0].requests if item["method"] == "tools/call"]
    assert [item["name"] for item in calls] == ["xiaoruo_browser_start_task", "xiaoruo_browser_task_status", "xiaoruo_browser_task_status"]
    assert all(item["arguments"] == {"task_id": "xr-123-1"} for item in calls[1:])


async def test_accepted_task_timeout_keeps_id_and_never_resubmits(discovery):
    client, opened = fake_client(discovery, states=["accepted"])
    result = await client.execute("navigate", {"tab_id": 2}, timeout=1.3)
    assert not result["ok"] and result["status"] == "unknown" and result["task_id"] == "xr-123-1"
    assert result["error"]["code"] == "native_ipc_timeout" and not result["retryable"]
    starts = [item for item in opened[0].requests if item.get("params", {}).get("name") == "xiaoruo_browser_start_task"]
    assert len(starts) == 1 and len(opened) == 1 and opened[0].closed


async def test_terminal_failure_preserves_native_reason_and_never_retries(discovery):
    client, opened = fake_client(discovery, states=["failed"])
    result = await client.execute("screenshot", {"tab_id": 2})
    assert not result["ok"] and result["status"] == "failed"
    assert result["error"]["code"] == "unsupported" and not result["retryable"]
    assert len(opened[0].requests) == 4


@pytest.mark.parametrize(("state", "code"), [
    ("timed_out", "task_timeout"), ("cancelled", "task_cancelled"),
])
async def test_native_terminal_timeout_and_cancellation_keep_task_id(discovery, state, code):
    client, opened = fake_client(discovery, states=["accepted", state])
    result = await client.execute("screenshot", {"tab_id": 2})
    assert not result["ok"] and result["status"] == state
    assert result["task_id"] == "xr-123-1" and result["error"]["code"] == code
    assert not result["retryable"] and opened[0].closed
    calls = [item["params"]["name"] for item in opened[0].requests if item["method"] == "tools/call"]
    assert calls == ["xiaoruo_browser_start_task", "xiaoruo_browser_task_status"]


class DeadlineTransport(Transport):
    """The native task expires using its submitted budget, not the client clock."""

    def write(self, raw, deadline):
        request = json.loads(raw)
        tool = request.get("params", {}).get("name")
        if tool == "xiaoruo_browser_start_task":
            self.start_remaining_ms = (deadline - time.monotonic()) * 1000
            self.native_timeout_ms = request["params"]["arguments"]["timeout_ms"]
            self.native_deadline = time.monotonic() + self.native_timeout_ms / 1000
            self.states = ["accepted"]
        elif tool == "xiaoruo_browser_task_status":
            self.states = ["timed_out" if time.monotonic() >= self.native_deadline else "running"]
        super().write(raw, deadline)


async def test_native_deadline_leaves_time_to_observe_terminal_timeout(discovery):
    client, opened = fake_client(discovery, transport_class=DeadlineTransport)
    result = await client.execute("screenshot", {"tab_id": 2}, timeout=1.5)
    assert result["status"] == "timed_out" and result["error"]["code"] == "task_timeout"
    assert result["task_id"] == "xr-123-1" and not result["retryable"]
    transport = opened[0]
    assert 1000 <= transport.native_timeout_ms < transport.start_remaining_ms - 200
    assert time.monotonic() < transport.deadline and transport.closed
    assert sum(item.get("params", {}).get("name") == "xiaoruo_browser_start_task"
               for item in transport.requests) == 1


@pytest.mark.parametrize(("timeout", "requested", "expected"), [
    (30, None, 29000), (300, None, 120000),
    (30, 1000, 1000), (30, 120000, 29000), (2, 3000, 1600),
])
async def test_native_timeout_budget_reserves_response_time_and_caps_explicit_value(
        discovery, timeout, requested, expected):
    client, opened = fake_client(discovery, states=["succeeded"])
    arguments = {"tab_id": 2}
    if requested is not None:
        arguments["timeout_ms"] = requested
    original = dict(arguments)
    result = await client.execute("screenshot", arguments, timeout=timeout)
    assert result["ok"] and arguments == original
    sent = opened[0].requests[-1]["params"]["arguments"]["timeout_ms"]
    assert expected - 200 <= sent <= expected
    if requested == 1000:
        assert sent == 1000


@pytest.mark.parametrize("requested", [None, 1000, 120000])
async def test_short_outer_deadline_fails_before_dispatch(discovery, requested):
    client, opened = fake_client(discovery)
    arguments = {} if requested is None else {"timeout_ms": requested}
    result = await client.execute("screenshot", arguments, timeout=.2)
    assert result["status"] == "failed" and result["error"]["code"] == "native_ipc_timeout"
    assert "task_id" not in result and not result["retryable"]
    assert len(opened[0].requests) == 3 and opened[0].closed


@pytest.mark.parametrize("requested", [True, False, None, "1000", 1000.0, float("nan"),
                                      float("inf"), 0, -1, 999, 120001, [], {}])
async def test_invalid_explicit_native_timeout_never_opens_pipe(discovery, requested):
    client, opened = fake_client(discovery)
    result = await client.execute("screenshot", {"timeout_ms": requested})
    assert result["status"] == "failed" and result["error"]["code"] == "invalid_argument"
    assert not result["retryable"] and not opened


@pytest.mark.parametrize("code", ["native_ipc_eof", "native_ipc_timeout", "native_ipc_protocol"])
async def test_status_error_keeps_accepted_id_and_does_not_replay(discovery, code):
    class StatusFailure(Transport):
        def readline(self, deadline):
            if self.requests[-1].get("params", {}).get("name") == "xiaoruo_browser_task_status":
                raise ipc.NativeIPCError(code, "Static safe status error")
            return super().readline(deadline)
    client, opened = fake_client(discovery, transport_class=StatusFailure, states=["accepted"])
    result = await client.execute("screenshot", {"tab_id": 2})
    assert result["status"] == "unknown" and result["task_id"] == "xr-123-1"
    assert result["error"]["code"] == code and not result["retryable"]
    assert len(opened) == 1 and opened[0].closed
    calls = [item["params"]["name"] for item in opened[0].requests if item["method"] == "tools/call"]
    assert calls == ["xiaoruo_browser_start_task", "xiaoruo_browser_task_status"]


async def test_cancelled_execute_during_status_read_releases_single_client_lock(discovery):
    entered, loop = asyncio.Event(), asyncio.get_running_loop()
    class PendingStatus(Transport):
        def readline(self, deadline):
            if self.requests[-1].get("params", {}).get("name") == "xiaoruo_browser_task_status":
                loop.call_soon_threadsafe(entered.set)
                while not self.cancelled:
                    time.sleep(.005)
                raise ipc.NativeIPCError("native_ipc_io", "Cancelled local status I/O")
            return super().readline(deadline)
    client, opened = fake_client(discovery, transport_class=PendingStatus, states=["accepted"])
    operation = asyncio.create_task(client.execute("screenshot", {"tab_id": 2}))
    await asyncio.wait_for(entered.wait(), 1)
    operation.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(operation, 1)
    assert len(opened) == 1 and opened[0].cancelled and opened[0].closed
    assert sum(item.get("params", {}).get("name") == "xiaoruo_browser_start_task"
               for item in opened[0].requests) == 1
    assert (await client.tabs())["tabs"] == []
    assert len(opened) == 2 and opened[1].closed


async def test_rejected_task_preserves_valid_native_task_id(discovery):
    class Rejected(Transport):
        def tool_result(self, value, images=False):
            return {**super().tool_result(value, images), "isError": True}
    client, opened = fake_client(discovery, transport_class=Rejected, states=["failed"])
    result = await client.execute("screenshot", {"tab_id": 2})
    assert result["status"] == "failed" and result["task_id"] == "xr-123-1"
    assert result["error"]["code"] == "native_task_rejected" and not result["retryable"]
    assert opened[0].closed and len(opened[0].requests) == 4


@pytest.mark.parametrize("code", ["native_ipc_eof", "native_ipc_timeout"])
async def test_unknown_write_on_disconnect_is_not_replayed(discovery, code):
    client, opened = fake_client(discovery, failure=ipc.NativeIPCError(code, "Static safe error"))
    result = await client.execute("click", {"tab_id": 2, "selector": "#send"})
    assert result["status"] == "unknown" and not result["ok"] and not result["retryable"]
    assert len(opened) == 1 and opened[0].closed


async def test_rpc_id_utf8_and_auth_failures_close_pipe(discovery):
    for response, code in [
        (b'{"jsonrpc":"2.0","id":999,"result":{}}', "native_ipc_protocol"),
        (b'\xff\n', "native_ipc_protocol"),
        (json.dumps({"jsonrpc": "2.0", "id": 1, "error": {"code": -32001, "message": TOKEN + PIPE}}).encode(), "native_auth_failed"),
    ]:
        client, opened = fake_client(discovery)
        factory = client._transport_factory
        def malformed(instance, deadline):
            transport = factory(instance, deadline)
            transport.readline = lambda deadline: response
            return transport
        client._transport_factory = malformed
        with pytest.raises(ipc.NativeIPCError) as error:
            await client.tabs()
        assert error.value.code == code and opened[0].closed
        assert TOKEN not in repr(error.value) and PIPE not in repr(error.value)


async def test_request_and_response_limits(discovery, monkeypatch):
    client, opened = fake_client(discovery)
    async with client.session() as session:
        with pytest.raises(ipc.NativeIPCError) as error:
            await session.call_tool("example", {"value": "x" * ipc.MAX_REQUEST})
        assert error.value.code == "native_ipc_limit"
        assert len(opened[0].requests) == 3
        opened[0].readline = lambda deadline: b"x" * 101
        monkeypatch.setattr(ipc, "MAX_RESPONSE", 100)
        with pytest.raises(ipc.NativeIPCError) as error:
            await session.call_tool("example")
        assert error.value.code == "native_ipc_limit"


async def test_large_upload_requires_explicit_adapter_allowance(discovery, monkeypatch):
    monkeypatch.setattr(ipc, "MAX_REQUEST", 600)
    monkeypatch.setattr(ipc, "MAX_UPLOAD_REQUEST", 2400)
    payload = {"tab_id": 2, "selector": "#file", "file_name": "test.txt",
               "file_data_base64": "YQ==" * 250}
    client, opened = fake_client(discovery, states=["succeeded"])
    ordinary = await client.execute("upload_file", payload)
    assert ordinary["error"]["code"] == "native_ipc_limit"
    assert ordinary["status"] == "failed" and not ordinary["retryable"]
    assert len(opened[0].requests) == 3
    allowed = await client.execute("upload_file", payload, _allow_upload_request=True)
    assert allowed["ok"] and len(opened[1].requests) == 4
    assert opened[1].requests[-1]["params"]["arguments"]["action"] == "upload_file"
    wrong_action = await client.execute("click", payload, _allow_upload_request=True)
    assert wrong_action["error"]["code"] == "native_ipc_limit"
    assert len(opened[2].requests) == 3


async def test_upload_allowance_remains_bounded_and_generic_calls_stay_small(discovery, monkeypatch):
    monkeypatch.setattr(ipc, "MAX_REQUEST", 600)
    monkeypatch.setattr(ipc, "MAX_UPLOAD_REQUEST", 2400)
    client, opened = fake_client(discovery, states=["succeeded"])
    result = await client.execute("upload_file", {"file_data_base64": "YQ==" * 1000},
                                  _allow_upload_request=True)
    assert result["error"]["code"] == "native_ipc_limit" and result["status"] == "failed"
    assert len(opened[0].requests) == 3
    with pytest.raises(ipc.NativeIPCError) as error:
        await client.call_tool("xiaoruo_browser_start_task",
                               {"action": "upload_file", "file_data_base64": "YQ==" * 250})
    assert error.value.code == "native_ipc_limit" and len(opened[1].requests) == 3


async def test_in_process_workflows_serialize_with_bounded_busy(discovery):
    client, opened = fake_client(discovery)
    async with client.session():
        with pytest.raises(ipc.NativeIPCError) as error:
            await client.tabs(timeout=.03)
        assert error.value.code == "native_ipc_busy" and len(opened) == 1
    assert (await client.tabs())["tabs"] == []


def test_cross_process_file_lease_is_bounded_and_reusable():
    first, second = ipc._FileLease("unit-test-bounded-lease"), ipc._FileLease("unit-test-bounded-lease")
    first.acquire(time.monotonic() + 1)
    try:
        with pytest.raises(ipc.NativeIPCError) as error:
            second.acquire(time.monotonic() + .03)
        assert error.value.code == "native_ipc_busy"
    finally:
        first.close()
    second.acquire(time.monotonic() + 1)
    second.close()


async def test_cancelled_read_cancels_and_closes_transport(discovery):
    client, opened = fake_client(discovery)
    entered = asyncio.Event()
    async def operation():
        async with client.session() as session:
            def blocking(deadline):
                while not session.transport.cancelled:
                    time.sleep(.005)
                raise ipc.NativeIPCError("native_ipc_io", "Cancelled local I/O")
            session.transport.readline = blocking
            entered.set()
            await session.call_tool("example")
    task = asyncio.create_task(operation())
    await entered.wait()
    # Let write complete and the fake read enter its worker thread.
    await asyncio.sleep(.025)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 1)
    assert opened[0].cancelled and opened[0].closed


class WinAPI:
    def __init__(self, server_pid=123):
        self.server_pid, self.closed, self.cancelled = server_pid, [], []
    def CreateFileW(self, *args):
        return 1000
    def GetNamedPipeServerProcessId(self, handle, target):
        target._obj.value = self.server_pid
        return True
    def CloseHandle(self, handle):
        self.closed.append(handle)
    def CancelIoEx(self, handle, overlapped):
        self.cancelled.append(handle)


def test_win32_server_pid_is_verified_before_any_rpc(discovery, monkeypatch):
    instance = discovery.service.discover()[0]
    api = WinAPI(server_pid=999)
    monkeypatch.setattr(ipc, "_kernel", lambda: api)
    with pytest.raises(ipc.NativeIPCError) as error:
        ipc._WinPipeTransport(instance, time.monotonic() + 1)
    assert error.value.code == "native_ipc_identity"
    assert api.closed == [1000] and api.cancelled == [1000]


@pytest.mark.skipif(not hasattr(ctypes, "set_last_error"), reason="Win32 error state")
def test_win32_pending_read_timeout_cancels_overlapped_and_closes_event():
    class Pending(WinAPI):
        def __init__(self):
            super().__init__()
            self.waits = [258, 0]
        def CreateEventW(self, *args):
            return 2000
        def ReadFile(self, *args):
            ctypes.set_last_error(997)
            return False
        def WaitForSingleObject(self, *args):
            return self.waits.pop(0)
    transport = ipc._WinPipeTransport.__new__(ipc._WinPipeTransport)
    transport.api, transport.handle, transport._pending = Pending(), 1000, []
    with pytest.raises(ipc.NativeIPCError) as error:
        transport._io(None, 10, time.monotonic() + .1, False)
    assert error.value.code == "native_ipc_timeout"
    assert transport.api.cancelled == [1000] and transport.api.closed == [2000]
    transport.close()
    assert transport.api.closed == [2000, 1000]


def test_stream_framing_accumulates_partial_utf8_and_keeps_following_frame():
    transport = ipc._WinPipeTransport.__new__(ipc._WinPipeTransport)
    transport.buffer = bytearray()
    chunks = [b'{"value":"\xe4', b'\xb8\xad"}\n{"next":true}\n']
    transport._io = lambda *args: chunks.pop(0)
    assert json.loads(transport.readline(time.monotonic() + 1)) == {"value": "中"}
    assert json.loads(transport.readline(time.monotonic() + 1)) == {"next": True}


async def test_invalid_action_input_is_rejected_before_opening_pipe(discovery):
    client, opened = fake_client(discovery)
    result = await client.execute("click", ["invalid"])
    assert result["error"]["code"] == "invalid_argument" and not opened
