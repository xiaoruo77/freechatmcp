from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from web_tools import uu_shared_gui as module


TARGET = {'available': True, 'hwnd': 9001, 'pid': 5200,
          'creation_time_utc_ticks': '638990000000000000',
          'title': 'Remote application', 'process_name': 'notepad'}
BOUNDS = {'left': -1920, 'top': -100, 'width': 3840, 'height': 1180}


class ObservedProcess:
    def __init__(self, pid, executable, created=100.25):
        self.pid = pid
        self.info = {'pid': pid, 'exe': str(executable), 'create_time': created}
        self.closed = False
        self.dangerous_calls = []

    def create_time(self):
        return self.info['create_time']

    def terminate(self):
        self.dangerous_calls.append('terminate')
        raise AssertionError('The shared GUI must never be terminated')

    def kill(self):
        self.dangerous_calls.append('kill')
        raise AssertionError('The shared GUI must never be killed')


class FakeEndpoint:
    def __init__(self):
        self.calls = []
        self.start_count = 0
        self.close_count = 0
        self.cleanup_complete = True
        self.capture_receipt = {'capture_id': 'ticket-1', 'remote_bounds': dict(BOUNDS),
                                'foreground': dict(TARGET), 'sha256': '0' * 64}
        self.failure = None

    def start(self, timeout=45):
        self.start_count += 1
        return {'computer': 'remote', 'window_station': 'WinSta0', 'input_desktop': 'Default'}

    def request(self, action, args, request_id=None, timeout=10):
        self.calls.append((action, copy.deepcopy(args), request_id))
        if self.failure is not None and action == self.failure[0]:
            raise self.failure[1]
        result = (copy.deepcopy(self.capture_receipt) if action == 'capture'
                  else {'foreground': dict(TARGET)} if action == 'status'
                  else {'accepted': True})
        return {'ok': True, 'action': action, 'result': result}

    def close(self):
        self.close_count += 1
        return {'cleanup_complete': self.cleanup_complete,
                'remote_terminal_absent': self.cleanup_complete,
                'local_channel_closed': self.cleanup_complete}


class FakeCapture:
    public_origin = 'https://example.test'

    def __init__(self, tmp_path):
        self.issue_count = 0
        self.waited = []
        self.cancelled = []
        self.ticket = {'upload_url': self.public_origin + '/capture/ticket-1',
                       'token': 'one-use-private-ticket', 'capture_id': 'ticket-1'}
        self.metadata = {'path': str(tmp_path / 'remote.png'), 'mime_type': 'image/png',
                         'width': BOUNDS['width'], 'height': BOUNDS['height'],
                         'sha256': '0' * 64}

    def issue(self):
        self.issue_count += 1
        return dict(self.ticket)

    def wait(self, capture_id):
        self.waited.append(capture_id)
        return dict(self.metadata)

    def cancel(self, capture_id):
        self.cancelled.append(capture_id)


@pytest.fixture
def rig(tmp_path, monkeypatch):
    root = tmp_path / 'UU'
    (root / 'bin').mkdir(parents=True)
    for executable in (root / 'GameViewer.exe', root / 'bin' / 'GameViewer.exe', root / 'bin' / 'uuyc-cli.exe'):
        executable.write_bytes(b'fixture')
    # Shapes mirror the real Default root/bin GUI + Server/Service inventory.
    processes = [ObservedProcess(24832, root / 'GameViewer.exe'),
                 ObservedProcess(24840, root / 'bin' / 'GameViewer.exe', 100.5),
                 ObservedProcess(300, root / 'bin' / 'GameViewerService.exe'),
                 ObservedProcess(301, root / 'bin' / 'GameViewerServer.exe'),
                 ObservedProcess(400, tmp_path / 'other' / 'GameViewer.exe')]
    state = {'default': {24832, 24840, 400}, 'logged_in': True,
             'devices': [{'deviceId': 'private-id-2', 'deviceName': '演示设备',
                          'isOnline': True, 'platform': 'windows'}], 'cli': [], 'factory': []}
    import psutil
    monkeypatch.setattr(psutil, 'process_iter', lambda fields: iter(processes))
    monkeypatch.setattr(module, '_default_window_pids', lambda: set(state['default']))

    def runner(command, **kwargs):
        operation = tuple(command[1:-1])
        state['cli'].append(operation)
        assert command[-1] == '--json'
        assert kwargs['stdin'] is not None
        assert operation in {('status',), ('device', 'list')}
        # Incoming phone control is active. Neither its ownership nor idle
        # status is a precondition to this independent temporary endpoint.
        data = ({'isLoggedIn': state['logged_in'], 'hasActiveConnections': True}
                if operation == ('status',) else {'devices': state['devices']})
        return SimpleNamespace(returncode=0, stdout=json.dumps({'success': True, 'data': data}), stderr='')

    endpoint = FakeEndpoint()
    capture = FakeCapture(tmp_path)

    def factory(cli, device_id):
        state['factory'].append((cli, device_id))
        return endpoint

    session = module.SharedUUGuiSession(tmp_path, root / 'GameViewer.exe', runner,
                                       endpoint_factory=factory, capture_client=capture)
    return SimpleNamespace(session=session, endpoint=endpoint, capture=capture,
                           state=state, processes=processes, root=root)


