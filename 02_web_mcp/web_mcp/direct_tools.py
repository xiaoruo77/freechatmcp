"""Small adapters for persisted host decisions and optional desktop actions."""
from __future__ import annotations

from typing import Any
import hashlib
import json
import math
from pathlib import Path
import re
import stat
import time

from web_memory.goal_repository import GoalRepository
from web_tools.windows_mcp import WindowsMCPBridge, _validate_args
from web_tools.background_desktop_client import call as call_background
from .paths import resolve_workspace_path
from .result_contract import safe_error


TOOL_METADATA = {
    'remote_computer': {'description': 'Remote real-desktop control, always preferring the existing UU route. Only a confirmed unavailable UU connection with no GUI input dispatch may use the enrolled remote receiver. Never switch transport or repeat input after an uncertain write. Devices/status can be read without preparing an installer. Receiver commands are queued: poll command_status, then inspect a fresh screenshot; queued/dispatched/ready are not whole-task completion. No local foreground, pointer or keyboard is used.', 'scope':'desktop_control'},
    "todesk_control": {"description": "Strict background ToDesk adapter: attach an existing allowlisted viewer, read a real window screenshot, inspect status, or close that exact viewer. Background mouse/keyboard and automatic connection have NOT passed real acceptance and are rejected before dispatch. Never activate/restore/show windows or fall back to local/global input. False input_verified is a real limitation, not task completion. Preserve UU and ToDesk receiver services.", "scope": "desktop_control"},
    "background_desktop": {"description": "Use a same-machine hidden desktop without touching local foreground input. UU: action=uu, arguments={action:connect,arguments:{device_name:exact name}} starts a temporary graphical agent on the other PC through an authenticated UU terminal. The logged-in local UU GUI and receiving services stay on Default, never moved or closed. This uses real remote screen PNGs and remote pointer/keyboard, not a native UU video viewer. Subactions: status/devices/keyboard_status, keyboard_start, screenshot, click/scroll/drag, type(text)/key(keys), disconnect/close. Screenshot returns remote_foreground; use a stable operation_id for writes, capture again after mouse/focus changes, verify actual GUI output. Unknown writes keep their receipts. cleanup stops only this task's exact helper/terminal; it never disconnects shared devices or ends the local UU receiver. Other apps support list_apps/launch_app/UIA/Edge/WPS APIs. Finish with cleanup; idle cleanup applies.", "scope": "desktop_control"},
    "vscode_background": {"description": "Use local VS Code workspace APIs without focusing editors: read/write documents and Notebook cells with expected hashes. No arbitrary commands or terminals. Dirty detection covers only the dedicated instance.", "scope": "workspace"},
    "goal_start": {"description": "Persist the ChatGPT goal, acceptance and explicit plan without starting AI work.", "scope": "task"},
    "goal_status": {"description": "Read saved checkpoints, errors, artifacts and linked deterministic tasks.", "scope": "task"},
    "goal_checkpoint": {"description": "Persist one host observation with an idempotency key; no action is replayed.", "scope": "task"},
    "goal_finish": {"description": "Record an outcome; completion requires evidence for every acceptance criterion.", "scope": "task"},
    "goal_cancel": {"description": "Cancel this goal and request cancellation of its linked deterministic tasks.", "scope": "task"},
    "task_status": {"description": "Return the actual status of an existing Worker task immediately.", "scope": "task"},
    "task_log": {"description": "Read bounded ordered task logs with a next_cursor.", "scope": "task"},
    **{f"windows_{name}": {"description": f"Windows-MCP {action}; default follows the background policy. After explicit user foreground authorization pass execution_mode='foreground' and confirm=true for this call.", "scope": "desktop_read" if name in {"snapshot", "screenshot", "control_status"} else "desktop_control"}
       for name, action in {
           "snapshot": "Snapshot", "screenshot": "Screenshot", "control_status": "ControlStatus",
           "click": "Click", "type": "Type", "shortcut": "Shortcut", "scroll": "Scroll", "app": "App",
       }.items()},
}

WINDOWS_ACTIONS = {
    "windows_snapshot": "Snapshot", "windows_screenshot": "Screenshot", "windows_control_status": "ControlStatus",
    "windows_click": "Click", "windows_type": "Type", "windows_shortcut": "Shortcut", "windows_scroll": "Scroll", "windows_app": "App",
}


