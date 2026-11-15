"""Close only verified private-desktop app instances and their idle helpers."""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from urllib.request import Request, ProxyHandler, build_opener


_UU_RECEIVER_NAMES = frozenset({
    'gameviewer.exe', 'gameviewerlauncher.exe', 'gameviewerservice.exe', 'gameviewerserver.exe',
    'gameviewerhealthd.exe',
    'todesk.exe', 'todesk_service.exe', 'zagent.exe',
})


def is_shared_uu_receiver(executable: str | Path) -> bool:
    """Shared remote-app login/receiver lifetime never belongs to a task."""
    return Path(executable).name.casefold() in _UU_RECEIVER_NAMES


def _default_window_pids() -> set[int]:
    import ctypes
    from ctypes import wintypes
    user=ctypes.WinDLL('user32',use_last_error=True)
    user.OpenDesktopW.argtypes=[wintypes.LPCWSTR,wintypes.DWORD,wintypes.BOOL,wintypes.DWORD]
    user.OpenDesktopW.restype=wintypes.HANDLE
    user.CloseDesktop.argtypes=[wintypes.HANDLE]
    callback=ctypes.WINFUNCTYPE(wintypes.BOOL,wintypes.HWND,wintypes.LPARAM)
    user.EnumDesktopWindows.argtypes=[wintypes.HANDLE,callback,wintypes.LPARAM]
    user.GetWindowThreadProcessId.argtypes=[wintypes.HWND,ctypes.POINTER(wintypes.DWORD)]
    handle=user.OpenDesktopW('Default',0,False,0x0041)
    if not handle:raise RuntimeError('Cannot verify the user desktop before cleanup')
    pids=set()
    @callback
    def collect(hwnd,_):
        pid=wintypes.DWORD();user.GetWindowThreadProcessId(hwnd,ctypes.byref(pid))
        if pid.value:pids.add(pid.value)
        return True
    try:user.EnumDesktopWindows(handle,collect,0)
    finally:user.CloseDesktop(handle)
    return pids


def cleanup_private_apps(windows: list[dict], allowed_apps: dict[str,Path], *, data_root: Path,
                         desktop_name: str = 'Xiaoruo1Background') -> dict:
    if not isinstance(desktop_name,str) or not re.fullmatch(r'Xiaoruo1Background(?:_[A-Za-z0-9_-]{1,48})?',desktop_name):
        raise ValueError('Only a private Xiaoruo1Background desktop can be cleaned up')
    import psutil
    import win32gui,win32con
    allowed={str(p.resolve()).casefold() for p in allowed_apps.values()}
    # Restore exact generic-app ownership across backend reloads. A registry
    # row alone never grants ownership: its desktop, PID, creation time and
    # executable must agree. Job records also cover owned headless helpers.
    owned=data_root/'background_desktop'/'owned-apps.json'
    known_generic=set()
    if owned.is_file() and owned.stat().st_size<=65536:
        try:
            records=json.loads(owned.read_text(encoding='utf8'))
            private_pids={w['pid'] for w in windows}
            for record in records:
                if not isinstance(record,dict) or record.get('desktop')!=desktop_name:continue
                if record.get('pid') not in private_pids and record.get('origin')!='generic_createprocess_job':continue
                try:
                    p=psutil.Process(record['pid'])
                    if is_shared_uu_receiver(p.exe()):continue
                    if p.create_time()==record.get('create_time') and str(Path(p.exe()).resolve()).casefold()==str(Path(record['executable']).resolve()).casefold():
                        allowed.add(str(Path(p.exe()).resolve()).casefold())
                        known_generic.add(p.pid)
                except (psutil.Error,KeyError,TypeError):pass
        except (ValueError,TypeError,AttributeError):pass
    protected=_default_window_pids()
    roots={w['pid'] for w in windows if w['visible'] and w['title']} | (known_generic-protected)
    if not roots:return {"ok":True,"closed_pids":[],"remaining_windows":[],"cleanup_complete":True}
    # The dedicated Code bridge is the authority for its own dirty buffers.
    config=data_root.parent/'07_web_config'/'background_desktop.json'
    try:
        d=json.loads(config.read_text(encoding='utf8'))
        token=Path(d['token_file']).read_text(encoding='utf8').strip()
        req=Request('http://127.0.0.1:48912/status',headers={'Authorization':'Bearer '+token})
        with build_opener(ProxyHandler({})).open(req,timeout=2) as r:status=json.load(r)
        dirty=[x for x in status.get('result',{}).get('documents',[]) if x.get('is_dirty') or x.get('isDirty')]
        if dirty:return {"ok":False,"cleanup_complete":False,"error_code":"unsaved_background_work","message":"Background VS Code contains unsaved work; it was preserved"}
    except Exception:
        pass # WM_CLOSE still preserves native save prompts; no forced window kill.
    processes={}
    receiver_pids=set()
    for pid in roots:
        try:
            p=psutil.Process(pid)
            if is_shared_uu_receiver(p.exe()):
                receiver_pids.add(pid)
                continue
            if pid in protected:
                raise RuntimeError('A private app also owns a user-desktop window; refusing cleanup')
            if str(Path(p.exe()).resolve()).casefold() not in allowed:
                continue
            processes[pid]=(p,p.create_time())
            for child in p.children(recursive=True):
                if child.pid not in protected and not is_shared_uu_receiver(child.exe()):
                    processes[child.pid]=(child,child.create_time())
        except psutil.Error:pass
    roots-=receiver_pids
    for w in windows:
        if w['pid'] in roots and w['pid'] in processes and w['visible'] and w['title']:
            win32gui.PostMessage(w['hwnd'],win32con.WM_CLOSE,0,0)
    deadline=time.monotonic()+5
    while time.monotonic()<deadline:
        if not any(win32gui.IsWindow(w['hwnd']) and win32gui.IsWindowVisible(w['hwnd']) for w in windows if w['pid'] in roots):break
        time.sleep(.1)
    remaining=[{'hwnd':w['hwnd'],'pid':w['pid']} for w in windows if w['pid'] in roots and win32gui.IsWindow(w['hwnd']) and win32gui.IsWindowVisible(w['hwnd'])]
    if remaining:
        return {"ok":False,"cleanup_complete":False,"remaining_windows":remaining,
                "error_code":"background_close_pending","message":"An app kept a save/confirmation window; no user work was force-closed"}
    closed=[]
    # Main windows have closed normally. Release only this exact process tree's
    # remaining helper processes, with creation time protecting against PID reuse.
    for pid,(process,created) in reversed(list(processes.items())):
        try:
            if process.is_running() and process.create_time()==created:
                if is_shared_uu_receiver(process.exe()):continue
                process.terminate()
            closed.append(pid)
        except psutil.Error:pass
    psutil.wait_procs([p for p,_ in processes.values()],timeout=2)
    return {"ok":True,"closed_pids":closed,"remaining_windows":[],"cleanup_complete":True,
            "host_input_used":False}