def connect(rig):
    result = rig.session.perform('connect', {'device_name': '演示设备'})
    assert result['ok'], result
    return result


def snapshot(rig):
    result = rig.session.perform('screenshot')
    assert result['ok'], result
    return result


def assert_preserved(rig):
    assert [(p.pid, p.closed, p.dangerous_calls) for p in rig.processes] == [
        (24832, False, []), (24840, False, []), (300, False, []),
        (301, False, []), (400, False, [])]
    assert set(rig.state['cli']) <= {('status',), ('device', 'list')}


def test_status_devices_are_readonly_and_never_launch_an_endpoint(rig):
    status = rig.session.perform('status')
    devices = rig.session.perform('devices')
    assert status['ok'] and status['logged_in'] and status['receiver_verified']
    assert status['preserved_gui_pids'] == [24832, 24840]
    assert status['capabilities']['native_uu_viewer'] is False
    assert status['phone_receiver_preserved'] is True
    assert status['incoming_phone_control_verified'] is None
    assert status['local_receiver_processes_preserved'] is True
    assert devices['devices'] == [{'device_name': '演示设备', 'online': True, 'platform': 'windows'}]
    assert 'private-id-2' not in json.dumps([status, devices])
    assert rig.endpoint.start_count == 0
    assert rig.state['factory'] == []
    assert_preserved(rig)


def test_connect_preserves_active_default_receiver_and_configures_only_owned_endpoint(rig):
    before = [copy.deepcopy(p.info) for p in rig.processes]
    result = connect(rig)
    assert result['connected'] and result['keyboard_ready']
    assert rig.endpoint.start_count == 1
    assert rig.state['factory'] == [(rig.session.cli, 'private-id-2')]
    assert rig.endpoint.calls[0][:2] == ('configure_capture', {'origin': rig.capture.public_origin})
    assert result['native_uu_viewer'] is False
    assert [p.info for p in rig.processes] == before
    connect(rig)
    assert rig.endpoint.start_count == 1
    assert len([c for c in rig.endpoint.calls if c[0] == 'configure_capture']) == 1
    assert_preserved(rig)


@pytest.mark.parametrize('change,error', [
    ('no_default', 'uu_default_receiver_required'),
    ('logged_out', 'uu_login_required'),
    ('no_cli', 'uu_executable_invalid'),
    ('no_bin', 'uu_executable_invalid'),
])
def test_connect_preconditions_never_start_or_touch_shared_processes(rig, change, error):
    if change == 'no_default':
        rig.state['default'] = set()
    elif change == 'logged_out':
        rig.state['logged_in'] = False
    elif change == 'no_cli':
        rig.session.cli.unlink()
    elif change == 'no_bin':
        rig.session.gui.unlink()
    result = rig.session.perform('connect', {'device_name': '演示设备'})
    assert result['error_code'] == error
    assert rig.endpoint.start_count == 0
    assert_preserved(rig)


@pytest.mark.parametrize('devices,error', [
    ([], 'uu_device_ambiguous'),
    ([{'deviceId': 'a', 'deviceName': '演示设备 extra', 'isOnline': True}], 'uu_device_ambiguous'),
    ([{'deviceId': 'a', 'deviceName': '演示设备', 'isOnline': False}], 'uu_device_offline'),
    ([{'deviceId': 'a', 'deviceName': '演示设备', 'isOnline': True},
      {'deviceId': 'b', 'deviceName': '演示设备', 'isOnline': True}], 'uu_device_ambiguous'),
])
def test_only_unique_exact_online_name_can_connect(rig, devices, error):
    rig.state['devices'] = devices
    result = rig.session.perform('connect', {'device_name': '演示设备'})
    assert result['error_code'] == error
    assert rig.endpoint.start_count == 0
    assert_preserved(rig)


def test_no_device_id_or_remote_command_or_capture_url_is_publicly_accepted(rig):
    for action, args in [('connect', {'device_id': 'private-id-2'}),
                         ('connect', {'device_name': '演示设备', 'command': 'unsafe'}),
                         ('screenshot', {'upload_url': 'https://caller.invalid'}),
                         ('configure_capture', {'origin': 'https://caller.invalid'}),
                         ('launch', {'command': 'unsafe'}),
                         ('keyboard_start', {'origin': 'https://caller.invalid'})]:
        result = rig.session.perform(action, args)
        assert result['ok'] is False
    assert rig.endpoint.calls == []
    assert rig.endpoint.start_count == 0
    assert_preserved(rig)


