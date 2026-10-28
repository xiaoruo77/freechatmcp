"""Use the existing browser's authenticated native IPC before legacy extension.

The bundled --mcp-stdio launcher owns an independent browser. It is deliberately
not used as an attachment fallback. Lightpanda requires a supported live native
interface and is reported unavailable until one is actually discovered.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import json
from pathlib import Path
import time
from typing import Any
import uuid


class NativeAdapterError(ValueError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def _engine(value: Any) -> str:
    return "lightpanda" if "lightpanda" in str(value).casefold() else "chromium"


def _structured(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    for key in ("structured", "structuredContent"):
        if isinstance(value.get(key), dict):
            return value[key]
    return value


def _native_result(task: dict[str, Any]) -> dict[str, Any]:
    value = task.get("result")
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, dict):
                return parsed
        except ValueError:
            return {"text": value}
    return value if isinstance(value, dict) else {}


def _public_error(code: str, message: str, *, task_id: str | None = None,
                  unknown: bool = False, native_state: str | None = None,
                  native_task: dict[str, Any] | None = None) -> dict[str, Any]:
    # Never include pipe names, discovery dictionaries, request payloads or
    # exception strings. The native client retains safe diagnostic codes.
    return {"ok": False, "verified": False,
            "error": {"code": code, "message": message},
            "native_task_id": task_id, "action_may_have_run": unknown,
            "native_state": native_state,
            "native_terminal": native_state in {"succeeded", "failed", "timed_out", "cancelled"},
            "native_task": _public_task(native_task or {}),
            "retryable": False}


def _public_task(task: dict[str, Any]) -> dict[str, Any]:
    return {key: task[key] for key in ("id", "taskId", "task_id", "state", "action", "tab_id", "tabId", "errorCode") if key in task}


_CEF_OBSERVATION_FUNCTION = r'''function observed(selector){
 const hash=value=>{let n=2166136261;for(const c of String(value))n=Math.imul(n^c.charCodeAt(0),16777619);return n>>>0;};
 const nodes=document.querySelectorAll(selector);const el=nodes.length===1?nodes[0]:null;
 const text=String(document.body&&document.body.innerText||"").slice(0,100000);
 return {url_hash:hash(location.href),url_length:String(location.href).length,
   body_hash:hash(text),body_length:text.length,match_count:nodes.length,
   checked:el&&typeof el.checked==="boolean"?el.checked:null,
   selected_index:el&&typeof el.selectedIndex==="number"?el.selectedIndex:null,
   expanded:el?el.getAttribute("aria-expanded"):null,pressed:el?el.getAttribute("aria-pressed"):null,
   value_length:el&&typeof el.value==="string"?el.value.length:null};
}'''

_CEF_CLICK_JS = r'''(()=>{const args=__ARGUMENTS__;
__OBSERVATION__
let dispatched=false;
try{
 const nodes=document.querySelectorAll(args.selector);
 if(nodes.length!==1)return JSON.stringify({verified:false,action_may_have_run:false,error:{code:"native_click_target_ambiguous"}});
 const el=nodes[0];
 if(el.disabled||el.getAttribute("aria-disabled")==="true"||el.type==="password"||el.type==="file")return JSON.stringify({verified:false,action_may_have_run:false,error:{code:"native_click_target_denied"}});
 const before=observed(args.selector);dispatched=true;el.click();const after=observed(args.selector);
 return JSON.stringify({helper:"xiaoruo1-cef-semantic-click/1",click_dispatched:true,
  verified:JSON.stringify(before)!==JSON.stringify(after),before,after});
}catch(_){return JSON.stringify({verified:false,action_may_have_run:dispatched,error:{code:"native_click_helper_unavailable"}});}
})()'''

_CEF_CLICK_OBSERVE_JS = r'''(()=>{const args=__ARGUMENTS__;
__OBSERVATION__
try{const after=observed(args.selector);return JSON.stringify({helper:"xiaoruo1-cef-click-observation/1",verified:JSON.stringify(args.before)!==JSON.stringify(after),after});}
catch(_){return JSON.stringify({verified:false,error:{code:"native_click_observation_unavailable"}});}
})()'''

_CEF_SCROLL_JS = r'''(()=>{const args=__ARGUMENTS__;
 const root=document.scrollingElement||document.documentElement;
 const before={x:window.scrollX,y:window.scrollY};
 const maximum={x:Math.max(0,root.scrollWidth-root.clientWidth),y:Math.max(0,root.scrollHeight-root.clientHeight)};
 const expected={x:Math.min(args.x,maximum.x),y:Math.min(args.y,maximum.y)};
 window.scrollTo({left:args.x,top:args.y,behavior:"instant"});
 const after={x:window.scrollX,y:window.scrollY};
 const verified=Math.abs(after.x-expected.x)<=1&&Math.abs(after.y-expected.y)<=1;
 return JSON.stringify({helper:"xiaoruo1-cef-scroll/1",verified,requested:{x:args.x,y:args.y},expected,maximum,
  before,after,scrolled:before.x!==after.x||before.y!==after.y,
  at_boundary:expected.x!==args.x||expected.y!==args.y});
})()'''


def _cef_helper_script(kind: str, arguments: dict[str, Any]) -> str:
    if kind in {"click", "observe_click"}:
        selector = arguments.get("selector")
        if not isinstance(selector, str) or not selector or len(selector) > 2000:
            raise NativeAdapterError("invalid_argument", "Semantic native click requires one bounded observed CSS selector")
        values = {"selector": selector}
        if kind == "observe_click":
            before = arguments.get("before")
            if not isinstance(before, dict):
                raise NativeAdapterError("invalid_argument", "Click observation requires its original bounded state")
            values["before"] = before
        template = _CEF_CLICK_JS if kind == "click" else _CEF_CLICK_OBSERVE_JS
        return template.replace("__OBSERVATION__", _CEF_OBSERVATION_FUNCTION).replace("__ARGUMENTS__", json.dumps(values, ensure_ascii=True))
    if kind == "scroll":
        if any(type(arguments.get(key)) is not int or not 0 <= arguments[key] <= 100000 for key in ("x", "y")):
            raise NativeAdapterError("invalid_argument", "Native scroll coordinates must be bounded nonnegative integers")
        return _CEF_SCROLL_JS.replace("__ARGUMENTS__", json.dumps({"x": arguments["x"], "y": arguments["y"]}))
    raise NativeAdapterError("unsupported", "Only the fixed semantic click and scroll helpers are available")


class NativeXiaoruoBrowserAdapter:
    """Short-lived authenticated IPC workflows; never launch another browser.

    The unified facade supplies its existing authorization, takeover gate,
    durable operation receipt and per-target fence. Native failure never falls
    through to the extension after a task might have entered the native queue.
    """

    def __init__(self, data_root: Path | None = None, discovery=None, client_factory=None):
        if discovery is None or client_factory is None:
            from .native_browser_ipc import NativeBrowserDiscovery, NativeBrowserClient
            discovery = discovery or NativeBrowserDiscovery()
            client_factory = client_factory or NativeBrowserClient
        self.discovery = discovery
        self.client_factory = client_factory
        self.data_root = Path(data_root) if data_root is not None else Path(__file__).resolve().parents[2] / "08_web_data"
        self._instances: dict[str, Any] = {}
        self._capabilities: dict[str, tuple[float, dict[str, Any]]] = {}

    async def native_instances(self, timeout: float = 10) -> dict[str, Any]:
        deadline = asyncio.get_running_loop().time() + timeout
        instances = await asyncio.to_thread(self.discovery.discover)
        result = []
        current = {}
        for instance in instances:
            descriptor = dict(instance.public_descriptor())
            identifier = descriptor.get("browser_id") or "native_" + instance.instance_id
            generation = descriptor.get("connection_generation") or descriptor.get("generation")
            if not generation:
                # Discovery must provide a validated process-generation identity;
                # a PID alone can be reused after the browser has exited.
                continue
            cache_key = identifier + ":" + str(generation)
            cached = self._capabilities.get(cache_key)
            try:
                if cached and time.monotonic() - cached[0] < 30:
                    caps = cached[1]
                else:
                    remaining = deadline - asyncio.get_running_loop().time()
                    if remaining <= 0:
                        break
                    caps = _structured(await self.client_factory(instance).capabilities(timeout=remaining))
                    self._capabilities[cache_key] = (time.monotonic(), caps)
            except Exception as error:
                # A busy single-client pipe does not trigger another browser,
                # credential export, or an unverified extension fallback.
                caps = cached[1] if cached else {}
                busy = getattr(error, "code", "") in {"native_ipc_busy", "native_ipc_timeout"}
                if not busy and not cached:
                    continue
                descriptor["busy"] = busy
            public = {"browser_id": identifier, "connection_generation": str(generation),
                "browser_kind": "xiaoruo_browser", "browser_family": "native",
                "adapter": "native_ipc", "transport": "windows_named_pipe",
                "stable_identity": True, "connected": True,
                "busy": descriptor.get("busy", False),
                "native_pid": descriptor.get("native_pid", descriptor.get("process_id", descriptor.get("pid", instance.pid))),
                "version": descriptor.get("version", "v72"),
                "engine": "cef_and_lightpanda", "capabilities": caps,
                "supported_actions": self._supported(caps),
                "limitations": ["Capabilities are checked again for the selected tab engine", "Unsupported operations never fall back after native dispatch"]}
            result.append(public)
            current[identifier] = instance
        self._instances = current
        return {"ok": True, "status": "completed", "browsers": result}

    @staticmethod
    def _supported(caps: dict[str, Any]) -> list[str]:
        # The runtime capability shape is normalized by _supports_engine. The
        # transport itself provides instance, tabs and task status independently
        # of the selected page engine.
        values = ["status", "list_tabs", "task_status"]
        for wire, native in {"open_tab": "new_tab", "close_tab": "close_tab",
            "activate_tab": "select_tab", "navigate": "navigate", "read": "read_page",
            "click": "click", "type": "fill", "press": "press", "select": "select_option",
            "scroll": "scroll", "wait": "read_page", "screenshot": "screenshot"}.items():
            if any(NativeXiaoruoBrowserAdapter._supports_engine(caps, engine, native) for engine in ("chromium", "lightpanda")):
                values.append(wire)
        return values

    @staticmethod
    def _supports_engine(caps: dict[str, Any], engine: str, action: str) -> bool:
        # v72 exposes common capability flags plus per-engine overrides. Page
        # screenshot has a distinct pixel/reader meaning and is never coerced.
        aliases = {"new_tab": "tabs", "close_tab": "tabs", "select_tab": "tabs",
                   "navigate": "navigation", "read_page": "readPage",
                   "select_option": "selectOption", "screenshot_window": "screenshotWindow"}
        name = aliases.get(action, action)
        entry = caps.get("cef" if engine == "chromium" else "lightpanda", {})
        if engine == "lightpanda" and action in {"read_page", "click", "fill", "press", "select_option", "scroll", "screenshot"} and isinstance(entry, dict) and name not in entry:
            return False
        if isinstance(entry, dict) and name in entry:
            value = entry[name]
        else:
            value = caps.get(name)
        if action == "screenshot":
            return engine == "chromium" and value in (True, "rendered-page-png")
        if value is True or value == "supported":
            return True
        if value is False or value == "unsupported":
            return False
        # Also accept an explicitly shaped matrix in tests/future compatible
        # versions; an unknown value remains unsupported.
        matrix = caps.get("engines", caps.get("capabilities", {}))
        if isinstance(matrix, list):
            matches = [item for item in matrix if isinstance(item, dict) and _engine(item.get("engine", item.get("name"))) == engine]
            entry = matches[0] if len(matches) == 1 else {}
        elif isinstance(matrix, dict):
            entry = matrix.get(engine, matrix.get("cef" if engine == "chromium" else engine, {}))
        else:
            entry = {}
        if isinstance(entry, dict):
            actions = entry.get("actions", entry)
            if isinstance(actions, list):
                return action in actions
            if isinstance(actions, dict):
                value = actions.get(action)
                return value is True or value == "supported" or (isinstance(value, dict) and value.get("supported") is True)
        return False

    @staticmethod
    def _tabs(value: dict[str, Any]) -> list[dict[str, Any]]:
        tabs = []
        for item in value.get("tabs", []):
            if not isinstance(item, dict) or not isinstance(item.get("id"), (int, str)):
                continue
            tabs.append({**item, "tab_id": str(item["id"]),
                "committed_url": str(item.get("url") or ""),
                "url": str(item.get("url") or item.get("pendingUrl") or ""),
                "pending_url": str(item.get("pendingUrl") or ""),
                "document_ready": item.get("documentReady") is True,
                "engine": _engine(item.get("engineMode", item.get("engine")))})
        return tabs

    async def native_dispatch(self, browser_id: str, generation: str, method: str,
                              payload: dict[str, Any], timeout: float) -> dict[str, Any]:
        deadline = asyncio.get_running_loop().time() + timeout
        def remaining():
            value = deadline - asyncio.get_running_loop().time()
            if value <= 0:
                raise NativeAdapterError("native_adapter_timeout", "The native adapter deadline expired")
            return value
        inventory = await self.native_instances(timeout=min(remaining(), 10))
        matches = [item for item in inventory["browsers"] if item["browser_id"] == browser_id and item["connection_generation"] == generation]
        if len(matches) != 1:
            raise NativeAdapterError("native_instance_stale", "The selected native browser instance or process generation is unavailable")
        instance = self._instances[browser_id]
        client = self.client_factory(instance)
        if method == "list_tabs":
            native = _structured(await client.tabs(timeout=remaining()))
            return {"tabs": self._tabs(native), "active_tab_id": str(native.get("activeTabId", "")), "verified": True, "adapter": "native_ipc"}
        if method == "task_status":
            task_id = payload.get("task_id")
            if not isinstance(task_id, str) or not task_id.startswith("xr-" + str(instance.pid) + "-"):
                return _public_error("native_task_not_owned", "Task does not belong to the selected native browser process")
            response = _structured(await client.call_tool("xiaoruo_browser_task_status", {"task_id": task_id}, timeout=remaining()))
            task = response.get("task", response)
            state = task.get("state") if isinstance(task, dict) else None
            return {"verified": True, "native_task_id": task_id, "native_state": state,
                "native_terminal": state in {"succeeded", "failed", "timed_out", "cancelled"},
                "native_task_succeeded": state == "succeeded", "native_task": _public_task(task)}
        mapping = {"open_tab": "new_tab", "close_tab": "close_tab", "activate_tab": "select_tab",
            "navigate": "navigate", "read_page": "read_page", "click": "click", "type_text": "fill",
            "press_key": "press", "select_option": "select_option", "scroll": "scroll", "wait": "read_page",
            "screenshot": "screenshot"}
        if method not in mapping:
            return _public_error("unsupported", "This browser action is not exposed by the native adapter")
        native_action = mapping[method]
        args = dict(payload)
        args.pop("timeout", None)
        tab_id = args.pop("tab_id", None)
        target = None
        if method != "open_tab":
            native_tabs = _structured(await client.tabs(timeout=remaining()))
            targets = [item for item in self._tabs(native_tabs) if item["tab_id"] == str(tab_id)]
            if len(targets) != 1:
                return _public_error("tab_not_found", "The exact native tab ID is absent; no other tab was selected")
            target = targets[0]
            args["tab_id"] = int(target["id"])
        requested_engine = args.get("engine")
        if requested_engine is not None and str(requested_engine).casefold() not in {"auto", "cef", "chromium", "lightpanda"}:
            return _public_error("unsupported", "Only the existing CEF default or Lightpanda engine can be explicitly selected")
        engine = _engine(args.get("engine", (target or {}).get("engine", "chromium")))
        if target is not None and requested_engine is not None and engine != target["engine"]:
            return _public_error("native_engine_change_denied", "Existing-tab engine changes require a separate state-loss authorization; create an isolated tab for another engine")
        mode = args.pop("mode", "page") if method == "screenshot" else None
        if method == "screenshot" and mode == "window":
            native_action = "screenshot_window"
        if mode not in (None, "page", "window"):
            return _public_error("invalid_argument", "Screenshot mode must be page or window")
        if mode == "window" and (target or {}).get("active") is not True:
            return _public_error("native_window_target_inactive", "Whole-window capture requires the exact selected native tab to be active")
        caps = matches[0]["capabilities"]
        if not self._supports_engine(caps, engine, native_action):
            return _public_error("unsupported", "The selected engine does not advertise this native capability")
        if method == "open_tab" or "engine" in args:
            # The shipped v72 schema uses auto for its CEF default. The docs'
            # chromium example is not a valid runtime enum on this build.
            args["engine"] = "auto" if engine == "chromium" else "lightpanda"
        if args.pop("frame_id", 0) != 0 or args.pop("shadow_path", None):
            return _public_error("unsupported", "The native API accepts native selectors, not extension frame IDs or shadow-path arrays")
        by = args.pop("by", None)
        if by not in (None, "css") or args.pop("name", None):
            return _public_error("unsupported", "The native adapter requires an observed exact CSS/native selector")
        args.pop("exact", None)
        if method == "type_text" and args.get("clear", True) is not True:
            return _public_error("unsupported", "Native fill cannot safely emulate append typing")
        args.pop("clear", None)
        if method == "type_text":
            args["value"] = args.pop("text", "")
        args.pop("settle_ms", None)
        args.pop("max_chars", None)
        args.pop("full_page", None)
        args.pop("include_frames", None)
        args.pop("mode", None)
        if method == "select_option":
            args["value"] = args.pop("option", "")
            if args.pop("option_by", "value") not in {"value", "text", "label"}:
                return _public_error("unsupported", "Native select_option does not accept index selection")
        if method == "scroll":
            direction = args.pop("direction", "down")
            amount = abs(int(args.pop("pixels", 600)))
            if direction not in {"up", "down", "left", "right"}:
                return _public_error("invalid_argument", "Invalid native scroll direction")
            # v72 scroll accepts nonnegative absolute offsets, not wheel events.
            # Explicit x/y is exact; directional defaults move to that offset.
            args.setdefault("x", amount if direction == "right" else 0)
            args.setdefault("y", amount if direction == "down" else 0)
        if method == "wait":
            milliseconds = args.pop("milliseconds", 1000)
            if type(milliseconds) is not int or not 0 <= milliseconds <= 120000 or milliseconds / 1000 >= remaining():
                return _public_error("invalid_argument", "Wait must leave time for a verified page state query")
            await asyncio.sleep(milliseconds / 1000)
        args = {key: value for key, value in args.items() if value is not None}
        if engine == "chromium" and method in {"click", "scroll"}:
            permissions = getattr(instance, "permissions", ())
            if "browser.script" not in permissions or caps.get("evaluate") is not True:
                return _public_error("unsupported", "Verified CEF semantic interaction requires the existing native evaluate capability and browser.script permission")
            return await self._verified_cef_operation(client, method, args, remaining, str(tab_id))
        response = await client.execute(native_action, args, timeout=max(.01, remaining() - .1))
        task = response.get("native_task", {})
        if not isinstance(task, dict):
            task = {}
        state = task.get("state")
        task_id = response.get("task_id") or task.get("id")
        if response.get("ok") is not True or state != "succeeded":
            return _public_error(str(task.get("errorCode") or (response.get("error") or {}).get("code") or "native_task_incomplete"),
                "Native task did not reach succeeded; inspect its task status before another write",
                task_id=task_id, unknown=state not in {"failed", "timed_out", "cancelled"},
                native_state=state or response.get("status"), native_task=task)
        result = _native_result(task)
        result.update({"verified": True, "native_task_id": task_id, "native_state": state,
                       "native_terminal": True, "native_task": _public_task(task),
                       "adapter": "native_ipc"})
        if tab_id is not None:
            result["tab_id"] = str(tab_id)
        if method in {"open_tab", "navigate", "activate_tab", "close_tab"}:
            try:
                after = self._tabs(_structured(await client.tabs(timeout=remaining())))
            except Exception:
                return _public_error("native_postcondition_unavailable", "Native task succeeded but its final tab state could not be queried; inspect this task and tab before another write", task_id=task_id, unknown=True, native_state="succeeded", native_task=task)
            if method == "open_tab":
                created = result.get("tab_id", result.get("tabId", task.get("tab_id", task.get("tabId"))))
                if created is None:
                    return _public_error("native_tab_id_missing", "Completed creation omitted its tab ID; inspect current tabs", task_id=task_id, unknown=True)
                result["tab_id"] = str(created)
                found = [item for item in after if item["tab_id"] == result["tab_id"]]
                wanted = payload.get("url")
                if len(found) != 1 or (wanted and found[0]["url"] != wanted) or (wanted and (found[0].get("loading") is not False or not found[0]["document_ready"])):
                    return _public_error("native_tab_state_unverified", "Created tab ID, effective URL or document readiness did not match; no replacement tab was guessed", task_id=task_id, unknown=True)
                result.update(found[0])
            elif method == "close_tab":
                if any(item["tab_id"] == str(tab_id) for item in after):
                    return _public_error("native_close_unverified", "Closed tab ID still exists", task_id=task_id, unknown=True)
                result["closed"] = True
            else:
                found = [item for item in after if item["tab_id"] == str(tab_id)]
                if len(found) != 1:
                    return _public_error("native_tab_state_unverified", "Native operation returned a missing tab ID", task_id=task_id, unknown=True)
                if method == "navigate" and (found[0]["url"] != payload.get("url") or found[0].get("loading") is not False or not found[0]["document_ready"]):
                    return _public_error("native_navigation_unverified", "Navigation URL or document readiness was not confirmed", task_id=task_id, unknown=True)
                if method == "activate_tab" and found[0].get("active") is not True:
                    return _public_error("native_activation_unverified", "Native tab selection did not become active", task_id=task_id, unknown=True)
                result.update(found[0])
        if method == "screenshot":
            return self._save_screenshot(response.get("images", []), result, mode)
        return result

    async def _verified_cef_operation(self, client, method: str, arguments: dict[str, Any], remaining, tab_id: str) -> dict[str, Any]:
        kind = "click" if method == "click" else "scroll"
        try:
            script = _cef_helper_script(kind, arguments)
        except NativeAdapterError as error:
            return _public_error(error.code, str(error))
        response = await client.execute("evaluate", {"tab_id": int(tab_id), "script": script}, timeout=max(.01, remaining() - .1))
        task = response.get("native_task") if isinstance(response.get("native_task"), dict) else {}
        state = task.get("state")
        task_id = response.get("task_id") or task.get("id") or task.get("taskId")
        if response.get("ok") is not True or state != "succeeded":
            return _public_error(str(task.get("errorCode") or (response.get("error") or {}).get("code") or "native_semantic_task_incomplete"),
                "Native semantic interaction did not reach succeeded; inspect its task before any new write",
                task_id=task_id, unknown=state not in {"failed", "timed_out", "cancelled"},
                native_state=state or response.get("status"), native_task=task)
        observed = _native_result(task)
        observation_tasks = []
        if method == "click" and observed.get("click_dispatched") is True and observed.get("verified") is not True:
            # The click itself is dispatched exactly once. Only bounded readonly
            # observations follow, allowing asynchronous page handlers to settle.
            observe_until = asyncio.get_running_loop().time() + min(2.0, max(0, remaining() - .2))
            before = observed.get("before")
            while isinstance(before, dict) and asyncio.get_running_loop().time() < observe_until:
                await asyncio.sleep(.1)
                script = _cef_helper_script("observe_click", {"selector": arguments["selector"], "before": before})
                probe = await client.execute("evaluate", {"tab_id": int(tab_id), "script": script}, timeout=max(.01, min(remaining() - .1, observe_until - asyncio.get_running_loop().time())))
                probe_task = probe.get("native_task") if isinstance(probe.get("native_task"), dict) else {}
                observation_tasks.append(probe.get("task_id") or probe_task.get("taskId") or probe_task.get("id"))
                if probe.get("ok") is not True or probe_task.get("state") != "succeeded":
                    break
                after = _native_result(probe_task)
                if after.get("verified") is True:
                    observed.update(verified=True, after=after.get("after"), verification="observed_change_after_single_semantic_click")
                    break
        if observed.get("error") and observed.get("action_may_have_run") is False:
            return _public_error(str(observed["error"].get("code", "native_semantic_refused")),
                "Native semantic helper refused this target before dispatch", task_id=task_id,
                unknown=False, native_state="succeeded", native_task=task)
        expected_helper = "xiaoruo1-cef-semantic-click/1" if method == "click" else "xiaoruo1-cef-scroll/1"
        verified = observed.get("helper") == expected_helper and observed.get("verified") is True
        # No ok=true is synthesized from evaluate acceptance. The unified facade
        # fences an executed but unverified operation until an independent read.
        return {**observed, "verified": verified, "tab_id": tab_id,
            "native_task_id": task_id, "native_state": "succeeded", "native_terminal": True,
            "native_task": _public_task(task), "native_observation_task_ids": observation_tasks,
            "native_verification_required": not verified, "action_may_have_run": True,
            "adapter": "native_ipc", "verification": observed.get("verification") or (
                "native_semantic_effect_observed" if verified else "semantic_action_dispatched_without_verified_effect")}

    def _save_screenshot(self, images: list[Any], result: dict[str, Any], mode: str | None) -> dict[str, Any]:
        valid = [image for image in images if isinstance(image, dict) and image.get("mimeType", image.get("mime_type")) == "image/png"]
        if len(valid) != 1:
            return _public_error("native_screenshot_missing", "Succeeded screenshot did not contain exactly one PNG", task_id=result.get("native_task_id"))
        encoded = valid[0].get("data", "")
        try:
            if not isinstance(encoded, str) or len(encoded) > 24 * 1024 * 1024:
                raise ValueError()
            body = base64.b64decode(encoded, validate=True)
            if not body.startswith(b"\x89PNG\r\n\x1a\n") or len(body) > 16 * 1024 * 1024:
                raise ValueError()
        except (ValueError, binascii.Error):
            return _public_error("native_screenshot_invalid", "Native screenshot was not a bounded PNG", task_id=result.get("native_task_id"))
        directory = self.data_root / "browser_screenshots"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / ("native-" + uuid.uuid4().hex + ".png")
        path.write_bytes(body)
        return {**result, "path": str(path), "mime_type": "image/png", "size_bytes": len(body),
                "screenshot_mode": mode, "pixel_scope": "native_window" if mode == "window" else "page"}


class XiaoruoBrowserAdapter:
    def __init__(self, client):
        self.client = client

    async def discover(self) -> dict[str, Any]:
        result = await self.client.browser_instances()
        browsers = [entry for entry in result.get('browsers', [])
                    if entry.get('browser_kind') == 'xiaoruo_browser']
        return {'ok': result.get('ok', True), 'browsers': browsers,
                'transport': 'existing_authenticated_extension',
                'starts_new_browser': False, 'exports_profile_data': False,
                'stdio_attach_supported': False, 'lightpanda_attach_supported': False}

    async def call(self, browser_id: str, action: str, *, tab_id: str | None = None,
                   arguments: dict[str, Any] | None = None, operation_id: str | None = None,
                   confirm: bool = False, timeout: int = 30000):
        inventory = await self.discover()
        matches = [item for item in inventory['browsers'] if item.get('browser_id') == browser_id]
        if len(matches) != 1:
            return {'ok': False, 'status': 'failed', 'browser_id': browser_id,
                    'error': {'code': 'xiaoruo_instance_unavailable',
                              'message': 'Select one currently connected manual Xiaoruo browser; no independent session was launched'},
                    'retryable': False}
        return await self.client.browser_control(browser_id=browser_id, action=action,
            tab_id=tab_id, arguments=arguments, operation_id=operation_id,
            confirm=confirm, timeout=timeout)