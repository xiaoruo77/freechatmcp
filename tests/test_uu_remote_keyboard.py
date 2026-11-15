import base64
import gzip
import hashlib
import json
import re
from types import SimpleNamespace

import pytest

from web_tools import uu_remote_keyboard as module


def encoded_frame(payload):
    data=json.dumps(payload,ensure_ascii=False,separators=(',',':')).encode('utf-8')
    return module._PREFIX+base64.b64encode(data).decode('ascii')+'|'+hashlib.sha256(data).hexdigest()+'|END\r\n'


def test_unsolicited_protocol_error_does_not_poison_stopped_observer():
    session=module.RemoteKeyboardSession(r'C:\UU\bin\uuyc-cli.exe','private-device-id')
    bad={'protocol':module._PROTOCOL,'nonce':session._nonce,'reqid':'','action':'protocol_error',
         'ok':False,'error':{'code':'request_checksum_mismatch','message':'invalid frame'}}
    session._terminal.feed(encoded_frame(bad))
    assert session._observe_stopped() is None
    stopped=reply(session._nonce,action='stopped',result={'reason':'stop','fixture_closed':False})
    session._terminal.feed(encoded_frame(stopped))
    assert session._observe_stopped()==stopped


@pytest.mark.parametrize('code',[
    'capture_upload_http_401','capture_upload_http_403','capture_upload_http_404',
    'capture_upload_http_409','capture_upload_http_413','capture_upload_http_415',
    'capture_upload_http_429','capture_upload_http_500','capture_upload_http_502',
    'capture_upload_http_503','capture_upload_http_504',
    'capture_upload_timeout_open','capture_upload_timeout_write','capture_upload_timeout_response',
    'capture_upload_tls_failed','capture_upload_connection_failed','capture_upload_dns_failed',
    'capture_encoding_failed','invalid_text','target_identity_mismatch',
])
def test_frame_accepts_fixed_upload_diagnostics_and_legacy_alphabetic_errors_without_raw_message(code):
    session=module.RemoteKeyboardSession(r'C:\UU\bin\uuyc-cli.exe','private-device-id')
    frame={'protocol':module._PROTOCOL,'nonce':session._nonce,'reqid':'capture-1','action':'capture',
           'ok':False,'error':{'code':code,'message':'PRIVATE_NATIVE_EXCEPTION','token':'PRIVATE_TOKEN'}}
    session._terminal.frames.append(frame)
    result=session._frame('capture-1','capture')
    assert result['error']=={'code':code,'message':'The remote GUI input helper rejected this operation'}
    assert 'PRIVATE_NATIVE_EXCEPTION' not in json.dumps(result) and 'PRIVATE_TOKEN' not in json.dumps(result)


@pytest.mark.parametrize('code',['capture_upload_http_418','capture_upload_http_200',
    'capture_upload_http_401_private','private_token_0123456789', 'error123', 'CAPTURE_UPLOAD_HTTP_401'])
def test_frame_does_not_accept_arbitrary_numeric_error_codes(code):
    session=module.RemoteKeyboardSession(r'C:\UU\bin\uuyc-cli.exe','private-device-id')
    session._terminal.frames.append({'protocol':module._PROTOCOL,'nonce':session._nonce,'reqid':'capture-1',
        'action':'capture','ok':False,'error':{'code':code,'message':'PRIVATE_NATIVE_EXCEPTION'}})
    with pytest.raises(module.RemoteKeyboardError) as caught:session._frame('capture-1','capture')
    assert caught.value.code=='uu_remote_protocol_invalid'


def test_cold_terminal_read_105_is_retried_without_mutating_session(fake_remote,monkeypatch):
    session,state=fake_remote
    original=session._cli_runner;calls=[]
    monkeypatch.setattr(module.time,'sleep',lambda _:None)
    def runner(argv,**kwargs):
        calls.append(argv[4])
        if len(calls)<3:return SimpleNamespace(returncode=105,stdout='{"success":false}',stderr='')
        return original(argv,**kwargs)
    session._cli_runner=runner
    assert session._exists() is False
    assert calls==['has','has','has']
    assert not state['writes']


def reply(nonce, reqid="", action="ready", result=None, **extra):
    return {"protocol": module._PROTOCOL, "nonce": nonce, "reqid": reqid, "action": action, "ok": True,
            "result": {} if result is None else result, **extra}


def runtime(computer="REMOTE_TEST", session=1):
    return {"computer": computer, "session_id": session, "active_session_id": session,
            "window_station": "WinSta0", "thread_desktop": "Default", "input_desktop": "Default",
            "helper_pid": 1234, "helper_creation_time_utc_ticks": "638980000000000000", "input_size": 40}