def test_capture_uses_only_one_use_trusted_ticket_and_returns_top_level_png(rig):
    connect(rig)
    result = snapshot(rig)
    assert result['capture'] == rig.capture.metadata
    assert result['remote_foreground'] == TARGET
    assert result['remote_bounds'] == BOUNDS
    assert result['capture_source'] == 'remote_user_desktop_graphics_proxy'
    assert [c for c in rig.endpoint.calls if c[0] == 'capture'][0][1] == rig.capture.ticket
    assert rig.capture.waited == ['ticket-1']
    assert rig.capture.cancelled == ['ticket-1']
    assert 'one-use-private-ticket' not in json.dumps(result)
    assert_preserved(rig)


def test_capture_accepts_adaptive_jpeg_with_final_bytes_metadata_and_original_geometry(rig):
    connect(rig)
    rig.capture.metadata.update(path=str(Path(rig.capture.metadata['path']).with_suffix('.jpg')),
                                mime_type='image/jpeg',size=230000)
    rig.endpoint.capture_receipt.update(mime_type='image/jpeg',format='jpeg',size_bytes=230000,
                                        width=BOUNDS['width'],height=BOUNDS['height'])
    result=snapshot(rig)
    assert result['capture']==rig.capture.metadata
    assert result['capture']['mime_type']=='image/jpeg'
    assert result['remote_bounds']==BOUNDS and result['remote_foreground']==TARGET
    assert rig.capture.cancelled==['ticket-1']
    assert rig.session._snapshot['target']=={key:TARGET[key] for key in ('hwnd','pid','creation_time_utc_ticks')}
    assert rig.session.SNAPSHOT_MAX_AGE==60
    assert_preserved(rig)


@pytest.mark.parametrize('invalid', ['receipt_mime','format','extension','size','size_bool',
                                   'metadata_size_bool','width','height','digest'])
def test_adaptive_capture_mismatch_never_authorizes_input_and_always_revokes_ticket(rig,invalid):
    connect(rig)
    rig.capture.metadata.update(path=str(Path(rig.capture.metadata['path']).with_suffix('.jpg')),
                                mime_type='image/jpeg',size=230000)
    rig.endpoint.capture_receipt.update(mime_type='image/jpeg',format='jpeg',size_bytes=230000,
                                        width=BOUNDS['width'],height=BOUNDS['height'])
    if invalid=='receipt_mime':rig.endpoint.capture_receipt['mime_type']='image/png'
    elif invalid=='format':rig.endpoint.capture_receipt['format']='png'
    elif invalid=='extension':rig.capture.metadata['path']=str(Path(rig.capture.metadata['path']).with_suffix('.png'))
    elif invalid=='size':rig.endpoint.capture_receipt['size_bytes']-=1
    elif invalid=='size_bool':rig.endpoint.capture_receipt['size_bytes']=True
    elif invalid=='metadata_size_bool':
        rig.capture.metadata['size']=True
        rig.endpoint.capture_receipt['size_bytes']=1
    elif invalid=='width':rig.endpoint.capture_receipt['width']-=1
    elif invalid=='height':rig.endpoint.capture_receipt['height']=True
    else:rig.endpoint.capture_receipt['sha256']='f'*64
    result=rig.session.perform('screenshot')
    assert not result['ok'] and result['error_code']=='uu_remote_capture_invalid'
    assert rig.capture.cancelled==['ticket-1'] and rig.session._snapshot is None
    assert rig.session.perform('type',{'text':'x'})['error_code']=='uu_remote_snapshot_required'
    assert_preserved(rig)


def test_capture_waits_sixty_seconds_without_changing_other_endpoint_timeouts(rig):
    timeouts=[]
    original=rig.endpoint.request
    def observe(action,args,request_id=None,timeout=10):
        timeouts.append((action,timeout))
        return original(action,args,request_id=request_id,timeout=timeout)
    rig.endpoint.request=observe
    connect(rig)
    snapshot(rig)
    assert ('capture',60) in timeouts
    assert all(timeout==15 for action,timeout in timeouts if action!='capture')


@pytest.mark.parametrize('invalid', ['id', 'bounds', 'dimensions', 'mime', 'digest'])
def test_invalid_capture_never_creates_an_input_target_and_cancels_ticket(rig, invalid):
    connect(rig)
    if invalid == 'id':
        rig.endpoint.capture_receipt['capture_id'] = 'different-ticket'
    elif invalid == 'bounds':
        rig.endpoint.capture_receipt['remote_bounds']['width'] = -1
    elif invalid == 'dimensions':
        rig.capture.metadata['width'] -= 1
    elif invalid == 'mime':
        rig.capture.metadata['mime_type'] = 'image/jpeg'
    elif invalid == 'digest':
        rig.capture.metadata['sha256'] = '1' * 64
    result = rig.session.perform('screenshot')
    assert result['error_code'] == 'uu_remote_capture_invalid'
    assert rig.capture.cancelled == ['ticket-1']
    assert rig.session.perform('type', {'text': 'x'})['error_code'] == 'uu_remote_snapshot_required'


