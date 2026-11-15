from __future__ import annotations

from pathlib import Path

from web_tools.installed_apps import discover_installed_apps, validate_executable, _command_line_to_argv


def test_validate_executable_requires_existing_gui_exe_and_rejects_launchers(tmp_path):
    good = tmp_path / "My Editor.exe"
    good.write_bytes(b"binary")
    assert validate_executable(str(good)) == good.resolve()
    assert validate_executable(str(tmp_path / "missing.exe")) is None
    assert validate_executable(str(tmp_path / "script.py")) is None
    blocked = tmp_path / "powershell.exe"
    blocked.write_bytes(b"binary")
    assert validate_executable(str(blocked)) is None
    updater = tmp_path / "Product Update.exe"
    updater.write_bytes(b"binary")
    assert validate_executable(str(updater)) is None


def test_shortcut_discovery_is_read_only_bounded_and_deduplicated(tmp_path):
    exe = tmp_path / "Editor.exe"
    exe.write_bytes(b"binary")
    shortcut_root = tmp_path / "Start Menu"
    shortcut_root.mkdir()
    for name in ("Editor.lnk", "Editor-copy.lnk", "Update.lnk", "Command.lnk"):
        (shortcut_root / name).write_text("placeholder", encoding="utf-8")
    calls = []

    def read_shortcut(path):
        calls.append(path)
        if path.name == "Update.lnk":
            return {"target": str(tmp_path / "Update.exe"), "arguments": ""}
        if path.name == "Command.lnk":
            return {"target": str(tmp_path / "powershell.exe"), "arguments": "-File bad.ps1"}
        return {"target": str(exe), "arguments": "--workspace \"D:\\Project\"", "cwd": str(tmp_path)}

    class EmptyRegistry:
        HKEY_CURRENT_USER = object()
        HKEY_LOCAL_MACHINE = object()
        KEY_READ = 1
        KEY_WOW64_64KEY = 2
        KEY_WOW64_32KEY = 4
        @staticmethod
        def OpenKey(*args):
            raise FileNotFoundError()
    result = discover_installed_apps(limit=20, registry_module=EmptyRegistry, shortcut_roots=[shortcut_root], shortcut_reader=read_shortcut)
    assert len(result) == 1
    assert result[0]["executable"] == str(exe.resolve())
    assert result[0]["background_compatibility"] == "unverified"
    assert result[0]["launch_policy"] == "explicit_backend_required"
    assert result[0]["source"] == "start_menu"
    assert {p.name for p in calls} == {"Editor.lnk", "Editor-copy.lnk", "Update.lnk", "Command.lnk"}


def test_registry_app_paths_are_cataloged_without_running_apps(tmp_path):
    exe = tmp_path / "Example.exe"
    exe.write_bytes(b"binary")

    class Key:
        def __init__(self, values=None, children=None):
            self.values = values or {}
            self.children = children or {}

    root = Key(children={"Example.exe": Key(values={"": f'"{exe}" --from-registry'})})

    class FakeRegistry:
        HKEY_CURRENT_USER = object()
        HKEY_LOCAL_MACHINE = object()
        KEY_READ = 1
        KEY_WOW64_64KEY = 2
        KEY_WOW64_32KEY = 4

        @staticmethod
        def OpenKey(hive, path, *args):
            if isinstance(hive, Key):
                return hive.children[path]
            if path.endswith("App Paths"):
                return root
            raise FileNotFoundError(path)

        @staticmethod
        def QueryInfoKey(key):
            return (len(key.children), 0, 0)

        @staticmethod
        def EnumKey(key, index):
            return list(key.children)[index]

        @staticmethod
        def QueryValueEx(key, value):
            if value not in key.values:
                raise FileNotFoundError(value)
            return key.values[value], 1

        @staticmethod
        def CloseKey(key):
            return None

    result = discover_installed_apps(limit=20, registry_module=FakeRegistry, shortcut_roots=[])
    assert len(result) == 1
    assert result[0]["name"] == "Example"
    assert result[0]["arguments"] == "--from-registry"
    assert result[0]["source"].startswith("registry:")


def test_limit_and_stable_id(tmp_path):
    exe_a = tmp_path / "A.exe"
    exe_b = tmp_path / "B.exe"
    exe_a.write_bytes(b"a")
    exe_b.write_bytes(b"b")
    root = tmp_path / "Programs"
    root.mkdir()
    for name in ("A.lnk", "B.lnk"):
        (root / name).write_text("x", encoding="utf-8")

    def reader(path):
        return {"target": str(exe_a if path.stem == "A" else exe_b), "arguments": ""}

    first = discover_installed_apps(limit=1, shortcut_roots=[root], shortcut_reader=reader)
    second = discover_installed_apps(limit=1, shortcut_roots=[root], shortcut_reader=reader)
    assert len(first) == 1 and first[0]["app_id"] == second[0]["app_id"]


def test_argument_parser_does_not_use_shell_semantics():
    assert _command_line_to_argv('--flag "two words"') in (["--flag", '"two words"'], ["--flag", "two words"])