@pytest.fixture
def fake_remote(monkeypatch):
    monkeypatch.setattr(module, "_WARMUP_SECONDS", 0)
    monkeypatch.setattr(module, "_ACK_SETTLE_SECONDS", 0)
    monkeypatch.setattr(module.socket, "gethostname", lambda: "CONTROLLER_TEST")
    monkeypatch.setenv("COMPUTERNAME", "CONTROLLER_TEST")
    state = {"exists": False, "computer": "REMOTE_TEST", "session": 1, "management": [], "writes": [],
             "requests": [], "output": [], "compressed": "", "suppress": False, "kill_fails": False,
             "wrong_ready": False, "probe_echo_only": False, "frame_action": None,
             "drop_chunk_character": False, "corrupt_same_length": False, "append_echo_only": False,
             "load_echo_only": False, "reader_started": False, "chunks": [], "chunk_index": 0,
             "late_output": [], "ghost_append_ack": False, "chunk_writes": [], "chunk_records": {},
             "chunk_attempts": {}, "nak_first_chunk_once": False, "drop_first_ack_once": False,
             "drop_final_ack": False, "tamper_accumulator": False, "duplicates": 0}

    class Process:
        pid = 4321

        def __init__(self):
            self.closed = False

        def write(self, value):
            state["writes"].append(value)
            if "XRUPROBE_" in value:
                # PowerShell enters the probe expression before writing its
                # result. A bare CR would overwrite an un-erased command tail
                # and does not model the actual console's submitted line.
                state["output"].append(value.rstrip('\r\n') + '\r\n')
                nonce = re.search(r"XRUPROBE_([a-f0-9]+)\|", value).group(1)
                if not state["probe_echo_only"]:
                    state["output"].append(f"\x1b[31mXRUPROBE_{nonce}|{state['computer']}|{state['session']}|END\x1b[0m\r\n")
            elif value.startswith("$xrB="):
                # Decode the fixed gzip-packed loader only. The fake never
                # evaluates PowerShell or executes a helper/desktop operation.
                state["launcher"] = value
                packed = re.search(r"FromBase64String\('([A-Za-z0-9+/=]+)'\)", value).group(1)
                program = gzip.decompress(base64.b64decode(packed)).decode('utf-8')
                state["program"] = program
                state["reader_started"] = True
                state["chunk_count"] = int(re.search(r"\$script:xrCount=([0-9]+);", program).group(1))
                state["nonce"] = re.search(r"\$script:xrNonce='([a-f0-9]+)'", program).group(1)
                state["expected_length"] = int(re.search(r"\$script:xrLength=([0-9]+);", program).group(1))
                state["expected_hash"] = re.search(r"\$xrWholeHash -cne '([a-f0-9]{64})'", program).group(1)
                assert re.search(r"-Nonce '([a-f0-9]+)'", program).group(1) == state["nonce"]
                assert re.search(r"-ExpectedComputer '([^']+)'", program).group(1) == state["computer"]
                state["output"].append(value)
                if not state["load_echo_only"]:
                    # build_loader.xrEmit clears the console and restores the
                    # cursor before each control record, including duplicates.
                    state["output"].append("\x1b[2J\x1b[1;1HXRU_LOAD_" + state["nonce"] + "|0|END\r\n")
            elif value.startswith('XRUCHUNK|'):
                state['chunk_writes'].append(value)
                fields = value.rstrip('\r\n').split('|')
                assert len(fields) == 6 and fields[0] == 'XRUCHUNK' and fields[-1] == 'END'
                nonce, index, chunk, digest = fields[1], int(fields[2]), fields[3], fields[4]
                assert nonce == state['nonce']
                assert hashlib.sha256(chunk.encode('ascii')).hexdigest() == digest
                state['chunk_attempts'][index] = state['chunk_attempts'].get(index, 0) + 1
                nak = '\x1b[2J\x1b[1;1HXRU_NAK_' + state['nonce'] + '|' + str(state['chunk_index']) + '|END\r\n'
                if state['nak_first_chunk_once'] and index == 0 and state['chunk_attempts'][index] == 1:
                    state['output'].append(nak)
                    return
                if state["drop_chunk_character"]:
                    chunk = chunk[:-1]
                if state["corrupt_same_length"]:
                    chunk = ("A" if chunk[0] != "A" else "B") + chunk[1:]
                # Match the actual receiver: a corrupted packet cannot advance
                # its expected index or enter the source accumulator.
                if hashlib.sha256(chunk.encode('ascii')).hexdigest() != digest:
                    state['output'].append(nak)
                    return
                if index in state['chunk_records']:
                    record = state['chunk_records'][index]
                    if record['data'] == chunk and record['hash'] == digest:
                        state['duplicates'] += 1
                        state['output'].append(value if state['append_echo_only'] else record['ack'])
                    else:
                        state['output'].append(nak)
                    return
                remaining = state['chunk_count'] - index - 1
                new_length = len(state['compressed']) + len(chunk)
                if (index != state['chunk_index'] or not 1 <= len(chunk) <= 1024
                        or (remaining and '=' in chunk) or new_length + remaining > state['expected_length']
                        or new_length + remaining * 1024 < state['expected_length']):
                    state['output'].append(nak)
                    return
                old_length = len(state["compressed"])
                state["compressed"] += chunk
                state["chunks"].append(chunk)
                state["chunk_index"] += 1
                state["output"].append(value)
                prefix = "XRU_APPEND_" + state["nonce"] + "_" + str(index) + "|"
                ack = '\x1b[2J\x1b[1;1H' + prefix + str(len(state['compressed'])) + '|' + digest + '|END\r\n'
                state['chunk_records'][index] = {'data': chunk, 'hash': digest, 'ack': ack}
                if (not state["append_echo_only"]
                        and not (state['drop_first_ack_once'] and index == 0 and state['chunk_attempts'][index] == 1)
                        and not (state['drop_final_ack'] and state['chunk_index'] == state['chunk_count'])):
                    if state["ghost_append_ack"] and index == 1:
                        # UU can redraw a new ACK index while its length cell
                        # still contains the preceding chunk's value.
                        state["output"].append('\x1b[2J\x1b[1;1H' + prefix + str(old_length) + '|' + digest + "|END\r\n")
                        state["late_output"].append(ack)
                        state["ghost_observed"] = True
                    else:
                        state["output"].append(ack)
                if state["chunk_index"] == state["chunk_count"]:
                    if state['tamper_accumulator']:
                        state['compressed'] = ('A' if state['compressed'][0] != 'A' else 'B') + state['compressed'][1:]
                    digest = hashlib.sha256(state["compressed"].encode("ascii")).hexdigest()
                    state["output"].append("\x1b[2J\x1b[1;1HXRU_VERIFY_" + state["nonce"] + "|" + str(len(state["compressed"])) + "|" + digest + "|END\r\n")
                    if len(state["compressed"]) != state["expected_length"] or digest != state["expected_hash"]:
                        state["output"].append("\x1b[2J\x1b[1;1HXRU_FAIL_" + state["nonce"] + "|1|END\r\n")
                        state["bootstrap_rejected"] = True
                    else:
                        state["loader_executed"] = True
                        data = runtime(state["computer"], state["session"])
                        if state["wrong_ready"]:
                            data["input_desktop"] = "Winlogon"
                        state["output"].append(encoded_frame(reply(state["nonce"], result=data)))
            else:
                request_wire=value.strip().split('|')
                assert request_wire[0]=='XIAORUO1_UU_REQUEST_V2'
                raw_request=base64.b64decode(request_wire[1])
                assert request_wire[2]==hashlib.sha256(raw_request).hexdigest()
                assert request_wire[3]=='END'
                request = json.loads(raw_request.decode('utf-8'))
                state["requests"].append(request)
                if not state["suppress"]:
                    result = {"foreground": {"available": False}} if request["action"] == "status" else {}
                    if request["action"] == "stop":
                        result = {"stopping": True, "fixture_closed": False}
                    state["output"].append(encoded_frame(reply(state["nonce"], request["reqid"], state["frame_action"] or request["action"], result)))
                    if request["action"] == "stop":
                        state["output"].append(encoded_frame(reply(state["nonce"], action="stopped", result={"fixture_closed": False, "reason": "stop"})))

        def read_available(self):
            value = "".join(state["output"])
            state["output"].clear()
            if not value and state["late_output"]:
                value = state["late_output"].pop(0)
            # Only the initial shell probe returns to a PowerShell prompt.
            # Once the continuous reader starts, chunks never reach that shell.
            return value+'\r\nPS C:\\UU> ' if value and not state["reader_started"] else value

        def poll(self):
            return 0 if self.closed else None

        def close(self):
            self.closed = True

    def factory(argv, **kwargs):
        state["factory"] = (argv, kwargs)
        state["exists"] = True
        state["process"] = Process()
        return state["process"]

    def runner(argv, **kwargs):
        action = argv[4]
        state["management"].append((argv, kwargs))
        if action == "kill" and state["kill_fails"]:
            raise OSError("private-device-id must never escape")
        if action == "kill":
            state["exists"] = False
        data = {"exists": state["exists"], "attachable": state["exists"]} if action == "has" else {}
        # UU 4.46 returns exit code 1 for a successfully queried missing term.
        code = 1 if action == "has" and not state["exists"] else 0
        return SimpleNamespace(returncode=code, stdout=json.dumps({"success": True, "data": data, "error": None}), stderr="")

    session = module.RemoteKeyboardSession(r"C:\UU\bin\uuyc-cli.exe", "private-device-id", process_factory=factory, cli_runner=runner)
    return session, state


