from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
import threading

import pytest

from web_tools.background_desktop_host import BackgroundDesktopHost
from test_background_desktop_host import FakeDesktopAPI


def test_uu_routes_on_actor_and_reuses_the_shared_receiver_session(tmp_path, monkeypatch):
    from web_tools import uu_shared_gui, remote_gui_capture_client
    api = FakeDesktopAPI()
    calls = []
    actions = []
    capture = object()

    class Session:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            calls.append(self)

        def perform(self, action, arguments):
            actions.append((action, arguments, threading.get_ident()))
            return {'ok': True, 'shared_receiver_preserved': True}

    monkeypatch.setattr(uu_shared_gui, 'SharedUUGuiSession', Session)
    monkeypatch.setattr(remote_gui_capture_client, 'RemoteCaptureClient', lambda _: capture)
    actor = BackgroundDesktopHost(data_root=tmp_path, allowed_apps={'uu': tmp_path/'GameViewer.exe'}, _api_factory=lambda: api)
    unrelated = SimpleNamespace(process_ids=lambda: {123})
    actor._launched.append(unrelated)

    def launch(args):
        pytest.fail('The shared UU receiver must not be relaunched on the private desktop')

    actor._launch_generic_app = launch
    try:
        actor.execute('create')
        result = actor.execute('uu', {'action': 'connect', 'arguments': {'device_name': 'test'}})
        assert result['ok']
        assert result['shared_receiver_preserved']
        assert actor.execute('uu', {'action': 'status'})['shared_receiver_preserved']
        assert len(calls) == 1
        assert calls[0].kwargs['data_root'] == tmp_path.resolve()
        assert calls[0].kwargs['executable'] == (tmp_path/'GameViewer.exe').resolve()
        assert calls[0].kwargs['cli_runner'] == actor._run_uu_cli
        assert calls[0].kwargs['capture_client'] is capture
        assert callable(calls[0].kwargs['endpoint_factory'])
        assert [action for action, _, _ in actions] == ['connect', 'status']
        assert len({thread for _, _, thread in actions}) == 1
        assert actions[0][2] != threading.get_ident()
        assert actor._launched == [unrelated]
    finally:
        actor._launched.clear()
        actor.close()


def test_cleanup_does_not_report_success_when_uu_disconnect_failed(tmp_path):
    api = FakeDesktopAPI()
    actor = BackgroundDesktopHost(data_root=tmp_path, allowed_apps={}, _api_factory=lambda: api)
    actor._uu_session = SimpleNamespace(perform=lambda *a: {'ok': False, 'error_code': 'uu_disconnect_pending'})
    try:
        actor.execute('create')
        result = actor.execute('cleanup')
        assert result['ok'] is False
        assert result['cleanup_complete'] is False
        assert result['error_code'] == 'uu_disconnect_pending'
    finally:
        actor.close()


@pytest.mark.parametrize('admin,confirmed', [(False, True), (True, False)])
async def test_uu_control_keeps_existing_authorization_requirement(monkeypatch, admin, confirmed):
    from web_mcp.direct_tools import DirectTools
    call = AsyncMock()
    monkeypatch.setattr('web_mcp.direct_tools.call_background', call)
    owner = SimpleNamespace(ctx=SimpleNamespace(settings=SimpleNamespace(admin_mode=admin)))
    with pytest.raises(PermissionError):
        await DirectTools._background_desktop(owner, 'uu', {'action': 'connect', 'arguments': {'device_name': 'test'}}, confirmed)
    call.assert_not_awaited()


async def test_uu_status_does_not_require_control_authorization(monkeypatch):
    from web_mcp.direct_tools import DirectTools
    call = AsyncMock(return_value={'ok': True})
    monkeypatch.setattr('web_mcp.direct_tools.call_background', call)
    owner = SimpleNamespace(ctx=SimpleNamespace(settings=SimpleNamespace(admin_mode=False)))
    assert await DirectTools._background_desktop(owner, 'uu', {'action': 'status'}) == {'ok': True}
    call.assert_awaited_once()
