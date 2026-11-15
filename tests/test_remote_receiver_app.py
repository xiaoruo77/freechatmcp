"""Application wiring for independent receivers; no external network or GUI."""
from __future__ import annotations

import asyncio
from dataclasses import replace
import hashlib
from io import BytesIO
import json
from pathlib import Path
import time

from fastapi.testclient import TestClient
from mcp.server.auth.provider import AccessToken
import pytest
from PIL import Image

from web_tools.remote_gui_capture import CaptureTicketManager


ORIGIN = "https://receiver.example"
BRIDGE_TOKEN = "receiver-app-test-bridge-credential-" * 2
INSTANCE = "1" * 32


@pytest.fixture(autouse=True)
def no_real_local_input_or_background_bridge(monkeypatch):
    from web_tools import background_desktop_client
    from web_tools.windows_mcp import WindowsMCPBridge
    from web_mcp import direct_tools
    def forbidden_request(*args, **kwargs):
        raise AssertionError("Receiver app tests must not contact a real background worker")
    async def forbidden_input(*args, **kwargs):
        raise AssertionError("Receiver app tests must not invoke local Windows input")
    async def unavailable_uu(settings, action, arguments=None, **kwargs):
        assert action == "uu"
        return {"ok": False, "status": "failed", "terminal": True,
            "result": {"data": {"ok": False, "error_code": "uu_device_offline",
                                 "phase": "connect", "action_may_have_run": False}},
            "error_code": "uu_device_offline",
            "evidence": {"host_input_used": False, "action_may_have_run": False}}
    monkeypatch.setattr(background_desktop_client, "_request", forbidden_request)
    monkeypatch.setattr(WindowsMCPBridge, "call", forbidden_input)
    monkeypatch.setattr(direct_tools, "call_background", unavailable_uu)


def auth(token):
    return {"Authorization": f"Bearer {token}"}


def receiver_settings(base, tmp_path, *, enabled=True, oauth=False, admin=True):
    token_path = tmp_path / "receiver-test-bridge.token"
    token_path.write_text(BRIDGE_TOKEN, encoding="utf-8")
    background = tmp_path / "receiver-test-background.json"
    background.write_text(json.dumps({"token_file": str(token_path),
                                     "capture_public_origin": ORIGIN}), encoding="utf-8")
    receiver = tmp_path / "receiver-test-config.json"
    receiver.write_text(json.dumps({"enabled": enabled, "public_origin": ORIGIN,
        "preferred_transport": "uu", "devices": {"lab": {"uu_device_name": "Test UU Lab",
            "receiver_device_name": "lab", "expected_computer": "LAB-PC"}}}), encoding="utf-8")
    return replace(base, orchestration_mode="chatgpt_direct", admin_mode=admin,
                   oauth_enabled=oauth, oauth_issuer_url="http://127.0.0.1",
                   oauth_resource_url="http://127.0.0.1/mcp", oauth_scopes=["web_agent"],
                   workspace_root=tmp_path, allowed_workspaces=[tmp_path],
                   extra={**base.extra, "background_desktop_config": str(background),
                          "remote_receiver_config": str(receiver)})


def rpc(client, token, method, params=None):
    response = client.post("/mcp", headers={**auth(token), "Host": "127.0.0.1:48271",
        "Accept": "application/json, text/event-stream", "MCP-Protocol-Version": "2025-06-18"},
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}})
    assert response.status_code == 200
    if response.headers.get("content-type", "").startswith("text/event-stream"):
        packet = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")][-1]
    else:
        packet = response.json()
    assert "error" not in packet
    return packet["result"]


def remote_rpc(client, settings, action, arguments=None, *, confirm=False, via_workflow=False,
               timeout_seconds=10):
    values = {"action": action, "arguments": arguments or {}, "confirm": confirm}
    request = {"name": "remote_computer", "arguments": values}
    if via_workflow:
        request = {"name": "run_workflow", "arguments": {"workflow": {
            "steps": [{"tool": "remote_computer", "arguments": values}]}, "timeout_seconds": timeout_seconds}}
    return rpc(client, settings.token, "tools/call", request)


