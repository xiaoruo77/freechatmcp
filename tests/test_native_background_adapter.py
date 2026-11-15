"""Background adapter unit contracts. No live browser or desktop input."""
from copy import deepcopy
import json
import shutil
import subprocess

import pytest

from web_tools import xiaoruo_browser_adapter as module
from web_tools.xiaoruo_browser_adapter import NativeXiaoruoBrowserAdapter, _cef_background_script


BROWSER, GENERATION = "native_background_fixture", "background-generation"


class Instance:
    pid = 42
    instance_id = "background_fixture"
    permissions = ("browser.script",)

    def public_descriptor(self):
        return {"browser_id": BROWSER, "connection_generation": GENERATION}


class Discovery:
    def discover(self):
        return [Instance()]


class Client:
    def __init__(self):
        self.calls, self.queries = [], 0
        self.active = 1
        self.target = {"id": 7, "url": "https://example.test/editor", "pendingUrl": "",
                       "loading": False, "documentReady": True, "active": False, "engine": "CEF / Chromium"}
        self.caps = {"evaluate": True, "tabs": True, "readPage": True, "fill": True,
                     "press": True, "click": True, "scroll": True, "selectOption": True,
                     "cef": {"screenshot": "rendered-page-png", "fileUpload": True}}
        self.reply = None
        self.reply_queue = []
        self.change_after = None
        self.transient_reads = 0
        self.query_changes = {}

    async def capabilities(self, timeout=30):
        return deepcopy(self.caps)

    async def tabs(self, timeout=30):
        self.queries += 1
        change = self.query_changes.pop(self.queries, None)
        if change:
            change(self)
        target = deepcopy(self.target)
        if self.transient_reads:
            self.transient_reads -= 1
            target.update(loading=True, documentReady=False)
        return {"tabs": [{"id": 1, "url": "https://example.test/user", "active": True,
                          "engine": "CEF / Chromium"}, target], "activeTabId": self.active}

    async def execute(self, action, arguments, timeout=30, **kwargs):
        self.calls.append((action, deepcopy(arguments), kwargs))
        assert action in {"evaluate", "screenshot", "upload_file"}, "No system input or activation fallback"
        if self.change_after:
            self.change_after(self)
        if self.reply_queue:
            observed = deepcopy(self.reply_queue.pop(0))
        elif self.reply is not None:
            observed = deepcopy(self.reply)
        elif "xiaoruo1-cef-dom-observation/1" in arguments.get("script", ""):
            observed = {"helper": "xiaoruo1-cef-dom-observation/1", "url": self.target["url"],
                        "text": "current editor", "file_inputs": [], "observation_complete": True}
        elif "xiaoruo1-cef-scroll/1" in arguments.get("script", ""):
            observed = {"helper": "xiaoruo1-cef-scroll/1", "verified": True, "after": {"y": 300}}
        else:
            prefix = "(()=>{const args="
            values, _ = json.JSONDecoder().raw_decode(arguments["script"][len(prefix):])
            observed = {"helper": "xiaoruo1-cef-background-interaction/1", "verified": True,
                        "action_may_have_run": True, "before": {"body_hash": 1}, "after": {"body_hash": 2},
                        "observed_value": values.get("value"), "value_matches": "value" in values,
                        "input_semantics": "synthetic_dom_events_untrusted"}
        task = {"taskId": f"xr-42-{len(self.calls)}", "state": "succeeded", "result": json.dumps(observed)}
        return {"ok": True, "native_task": task, "task_id": task["taskId"]}


@pytest.fixture
def setup(tmp_path):
    client = Client()
    adapter = NativeXiaoruoBrowserAdapter(tmp_path, Discovery(), lambda instance: client)
    return adapter, client


async def call(adapter, method, **arguments):
    return await adapter.native_dispatch(BROWSER, GENERATION, method,
        {"tab_id": "7", "background_only": True, "expected_active_tab_id": "1", **arguments}, 5)


