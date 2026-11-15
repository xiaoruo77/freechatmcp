"""Safe local compile/schema tests. These never enter remote runtime or GUI code."""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess

import pytest


SCRIPT = Path(__file__).parents[1] / "06_web_tools/web_tools/uu_remote_input.ps1"
POWERSHELL = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
NONCE = "0123456789abcdef0123456789abcdef"
REQUEST_PREFIX = "XIAORUO1_UU_REQUEST_V2|"


def _request_wire(request):
    raw = json.dumps(request, ensure_ascii=True).encode("utf-8")
    encoded = base64.b64encode(raw).decode("ascii")
    return REQUEST_PREFIX + encoded + "|" + hashlib.sha256(raw).hexdigest() + "|END"


def _run_safe(command):
    if os.name != "nt" or not POWERSHELL.is_file():
        pytest.skip("Windows PowerShell compile test")
    # The invoked command is a known schema/compile-only expression. No actual
    # endpoint runtime or input request loop is started in these tests.
    encoded = base64.b64encode(command.encode("utf-16-le")).decode("ascii")
    result = subprocess.run(
        [str(POWERSHELL), "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
        env=dict(os.environ, PSModulePath=str(POWERSHELL.parent / "Modules")),
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def test_helper_self_test_compiles_full_win32_union_without_input_or_gui():
    result = _run_safe("& '" + str(SCRIPT).replace("'", "''") + "' -ProtocolSelfTest")
    data = json.loads(result)
    assert data["ok"] and data["no_input_sent"] and data["no_gui_created"]
    assert data["input_size"] == 40
    assert data["union_offset"] == 8
    assert data["unicode_events"] == 8
    assert data["chord_events"] == 4
    assert data["newline_events"] == 6 and data["crlf_events"] == 6
    assert data["newline_virtual_key"] == 13 and data["newline_flags"] == [0, 2]
    assert data["rejected_schema_samples"] == 5
    assert data["protocol"] == "xiaoruo1.uu.remote-input.v2"
    assert "XIAORUO1_UU_INPUT_V2|" not in result


@pytest.mark.parametrize("text", ["a\nb", "a\r\nb", "a\rb"])
def test_native_text_newline_is_one_return_pair_without_sendinput(text):
    encoded = base64.b64encode(text.encode("utf-8")).decode()
    command = (
        ". '" + str(SCRIPT).replace("'", "''") + "' -ProtocolSelfTest > $null\n"
        "$text=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('" + encoded + "'));"
        "$events=[Xiaoruo1.UURemoteInputV1.Native]::BuildUnicode($text);"
        "$rows=@($events|ForEach-Object{[ordered]@{type=[int]$_.type;vk=[int]$_.data.keyboard.wVk;scan=[int]$_.data.keyboard.wScan;flags=[int]$_.data.keyboard.dwFlags}});"
        "ConvertTo-Json -InputObject $rows -Compress"
    )
    assert json.loads(_run_safe(command)) == [
        {"type": 1, "vk": 0, "scan": ord("a"), "flags": 4},
        {"type": 1, "vk": 0, "scan": ord("a"), "flags": 6},
        {"type": 1, "vk": 13, "scan": 0, "flags": 0},
        {"type": 1, "vk": 13, "scan": 0, "flags": 2},
        {"type": 1, "vk": 0, "scan": ord("b"), "flags": 4},
        {"type": 1, "vk": 0, "scan": ord("b"), "flags": 6},
    ]


def test_native_text_preserves_unicode_surrogates_tabs_and_blank_lines():
    text = "中😀\t\r\n\r\nX"
    encoded = base64.b64encode(text.encode("utf-8")).decode()
    command = (
        ". '" + str(SCRIPT).replace("'", "''") + "' -ProtocolSelfTest > $null\n"
        "$text=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('" + encoded + "'));"
        "$events=[Xiaoruo1.UURemoteInputV1.Native]::BuildUnicode($text);"
        "$rows=@($events|ForEach-Object{[ordered]@{vk=[int]$_.data.keyboard.wVk;scan=[int]$_.data.keyboard.wScan;flags=[int]$_.data.keyboard.dwFlags}});"
        "ConvertTo-Json -InputObject $rows -Compress"
    )
    actual = json.loads(_run_safe(command))
    expected = []
    for unit in [0x4E2D, 0xD83D, 0xDE00, 9, "enter", "enter", ord("X")]:
        if unit == "enter":
            expected.extend([{"vk": 13, "scan": 0, "flags": 0}, {"vk": 13, "scan": 0, "flags": 2}])
        else:
            expected.extend([{"vk": 0, "scan": unit, "flags": 4}, {"vk": 0, "scan": unit, "flags": 6}])
    assert actual == expected


def test_reserved_transport_console_is_protected_without_blocking_fixture_or_user_apps():
    cases = [
        ("xru_input_0123456789abcdef", "ConsoleWindowClass", "conhost", True),
        ("Remote xru_input_0123456789abcdef", "CASCADIA_HOSTING_WINDOW_CLASS", "WindowsTerminal", True),
        ("XRU_INPUT_0123456789abcdef", "consolewindowclass", "powershell", True),
        ("xru_input_0123456789abcdef", "OtherWindowClass", "WindowsTerminal", True),
        ("xru_input_0123456789abcdef", "OtherWindowClass", "WindowsTerminal.exe", True),
        ("Xiaoruo1 UU GUI Input Test 01234567", "WindowsForms10.Window.8.app", "powershell", False),
        ("xru_input_fixture", "WindowsForms10.Window.8.app", "powershell", False),
        ("User terminal", "ConsoleWindowClass", "conhost", False),
        ("xru_input_new.py - Visual Studio Code", "Chrome_WidgetWin_1", "Code", False),
        (None, "ConsoleWindowClass", "conhost", False),
    ]
    encoded = base64.b64encode(json.dumps([list(case[:3]) for case in cases]).encode()).decode()
    command = (
        ". '" + str(SCRIPT).replace("'", "''") + "' -ProtocolSelfTest > $null\n"
        "$rows=ConvertFrom-Json ([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('" + encoded + "')));"
        "$answers=@();foreach($row in $rows){$answers+=[Xiaoruo1.UURemoteInputV1.Native]::IsProtectedTransportWindow($row[0],$row[1],$row[2])};"
        "ConvertTo-Json -InputObject @($answers) -Compress"
    )
    assert json.loads(_run_safe(command)) == [case[3] for case in cases]


def test_remote_queue_plan_skips_self_zero_foreground_and_duplicate_threads():
    cases = [([11, 22, 33], [22, 33]), ([11, 11, 33], [33]),
             ([11, 22, 22], [22]), ([11, 0, 11], []), ([11, 0, 33], [33])]
    encoded = base64.b64encode(json.dumps([case[0] for case in cases]).encode()).decode()
    command = (
        ". '" + str(SCRIPT).replace("'", "''") + "' -ProtocolSelfTest > $null\n"
        "$rows=ConvertFrom-Json ([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('" + encoded + "')));"
        "$answers=@();foreach($row in $rows){$answers+=,[ordered]@{plan=@([Xiaoruo1.UURemoteInputV1.Native]::BuildAttachThreads([uint32]$row[0],[uint32]$row[1],[uint32]$row[2]))}};"
        "$invalid=$false;try{$null=[Xiaoruo1.UURemoteInputV1.Native]::BuildAttachThreads(0,22,33)}catch{$invalid=$true};"
        "[ordered]@{plans=$answers;invalid_rejected=$invalid}|ConvertTo-Json -Depth 6 -Compress"
    )
    result = json.loads(_run_safe(command))
    assert result["plans"] == [{"plan": case[1]} for case in cases]
    assert result["invalid_rejected"] is True


def test_remote_activation_interop_signatures_compile_without_invoking_gui_apis():
    command = (
        ". '" + str(SCRIPT).replace("'", "''") + "' -ProtocolSelfTest > $null\n"
        "$flags=[Reflection.BindingFlags]'NonPublic,Static';$rows=@();"
        "foreach($name in @('AttachThreadInput','PeekMessage','BringWindowToTop','GetWindowText','GetClassName')){"
        "$method=[Xiaoruo1.UURemoteInputV1.Native].GetMethod($name,$flags);"
        "$attr=$method.GetCustomAttributes([Runtime.InteropServices.DllImportAttribute],$false)[0];"
        "$rows+=[ordered]@{name=$name;entry=$attr.EntryPoint;exact_spelling=$attr.ExactSpelling;returns=$method.ReturnType.FullName;parameters=@($method.GetParameters()|ForEach-Object{$_.ParameterType.FullName})}};"
        "ConvertTo-Json -InputObject $rows -Depth 5 -Compress"
    )
    rows = {row["name"]: row for row in json.loads(_run_safe(command))}
    assert rows["AttachThreadInput"] == {"name": "AttachThreadInput", "entry": "AttachThreadInput",
        "exact_spelling": False, "returns": "System.Boolean", "parameters": ["System.UInt32", "System.UInt32", "System.Boolean"]}
    assert rows["PeekMessage"] == {"name": "PeekMessage", "entry": "PeekMessageW",
        "exact_spelling": True, "returns": "System.Boolean", "parameters": ["System.IntPtr", "System.IntPtr", "System.UInt32", "System.UInt32", "System.UInt32"]}
    assert rows["BringWindowToTop"]["parameters"] == ["System.IntPtr"]
    for name in ["GetWindowText", "GetClassName"]:
        assert rows[name]["entry"] == name + "W"
        assert rows[name]["exact_spelling"] is True
        assert rows[name]["parameters"] == ["System.IntPtr", "System.Text.StringBuilder", "System.Int32"]


def test_fixed_protocol_rejects_unsafe_or_ambiguous_requests_before_runtime():
    expected = {"hwnd": 10, "pid": 20, "creation_time_utc_ticks": "123"}
    cases = [
        (True, {"action": "status", "arguments": {}}),
        (True, {"action": "type", "arguments": {"text": "ABC中😀", "expect": expected}}),
        (True, {"action": "key", "arguments": {"keys": "CTRL+SHIFT+A", "expect": expected}}),
        (True, {"action": "create_test_window", "arguments": {}}),
        (True, {"action": "stop", "arguments": {}}),
        (False, {"action": "type", "arguments": {"text": "ABC"}}),
        (False, {"action": "type", "arguments": {"text": "A\0B", "expect": expected}}),
        (False, {"action": "type", "arguments": {"text": "a" * 2001, "expect": expected}}),
        (False, {"action": "type", "arguments": {"text": "ABC", "expect": dict(expected, hwnd=0)}}),
        (False, {"action": "type", "arguments": {"text": "ABC", "expect": dict(expected, creation_time_utc_ticks=123)}}),
        (False, {"action": "type", "arguments": {"text": "ABC", "expect": expected, "activate": "true"}}),
        (False, {"action": "key", "arguments": {"keys": ["CTRL", "CONTROL"], "expect": expected}}),
        (False, {"action": "key", "arguments": {"keys": ["CTRL", "ALT", "DELETE"], "expect": expected}}),
        (False, {"action": "key", "arguments": {"keys": ["CTRL", 123], "expect": expected}}),
        (False, {"action": "execute", "arguments": {"command": "forbidden"}}),
        (False, {"action": "status", "arguments": {"command": "forbidden"}}),
        (False, {"action": "status", "arguments": {}, "nonce": "wrong"}),
        (False, {"action": "status", "arguments": {}, "reqid": "bad\nreqid"}),
    ]
    lines = []
    for index, (_, request) in enumerate(cases):
        value = {"nonce": NONCE, "reqid": f"schema-{index}", **request}
        lines.append(_request_wire(value))
    literal_lines = ",".join("'" + line + "'" for line in lines)
    command = (
        ". '" + str(SCRIPT).replace("'", "''") + "' -ProtocolSelfTest > $null\n"
        "$results=@(); foreach($line in @(" + literal_lines + ")) {\n"
        " try {$null=Get-Request $line '" + NONCE + "'; $results += $true}\n"
        " catch {$results += $false}\n"
        "}; ConvertTo-Json -InputObject @($results) -Compress"
    )
    accepted = json.loads(_run_safe(command))
    assert accepted == [valid for valid, _ in cases]


def test_graphical_agent_schemas_are_validated_without_screenshot_network_or_input():
    bounds = {"left": -1920, "top": -100, "width": 3840, "height": 1180}
    click = {"x": -10, "y": 20, "button": "left", "count": 1, "bounds": bounds}
    scroll = {"x": 0, "y": 0, "vertical": -120, "horizontal": 120, "bounds": bounds}
    drag = {"x": -100, "y": -90, "to_x": 1919, "to_y": 1079, "duration_ms": 50, "bounds": bounds}
    capture = {"capture_id": "capture_test-123", "upload_url": "https://capture.example/remote-gui/captures/capture_test-123", "token": "opaque_ticket_0123456789"}
    cases = [
        (True, "configure_capture", {"origin": "https://capture.example"}),
        (True, "configure_capture", {"origin": "https://capture.example:8443"}),
        (True, "capture", capture), (True, "click", click),
        (True, "scroll", scroll), (True, "drag", drag),
        (False, "configure_capture", {"origin": "http://capture.example"}),
        (False, "configure_capture", {"origin": "https://capture.example/"}),
        (False, "configure_capture", {"origin": "https://user@capture.example"}),
        (False, "configure_capture", {"origin": "https://capture.example/path"}),
        (False, "capture", dict(capture, upload_url=capture["upload_url"] + "?token=secret")),
        (False, "capture", dict(capture, upload_url="https://user@capture.example/remote-gui/captures/capture_test-123")),
        (False, "capture", dict(capture, upload_url="https://capture.example/remote-gui/captures/other")),
        (False, "capture", dict(capture, capture_id="../escape")),
        (False, "capture", dict(capture, token="bad\r\nAuthorization: test")),
        (False, "click", dict(click, x=True)),
        (False, "click", dict(click, x="20")),
        (False, "click", dict(click, x=0.5)),
        (False, "click", dict(click, y=-101)),
        (False, "click", dict(click, x=1920)),
        (False, "click", dict(click, button="LEFT")),
        (False, "click", dict(click, count=3)),
        (False, "click", dict(click, expect={})),
        (False, "click", dict(click, bounds=dict(bounds, width=8193))),
        (False, "click", dict(click, bounds=dict(bounds, width=8192, height=8192))),
        (False, "click", dict(click, bounds=dict(bounds, left=2147483647, width=2))),
        (False, "scroll", dict(scroll, vertical=0, horizontal=0)),
        (False, "scroll", dict(scroll, vertical=12001)),
        (False, "drag", dict(drag, duration_ms=49)),
        (False, "drag", dict(drag, duration_ms=5001)),
        (False, "drag", dict(drag, to_x=1920)),
    ]
    lines = [_request_wire({"nonce": NONCE, "reqid": f"gui-{i}", "action": action, "arguments": args})
             for i, (_, action, args) in enumerate(cases)]
    command = (
        ". '" + str(SCRIPT).replace("'", "''") + "' -ProtocolSelfTest > $null\n"
        "$answers=@();foreach($line in @(" + ",".join("'" + line + "'" for line in lines) + ")){"
        "try{$null=Get-Request $line '" + NONCE + "';$answers+=$true}catch{$answers+=$false}};"
        "ConvertTo-Json -InputObject $answers -Compress"
    )
    assert json.loads(_run_safe(command)) == [valid for valid, _, _ in cases]


def test_mouse_builders_use_physical_virtual_screen_and_balanced_button_events_only():
    command = (
        ". '" + str(SCRIPT).replace("'", "''") + "' -ProtocolSelfTest > $null\n"
        "$n=[Xiaoruo1.UURemoteInputV1.Native];"
        "$click=$n::BuildClick(-1920,-100,'right',2,-1920,-100,3840,1180);"
        "$scroll=$n::BuildScroll(1919,1079,-120,240,-1920,-100,3840,1180);"
        "$drag=$n::BuildDrag(-1920,-100,1919,1079,50,-1920,-100,3840,1180);"
        "$rows=[ordered]@{};foreach($name in @('click','scroll','drag')){"
        "$events=Get-Variable $name -ValueOnly;"
        "$rows[$name]=@($events|ForEach-Object{[ordered]@{type=[int]$_.type;x=$_.data.mouse.dx;y=$_.data.mouse.dy;"
        "flags=[uint32]$_.data.mouse.dwFlags;data=[uint32]$_.data.mouse.mouseData}})};"
        "$rows|ConvertTo-Json -Depth 6 -Compress"
    )
    data = json.loads(_run_safe(command))
    common = 0x8000 | 0x4000
    assert [event["flags"] for event in data["click"]] == [common | f for f in [1, 8, 16, 8, 16]]
    assert all(event["x"] == event["y"] == 0 for event in data["click"])
    assert [event["flags"] for event in data["scroll"]] == [common | f for f in [1, 0x800, 0x1000]]
    assert [event["data"] for event in data["scroll"]] == [0, 2**32 - 120, 240]
    assert all(event["x"] == event["y"] == 65535 for event in data["scroll"])
    drag = data["drag"]
    assert len(drag) == 7
    assert drag[0]["x"] == drag[0]["y"] == 0
    assert drag[-1]["x"] == drag[-1]["y"] == 65535
    assert [event["flags"] & 0x7F for event in drag] == [1, 2, 1, 1, 1, 1, 4]
    assert all(event["type"] == 0 and event["flags"] & common == common
               for events in data.values() for event in events)


def test_capture_ingress_is_session_fixed_and_ticket_never_appears_in_validation_errors():
    command = (
        ". '" + str(SCRIPT).replace("'", "''") + "' -ProtocolSelfTest > $null\n"
        "$n=[Xiaoruo1.UURemoteInputV1.Native];$token='private_ticket_0123456789';"
        "$missing=$false;try{$null=$n::ValidateConfiguredCapture('id1','https://capture.example/remote-gui/captures/id1',$token)}catch{$missing=$true};"
        "$null=$n::ConfigureCapture('https://capture.example');$null=$n::ConfigureCapture('https://capture.example');"
        "$url=$n::ValidateConfiguredCapture('id1','https://capture.example/remote-gui/captures/id1',$token);"
        "$changed=$false;try{$null=$n::ConfigureCapture('https://other.example')}catch{$changed=$true};"
        "$cross=$false;$message='';try{$null=$n::ValidateConfiguredCapture('id1','https://other.example/remote-gui/captures/id1',$token)}"
        "catch{$cross=$true;$message=$_.Exception.InnerException.Message};"
        "[ordered]@{missing=$missing;changed=$changed;cross=$cross;url=$url;message=$message}|ConvertTo-Json -Compress"
    )
    output = _run_safe(command)
    result = json.loads(output)
    assert result["missing"] and result["changed"] and result["cross"]
    assert result["url"] == "https://capture.example/remote-gui/captures/id1"
    assert "capture_origin_mismatch:" in result["message"]
    assert "private_ticket" not in output


def test_capture_png_header_bounds_and_foreground_binding_are_pure():
    command = (
        ". '" + str(SCRIPT).replace("'", "''") + "' -ProtocolSelfTest > $null\n"
        "$n=[Xiaoruo1.UURemoteInputV1.Native];"
        "$png=New-Object byte[] 33;$header=[byte[]]@(137,80,78,71,13,10,26,10,0,0,0,13,73,72,68,82,0,0,0,2,0,0,0,3);"
        "[Array]::Copy($header,$png,$header.Length);$n::ValidatePng($png,2,3);"
        "$rejected=0;foreach($case in @(@(2,4),@(8192,8192))){try{$n::ValidatePng($png,$case[0],$case[1])}catch{$rejected++}};"
        "$png[0]=0;try{$n::ValidatePng($png,2,3)}catch{$rejected++};"
        "$oversize=New-Object byte[] (16*1024*1024+1);[Array]::Copy($header,$oversize,$header.Length);"
        "try{$n::ValidatePng($oversize,2,3)}catch{$rejected++};"
        "$before=New-Object 'System.Collections.Generic.Dictionary[string,object]';"
        "$before.Add('available',$true);$before.Add('hwnd',10L);$before.Add('pid',20);$before.Add('creation_time_utc_ticks','123');"
        "$after=New-Object 'System.Collections.Generic.Dictionary[string,object]' (,$before);"
        "$stable=$n::StableCaptureForeground($before,$after);$after['creation_time_utc_ticks']='124';"
        "$changed=$n::StableCaptureForeground($before,$after);"
        "[ordered]@{rejected=$rejected;stable=$stable;changed=$changed}|ConvertTo-Json -Depth 5 -Compress"
    )
    data = json.loads(_run_safe(command))
    assert data["rejected"] == 4
    assert data["stable"]["available"] is True
    assert data["changed"] == {"available": False, "target_changed": True}


def test_capture_and_mouse_native_entrypoints_compile_but_do_not_execute_in_self_test():
    # Static inspection protects the actual dispatch boundary: this test must
    # never call these methods or capture/upload this user's local desktop.
    source = SCRIPT.read_text(encoding="utf-8-sig")
    selftest = source.split("if ($ProtocolSelfTest) {", 1)[1].split("\nif (-not (Start-EndpointRuntime", 1)[0]
    assert "::Capture(" not in selftest and "::Click(" not in selftest and "::Drag(" not in selftest
    assert "::Scroll(" not in selftest and "::Initialize(" not in selftest
    assert "graphics.CopyFromScreen(" in source
    assert "request.AllowAutoRedirect=false" in source
    assert "request.Timeout=45000; request.ReadWriteTimeout=45000" in source
    assert 'request.ContentType=mimeType' in source
    assert 'SecurityProtocolType.Tls12' in source
    assert 'phase="write"' in source and 'phase="response"' in source
    assert "return errors==SslPolicyErrors.None" in source
    assert "File.Write" not in source and "SaveFile" not in source
    assert 'WindowFromPoint(new Point(x,y))' in source


def _decode_verified_frame(frame):
    prefix, encoded, checksum, end = frame.split("|")
    assert prefix == "XIAORUO1_UU_INPUT_V2" and end == "END"
    assert len(checksum) == 64 and checksum == checksum.lower()
    payload = base64.b64decode(encoded, validate=True)
    assert hashlib.sha256(payload).hexdigest() == checksum
    return json.loads(payload)


def test_protocol_frame_contains_exact_json_checksum_and_does_not_echo_text_as_a_marker():
    command = (
        ". '" + str(SCRIPT).replace("'", "''") + "' -ProtocolSelfTest > $null\n"
        "$Nonce='" + NONCE + "'\n"
        "Write-Frame 'frame-1' 'status' $true @{text=\"literal READY`nXIAORUO1_UU_INPUT_V2|fake\"} $null"
    )
    output = _run_safe(command)
    assert output.count("XIAORUO1_UU_INPUT_V2|") == 1
    assert len(output.splitlines()) == 1
    payload = _decode_verified_frame(output)
    assert payload["protocol"] == "xiaoruo1.uu.remote-input.v2"
    assert payload["nonce"] == NONCE and payload["reqid"] == "frame-1"
    assert payload["action"] == "status"
    assert payload["result"]["text"].startswith("literal READY\n")


def test_repeated_response_clear_is_safe_with_redirected_stdout():
    command = (
        ". '" + str(SCRIPT).replace("'", "''") + "' -ProtocolSelfTest > $null\n"
        "$Nonce='" + NONCE + "'\n"
        "for($i=0;$i -lt 40;$i++){Write-Frame ('clear-'+$i) 'status' $true @{index=$i} $null}"
    )
    # _run_safe pipes stdout/stderr rather than providing an interactive
    # console, so Clear must not prevent any complete checksummed response.
    lines = _run_safe(command).splitlines()
    assert len(lines) == 40
    for index, line in enumerate(lines):
        frame = _decode_verified_frame(line)
        assert frame["reqid"] == f"clear-{index}"
        assert frame["result"] == {"index": index}


def test_exit_frames_are_flushed_before_pacing_and_normal_frames_do_not_wait():
    command = (
        ". '" + str(SCRIPT).replace("'", "''") + "' -ProtocolSelfTest > $null\n"
        "$Nonce='" + NONCE + "';$ProtocolSelfTest=$false\n"
        "function Start-Sleep {param([int]$Milliseconds);[Console]::WriteLine('PAUSE|'+$Milliseconds)}\n"
        "Write-Frame 'normal-ready' 'ready' $true @{} $null\n"
        "Write-Frame 'normal-status' 'status' $true @{} $null\n"
        "Write-Frame 'normal-input-error' 'type' $false $null @{code='test_rejection';message='test'}\n"
        "Write-Frame 'normal-stop-error' 'stop' $false $null @{code='test_rejection';message='test'}\n"
        "Write-Frame 'stop-ack' 'stop' $true @{stopping=$true;fixture_closed=$true} $null\n"
        "Write-Frame '' 'stopped' $true @{reason='stop';fixture_closed=$true} $null\n"
        "Write-Frame '' 'ready' $false $null @{code='remote_initialization_failed';message='test'}"
    )
    lines = _run_safe(command).splitlines()
    events = []
    for line in lines:
        if line.startswith("PAUSE|"):
            events.append(line)
        else:
            frame = _decode_verified_frame(line)
            events.append((frame["reqid"], frame["action"], frame["ok"]))
    assert events == [
        ("normal-ready", "ready", True), ("normal-status", "status", True),
        ("normal-input-error", "type", False), ("normal-stop-error", "stop", False),
        ("stop-ack", "stop", True), "PAUSE|350",
        ("", "stopped", True), "PAUSE|350",
        ("", "ready", False), "PAUSE|350",
    ]


def test_self_test_protocol_frames_skip_exit_pacing():
    command = (
        ". '" + str(SCRIPT).replace("'", "''") + "' -ProtocolSelfTest > $null\n"
        "$Nonce='" + NONCE + "'\n"
        "function Start-Sleep {throw 'self-test-must-not-wait'}\n"
        "Write-Frame 'stop-ack' 'stop' $true @{} $null\n"
        "Write-Frame '' 'stopped' $true @{reason='stop'} $null\n"
        "Write-Frame '' 'ready' $false $null @{code='test';message='test'}"
    )
    frames = [_decode_verified_frame(line) for line in _run_safe(command).splitlines()]
    assert [(frame["action"], frame["ok"]) for frame in frames] == [
        ("stop", True), ("stopped", True), ("ready", False)
    ]


@pytest.mark.parametrize("part", ["payload", "checksum", "terminator"])
def test_actual_response_frame_rejects_repaint_corruption(part):
    command = (
        ". '" + str(SCRIPT).replace("'", "''") + "' -ProtocolSelfTest > $null\n"
        "$Nonce='" + NONCE + "'\n"
        "Write-Frame 'frame-original' 'type' $false @{} @{code='input_delivery_incomplete';message='test-only'}"
    )
    output = _run_safe(command)
    assert _decode_verified_frame(output)["reqid"] == "frame-original"
    pieces = output.split("|")
    if part == "payload":
        payload = json.loads(base64.b64decode(pieces[1]))
        payload["reqid"] = "frame-repainted"
        pieces[1] = base64.b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode()
    elif part == "checksum":
        pieces[2] = ("0" if pieces[2][0] != "0" else "1") + pieces[2][1:]
    else:
        pieces[3] = "EN"
    with pytest.raises(AssertionError):
        _decode_verified_frame("|".join(pieces))


def test_request_wire_checks_full_line_and_checksum_before_schema():
    value = {"nonce": NONCE, "reqid": "crc-request", "action": "status", "arguments": {}}
    good = _request_wire(value)
    pieces = good.split("|")
    changed = dict(value, action="execute")
    corrupted_payload = base64.b64encode(json.dumps(changed).encode()).decode()
    cases = [
        (good, "accepted"),
        (pieces[1], "invalid_request_frame"),
        (good.removesuffix("|END"), "invalid_request_frame"),
        (good + "trailing", "invalid_request_frame"),
        (" " + good, "invalid_request_frame"),
        (good.replace("REQUEST_V2", "request_v2"), "invalid_request_frame"),
        ("|".join([pieces[0], pieces[1], pieces[2].upper(), pieces[3]]), "invalid_request_frame"),
        ("|".join([pieces[0], corrupted_payload, pieces[2], pieces[3]]), "request_checksum_mismatch"),
        (_request_wire(changed), "unsupported_action"),
    ]
    lines = ",".join("'" + line + "'" for line, _ in cases)
    command = (
        ". '" + str(SCRIPT).replace("'", "''") + "' -ProtocolSelfTest > $null\n"
        "$results=@();foreach($line in @(" + lines + ")){"
        "try{$null=Get-Request $line '" + NONCE + "';$results+='accepted'}"
        "catch{$results+=($_.Exception.Message -split ':',2)[0]}};"
        "ConvertTo-Json -InputObject @($results) -Compress"
    )
    assert json.loads(_run_safe(command)) == [code for _, code in cases]


@pytest.mark.parametrize(
    "proof,nonce,expected_code,proof_called",
    [
        ("$script:proofCalled=$true;throw 'interactive_session_required: PRIVATE_STARTUP_DETAIL'", NONCE, "interactive_session_required", True),
        ("$script:proofCalled=$true;throw 'PRIVATE_STARTUP_DETAIL'", NONCE, "remote_initialization_failed", True),
        ("$script:proofCalled=$true;return @{computer='SAFE_TEST'}", "bad-nonce", "nonce_required", False),
    ],
)
def test_runtime_ready_failure_is_framed_safe_and_cleans_owned_state(proof, nonce, expected_code, proof_called):
    command = (
        ". '" + str(SCRIPT).replace("'", "''") + "' -ProtocolSelfTest > $null\n"
        "$Nonce='" + nonce + "';$script:cleanupCount=0;$script:proofCalled=$false;"
        "$sink=New-Object IO.StringWriter;$saved=[Console]::Out;"
        "try{[Console]::SetOut($sink);"
        "$started=Start-EndpointRuntime -Proof {" + proof + "} -Cleanup {$script:cleanupCount++}}"
        "finally{[Console]::SetOut($saved)};"
        "[ordered]@{started=$started;cleanup_count=$script:cleanupCount;proof_called=$script:proofCalled;frame=$sink.ToString().Trim()}|ConvertTo-Json -Compress"
    )
    result = json.loads(_run_safe(command))
    assert result["started"] is False
    assert result["cleanup_count"] == 1
    assert result["proof_called"] is proof_called
    frame = _decode_verified_frame(result["frame"])
    assert frame["action"] == "ready" and frame["reqid"] == "" and frame["ok"] is False
    assert frame["error"]["code"] == expected_code
    assert "PRIVATE_STARTUP_DETAIL" not in json.dumps(frame)
    assert frame["error"]["message"] == "The remote GUI input endpoint could not initialize"


def test_runtime_ready_success_preserves_dictionary_result_without_gui_runtime():
    command = (
        ". '" + str(SCRIPT).replace("'", "''") + "' -ProtocolSelfTest > $null\n"
        "$Nonce='" + NONCE + "';$script:cleanupCount=0;"
        "$sink=New-Object IO.StringWriter;$saved=[Console]::Out;"
        "try{[Console]::SetOut($sink);"
        "$started=Start-EndpointRuntime -Proof {$r=New-Object 'System.Collections.Generic.Dictionary[string,object]';$r.Add('computer','SAFE_TEST');$r.Add('session_id',1);return $r} -Cleanup {$script:cleanupCount++}}"
        "finally{[Console]::SetOut($saved)};"
        "[ordered]@{started=$started;cleanup_count=$script:cleanupCount;frame=$sink.ToString().Trim()}|ConvertTo-Json -Compress"
    )
    result = json.loads(_run_safe(command))
    assert result["started"] is True and result["cleanup_count"] == 0
    frame = _decode_verified_frame(result["frame"])
    assert frame["ok"] is True and frame["action"] == "ready"
    assert frame["result"] == {"computer": "SAFE_TEST", "session_id": 1}


def test_runtime_resource_failure_does_not_emit_ready_success_or_raw_exception():
    command = (
        ". '" + str(SCRIPT).replace("'", "''") + "' -ProtocolSelfTest > $null\n"
        "$Nonce='" + NONCE + "';$script:cleanupCount=0;"
        "$sink=New-Object IO.StringWriter;$saved=[Console]::Out;"
        "try{[Console]::SetOut($sink);"
        "$started=Start-EndpointRuntime -Proof {return @{computer='SAFE_TEST'}} -Prepare {throw 'PRIVATE_RESOURCE_FAILURE'} -Cleanup {$script:cleanupCount++}}"
        "finally{[Console]::SetOut($saved)};"
        "[ordered]@{started=$started;cleanup_count=$script:cleanupCount;frame=$sink.ToString().Trim()}|ConvertTo-Json -Compress"
    )
    result = json.loads(_run_safe(command))
    assert result["started"] is False and result["cleanup_count"] == 1
    frame = _decode_verified_frame(result["frame"])
    assert frame["ok"] is False and frame["action"] == "ready"
    assert frame["error"]["code"] == "remote_initialization_failed"
    assert "PRIVATE_RESOURCE_FAILURE" not in json.dumps(frame)


def _request_literal(request):
    return "'" + _request_wire(request) + "'"


def test_receipt_replay_canonical_hash_conflict_and_terminal_failure():
    original = {"nonce": NONCE, "reqid": "input-1", "action": "type",
                "arguments": {"text": "A中😀", "expect": {"hwnd": 10, "pid": 20, "creation_time_utc_ticks": "123"}}}
    reordered = {"arguments": {"expect": {"creation_time_utc_ticks": "123", "pid": 20.0, "hwnd": 10.0}, "text": "A中😀"},
                 "action": "type", "reqid": "input-1", "nonce": NONCE}
    conflict = json.loads(json.dumps(original))
    conflict["arguments"]["text"] = "different text"
    command = (
        ". '" + str(SCRIPT).replace("'", "''") + "' -ProtocolSelfTest > $null\n"
        "$r1=Get-Request " + _request_literal(original) + " '" + NONCE + "'\n"
        "$r2=Get-Request " + _request_literal(reordered) + " '" + NONCE + "'\n"
        "$r3=Get-Request " + _request_literal(conflict) + " '" + NONCE + "'\n"
        "$cache=New-ReceiptCache\n"
        "$first=Start-Receipt $cache $r1\n"
        "$hashSame=(Get-PayloadHash $r1) -ceq (Get-PayloadHash $r2)\n"
        "$inflightRejected=$false; try{$null=Start-Receipt $cache $r2}catch{$inflightRejected=$_.Exception.Message -like 'request_inflight:*'}\n"
        "$failureFrame='XIAORUO1_UU_INPUT_V2|eyJvayI6ZmFsc2V9|checksum-test-literal|END'\n"
        "Complete-Receipt $cache $first $failureFrame\n"
        "$replay=Start-Receipt $cache $r2\n"
        "$conflictRejected=$false; try{$null=Start-Receipt $cache $r3}catch{$conflictRejected=$_.Exception.Message -like 'request_id_conflict:*'}\n"
        "$afterConflict=Start-Receipt $cache $r1\n"
        "[ordered]@{first=$first.Kind;hash_same=$hashSame;hash_length=$first.Hash.Length;inflight_rejected=$inflightRejected;replay=$replay.Kind;frame_exact=($replay.Frame -ceq $failureFrame);conflict_rejected=$conflictRejected;original_preserved=($afterConflict.Frame -ceq $failureFrame);count=$cache.Items.Count}|ConvertTo-Json -Compress"
    )
    result = json.loads(_run_safe(command))
    assert result == {"first": "new", "hash_same": True, "hash_length": 64, "inflight_rejected": True,
                      "replay": "replay", "frame_exact": True, "conflict_rejected": True,
                      "original_preserved": True, "count": 1}


def test_receipt_cache_is_bounded_fifo_and_never_evicts_inflight():
    command = (
        ". '" + str(SCRIPT).replace("'", "''") + "' -ProtocolSelfTest > $null\n"
        "function Request($id){return [PSCustomObject]@{nonce='" + NONCE + "';reqid=$id;action='status';arguments=[PSCustomObject]@{}}}\n"
        "$cache=New-ReceiptCache\n"
        "for($i=0;$i -lt 256;$i++){$ticket=Start-Receipt $cache (Request ('id-'+$i));Complete-Receipt $cache $ticket ('frame-'+$i)}\n"
        "$null=Start-Receipt $cache (Request 'id-256')\n"
        "$bounded=($cache.Items.Count -eq 256 -and $cache.Order.Count -eq 256)\n"
        "$fifo=($cache.Order[0] -ceq ('" + NONCE + ":id-1'))\n"
        "$small=New-ReceiptCache 2\n"
        "$a=Start-Receipt $small (Request 'A');$b=Start-Receipt $small (Request 'B')\n"
        "$busy=$false;try{$null=Start-Receipt $small (Request 'C')}catch{$busy=$_.Exception.Message -like 'receipt_cache_busy:*'}\n"
        "Complete-Receipt $small $b 'B-done';$c=Start-Receipt $small (Request 'C')\n"
        "$protected=$small.Items.ContainsKey($a.Key) -and (-not $small.Items.ContainsKey($b.Key))\n"
        "Complete-Receipt $small $a 'A-done';Complete-Receipt $small $c 'C-done'\n"
        "$null=Start-Receipt $small (Request 'C')\n"
        "$d=Start-Receipt $small (Request 'D')\n"
        "$originalOrder=(-not $small.Items.ContainsKey($a.Key)) -and $small.Items.ContainsKey($c.Key)\n"
        "[ordered]@{bounded=$bounded;fifo=$fifo;busy=$busy;inflight_protected=$protected;arrival_order_preserved=$originalOrder}|ConvertTo-Json -Compress"
    )
    assert json.loads(_run_safe(command)) == {"bounded": True, "fifo": True, "busy": True,
                                             "inflight_protected": True, "arrival_order_preserved": True}


def test_receipt_keys_preserve_nonce_and_reqid_case():
    command = (
        ". '" + str(SCRIPT).replace("'", "''") + "' -ProtocolSelfTest > $null\n"
        "$cache=New-ReceiptCache\n"
        "$r1=[PSCustomObject]@{nonce='" + NONCE + "';reqid='ID';action='status';arguments=[PSCustomObject]@{}}\n"
        "$r2=[PSCustomObject]@{nonce='" + NONCE + "';reqid='id';action='status';arguments=[PSCustomObject]@{}}\n"
        "$r3=[PSCustomObject]@{nonce='othernonce';reqid='ID';action='status';arguments=[PSCustomObject]@{}}\n"
        "$a=Start-Receipt $cache $r1;$b=Start-Receipt $cache $r2;$c=Start-Receipt $cache $r3\n"
        "[ordered]@{count=$cache.Items.Count;new=@($a.Kind,$b.Kind,$c.Kind)}|ConvertTo-Json -Compress"
    )
    assert json.loads(_run_safe(command)) == {"count": 3, "new": ["new", "new", "new"]}
