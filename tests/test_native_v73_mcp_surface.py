"""Official MCP entry points, including hosts with a cached older tool list."""
from dataclasses import replace
import json

from fastapi.testclient import TestClient
import pytest


def rpc(client, method, params=None):
    response = client.post('/mcp', headers={
        'Authorization': 'Bearer pytest-token', 'Accept': 'application/json, text/event-stream',
        'Host': '127.0.0.1:48271',
    }, json={'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params or {}})
    assert response.status_code == 200
    if response.headers.get('content-type', '').startswith('text/event-stream'):
        return [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: ')][-1]['result']
    return response.json()['result']


def invoke(client, name, args):
    return rpc(client, 'tools/call', {'name': name, 'arguments': args})['structuredContent']


@pytest.mark.parametrize('admin', [True, False])
def test_cached_host_uses_real_registry_and_keeps_admin_gate(isolated_settings, monkeypatch, admin):
    from web_mcp.server import create_app
    from web_mcp.tool_registry import ToolRegistry
    from web_tools.browser_client import BrowserBridgeClient
    calls = []

    async def forbidden(*args, **kwargs):
        raise AssertionError('No Sampling, Python runner, or hidden supervisor is allowed')

    async def control(self, **kwargs):
        calls.append(kwargs)
        return {'ok': True, 'status': 'completed', 'stages': {
            'file_selected': True, 'server_received': None, 'platform_processed': None}}

    monkeypatch.setattr(ToolRegistry, '_autonomous_sample', forbidden)
    monkeypatch.setattr(ToolRegistry, '_run_python_script', forbidden)
    monkeypatch.setattr(BrowserBridgeClient, 'browser_control', control)
    settings = replace(isolated_settings, orchestration_mode='chatgpt_direct', oauth_enabled=False, admin_mode=admin)
    with TestClient(create_app(settings)) as client:
        assert client.post('/mcp', json={}).status_code == 401
        tools = {x['name']: x for x in rpc(client, 'tools/list')['tools']}
        assert {'run_workflow', 'browser_control', 'browser_instances', 'get_system_info'} <= tools.keys()
        assert 'upload_file' in tools['browser_control']['inputSchema']['properties']['action']['enum']
        assert not tools['browser_control']['annotations']['readOnlyHint']
        args = {'browser_id': 'native_fixture', 'tab_id': '5', 'action': 'upload_file',
                'arguments': {'selector': '#observed-cover-input', 'file_path': 'allowlisted/cover.jpg',
                              'expected_generation': 'fixture-generation'},
                'operation_id': 'v73-surface-upload', 'confirm': True, 'timeout': 20000}
        reply = invoke(client, 'run_workflow', {'workflow': {'steps': [
            {'tool': 'browser_control', 'arguments': args}]}, 'timeout_seconds': 25})
        if admin:
            assert reply['ok'] and calls == [args]
            selected = reply['steps'][0]['result']['stages']
            assert selected['file_selected'] and selected['server_received'] is None
        else:
            assert not reply['ok'] and calls == []


def test_cached_host_discovery_project_path_and_access_instructions(isolated_settings, monkeypatch):
    from web_mcp.server import create_app
    from web_tools.browser_client import BrowserBridgeClient
    seen = []

    async def inventory(self, timeout=10000):
        seen.append(timeout)
        return {'ok': True, 'browsers': [{'browser_id': 'native_fixture'}]}

    monkeypatch.setattr(BrowserBridgeClient, 'browser_instances', inventory)
    settings = replace(isolated_settings, orchestration_mode='chatgpt_direct', oauth_enabled=False, admin_mode=False)
    with TestClient(create_app(settings)) as client:
        result = invoke(client, 'run_workflow', {'workflow': {'steps': [
            {'tool': 'browser_instances', 'arguments': {'timeout': 5000}},
            {'tool': 'video_project_path', 'arguments': {'date': '2026-10-27', 'create': False}},
        ]}})
        assert result['ok'] and seen == [5000]
        assert result['steps'][1]['result']['path'].replace(chr(92), '/').endswith('2026_10/27')
        info = invoke(client, 'get_system_info', {})['browser_mcp_access']
        assert info['cached_host_entrypoint'] == 'run_workflow'
        assert info['native_upload_action'] == 'upload_file'


async def test_inventory_preserves_safe_discovery_diagnostics(monkeypatch):
    from web_tools.browser_bridge import EdgeExtensionBridge
    bridge = EdgeExtensionBridge()

    class Native:
        async def native_instances(self, timeout):
            return {'browsers': [], 'native_discovery_diagnostics': [{'code': 'discovery_access_denied'}]}

    monkeypatch.setattr(bridge, 'native_browser_adapter', lambda: Native())
    result = await bridge.browser_instances()
    assert result['native_discovery_diagnostics'] == [{'code': 'discovery_access_denied'}]
