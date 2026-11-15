from pathlib import Path
import inspect
import pytest


def test_capture_rejects_empty_window_content(tmp_path,monkeypatch):
    # The production rejection path is exercised using a uniformly painted
    # bitmap, rather than accepting success merely because PrintWindow returned.
    from PIL import Image
    from web_tools.background_capture import validate_content
    image=Image.new('RGB',(120,100),'black')
    with pytest.raises(RuntimeError,match='blank/unrendered'):validate_content(image)


def test_main_desktop_hwnd_cannot_reach_capture(tmp_path,monkeypatch):
    from web_tools.background_desktop_host import BackgroundDesktopHost
    from test_background_desktop_host import FakeDesktopAPI
    actor=BackgroundDesktopHost(data_root=tmp_path,allowed_apps={},_api_factory=FakeDesktopAPI)
    called=[]
    monkeypatch.setattr('web_tools.background_capture.capture_window',lambda *a:called.append(a))
    try:
        actor.execute('create')
        result=actor.execute('screenshot',{'hwnd':123456789})
        assert result['ok'] is False
        assert not called
    finally:actor.close()


def test_capture_and_launcher_have_no_input_desktop_switch():
    import web_tools.background_capture as capture
    source=inspect.getsource(capture)
    assert 'SwitchDesktop(' not in source and 'SetForegroundWindow(' not in source