def call(client, settings, action, arguments=None, *, confirm=False, via_workflow=False,
         timeout_seconds=10):
    raw = remote_rpc(client, settings, action, arguments, confirm=confirm, via_workflow=via_workflow,
                     timeout_seconds=timeout_seconds)
    result = raw["structuredContent"]
    assert {"ok", "status", "terminal", "operation", "result", "evidence"} <= result.keys()
    assert result["operation"] == ("run_workflow" if via_workflow else "remote_computer")
    if result["ok"] is True or "remote_tool" in result:
        assert result["remote_tool"] == "remote_computer"
    assert raw.get("isError", False) == (not result["ok"])
    return result


def enroll(client, app):
    invitation = app.state.remote_receiver_manager.issue_enrollment("lab", "LAB-PC")
    response = client.post("/remote-receiver/enroll", headers=auth(invitation["enrollment_token"]),
                           json={"enrollment_id": invitation["enrollment_id"], "computer_name": "LAB-PC"})
    assert response.status_code == 200
    return response.json(), invitation


def poll(client, device, session_id=None):
    body = {"device_id": device["device_id"], "computer_name": "LAB-PC", "instance_id": INSTANCE}
    if session_id:
        body["session_id"] = session_id
    response = client.post("/remote-receiver/poll", headers=auth(device["device_token"]), json=body)
    assert response.status_code == 200
    return response.json()["command"]


def reply_start(client, device, command):
    response = client.post("/remote-receiver/reply", headers=auth(device["device_token"]), json={
        "device_id": device["device_id"], "command_id": command["command_id"],
        "session_id": command["session_id"], "ok": True,
        "result": {"runtime": {"computer": "LAB-PC", "session_id": 1, "active_session_id": 1,
            "window_station": "WinSta0", "thread_desktop": "Default", "input_desktop": "Default",
            "helper_pid": 345, "helper_creation_time_utc_ticks": "638980000000000000"},
            "foreground": {"available": True, "hwnd": 123, "pid": 456,
                           "creation_time_utc_ticks": "638980000000000000"}}})
    assert response.status_code == 200


def test_receiver_factory_uses_default_project_config_when_extra_is_absent(isolated_settings, tmp_path):
    from web_mcp.server import _configured_receiver_manager
    project = tmp_path / "fake-production-project"
    config = project / "07_web_config" / "remote_receiver.json"
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({"enabled": True, "public_origin": ORIGIN}), encoding="utf-8")
    settings = replace(isolated_settings, project_root=project,
        extra={key: value for key, value in isolated_settings.extra.items() if key != "remote_receiver_config"})
    captures = CaptureTicketManager(settings.data_root, ORIGIN)
    manager = _configured_receiver_manager(settings, captures)
    try:
        assert manager is not None
        assert manager.public_origin == ORIGIN
        assert manager.db_path.is_relative_to(settings.data_root)
    finally:
        if manager:
            manager.close()


def test_explicit_disabled_receiver_config_overrides_enabled_project_default(isolated_settings, tmp_path):
    from web_mcp.server import _configured_receiver_manager
    project = tmp_path / "fake-production-project"
    default = project / "07_web_config" / "remote_receiver.json"
    default.parent.mkdir(parents=True)
    default.write_text(json.dumps({"enabled": True, "public_origin": ORIGIN}), encoding="utf-8")
    override = tmp_path / "receiver-disabled.json"
    override.write_text(json.dumps({"enabled": False, "public_origin": ORIGIN}), encoding="utf-8")
    settings = replace(isolated_settings, project_root=project,
                       extra={**isolated_settings.extra, "remote_receiver_config": str(override)})
    assert _configured_receiver_manager(settings, CaptureTicketManager(settings.data_root, ORIGIN)) is None


