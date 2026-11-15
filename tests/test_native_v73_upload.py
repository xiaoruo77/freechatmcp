"""v73 upload selection contracts; no browser is launched or contacted."""
from __future__ import annotations

import base64
from copy import deepcopy
import json
from pathlib import Path
import shutil
import subprocess

import pytest

from web_tools.browser_upload import MAX_BYTES, UploadError, read_allowed_media
from web_tools.unified_browser import UnifiedBrowserService
from web_tools import xiaoruo_browser_adapter as adapter_module
from web_tools.xiaoruo_browser_adapter import NativeXiaoruoBrowserAdapter, _cef_helper_script


BROWSER = "native_v73_fixture"
GENERATION = "generation-v73-fixture"
URL = "http://127.0.0.1:48272/upload-test/fixture.html"


class Instance:
    pid = 42
    instance_id = "v73fixture"
    permissions = ("browser.script",)

    def public_descriptor(self):
        return {"browser_id": BROWSER, "connection_generation": GENERATION, "native_pid": self.pid}


class Discovery:
    last_diagnostics = [{"code": "native_discovery_process_not_running", "pid": 999}]

    def discover(self):
        return [Instance()]


def input_metadata(selector="#cover"):
    return {"selector": selector, "tag": "input", "type": "file", "disabled": False,
            "accept": "image/jpeg,image/png", "multiple": False, "scope": "main", "name": "cover",
            "selected_files": [], "visible": False}


class Client:
    def __init__(self):
        self.calls = []
        self.state = "succeeded"
        self.caps = {"tabs": True, "readPage": True, "evaluate": True,
                     "fileUpload": "cef-only", "dynamicElementWait": True,
                     "cef": {"readPage": True, "fileUpload": True}}
        self.tab = {"id": 7, "url": URL, "engine": "CEF / Chromium",
                    "loading": False, "documentReady": True, "active": True}
        self.dom = {"helper": "xiaoruo1-cef-dom-observation/1", "url": URL,
                    "text": "current dynamic DOM", "observation_complete": True,
                    "file_inputs": [input_metadata(), input_metadata("#video")],
                    "elements": [{"selector": "#title", "value": "current title"}],
                    "images": [{"selector": "#preview", "src": "https://example.test/cover.jpg",
                                "natural_width": 1280, "natural_height": 720, "visible": True}],
                    "selector_match_count": 1, "selector_is_file": True}

    async def capabilities(self, timeout=30):
        return deepcopy(self.caps)

    async def tabs(self, timeout=30):
        return {"tabs": [deepcopy(self.tab)]}

    async def execute(self, action, arguments, timeout=30, *, _allow_upload_request=False):
        self.calls.append((action, deepcopy(arguments), _allow_upload_request))
        result = {"text": "native text without dynamic controls"}
        state = self.state if action == "upload_file" else "succeeded"
        if action == "evaluate":
            assert "xiaoruo1-cef-dom-observation/1" in arguments["script"]
            result = deepcopy(self.dom)
        task = {"taskId": "xr-42-" + str(len(self.calls)), "state": state, "action": action,
                "result": json.dumps(result)}
        if state in {"failed", "timed_out"}:
            task["errorCode"] = "operation_failed" if state == "failed" else "timeout"
        return {"ok": state == "succeeded", "task_id": task["taskId"], "native_task": task}

    async def call_tool(self, name, arguments, timeout=30):
        return {"structured": {"task": {"taskId": arguments["task_id"], "state": self.state}}}


