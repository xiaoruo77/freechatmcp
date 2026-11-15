"""Read-only catalog of locally installed Windows GUI applications.

This module discovers application launch records; it never starts an app and
does not claim that an application supports private-desktop automation.  The
caller must still choose a backend and verify its process/window ownership.
"""

from __future__ import annotations

import ctypes
import hashlib
import os
import re
from pathlib import Path
from typing import Any, Callable, Iterable


_MAX_SHORTCUTS = 2_000
_MAX_NAME = 260
_BLOCKED_EXECUTABLES = frozenset({
    "cmd.exe", "command.com", "powershell.exe", "pwsh.exe", "wscript.exe",
    "cscript.exe", "rundll32.exe", "regsvr32.exe", "msiexec.exe",
    "conhost.exe", "wt.exe", "control.exe", "mmc.exe", "explorer.exe",
})
_MAINTENANCE_RE = re.compile(r"(?:^|[ _.-])(uninstall|unins|update|updater|repair|modify|setup|installer)(?:$|[ _.-])", re.I)


def _maintenance_name(*values: str) -> bool:
    return any(_MAINTENANCE_RE.search(str(value or "")) for value in values)


def _unquote(value: str) -> str:
    value = str(value or "").strip()
    if len(value) >= 2 and value[0] == value[-1] == '"':
        return value[1:-1]
    return value


def _extract_executable(raw: Any) -> tuple[str, str]:
    """Extract an executable and preserved argument string from a shortcut value."""
    if not isinstance(raw, str):
        return "", ""
    value = raw.strip()
    if not value:
        return "", ""
    if value.startswith('"'):
        end = value.find('"', 1)
        if end < 0:
            return "", ""
        return value[1:end], value[end + 1:].strip()
    match = re.search(r"(?i)\.exe(?:\s|$)", value)
    if match:
        end = match.end()
        return value[:end], value[end:].strip()
    return value, ""


def _command_line_to_argv(command_line: str) -> list[str]:
    """Parse Windows arguments without invoking a shell."""
    if not command_line:
        return []
    try:
        import win32api  # type: ignore
        return [str(item) for item in win32api.CommandLineToArgvW(command_line)]
    except Exception:
        pass
    if os.name != "nt":
        # Tests and non-Windows callers still receive deterministic tokenization.
        import shlex
        return shlex.split(command_line, posix=False)
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    argc = ctypes.c_int()
    shell32.CommandLineToArgvW.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_int)]
    shell32.CommandLineToArgvW.restype = ctypes.POINTER(ctypes.c_wchar_p)
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    pointer = shell32.CommandLineToArgvW(command_line, ctypes.byref(argc))
    if not pointer:
        raise OSError(ctypes.get_last_error(), "CommandLineToArgvW failed")
    try:
        return [pointer[index] for index in range(argc.value)]
    finally:
        kernel32.LocalFree(pointer)


def validate_executable(value: Any, *, maintenance_name: str = "") -> Path | None:
    """Return an existing absolute executable or ``None`` for unsafe entries."""
    if not isinstance(value, (str, os.PathLike)):
        return None
    raw = _unquote(os.fspath(value))
    if not raw or any(char in raw for char in ("\x00", "\r", "\n")):
        return None
    path = Path(raw).expanduser()
    if not path.is_absolute() or path.suffix.casefold() != ".exe":
        return None
    try:
        path = path.resolve(strict=True)
    except OSError:
        return None
    if not path.is_file() or path.name.casefold() in _BLOCKED_EXECUTABLES:
        return None
    if _maintenance_name(maintenance_name, path.stem):
        return None
    return path


def _entry(executable: Any, name: str, arguments: str = "", cwd: Any = "", source: str = "") -> dict[str, Any] | None:
    path = validate_executable(executable, maintenance_name=name)
    if path is None:
        return None
    try:
        working = Path(str(cwd)).expanduser().resolve() if cwd else path.parent
        if not working.is_absolute() or not working.is_dir():
            working = path.parent
    except OSError:
        working = path.parent
    arguments = str(arguments or "").strip()
    # Validate parsing but preserve the original argument text for the caller.
    try:
        argv = _command_line_to_argv(arguments)
    except Exception:
        return None
    identity = hashlib.sha256((str(path).casefold() + "\0" + arguments + "\0" + str(working).casefold()).encode("utf-8")).hexdigest()[:20]
    return {
        "app_id": f"app-{identity}",
        "name": str(name or path.stem)[:_MAX_NAME],
        "executable": str(path),
        "arguments": arguments,
        "argv": argv,
        "cwd": str(working),
        "source": str(source or "unknown"),
        "background_compatibility": "unverified",
        "launch_policy": "explicit_backend_required",
    }