@pytest.mark.parametrize("enabled", [False, None])
def test_disabled_or_missing_config_exposes_no_receiver_routes(isolated_settings, tmp_path, enabled):
    from web_mcp.server import create_app
    settings = receiver_settings(isolated_settings, tmp_path, enabled=False)
    if enabled is None:
        Path(settings.extra["remote_receiver_config"]).unlink()
    app = create_app(settings)
    assert app.state.remote_receiver_manager is None
    assert app.state.remote_receiver_controller is None
    paths = {getattr(route, "path", None) for route in app.routes}
    assert not {"/remote-receiver/enroll", "/remote-receiver/poll", "/remote-receiver/reply"} & paths
    app.state.remote_gui_capture_manager.close()


def test_receiver_enrollment_remains_available_without_capture_service(isolated_settings, tmp_path):
    from web_mcp.server import create_app
    settings = receiver_settings(isolated_settings, tmp_path)
    Path(settings.extra["background_desktop_config"]).unlink()
    app = create_app(settings)
    assert app.state.remote_gui_capture_manager is None
    assert app.state.remote_receiver_manager is not None
    with TestClient(app) as client:
        enroll(client, app)
        result = client.portal.call(app.state.remote_receiver_controller.perform, "screenshot", {
            "session_id": "1" * 32, "operation_id": "no-capture"})
        assert result["error_code"] == "remote_capture_unavailable"
        assert result["host_input_used"] is False


def test_oauth_enabled_receiver_protocol_uses_only_enrollment_and_device_credentials(
        isolated_settings, tmp_path, monkeypatch):
    from web_mcp.server import create_app
    settings = receiver_settings(isolated_settings, tmp_path, oauth=True)
    app = create_app(settings)
    with TestClient(app) as client:
        observed = []
        provider = app.state.web_agent.oauth_provider
        original = provider.load_access_token
        async def observe(token):
            observed.append(token)
            return await original(token)
        monkeypatch.setattr(provider, "load_access_token", observe)
        device, _ = enroll(client, app)
        start = app.state.remote_receiver_manager.start_session("lab", "connect-protocol")
        command = poll(client, device)
        assert command["command_id"] == start["command_id"]
        assert command["action"] == "start"
        reply_start(client, device, command)
        assert app.state.remote_receiver_manager.session_status(start["session_id"])["state"] == "ready"
        assert observed == []
        assert client.get("/api/config", headers=auth(device["device_token"])).status_code == 401
        assert device["device_token"] in observed
        assert client.get("/api/config", headers=auth(settings.token)).status_code == 200


@pytest.mark.parametrize("credential", ["admin", "oauth", "enrollment"])
def test_mcp_and_enrollment_credentials_cannot_impersonate_receiver_device(
        isolated_settings, tmp_path, credential):
    from web_mcp.server import create_app
    settings = receiver_settings(isolated_settings, tmp_path, oauth=True)
    app = create_app(settings)
    with TestClient(app) as client:
        device, invitation = enroll(client, app)
        supplied = settings.token if credential == "admin" else invitation["enrollment_token"]
        if credential == "oauth":
            supplied = "receiver-app-persisted-oauth-test-token"
            async def persist():
                await app.state.web_agent.oauth_provider._store_access_token(AccessToken(
                    token=supplied, client_id="receiver-app-test", scopes=["web_agent"],
                    expires_at=int(time.time()) + 300, resource=settings.oauth_resource_url))
                await app.state.web_agent.db.commit()
            client.portal.call(persist)
            assert client.get("/api/config", headers=auth(supplied)).status_code == 200
        response = client.post("/remote-receiver/poll", headers=auth(supplied), json={
            "device_id": device["device_id"], "computer_name": "LAB-PC", "instance_id": INSTANCE})
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "receiver_authentication_required"


