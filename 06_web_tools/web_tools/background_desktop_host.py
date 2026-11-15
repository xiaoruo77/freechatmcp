"""A current-user actor confined to a named, non-input Win32 desktop.

This is an opt-in local backend, not a second physical input device. It never
switches desktops or synthesizes input. Applications need native UIA patterns;
screenshots, coordinates, keyboard shortcuts and clipboard access are absent.
"""
from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError as FutureTimeout
import ctypes
from ctypes import wintypes
import os
import json
from pathlib import Path
import queue
import re
import subprocess
import threading
import time
import uuid
from typing import Any


class BackgroundDesktopError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _error(code: str, message: str) -> dict[str, Any]:
    return {"ok": False, "error_code": code, "message": message,
            "foreground_input_used": False}


class _PrivateProcess:
    def __init__(self, process_handle, pid: int, job_handle=None):
        self.handle = process_handle
        self.pid = pid
        self.job_handle=job_handle

    def poll(self):
        import win32process
        status = win32process.GetExitCodeProcess(self.handle)
        return None if status == 259 else status

    def close(self):
        self.handle.Close()
        if self.job_handle:self.job_handle.Close()

    def process_ids(self):
        if self.job_handle:
            import win32job
            return set(win32job.QueryInformationJobObject(self.job_handle,win32job.JobObjectBasicProcessIdList))
        return {self.pid}


def _create_private_process(executable: Path, argv: list[str], *, desktop: str,
                            environment: dict[str, str], cwd: Path, isolated_job:bool=False) -> _PrivateProcess:
    """Use real STARTUPINFO.lpDesktop; subprocess.STARTUPINFO drops this field."""
    import win32process
    if not re.fullmatch(r"WinSta0\\Xiaoruo1Background(?:_[A-Za-z0-9_-]{1,48})?", desktop):
        raise BackgroundDesktopError("desktop_target_mismatch", "Only the private desktop can receive a process")
    startup = win32process.STARTUPINFO()
    startup.lpDesktop = desktop
    flags=0x00000200 | 0x00000400 | 0x08000000
    if isolated_job:flags|=0x00000004
    process_handle, thread_handle, pid, _ = win32process.CreateProcess(
        str(executable), subprocess.list2cmdline(argv), None, None, False,
        flags,  # suspended until the private job is assigned for generic apps
        environment, str(cwd), startup)
    job=None
    try:
        if isolated_job:
            import win32job
            from .generic_launch import ui_restrictions
            job=win32job.CreateJobObject(None,'Xiaoruo1Background_'+uuid.uuid4().hex)
            win32job.SetInformationJobObject(job,win32job.JobObjectBasicUIRestrictions,{'UIRestrictionsClass':ui_restrictions()})
            win32job.AssignProcessToJobObject(job,process_handle)
            win32process.ResumeThread(thread_handle)
        return _PrivateProcess(process_handle,pid,job)
    except Exception:
        # The new process has never run. Stop it if its UI boundary cannot be
        # installed, instead of retrying without those restrictions.
        win32process.TerminateProcess(process_handle,1)
        process_handle.Close()
        if job:job.Close()
        raise BackgroundDesktopError('background_job_unavailable','Unable to assign the non-input process job; the new process was not allowed to run') from None
    finally:thread_handle.Close()


