import pytest
from web_tools.browser_bridge import EdgeExtensionBridge, BrowserBridgeError
from web_tools.unified_browser import UnifiedBrowserService


async def test_missing_explicit_tab_never_falls_back_to_other_browser():
    bridge = EdgeExtensionBridge()
    bridge._websocket = object()
    bridge._connection_id = 'other-browser'
    bridge._connections['other-browser'] = {'websocket': bridge._websocket}

    async def list_tabs():
        return {'tabs': []}

    bridge.list_tabs = list_tabs
    with pytest.raises(BrowserBridgeError, match='absent'):
        await bridge.call('click', {'tab_id': 'expired-edge-tab'})
    with pytest.raises(BrowserBridgeError, match='expired'):
        await bridge.call('read_page', {}, _connection_id='expired-connection')


@pytest.mark.parametrize('scope', [{'frame_id': 12}, {'shadow_path': ['#host']}])
async def test_legacy_cef_cannot_silently_ignore_scope(tmp_path, scope):
    calls = []

    async def discover():
        return {'browsers': [{'browser_id': 'browser_manual', 'browser_kind': 'xiaoruo_browser',
            'version': '1.8.4', 'connection_generation': 'generation1', 'connected': True,
            'supported_actions': ['read']}]}

    async def dispatch(*args):
        calls.append(args)
        return {'text': 'wrong main frame'}

    service = UnifiedBrowserService(tmp_path / 'receipts.db', discover, dispatch)
    value = await service.control('browser_manual', 'read', '17', scope)
    assert not value['ok'] and value['error']['code'] == 'browser_scope_unsupported'
    assert not calls