def test_start_only_loads_fixed_helper_in_remote_memory(fake_remote):
    session, state = fake_remote
    data = session.start()
    assert data == runtime()
    argv, kwargs = state["factory"]
    assert argv[:5] == [str(session.cli), "term", "-d", "private-device-id", "new"]
    assert re.fullmatch(r"xru_input_[a-f0-9]{16}", argv[5])
    assert len(argv[5]) == 26
    assert kwargs["columns"] == 160 and kwargs["rows"] == 52
    assert state["requests"] == []
    decoded = gzip.decompress(base64.b64decode(state["compressed"])).decode("utf-8")
    assert decoded == module.Path(module.__file__).with_name("uu_remote_input.ps1").read_text(encoding="utf-8-sig")
    assert state["chunks"] and all(len(chunk) <= 1024 for chunk in state["chunks"])
    assert len(state["chunks"]) == state["chunk_count"]
    assert len(state["compressed"]) == state["expected_length"]
    assert hashlib.sha256(state["compressed"].encode('ascii')).hexdigest() == state["expected_hash"]
    assert state["loader_executed"] and not state.get("bootstrap_rejected")
    programs = [line for line in state["writes"] if line.startswith("$xrB=")]
    assert programs == [state['launcher']] and len(programs[0]) <= 4001
    assert len(state['program']) < 6000
    assert "[Console]::ReadLine()" in state['program'] and state['program'].endswith(';exit')
    assert all(len(wire) <= 1301 for wire in state['chunk_writes'])
    assert not any(line.startswith("Remove-Module ") for line in state["writes"])
    assert not any("ExecutionPolicy" in line or "Set-Content" in line for line in state["writes"])
    count = len(state["writes"])
    assert session.start() == data and len(state["writes"]) == count
    renderer = session._terminal
    result = session.close()
    assert result["cleanup_complete"] and result["helper_stopped_observed"]
    assert state["process"].closed and not state["exists"]
    assert renderer._screen is renderer._stream is None
    assert not renderer.text and not renderer.frames and not renderer._seen_frames