def test_only_exact_receiver_post_routes_bypass_global_oauth(isolated_settings, tmp_path, monkeypatch):
    from web_mcp.server import create_app
    settings = receiver_settings(isolated_settings, tmp_path, oauth=True)
    app = create_app(settings)
    with TestClient(app) as client:
        device, _ = enroll(client, app)
        observed = []
        provider = app.state.web_agent.oauth_provider
        original = provider.load_access_token
        async def observe(token):
            observed.append(token)
            return await original(token)
        monkeypatch.setattr(provider, "load_access_token", observe)
        for method, path in [
            ("GET", "/remote-receiver/enroll"), ("GET", "/remote-receiver/poll"),
            ("DELETE", "/remote-receiver/reply"), ("POST", "/remote-receiver/enroll/"),
            ("POST", "/remote-receiver/poll/extra"), ("POST", "/remote-receiver"),
        ]:
            count = len(observed)
            client.request(method, path, headers=auth(device["device_token"]), follow_redirects=False)
            assert len(observed) == count + 1, (method, path)
        # Device credentials never authenticate the MCP transport.
        mcp = client.post("/mcp", headers={**auth(device["device_token"]), "Host": "127.0.0.1:48271",
            "Accept": "application/json, text/event-stream"}, json={"jsonrpc": "2.0", "id": 1,
                "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                    "clientInfo": {"name": "receiver-app-test", "version": "1"}}})
        assert mcp.status_code == 401


def test_direct_receiver_tool_requires_admin_and_confirmation_before_write(isolated_settings, tmp_path):
    from web_mcp.server import create_app
    settings = receiver_settings(isolated_settings, tmp_path, admin=False)
    with TestClient(create_app(settings)) as client:
        assert call(client, settings, "devices")["ok"] is True
        denied = call(client, settings, "connect", {"device_name": "lab", "operation_id": "connect"}, confirm=True)
        assert denied["ok"] is False
        assert denied["error_code"] == "permission_denied"
    settings = replace(settings, admin_mode=True)
    app = create_app(settings)
    with TestClient(app) as client:
        enroll(client, app)
        denied = call(client, settings, "connect", {"device_name": "lab", "operation_id": "connect"})
        assert denied["ok"] is False
        assert not app.state.remote_receiver_manager.devices()[0].get("session_id")


@pytest.mark.parametrize("via_workflow", [False, True])
def test_direct_receiver_mcp_preserves_queued_dispatched_and_ready_envelopes(
        isolated_settings, tmp_path, monkeypatch, via_workflow):
    from web_mcp.server import create_app
    from web_tools.windows_mcp import WindowsMCPBridge
    async def forbidden(*args, **kwargs):
        raise AssertionError("Receiver tool attempted local Windows input")
    monkeypatch.setattr(WindowsMCPBridge, "call", forbidden)
    settings = receiver_settings(isolated_settings, tmp_path)
    app = create_app(settings)
    with TestClient(app) as client:
        assert app.state.web_agent.registry.direct_tools.remote_receiver_controller is app.state.remote_receiver_controller
        tools = rpc(client, settings.token, "tools/list")["tools"]
        assert "remote_computer" in {tool["name"] for tool in tools}
        device, _ = enroll(client, app)
        queued = call(client, settings, "connect", {"device_name": "lab", "operation_id": "connect"},
                      confirm=True, via_workflow=via_workflow)
        assert queued["status"] == "queued" and queued["terminal"] is False, queued.get("error_code")
        assert queued.get("completed") is not True
        session_id = queued["session_id"]
        command = poll(client, device)
        dispatched = call(client, settings, "command_status", {"session_id": session_id,
            "command_id": command["command_id"]}, via_workflow=via_workflow)
        assert dispatched["status"] == "dispatched" and dispatched["terminal"] is False
        assert dispatched.get("completed") is not True
        assert dispatched["remote_effect_verified"] is False
        reply_start(client, device, command)
        ready = call(client, settings, "session_status", {"session_id": session_id}, via_workflow=via_workflow)
        assert ready["status"] == "ready" and ready["terminal"] is False
        assert ready.get("completed") is not True
        assert ready["host_input_used"] is ready["foreground_input_used"] is False
        assert ready["remote_effect_verified"] is False


@pytest.mark.parametrize("via_workflow", [False, True])
@pytest.mark.parametrize("mime_type", ["image/png", "image/jpeg"])
def test_receiver_uploaded_png_is_verified_and_returned_as_mcp_image(isolated_settings, tmp_path, via_workflow, mime_type):
    from web_mcp.server import create_app
    settings = receiver_settings(isolated_settings, tmp_path)
    app = create_app(settings)
    with TestClient(app) as client:
        device, _ = enroll(client, app)
        connected = call(client, settings, "connect", {
            "device_name": "lab", "operation_id": "start-for-capture"}, confirm=True, via_workflow=via_workflow)
        assert connected["ok"] is True, connected.get("error_code")
        start = poll(client, device)
        reply_start(client, device, start)
        session_id = connected["session_id"]
        queued = call(client, settings, "screenshot", {"session_id": session_id,
            "operation_id": "capture"}, via_workflow=via_workflow)
        assert queued["status"] == "queued" and queued["terminal"] is False
        command = poll(client, device, start["session_id"])
        ticket = command["arguments"]
        output = BytesIO()
        Image.new("RGB", (4, 3), "blue").save(output, format="JPEG" if mime_type == "image/jpeg" else "PNG")
        headers = auth(ticket["token"])
        if mime_type == "image/jpeg":
            headers["Content-Type"] = "image/jpeg"
        upload = client.post("/remote-gui/captures/" + ticket["capture_id"],
                             headers=headers, content=output.getvalue())
        assert upload.status_code == 200
        receipt = {"capture_id": ticket["capture_id"], "sha256": upload.json()["sha256"],
            "width": 4, "height": 3,
            "remote_bounds": {"left": -100, "top": 0, "width": 4, "height": 3},
            "foreground": {"available": True, "hwnd": 100, "pid": 200, "creation_time_utc_ticks": "300"}}
        response = client.post("/remote-receiver/reply", headers=auth(device["device_token"]), json={
            "device_id": device["device_id"], "session_id": command["session_id"],
            "command_id": command["command_id"], "ok": True, "result": receipt})
        assert response.status_code == 200
        raw = remote_rpc(client, settings, "command_status", {"session_id": session_id,
            "command_id": queued["command_id"]}, via_workflow=via_workflow)
        result = raw["structuredContent"]
        assert result["remote_tool"] == "remote_computer"
        assert result["capture_verified"] is True
        assert result["capture"]["mime_type"] == mime_type
        assert Path(result["capture"]["path"]).suffix == (".jpg" if mime_type == "image/jpeg" else ".png")
        assert result["remote_effect_verified"] is False
        images = [block for block in raw["content"] if block["type"] == "image"]
        assert len(images) == 1 and images[0]["mimeType"] == mime_type
        assert ticket["token"] not in json.dumps(raw)


def test_production_alias_shape_preserves_uu_priority_without_receiver_call(
        isolated_settings, tmp_path, monkeypatch):
    from web_mcp import direct_tools
    from web_mcp.server import create_app
    settings = receiver_settings(isolated_settings, tmp_path)
    app = create_app(settings)
    with TestClient(app) as client:
        assert call(client, settings, "status")["ok"] is True
        uu_calls = []
        async def available_uu(settings, action, arguments=None, **kwargs):
            uu_calls.append((action, arguments))
            return {"ok": True, "status": "completed", "terminal": True,
                "result": {"data": {"ok": True, "connected": True, "keyboard_ready": True}}}
        async def forbidden_receiver(*args, **kwargs):
            raise AssertionError("An available UU connection must not call the standby receiver")
        monkeypatch.setattr(direct_tools, "call_background", available_uu)
        monkeypatch.setattr(app.state.remote_receiver_controller, "perform", forbidden_receiver)
        result = call(client, settings, "connect", {"device_name": "lab", "operation_id": "uu-first"}, confirm=True)
        assert result["ok"] is True and result["status"] == "ready"
        assert result["terminal"] is False and result["transport"] == "uu"
        assert uu_calls == [("uu", {"action": "connect", "arguments": {"device_name": "Test UU Lab"}})]
        assert app.state.remote_receiver_manager.devices() == []


@pytest.mark.parametrize("batch_shape", ["mixed_tool", "acceptance", "mixed_runner"])
def test_direct_workflow_rejects_mixed_receiver_batch_before_dispatch(isolated_settings, tmp_path, batch_shape):
    from web_mcp.server import create_app
    settings = receiver_settings(isolated_settings, tmp_path)
    app = create_app(settings)
    with TestClient(app) as client:
        enroll(client, app)
        workflow = {"steps": [{"action": "remote_computer", "args": {
                "action": "connect", "arguments": {"device_name": "lab", "operation_id": "batch"},
                "confirm": True}}]}
        if batch_shape == "mixed_tool":
            workflow["steps"].append({"tool": "list_directory", "arguments": {"path": "."}})
        elif batch_shape == "acceptance":
            workflow["acceptance"] = [{"type": "exists", "path": "."}]
        else:
            workflow["steps"].append({"profile": "python_script", "payload": {"code": "pass"}})
        raw = rpc(client, settings.token, "tools/call", {"name": "run_workflow", "arguments": {
            "workflow": workflow, "timeout_seconds": 10}})
        result = raw["structuredContent"]
        assert result["ok"] is False and result["status"] == "failed"
        message = result["error"]["message"]
        assert ("Use one remote_computer call at a time" in message
                or "Split mixed runner/tool batches" in message)
        assert app.state.remote_receiver_manager.devices()[0]["session_id"] is None


def test_single_remote_workflow_uses_transport_deadline_instead_of_generic_batch_budget(
        isolated_settings, tmp_path, monkeypatch):
    from web_mcp import direct_tools
    from web_mcp.server import create_app
    settings = receiver_settings(isolated_settings, tmp_path)
    async def delayed_uu(settings, action, arguments=None, **kwargs):
        await asyncio.sleep(1.1)
        return {"ok": True, "status": "completed", "terminal": True,
                "result": {"data": {"ok": True, "connected": True, "keyboard_ready": True}}}
    monkeypatch.setattr(direct_tools, "call_background", delayed_uu)
    with TestClient(create_app(settings)) as client:
        result = call(client, settings, "connect", {"device_name": "lab", "operation_id": "slow-uu"},
                      confirm=True, via_workflow=True, timeout_seconds=1)
        assert result["ok"] is True and result["status"] == "ready"
        assert result["terminal"] is False and result["transport"] == "uu"


def _package_stubs(app, monkeypatch):
    from web_tools import remote_receiver_package
    calls = {"enroll": 0, "build": 0}
    simulated_token = "simulated-enrollment-capability-for-tests-only"
    async def enrollment(action, arguments, confirm=False):
        assert action == "enroll" and confirm is True
        calls["enroll"] += 1
        return {"ok": True, "status": "completed", "terminal": True,
            "enrollment_id": "simulated-enrollment", "enrollment_token": simulated_token,
            "device_name": arguments["device_name"], "expected_computer": arguments["expected_computer"],
            "origin": ORIGIN, "expires_at": time.time() + 300}
    def build(invitation, source, output, operation_id):
        assert invitation["enrollment_token"] == simulated_token
        calls["build"] += 1
        key = hashlib.sha256(operation_id.encode()).hexdigest()
        directory = Path(output) / key
        directory.mkdir(parents=True)
        archive = directory / "Xiaoruo1Receiver-DEMO-NODE.zip"
        archive.write_bytes(b"Harmless package-validation fixture; no executable or credential.")
        public = {"path": str(archive), "mime_type": "application/zip", "size_bytes": archive.stat().st_size,
            "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
            "device_name": invitation["device_name"], "expected_computer": invitation["expected_computer"],
            "expires_at": invitation["expires_at"]}
        metadata = {key: value for key, value in public.items() if key not in {"path", "mime_type"}}
        metadata["operation_sha256"] = key
        (directory / "package_metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
        return public
    monkeypatch.setattr(app.state.remote_receiver_controller, "perform", enrollment)
    monkeypatch.setattr(remote_receiver_package, "prepare_receiver_package", build)
    return calls, simulated_token


def test_public_release_does_not_prepare_private_receiver_installer(isolated_settings, tmp_path):
    from web_mcp.server import create_app
    settings = receiver_settings(isolated_settings, tmp_path)
    with TestClient(create_app(settings)) as client:
        out = call(client, settings, "prepare_package", {"device_name": "lab",
                   "expected_computer": "LAB-PC", "operation_id": "public-release"}, confirm=True)
        assert "not supported in this source release" in str(out)
        assert not (settings.data_root / "remote_receiver" / "packages").exists()