@pytest.mark.parametrize('action,args,expected', [
    ('click', {'x': 10, 'y': 20}, {'x': -1910, 'y': -80, 'button': 'left', 'count': 1}),
    ('scroll', {'x': 0, 'y': 0, 'vertical': -120}, {'x': -1920, 'y': -100, 'vertical': -120, 'horizontal': 0}),
    ('drag', {'x': 10, 'y': 20, 'to_x': 3000, 'to_y': 800, 'duration_ms': 50},
     {'x': -1910, 'y': -80, 'to_x': 1080, 'to_y': 700, 'duration_ms': 50}),
])
def test_mouse_coordinates_map_physical_virtual_bounds_and_clear_target(rig, action, args, expected):
    connect(rig)
    snapshot(rig)
    result = rig.session.perform(action, {**args, 'operation_id': 'stable-mouse'})
    assert result['ok'], result
    call = [c for c in rig.endpoint.calls if c[0] == action][-1]
    assert call == (action, {**expected, 'bounds': BOUNDS}, 'stable-mouse:0')
    assert result['remote_effect_verified'] is False and result['verification_required']
    assert rig.session.perform('key', {'keys': 'CTRL+A'})['error_code'] == 'uu_remote_snapshot_required'
    assert_preserved(rig)


@pytest.mark.parametrize('action,args', [
    ('click', {'x': -1, 'y': 0}), ('click', {'x': 3840, 'y': 0}),
    ('click', {'x': 0, 'y': 1180}), ('click', {'x': 0.5, 'y': 0}),
    ('click', {'x': True, 'y': 0}), ('click', {'x': 1, 'y': 1, 'count': 100}),
    ('click', {'x': 1, 'y': 1, 'button': 'x1'}),
    ('click', {'x': 1, 'y': 1, 'button': []}),
    ('click', {'x': 1, 'y': 1, 'button': {}}),
    ('click', {'x': 1, 'y': 1, 'bounds': BOUNDS}),
    ('scroll', {'x': 1, 'y': 1, 'vertical': 0}),
    ('scroll', {'x': 1, 'y': 1, 'vertical': float('nan')}),
    ('drag', {'x': 1, 'y': 1, 'to_x': 20, 'to_y': 20, 'duration_ms': 0}),
    ('drag', {'x': 1, 'y': 1, 'to_x': 20, 'to_y': 20, 'steps': 10}),
])
def test_invalid_mouse_schema_is_rejected_before_remote_input(rig, action, args):
    connect(rig)
    snapshot(rig)
    assert rig.session.perform(action, args)['ok'] is False
    assert not any(c[0] in {'click', 'scroll', 'drag'} for c in rig.endpoint.calls)
    assert_preserved(rig)


def test_keyboard_normalizes_before_1000_character_chunks_and_uses_exact_target(rig):
    connect(rig)
    snapshot(rig)
    text = 'a' * 999 + '\r\n' + '测试🙂'
    result = rig.session.perform('type', {'text': text, 'operation_id': 'text-op'})
    assert result['ok'], result
    calls = [c for c in rig.endpoint.calls if c[0] == 'type']
    assert [c[1]['text'] for c in calls] == ['a' * 999 + '\n', '测试🙂']
    assert [c[2] for c in calls] == ['text-op:0', 'text-op:1']
    identity = {k: TARGET[k] for k in ('hwnd', 'pid', 'creation_time_utc_ticks')}
    assert all(c[1]['expect'] == identity and c[1]['activate'] is True for c in calls)
    repeated = rig.session.perform('type', {'text': text, 'operation_id': 'text-op'})
    assert repeated == result
    assert len([c for c in rig.endpoint.calls if c[0] == 'type']) == 2
    assert rig.session.perform('type', {'text': 'different', 'operation_id': 'text-op'})['error_code'] == 'uu_operation_id_conflict'
    assert_preserved(rig)


def test_cached_mouse_receipt_does_not_repeat_click_without_snapshot(rig):
    connect(rig)
    snapshot(rig)
    args = {'x': 1, 'y': 2, 'operation_id': 'one-click'}
    first = rig.session.perform('click', args)
    assert first['ok']
    assert rig.session.perform('click', args) == first
    assert len([c for c in rig.endpoint.calls if c[0] == 'click']) == 1


@pytest.mark.parametrize('args', [
    {'text': '\0'}, {'text': ''}, {'text': '\ud800'},
    {'text': 'x', 'activate': 1},
    {'text': 'x', 'expected_target': {**TARGET, 'pid': 999}},
    {'text': 'x', 'operation_id': 'not allowed'},
    {'text': 'x', 'command': 'unsafe'},
])
def test_invalid_text_and_identity_are_not_sent(rig, args):
    connect(rig)
    snapshot(rig)
    assert not rig.session.perform('type', args)['ok']
    assert not any(c[0] == 'type' for c in rig.endpoint.calls)


