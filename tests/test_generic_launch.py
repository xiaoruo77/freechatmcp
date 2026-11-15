from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import sys
import pytest
from web_tools.generic_launch import prepare_application,check_user_instance,GenericLaunchError


def test_prepare_preserves_literal_arguments_without_shell(tmp_path):
    exe=tmp_path/'Custom Editor.exe';exe.touch()
    item=prepare_application({'executable':str(exe),'argv':['a b','$(not-a-shell)','&literal']})
    assert item['argv']==[str(exe),'a b','$(not-a-shell)','&literal']
    assert item['background_compatibility']=='unverified'


def test_catalog_launch_uses_parsed_shortcut_argv(tmp_path,monkeypatch):
    exe=tmp_path/'MyApp.exe';exe.touch()
    monkeypatch.setattr('web_tools.installed_apps.discover_installed_apps',lambda **k:[{
        'app_id':'app-one','name':'My App','executable':str(exe),'arguments':'--name "two words"',
        'argv':['--name','two words'],'cwd':str(tmp_path),'source':'start_menu'}])
    item=prepare_application({'app':'app-one'})
    assert item['argv']==[str(exe),'--name','two words']


@pytest.mark.parametrize('args',[{'executable':'relative.exe'}, {'executable':'https://example.com/app.exe'}, {'app':'missing'}])
def test_invalid_or_ambiguous_targets_do_not_launch(tmp_path,monkeypatch,args):
    monkeypatch.setattr('web_tools.installed_apps.discover_installed_apps',lambda **k:[])
    with pytest.raises(GenericLaunchError):prepare_application(args)


def test_existing_user_window_is_refused_before_process_creation(tmp_path,monkeypatch):
    exe=tmp_path/'User App.exe'
    monkeypatch.setattr('web_tools.background_lifecycle._default_window_pids',lambda:{44})
    monkeypatch.setitem(sys.modules,'psutil',SimpleNamespace(Process=lambda p:SimpleNamespace(exe=lambda:str(exe)),Error=OSError))
    with pytest.raises(GenericLaunchError) as exc:check_user_instance(exe)
    assert exc.value.code=='existing_user_instance_conflict'


def test_new_process_is_suspended_until_job_is_assigned(monkeypatch):
    from web_tools.background_desktop_host import _create_private_process
    events=[]
    handle=SimpleNamespace(Close=lambda:events.append('close'))
    def create(*args):
        assert args[5]&4
        assert args[-1].lpDesktop=='WinSta0\\Xiaoruo1Background'
        events.append('create_suspended');return handle,handle,77,88
    monkeypatch.setitem(sys.modules,'win32process',SimpleNamespace(STARTUPINFO=SimpleNamespace,
        CreateProcess=create,ResumeThread=lambda h:events.append('resume'),TerminateProcess=lambda *a:events.append('terminate')))
    job=SimpleNamespace(Close=lambda:events.append('job_close'))
    monkeypatch.setitem(sys.modules,'win32job',SimpleNamespace(CreateJobObject=lambda *a:job,
        JobObjectBasicUIRestrictions=4,SetInformationJobObject=lambda *a:events.append('restrict'),
        AssignProcessToJobObject=lambda *a:events.append('assign')))
    monkeypatch.setattr('web_tools.generic_launch.ui_restrictions',lambda:223)
    _create_private_process(Path('C:/Test.exe'),['C:/Test.exe'],desktop='WinSta0\\Xiaoruo1Background',environment={},cwd=Path('C:/'),isolated_job=True)
    assert events[:4]==['create_suspended','restrict','assign','resume']


def test_job_failure_does_not_resume_new_process(monkeypatch):
    from web_tools.background_desktop_host import _create_private_process,BackgroundDesktopError
    events=[]
    handle=SimpleNamespace(Close=lambda:None)
    monkeypatch.setitem(sys.modules,'win32process',SimpleNamespace(STARTUPINFO=SimpleNamespace,
        CreateProcess=lambda *a:(handle,handle,77,88),ResumeThread=lambda h:events.append('resume'),TerminateProcess=lambda *a:events.append('terminate')))
    monkeypatch.setitem(sys.modules,'win32job',SimpleNamespace(CreateJobObject=lambda *a:handle,
        JobObjectBasicUIRestrictions=4,SetInformationJobObject=lambda *a:None,
        AssignProcessToJobObject=Mock(side_effect=OSError('denied'))))
    monkeypatch.setattr('web_tools.generic_launch.ui_restrictions',lambda:223)
    with pytest.raises(BackgroundDesktopError):
        _create_private_process(Path('C:/Test.exe'),['C:/Test.exe'],desktop='WinSta0\\Xiaoruo1Background',environment={},cwd=Path('C:/'),isolated_job=True)
    assert events==['terminate']


def test_exited_wrapper_waits_for_surviving_job_child_window(tmp_path,monkeypatch):
    import json
    from web_tools import background_desktop_host as module

    wrapper=tmp_path/'Launcher.exe';wrapper.touch()
    child=tmp_path/'Gui.exe';child.touch()
    clock=[0.0]
    monkeypatch.setattr(module.time,'monotonic',lambda:clock[0])
    monkeypatch.setattr(module.time,'sleep',lambda seconds:clock.__setitem__(0,clock[0]+seconds))
    monkeypatch.setattr('web_tools.generic_launch.check_user_instance',lambda exe:None)
    process=SimpleNamespace(pid=11,poll=lambda:0,process_ids=lambda:{22})
    monkeypatch.setattr(module,'_create_private_process',lambda *a,**k:process)
    monkeypatch.setitem(sys.modules,'psutil',SimpleNamespace(
        Process=lambda pid:SimpleNamespace(create_time=lambda:123.0,exe=lambda:str(child)),Error=OSError))
    actor=module.BackgroundDesktopHost(data_root=tmp_path,allowed_apps={})
    window={'hwnd':99,'pid':22,'title':'Child GUI','visible':True,'desktop':actor.desktop_name}
    actor._windows=lambda:[window] if clock[0]>=3 else []

    result=actor._launch_generic_app({'executable':str(wrapper),'timeout':5})

    assert result['ok'] is True
    assert result['windows']==[window]
    assert result['launcher_exit_code']==0
    assert 3<=clock[0]<5
    records=json.loads((tmp_path/'background_desktop'/'owned-apps.json').read_text())
    assert records[0]['pid']==22 and records[0]['executable']==str(child)


def test_exited_wrapper_with_empty_job_does_not_wait_for_launch_timeout(tmp_path,monkeypatch):
    from web_tools import background_desktop_host as module

    wrapper=tmp_path/'Launcher.exe';wrapper.touch()
    clock=[0.0]
    monkeypatch.setattr(module.time,'monotonic',lambda:clock[0])
    monkeypatch.setattr(module.time,'sleep',lambda seconds:clock.__setitem__(0,clock[0]+seconds))
    monkeypatch.setattr('web_tools.generic_launch.check_user_instance',lambda exe:None)
    process=SimpleNamespace(pid=11,poll=lambda:0,process_ids=lambda:set())
    monkeypatch.setattr(module,'_create_private_process',lambda *a,**k:process)
    actor=module.BackgroundDesktopHost(data_root=tmp_path,allowed_apps={})
    actor._windows=lambda:[]

    result=actor._launch_generic_app({'executable':str(wrapper),'timeout':45})

    assert result['ok'] is False
    assert result['error_code']=='private_app_window_unavailable'
    assert clock[0]<1
