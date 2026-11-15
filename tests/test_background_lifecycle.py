import json
from pathlib import Path
from types import SimpleNamespace
import sys
import pytest
from web_tools import background_lifecycle as lifecycle


def test_cleanup_never_terminates_descendant_with_user_desktop_window(tmp_path,monkeypatch):
    terminated=[];messages=[]
    exe=tmp_path/'Code.exe'
    class Process:
        def __init__(self,pid):self.pid=pid
        def exe(self):return str(exe)
        def create_time(self):return self.pid*100
        def children(self,recursive=True):return [Process(22),Process(33)]
        def is_running(self):return True
        def terminate(self):terminated.append(self.pid)
    fake=SimpleNamespace(Process=Process,Error=RuntimeError,wait_procs=lambda *a,**k:([],[]))
    monkeypatch.setitem(sys.modules,'psutil',fake)
    monkeypatch.setitem(sys.modules,'win32gui',SimpleNamespace(
        PostMessage=lambda h,*a:messages.append(h),IsWindow=lambda h:False,IsWindowVisible=lambda h:False))
    monkeypatch.setitem(sys.modules,'win32con',SimpleNamespace(WM_CLOSE=16))
    monkeypatch.setattr(lifecycle,'_default_window_pids',lambda:{22})
    result=lifecycle.cleanup_private_apps([{'hwnd':100,'pid':11,'visible':True,'title':'Private Code'}],{'vscode':exe},data_root=tmp_path)
    assert result['cleanup_complete'] is True
    assert terminated==[33,11] and 22 not in terminated
    assert messages==[100]


def test_cleanup_refuses_app_that_also_owns_main_desktop_window(tmp_path,monkeypatch):
    exe=tmp_path/'Code.exe'
    monkeypatch.setattr(lifecycle,'_default_window_pids',lambda:{11})
    fake=SimpleNamespace(Process=lambda p:SimpleNamespace(pid=p,exe=lambda:str(exe)),Error=OSError)
    monkeypatch.setitem(sys.modules,'psutil',fake)
    monkeypatch.setitem(sys.modules,'win32gui',SimpleNamespace(PostMessage=lambda *a:pytest.fail('must not send input/close')))
    monkeypatch.setitem(sys.modules,'win32con',SimpleNamespace(WM_CLOSE=16))
    with pytest.raises(RuntimeError,match='user-desktop'):
        lifecycle.cleanup_private_apps([{'hwnd':100,'pid':11,'visible':True,'title':'Shared app'}],{'vscode':exe},data_root=tmp_path)


@pytest.mark.parametrize('desktop_name',['Xiaoruo1Background','Xiaoruo1Background_uu_probe-1'])
def test_cleanup_restores_exact_owned_headless_job_on_selected_desktop(tmp_path,monkeypatch,desktop_name):
    terminated=[]
    exe=tmp_path/'Gui.exe'
    process=SimpleNamespace(pid=11,exe=lambda:str(exe),create_time=lambda:123.0,
        children=lambda **k:[],is_running=lambda:True,terminate=lambda:terminated.append(11))
    monkeypatch.setitem(sys.modules,'psutil',SimpleNamespace(Process=lambda pid:process,Error=OSError,wait_procs=lambda *a,**k:([],[])))
    monkeypatch.setitem(sys.modules,'win32gui',SimpleNamespace(PostMessage=lambda *a:pytest.fail('headless job has no window')))
    monkeypatch.setitem(sys.modules,'win32con',SimpleNamespace(WM_CLOSE=16))
    monkeypatch.setattr(lifecycle,'_default_window_pids',lambda:set())
    owned=tmp_path/'background_desktop'/'owned-apps.json';owned.parent.mkdir()
    owned.write_text(json.dumps([{'pid':11,'create_time':123.0,'executable':str(exe),
        'desktop':desktop_name,'origin':'generic_createprocess_job'}]))

    result=lifecycle.cleanup_private_apps([],{},data_root=tmp_path,desktop_name=desktop_name)

    assert result['cleanup_complete'] is True
    assert result['closed_pids']==terminated==[11]


@pytest.mark.parametrize('record_desktop',[
    'Xiaoruo1Background','Xiaoruo1Background_other','Default',
    'Xiaoruo1Background_','Xiaoruo1Background_bad/name',
])
def test_cleanup_does_not_restore_another_desktops_job(tmp_path,monkeypatch,record_desktop):
    monkeypatch.setitem(sys.modules,'psutil',SimpleNamespace(
        Process=lambda pid:pytest.fail('foreign desktop record must not be inspected'),Error=OSError))
    monkeypatch.setitem(sys.modules,'win32gui',SimpleNamespace())
    monkeypatch.setitem(sys.modules,'win32con',SimpleNamespace(WM_CLOSE=16))
    monkeypatch.setattr(lifecycle,'_default_window_pids',lambda:set())
    owned=tmp_path/'background_desktop'/'owned-apps.json';owned.parent.mkdir()
    owned.write_text(json.dumps([{'pid':11,'create_time':123.0,'executable':str(tmp_path/'Gui.exe'),
        'desktop':record_desktop,'origin':'generic_createprocess_job'}]))

    result=lifecycle.cleanup_private_apps([],{},data_root=tmp_path,desktop_name='Xiaoruo1Background_current')

    assert result['cleanup_complete'] is True
    assert result['closed_pids']==[]


@pytest.mark.parametrize('desktop_name',['Default','Xiaoruo1Background_','Xiaoruo1Background_bad/name'])
def test_cleanup_rejects_invalid_private_desktop_name(tmp_path,desktop_name):
    with pytest.raises(ValueError,match='private'):
        lifecycle.cleanup_private_apps([],{},data_root=tmp_path,desktop_name=desktop_name)