@pytest.fixture
def setup(tmp_path, monkeypatch):
    media_root = tmp_path / "media"
    folder = media_root / "2026_10" / "28"
    folder.mkdir(parents=True)
    file = folder / "封面.jpg"
    file.write_bytes(b"\xff\xd8\xff" + b"fixture-cover")
    reader = lambda value: read_allowed_media(value, media_root)
    monkeypatch.setattr(adapter_module, "read_allowed_media", reader)
    client = Client()
    adapter = NativeXiaoruoBrowserAdapter(tmp_path, Discovery(), lambda instance: client)
    desktop_calls = []

    async def desktop():
        desktop_calls.append(True)

    async def discover():
        return await adapter.native_instances()

    async def dispatch(browser, generation, method, payload, timeout):
        return await adapter.native_dispatch(browser, generation, method, payload, timeout)

    service = UnifiedBrowserService(tmp_path / "receipts.db", discover, dispatch, desktop)
    return service, adapter, client, file, desktop_calls


async def read(service):
    return await service.control(BROWSER, "read", "7", {"max_chars": 40000})


async def upload(service, file, operation="upload-cover-0001", selector="#cover", **extra):
    arguments = {"expected_generation": GENERATION, "selector": selector, "file_path": str(file), **extra}
    return await service.control(BROWSER, "upload_file", "7", arguments, operation, True, 5000)


async def test_real_v73_capability_and_diagnostics_are_advertised(setup):
    _, adapter, client, _, _ = setup
    inventory = await adapter.native_instances()
    assert "upload_file" in inventory["browsers"][0]["supported_actions"]
    assert inventory["browsers"][0]["version"] == "v73"
    assert inventory["native_discovery_diagnostics"] == Discovery.last_diagnostics
    assert not adapter._supports_engine(client.caps, "lightpanda", "upload_file")
    assert not adapter._supports_engine({"fileUpload": "cef-only", "cef": {"fileUpload": False}}, "chromium", "upload_file")
    assert not adapter._supports_engine({}, "chromium", "upload_file")


async def test_read_exposes_current_dynamic_controls_and_preview_without_public_js(setup):
    service, _, client, _, _ = setup
    first = await read(service)
    assert first["result"]["text"] == "current dynamic DOM"
    assert first["result"]["images"][0]["natural_width"] == 1280
    assert first["result"]["file_inputs"][0]["selector"] == "#cover"
    client.dom["file_inputs"] = [input_metadata("#new-dynamic-cover")]
    second = await read(service)
    assert second["result"]["file_inputs"][0]["selector"] == "#new-dynamic-cover"
    denied = await service.control(BROWSER, "read", "7", {"script": "document.cookie"})
    assert denied["error"]["code"] == "browser_arguments_invalid"
    assert [action for action, _, _ in client.calls] == ["read_page", "evaluate", "read_page", "evaluate"]


async def test_upload_reuses_media_policy_native_protocol_and_distinct_stages(setup):
    service, _, client, file, desktop = setup
    await read(service)
    value = await upload(service, file, wait_timeout_ms=1200)
    assert value["ok"] and value["evidence"]["verified"]
    result = value["result"]
    assert result["status"] == "selected" and result["native_terminal"]
    assert result["stages"] == {"file_selected": True, "upload_started": None,
                                "server_received": None, "platform_processed": None}
    assert result["verification"] == "native_file_control_names_and_count"
    assert result["selected_file"]["name"] == file.name
    calls = [(args, allowed) for action, args, allowed in client.calls if action == "upload_file"]
    assert len(calls) == 1 and calls[0][1] is True
    args = calls[0][0]
    assert set(args) == {"tab_id", "selector", "file_name", "file_data_base64", "wait_timeout_ms"}
    assert args["selector"] == "#cover" and args["tab_id"] == 7 and args["wait_timeout_ms"] == 1200
    assert base64.b64decode(args["file_data_base64"]) == file.read_bytes()
    stored = service.receipts.get("upload-cover-0001")["result"]
    assert args["file_data_base64"] not in stored and "file_data_base64" not in stored
    assert "data_base64" not in json.dumps(result) and len(desktop) == 1


