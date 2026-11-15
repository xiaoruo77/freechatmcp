from pathlib import Path
from types import SimpleNamespace
import sys

import pytest
from web_tools import background_lifecycle as lifecycle
from web_tools.background_desktop_host import BackgroundDesktopError, BackgroundDesktopHost


@pytest.mark.parametrize('name', ['GameViewer.exe','GameViewerLauncher.exe','GameViewerService.exe','GameViewerServer.exe','GameViewerHealthd.exe'])
def test_cleanup_preserves_receiver_even_in_owned_job_and_private_desktop(tmp_path, monkeypatch, name):
    import json
    receiver = tmp_path / name
    process = SimpleNamespace(pid=55, exe=lambda: str(receiver), create_time=lambda: 123,
        children=lambda **k: [], is_running=lambda: True,
        terminate=lambda: pytest.fail('shared receiver must survive task cleanup'))
    monkeypatch.setitem(sys.modules, 'psutil', SimpleNamespace(Process=lambda p: process, Error=OSError,
        wait_procs=lambda *a, **k: ([], [])))
    monkeypatch.setitem(sys.modules, 'win32gui', SimpleNamespace(
        PostMessage=lambda *a: pytest.fail('receiver must receive no WM_CLOSE'),
        IsWindow=lambda h: True, IsWindowVisible=lambda h: True))
    monkeypatch.setitem(sys.modules, 'win32con', SimpleNamespace(WM_CLOSE=16))
    monkeypatch.setattr(lifecycle, '_default_window_pids', lambda: set())
    owned = tmp_path/'background_desktop'/'owned-apps.json'
    owned.parent.mkdir()
    owned.write_text(json.dumps([dict(pid=55,create_time=123,executable=str(receiver),
        desktop='Xiaoruo1Background',origin='generic_createprocess_job')]))
    result=lifecycle.cleanup_private_apps([dict(hwnd=1,pid=55,visible=True,title='UU')],
        {'uu': receiver}, data_root=tmp_path)
    assert result['cleanup_complete'] and result['closed_pids'] == []


def test_cleanup_does_not_kill_receiver_child_of_owned_task(tmp_path,monkeypatch):
    closed=[]
    class Process:
        def __init__(self,pid): self.pid=pid
        def exe(self): return str(tmp_path/('Task.exe' if self.pid==10 else 'GameViewer.exe'))
        def create_time(self): return self.pid
        def children(self,**k): return [Process(20)]
        def is_running(self): return True
        def terminate(self): closed.append(self.pid)
    monkeypatch.setitem(sys.modules,'psutil',SimpleNamespace(Process=Process,Error=OSError,wait_procs=lambda *a,**k:([],[])))
    monkeypatch.setitem(sys.modules,'win32gui',SimpleNamespace(PostMessage=lambda *a:None,IsWindow=lambda h:False,IsWindowVisible=lambda h:False))
    monkeypatch.setitem(sys.modules,'win32con',SimpleNamespace(WM_CLOSE=16))
    monkeypatch.setattr(lifecycle,'_default_window_pids',lambda:set())
    result=lifecycle.cleanup_private_apps([dict(hwnd=1,pid=10,visible=True,title='Task')],{'task':tmp_path/'Task.exe'},data_root=tmp_path)
    assert result['cleanup_complete'] and closed==[10]


@pytest.mark.parametrize('exe',['GameViewer.exe','bin/GameViewer.exe','GameViewerLauncher.exe'])
def test_generic_launch_cannot_reintroduce_private_shared_uu(tmp_path,monkeypatch,exe):
    executable=tmp_path/exe
    monkeypatch.setattr('web_tools.generic_launch.prepare_application',lambda args:{'executable':executable})
    monkeypatch.setattr('web_tools.generic_launch.check_user_instance',lambda *a:pytest.fail('UU must be blocked before generic launch'))
    actor=BackgroundDesktopHost(data_root=tmp_path,allowed_apps={})
    with pytest.raises(BackgroundDesktopError,match='UU login and receiver'):
        actor._launch_generic_app({'executable':str(executable)})