class _Win32Desktop:
    def __init__(self):
        if os.name != "nt":
            raise BackgroundDesktopError("windows_required", "Windows is required")
        self.user = ctypes.WinDLL("user32", use_last_error=True)
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.ole = ctypes.WinDLL("ole32", use_last_error=True)
        self.user.CreateDesktopW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR,
            ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
        self.user.CreateDesktopW.restype = wintypes.HANDLE
        self.user.SetThreadDesktop.argtypes = [wintypes.HANDLE]
        self.user.SetThreadDesktop.restype = wintypes.BOOL
        self.user.GetThreadDesktop.argtypes = [wintypes.DWORD]
        self.user.GetThreadDesktop.restype = wintypes.HANDLE
        self.user.OpenInputDesktop.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.user.OpenInputDesktop.restype = wintypes.HANDLE
        self.user.CloseDesktop.argtypes = [wintypes.HANDLE]
        self.user.CloseDesktop.restype = wintypes.BOOL
        self.user.GetUserObjectInformationW.argtypes = [wintypes.HANDLE, ctypes.c_int,
            ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
        self.user.GetUserObjectInformationW.restype = wintypes.BOOL
        self.user.GetForegroundWindow.restype = wintypes.HWND
        self.user.GetProcessWindowStation.restype = wintypes.HANDLE
        self.user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        self.user.GetWindowThreadProcessId.restype = wintypes.DWORD
        self.user.GetWindowTextLengthW.argtypes = [wintypes.HWND]
        self.user.GetWindowTextLengthW.restype = ctypes.c_int
        self.user.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        self.user.IsWindowVisible.argtypes = [wintypes.HWND]
        self.user.IsWindowVisible.restype = wintypes.BOOL
        self._enum_callback = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        self.user.EnumDesktopWindows.argtypes = [wintypes.HANDLE, self._enum_callback, wintypes.LPARAM]
        self.user.EnumDesktopWindows.restype = wintypes.BOOL
        self.kernel.GetCurrentThreadId.restype = wintypes.DWORD
        self.kernel.ProcessIdToSessionId.argtypes = [wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
        self.ole.CoInitializeEx.argtypes = [ctypes.c_void_p, wintypes.DWORD]
        self.ole.CoInitializeEx.restype = ctypes.c_long

    def object_name(self, handle) -> str:
        size = wintypes.DWORD()
        buffer = ctypes.create_unicode_buffer(512)
        if not self.user.GetUserObjectInformationW(handle, 2, buffer, ctypes.sizeof(buffer), ctypes.byref(size)):
            raise BackgroundDesktopError("desktop_inspection_failed", "Cannot inspect the desktop")
        return buffer.value

    def input_state(self) -> dict[str, Any]:
        handle = self.user.OpenInputDesktop(0, False, 0x0001)
        if not handle:
            raise BackgroundDesktopError("input_desktop_unavailable", "Input desktop is unavailable or secure")
        try:
            return {"desktop": self.object_name(handle),
                    "foreground_hwnd": int(self.user.GetForegroundWindow() or 0)}
        finally:
            self.user.CloseDesktop(handle)

    def create_and_attach(self, name: str):
        session = wintypes.DWORD()
        if not self.kernel.ProcessIdToSessionId(os.getpid(), ctypes.byref(session)) or session.value == 0:
            raise BackgroundDesktopError("interactive_user_required", "Run this host in the logged-in user session")
        if self.object_name(self.user.GetProcessWindowStation()).casefold() != "winsta0":
            raise BackgroundDesktopError("interactive_user_required", "Run this host in the interactive WinSta0 window station")
        if self.input_state()["desktop"].casefold() != "default":
            raise BackgroundDesktopError("input_desktop_unavailable", "Create the actor from the unlocked Default desktop")
        # Read/create/enumerate/write object rights; no switch-desktop right.
        handle = self.user.CreateDesktopW(name, None, None, 0, 0x00C7, None)
        if not handle:
            raise BackgroundDesktopError("desktop_create_failed", "Cannot create the private desktop")
        if not self.user.SetThreadDesktop(handle):
            self.user.CloseDesktop(handle)
            raise BackgroundDesktopError("desktop_attach_failed", "Cannot attach the actor thread")
        current = self.user.GetThreadDesktop(self.kernel.GetCurrentThreadId())
        if self.object_name(current) != name:
            self.user.CloseDesktop(handle)
            raise BackgroundDesktopError("desktop_target_mismatch", "Actor did not attach to the private desktop")
        return handle

    def initialize_com(self):
        result = self.ole.CoInitializeEx(None, 2)  # STA, after SetThreadDesktop.
        if result not in (0, 1):
            raise BackgroundDesktopError("com_initialization_failed", "Cannot initialize the actor COM apartment")

    def release_com(self):
        self.ole.CoUninitialize()

    def close_handle(self, handle):
        self.user.CloseDesktop(handle)

    def windows(self, handle, name: str) -> list[dict[str, Any]]:
        if self.object_name(handle) != name:
            raise BackgroundDesktopError("desktop_target_mismatch", "Enumeration handle is not the private desktop")
        found = []

        @self._enum_callback
        def collect(hwnd, _):
            pid = wintypes.DWORD()
            self.user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            try:
                if not pid.value:
                    return True
                # EnumDesktopWindows scopes HWNDs to this exact desktop handle.
                # GetThreadDesktop on a foreign process can return an unusable
                # handle for a non-input desktop, so it is not a second proof.
                length = min(self.user.GetWindowTextLengthW(hwnd), 1024)
                title = ctypes.create_unicode_buffer(length + 1)
                self.user.GetWindowTextW(hwnd, title, len(title))
                found.append({"hwnd": int(hwnd), "pid": int(pid.value),
                    "title": title.value, "visible": bool(self.user.IsWindowVisible(hwnd)),
                    "desktop": name})
                return True
            except BackgroundDesktopError:
                # A window can disappear during enumeration. Never rebind it.
                return True

        ctypes.set_last_error(0)
        if not self.user.EnumDesktopWindows(handle, collect, 0) and ctypes.get_last_error():
            raise BackgroundDesktopError("desktop_enumeration_failed", "Cannot enumerate the private desktop")
        return found


class _UIAutomation:
    """COM objects live and are used only on the private-desktop actor thread."""
    def __init__(self):
        try:
            import comtypes.client
            module = comtypes.client.GetModule("UIAutomationCore.dll")
            self.client = comtypes.client.CreateObject(
                "{FF48DBA4-60EF-4201-AA87-54103EEF594E}", interface=module.IUIAutomation)
            self.module = module
        except Exception:
            raise BackgroundDesktopError("uia_unavailable", "UI Automation is unavailable in this host runtime") from None
        self.elements = {}
        self.generation = 0

    def snapshot(self, windows: list[dict[str, Any]], hwnd: int | None, limit: int) -> list[dict[str, Any]]:
        self.elements.clear()
        self.generation += 1
        roots = windows if hwnd is None else [w for w in windows if w["hwnd"] == hwnd]
        if hwnd is not None and not roots:
            raise BackgroundDesktopError("desktop_target_mismatch", "Window is not on the private desktop")
        result = []
        walker = self.client.ControlViewWalker
        pending = []
        for window in roots:
            try:
                pending.append((self.client.ElementFromHandle(window["hwnd"]), window["hwnd"], 0))
            except Exception:
                continue
        while pending and len(result) < limit:
            element, root_hwnd, depth = pending.pop()
            try:
                identity = f"{self.generation}:{len(result) + 1}"
                patterns = []
                for property_name, pattern in (("UIA_IsInvokePatternAvailablePropertyId", "invoke"),
                        ("UIA_IsValuePatternAvailablePropertyId", "value"),
                        ("UIA_IsSelectionItemPatternAvailablePropertyId", "selection"),
                        ("UIA_IsScrollPatternAvailablePropertyId", "scroll"),
                        ("UIA_IsTogglePatternAvailablePropertyId","toggle"),
                        ("UIA_IsExpandCollapsePatternAvailablePropertyId","expand_collapse"),
                        ("UIA_IsRangeValuePatternAvailablePropertyId","range_value"),
                        ("UIA_IsWindowPatternAvailablePropertyId","window")):
                    property_id=getattr(self.module,property_name)
                    value=element.GetCurrentPropertyValue(property_id)
                    raw=getattr(value,'value',value)
                    if type(raw) in {bool,int} and raw!=0:
                        patterns.append(pattern)
                result.append({"element_id": identity, "hwnd": root_hwnd,
                    "name": str(element.CurrentName)[:1000],
                    "control_type": int(element.CurrentControlType), "depth": depth,
                    "patterns": patterns})
                self.elements[identity] = (element, root_hwnd)
                if depth < 25:
                    child = walker.GetFirstChildElement(element)
                    siblings = []
                    while child and len(siblings) < limit:
                        siblings.append((child, root_hwnd, depth + 1))
                        child = walker.GetNextSiblingElement(child)
                    pending.extend(reversed(siblings))
            except Exception:
                continue
        return result

    def perform(self, action: str, args: dict, windows: list[dict]) -> dict:
        identity = args.get("element_id")
        if not isinstance(identity, str) or identity not in self.elements:
            raise BackgroundDesktopError("uia_target_stale", "Take a fresh snapshot and use its element_id")
        element, hwnd = self.elements[identity]
        if hwnd not in {w["hwnd"] for w in windows}:
            raise BackgroundDesktopError("desktop_target_mismatch", "Target window is no longer on the private desktop")
        # Revalidate the ancestor HWND; a stale provider must not escape the desktop.
        ancestor = element
        rooted = False
        for _ in range(64):
            if not ancestor:
                break
            if int(ancestor.CurrentNativeWindowHandle or 0) == hwnd:
                rooted = True
                break
            ancestor = self.client.ControlViewWalker.GetParentElement(ancestor)
        if not rooted:
            raise BackgroundDesktopError("desktop_target_mismatch", "UIA target is not rooted in the private window")
        specs = {
            "click": (10000, self.module.IUIAutomationInvokePattern, "Invoke"),
            "type": (10002, self.module.IUIAutomationValuePattern, "SetValue"),
            "selection": (10010, self.module.IUIAutomationSelectionItemPattern, "Select"),
            "scroll": (10004, self.module.IUIAutomationScrollPattern, "Scroll"),
            "toggle": (10015,self.module.IUIAutomationTogglePattern,"Toggle"),
            "expand": (10005,self.module.IUIAutomationExpandCollapsePattern,"Expand"),
            "collapse": (10005,self.module.IUIAutomationExpandCollapsePattern,"Collapse"),
            "set_range": (10003,self.module.IUIAutomationRangeValuePattern,"SetValue"),
        }
        pattern_id, interface, method = specs[action]
        try:
            pattern = element.GetCurrentPattern(pattern_id).QueryInterface(interface)
            if action == "type":
                if pattern.CurrentIsReadOnly:
                    raise BackgroundDesktopError("uia_value_readonly", "The target value is read-only")
                pattern.SetValue(args["text"])
            elif action == "scroll":
                amounts = {"none": 2, "large_decrement": 0, "small_decrement": 1,
                    "large_increment": 3, "small_increment": 4}
                pattern.Scroll(amounts[args.get("horizontal", "none")], amounts[args.get("vertical", "none")])
            elif action=='set_range':
                value=args['value']
                if pattern.CurrentIsReadOnly or not pattern.CurrentMinimum<=value<=pattern.CurrentMaximum:
                    raise BackgroundDesktopError('uia_range_invalid','The target range is read-only or the value is outside its limits')
                pattern.SetValue(value)
            else:
                getattr(pattern, method)()
        except BackgroundDesktopError:
            raise
        except Exception:
            raise BackgroundDesktopError("uia_pattern_unavailable", "The requested native pattern is unavailable; no input fallback was attempted") from None
        return {"ok": True, "action": action, "element_id": identity,
                "verification_required": True, "foreground_input_used": False}


class BackgroundDesktopHost:
    """Synchronous facade; HTTP callers never hold desktop/UIA COM objects."""
    _fields = {"status": set(), "create": set(), "list_windows": set(),
        "list_apps": {"query","limit"},
        "launch_app": {"app", "executable", "argv", "cwd", "timeout", "document_path"}, "snapshot": {"hwnd", "max_elements"},
        "edge": {"action", "arguments"},
        "wps": {"action", "arguments"},
        "uu": {"action", "arguments"},
        "todesk": {"action", "arguments"},
        "cleanup": set(),
        "screenshot": {"hwnd"},
        "click": {"element_id"}, "type": {"element_id", "text"},
        "selection": {"element_id"}, "scroll": {"element_id", "horizontal", "vertical"},
        "toggle":{"element_id"},"expand":{"element_id"},"collapse":{"element_id"},
        "set_range":{"element_id","value"}}

    def __init__(self, *, data_root: Path, allowed_apps: dict[str, Path],
                 desktop_name: str = "Xiaoruo1Background", _api_factory=None):
        if not re.fullmatch(r"Xiaoruo1Background(?:_[A-Za-z0-9_-]{1,48})?", desktop_name):
            raise ValueError("Only a private Xiaoruo1Background desktop name is allowed")
        self.data_root = Path(data_root).resolve()
        self.allowed_apps = {key: Path(value).resolve() for key, value in allowed_apps.items()}
        self.desktop_name = desktop_name
        self._factory = _api_factory or _Win32Desktop
        self._api = None
        self._thread = None
        self._handle = None
        self._requests = queue.Queue()
        self._ready = Future()
        self._lock = threading.Lock()
        self._uia = None
        self._launched = []
        self._uu_session = None
        self._todesk_session = None
        self._uu_job = None
        self._uu_cli_executor = None
        self._uu_cli_thread_id = None
        self._closed = False

    def _validate(self, action, args):
        if action not in self._fields:
            raise BackgroundDesktopError("background_action_unsupported", "Only native pattern actions are supported; screenshots and keyboard shortcuts are unsupported")
        if not isinstance(args, dict) or set(args) - self._fields[action]:
            raise BackgroundDesktopError("invalid_arguments", "Unsupported arguments; coordinates and arbitrary commands are forbidden")
        if action == "launch_app":
            if not isinstance(args.get('app',args.get('executable')),str):
                raise BackgroundDesktopError('application_required','Choose an installed app or an absolute executable path')
            timeout = args.get("timeout", 20)
            if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 1 <= timeout <= 45:
                raise BackgroundDesktopError("invalid_arguments", "Launch timeout must be between 1 and 45 seconds")
        if action in {"snapshot", "screenshot"}:
            for key, default, low, high in (("hwnd", None, 1, 2**64), ("max_elements", 200, 1, 500)):
                value = args.get(key, default)
                if value is not None and (isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high):
                    raise BackgroundDesktopError("invalid_arguments", f"Invalid {key}")
        if action == "type" and (not isinstance(args.get("text"), str) or len(args["text"]) > 40_000):
            raise BackgroundDesktopError("invalid_arguments", "Text must be a string of at most 40000 characters")
        if action == "scroll" and any(args.get(key, "none") not in {
                "none", "large_decrement", "small_decrement", "large_increment", "small_increment"}
                for key in ("horizontal", "vertical")):
            raise BackgroundDesktopError("invalid_arguments", "Unsupported native scroll amount")
        if action=='set_range':
            import math
            value=args.get('value')
            if type(value) not in {int,float} or not math.isfinite(value):
                raise BackgroundDesktopError('invalid_arguments','A finite numeric value is required')

    def execute(self, action: str, args: dict | None = None) -> dict[str, Any]:
        try:
            arguments = {} if args is None else args
            self._validate(action, arguments)
            observations = []
            with self._lock:
                if self._closed:
                    raise BackgroundDesktopError("background_host_closed", "This actor is closed")
                if action == "status":
                    return {"ok": True, "created": self._thread is not None and self._ready.done() and self._ready.exception() is None,
                        "desktop": self.desktop_name, "foreground_input_used": False,
                        "supported_actions": sorted(self._fields), "screenshot_supported": True}
                if action == "create" and self._thread is None:
                    self._api = self._factory()
                    observations.append(self._api.input_state())
                    if {'uu','todesk'} & self.allowed_apps.keys():
                        # The authenticated non-GUI terminal client must start
                        # from Default. The shared receiver stays there; all
                        # injected input occurs on the verified other computer.
                        self._uu_cli_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="UUConnectionCLI")
                        self._uu_cli_thread_id = self._uu_cli_executor.submit(threading.get_ident).result(timeout=2)
                    self._thread = threading.Thread(target=self._run, name="Xiaoruo1BackgroundActor", daemon=True)
                    self._thread.start()
                if self._thread is None:
                    raise BackgroundDesktopError("background_desktop_not_created", "Call create before using the private desktop")
            self._ready.result(timeout=15)
            future = Future()
            self._requests.put((action, arguments, future))
            extended_uu=action=='uu' and arguments.get('action') in {'connect','keyboard_start'}
            uu_screenshot=action=='uu' and arguments.get('action')=='screenshot'
            deadline = time.monotonic() + (100 if extended_uu else 90 if uu_screenshot else 60)
            while True:
                state = self._api.input_state()
                if not observations or observations[-1] != state:
                    observations.append(state)
                if state["desktop"].casefold() == self.desktop_name.casefold():
                    raise BackgroundDesktopError("private_desktop_became_input", "Private desktop unexpectedly became the input desktop")
                try:
                    result = future.result(timeout=0.1)
                    break
                except FutureTimeout:
                    if time.monotonic() >= deadline:
                        raise BackgroundDesktopError("background_action_timeout", "Actor did not finish; controls were not retried")
            result["input_evidence"] = {"observations": observations[:100],
                "private_desktop_was_input": False,
                "foreground_unchanged": len({s["foreground_hwnd"] for s in observations}) == 1,
                "user_changes_are_not_failure": True}
            return result
        except BackgroundDesktopError as exc:
            return _error(exc.code, str(exc))
        except FutureTimeout:
            return _error("background_actor_start_timeout", "Actor startup timed out")
        except Exception:
            return _error("background_actor_failed", "Background actor failed; no input fallback was attempted")

    def _run(self):
        com_ready = False
        try:
            self._handle = self._api.create_and_attach(self.desktop_name)
            self._api.initialize_com()
            com_ready = True
            self._ready.set_result(True)
            while True:
                request = self._requests.get()
                if request is None:
                    break
                action, args, future = request
                try:
                    future.set_result(self._dispatch(action, args))
                except Exception as exc:
                    future.set_exception(exc)
        except Exception as exc:
            if not self._ready.done():
                self._ready.set_exception(exc)
        finally:
            if self._uu_session is not None:
                try:
                    self._uu_session.perform('close', {})
                except Exception:
                    pass
            if self._todesk_session is not None:
                try:
                    self._run_uu_default(self._todesk_session.perform, 'close', {}, _wait_timeout=15)
                except Exception:
                    pass
            self._uia = None
            if com_ready:
                self._api.release_com()

    def _windows(self):
        return self._api.windows(self._handle, self.desktop_name)

    def _dispatch(self, action, args):
        if action == "create":
            return {"ok": True, "created": True, "desktop": self.desktop_name,
                    "actor_thread_id": threading.get_native_id(), "foreground_input_used": False}
        if action=='list_apps':
            from .installed_apps import discover_installed_apps
            query=args.get('query','')
            limit=args.get('limit',100)
            if not isinstance(query,str) or len(query)>200 or type(limit) is not int or not 1<=limit<=1000:
                raise BackgroundDesktopError('invalid_arguments','Use a bounded app query and a limit from 1 to 1000')
            entries=discover_installed_apps(limit=1000)
            matches=[entry for entry in entries if query.casefold() in entry['name'].casefold() or query.casefold() in entry['executable'].casefold()]
            return {'ok':True,'apps':matches[:limit],'matched_count':len(matches),'scope':'registered_or_start_menu_win32_apps',
                    'background_compatibility':'evaluated_at_runtime','foreground_input_used':False}
        if action == "launch_app":
            if args.get('executable') or args.get('app') not in {'vscode','edge','wps','wps_spreadsheet','wps_presentation'}:
                return self._launch_generic_app(args)
            if args["app"] != "vscode":
                return self._launch_other_app(args)
            executable = self.allowed_apps["vscode"]
            if not executable.is_file() or executable.name.casefold() != "code.exe":
                raise BackgroundDesktopError("background_app_unavailable", "Configured Code.exe does not exist")
            project_root = self.data_root.parent
            extension = project_root / "10_web_deploy" / "vscode_background_extension"
            bridge_config = project_root / "07_web_config" / "vscode_background.json"
            if not (extension / "package.json").is_file() or not bridge_config.is_file():
                raise BackgroundDesktopError("background_editor_bridge_unavailable", "Prepare the private VS Code bridge before launching the editor")
            profile = self.data_root / "background_desktop" / "vscode-profile"
            profile.mkdir(parents=True, exist_ok=True)
            # This profile is never copied from the user's authenticated profile.
            argv = [str(executable), "--new-window", "--user-data-dir", str(profile),
                    "--disable-gpu", "--force-renderer-accessibility",
                    "--extensionDevelopmentPath", str(extension),
                    "--extensions-dir", str(Path.home() / ".vscode" / "extensions")]
            environment = os.environ.copy()
            environment["XIAORUO1_VSCODE_BRIDGE_CONFIG"] = str(bridge_config)
            process = _create_private_process(executable, argv,
                desktop="WinSta0\\" + self.desktop_name, environment=environment,
                cwd=executable.parent)
            self._launched.append(process)
            end = time.monotonic() + args.get("timeout", 20)
            windows = []
            while time.monotonic() < end:
                windows = [w for w in self._windows() if w["visible"] and w["title"]]
                if windows:
                    break
                time.sleep(0.2)
            return {"ok": bool(windows), "desktop": self.desktop_name,
                "launched_pid": process.pid, "launcher_exit_code": process.poll(),
                "windows": windows, "profile": str(profile),
                "error_code": None if windows else "background_app_window_unavailable",
                "foreground_input_used": False}
        windows = self._windows()
        if action == "cleanup":
            remote_cleanup=None
            todesk_cleanup=None
            if self._todesk_session is not None:
                todesk_cleanup=self._run_uu_default(self._todesk_session.perform,'close',{},_wait_timeout=15)
                if not todesk_cleanup.get('ok'):
                    return {**todesk_cleanup,'cleanup_complete':False}
            if self._uu_session is not None:
                remote_cleanup = self._uu_session.perform("close", {})
                if not remote_cleanup.get("ok"):
                    return {**remote_cleanup, "cleanup_complete": False}
                windows = self._windows()
            from .background_lifecycle import cleanup_private_apps
            result=cleanup_private_apps(windows,self.allowed_apps,data_root=self.data_root,
                                        desktop_name=self.desktop_name)
            if remote_cleanup is not None:result['uu_cleanup']=remote_cleanup
            if todesk_cleanup is not None:result['todesk_cleanup']=todesk_cleanup
            return result
        if action == 'todesk':
            from .todesk_control import ToDeskSession
            executable=self.allowed_apps.get('todesk')
            if executable is None:
                raise BackgroundDesktopError('todesk_not_configured','Configure the installed ToDesk client first')
            if self._todesk_session is None:
                from .todesk_background import StrictBackgroundToDesk
                config_path=self.data_root.parent/'07_web_config'/'todesk_control.json'
                config=json.loads(config_path.read_text(encoding='utf-8-sig'))
                if config.get('enabled') is not True:
                    raise BackgroundDesktopError('todesk_disabled','ToDesk control is disabled')
                devices=config.get('allowed_devices',{})
                self._todesk_session=ToDeskSession(data_root=self.data_root,executable=executable,
                    allowed_devices=devices,native=StrictBackgroundToDesk(str(executable),devices))
            return self._run_uu_default(self._todesk_session.perform,args.get('action','status'),
                args.get('arguments') or {},_wait_timeout=45)
        if action == "uu":
            from .uu_shared_gui import SharedUUGuiSession
            executable = self.allowed_apps.get("uu")
            if executable is None:
                raise BackgroundDesktopError("uu_not_configured", "Configure the installed UU Remote launcher first")
            if self._uu_session is None:
                def keyboard_uu(cli,device_id):
                    from .uu_remote_keyboard import RemoteKeyboardSession
                    from .uu_conpty import PseudoConsoleProcess
                    return RemoteKeyboardSession(cli,device_id,
                        process_factory=lambda *a,**kw:self._run_uu_default(PseudoConsoleProcess,*a,_wait_timeout=10,**kw),
                        cli_runner=self._run_uu_cli,
                        state_root=self.data_root/'background_desktop'/'uu-sessions')
                from .remote_gui_capture_client import RemoteCaptureClient
                self._uu_session = SharedUUGuiSession(
                    data_root=self.data_root, executable=executable,
                    cli_runner=self._run_uu_cli, endpoint_factory=keyboard_uu,
                    capture_client=RemoteCaptureClient(self.data_root))
            return self._uu_session.perform(args.get("action", "status"), args.get("arguments") or {})
        if action == "edge":
            from .background_edge import edge_action
            return edge_action(args.get("action", "status"), args.get("arguments") or {}, data_root=self.data_root, windows=windows)
        if action == "wps":
            from .background_wps import wps_action
            return wps_action(args.get("action","status"),args.get("arguments") or {},windows=windows,data_root=self.data_root)
        if action == "screenshot":
            from .background_capture import capture_window, capture_desktop
            selected = [w for w in windows if w["visible"] and
                        (args.get("hwnd") is None or args["hwnd"] == w["hwnd"])]
            path = self.data_root / "background_desktop" / "captures" / (uuid.uuid4().hex + ".png")
            if args.get("hwnd") is None:
                try:capture=capture_desktop(windows,path)
                except (RuntimeError,ValueError) as exc:
                    raise BackgroundDesktopError('background_capture_not_ready',str(exc)) from None
                return {"ok":True,"desktop":self.desktop_name,"capture":capture,"foreground_input_used":False}
            if len(selected) != 1:
                raise BackgroundDesktopError("private_window_selection_required", "Select one explicit private window HWND from list_windows")
            try:capture = capture_window(selected[0]["hwnd"], path)
            except (RuntimeError,ValueError) as exc:
                raise BackgroundDesktopError('background_capture_not_ready',str(exc)) from None
            return {"ok":True,"desktop":self.desktop_name,"capture":capture,"window":selected[0],
                    "foreground_input_used":False}
        if action == "list_windows":
            return {"ok": True, "desktop": self.desktop_name, "windows": windows,
                    "foreground_input_used": False}
        if action == "snapshot":
            try:
                if self._uia is None:
                    self._uia = _UIAutomation()
                elements = self._uia.snapshot(windows, args.get("hwnd"), args.get("max_elements", 200))
                return {"ok": True, "desktop": self.desktop_name, "windows": windows,
                    "elements": elements, "uia_supported": True,
                    "screenshot_supported": True, "foreground_input_used": False}
            except BackgroundDesktopError as exc:
                if exc.code != "uia_unavailable":
                    raise
                return {"ok": True, "desktop": self.desktop_name, "windows": windows,
                    "elements": [], "uia_supported": False, "uia_error_code": exc.code,
                    "screenshot_supported": True, "foreground_input_used": False}
        if self._uia is None:
            raise BackgroundDesktopError("uia_target_stale", "Take a fresh UIA snapshot first")
        return self._uia.perform(action, args, windows)

    def _run_uu_cli(self, *args, **kwargs):
        return self._run_uu_default(subprocess.run, *args,
                                    _wait_timeout=kwargs.get("timeout", 25) + 2, **kwargs)

    def _run_uu_default(self, function, *args, _wait_timeout=30, **kwargs):
        if self._uu_cli_executor is None:
            raise BackgroundDesktopError("uu_cli_worker_unavailable", "Create the UU connection worker before the private actor")
        def run():
            native = _Win32Desktop()
            current = native.user.GetThreadDesktop(native.kernel.GetCurrentThreadId())
            if native.object_name(current).casefold() != "default":
                raise BackgroundDesktopError("uu_cli_desktop_mismatch", "The non-GUI UU CLI worker is not on its expected desktop")
            return function(*args, **kwargs)
        if threading.get_ident() == self._uu_cli_thread_id:
            return run()
        return self._uu_cli_executor.submit(run).result(timeout=_wait_timeout)

    def _launch_generic_app(self,args):
        from .generic_launch import prepare_application,check_user_instance,GenericLaunchError
        try:
            entry=prepare_application(args)
            executable=entry['executable']
            from .background_lifecycle import is_shared_uu_receiver
            if is_shared_uu_receiver(executable):
                raise GenericLaunchError('uu_shared_receiver_preserved',
                    'UU login and receiver remain on the user desktop; use the UU temporary graphical-agent action')
            # Preserve already verified independent-profile strategies when a
            # user selected the same program through its catalog/path entry.
            for name,path in self.allowed_apps.items():
                if name in {'vscode','edge','wps','wps_spreadsheet','wps_presentation'} and path==executable:
                    if args.get('argv') or args.get('cwd'):
                        raise GenericLaunchError('application_strategy_required','This app uses an existing private-instance strategy; custom launch arguments are not accepted')
                    return self._dispatch('launch_app',{'app':name,**{k:v for k,v in args.items() if k in {'document_path','timeout'}}})
            check_user_instance(executable)
        except GenericLaunchError as exc:
            raise BackgroundDesktopError(exc.code,str(exc)) from None
        except (OSError,ValueError) as exc:
            raise BackgroundDesktopError('application_selection_invalid',str(exc)) from None
        existing={w['hwnd'] for w in self._windows()}
        process=_create_private_process(executable,entry['argv'],desktop='WinSta0\\'+self.desktop_name,
            environment=os.environ.copy(),cwd=entry['cwd'],isolated_job=True)
        self._launched.append(process)
        self.allowed_apps['generic_'+str(process.pid)]=executable
        import psutil
        owned=self.data_root/'background_desktop'/'owned-apps.json'
        try:records=json.loads(owned.read_text(encoding='utf8')) if owned.is_file() else []
        except ValueError:records=[]
        records=[r for r in records if isinstance(r,dict)][-199:]
        deadline=time.monotonic()+args.get('timeout',20)
        found=[]
        while time.monotonic()<deadline:
            members=process.process_ids()
            found=[w for w in self._windows() if w['pid'] in members and w['visible'] and w['hwnd'] not in existing and w['title']]
            if found:break
            # Launchers can exit before their GUI child creates a window. Job
            # membership, rather than the wrapper's exit, owns that lifetime.
            if process.poll() is not None and not members:break
            time.sleep(.1)
        for pid in process.process_ids():
            try:
                p=psutil.Process(pid)
                records=[r for r in records if r.get('pid')!=pid]
                records.append({'pid':pid,'create_time':p.create_time(),'executable':p.exe(),'desktop':self.desktop_name,
                                'origin':'generic_createprocess_job'})
            except psutil.Error:pass
        owned.parent.mkdir(parents=True,exist_ok=True);owned.write_text(json.dumps(records,ensure_ascii=False),encoding='utf8')
        return {'ok':bool(found),'app':entry['name'],'executable':str(executable),'launched_pid':process.pid,
                'desktop':self.desktop_name,'windows':found,'launcher_exit_code':process.poll(),
                'background_compatibility':'private_window_observed' if found else 'no_private_window_observed',
                'process_job_ui_restrictions':True,'screenshot_and_controls':'inspect_at_runtime',
                'complete_compatibility_verified':False,'foreground_input_used':False,
                'error_code':None if found else 'private_app_window_unavailable'}

    def _launch_other_app(self, args):
        app = args["app"]
        executable = self.allowed_apps[app]
        names = {"edge":"msedge.exe","wps":"wps.exe","wps_spreadsheet":"et.exe","wps_presentation":"wpp.exe"}
        if not executable.is_file() or executable.name.casefold() != names[app]:
            raise BackgroundDesktopError("background_app_unavailable", "Configured application executable is invalid")
        before = self._windows()
        if app == "edge":
            profile = self.data_root / "background_desktop" / "edge-profile"
            profile.mkdir(parents=True, exist_ok=True)
            argv = [str(executable), "--user-data-dir="+str(profile), "--new-window", "--no-first-run",
                    "--no-default-browser-check", "--disable-gpu", "--disable-backgrounding-occluded-windows",
                    "--disable-renderer-backgrounding", "--disable-features=CalculateNativeWinOcclusion",
                    "--remote-debugging-address=127.0.0.1", "--remote-debugging-port=48913", "about:blank"]
            existing = [w for w in before if w["visible"] and w["title"] and "edge" in w["title"].casefold()]
            if existing:
                return {"ok":True,"app":app,"desktop":self.desktop_name,"windows":existing,"reused_private_instance":True,"foreground_input_used":False}
        else:
            raw = args.get("document_path")
            if not isinstance(raw,str) or not Path(raw).is_absolute():
                raise BackgroundDesktopError("document_path_required", "WPS requires an explicit local document path")
            document = Path(raw).resolve(strict=True)
            suffixes={"wps":{'.docx','.doc','.rtf','.wps'},"wps_spreadsheet":{'.xlsx','.xls','.et'},"wps_presentation":{'.pptx','.ppt','.dps'}}
            if document.drive.casefold() not in {'c:','d:'} or not document.is_file() or document.suffix.casefold() not in suffixes[app]:
                raise BackgroundDesktopError("document_path_denied", "Document type or local path is unsupported")
            component = {"wps":"/wps","wps_spreadsheet":"/et","wps_presentation":"/wpp"}[app]
            argv = [str(executable), "/prometheus", component, "/n", str(document)]
        process = _create_private_process(executable,argv,desktop="WinSta0\\"+self.desktop_name,
                                         environment=os.environ.copy(),cwd=executable.parent)
        self._launched.append(process)
        previous = {w["hwnd"] for w in before}
        end=time.monotonic()+args.get("timeout",20)
        selected=[]
        while time.monotonic()<end:
            selected=[w for w in self._windows() if w["hwnd"] not in previous and w["visible"] and w["title"]]
            if selected:break
            time.sleep(.2)
        return {"ok":bool(selected),"app":app,"desktop":self.desktop_name,"launched_pid":process.pid,
                "launcher_exit_code":process.poll(),
                "windows":selected,"error_code":None if selected else "private_app_window_unavailable",
                "foreground_input_used":False}

    def close(self):
        """Release our actor only. Never close or kill any application window."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            if self._thread is not None:
                self._requests.put(None)
        if self._thread is not None:
            self._thread.join(timeout=30)
            if not self._thread.is_alive() and self._handle:
                self._api.close_handle(self._handle)
                self._handle = None
                for process in self._launched:
                    process.close()
        if self._uu_cli_executor is not None:
            self._uu_cli_executor.shutdown(wait=False, cancel_futures=True)
            self._uu_cli_executor = None
