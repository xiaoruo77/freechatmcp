from dataclasses import replace
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest

from test_native_v73_mcp_surface import invoke


async def test_background_session_check_does_not_claim_foreground_lease(monkeypatch):
    from web_tools.browser_bridge import EdgeExtensionBridge
    from web_tools import windows_mcp

    def forbidden(*args, **kwargs):
        raise AssertionError('Background DOM calls must not construct a Windows-MCP input client')

    monkeypatch.setattr(windows_mcp, 'WindowsMCPBridge', forbidden)
    monkeypatch.setattr(windows_mcp, 'interactive_desktop_state', lambda: {
        'available': True, 'session_id': 9, 'user_input_active': True})
    state = await EdgeExtensionBridge().native_background_preflight()
    assert state['unlocked'] and state['session_active'] and not state['secure_desktop']


@pytest.mark.parametrize('code', ['desktop_locked_or_secure', 'interactive_session_required', 'desktop_session_inactive'])
async def test_background_check_never_bypasses_secure_desktop(monkeypatch, code):
    from web_tools.browser_bridge import EdgeExtensionBridge
    from web_tools.unified_browser import BrowserControlError
    from web_tools import windows_mcp
    monkeypatch.setattr(windows_mcp, 'interactive_desktop_state', lambda: {'available': False, 'error_code': code})
    with pytest.raises(BrowserControlError) as error:
        await EdgeExtensionBridge().native_background_preflight()
    assert error.value.code == 'foreground_required'


async def test_default_foreground_preflight_still_refuses_user_takeover(monkeypatch):
    from web_tools.browser_bridge import EdgeExtensionBridge
    from web_tools.browser_upload import UploadError
    from web_tools import windows_mcp
    calls = []

    class Desktop:
        def __init__(self, settings):
            pass

        async def call(self, name, arguments):
            calls.append(name)
            return {'ok': True, 'result': {'data': {'state': 'user'}}}

        async def close(self):
            calls.append('close')

    monkeypatch.setattr(windows_mcp, 'interactive_desktop_state', lambda: {'available': True})
    monkeypatch.setattr(windows_mcp, 'WindowsMCPBridge', Desktop)
    with pytest.raises(UploadError) as error:
        await EdgeExtensionBridge().upload_desktop_preflight()
    assert error.value.code == 'upload_desktop_control_unavailable'
    assert calls == ['ControlStatus', 'close']


@pytest.mark.parametrize('admin', [True, False])
def test_formal_cached_host_keeps_background_mode_and_account_gate(isolated_settings, monkeypatch, admin):
    from web_mcp.server import create_app
    from web_mcp.tool_registry import ToolRegistry
    from web_tools.browser_client import BrowserBridgeClient
    calls = []

    async def forbidden(*args, **kwargs):
        raise AssertionError('No runner or Sampling for a background browser tool')

    async def control(self, **kwargs):
        calls.append(kwargs)
        return {'ok': True, 'status': 'completed', 'background_execution': {'mode': 'native_background'}}

    monkeypatch.setattr(ToolRegistry, '_autonomous_sample', forbidden)
    monkeypatch.setattr(ToolRegistry, '_run_python_script', forbidden)
    monkeypatch.setattr(BrowserBridgeClient, 'browser_control', control)
    settings = replace(isolated_settings, orchestration_mode='chatgpt_direct', oauth_enabled=False, admin_mode=admin)
    args = {'browser_id': 'native_fixture', 'tab_id': '5', 'action': 'type',
            'arguments': {'selector': '#observed', 'text': 'background',
                          'expected_generation': 'generation1', 'execution_mode': 'native_background'},
            'operation_id': 'background-surface-type', 'confirm': True, 'timeout': 10000}
    with TestClient(create_app(settings)) as client:
        result = invoke(client, 'run_workflow', {'workflow': {'steps': [
            {'tool': 'browser_control', 'arguments': args}]}})
    assert bool(result['ok']) is admin
    assert calls == [args] if admin else calls == []