def _registry_entries(registry_module: Any = None) -> Iterable[dict[str, Any]]:
    if os.name != "nt" and registry_module is None:
        return []
    if registry_module is not None:
        registry = registry_module
    else:
        try:
            import winreg as registry
        except ImportError:
            registry = None
    if registry is None:
        return []
    result: list[dict[str, Any]] = []
    base = r"Software\Microsoft\Windows\CurrentVersion\App Paths"
    hives = [("HKCU", getattr(registry, "HKEY_CURRENT_USER", None)), ("HKLM", getattr(registry, "HKEY_LOCAL_MACHINE", None))]
    views = [0, getattr(registry, "KEY_WOW64_64KEY", 0), getattr(registry, "KEY_WOW64_32KEY", 0)]
    seen_keys: set[tuple[str, int]] = set()
    for hive_name, hive in hives:
        if hive is None:
            continue
        for view in views:
            key_id = (hive_name, int(view))
            if key_id in seen_keys:
                continue
            seen_keys.add(key_id)
            try:
                root = registry.OpenKey(hive, base, 0, getattr(registry, "KEY_READ", 0x20019) | view)
            except (OSError, FileNotFoundError):
                continue
            try:
                count = min(int(registry.QueryInfoKey(root)[0]), _MAX_SHORTCUTS)
                for index in range(count):
                    try:
                        sub_name = str(registry.EnumKey(root, index))
                        sub = registry.OpenKey(root, sub_name, 0, getattr(registry, "KEY_READ", 0x20019) | view)
                        try:
                            target = registry.QueryValueEx(sub, "")[0]
                        finally:
                            try:
                                registry.CloseKey(sub)
                            except Exception:
                                pass
                        executable, arguments = _extract_executable(target)
                        item = _entry(executable, Path(sub_name).stem, arguments, Path(executable).parent if executable else "", f"registry:{hive_name}")
                        if item:
                            result.append(item)
                    except (OSError, ValueError, TypeError):
                        continue
            finally:
                try:
                    registry.CloseKey(root)
                except Exception:
                    pass
    return result


def _shortcut_roots(env: dict[str, str] | None = None) -> list[Path]:
    values = dict(os.environ if env is None else env)
    roots = []
    appdata = values.get("APPDATA")
    programdata = values.get("PROGRAMDATA", r"C:\ProgramData")
    if appdata:
        roots.append(Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs")
    if programdata:
        roots.append(Path(programdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs")
    return roots


def _shortcut_entries(roots: Iterable[Path], shortcut_reader: Callable[[Path], dict[str, Any]] | None = None) -> Iterable[dict[str, Any]]:
    if shortcut_reader is None:
        try:
            import win32com.client  # type: ignore
            shell = win32com.client.Dispatch("WScript.Shell")
            def shortcut_reader(path: Path) -> dict[str, Any]:
                shortcut = shell.CreateShortcut(str(path))
                return {"target": shortcut.TargetPath, "arguments": shortcut.Arguments, "cwd": shortcut.WorkingDirectory}
        except Exception:
            return []
    result: list[dict[str, Any]] = []
    seen = 0
    for root in roots:
        if not root.is_dir():
            continue
        for directory, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
            dirnames[:] = [name for name in dirnames if name not in {".git", "node_modules"}]
            for filename in filenames:
                if seen >= _MAX_SHORTCUTS:
                    return result
                if not filename.casefold().endswith(".lnk"):
                    continue
                seen += 1
                path = Path(directory) / filename
                try:
                    details = shortcut_reader(path)
                    if not isinstance(details, dict):
                        continue
                    item = _entry(details.get("target"), path.stem, details.get("arguments", ""), details.get("cwd", ""), "start_menu")
                    if item:
                        result.append(item)
                except Exception:
                    continue
    return result


def discover_installed_apps(*, limit: int = 300, registry_module: Any = None,
                            shortcut_roots: Iterable[Path] | None = None,
                            shortcut_reader: Callable[[Path], dict[str, Any]] | None = None,
                            env: dict[str, str] | None = None) -> list[dict[str, Any]]:
    """Return a bounded, deduplicated app catalog without launching anything."""
    limit = max(1, min(int(limit), 1000))
    entries = list(_registry_entries(registry_module))
    entries.extend(_shortcut_entries(list(shortcut_roots) if shortcut_roots is not None else _shortcut_roots(env), shortcut_reader))
    dedup: dict[tuple[str, str], dict[str, Any]] = {}
    for item in entries:
        key = (str(item["executable"]).casefold(), str(item.get("arguments", "")))
        previous = dedup.get(key)
        if previous is None:
            dedup[key] = item
        elif item["source"] not in previous["source"]:
            previous["source"] += ";" + item["source"]
    return list(dedup.values())[:limit]