@pytest.mark.parametrize("method,args", [
    ("type_text", {"selector": "#title", "text": "new title"}),
    ("press_key", {"selector": "#tag", "key": "Enter"}),
    ("select_option", {"selector": "#category", "option": "science"}),
    ("click", {"selector": "#save"}),
    ("scroll", {"y": 300}),
    ("read_page", {}), ("wait", {"milliseconds": 0}),
])
async def test_background_never_dispatches_raw_input_or_activation_and_checks_each_task(setup, method, args):
    adapter, client = setup
    result = await call(adapter, method, **args)
    assert result["verified"] and result["execution_mode"] == "native_background"
    assert len(client.calls) == 1 and client.calls[0][0] == "evaluate"
    assert client.queries >= 4  # initial exact target, fence, before task, after task
    assert set(client.calls[0][1]) == {"tab_id", "script"}
    assert "background_only" not in client.calls[0][1]["script"]
    assert "expected_active_tab_id" not in client.calls[0][1]["script"]


@pytest.mark.parametrize("method,args", [("activate_tab", {}), ("navigate", {"url": "https://example.test/"}),
    ("open_tab", {}), ("close_tab", {}), ("screenshot", {"mode": "window"}),
    ("press_key", {"selector": "#tag", "key": "Ctrl+V"})])
async def test_foreground_operations_are_refused_without_dispatch(setup, method, args):
    adapter, client = setup
    result = await call(adapter, method, **args)
    assert result["error"]["code"] == "foreground_required" and not result["action_may_have_run"]
    assert not client.calls


@pytest.mark.parametrize("changed", ["active_id", "active_flag", "loading", "url", "pending", "engine"])
async def test_preflight_fails_closed_for_changed_or_active_target(setup, changed):
    adapter, client = setup
    if changed == "active_id":
        client.active = 2
    else:
        key, value = {"active_flag": ("active", True), "loading": ("loading", True),
                      "url": ("documentReady", False), "pending": ("pendingUrl", "https://example.test/new"),
                      "engine": ("engine", "unknown-engine")}[changed]
        client.target[key] = value
    result = await call(adapter, "type_text", selector="#title", text="new title")
    assert not result["verified"] and not result["action_may_have_run"]
    assert not client.calls


@pytest.mark.parametrize("change", [lambda client: setattr(client, "active", 2),
    lambda client: client.target.update(url="https://example.test/new"),
    lambda client: client.target.update(active=True),
    lambda client: client.target.update(loading=True)])
async def test_postflight_retains_task_fence_and_never_replays_when_user_or_page_changes(setup, change):
    adapter, client = setup
    client.change_after = change
    result = await call(adapter, "type_text", selector="#title", text="new title")
    assert result["error"]["code"] == "native_background_interrupted"
    assert not result["verified"] and result["action_may_have_run"]
    assert result["native_task_id"] == "xr-42-1" and result["native_terminal"]
    assert len(client.calls) == 1


async def test_background_click_allows_same_origin_route_and_audits_transition(setup):
    adapter, client = setup
    def route(value):
        value.target.update(url="https://example.test/upload-manager/article?group=draft")
        value.transient_reads = 3
    client.change_after = route
    result = await call(adapter, "click", selector="#save")
    assert result["verified"] and result["url_transition"] is True
    assert result["action_may_have_run"] and len(client.calls) == 1


async def test_background_click_retains_dispatch_context_when_route_commits_before_observer(setup):
    adapter, client = setup
    client.reply_queue = [
        {"helper": "xiaoruo1-cef-background-interaction/1", "verified": False,
         "action_may_have_run": True, "action": "click",
         "before": {"body_hash": 1}, "after": {"body_hash": 1}},
        {"helper": "xiaoruo1-cef-click-observation/1", "verified": True,
         "after": {"body_hash": 2}},
    ]
    # Four queries see the old document, including the click's immediate
    # postflight. Only the observer's preflight sees the committed SPA route.
    client.query_changes[5] = lambda value: value.target.update(
        url="https://example.test/editor?group=draft&page=1")
    result = await call(adapter, "click", selector="#draft")
    assert result["verified"] and result["url_transition"] is True
    assert result["action_may_have_run"] and result["native_task_id"] == "xr-42-1"
    assert result["native_observation_task_ids"] == ["xr-42-2"]
    assert len(client.calls) == 2
    assert "xiaoruo1-cef-background-interaction/1" in client.calls[0][1]["script"]
    assert "xiaoruo1-cef-click-observation/1" in client.calls[1][1]["script"]


