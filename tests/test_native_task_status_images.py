"""Recovered native captures cross the facade and direct MCP image boundary."""
import base64
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from web_mcp import mcp_server
from web_tools.unified_browser import UnifiedBrowserService, result


PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jC1sAAAAASUVORK5CYII=')
TASK_ID = 'xr-42-15'


def capture_receipt(path, action='screenshot'):
    window = action == 'screenshot_window'
    return {'verified': True, 'adapter': 'native_ipc', 'recovered_from_task_status': True,
            'native_task_succeeded': True, 'native_terminal': True, 'native_state': 'succeeded',
            'native_task_id': TASK_ID, 'native_task': {'taskId': TASK_ID, 'state': 'succeeded',
                'action': action, 'tabId': 4, 'errorCode': None},
            'path': str(path), 'mime_type': 'image/png', 'size_bytes': len(PNG),
            'screenshot_mode': 'window' if window else 'page',
            'pixel_scope': 'native_window' if window else 'page'}


def artifact(receipt):
    return {**{key: receipt[key] for key in ('path', 'mime_type', 'size_bytes')}, 'private': True}


async def host_call(monkeypatch, root, invoke):
    registry = SimpleNamespace(chatgpt_direct=True, ctx=SimpleNamespace(settings=SimpleNamespace(data_root=root)),
                               invoke=invoke)
    monkeypatch.setattr(mcp_server, '_registry', registry)
    return await mcp_server._direct_public_tool(mcp_server.browser_control)(
        'native_fixture', 'task_status', arguments={'task_id': TASK_ID})


async def facade_host_call(monkeypatch, root, receipt, *, adapter='native_ipc'):
    calls = []

    async def discover():
        return {'browsers': [{'browser_id': 'native_fixture', 'connection_generation': 'generation1',
                             'adapter': adapter, 'supported_actions': ['task_status'], 'connected': True}]}

    async def dispatch(browser, generation, method, arguments, timeout):
        calls.append((method, arguments))
        assert method == 'task_status', 'Recovery must never dispatch a new capture or select a tab'
        return deepcopy(receipt)

    service = UnifiedBrowserService(root / 'receipts.db', discover, dispatch)

    async def invoke(name, arguments):
        assert name == 'browser_control'
        return await service.control(**{key: value for key, value in arguments.items() if key != '__mcp_ctx'})

    return await host_call(monkeypatch, root, invoke), calls


@pytest.mark.parametrize('action,mode,scope', [('screenshot', 'page', 'page'),
                                             ('screenshot_window', 'window', 'native_window')])
async def test_late_success_surfaces_original_task_private_artifact_and_host_png(tmp_path, monkeypatch, action, mode, scope):
    path = tmp_path / 'browser_screenshots' / 'late.png'
    path.parent.mkdir()
    path.write_bytes(PNG)
    receipt = capture_receipt(path, action)
    reply, calls = await facade_host_call(monkeypatch, tmp_path, receipt)
    assert not reply.isError and reply.structuredContent['action'] == 'task_status'
    assert reply.structuredContent['artifacts'] == [artifact(receipt)]
    recovered = reply.structuredContent['result']['data']['result']
    assert recovered['native_task_id'] == TASK_ID
    assert (recovered['screenshot_mode'], recovered['pixel_scope']) == (mode, scope)
    images = [entry for entry in reply.content if entry.type == 'image']
    assert len(images) == 1 and images[0].mimeType == 'image/png'
    assert base64.b64decode(images[0].data) == PNG
    assert len(calls) == 1 and calls[0][0] == 'task_status' and calls[0][1]['task_id'] == TASK_ID
    assert base64.b64encode(PNG).decode() not in json.dumps(reply.structuredContent)


@pytest.mark.parametrize('changes', [
    {'adapter': 'extension'}, {'recovered_from_task_status': False}, {'recovered_from_task_status': 1},
    {'verified': False}, {'native_task_succeeded': False}, {'native_task_succeeded': 1},
    {'native_terminal': False}, {'native_state': 'running'}, {'native_state': 'failed'},
    {'native_state': 'timed_out'}, {'ok': False}, {'error': {'code': 'timeout'}},
    {'native_task': None}, {'native_task_id': 'xr-42-other'}, {'mime_type': 'image/jpeg'},
    {'size_bytes': True}, {'size_bytes': 0}, {'size_bytes': 16 * 1024 * 1024 + 1},
    {'screenshot_mode': 'window'}, {'pixel_scope': 'native_window'},
    {'path': ''},
    {'capture_pixels_available': False}, {'capture_pixels_available': 1},
])
async def test_incomplete_or_contradictory_capture_markers_never_become_artifacts(tmp_path, monkeypatch, changes):
    receipt = capture_receipt(tmp_path / 'browser_screenshots' / 'late.png')
    receipt.update(changes)
    reply, calls = await facade_host_call(monkeypatch, tmp_path, receipt)
    assert not reply.structuredContent['artifacts']
    assert all(entry.type != 'image' for entry in reply.content)
    assert len(calls) == 1