@pytest.mark.parametrize("confirm,operation,arguments,code", [
    (False, "upload-cover-0001", {"expected_generation": GENERATION}, "browser_confirmation_required"),
    (True, None, {"expected_generation": GENERATION}, "browser_operation_id_required"),
    (True, "upload-cover-0001", {}, "browser_generation_required"),
    (True, "upload-cover-0001", {"expected_generation": "stale"}, "browser_generation_changed"),
    (True, "upload-cover-0001", {"expected_generation": GENERATION, "file_data_base64": "eA=="}, "browser_arguments_invalid"),
    (True, "upload-cover-0001", {"expected_generation": GENERATION, "wait_timeout_ms": True}, "browser_arguments_invalid"),
])
async def test_upload_authority_cannot_be_supplied_by_page_or_protocol_fields(setup, confirm, operation, arguments, code):
    service, _, client, file, desktop = setup
    value = await service.control(BROWSER, "upload_file", "7", {"selector": "#cover", "file_path": str(file), **arguments}, operation, confirm)
    assert value["error"]["code"] == code and value["evidence"]["executed"] is False
    assert not client.calls and not desktop


async def test_upload_requires_a_fresh_successful_read_of_this_input(setup):
    service, _, client, file, _ = setup
    value = await upload(service, file)
    assert value["error"]["code"] == "upload_selector_unobserved"
    assert not client.calls
    await read(service)
    value = await upload(service, file, operation="upload-cover-0002", selector="input[type=file]")
    assert value["error"]["code"] == "upload_selector_unobserved"
    assert not any(action == "upload_file" for action, _, _ in client.calls)


@pytest.mark.parametrize("change,code", [
    ("ambiguous", "upload_target_ambiguous"), ("disabled", "upload_target_changed"),
    ("different_accept", "upload_target_changed"), ("already_selected", "upload_target_already_selected"),
    ("different_document", "upload_observation_unavailable"), ("incomplete", "upload_observation_unavailable"),
])
async def test_current_dom_is_revalidated_before_any_bytes_are_sent(setup, change, code):
    service, _, client, file, _ = setup
    await read(service)
    if change == "ambiguous":
        client.dom["selector_match_count"] = 2
    elif change == "disabled":
        client.dom["file_inputs"][0]["disabled"] = True
    elif change == "different_accept":
        client.dom["file_inputs"][0]["accept"] = "video/*"
    elif change == "already_selected":
        client.dom["file_inputs"][0]["selected_files"] = [{"name": "prior.jpg", "size": 99}]
    elif change == "different_document":
        client.dom["url"] = URL + "#changed"
    else:
        client.dom["observation_complete"] = False
    value = await upload(service, file)
    assert value["error"]["code"] == code and value["status"] == "failed"
    assert not any(action == "upload_file" for action, _, _ in client.calls)


@pytest.mark.parametrize("media,code", [("outside", "upload_path_denied"), ("wrong_header", "upload_content_mismatch"),
                                       ("oversized", "upload_size_denied"), ("svg", "upload_extension_denied")])
async def test_native_upload_keeps_the_existing_media_path_format_and_size_limits(setup, media, code, tmp_path):
    service, _, client, file, _ = setup
    await read(service)
    if media == "outside":
        file = tmp_path / "outside.jpg"
        file.write_bytes(b"\xff\xd8\xffoutside")
    elif media == "wrong_header":
        file.write_bytes(b"not-a-jpeg")
    elif media == "oversized":
        with file.open("wb") as stream:
            stream.truncate(MAX_BYTES + 1)
    else:
        file = file.with_suffix(".svg")
        file.write_text("<svg/>")
    value = await upload(service, file)
    assert value["error"]["code"] == code
    assert not any(action == "upload_file" for action, _, _ in client.calls)


@pytest.mark.parametrize("state,terminal", [("accepted", False), ("running", False), ("failed", True),
                                          ("timed_out", True), ("cancelled", True)])