@pytest.mark.parametrize("query", [5, 6])
@pytest.mark.parametrize("changed", ["active_id", "active_flag", "cross_origin", "target_replaced", "engine"])
async def test_background_click_observer_guard_failure_retains_mutation_receipt(setup, query, changed):
    adapter, client = setup
    client.reply_queue = [
        {"helper": "xiaoruo1-cef-background-interaction/1", "verified": False,
         "action_may_have_run": True, "action": "click",
         "before": {"body_hash": 1}, "after": {"body_hash": 1}},
        {"helper": "xiaoruo1-cef-click-observation/1", "verified": True,
         "after": {"body_hash": 2}},
    ]
    def change(value):
        if changed == "active_id":
            value.active = 2
        else:
            key, item = {"active_flag": ("active", True),
                         "cross_origin": ("url", "https://other.example.test/draft"),
                         "target_replaced": ("id", 8),
                         "engine": ("engine", "unknown-engine")}[changed]
            value.target[key] = item
    client.query_changes[query] = change
    result = await call(adapter, "click", selector="#draft")
    assert result["error"]["code"] == "native_background_interrupted"
    assert not result["verified"] and result["action_may_have_run"] is True
    # Even an interruption after the observer has returned belongs to the
    # original click, never to an unexecuted or synthetic second write.
    assert result["native_task_id"] == "xr-42-1"
    assert result["native_state"] == "succeeded" and result["native_terminal"] is True
    assert len(client.calls) == (1 if query == 5 else 2)
    assert all(action == "evaluate" for action, _, _ in client.calls)
    assert sum("xiaoruo1-cef-background-interaction/1" in arguments["script"]
               for _, arguments, _ in client.calls) == 1


async def test_background_click_rejects_cross_origin_route_without_recovery(setup):
    adapter, client = setup
    client.change_after = lambda value: value.target.update(url="https://other.example.test/draft")
    result = await call(adapter, "click", selector="#save")
    assert result["error"]["code"] == "native_background_interrupted"
    assert result["action_may_have_run"] and len(client.calls) == 1


async def test_background_type_keeps_default_url_fence_for_same_origin_change(setup):
    adapter, client = setup
    client.change_after = lambda value: value.target.update(url="https://example.test/changed")
    result = await call(adapter, "type_text", selector="#title", text="new title")
    assert result["error"]["code"] == "native_background_interrupted"
    assert result["action_may_have_run"] and len(client.calls) == 1


async def test_background_click_rejects_active_tab_change(setup):
    adapter, client = setup
    client.change_after = lambda value: setattr(value, "active", 2)
    result = await call(adapter, "click", selector="#save")
    assert result["error"]["code"] == "native_background_interrupted"
    assert result["action_may_have_run"] and len(client.calls) == 1


async def test_untrusted_key_without_observed_effect_requires_foreground(setup):
    adapter, client = setup
    client.reply = {"helper": "xiaoruo1-cef-background-interaction/1", "verified": False,
                    "action_may_have_run": True, "error": {"code": "foreground_required"}}
    result = await call(adapter, "press_key", selector="#tag", key="Enter")
    assert result["error"]["code"] == "foreground_required"
    assert result["input_semantics"] == "synthetic_dom_events_untrusted"
    assert result["action_may_have_run"] and len(client.calls) == 1


async def test_text_is_not_verified_from_task_success_without_actual_value_match(setup):
    adapter, client = setup
    client.reply = {"helper": "xiaoruo1-cef-background-interaction/1", "verified": True,
                    "observed_value": "old title", "value_matches": False}
    result = await call(adapter, "type_text", selector="#title", text="new title")
    assert not result["verified"] and result["native_verification_required"]