@pytest.mark.parametrize('keys', ['CTRL+ALT+DELETE', 'CTRL+CTRL+A', 'not-a-key', [], ['A'] * 6])
def test_invalid_keys_never_reach_remote(rig, keys):
    connect(rig)
    snapshot(rig)
    assert not rig.session.perform('key', {'keys': keys})['ok']
    assert not any(c[0] == 'key' for c in rig.endpoint.calls)


def test_valid_chord_activates_only_remote_exact_app(rig):
    connect(rig)
    snapshot(rig)
    result = rig.session.perform('key', {'keys': 'CTRL+A', 'operation_id': 'select'})
    assert result['ok'], result
    call = [c for c in rig.endpoint.calls if c[0] == 'key'][-1]
    assert call[1] == {'keys': ['CTRL', 'A'], 'expect': {k: TARGET[k] for k in ('hwnd', 'pid', 'creation_time_utc_ticks')}, 'activate': True}
    assert_preserved(rig)


def test_stale_image_or_replaced_receiver_prevents_input(rig, monkeypatch):
    connect(rig)
    snapshot(rig)
    rig.session._snapshot['at'] -= 61
    assert rig.session.perform('click', {'x': 1, 'y': 1})['error_code'] == 'uu_remote_snapshot_required'
    snapshot(rig)
    rig.processes[0].info['create_time'] += 1
    result = rig.session.perform('type', {'text': 'x'})
    assert result['error_code'] == 'uu_receiver_identity_changed'
    assert not any(c[0] in {'type', 'click'} for c in rig.endpoint.calls)
    assert_preserved(rig)


@pytest.mark.parametrize('action', ['close', 'disconnect'])
def test_cleanup_only_closes_exact_temporary_endpoint_not_app_or_device(rig, action):
    connect(rig)
    snapshot(rig)
    before = [copy.deepcopy(p.info) for p in rig.processes]
    result = rig.session.perform(action)
    assert result['ok'] and result['cleanup_complete']
    assert result['shared_receiver_closed'] is False
    assert result['device_disconnected'] is False
    assert rig.endpoint.close_count == 1
    assert [p.info for p in rig.processes] == before
    assert rig.session.perform(action)['cleanup_complete']
    assert rig.endpoint.close_count == 1
    assert_preserved(rig)


def test_incomplete_cleanup_retains_exact_endpoint_and_never_reconnects(rig):
    connect(rig)
    rig.endpoint.cleanup_complete = False
    result = rig.session.perform('close')
    assert not result['ok'] and not result['cleanup_complete']
    assert rig.session._endpoint is rig.endpoint
    result = rig.session.perform('connect', {'device_name': '演示设备'})
    assert result['error_code'] == 'uu_endpoint_cleanup_required'
    assert rig.endpoint.start_count == 1
    rig.endpoint.cleanup_complete = True
    assert rig.session.perform('close')['cleanup_complete']
    assert rig.endpoint.close_count == 2
    assert rig.session._endpoint is None
    assert_preserved(rig)


def test_cleanup_does_not_require_or_touch_current_receiver(rig):
    connect(rig)
    rig.state['default'] = set()
    before = len(rig.state['cli'])
    assert rig.session.perform('close')['cleanup_complete']
    assert len(rig.state['cli']) == before
    assert rig.endpoint.close_count == 1
    assert_preserved(rig)


def test_remote_timeout_returns_stable_id_and_retry_reuses_exact_request(rig):
    from web_tools.uu_remote_keyboard import RemoteKeyboardError
    connect(rig)
    snapshot(rig)
    args = {'x': 10, 'y': 20, 'operation_id': 'timeout-click'}
    rig.endpoint.failure = ('click', RemoteKeyboardError('uu_remote_timeout', 'Receipt timeout', may_have_run=True, request_id='timeout-click:0'))
    first = rig.session.perform('click', args)
    assert first['error_code'] == 'uu_remote_timeout'
    assert first['action_may_have_run'] and first['operation_id'] == 'timeout-click'
    snapshot(rig)
    blocked = rig.session.perform('click', {'x': 30, 'y': 40, 'operation_id': 'different-click'})
    assert blocked['error_code'] == 'uu_remote_outcome_unknown'
    assert blocked['pending_operation_ids'] == ['timeout-click']
    assert len([c for c in rig.endpoint.calls if c[0] == 'click']) == 1
    rig.endpoint.failure = None
    second = rig.session.perform('click', args)
    assert second['ok']
    calls = [c for c in rig.endpoint.calls if c[0] == 'click']
    assert calls[0] == calls[1]
    assert_preserved(rig)