@pytest.mark.parametrize('changes', [
    {'state': 'running'}, {'state': 'failed'}, {'state': 'timed_out'}, {'errorCode': 'timeout'},
    {'error': 'failure'}, {'action': 'read_page'}, {'action': 'screenshot_window'}, {'action': {}},
    {'taskId': 'xr-42-other'}, {'id': 'xr-42-other'}, {'task_id': 'xr-42-other'},
])
async def test_failed_noncapture_and_conflicting_native_tasks_never_become_artifacts(tmp_path, monkeypatch, changes):
    receipt = capture_receipt(tmp_path / 'browser_screenshots' / 'late.png')
    receipt['native_task'].update(changes)
    reply, _ = await facade_host_call(monkeypatch, tmp_path, receipt)
    assert not reply.structuredContent['artifacts']
    assert all(entry.type != 'image' for entry in reply.content)


async def test_receipt_for_another_requested_task_and_extension_adapter_have_no_artifact(tmp_path, monkeypatch):
    receipt = capture_receipt(tmp_path / 'browser_screenshots' / 'late.png')
    receipt.update(native_task_id='xr-42-other')
    receipt['native_task']['taskId'] = 'xr-42-other'
    reply, _ = await facade_host_call(monkeypatch, tmp_path, receipt)
    assert not reply.structuredContent['artifacts']
    receipt = capture_receipt(tmp_path / 'browser_screenshots' / 'late.png')
    reply, _ = await facade_host_call(monkeypatch, tmp_path, receipt, adapter='extension')
    assert not reply.structuredContent['artifacts']


@pytest.mark.parametrize('case', ['outside', 'traversal', 'missing', 'oversized', 'actual_size',
                                  'artifact_path', 'artifact_mime', 'artifact_size', 'not_private',
                                  'unverified', 'running', 'noncapture', 'no_recovery_marker'])
async def test_host_rechecks_capture_evidence_metadata_and_private_path_guards(tmp_path, monkeypatch, case):
    private = tmp_path / 'browser_screenshots'
    private.mkdir()
    allowed = private / 'late.png'
    allowed.write_bytes(PNG)
    receipt = capture_receipt(allowed)
    if case in {'outside', 'traversal'}:
        outside = tmp_path / 'outside.png'
        outside.write_bytes(PNG)
        receipt['path'] = str(outside if case == 'outside' else private / '..' / 'outside.png')
    elif case == 'missing':
        receipt['path'] = str(private / 'missing.png')
    elif case == 'oversized':
        with allowed.open('wb') as stream:
            stream.truncate(12 * 1024 * 1024 + 1)
        receipt['size_bytes'] = allowed.stat().st_size
    elif case == 'actual_size':
        receipt['size_bytes'] += 1
    elif case == 'noncapture':
        receipt['native_task']['action'] = 'read_page'
    elif case == 'no_recovery_marker':
        receipt.pop('recovered_from_task_status')
    value = result('native_fixture', None, 'task_status', data=receipt, executed=True, verified=True)
    value['artifacts'] = [artifact(receipt)]
    if case.startswith('artifact_'):
        key = case.removeprefix('artifact_')
        field = {'path': 'path', 'mime': 'mime_type', 'size': 'size_bytes'}[key]
        value['artifacts'][0][field] = {'path': str(private / 'other.png'), 'mime': 'image/jpeg', 'size': 1}[key]
    elif case == 'not_private':
        value['artifacts'][0]['private'] = False
    elif case == 'unverified':
        value['evidence']['verified'] = False
    elif case == 'running':
        value['status'] = 'running'

    async def invoke(name, arguments):
        return value

    reply = await host_call(monkeypatch, tmp_path, invoke)
    assert all(entry.type != 'image' for entry in reply.content)


async def test_metadata_only_completed_task_does_not_emit_host_pixels(tmp_path, monkeypatch):
    receipt = {'verified': True, 'native_task_succeeded': True, 'native_terminal': True,
               'native_state': 'succeeded', 'native_task_id': TASK_ID,
               'native_task': {'taskId': TASK_ID, 'action': 'screenshot', 'state': 'succeeded'},
               'capture_pixels_available': False}
    reply, calls = await facade_host_call(monkeypatch, tmp_path, receipt)
    assert not reply.isError and not reply.structuredContent['artifacts']
    assert all(entry.type != 'image' for entry in reply.content) and len(calls) == 1