def test_owned_term_name_uses_short_unique_underscore_template(fake_remote):
    session, _ = fake_remote
    other = module.RemoteKeyboardSession(r"C:\UU\uuyc-cli.exe", "private-device-id")
    assert session._name != other._name
    assert re.fullmatch(r"[A-Za-z0-9_]{26}", session._name)
    assert re.fullmatch(r"xru_input_[a-f0-9]{16}", other._name)


@pytest.mark.parametrize("failure", ["drop_chunk_character", "corrupt_same_length", "tamper_accumulator"])
def test_bootstrap_integrity_rejects_dropped_or_altered_source_before_execution(fake_remote, monkeypatch, failure):
    session, state = fake_remote
    state[failure] = True
    clock = [0.0]
    monkeypatch.setattr(module.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(module.time, 'sleep', lambda seconds: clock.__setitem__(0, clock[0]+max(seconds, .0001)))
    with pytest.raises(module.RemoteKeyboardError) as error:
        session.start(timeout=10)
    assert error.value.code == "uu_bootstrap_integrity_failed"
    assert not state.get("loader_executed")
    assert not state["requests"]
    if failure != 'tamper_accumulator':
        assert not state['chunks']
        assert len(state['chunk_writes']) == 3
        assert len(set(state['chunk_writes'])) == 1
    else:
        assert state['bootstrap_rejected'] and len(state['chunks']) == state['chunk_count']
    assert session.close()["cleanup_complete"]


def test_bootstrap_echo_is_not_append_ack_and_source_retries_are_bounded(fake_remote, monkeypatch):
    session, state = fake_remote
    state["append_echo_only"] = True
    clock = [0.0]
    monkeypatch.setattr(module.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(module.time, 'sleep', lambda seconds: clock.__setitem__(0, clock[0]+max(seconds, .0001)))
    with pytest.raises(module.RemoteKeyboardError) as error:
        session.start(timeout=10)
    assert error.value.code == "uu_bootstrap_integrity_failed"
    assert error.value.stage == session.stage == "bootstrap_chunk_0"
    assert "stage=bootstrap_chunk_0" in str(error.value)
    assert len(state["chunks"]) == 1
    assert len(state['chunk_writes']) == 3 and len(set(state['chunk_writes'])) == 1
    assert not state.get("loader_executed")
    assert session.close()["cleanup_complete"]


def test_continuous_reader_expression_echo_is_not_a_load_ack(fake_remote):
    session, state = fake_remote
    state['load_echo_only'] = True
    with pytest.raises(module.RemoteKeyboardError) as error:
        session.start(timeout=.03)
    assert error.value.code == 'uu_remote_input_timeout'
    assert error.value.stage == 'bootstrap_reader'
    assert not state['chunks'] and not state.get('loader_executed')
    assert len([line for line in state['writes'] if line.startswith("$xrB=")]) == 1
    assert session.close()['cleanup_complete']


def test_bootstrap_waits_past_ghost_old_length_until_correct_ack(fake_remote):
    session, state = fake_remote
    state['ghost_append_ack'] = True
    assert session.start() == runtime()
    assert state['ghost_observed']
    assert len(state['chunks']) == state['chunk_count']
    assert state['loader_executed'] and not state['requests']
    assert session.close()['cleanup_complete']


@pytest.mark.parametrize('fault', ['drop_first_ack_once', 'nak_first_chunk_once'])
def test_idempotent_source_retries_recover_missing_ack_or_nak_without_double_append(fake_remote, monkeypatch, fault):
    session, state = fake_remote
    state[fault] = True
    clock = [0.0]
    monkeypatch.setattr(module.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(module.time, 'sleep', lambda seconds: clock.__setitem__(0, clock[0]+max(seconds, .0001)))
    assert session.start() == runtime()
    assert state['chunk_attempts'][0] == 2
    first_wires = [wire for wire in state['chunk_writes'] if wire.split('|')[2] == '0']
    assert len(first_wires) == 2 and len(set(first_wires)) == 1
    assert len(state['chunks']) == state['chunk_count'] == len(state['chunk_records'])
    assert hashlib.sha256(state['compressed'].encode('ascii')).hexdigest() == state['expected_hash']
    assert not state['requests'] and state['loader_executed']
    assert session.close()['cleanup_complete']


def test_final_matching_verify_and_ready_prove_acceptance_when_last_ack_is_lost(fake_remote):
    session, state = fake_remote
    state['drop_final_ack'] = True
    assert session.start() == runtime()
    assert state['chunk_attempts'][state['chunk_count']-1] == 1
    assert len(state['chunks']) == state['chunk_count']
    assert state['loader_executed'] and not state['requests']
    assert session.close()['cleanup_complete']


def test_rejects_controller_identity_before_loading_or_input(fake_remote):
    session, state = fake_remote
    state["computer"] = "CONTROLLER_TEST"
    with pytest.raises(module.RemoteKeyboardError) as error:
        session.start()
    assert error.value.code == "uu_remote_identity_rejected"
    assert not state["compressed"] and not state["requests"]
    assert session.close()["cleanup_complete"]


def test_probe_expression_echo_is_not_remote_identity(fake_remote):
    session, state = fake_remote
    state["probe_echo_only"] = True
    with pytest.raises(module.RemoteKeyboardError) as error:
        session.start(timeout=0.03)
    assert error.value.code == "uu_remote_input_timeout"
    assert not state["compressed"]
    assert session.close()["cleanup_complete"]


def test_ready_rejects_secure_or_wrong_desktop(fake_remote):
    session, state = fake_remote
    state["wrong_ready"] = True
    with pytest.raises(module.RemoteKeyboardError) as error:
        session.start()
    assert error.value.code == "uu_remote_runtime_rejected"
    assert not state["requests"]
    assert session.close()["cleanup_complete"]


def test_fixed_action_requests_and_receipts_are_not_resent(fake_remote):
    session, state = fake_remote
    session.start()
    first = session.request("status", {}, request_id="status-1")
    assert first["action"] == "status" and first["ok"]
    assert len(state["requests"]) == 1
    first["result"]["mutated"] = True
    assert "mutated" not in session.request("status", {}, request_id="status-1")["result"]
    assert len(state["requests"]) == 1
    with pytest.raises(module.RemoteKeyboardError) as error:
        session.request("fixture_state", {}, request_id="status-1")
    assert error.value.code == "uu_remote_request_id_conflict"
    assert len(state["requests"]) == 1
    assert session.close()["cleanup_complete"]


def test_unknown_outcome_same_id_only_polls_then_accepts_late_reply(fake_remote):
    session, state = fake_remote
    session.start()
    state["suppress"] = True
    arguments = {"text": "测试🙂", "expect": {"hwnd": 10, "pid": 20, "creation_time_utc_ticks": "123"}}
    for _ in range(2):
        with pytest.raises(module.RemoteKeyboardError) as error:
            session.request("type", arguments, request_id="exact-once", timeout=0.02)
        assert error.value.unknown_outcome and error.value.request_id == "exact-once"
    assert len(state["requests"]) == 1
    state["output"].append(encoded_frame(reply(state["nonce"], "exact-once", "type", {"inserted_events": 8})))
    assert session.request("type", arguments, request_id="exact-once")["result"]["inserted_events"] == 8
    assert len(state["requests"]) == 1
    state["suppress"] = False
    assert session.close()["cleanup_complete"]


@pytest.mark.parametrize("action,args", [
    ("execute", {"command": "no"}), ([], {}), ("status", {"extra": True}),
    ("type", {"text": "a" * 1001, "expect": {"hwnd": 1, "pid": 2, "creation_time_utc_ticks": "3"}}),
    ("type", {"text": "bad\0", "expect": {"hwnd": 1, "pid": 2, "creation_time_utc_ticks": "3"}}),
    ("type", {"text": "bad", "expect": {"hwnd": True, "pid": 2, "creation_time_utc_ticks": "3"}}),
    ("type", {"text": "bad", "expect": {"hwnd": 1, "pid": 2, "creation_time_utc_ticks": "9223372036854775808"}}),
    ("key", {"keys": "CTRL+ALT+DELETE", "expect": {"hwnd": 1, "pid": 2, "creation_time_utc_ticks": "3"}}),
    ("key", {"keys": ["CTRL", "CONTROL"], "expect": {"hwnd": 1, "pid": 2, "creation_time_utc_ticks": "3"}}),
])
def test_schema_rejection_sends_nothing(fake_remote, action, args):
    session, state = fake_remote
    session.start()
    with pytest.raises(module.RemoteKeyboardError):
        session.request(action, args)
    assert state["requests"] == []
    assert session.close()["cleanup_complete"]


def test_frame_parser_handles_vt_chunk_boundaries_and_line_wraps():
    payload = reply("a" * 32, "r1", "status", {"text": "测试🙂"})
    frame = encoded_frame(payload)
    prefix, encoded = frame.split("|", 1)
    wrapped = prefix + "|" + "\r\n".join(encoded[index:index + 37] for index in range(0, len(encoded), 37))
    terminal = module._TerminalFrames()
    raw = "\x1b]0;secret title\x1b\\" + wrapped + "\x1b[32mPS C:\\>\x1b[0m"
    for character in raw:
        terminal.feed(character)
    assert terminal.frames == [payload]
    assert "secret title" not in terminal.text


def test_frame_parser_rejects_echoed_prefix_and_keeps_next_real_frame():
    terminal = module._TerminalFrames()
    terminal.feed("[Console]::WriteLine('" + module._PREFIX + "'+$base64)\r\n")
    assert terminal.frames == []
    terminal.feed(encoded_frame(reply("a" * 32)))
    assert len(terminal.frames) == 1


def test_frame_parse_keeps_coalesced_bootstrap_control_acknowledgements():
    nonce = 'a' * 32
    append = 'XRU_APPEND_' + nonce + '_56|14440|END'
    verify = 'XRU_VERIFY_' + nonce + '|14440|' + 'b' * 64 + '|END'
    payload = reply(nonce, result=runtime())
    terminal = module._TerminalFrames()
    terminal.feed(append + '\r\n' + verify + '\r\n' + encoded_frame(payload))
    assert terminal.frames == [payload]
    assert append in terminal.text
    assert verify in terminal.text


@pytest.mark.parametrize('tamper', ['payload', 'checksum', 'missing_end'])
def test_v2_frame_requires_matching_raw_json_checksum_and_end(tamper):
    payload = reply('a' * 32, 'crc-request', 'status', {'value': 12})
    valid = encoded_frame(payload)
    prefix, encoded, checksum, end = valid.rstrip('\r\n').split('|')
    if tamper == 'payload':
        altered = {**payload, 'result': {'value': 99}}
        encoded = base64.b64encode(json.dumps(altered, ensure_ascii=False, separators=(',', ':')).encode()).decode()
    elif tamper == 'checksum':
        checksum = ('0' if checksum[0] != '0' else '1') + checksum[1:]
    else:
        end = 'EN'
    terminal = module._TerminalFrames()
    terminal.feed('|'.join((prefix, encoded, checksum, end)) + '\r\n')
    assert terminal.frames == []
    terminal.feed('\r\x1b[2K' + valid)
    assert terminal.frames == [payload]


def test_v2_checksum_rejects_repainted_new_request_with_old_result():
    old = reply('b' * 32, 'request-old', 'status', {'value': 'old'})
    new = reply('b' * 32, 'request-new', 'status', {'value': 'new'})
    ghost = {**new, 'result': old['result']}
    old_checksum = encoded_frame(old).rstrip('\r\n').split('|')[2]
    ghost_data = json.dumps(ghost, ensure_ascii=False, separators=(',', ':')).encode()
    terminal = module._TerminalFrames()
    terminal.feed(module._PREFIX + base64.b64encode(ghost_data).decode() + '|' + old_checksum + '|END\r\n')
    assert terminal.frames == []
    terminal.feed(encoded_frame(new))
    assert terminal.frames == [new]


def test_long_unicode_v2_frame_survives_more_than_32_wrapped_rows():
    payload = reply('c' * 32, 'unicode-long', 'fixture_state', {'text': '中文' * 500})
    body = encoded_frame(payload)[len(module._PREFIX):].rstrip('\r\n')
    width = 72
    chunks = [body[index:index + width] for index in range(0, len(body), width)]
    assert len(chunks) > 32
    terminal = module._TerminalFrames()
    terminal.feed(module._PREFIX + '\r\n'.join(chunks) + '\r\n')
    assert terminal.frames == [payload]


def test_vt_cursor_overwrite_reconstructs_ack_instead_of_concatenating_bytes():
    terminal = module._TerminalFrames()
    prefix = "XRUA_" + "a" * 32 + "_psrl|"
    terminal.feed(prefix + "X|END")
    assert prefix + "1|END" not in terminal.text
    # The acknowledgement is produced by replacing one screen cell. The raw
    # suffix alone contains no prefix and the earlier cell was invalid.
    terminal.feed("\x1b[" + str(len(prefix) + 1) + "G1")
    assert prefix + "1|END" in terminal.text
    assert terminal.screen_text == prefix + "1|END"


def test_vt_repair_and_redraw_deliver_each_protocol_frame_once():
    terminal = module._TerminalFrames()
    payload = reply("a" * 32, "rewritten", "status", {"value": 42})
    frame = encoded_frame(payload).rstrip("\r\n")
    repair = len(module._PREFIX) + 12
    terminal.feed(frame[:repair] + "!" + frame[repair + 1:])
    assert terminal.frames == []
    terminal.feed("\x1b[1;" + str(repair + 1) + "H" + frame[repair])
    assert terminal.frames == [payload]
    terminal.frames.clear()  # Logical consumer does not reset the VT screen.
    terminal.text = ""
    terminal.feed("\r\x1b[2K" + frame)
    assert terminal.frames == []


def test_vt_out_of_order_screen_fragments_reconstruct_complete_marker():
    terminal = module._TerminalFrames()
    marker = "XRU_APPEND_" + "b" * 32 + "_0|256|END"
    split = 25
    terminal.feed("\x1b[1;" + str(split + 1) + "H" + marker[split:])
    terminal.feed("\x1b[1;1H" + marker[:split])
    assert marker in terminal.text
    assert terminal.screen_text == marker


def test_marker_is_captured_before_erase_and_scroll_in_same_read():
    terminal = module._TerminalFrames()
    payload = reply("c" * 32, "transient", "status")
    terminal.feed(encoded_frame(payload) + "\x1b[2J\x1b[1;1HPS C:\\>" + "\r\n" * 100)
    assert terminal.frames == [payload]
    assert module._PREFIX not in terminal.screen_text


def test_wrong_nonce_and_protocol_are_not_accepted(fake_remote):
    session, state = fake_remote
    session.start()
    state["suppress"] = True
    state["output"].append(encoded_frame(reply("wrong", "r1", "status")))
    state["output"].append(encoded_frame(reply(state["nonce"], "r1", "status", protocol="wrong")))
    with pytest.raises(module.RemoteKeyboardError) as error:
        session.request("status", {}, request_id="r1", timeout=0.02)
    assert error.value.unknown_outcome
    state["suppress"] = False
    assert session.close()["cleanup_complete"]


def test_matching_id_wrong_action_is_protocol_failure(fake_remote):
    session, state = fake_remote
    session.start()
    state["frame_action"] = "type"
    with pytest.raises(module.RemoteKeyboardError) as error:
        session.request("status", {})
    assert error.value.code == "uu_remote_protocol_mismatch" and error.value.may_have_run
    state["frame_action"] = None
    assert session.close()["cleanup_complete"]


def test_terminal_kill_failure_is_reported_and_local_channel_still_closes(fake_remote):
    session, state = fake_remote
    session.start()
    state["kill_fails"] = True
    result = session.close()
    assert not result["cleanup_complete"] and not result["remote_terminal_absent"]
    assert result["local_channel_closed"] and state["process"].closed
    assert "private-device-id" not in json.dumps(result)
    state["kill_fails"] = False
    assert session.close()["cleanup_complete"]


def test_close_reconciles_delayed_exact_terminal_deletion_without_second_kill(fake_remote,monkeypatch):
    session,state=fake_remote
    session.start()
    original=session._cli_runner
    after_kill=False
    polls=0
    def runner(argv,**kwargs):
        nonlocal after_kill,polls
        if argv[4]=='kill':
            state['management'].append((argv,kwargs));after_kill=True
            return SimpleNamespace(returncode=0,stdout='{"success":true,"data":{}}',stderr='')
        if after_kill:
            polls+=1
            if polls>=3:state['exists']=False
        return original(argv,**kwargs)
    session._cli_runner=runner
    monkeypatch.setattr(module.time,'sleep',lambda _:None)
    result=session.close()
    assert result['cleanup_complete'] and result['remote_terminal_absent'] and polls==3
    kills=[a for a,k in state['management'] if a[4]=='kill']
    assert len(kills)==1 and kills[0][1]=='term' and kills[0][5]==session._name


def test_close_rejected_kill_is_diagnostic_if_exact_terminal_is_proven_absent(fake_remote):
    session,state=fake_remote
    session.start()
    original=session._cli_runner
    def runner(argv,**kwargs):
        if argv[4]=='kill':
            state['management'].append((argv,kwargs));state['exists']=False
            return SimpleNamespace(returncode=105,stdout='{"success":false,"error":{"code":105}}',stderr='')
        return original(argv,**kwargs)
    session._cli_runner=runner
    result=session.close()
    assert result['cleanup_complete'] and result['remote_terminal_absent']
    assert result['diagnostic_codes']==['uu_terminal_management_rejected']
    assert 'error_codes' not in result
    assert all(a[1]=='term' and a[4] in {'has','kill'} for a,k in state['management'])


def test_close_already_absent_exact_terminal_does_not_send_kill(fake_remote):
    session,state=fake_remote
    session.start()
    state['exists']=False
    state['output'].append(encoded_frame(reply(session._nonce,action='stopped',result={'reason':'idle_timeout','fixture_closed':False})))
    result=session.close()
    assert result['cleanup_complete'] and result['helper_stopped_observed']
    assert not any(a[4]=='kill' for a,k in state['management'])


def test_existing_terminal_name_is_not_reused_or_killed(fake_remote):
    session, state = fake_remote
    state["exists"] = True
    with pytest.raises(module.RemoteKeyboardError) as error:
        session.start()
    assert error.value.code == "uu_terminal_name_conflict"
    assert session.close()["cleanup_complete"]
    assert [call[0][4] for call in state["management"]] == ["has"]


def test_read_failure_after_write_remains_unknown_and_not_retried(fake_remote):
    session, state = fake_remote
    session.start()
    original_read = state["process"].read_available
    state["process"].read_available = lambda: (_ for _ in ()).throw(OSError("sensitive output")) if state["requests"] else original_read()
    with pytest.raises(module.RemoteKeyboardError) as error:
        session.request("status", {}, request_id="no-resend")
    assert error.value.may_have_run and error.value.request_id == "no-resend"
    assert "sensitive output" not in str(error.value)
    assert len(state["requests"]) == 1
    state["process"].read_available = original_read
    session.close()


def test_explicit_stop_prevents_requests_reaching_the_returned_shell(fake_remote):
    session, state = fake_remote
    session.start()
    assert session.request("stop", {}, request_id="stop-once")["ok"]
    with pytest.raises(module.RemoteKeyboardError) as error:
        session.request("status", {})
    assert error.value.code == "uu_remote_helper_stopped"
    assert len(state["requests"]) == 1
    assert session.request("stop", {}, request_id="stop-once")["ok"]
    assert session.close()["cleanup_complete"]
    assert len(state["requests"]) == 1


def test_observed_idle_exit_is_cleaned_up_without_writing_to_shell(fake_remote):
    session, state = fake_remote
    session.start()
    state["output"].append(encoded_frame(reply(state["nonce"], action="stopped", result={"fixture_closed": False, "reason": "idle_timeout"})))
    result = session.close()
    assert result["cleanup_complete"] and result["helper_stopped_observed"]
    assert not result["stop_acknowledged"] and not state["requests"]


@pytest.mark.parametrize("action,code,payload,accepted", [
    ("has", 1, {"success": True, "data": {"exists": False, "attachable": False}, "error": None}, True),
    ("has", 1, {"success": True, "data": {"exists": True}}, False),
    ("has", 1, {"success": False, "data": {"exists": False}}, False),
    ("has", 1, {"success": True, "data": {"exists": 0}}, False),
    ("has", 1, {"success": True, "data": {}}, False),
    ("has", 2, {"success": True, "data": {"exists": False}}, False),
    ("kill", 1, {"success": True, "data": {"exists": False}}, False),
])
def test_real_missing_term_exit_one_is_the_only_nonzero_success(fake_remote, action, code, payload, accepted):
    session, _ = fake_remote
    session._cli_runner = lambda *args, **kwargs: SimpleNamespace(returncode=code, stdout=json.dumps(payload), stderr="")
    if accepted:
        assert session._management(action) == payload
        assert session._exists() is False
    else:
        with pytest.raises(module.RemoteKeyboardError) as error:
            session._management(action)
        assert error.value.code == "uu_terminal_management_rejected"