def test_partial_text_retry_keeps_successful_chunks_and_polls_same_unknown_request(rig):
    from web_tools.uu_remote_keyboard import RemoteKeyboardError
    connect(rig)
    snapshot(rig)
    original = rig.endpoint.request
    seen_second = False

    def fail_second(action, args, request_id=None, timeout=10):
        nonlocal seen_second
        if action == 'type' and request_id == 'long-text:1' and not seen_second:
            seen_second = True
            rig.endpoint.calls.append((action, copy.deepcopy(args), request_id))
            raise RemoteKeyboardError('uu_remote_input_timeout', 'Unknown receipt', may_have_run=True, request_id=request_id)
        return original(action, args, request_id=request_id, timeout=timeout)

    rig.endpoint.request = fail_second
    args = {'text': 'A' * 1000 + 'B' * 1000 + 'C', 'operation_id': 'long-text'}
    assert rig.session.perform('type', args)['action_may_have_run']
    assert rig.session.perform('type', args)['ok']
    calls = [c for c in rig.endpoint.calls if c[0] == 'type']
    assert [c[2] for c in calls] == ['long-text:0', 'long-text:1', 'long-text:1', 'long-text:2']
    assert calls[1] == calls[2]


def test_endpoint_start_failure_exposes_only_fixed_code_and_safe_stage(rig):
    from web_tools.uu_remote_keyboard import RemoteKeyboardError

    def start(timeout=45):
        raise RemoteKeyboardError('uu_remote_runtime_timeout', 'secret token and terminal raw bytes', stage='read_ready', may_have_run=True)

    rig.endpoint.start = start
    result = rig.session.perform('connect', {'device_name': '演示设备'})
    assert result['error_code'] == 'uu_remote_runtime_timeout'
    assert result['stage'] == 'read_ready'
    assert result['phase'] == 'remote_endpoint_start'
    assert result['action_may_have_run'] is True
    assert 'secret token' not in json.dumps(result)
    assert rig.session._endpoint is rig.endpoint
    assert rig.session.perform('close')['cleanup_complete']
    assert_preserved(rig)


@pytest.mark.parametrize('method,exception,code,phase', [
    ('issue', ValueError('secret local token'), 'uu_capture_ticket_failed', 'capture_ticket'),
    ('wait', TimeoutError('secret URL'), 'uu_capture_upload_timeout', 'capture_upload_wait'),
    ('wait', RuntimeError('secret raw HTTP body'), 'uu_capture_upload_failed', 'capture_upload_wait'),
])
def test_capture_failures_have_fixed_diagnostics_without_ticket_or_raw_http(rig, method, exception, code, phase):
    connect(rig)
    def fail(*args):
        raise exception
    setattr(rig.capture, method, fail)
    result = rig.session.perform('screenshot')
    assert result['error_code'] == code
    assert result['phase'] == phase
    assert 'secret' not in json.dumps(result)
    assert rig.capture.cancelled == ([] if method == 'issue' else ['ticket-1'])


@pytest.mark.parametrize('updates', [
    {'width': 8193}, {'left': -32769}, {'top': 32768},
    {'width': 8192, 'height': 8192},
])
def test_remote_capture_bounds_match_protocol_limits(rig, updates):
    connect(rig)
    rig.endpoint.capture_receipt['remote_bounds'].update(updates)
    assert rig.session.perform('screenshot')['error_code'] == 'uu_remote_capture_invalid'


def test_host_uu_route_constructs_only_shared_proxy_without_legacy_handoff_or_gui_launch(rig, monkeypatch):
    from web_tools import background_desktop_host as host_module
    from web_tools import background_uu, uu_client_handoff, uu_remote_keyboard
    from web_tools import remote_gui_capture_client

    def forbidden(*args, **kwargs):
        raise AssertionError('No legacy GUI launch/handoff is permitted')

    monkeypatch.setattr(background_uu, 'UUBackgroundSession', forbidden)
    monkeypatch.setattr(uu_client_handoff, 'prepare_idle_client', forbidden)
    monkeypatch.setattr(host_module, '_create_private_process', forbidden)
    monkeypatch.setattr(remote_gui_capture_client, 'RemoteCaptureClient', lambda root: rig.capture)
    constructed = []
    def endpoint_factory(cli, device_id, **kwargs):
        constructed.append((cli, device_id, kwargs))
        return rig.endpoint
    monkeypatch.setattr(uu_remote_keyboard, 'RemoteKeyboardSession', endpoint_factory)
    actor = host_module.BackgroundDesktopHost(data_root=rig.session.data_root,
        allowed_apps={'uu': rig.session.executable})
    monkeypatch.setattr(actor, '_windows', lambda: [])
    monkeypatch.setattr(actor, '_run_uu_cli', rig.session._cli_runner)
    try:
        result = actor._dispatch('uu', {'action': 'connect', 'arguments': {'device_name': '演示设备'}})
        assert result['ok'], result
        assert isinstance(actor._uu_session, module.SharedUUGuiSession)
        assert constructed[0][:2] == (rig.session.cli, 'private-id-2')
        assert callable(constructed[0][2]['process_factory'])
        assert actor._dispatch('uu', {'action': 'screenshot'})['capture'] == rig.capture.metadata
        result = actor._dispatch('uu', {'action': 'close'})
        assert result['cleanup_complete']
        assert rig.endpoint.close_count == 1
        assert_preserved(rig)
    finally:
        actor.close()


