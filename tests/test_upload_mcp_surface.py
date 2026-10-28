from dataclasses import replace
import json

from fastapi.testclient import TestClient


def rpc(client, method, params=None):
    response = client.post('/mcp', headers={
        'Authorization': 'Bearer pytest-token',
        'Accept': 'application/json, text/event-stream',
        'Host': '127.0.0.1:48271',
    }, json={'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params or {}})
    assert response.status_code == 200
    if response.headers.get('content-type', '').startswith('text/event-stream'):
        return [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: ')][-1]['result']
    return response.json()['result']


def test_upload_surface_auth_schema_and_realistic_selection_contract(isolated_settings, monkeypatch):
    from web_mcp.server import create_app
    from web_tools.browser_client import BrowserBridgeClient
    calls = []

    async def select(self, **kwargs):
        calls.append(kwargs)
        return {'ok': True, 'status': 'selected', 'stages': {
            'file_selected': True, 'upload_started': None,
            'server_received': None, 'platform_processed': None},
            'selected_file': {'name': 'example.mp4', 'size': 1024}}

    monkeypatch.setattr(BrowserBridgeClient, 'upload_file', select)
    settings = replace(isolated_settings, orchestration_mode='chatgpt_direct', oauth_enabled=False, admin_mode=True)
    with TestClient(create_app(settings)) as client:
        assert client.post('/mcp', json={}).status_code == 401
        tools = {x['name']: x for x in rpc(client, 'tools/list')['tools']}
        upload = tools['browser_upload_file']
        assert {'tab_id', 'selector', 'file_path', 'idempotency_key'} <= set(upload['inputSchema']['required'])
        assert not upload['annotations']['readOnlyHint']
        assert tools['browser_upload_status']['annotations']['readOnlyHint']
        assert not tools['video_project_path']['annotations']['readOnlyHint']
        args = {'tab_id': '17', 'selector': 'input[type="file"][data-xiaoruo1-upload-id="observed-id"]',
                'file_path': r'C:\safe-fixture\video.mp4',
                'idempotency_key': 'mcp-surface-test'}
        refused = rpc(client, 'tools/call', {'name': 'browser_upload_file', 'arguments': args})
        assert refused['isError'] and not calls
        args['confirm'] = True
        selected = rpc(client, 'tools/call', {'name': 'browser_upload_file', 'arguments': args})['structuredContent']
        assert selected['ok'] and selected['stages']['file_selected'] is True
        assert selected['stages']['server_received'] is None
        assert selected['stages']['platform_processed'] is None
        assert calls == [{**args, 'timeout': 30000}]
        project = rpc(client, 'tools/call', {'name': 'video_project_path', 'arguments': {'date': '2026-10-27'}})['structuredContent']
        assert project['ok'] and project['result']['data']['path'].endswith(r'2026_10\27')


def test_upload_admin_boundary(isolated_settings, monkeypatch):
    from web_mcp.server import create_app
    from web_tools.browser_client import BrowserBridgeClient

    async def unexpected(*args, **kwargs):
        raise AssertionError('Unprivileged upload must never reach the bridge')

    monkeypatch.setattr(BrowserBridgeClient, 'upload_file', unexpected)
    settings = replace(isolated_settings, orchestration_mode='chatgpt_direct', oauth_enabled=False, admin_mode=False)
    with TestClient(create_app(settings)) as client:
        response = rpc(client, 'tools/call', {'name': 'browser_upload_file', 'arguments': {
            'tab_id': '17', 'selector': 'observed', 'file_path': 'example.mp4',
            'idempotency_key': 'test-admin-boundary', 'confirm': True}})
        assert response['isError'] and not response['structuredContent']['ok']