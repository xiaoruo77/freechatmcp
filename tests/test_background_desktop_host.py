from pathlib import Path
import threading
from types import SimpleNamespace

import pytest

from web_tools.background_desktop_host import BackgroundDesktopHost


class FakeDesktopAPI:
    def __init__(self):
        self.calls = []
        self.owner_thread = None

    def input_state(self):
        return {"desktop": "Default", "foreground_hwnd": 42}

    def create_and_attach(self, name):
        self.owner_thread = threading.get_ident()
        self.calls.append(("attach", name))
        return 77

    def initialize_com(self):
        assert self.owner_thread == threading.get_ident()
        assert self.calls[-1][0] == "attach"
        self.calls.append(("COM",))

    def windows(self, handle, name):
        assert self.owner_thread == threading.get_ident()
        assert handle == 77
        return [{"hwnd": 99, "pid": 123, "title": "Private test window", "desktop": name, "visible": True}]

    def release_com(self):
        assert self.owner_thread == threading.get_ident()
        self.calls.append(("release_COM",))

    def close_handle(self, handle):
        self.calls.append(("close", handle))


def host(tmp_path):
    api = FakeDesktopAPI()
    actor = BackgroundDesktopHost(data_root=tmp_path, allowed_apps={}, _api_factory=lambda: api)
    return actor, api


@pytest.mark.parametrize("name", ["Default", "Winlogon", "ScreenSaver", "WinSta0\\Default", "Other"])
def test_user_and_secure_desktops_cannot_be_targeted(tmp_path, name):
    with pytest.raises(ValueError):
        BackgroundDesktopHost(data_root=tmp_path, allowed_apps={}, desktop_name=name)


@pytest.mark.parametrize("action,args", [
    ("click", {"x": 1, "y": 2}), ("type", {"text": "hi", "keys": "ENTER"}),
    ("shortcut", {"keys": "CTRL+S"}), ("screenshot", {}), ("clipboard", {}),
    ("launch_app", {"app": "powershell", "argv": ["secret"]}),
])
def test_unsupported_input_and_commands_never_start_actor(tmp_path, action, args):
    actor, api = host(tmp_path)
    try:
        assert not actor.execute(action, args)["ok"]
        assert not api.calls
        assert actor._thread is None
    finally:
        actor.close()


def test_status_is_read_only_and_actions_require_explicit_creation(tmp_path):
    actor, api = host(tmp_path)
    try:
        assert actor.execute("status")["created"] is False
        assert actor.execute("list_windows")["error_code"] == "background_desktop_not_created"
        assert not api.calls
    finally:
        actor.close()


@pytest.mark.parametrize('subaction,elapsed,ok',[
    ('screenshot',89,True),('screenshot',91,False),
    ('connect',99,True),('connect',101,False),('keyboard_start',99,True),
    ('type',61,False),('key',61,False),('click',61,False),('scroll',61,False),
    ('drag',61,False),('close',61,False),('status',61,False),
])
def test_only_uu_screenshot_actor_deadline_is_extended_to_ninety_seconds(tmp_path,monkeypatch,subaction,elapsed,ok):
    from web_tools import background_desktop_host as module
    actor,_=host(tmp_path)
    original_queue=None
    class Pending:
        calls=0
        def result(self,timeout):
            self.calls+=1
            if self.calls==1:raise module.FutureTimeout()
            return {'ok':True}
    pending=Pending()
    try:
        assert actor.execute('create')['ok']
        # No UU work enters the actor queue: only its bounded wait is tested.
        original_queue=actor._requests
        actor._requests=SimpleNamespace(put=lambda record:None)
        ticks=iter([100.0,100.0+elapsed])
        with monkeypatch.context() as patch:
            patch.setattr(module,'Future',lambda:pending)
            patch.setattr(module.time,'monotonic',lambda:next(ticks))
            result=actor.execute('uu',{'action':subaction,'arguments':{}})
        assert result['ok'] is ok
        if not ok:assert result['error_code']=='background_action_timeout'
    finally:
        if original_queue is not None:actor._requests=original_queue
        actor.close()


def test_actor_shutdown_closes_only_exact_remote_endpoint(tmp_path):
    actor, api = host(tmp_path)
    calls=[]
    assert actor.execute('create')['ok']
    actor._uu_session=SimpleNamespace(perform=lambda action,args:calls.append((action,args)) or {'ok':True})
    actor.close()
    assert calls==[('close',{})]
    assert api.calls[-2:]==[('release_COM',),('close',77)]