def arrange_replacement(rig):
    replacement = FakeEndpoint()
    replacements = []
    def factory(cli, device_id):
        replacements.append((cli, device_id))
        return replacement
    rig.session._endpoint_factory = factory
    return replacement, replacements


def stopped_error():
    from web_tools.uu_remote_keyboard import RemoteKeyboardError
    return RemoteKeyboardError('uu_remote_helper_stopped', 'The idle helper ended')


def test_explicit_keyboard_start_probes_healthy_existing_helper_without_replacement(rig):
    connect(rig)
    replacement, replacements = arrange_replacement(rig)
    before = len(rig.endpoint.calls)
    result = rig.session.perform('keyboard_start')
    assert result['ok'] and result['endpoint_recovered'] is False
    assert rig.endpoint.calls[before:] == [('status', {}, None)]
    assert rig.endpoint.close_count == 0
    assert replacements == []
    assert replacement.start_count == 0
    assert_preserved(rig)


@pytest.mark.parametrize('first_action', ['keyboard_start', 'keyboard_status', 'screenshot'])
def test_idle_stop_requires_explicit_recovery_and_keeps_exact_device_and_receiver(rig, first_action):
    connect(rig)
    snapshot(rig)
    replacement, replacements = arrange_replacement(rig)
    rig.endpoint.failure = ('status' if first_action != 'screenshot' else 'capture', stopped_error())
    first = rig.session.perform(first_action)
    if first_action != 'keyboard_start':
        assert first['error_code'] == 'uu_remote_helper_stopped'
        assert rig.session._ready is False
        assert rig.session._snapshot is None
        assert rig.endpoint.close_count == 0 and replacements == []
        # Ordinary requests and connect never silently restart an idle helper.
        assert rig.session.perform('screenshot')['error_code'] == 'uu_remote_helper_stopped'
        assert not rig.session.perform('connect', {'device_name': '演示设备'})['ok']
        assert rig.endpoint.close_count == 0 and replacements == []
        rig.endpoint.failure = ('status', stopped_error())
        first = rig.session.perform('keyboard_start')
    assert first['ok'], first
    assert first['endpoint_recovered'] and first['fresh_screenshot_required']
    assert rig.endpoint.close_count == 1
    assert replacements == [(rig.session.cli, 'private-id-2')]
    assert replacement.start_count == 1
    assert replacement.calls[0] == ('configure_capture', {'origin': rig.capture.public_origin}, None)
    assert not any(c[0] in rig.session.INPUT_ACTIONS for c in replacement.calls)
    assert rig.session._target_name == '演示设备'
    assert rig.session.perform('type', {'text': 'new'})['error_code'] == 'uu_remote_snapshot_required'
    assert_preserved(rig)


@pytest.mark.parametrize('code', ['uu_remote_input_timeout', 'uu_terminal_exited', 'uu_remote_protocol_invalid'])
def test_nonconclusive_status_failure_never_replaces_or_closes_endpoint(rig, code):
    from web_tools.uu_remote_keyboard import RemoteKeyboardError
    connect(rig)
    replacement, replacements = arrange_replacement(rig)
    rig.endpoint.failure = ('status', RemoteKeyboardError(code, 'No stopped proof', may_have_run=True))
    result = rig.session.perform('keyboard_start')
    assert result['error_code'] == code
    assert rig.session._endpoint is rig.endpoint
    assert rig.endpoint.close_count == 0
    assert replacements == [] and replacement.start_count == 0
    assert_preserved(rig)


def test_unknown_input_blocks_even_explicit_idle_stop_recovery(rig):
    from web_tools.uu_remote_keyboard import RemoteKeyboardError
    connect(rig)
    snapshot(rig)
    replacement, replacements = arrange_replacement(rig)
    rig.endpoint.failure = ('key', RemoteKeyboardError('uu_remote_input_timeout', 'Unknown input', may_have_run=True))
    assert rig.session.perform('key', {'keys': 'CTRL+A', 'operation_id': 'uncertain-input'})['action_may_have_run']
    rig.endpoint.failure = ('status', stopped_error())
    result = rig.session.perform('keyboard_start')
    assert result['error_code'] == 'uu_remote_outcome_unknown'
    assert result['pending_operation_ids'] == ['uncertain-input']
    assert rig.session._ready is False and rig.session._helper_stopped
    assert rig.session._operations['uncertain-input']['uncertain'] is True
    assert rig.session._endpoint is rig.endpoint
    assert rig.endpoint.close_count == 0
    assert replacement.start_count == 0 and replacements == []
    assert_preserved(rig)