class DirectTools:
    def __init__(self, ctx):
        self.ctx = ctx
        self.goals = GoalRepository(ctx.db)
        self.desktop = WindowsMCPBridge(ctx.settings)
        self.task_status_callback = None
        self.remote_receiver_controller = None
        self.remote_controller_router = None

    @staticmethod
    def windows_request(arguments: dict[str, Any]):
        args = dict(arguments)
        confirm = args.pop("confirm", False)
        execution_mode = args.pop("execution_mode", "background")
        if not isinstance(execution_mode, str) or execution_mode not in {"background", "foreground"}:
            raise ValueError("execution_mode must be background or foreground")
        if "arguments" in args:
            native = args.pop("arguments")
            if args or not isinstance(native, dict):
                raise ValueError("Use one desktop arguments object without additional outer fields")
        else:
            native = args
        return native, execution_mode, confirm

    def validate_windows_request(self, name: str, arguments: dict[str, Any]):
        native, execution_mode, confirm = self.windows_request(arguments)
        _validate_args(WINDOWS_ACTIONS[name], native)
        return native, execution_mode, confirm

    async def invoke(self, name: str, arguments: dict[str, Any]) -> Any:
        if name in WINDOWS_ACTIONS:
            args, execution_mode, confirm = self.validate_windows_request(name, arguments)
            if TOOL_METADATA[name]["scope"] == "desktop_control" or execution_mode == "foreground":
                if not self.ctx.settings.admin_mode or not self.ctx.settings.windows_mcp_control_enabled:
                    raise PermissionError("Windows desktop control is disabled; enable it only after user approval")
                if confirm is not True:
                    raise PermissionError("Desktop control requires explicit confirm=true and user authorization")
            # The adapter independently validates its upstream whitelist/schema.
            if execution_mode == "foreground":
                return await self.desktop.call(WINDOWS_ACTIONS[name], args,
                    execution_mode="foreground", foreground_authorized=confirm is True)
            return await self.desktop.call(WINDOWS_ACTIONS[name], args)
        return await getattr(self, "_" + name)(**arguments)

    async def _background_desktop(self, action: str = "status", arguments: dict[str, Any] | None = None, confirm: bool = False) -> dict[str, Any]:
        allowed = {"status", "create", "list_apps", "list_windows", "launch_app", "snapshot", "screenshot", "edge", "wps", "uu", "todesk", "cleanup", "click", "type", "selection", "scroll", "toggle", "expand", "collapse", "set_range"}
        if action not in allowed:
            raise ValueError("Unsupported local background desktop action")
        if action=='todesk' and (arguments or {}).get('action') in {'click','type','key','scroll','drag'}:
            if not self.ctx.settings.admin_mode or confirm is not True:
                raise PermissionError('Background application changes require user authorization and confirm=true')
            message='ToDesk 后台键鼠尚未验证有效，已在派发前停止；不会切换前台或使用本机鼠标键盘。'
            return {'ok':False,'status':'failed','terminal':True,'operation':'todesk_control',
                    'result':{'summary':message,'data':{'background_only':True,'input_dispatched':False}},
                    'error_code':'background_input_unverified','error':{'code':'background_input_unverified','message':message},
                    'artifacts':[],'retryable':False,
                    'evidence':{'host_input_used':False,'action_may_have_run':False}}
        reading = action in {"status", "list_apps", "list_windows", "snapshot", "screenshot"} or (action in {"edge","wps"} and (arguments or {}).get("action") in {"status","read","list_tabs","screenshot"})
        reading = reading or (action == "uu" and (arguments or {}).get("action", "status") in {"status", "devices", "inspect", "screenshot", "ui_snapshot", "keyboard_status"})
        reading = reading or (action == 'todesk' and (arguments or {}).get('action','status') in {'status','devices','inspect','screenshot'})
        if not reading:
            if not self.ctx.settings.admin_mode or confirm is not True:
                raise PermissionError("Background application changes require user authorization and confirm=true")
        background_arguments = dict(arguments or {})
        nested = background_arguments.get("arguments")
        if action == "wps" and isinstance(nested, dict):
            nested = dict(nested)
            for label in ("document_path", "output_path"):
                if label in nested:
                    nested[label] = str(resolve_workspace_path(
                        nested[label], self.ctx.settings.allowed_workspaces, must_exist=label == "document_path"))
            background_arguments["arguments"] = nested
        elif action == "launch_app":
            for label in ("cwd", "document_path"):
                if label in background_arguments and background_arguments[label]:
                    background_arguments[label] = str(resolve_workspace_path(
                        background_arguments[label], self.ctx.settings.allowed_workspaces, must_exist=True))
        return await call_background(self.ctx.settings, action, background_arguments)

    async def _todesk_control(self, action: str = 'status', arguments: dict[str, Any] | None = None,
                              confirm: bool = False) -> dict[str, Any]:
        return await self._background_desktop('todesk', {'action':action,'arguments':arguments or {}},confirm)

    @staticmethod
    def _remote_envelope(raw):
        """Keep receiver queue states intact instead of promoting them to done."""
        raw=dict(raw)
        status=str(raw.get('status','error'))
        ok=raw.get('ok') is True
        capture=raw.get('capture')
        artifacts=raw.get('artifacts',[])
        if isinstance(capture,dict):artifacts=[{**capture,'private':True}]
        code=raw.get('error_code')
        return {**raw,'ok':ok,'status':status,'terminal':raw.get('terminal') is True,
                'task_id':None,'operation':'remote_computer','remote_tool':'remote_computer',
                'result':{'summary':str(raw.get('message') or 'remote_computer: '+status),'data':raw},
                'artifacts':artifacts,
                'error':None if ok else {'code':code or 'remote_control_failed','message':str(raw.get('message') or code or 'Remote operation failed')},
                'retryable':False,'evidence':{'background_only':True,'host_input_used':False,
                    'foreground_input_used':False,'remote_effect_verified':raw.get('remote_effect_verified') is True}}

    async def _remote_computer(self, action: str = 'status', arguments: dict[str,Any] | None = None,
                               confirm: bool = False):
        args={} if arguments is None else arguments
        if not isinstance(args,dict):raise ValueError('Use one arguments object')
        reading=action in {'devices','status','session_status','screenshot','command_status'}
        if not reading and (not self.ctx.settings.admin_mode or confirm is not True):
            raise PermissionError('Remote computer changes require user authorization and confirm=true')
        core=getattr(self,'remote_receiver_controller',None)
        if action=='enroll':
            raise ValueError('Receiver enrollment packaging is not included in this source release')
        if action == 'prepare_package':
            raise ValueError('Preparing a machine-specific receiver installer is not supported in this source release')
        if getattr(self,'remote_controller_router',None) is None:
            from web_tools.remote_control_route import PreferredRemoteController
            path=Path(self.ctx.settings.extra.get('remote_receiver_config') or Path(self.ctx.settings.project_root)/'07_web_config'/'remote_receiver.json')
            config=json.loads(path.read_text(encoding='utf-8-sig')) if path.is_file() else {}
            async def uu_call(subaction,values,authorized=False):
                return await call_background(self.ctx.settings,'uu',{'action':subaction,'arguments':values})
            aliases={name:{key:value[key] for key in ('uu_device_name','receiver_device_name')}
                     for name,value in config.get('devices',{}).items()}
            self.remote_controller_router=PreferredRemoteController(uu_call,core,aliases)
        raw=await self.remote_controller_router.perform(action,args,confirm)
        return self._remote_envelope(raw)

    async def _vscode_background(self, action: str = "status", arguments: dict[str, Any] | None = None, confirm: bool = False) -> dict[str, Any]:
        if action not in {"status", "read_document", "write_document", "read_notebook", "patch_notebook_cells", "save_document"}:
            raise ValueError("Unsupported VS Code workspace operation")
        if action not in {"status", "read_document", "read_notebook"} and confirm is not True:
            raise PermissionError("VS Code edits require user authorization and confirm=true")
        return await call_background(self.ctx.settings, action, arguments or {}, vscode=True)

    async def _goal_start(self, goal: str, acceptance: list[str], workspace: str = ".", plan: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        root = resolve_workspace_path(workspace, self.ctx.settings.allowed_workspaces, must_exist=True)
        if not root.is_dir():
            raise ValueError("workspace must be a directory")
        value = await self.goals.create(goal, acceptance, str(root), plan)
        return {**value, "goal_id": value["id"]}

    async def _goal_status(self, goal_id: str) -> dict[str, Any] | None:
        value = await self.goals.get(goal_id)
        return {**value, "goal_id": goal_id} if value else None

    async def _goal_checkpoint(self, goal_id: str, step_id: str, observation: dict[str, Any], next_intent: str | None = None,
                               idempotency_key: str | None = None, task_id: str | None = None, status: str = "completed",
                               artifacts: list[str] | None = None, error: dict[str, Any] | None = None, supersedes_task_id: str | None = None) -> dict[str, Any]:
        value = await self.goals.checkpoint(goal_id, step_id, observation, next_intent, idempotency_key, task_id, status, artifacts, error, supersedes_task_id)
        return {**value, "goal_id": goal_id}

    async def _goal_finish(self, goal_id: str, outcome: str, evidence: dict[str, Any]) -> dict[str, Any]:
        value = await self.goals.finish(goal_id, outcome, evidence)
        from web_tools.background_desktop_client import configured
        cleanup = await call_background(self.ctx.settings,"cleanup") if configured(self.ctx.settings) else None
        return {**value, "goal_id": goal_id, "background_cleanup": cleanup}

    async def _goal_cancel(self, goal_id: str) -> dict[str, Any]:
        value = await self.goals.cancel(goal_id)
        return {**value, "goal_id": goal_id}

    async def _task_status(self, task_id: str) -> dict[str, Any] | None:
        if self.task_status_callback is not None:
            return await self.task_status_callback(task_id, timeout_seconds=0)
        value = await self.ctx.tasks.get(task_id)
        return {**value, "task_id": task_id} if value else None

    async def _task_log(self, task_id: str, cursor: int = 0, limit: int = 50) -> dict[str, Any]:
        if not await self.ctx.tasks.get(task_id):
            raise FileNotFoundError(task_id)
        if cursor < 0:
            raise ValueError("cursor must be nonnegative")
        limit = max(1, min(int(limit), 100))
        rows = await self.ctx.db.fetchall("SELECT id,level,message,created_at FROM task_logs WHERE task_id=? AND id>? ORDER BY id LIMIT ?", (task_id, cursor, limit + 1))
        page = [{**row, "message": safe_error(row["message"])} for row in rows[:limit]]
        return {"task_id": task_id, "entries": page, "next_cursor": page[-1]["id"] if len(rows) > limit else None}