@pytest.mark.parametrize("method", ["click", "press_key"])
async def test_background_event_observes_delayed_effect_without_redispatch(setup, method):
    adapter, client = setup
    client.reply_queue = [
        {"helper": "xiaoruo1-cef-background-interaction/1", "verified": False,
         "action_may_have_run": True, "action": "click" if method == "click" else "press_key",
         "before": {"body_hash": 1}, "after": {"body_hash": 1}},
        {"helper": "xiaoruo1-cef-click-observation/1", "verified": True,
         "after": {"body_hash": 2}},
    ]
    arguments = {"selector": "#action"} if method == "click" else {"selector": "#tag", "key": "Enter"}
    result = await call(adapter, method, **arguments)
    assert result["verified"] and result["native_observation_task_ids"]
    assert len(client.calls) == 2 and all(action == "evaluate" for action, _, _ in client.calls)


def test_background_script_is_fixed_json_only_and_has_no_focus_or_native_key_fallback():
    script = _cef_background_script("type_text", {"selector": '#title\");callerScript=true;//', "text": "value"})
    values, _ = json.JSONDecoder().raw_decode(script[len("(()=>{const args="):])
    assert values == {"action": "type_text", "selector": '#title\");callerScript=true;//', "value": "value"}
    for forbidden in (".focus(", "window.open(", "SendInput", "SetForegroundWindow", "select_tab", "system_input"):
        assert forbidden not in script
    assert 'new KeyboardEvent(' in script and 'new Event("input"' in script and 'new Event("change"' in script
    assert 'setter.call(el,expected)' in script
    node = shutil.which("node")
    if node:
        compiled = subprocess.run([node, "-e", "new Function(require('fs').readFileSync(0,'utf8'));"],
                                  input=script, text=True, capture_output=True, timeout=10)
        assert compiled.returncode == 0, compiled.stderr