async def test_non_succeeded_upload_retains_task_fence_and_never_replays(setup, state, terminal):
    service, _, client, file, _ = setup
    await read(service)
    client.state = state
    value = await upload(service, file)
    assert not value["ok"] and value["status"] == "outcome_unknown"
    assert value["result"]["native_state"] == state and value["result"]["native_terminal"] is terminal
    assert value["native_task_id"].startswith("xr-42-") and not value["retryable"]
    duplicate = await upload(service, file)
    assert duplicate["duplicate_call"] and duplicate["redispatched"] is False
    another = await upload(service, file, operation="upload-cover-0002")
    assert another["error"]["code"] == "browser_target_fenced"
    assert len([action for action, _, _ in client.calls if action == "upload_file"]) == 1


async def test_selected_upload_is_durable_and_new_key_cannot_replace_same_input(setup):
    service, _, client, file, desktop = setup
    await read(service)
    await upload(service, file)

    async def takeover():
        raise UploadError("upload_desktop_control_unavailable", "UserTakeover owns the desktop")

    service.desktop_check = takeover
    duplicate = await upload(service, file)
    assert duplicate["duplicate_call"] and duplicate["redispatched"] is False
    second = await upload(service, file, operation="upload-cover-0002")
    assert second["error"]["code"] == "upload_duplicate_prevented"
    restarted = UnifiedBrowserService(service.receipts.path, service.discover, service.dispatch, takeover)
    assert (await upload(restarted, file))["duplicate_call"]
    assert len(desktop) == 1 and len([a for a, _, _ in client.calls if a == "upload_file"]) == 1


async def test_other_file_input_in_same_tab_is_independent_after_verified_selection(setup):
    service, _, client, file, _ = setup
    await read(service)
    assert (await upload(service, file))["ok"]
    assert (await upload(service, file, operation="upload-video-0002", selector="#video"))["ok"]
    assert [args["selector"] for action, args, _ in client.calls if action == "upload_file"] == ["#cover", "#video"]


@pytest.mark.parametrize("gate", ["missing", "takeover"])
async def test_upload_fails_closed_when_user_takeover_preflight_is_unavailable(setup, gate):
    service, _, client, file, _ = setup
    await read(service)

    async def takeover():
        raise UploadError("upload_desktop_control_unavailable", "UserTakeover owns the desktop")

    service.desktop_check = None if gate == "missing" else takeover
    value = await upload(service, file)
    assert value["error"]["code"] == "upload_desktop_control_unavailable"
    assert value["evidence"]["executed"] is False
    assert not any(action == "upload_file" for action, _, _ in client.calls)


async def test_unified_upload_cannot_change_existing_edge_route(tmp_path):
    calls = []

    async def discover():
        return {"browsers": [{"browser_id": BROWSER, "adapter": "extension", "connected": True,
                              "connection_generation": GENERATION, "supported_actions": ["upload_file"]}]}

    async def dispatch(*args):
        calls.append(args)

    service = UnifiedBrowserService(tmp_path / "edge.db", discover, dispatch)
    value = await upload(service, tmp_path / "cover.jpg")
    assert value["error"]["code"] == "unsupported" and not calls


def test_fixed_dom_helper_has_valid_javascript_and_no_page_mutation_or_storage_access():
    script = _cef_helper_script("read_dom", {"selector": '#cover\");globalThis.caller=true;//', "max_chars": 40000})
    for denied in ("setAttribute(", "localStorage", "sessionStorage", "document.cookie", ".files =", "DataTransfer(", "dispatchEvent("):
        assert denied not in script
    assert "background_image" in script and "data-image-omitted" in script
    assert "href:" in script and "target:" in script and "download:" in script
    prefix = "(()=>{const args="
    arguments, _ = json.JSONDecoder().raw_decode(script[len(prefix):])
    assert arguments == {"selector": '#cover\");globalThis.caller=true;//', "max_chars": 40000}
    node = shutil.which("node")
    if node:
        # Parse the exact emitted helper; caller selector remains string data.
        compiled = subprocess.run([node, "-e", "new Function(require('fs').readFileSync(0,'utf8'));"],
                                  input=script, text=True, capture_output=True, timeout=10)
        assert compiled.returncode == 0, compiled.stderr
