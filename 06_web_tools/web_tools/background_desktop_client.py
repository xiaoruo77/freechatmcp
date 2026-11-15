"""Narrow client for the user's same-machine background desktop backend."""
from __future__ import annotations

import asyncio
import json
import re
import time
import sys
from pathlib import Path
from urllib.request import Request, ProxyHandler, build_opener
from urllib.error import URLError,HTTPError


def configured(settings) -> bool:
    root = getattr(settings, "project_root", None)
    override=(getattr(settings,'extra',None) or {}).get('background_desktop_config')
    if override is not None:return Path(override).is_file()
    return bool(root and (Path(root) / "07_web_config" / "background_desktop.json").is_file())


def _request(settings, action: str, arguments: dict, *, vscode: bool = False) -> dict:
    root = Path(settings.project_root)
    config_path = Path((getattr(settings,'extra',None) or {}).get('background_desktop_config') or root / "07_web_config" / "background_desktop.json")
    if config_path.stat().st_size > 16384:
        raise ValueError("Background desktop configuration is too large")
    config = json.loads(config_path.read_text(encoding="utf-8"))
    if config.get("port") != 48911:
        raise ValueError("Background desktop port is invalid")
    token_path = Path(config["token_file"])
    if not token_path.is_absolute() or token_path.stat().st_size > 4096:
        raise ValueError("Background desktop credential reference is invalid")
    token = token_path.read_text(encoding="utf-8").strip()
    if len(token) < 32 or any(c in token for c in "\n\r\0"):
        raise ValueError("Background desktop credential is invalid")
    payload = {**arguments, "action": action} if vscode else {"action": action, "arguments": arguments}
    request = Request("http://127.0.0.1:48911/" + ("vscode" if vscode else "request"),
        method="POST", data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + token})
    opener=build_opener(ProxyHandler({}))
    # UU may bootstrap its remote endpoint or upload one bounded PNG. Keep the
    # outer read deadline longer than the endpoint's acknowledgement deadline.
    if action=='uu' and arguments.get('action')=='screenshot':request_timeout=95
    elif action=='uu' and arguments.get('action') in {'connect','keyboard_start'}:request_timeout=110
    else:request_timeout=60 if (action=='todesk' or action=='uu' and arguments.get('action') in {'type','screenshot','close','disconnect'}) else 30
    try:
        response=opener.open(request,timeout=request_timeout)
    except URLError as exc:
        # Restart only after a connection-refused error, never replay an action
        # with an unknown result or activate the user's apps for a status read.
        import errno
        reason=getattr(exc,'reason',None)
        refused=getattr(reason,'winerror',None)==10061 or getattr(reason,'errno',None)==errno.ECONNREFUSED
        if not refused:raise
        if action in {'status','cleanup'}:
            return {'ok':True,'backend':'local_background','result':{
                'ok':True,'created':False,'state':'idle_stopped','cleanup_complete':True,
                'already_stopped':True,'host_input_used':False}}
        from .windows_user_process import run_background_command_as_active_user, is_local_system
        command=[str(root/'.venv'/'Scripts'/'python.exe'),str(root/'06_web_tools'/'web_tools'/'background_launcher.py')]
        if is_local_system():
            result=run_background_command_as_active_user(command,root,10)
        else:
            import subprocess
            result=subprocess.run(command,cwd=str(root),stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,timeout=10,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        if getattr(result,'return_code',getattr(result,'returncode',None))!=0:raise RuntimeError('Background user-session launcher failed')
        deadline=time.monotonic()+5
        while True:
            try:
                response=opener.open(request,timeout=request_timeout)
                break
            except URLError as later:
                if isinstance(later,HTTPError) or time.monotonic()>=deadline:raise
                time.sleep(.2)
    with response:
        raw = response.read(4 * 1024 * 1024 + 1)
    if len(raw) > 4 * 1024 * 1024:
        raise ValueError("Background desktop response exceeds size limit")
    value = json.loads(raw)
    if not isinstance(value, dict) or value.get("backend") != "local_background":
        raise ValueError("Background desktop returned an invalid response")
    return value


def _error_source(response: dict):
    """Find project error records, never stringify arbitrary exceptions."""
    pending=[response.get('result'),response.get('error'),response]
    for _ in range(12):
        if not pending:break
        candidate=pending.pop(0)
        if not isinstance(candidate,dict):continue
        code=candidate.get('error_code') or candidate.get('code')
        if isinstance(code,str) and re.fullmatch(r'[a-z][a-z0-9_]{0,119}',code):
            return candidate,code
        nested=candidate.get('error')
        if isinstance(nested,dict):pending.append(nested)
    return None,None


def _metadata(*sources):
    result={}
    for source in sources:
        if not isinstance(source,dict):continue
        for name in ('phase','request_id','diagnostics'):
            value=source.get(name)
            if name in result:continue
            if name=='diagnostics' and isinstance(value,dict):result[name]=value
            elif name=='phase' and isinstance(value,str) and re.fullmatch(r'[a-z][a-z0-9_]{0,79}',value):result[name]=value
            elif name=='request_id' and isinstance(value,str) and re.fullmatch(r'[A-Za-z0-9_.:-]{1,160}',value):result[name]=value
    return result


def _may_have_run(*sources):
    for source in sources:
        if not isinstance(source,dict):continue
        for candidate in (source,source.get('evidence')):
            if isinstance(candidate,dict) and type(candidate.get('action_may_have_run')) is bool:
                return candidate['action_may_have_run']
    # No action-name inference: transport failures can leave a write's outcome
    # unknown, whereas validation failures can explicitly prove no dispatch.
    return None


async def call(settings, action: str, arguments: dict | None = None, *, vscode: bool = False) -> dict:
    metadata={};may_have_run=None
    try:
        response = await asyncio.to_thread(_request, settings, action, arguments or {}, vscode=vscode)
        ok = response.get("ok") is True
        data = response.get("result")
        if ok:
            error=None
            metadata=_metadata(data,response)
            may_have_run=_may_have_run(data,response)
        else:
            source,code=_error_source(response)
            if source is None:
                data=None
                error={'code':'background_action_unavailable',
                       'message':'后台操作未完成；不会回退到用户桌面。'}
            else:
                message=source.get('message')
                if not isinstance(data,dict):
                    data=response.get('error') if isinstance(response.get('error'),dict) else source
                metadata=_metadata(source,data,response)
                error={'code':code,'message':message[:1800] if isinstance(message,str) and message else '后台操作未完成；不会回退到用户桌面。',**metadata}
                may_have_run=_may_have_run(data,source,response)
    except Exception:
        ok, data = False, None
        metadata={};may_have_run=None
        error = {"code": "local_background_unavailable", "message": "本机后台桌面不可用；不会回退到用户桌面。"}
    captures=[]
    if ok and isinstance(data,dict) and isinstance(data.get('capture'),dict):
        capture=data['capture'];p=Path(capture.get('path',''))
        private_root=Path(settings.project_root)/'08_web_data'/'background_desktop'/'captures'
        if p.is_absolute() and p.resolve().is_relative_to(private_root.resolve()) and p.is_file():
            captures=[{**capture,'private':True}]
    return {"ok": ok, "status": "completed" if ok else "failed", "terminal": True,
            "task_id": None, "operation": "vscode_background" if vscode else "background_desktop",
            "result": {"summary": "本机后台操作已返回结果" if ok else error["message"], "data": data},
            "artifacts": captures, "error": error, "error_code": error["code"] if error else None,
            "retryable": False, "evidence": {"backend": "local_background", "host_input_used": False,
                "action_may_have_run": may_have_run, **metadata}}


async def windows_action(settings, action: str, arguments: dict | None = None) -> dict:
    args = arguments or {}
    if action == "ControlStatus":
        result = await call(settings, "status")
        if result["ok"]:
            from .desktop_policy import load_desktop_policy
            policy = load_desktop_policy(settings)
            data = result["result"]["data"]
            data.update(mode=policy["mode"], host_input_allowed=False,
                        foreground_available_on_request=policy["foreground_available_on_request"],
                        effective_execution_mode="background",
                        isolated_desktop_ready=data.get("created") is True, backend="local_background")
        return result
    if action == "Snapshot":
        return await call(settings, "snapshot")
    if action == "Screenshot":
        return await call(settings,"screenshot")
    if action == "App" and args.get("mode", "launch") == "launch":
        name = str(args.get("name", "")).strip().casefold()
        if name in {"visual studio code", "vs code", "code", "code.exe"}:
            name = "vscode"
        elif name in {"microsoft edge","msedge","msedge.exe"}:name="edge"
        elif name in {"wps office","wps文字","wps.exe"}:name="wps"
        return await call(settings, "launch_app", {"app": name})
    if action in {"Click", "Type", "Scroll"} and args.get("label") is not None and args.get("loc") is None:
        return await call(settings, action.casefold(), args)
    return {"ok": False, "status": "failed", "terminal": True, "task_id": None,
            "operation": "background_desktop", "result": {"summary": "", "data": None}, "artifacts": [],
            "error": {"code": "background_pattern_required", "message": "隐藏桌面操作需要应用接口或控件模式；截图请使用 screenshot。物理坐标、全局按键和切换用户前台不会执行。"},
            "error_code": "background_pattern_required", "retryable": False,
            "evidence": {"host_input_used": False, "action_may_have_run": False}}