def test_transport_unknown_write_without_shared_record_also_blocks_recovery(rig):
    connect(rig)
    replacement, replacements = arrange_replacement(rig)
    rig.endpoint._requests = {'wire-op:0': {'action': 'click', 'reply': None}}
    rig.endpoint.failure = ('status', stopped_error())
    result = rig.session.perform('keyboard_start')
    assert result['error_code'] == 'uu_remote_outcome_unknown'
    assert result['pending_operation_ids'] == ['wire-op:0']
    assert rig.endpoint.close_count == 0
    assert replacements == [] and replacement.start_count == 0


def test_idle_recovery_requires_complete_exact_cleanup_and_can_retry_it(rig):
    from web_tools.uu_remote_keyboard import RemoteKeyboardError
    connect(rig)
    replacement, replacements = arrange_replacement(rig)
    rig.endpoint.failure = ('status', stopped_error())
    rig.endpoint.cleanup_complete = False
    first = rig.session.perform('keyboard_start')
    assert first['error_code'] == 'uu_endpoint_cleanup_pending'
    assert rig.endpoint.close_count == 1
    assert rig.session._endpoint is rig.endpoint
    assert replacements == [] and replacement.start_count == 0
    assert rig.session._target_name == '演示设备'
    # close() may have closed the old local channel before remote deletion was
    # confirmed. Retry the same exact cleanup using the prior stopped proof.
    rig.endpoint.failure = ('status', RemoteKeyboardError('uu_remote_input_not_ready', 'Closed old channel'))
    rig.endpoint.cleanup_complete = True
    result = rig.session.perform('keyboard_start')
    assert result['ok'] and result['endpoint_recovered']
    assert rig.endpoint.close_count == 2
    assert replacements == [(rig.session.cli, 'private-id-2')]
    assert replacement.start_count == 1
    assert_preserved(rig)


def test_idle_recovery_cleanup_exception_keeps_exact_endpoint_and_no_replacement(rig):
    connect(rig)
    replacement, replacements = arrange_replacement(rig)
    rig.endpoint.failure = ('status', stopped_error())
    def fail_close():
        rig.endpoint.close_count += 1
        raise RuntimeError('secret raw terminal bytes')
    rig.endpoint.close = fail_close
    result = rig.session.perform('keyboard_start')
    assert result['error_code'] == 'uu_endpoint_cleanup_pending'
    assert 'secret' not in json.dumps(result)
    assert rig.session._endpoint is rig.endpoint
    assert replacements == [] and replacement.start_count == 0
    assert_preserved(rig)


def test_stopped_partial_input_is_retired_and_never_replayed_on_new_endpoint(rig):
    connect(rig)
    snapshot(rig)
    replacement, replacements = arrange_replacement(rig)
    original = rig.endpoint.request
    def stop_second(action, args, request_id=None, timeout=10):
        if action == 'type' and request_id == 'partial-op:1':
            rig.endpoint.calls.append((action, copy.deepcopy(args), request_id))
            rig.endpoint.failure = ('status', stopped_error())
            raise stopped_error()
        return original(action, args, request_id=request_id, timeout=timeout)
    rig.endpoint.request = stop_second
    args = {'text': 'A' * 1000 + 'B', 'operation_id': 'partial-op'}
    result = rig.session.perform('type', args)
    assert result['error_code'] == 'uu_remote_helper_stopped'
    assert len(rig.session._operations['partial-op']['receipts']) == 1
    assert rig.session.perform('keyboard_start')['endpoint_recovered']
    snapshot(rig)
    result = rig.session.perform('type', args)
    assert result['error_code'] == 'uu_operation_endpoint_retired'
    assert result['delivered_chunks'] == 1
    assert not any(c[0] == 'type' for c in replacement.calls)
    assert rig.session.perform('type', {'text': 'new operation', 'operation_id': 'new-op'})['ok']
    assert_preserved(rig)


def test_completed_input_returns_cached_receipt_after_recovery_without_replay(rig):
    connect(rig)
    snapshot(rig)
    args = {'keys': 'CTRL+A', 'operation_id': 'completed-op'}
    before = rig.session.perform('key', args)
    assert before['ok']
    replacement, replacements = arrange_replacement(rig)
    rig.endpoint.failure = ('status', stopped_error())
    assert rig.session.perform('keyboard_start')['endpoint_recovered']
    assert rig.session.perform('key', args) == before
    assert not any(c[0] == 'key' for c in replacement.calls)
    assert_preserved(rig)


def test_live_incomplete_start_is_not_mistaken_for_idle_stop(rig):
    from web_tools.uu_remote_keyboard import RemoteKeyboardError
    rig.endpoint.failure = ('configure_capture', RemoteKeyboardError('uu_remote_input_timeout', 'No configure receipt', may_have_run=True))
    assert not rig.session.perform('connect', {'device_name': '演示设备'})['ok']
    replacement, replacements = arrange_replacement(rig)
    rig.endpoint.failure = None
    result = rig.session.perform('keyboard_start')
    assert result['error_code'] == 'uu_endpoint_cleanup_required'
    assert rig.endpoint.close_count == 0
    assert replacements == [] and replacement.start_count == 0
