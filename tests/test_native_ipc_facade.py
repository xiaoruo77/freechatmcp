import pytest
from web_tools.unified_browser import UnifiedBrowserService


@pytest.mark.parametrize('state,terminal,expected', [('failed', True, 'failed'), ('timed_out', True, 'failed'), ('running', False, 'outcome_unknown')])
async def test_native_failure_and_task_identity_are_preserved(tmp_path, state, terminal, expected):
    async def discover():
        return {'browsers': [{'browser_id': 'native_fixture', 'connection_generation': 'generation1',
            'adapter': 'native_ipc', 'supported_actions': ['click'], 'connected': True}]}

    async def dispatch(browser, generation, method, args, timeout):
        if method == 'list_tabs':
            return {'tabs': [{'tab_id': '17', 'url': 'https://example.com/'}]}
        return {'ok': False, 'native_terminal': terminal, 'native_state': state,
            'native_task_id': 'xr-42-1', 'error': {'code': 'unsupported' if terminal else 'timeout', 'message': 'Native controlled error'}}

    service = UnifiedBrowserService(tmp_path / 'receipts.db', discover, dispatch)
    value = await service.control('native_fixture', 'click', '17',
        {'selector': '#button', 'expected_generation': 'generation1'}, 'operation-fixture1', True)
    assert not value['ok'] and value['status'] == expected
    assert value['native_task_id'] == 'xr-42-1'
    assert value['error']['code'] == ('unsupported' if terminal else 'timeout')
    assert service.receipts.pending(service.resource('native_fixture', '17')) is None if terminal else value['status'] == 'outcome_unknown'


async def test_native_first_inventory_and_explicit_dispatch_do_not_change_edge(monkeypatch):
    from web_tools.browser_bridge import EdgeExtensionBridge
    bridge = EdgeExtensionBridge()

    class Native:
        async def native_instances(self, timeout):
            return {'browsers': [{'browser_id': 'native_fixture', 'native_pid': 42, 'adapter': 'native_ipc'}]}

        async def native_dispatch(self, browser, generation, method, payload, timeout):
            return {'native': True, 'browser': browser, 'method': method}

    monkeypatch.setattr(bridge, 'native_browser_adapter', lambda: Native())
    bridge._connections['edge-generation'] = {'instance_id': 'edge-fixture', 'browser_kind': 'edge', 'browser_family': 'edge'}
    inventory = await bridge.browser_instances()
    assert inventory['browsers'][0]['browser_id'] == 'native_fixture'
    assert inventory['browsers'][1]['browser_kind'] == 'edge'
    native = await bridge.browser_dispatch('native_fixture', 'generation1', 'list_tabs', {}, 1)
    assert native['native']
    calls = []

    async def extension(method, payload, timeout, _connection_id=None):
        calls.append(_connection_id)
        return {'edge': True}

    monkeypatch.setattr(bridge, 'call', extension)
    edge = await bridge.browser_dispatch('browser_edge-fixture', 'edge-generation', 'list_tabs', {}, 1)
    assert edge['edge'] and calls == ['edge-generation']


async def test_native_screenshot_surfaces_private_artifact_for_host_image(tmp_path):
    async def discover():
        return {'browsers': [{'browser_id': 'native_fixture', 'connection_generation': 'generation1',
            'adapter': 'native_ipc', 'supported_actions': ['screenshot'], 'connected': True}]}

    async def dispatch(browser, generation, method, args, timeout):
        if method == 'list_tabs':
            return {'tabs': [{'tab_id': '17', 'url': 'https://example.com/'}]}
        return {'path': str(tmp_path / 'private.png'), 'mime_type': 'image/png', 'size_bytes': 128,
                'verified': True, 'native_state': 'succeeded'}

    service = UnifiedBrowserService(tmp_path / 'receipts.db', discover, dispatch)
    value = await service.control('native_fixture', 'screenshot', '17')
    assert value['ok'] and value['artifacts'][0]['private'] is True
    assert value['artifacts'][0]['path'] == str(tmp_path / 'private.png')


def test_envelope_preserves_native_unsupported_error_code():
    from web_mcp.result_contract import normalize_result
    response = normalize_result('browser_control', {'ok': False, 'status': 'failed',
        'error': {'code': 'unsupported', 'message': 'Lightpanda page pixels are unavailable'}})
    assert response['error_code'] == 'unsupported' and response['error']['code'] == 'unsupported'