def _run_helper(node, script, setup):
    # This executes the exact generated fixed helper against minimal local DOM
    # objects; it never attaches a browser and can prove no focus fallback ran.
    harness = r'''
let focusCalls=0,events=[];
class HTMLElement { focus(){focusCalls++;} }
class SVGElement extends HTMLElement {}
class HTMLInputElement extends HTMLElement {
 constructor(){super();this.localName="input";this.type="text";this._value="old";this.disabled=false;this.readOnly=false;this.parentElement=null;}
 get value(){return this._value;}set value(v){this._value=v;}
 getAttribute(name){return null;}closest(){return null;}matches(){return false;}
 dispatchEvent(event){events.push({type:event.type,isTrusted:event.isTrusted,key:event.key,code:event.code,keyCode:event.keyCode,which:event.which,charCode:event.charCode});if(this.handler)this.handler(event);return true;}
}
class HTMLTextAreaElement extends HTMLInputElement {}
class HTMLSelectElement extends HTMLInputElement {}
class HTMLFormElement extends HTMLElement {submit(){} requestSubmit(){}}
class KeyboardEvent extends Event {constructor(type,args){super(type,args);this.key=args.key;this.code=args.code;}}
const el=new HTMLInputElement();const window={focus(){focusCalls++;},open(){focusCalls++;}};
global.location={href:"https://example.test/editor"};const document={body:{innerText:"editor"},querySelectorAll:()=>[el],querySelector:()=>null};
'''
    source = harness + setup + "\nconst value=" + script + ";process.stdout.write(JSON.stringify({value:JSON.parse(value),events,focusCalls}));"
    result = subprocess.run([node, "-e", source], text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_actual_background_type_sets_value_and_fires_only_untrusted_dom_events():
    node = shutil.which("node")
    if not node:
        pytest.skip("existing node runtime unavailable")
    value = _run_helper(node, _cef_background_script("type_text", {"selector": "#title", "text": "new title"}), "")
    assert value["value"]["observed_value"] == "new title" and value["value"]["verified"]
    assert value["events"] == [{"type": "input", "isTrusted": False}, {"type": "change", "isTrusted": False}]
    assert value["focusCalls"] == 0


def test_actual_background_key_does_not_fall_back_when_trusted_events_are_required():
    node = shutil.which("node")
    if not node:
        pytest.skip("existing node runtime unavailable")
    setup = "el.handler=event=>{if(event.isTrusted)document.body.innerText='changed';};"
    value = _run_helper(node, _cef_background_script("press_key", {"selector": "#tag", "key": "Enter"}), setup)
    assert value["value"]["error"]["code"] == "foreground_required" and not value["value"]["verified"]
    assert all(event["isTrusted"] is False for event in value["events"]) and value["focusCalls"] == 0


@pytest.mark.parametrize("key,key_code", [
    ("Enter", 13), ("Escape", 27), ("ArrowUp", 38), ("ArrowDown", 40),
    ("ArrowLeft", 37), ("ArrowRight", 39), ("Backspace", 8), ("Delete", 46),
    ("Home", 36), ("End", 35),
])
def test_background_key_events_use_fixed_legacy_codes_without_trust(key, key_code):
    node = shutil.which("node")
    if not node:
        pytest.skip("existing node runtime unavailable")
    value = _run_helper(node, _cef_background_script("press_key", {"selector": "#tag", "key": key}), "")
    assert value["value"]["error"]["code"] == "foreground_required"
    assert [(event["key"], event["code"], event["keyCode"], event["which"], event["charCode"], event["isTrusted"])
            for event in value["events"]] == [(key, key, key_code, key_code, 0, False)] * 2


@pytest.mark.parametrize("setup", [
    'el.onclick=function(){window.open("https://example.test/");};',
    'el.matches=()=>true;',
    'el.closest=()=>({control:{type:"file"}});',
])
def test_actual_background_click_refuses_known_foreground_targets_before_dispatch(setup):
    node = shutil.which("node")
    if not node:
        pytest.skip("existing node runtime unavailable")
    value = _run_helper(node, _cef_background_script("click", {"selector": "#action"}), setup)
    assert value["value"]["error"]["code"] == "foreground_required"
    assert value["value"]["action_may_have_run"] is False and value["focusCalls"] == 0


@pytest.mark.parametrize("href,target,download,allowed", [
    ("https://example.test/drafts/1", "_self", None, True),
    ("/drafts/1", None, None, True),
    ("https://other.example.test/drafts/1", "", None, False),
    ("https://example.test/drafts/1", "_blank", None, False),
    ("https://example.test/drafts/1", "_self", "", False),
    ("javascript:void(0)", None, None, True),
    ("javascript:;", None, None, True),
    ("javascript:alert(1)", None, None, False),
    ("mailto:author@example.test", None, None, False),
    ("https://user@example.test/drafts/1", "_self", None, False),
])
def test_background_click_anchor_policy_is_same_origin_and_fixed_noop_only(href, target, download, allowed):
    node = shutil.which("node")
    if not node:
        pytest.skip("existing node runtime unavailable")
    values = {"href": href, "target": target, "download": download}
    setup = f'''el.localName="a";el.click=()=>{{}};
el.matches=selector=>selector==="a[href],area[href]";
el.hasAttribute=name=>({json.dumps(values)}[name]!==null);
el.getAttribute=name=>({json.dumps(values)}[name]);'''
    value = _run_helper(node, _cef_background_script("click", {"selector": "#draft"}), setup)
    if allowed:
        assert value["value"]["action_may_have_run"] and not value["value"]["error"]
    else:
        assert value["value"]["error"]["code"] == "foreground_required"
        assert value["value"]["action_may_have_run"] is False


@pytest.mark.parametrize("target,base_target,allowed", [(None, "_blank", False), ("_self", "_blank", True)])
def test_background_click_anchor_honors_effective_base_target(target, base_target, allowed):
    node = shutil.which("node")
    if not node:
        pytest.skip("existing node runtime unavailable")
    values = {"href": "https://example.test/drafts/1", "target": target, "download": None}
    setup = f'''el.localName="a";el.click=()=>{{}};
el.matches=selector=>selector==="a[href],area[href]";
el.hasAttribute=name=>({json.dumps(values)}[name]!==null);
el.getAttribute=name=>({json.dumps(values)}[name]);
document.querySelector=()=>({{getAttribute:name=>name==="target"?{json.dumps(base_target)}:null}});'''
    value = _run_helper(node, _cef_background_script("click", {"selector": "#draft"}), setup)
    assert (value["value"]["action_may_have_run"] and not value["value"]["error"]) is allowed