def test_actor_attaches_before_com_and_owns_window_enumeration(tmp_path):
    actor, api = host(tmp_path)
    main_thread = threading.get_ident()
    try:
        assert actor.execute("create")["ok"]
        result = actor.execute("list_windows")
        assert result["windows"][0]["desktop"] == "Xiaoruo1Background"
        assert api.owner_thread != main_thread
        assert result["input_evidence"]["private_desktop_was_input"] is False
        assert result["input_evidence"]["foreground_unchanged"] is True
    finally:
        actor.close()
    assert api.calls[-2:] == [("release_COM",), ("close", 77)]


def test_pattern_action_requires_a_fresh_actor_owned_snapshot(tmp_path):
    actor, _ = host(tmp_path)
    try:
        actor.execute("create")
        assert actor.execute("click", {"element_id": "foreign"})["error_code"] == "uia_target_stale"
    finally:
        actor.close()


def test_user_foreground_change_is_recorded_without_taking_it_back(tmp_path):
    actor, api = host(tmp_path)
    observations = iter([42, 43])
    api.input_state = lambda: {"desktop": "Default", "foreground_hwnd": next(observations, 43)}
    original = actor._dispatch

    def slow(action, args):
        import time
        time.sleep(0.15)
        return original(action, args)

    actor._dispatch = slow
    try:
        result = actor.execute("create")
        assert result["ok"]
        assert result["input_evidence"]["foreground_unchanged"] is False
        assert result["input_evidence"]["user_changes_are_not_failure"] is True
    finally:
        actor.close()


def test_launch_uses_only_private_desktop_and_new_profile(tmp_path, monkeypatch):
    from web_tools import background_desktop_host as module

    data = tmp_path / "08_web_data"
    extension = tmp_path / "10_web_deploy" / "vscode_background_extension"
    extension.mkdir(parents=True)
    (extension / "package.json").write_text("{}")
    config = tmp_path / "07_web_config" / "vscode_background.json"
    config.parent.mkdir()
    config.write_text("{}")
    code = tmp_path / "Code.exe"
    code.touch()
    calls = []

    def spawn(executable, argv, **options):
        calls.append((argv, options))
        return SimpleNamespace(pid=123, poll=lambda: None, close=lambda: None)

    monkeypatch.setattr(module, "_create_private_process", spawn)
    api = FakeDesktopAPI()
    actor = BackgroundDesktopHost(data_root=data, allowed_apps={"vscode": code}, _api_factory=lambda: api)
    try:
        actor.execute("create")
        assert actor.execute("launch_app", {"app": "vscode"})["ok"]
    finally:
        actor.close()
    argv, options = calls[0]
    assert options["desktop"] == "WinSta0\\Xiaoruo1Background"
    assert argv[argv.index("--user-data-dir") + 1] == str(data / "background_desktop" / "vscode-profile")
    assert argv[argv.index("--extensionDevelopmentPath") + 1] == str(extension)
    assert options["environment"]["XIAORUO1_VSCODE_BRIDGE_CONFIG"] == str(config)
    assert "--disable-extensions" not in argv


def test_real_startupinfo_desktop_field_is_passed_to_win32_createprocess(monkeypatch):
    import sys
    from web_tools.background_desktop_host import _create_private_process

    calls = []
    handle = SimpleNamespace(Close=lambda: None)

    def create(*args):
        calls.append(args)
        return handle, handle, 101, 102

    monkeypatch.setitem(sys.modules, "win32process", SimpleNamespace(
        STARTUPINFO=SimpleNamespace, CreateProcess=create))
    process = _create_private_process(Path("C:/app.exe"), ["C:/app.exe", "a b"],
        desktop="WinSta0\\Xiaoruo1Background", environment={}, cwd=Path("C:/"))
    assert process.pid == 101
    assert calls[0][-1].lpDesktop == "WinSta0\\Xiaoruo1Background"
    assert calls[0][4] is False  # no handle inheritance


def test_cleanup_passes_the_actors_exact_private_desktop(tmp_path,monkeypatch):
    calls=[]
    monkeypatch.setattr('web_tools.background_lifecycle.cleanup_private_apps',
        lambda windows,apps,**options:calls.append(options) or {'ok':True,'cleanup_complete':True})
    api=FakeDesktopAPI()
    actor=BackgroundDesktopHost(data_root=tmp_path,allowed_apps={},
        desktop_name='Xiaoruo1Background_probe-1',_api_factory=lambda:api)
    try:
        actor.execute('create')
        assert actor.execute('cleanup')['cleanup_complete'] is True
    finally:
        actor.close()
    assert calls==[{'data_root':tmp_path.resolve(),'desktop_name':'Xiaoruo1Background_probe-1'}]
