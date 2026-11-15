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
from urllib.parse import urlsplit

from .browser_upload import UploadError, allowed_url, read_allowed_media, receipt as upload_receipt


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


# Fixed, read-only DOM observation. No attributes, page globals, file controls
# or caller-provided JavaScript are written. Selectors are published only when
# they identify exactly one node across accessible documents/open shadow roots.
_CEF_DOM_JS = r'''(()=>{const args=__ARGUMENTS__;
 const roots=[],seen=new Set();let visited=0,truncated=false,rootsTruncated=false,inputTruncated=false,inaccessible=0;
 function collect(root,scope,depth){
  if(!root||seen.has(root))return;
  if(depth>8||roots.length>=32){truncated=true;rootsTruncated=true;return;}
  seen.add(root);roots.push({root,scope});
  for(const el of root.querySelectorAll("*")){
   if(++visited>20000){truncated=true;rootsTruncated=true;return;}
   if(el.shadowRoot)collect(el.shadowRoot,scope+" / open-shadow",depth+1);
   if(el.localName==="iframe"||el.localName==="frame"){
    try{if(el.contentDocument)collect(el.contentDocument,scope+" / same-origin-frame",depth+1);else inaccessible++;}
    catch(_){inaccessible++;}
   }
  }
 }
 collect(document,"main",0);
 function matches(selector){const found=[];try{for(const item of roots)for(const el of item.root.querySelectorAll(selector))found.push(el);}catch(_){return []; }return found;}
 function precise(el){
  const candidates=[];if(el.id)candidates.push("#"+CSS.escape(el.id));
  let node=el,path=[];
  while(node&&node.nodeType===1&&path.length<60){
   let index=1;for(let prev=node.previousElementSibling;prev;prev=prev.previousElementSibling)if(prev.localName===node.localName)index++;
   path.unshift(CSS.escape(node.localName)+":nth-of-type("+index+")");node=node.parentElement;
  }
  if(!node)candidates.push(path.join(" > "));
  for(const selector of candidates){if(selector.length>2000)continue;const found=matches(selector);if(found.length===1&&found[0]===el)return selector;}
  return null;
 }
 function safeBackgroundImage(value){
  return String(value||"").replace(/url\(\s*(?:"data:[^"]*"|'data:[^']*'|data:[^)]*)\s*\)/gi,"url(data-image-omitted)").slice(0,4000);
 }
 function describe(el,scope){
  const file=el.localName==="input"&&el.type==="file";
  const box=el.getBoundingClientRect(),style=getComputedStyle(el);
  const visible=!!(box.width&&box.height)&&style.display!=="none"&&style.visibility!=="hidden";
  const item={selector:precise(el),tag:el.localName,type:String(el.type||""),
   role:el.getAttribute("role"),name:String(el.getAttribute("aria-label")||el.getAttribute("title")||el.getAttribute("name")||(!file&&el.type!=="password"?el.innerText:"")||"").slice(0,200),
   disabled:!!el.disabled||el.getAttribute("aria-disabled")==="true",scope,visible,
   title:String(el.getAttribute("title")||"").slice(0,500),aria_label:String(el.getAttribute("aria-label")||"").slice(0,500),
   label:Array.from(el.labels||[]).map(label=>String(label.innerText||"")).join(" ").slice(0,500),
   class_name:String(el.getAttribute("class")||"").slice(0,1000),
   background_image:safeBackgroundImage(style.backgroundImage),
   href:el.hasAttribute("href")?String(el.getAttribute("href")||"").slice(0,4000):null,
   target:el.hasAttribute("target")?String(el.getAttribute("target")||"").slice(0,200):null,
   download:el.hasAttribute("download")?String(el.getAttribute("download")||"").slice(0,4000):null,
   aria_checked:el.getAttribute("aria-checked"),aria_selected:el.getAttribute("aria-selected"),
   aria_expanded:el.getAttribute("aria-expanded"),aria_pressed:el.getAttribute("aria-pressed"),
   checked:typeof el.checked==="boolean"?el.checked:null};
   if(!file&&el.type!=="password"&&el.type!=="hidden"&&typeof el.value==="string")item.value=el.value.slice(0,4000);
  const contenteditable=String(el.getAttribute("contenteditable")||"").toLowerCase();
  if(visible&&!file&&el.type!=="password"&&el.type!=="hidden"&&el.isContentEditable===true&&["true","plaintext-only"].includes(contenteditable)){
   item.contenteditable=contenteditable;
   item.editable_text=String(typeof el.innerText==="string"?el.innerText:el.textContent||"").replace(/\r\n?/g,"\n").slice(0,4000);
  }
  if(el.localName==="select")item.selected_text=Array.from(el.selectedOptions||[]).map(option=>option.text).join(" ").slice(0,1000);
  if(file)Object.assign(item,{accept:String(el.accept||""),multiple:!!el.multiple,
   selected_files:Array.from(el.files||[]).slice(0,32).map(f=>({name:String(f.name).slice(0,255),size:f.size,type:f.type}))});
  return item;
 }
 const elements=[],file_inputs=[],images=[];
 for(const {root,scope} of roots){
  const interactive=new Set(root.querySelectorAll('a[href],button,input:not([type="hidden"]),select,textarea,label,[role="button"],[role="link"],[role="checkbox"],[role="radio"],[role="combobox"],[contenteditable="true"],[contenteditable="plaintext-only"],[tabindex],[onclick],[aria-checked]'));
   for(const el of root.querySelectorAll("div,span")){
    const text=String(el.innerText||"").trim(),style=getComputedStyle(el),box=el.getBoundingClientRect();
    const visible=!!(box.width&&box.height)&&style.display!=="none"&&style.visibility!=="hidden";
    const hasRealBackgroundImage=/url\(\s*["']?(?:https?:|blob:|\/\/)/i.test(String(style.backgroundImage||""));
    if(style.cursor==="pointer"&&((text&&text.length<=200)||(visible&&!text&&hasRealBackgroundImage)))interactive.add(el);
  }
  for(const el of interactive){
   if(elements.length>=300){truncated=true;break;}const item=describe(el,scope);if(item.visible||item.type==="file")elements.push(item);
  }
  for(const el of root.querySelectorAll('input[type="file"]')){
   if(file_inputs.length>=64){truncated=true;inputTruncated=true;break;}file_inputs.push(describe(el,scope));
  }
  for(const el of root.querySelectorAll("img")){
   if(images.length>=100){truncated=true;break;}const box=el.getBoundingClientRect(),style=getComputedStyle(el);
   const src=String(el.currentSrc||el.src||"");
   images.push({selector:precise(el),scope,src:/^(https?:|blob:)/i.test(src)?src.slice(0,4000):(src?"inline-image-omitted":""),
    alt:String(el.alt||"").slice(0,500),natural_width:el.naturalWidth,natural_height:el.naturalHeight,
    visible:!!(box.width&&box.height)&&style.display!=="none"&&style.visibility!=="hidden"});
  }
 }
 const selected=args.selector?matches(args.selector):[];
  const selected_elements=selected.slice(0,64).map(el=>{
   const owner=roots.find(item=>item.root===el.getRootNode());
   return describe(el,owner?owner.scope:"unknown");
  });
  const selected_elements_truncated=selected.length>64;
  if(selected_elements_truncated)truncated=true;
 return JSON.stringify({helper:"xiaoruo1-cef-dom-observation/1",url:String(location.href),title:String(document.title||"").slice(0,500),
  text:String(document.body&&document.body.innerText||"").slice(0,args.max_chars),elements,file_inputs,images,
  selected_elements,selected_elements_truncated,
  selector_match_count:args.selector?selected.length:null,
  selector_is_file:args.selector&&selected.length===1?selected[0].localName==="input"&&selected[0].type==="file":false,
  observation_complete:!rootsTruncated&&!inputTruncated,truncated,inaccessible_frames:inaccessible});
})()'''


def _cef_helper_script(kind: str, arguments: dict[str, Any]) -> str:
    if kind == "read_dom":
        selector = arguments.get("selector")
        maximum = arguments.get("max_chars", 100000)
        if selector is not None and (not isinstance(selector, str) or not selector or len(selector) > 2000):
            raise NativeAdapterError("invalid_argument", "DOM observation requires a bounded CSS selector")
        if type(maximum) is not int or not 100 <= maximum <= 100000:
            raise NativeAdapterError("invalid_argument", "DOM text observation must be bounded")
        return _CEF_DOM_JS.replace("__ARGUMENTS__", json.dumps({"selector": selector, "max_chars": maximum}, ensure_ascii=True))
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


# Background input deliberately does not call the native fill/press actions:
# the installed native documentation provides no verified no-focus guarantee
# for those actions. Synthetic DOM events have isTrusted=false and cannot
# stand in for a physical/trusted keypress.
_CEF_BACKGROUND_INTERACTION_JS = r'''(()=>{const args=__ARGUMENTS__;
__OBSERVATION__
let dispatched=false,foregroundRequested=false;const restore=[];
const refused=(code="foreground_required")=>JSON.stringify({helper:"xiaoruo1-cef-background-interaction/1",verified:false,action_may_have_run:dispatched,error:{code},execution_mode:"native_background",input_semantics:"synthetic_dom_events_untrusted"});
 const plaintext=el=>String(typeof el.innerText==="string"?el.innerText:el.textContent||"").replace(/\r\n?/g,"\n");
 function safeSameTabAnchor(el){
  if(!el.matches("a[href],area[href]"))return true;
  if(el.hasAttribute("download"))return false;
  const ownTarget=String(el.getAttribute("target")||"").trim().toLowerCase();
  let baseTarget="";try{const base=document.querySelector("base[target]");baseTarget=String(base&&base.getAttribute("target")||"").trim().toLowerCase();}catch(_){return false;}
  const target=ownTarget||baseTarget;
  if(target&&target!=="_self")return false;
  const href=String(el.getAttribute("href")||"");
  if(href==="javascript:void(0)"||href==="javascript:;")return true;
  try{const base=new URL(location.href),url=new URL(href,base.href);
   return !base.username&&!base.password&&!url.username&&!url.password
    &&/^https?:$/i.test(url.protocol)&&url.origin===base.origin;
  }catch(_){return false;}
 }
try{
 const nodes=document.querySelectorAll(args.selector);if(nodes.length!==1)return refused("native_background_target_ambiguous");
 const el=nodes[0],tag=el.localName,type=String(el.type||"").toLowerCase();
 if(el.disabled||el.readOnly||el.hidden||el.getAttribute("aria-disabled")==="true"||el.getAttribute("aria-readonly")==="true"||/^(password|file|hidden)$/.test(type))return refused();
 const label=el.closest("label");if(label&&label.control&&label.control.type==="file")return refused();
 for(let node=el;node;node=node.parentElement){
  if(node.matches('a[href],area[href]')&&!safeSameTabAnchor(node))return refused();
  if(node.matches('[download],[target="_blank"],input[type="file"]'))return refused();
  if(node.form&&((node.localName==="button"&&node.type!=="button")||/^(submit|image|reset)$/.test(String(node.type))))return refused();
  for(const name of ["onclick","onkeydown","onkeyup","oninput","onchange"]){
   const text=String(node.getAttribute(name)||"")+String(node[name]||"");
   if(/(?:\bfocus\s*\(|\bopen\s*\(|showPicker\s*\(|requestFullscreen\s*\(|\blocation\b|\bhistory\b|requestSubmit\s*\(|\.submit\s*\()/i.test(text))return refused();
  }
 }
 // Guard known synchronous page hooks as well as the default-action checks.
 // No saved method is ever called: foreground requests are explicit refusal.
 const blocked=()=>{foregroundRequested=true;throw new Error("xiaoruo_background_foreground_required");};
 function block(owner,key){const descriptor=Object.getOwnPropertyDescriptor(owner,key);if(!descriptor||descriptor.configurable){Object.defineProperty(owner,key,{value:blocked,configurable:true,writable:true});restore.push(()=>{if(descriptor)Object.defineProperty(owner,key,descriptor);else delete owner[key];});}else throw new Error("xiaoruo_background_guard_unavailable");}
 for(const owner of [HTMLElement.prototype,SVGElement.prototype])block(owner,"focus");
 // Keep synchronous dialogs and popup primitives out of the background route.
 // These are page-side effects just like window.open: a synthetic click must
 // never cause a native dialog or popup while the tab is inactive.
 for(const key of ["focus","open","alert","confirm","prompt","print"])block(window,key);
 if(typeof HTMLInputElement.prototype.showPicker==="function")block(HTMLInputElement.prototype,"showPicker");
 for(const key of ["submit","requestSubmit"])if(typeof HTMLFormElement.prototype[key]==="function")block(HTMLFormElement.prototype,key);
 if(typeof HTMLDialogElement!=="undefined")for(const key of ["show","showModal"])if(typeof HTMLDialogElement.prototype[key]==="function")block(HTMLDialogElement.prototype,key);
 const before=observed(args.selector);let expected=null,editable=false;
 if(args.action==="type_text"){
  const contenteditable=String(el.getAttribute("contenteditable")||"").toLowerCase();
  editable=!/^(input|textarea|select)$/.test(tag)&&el.isContentEditable===true&&["true","plaintext-only"].includes(contenteditable);
  if(editable){
   // A whole editable host is replaced with literal text only. Nested form
   // controls and independent editable regions must never be destroyed.
   if(el.querySelector('input,textarea,select,button,form,iframe,frame,object,embed,[contenteditable]'))return refused("unsupported");
   expected=args.value;dispatched=true;el.textContent=expected;
  }else{
   if(!((tag==="input"&&/^(text|search|email|url|tel|number)$/.test(type))||tag==="textarea"))return refused("unsupported");
   const prototype=tag==="textarea"?HTMLTextAreaElement.prototype:HTMLInputElement.prototype;
   const setter=Object.getOwnPropertyDescriptor(prototype,"value").set;
   expected=args.value;dispatched=true;setter.call(el,expected);
  }
  el.dispatchEvent(new Event("input",{bubbles:true}));el.dispatchEvent(new Event("change",{bubbles:true}));
 }else if(args.action==="select_option"){
  if(tag!=="select")return refused("unsupported");
  const options=Array.from(el.options).filter(option=>args.option_by==="value"?option.value===args.value:option.text===args.value);
  if(options.length!==1||options[0].disabled)return refused("native_background_option_ambiguous");
  expected=options[0].value;dispatched=true;Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype,"value").set.call(el,expected);
  el.dispatchEvent(new Event("input",{bubbles:true}));el.dispatchEvent(new Event("change",{bubbles:true}));
 }else if(args.action==="press_key"){
  if(!((tag==="input"&&/^(text|search|email|url|tel|number)$/.test(type))||tag==="textarea"))return refused();
  // Keep this mapping fixed and allowlisted. KeyboardEvent constructors do
  // not reliably expose legacy keyCode/which, while Vue handlers may inspect
  // them; defining these own read-only values still leaves isTrusted=false.
  const keyCodes={Enter:13,Escape:27,ArrowUp:38,ArrowDown:40,ArrowLeft:37,ArrowRight:39,Backspace:8,Delete:46,Home:36,End:35};
  const keyCode=Object.prototype.hasOwnProperty.call(keyCodes,args.key)?keyCodes[args.key]:0;if(!keyCode)return refused();
  const makeKeyEvent=type=>{const event=new KeyboardEvent(type,{key:args.key,code:args.key,bubbles:true,cancelable:true});
   Object.defineProperties(event,{keyCode:{value:keyCode,enumerable:true},which:{value:keyCode,enumerable:true},charCode:{value:0,enumerable:true}});return event;};
  dispatched=true;el.dispatchEvent(makeKeyEvent("keydown"));
  el.dispatchEvent(makeKeyEvent("keyup"));
 }else if(args.action==="click"){dispatched=true;el.click();}else return refused("unsupported");
 if(foregroundRequested)return refused();
 const after=observed(args.selector),value=editable?plaintext(el):typeof el.value==="string"?el.value:null;
 const valueMatches=expected===null?null:value===(editable?expected.replace(/\r\n?/g,"\n"):expected);
 const verified=expected!==null?valueMatches:JSON.stringify(before)!==JSON.stringify(after);
 return JSON.stringify({helper:"xiaoruo1-cef-background-interaction/1",verified,action_may_have_run:dispatched,
  action:args.action,before,after,observed_value:value,value_matches:valueMatches,
  execution_mode:"native_background",input_semantics:"synthetic_dom_events_untrusted",
  error:!verified&&args.action==="press_key"?{code:"foreground_required"}:null});
}catch(_){return refused();}finally{for(const undo of restore.reverse())try{undo();}catch(_){}}
})()'''


def _cef_background_script(method: str, arguments: dict[str, Any]) -> str:
    selector = arguments.get("selector")
    if not isinstance(selector, str) or not selector or len(selector) > 2000:
        raise NativeAdapterError("invalid_argument", "Background interaction requires one bounded observed CSS selector")
    values: dict[str, Any] = {"action": method, "selector": selector}
    if method == "type_text":
        text = arguments.get("text", "")
        if not isinstance(text, str) or len(text) > 100000 or arguments.get("clear", True) is not True:
            raise NativeAdapterError("unsupported", "Background text input supports bounded replacement values only")
        values["value"] = text
    elif method == "select_option":
        value, by = arguments.get("option", ""), arguments.get("option_by", "value")
        if not isinstance(value, str) or len(value) > 10000 or by not in {"value", "text", "label"}:
            raise NativeAdapterError("unsupported", "Background select requires one exact option value or label")
        values.update(value=value, option_by=by)
    elif method == "press_key":
        key = arguments.get("key")
        if key not in {"Enter", "Escape", "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight", "Backspace", "Delete", "Home", "End"}:
            raise NativeAdapterError("foreground_required", "This key requires foreground trusted keyboard input")
        values["key"] = key
    elif method != "click":
        raise NativeAdapterError("unsupported", "No background helper exists for this action")
    return _CEF_BACKGROUND_INTERACTION_JS.replace("__OBSERVATION__", _CEF_OBSERVATION_FUNCTION).replace(
        "__ARGUMENTS__", json.dumps(values, ensure_ascii=True))


class _BackgroundGuardError(NativeAdapterError):
    def __init__(self, result):
        self.result = result
        super().__init__(result["error"]["code"], result["error"]["message"])


class _BackgroundClient:
    """Fence every queued task with an independent, exact native tab query."""
    def __init__(self, client, tab_id, expected_active, remaining, target, *, allow_same_origin_transition=False):
        self.client, self.tab_id, self.expected_active = client, str(tab_id), str(expected_active)
        self.remaining, self.target = remaining, target
        self.allow_same_origin_transition = allow_same_origin_transition is True
        self.url_transition = False
        self._mutation_response = None

    async def _state(self, timeout=None):
        try:
            native = _structured(await self.client.tabs(timeout=timeout if timeout is not None else self.remaining()))
            found = [tab for tab in NativeXiaoruoBrowserAdapter._tabs(native) if tab["tab_id"] == self.tab_id]
            raw = [tab for tab in native.get("tabs", []) if isinstance(tab, dict) and str(tab.get("id")) == self.tab_id]
            if len(found) != 1 or len(raw) != 1:
                return None
            current = found[0]
            pending = current["pending_url"]
            effective_url = pending or current["url"]
            baseline = self.target["url"]
            return {
                "stable": (str(native.get("activeTabId", "")) == self.expected_active
                           and self.expected_active != self.tab_id and current.get("active") is False
                           and _background_cef(raw[0])),
                "ready": current.get("loading") is False and current["document_ready"] and not pending,
                "url": current["url"], "pending_url": pending, "effective_url": effective_url,
                "same_url": current["url"] == baseline and not pending,
                "same_origin": _same_origin(baseline, effective_url),
            }
        except Exception:
            return None

    async def check(self, response=None):
        # A click's route may commit after its first post-dispatch query. Later
        # read-only probes must retain that click's phase and receipt even when
        # their own preflight fails or their native task has a different ID.
        if self._mutation_response is not None:
            response = self._mutation_response
        post_dispatch = response is not None
        loop = asyncio.get_running_loop()
        # Only a background click may trigger an in-document same-origin
        # route. Readiness is observed for a bounded period; no foreground
        # activation or second browser action is attempted.
        wait_until = loop.time()
        if post_dispatch and self.allow_same_origin_transition:
            try:
                wait_until += min(2.0, max(0, self.remaining() - .2))
            except NativeAdapterError:
                pass
        while True:
            query_timeout = None
            if wait_until > loop.time():
                try:
                    query_timeout = min(self.remaining(), max(.01, wait_until - loop.time()))
                except NativeAdapterError:
                    query_timeout = .01
            state = await self._state(query_timeout)
            valid = bool(state and state["stable"] and state["ready"] and state["same_url"])
            if (state and state["stable"] and state["ready"] and post_dispatch
                    and self.allow_same_origin_transition and not state["same_url"] and state["same_origin"]):
                self.target["url"] = state["url"]
                self.url_transition = True
                valid = True
            if valid:
                return
            can_wait = (post_dispatch and self.allow_same_origin_transition and state is not None
                        and state["stable"] and state["same_origin"] and loop.time() < wait_until)
            if not can_wait:
                break
            await asyncio.sleep(min(.1, max(.001, wait_until - loop.time())))
        task = (response or {}).get("native_task", {})
        task = task if isinstance(task, dict) else {}
        raise _BackgroundGuardError(_public_error("native_background_interrupted",
            "The active tab or exact background document changed; foreground control is required before continuing",
            task_id=(response or {}).get("task_id") or task.get("id") or task.get("taskId"),
            unknown=post_dispatch, native_state=task.get("state"), native_task=task))

    async def execute(self, action, arguments, **kwargs):
        if action not in {"evaluate", "upload_file", "screenshot"}:
            raise _BackgroundGuardError(_public_error("foreground_required", "This native action is not proven safe for background execution"))
        await self.check()
        response = await self.client.execute(action, arguments, **kwargs)
        # Transition permission belongs only to a click workflow. Its first
        # task is the mutating helper; subsequent tasks only observe it and
        # must never replace the original mutation receipt.
        if self.allow_same_origin_transition and self._mutation_response is None:
            self._mutation_response = response
        await self.check(response)
        return response


def _background_cef(tab):
    # Unlike the legacy engine normalizer, unknown engine labels cannot grant
    # permission to the background route.
    label = str(tab.get("engineMode", tab.get("engine", ""))).casefold()
    return label in {"cef", "chromium", "cef / chromium", "cef/chromium"}


def _same_origin(left, right):
    try:
        a, b = urlsplit(str(left)), urlsplit(str(right))
        if not a.scheme or not b.scheme or not a.hostname or not b.hostname:
            return False
        if a.scheme.casefold() != b.scheme.casefold() or a.hostname.casefold() != b.hostname.casefold():
            return False
        def port(value):
            if value.port is not None:
                return value.port
            return {"http": 80, "https": 443}.get(value.scheme.casefold())
        return port(a) == port(b)
    except (TypeError, ValueError):
        return False


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
        self._file_observations: dict[tuple[str, str, str], dict[str, Any]] = {}

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
                if busy:
                    descriptor["busy_reason"] = getattr(error, "code", "native_ipc_busy")
            public = {"browser_id": identifier, "connection_generation": str(generation),
                "browser_kind": "xiaoruo_browser", "browser_family": "native",
                "adapter": "native_ipc", "transport": "windows_named_pipe",
                "stable_identity": True, "connected": True,
                "busy": descriptor.get("busy", False),
                "busy_reason": descriptor.get("busy_reason"),
                "native_pid": descriptor.get("native_pid", descriptor.get("process_id", descriptor.get("pid", instance.pid))),
                "version": descriptor.get("version") or ("v73" if self._supports_engine(caps, "chromium", "upload_file") else "unknown"),
                "engine": "cef_and_lightpanda", "capabilities": caps,
                "supported_actions": self._supported(caps),
                "limitations": ["Capabilities are checked again for the selected tab engine", "Unsupported operations never fall back after native dispatch"]}
            result.append(public)
            current[identifier] = instance
        self._instances = current
        return {"ok": True, "status": "completed", "browsers": result,
                "native_discovery_diagnostics": list(getattr(self.discovery, "last_diagnostics", []))}

    @staticmethod
    def _supported(caps: dict[str, Any]) -> list[str]:
        # The runtime capability shape is normalized by _supports_engine. The
        # transport itself provides instance, tabs and task status independently
        # of the selected page engine.
        values = ["status", "list_tabs", "task_status"]
        for wire, native in {"open_tab": "new_tab", "close_tab": "close_tab",
            "activate_tab": "select_tab", "navigate": "navigate", "read": "read_page",
            "click": "click", "type": "fill", "press": "press", "select": "select_option",
            "scroll": "scroll", "wait": "read_page", "screenshot": "screenshot", "upload_file": "upload_file"}.items():
            if any(NativeXiaoruoBrowserAdapter._supports_engine(caps, engine, native) for engine in ("chromium", "lightpanda")):
                values.append(wire)
        return values

    @staticmethod
    def _supports_engine(caps: dict[str, Any], engine: str, action: str) -> bool:
        # v72 exposes common capability flags plus per-engine overrides. Page
        # screenshot has a distinct pixel/reader meaning and is never coerced.
        aliases = {"new_tab": "tabs", "close_tab": "tabs", "select_tab": "tabs",
                   "navigate": "navigation", "read_page": "readPage",
                   "select_option": "selectOption", "screenshot_window": "screenshotWindow", "upload_file": "fileUpload"}
        name = aliases.get(action, action)
        entry = caps.get("cef" if engine == "chromium" else "lightpanda", {})
        if action == "upload_file":
            # v73's real capability is fileUpload:'cef-only', cef.fileUpload:true.
            # A global flag never grants a file control to Lightpanda.
            if engine != "chromium":
                return False
            if isinstance(entry, dict) and name in entry:
                return entry[name] is True or entry[name] == "supported"
            if caps.get(name) == "cef-only":
                return True
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
        if payload.get("background_only") is True:
            try:
                result = await self._native_background_dispatch(client, instance, matches[0]["capabilities"],
                    browser_id, generation, method, payload, remaining)
                # Keep the explicit mode visible on every background outcome,
                # including deterministic refusals returned before dispatch.
                return {**result, "execution_mode": "native_background"}
            except _BackgroundGuardError as error:
                return {**error.result, "execution_mode": "native_background"}
        if method == "list_tabs":
            native = _structured(await client.tabs(timeout=remaining()))
            return {"tabs": self._tabs(native), "active_tab_id": str(native.get("activeTabId", "")), "verified": True, "adapter": "native_ipc"}
        if method == "task_status":
            task_id = payload.get("task_id")
            if not isinstance(task_id, str) or not task_id.startswith("xr-" + str(instance.pid) + "-"):
                return _public_error("native_task_not_owned", "Task does not belong to the selected native browser process")
            reply = await client.call_tool("xiaoruo_browser_task_status", {"task_id": task_id}, timeout=remaining())
            response = _structured(reply)
            task = response.get("task", response)
            state = task.get("state") if isinstance(task, dict) else None
            returned_ids = [task[key] for key in ("id", "taskId", "task_id") if key in task] if isinstance(task, dict) else []
            if reply.get("is_error") is True or not returned_ids or any(identifier != task_id for identifier in returned_ids) or not isinstance(state, str) or state not in {"accepted", "running", "succeeded", "failed", "timed_out", "cancelled"}:
                return _public_error("native_task_status_invalid", "Native status did not identify the requested task and a known state", task_id=task_id, unknown=True)
            succeeded = state == "succeeded" and not task.get("errorCode") and not task.get("error")
            result = {"verified": True, "native_task_id": task_id, "native_state": state,
                "native_terminal": state in {"succeeded", "failed", "timed_out", "cancelled"},
                "native_task_succeeded": succeeded, "native_task": _public_task(task)}
            # A late capture may finish after its caller's IPC deadline. Recover
            # the original task's pixels instead of dispatching another capture
            # (or activating a tab/window as an implicit fallback).
            if succeeded and task.get("action") in {"screenshot", "screenshot_window"}:
                mode = "window" if task["action"] == "screenshot_window" else "page"
                if not reply.get("images"):
                    # v73 may attach pixels only to the first successful poll.
                    # Later status reads are still valid task receipts.
                    return {**result, "capture_pixels_available": False}
                return self._save_screenshot(reply.get("images", []),
                    {**result, "recovered_from_task_status": True, "capture_pixels_available": True, "adapter": "native_ipc"}, mode)
            return result
        mapping = {"open_tab": "new_tab", "close_tab": "close_tab", "activate_tab": "select_tab",
            "navigate": "navigate", "read_page": "read_page", "click": "click", "type_text": "fill",
            "press_key": "press", "select_option": "select_option", "scroll": "scroll", "wait": "read_page",
            "screenshot": "screenshot", "upload_file": "upload_file"}
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
        if method == "upload_file":
            if set(payload) - {"tab_id", "file_path", "selector", "wait_timeout_ms", "timeout"}:
                return _public_error("invalid_argument", "Native upload accepts one allowed local media path and an observed selector")
            return await self._upload_file(client, instance, caps, browser_id, generation, str(tab_id), target, payload, remaining)
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
        maximum = args.pop("max_chars", 100000)
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
        if method in {"read_page", "wait"} and engine == "chromium":
            observed = await self._observe_dom(client, instance, caps, str(tab_id), remaining,
                                               selector=payload.get("selector"), max_chars=maximum)
            if observed.get("helper") == "xiaoruo1-cef-dom-observation/1":
                result.update({key: value for key, value in observed.items() if key not in {"helper", "verified"}})
                self._remember_file_inputs(browser_id, generation, str(tab_id), observed)
            else:
                result.update(file_inputs=[], elements=[], dom_observation_available=False,
                              dom_observation_error=observed.get("error"))
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

    async def _native_background_dispatch(self, client, instance, caps, browser_id, generation,
                                          method, payload, remaining):
        """An explicit CEF-only route, independent of all foreground actions."""
        if method not in {"read_page", "wait", "click", "type_text", "press_key", "select_option", "scroll", "screenshot", "upload_file"}:
            return _public_error("foreground_required", "This action requires explicit foreground execution")
        args = dict(payload)
        args.pop("background_only", None)
        expected_active = args.pop("expected_active_tab_id", None)
        tab_id = args.get("tab_id")
        if (not isinstance(expected_active, str) or not expected_active or
                not isinstance(tab_id, (int, str)) or str(tab_id) == expected_active):
            return _public_error("foreground_required", "Background execution requires an exact inactive tab and observed active tab ID")
        try:
            native = _structured(await client.tabs(timeout=remaining()))
        except Exception:
            # This is still pre-dispatch: no native task was submitted. Keep
            # the refusal credential-free and explicitly non-uncertain.
            return _public_error("native_background_interrupted",
                "The exact background tab state could not be verified; foreground control is required before continuing")
        targets = [tab for tab in self._tabs(native) if tab["tab_id"] == str(tab_id)]
        if len(targets) != 1:
            return _public_error("tab_not_found", "The exact background tab is absent")
        target = targets[0]
        raw_target = [tab for tab in native.get("tabs", []) if isinstance(tab, dict) and str(tab.get("id")) == str(tab_id)]
        if len(raw_target) != 1 or not _background_cef(raw_target[0]):
            return _public_error("unsupported", "Native background execution is supported only by the existing CEF engine")
        if args.get("engine") not in (None, "auto", "cef", "chromium"):
            return _public_error("unsupported", "Background execution cannot change the existing tab engine")
        if args.get("frame_id", 0) != 0 or args.get("shadow_path") or args.get("by") not in (None, "css") or args.get("name"):
            return _public_error("unsupported", "Background helpers require the exact observed CSS selector in the main document")
        if method == "screenshot" and args.get("mode", "page") != "page":
            return _public_error("foreground_required", "Whole-window capture requires foreground execution")
        guarded = _BackgroundClient(client, tab_id, expected_active, remaining, target,
                                    allow_same_origin_transition=method == "click")
        await guarded.check()
        if method == "screenshot":
            if not self._supports_engine(caps, "chromium", "screenshot"):
                return _public_error("unsupported", "The CEF engine does not advertise native page screenshots")
            response = await guarded.execute("screenshot", {"tab_id": int(tab_id)}, timeout=max(.01, remaining() - .1))
            task = response.get("native_task") if isinstance(response.get("native_task"), dict) else {}
            if response.get("ok") is not True or task.get("state") != "succeeded":
                return self._background_task_failure(response)
            result = {"verified": True, "native_task_id": response.get("task_id") or task.get("id") or task.get("taskId"),
                      "native_state": "succeeded", "native_terminal": True, "native_task": _public_task(task),
                      "tab_id": str(tab_id), "adapter": "native_ipc", "execution_mode": "native_background"}
            return self._save_screenshot(response.get("images", []), result, "page")
        if "browser.script" not in getattr(instance, "permissions", ()) or not self._supports_engine(caps, "chromium", "evaluate"):
            return _public_error("unsupported", "Background DOM helpers require native evaluate and browser.script permission")
        if method == "upload_file":
            if set(args) - {"tab_id", "file_path", "selector", "wait_timeout_ms", "timeout"}:
                return _public_error("invalid_argument", "Native upload accepts one allowed local media path and an observed selector")
            if not self._supports_engine(caps, "chromium", "upload_file"):
                return _public_error("unsupported", "The CEF engine does not advertise native upload_file")
            result = await self._upload_file(guarded, instance, caps, browser_id, generation,
                                             str(tab_id), target, args, remaining)
            return {**result, "execution_mode": "native_background"}
        if method == "wait":
            milliseconds = args.get("milliseconds", 1000)
            if type(milliseconds) is not int or not 0 <= milliseconds <= 120000 or milliseconds / 1000 >= remaining():
                return _public_error("invalid_argument", "Wait must leave time for a verified background query")
            await asyncio.sleep(milliseconds / 1000)
        if method in {"read_page", "wait"}:
            observed = await self._observe_dom(guarded, instance, caps, str(tab_id), remaining,
                                               selector=args.get("selector"), max_chars=args.get("max_chars", 100000))
            if observed.get("helper") != "xiaoruo1-cef-dom-observation/1":
                return observed
            self._remember_file_inputs(browser_id, generation, str(tab_id), observed)
            return {**observed, "verified": True, "tab_id": str(tab_id), "adapter": "native_ipc",
                    "execution_mode": "native_background", "verification": "native_background_dom_observed"}
        if method == "scroll":
            direction, pixels = args.get("direction", "down"), args.get("pixels", 600)
            if direction not in {"up", "down", "left", "right"} or type(pixels) is not int:
                return _public_error("invalid_argument", "Background scroll requires bounded integer coordinates")
            args.setdefault("x", abs(pixels) if direction == "right" else 0)
            args.setdefault("y", abs(pixels) if direction == "down" else 0)
            try:
                script = _cef_helper_script("scroll", args)
            except NativeAdapterError as error:
                return _public_error(error.code, str(error))
            marker = "xiaoruo1-cef-scroll/1"
        else:
            try:
                script = _cef_background_script(method, args)
            except NativeAdapterError as error:
                return _public_error(error.code, str(error))
            marker = "xiaoruo1-cef-background-interaction/1"
        response = await guarded.execute("evaluate", {"tab_id": int(tab_id), "script": script}, timeout=max(.01, remaining() - .1))
        task = response.get("native_task") if isinstance(response.get("native_task"), dict) else {}
        if response.get("ok") is not True or task.get("state") != "succeeded":
            failure = self._background_task_failure(response)
            if method == "click":
                failure["url_transition"] = guarded.url_transition
            return failure
        observed = _native_result(task)
        task_id = response.get("task_id") or task.get("id") or task.get("taskId")
        observation_tasks = []
        verified = observed.get("helper") == marker and observed.get("verified") is True and not observed.get("error")
        if method == "type_text":
            verified = verified and observed.get("value_matches") is True and observed.get("observed_value") == args.get("text", "")
        elif method == "select_option":
            verified = verified and observed.get("value_matches") is True
        elif method in {"click", "press_key"}:
            verified = verified and isinstance(observed.get("before"), dict) and isinstance(observed.get("after"), dict) and observed["before"] != observed["after"]
            # A Vue/React handler may commit its visible state asynchronously.
            # Observe only; never replay the already-dispatched click/key event.
            if not verified and not observed.get("error") and isinstance(observed.get("before"), dict):
                loop = asyncio.get_running_loop()
                try:
                    observe_until = loop.time() + min(2.0, max(0, remaining() - .2))
                except NativeAdapterError:
                    observe_until = loop.time()
                while loop.time() < observe_until:
                    await asyncio.sleep(min(.1, observe_until - loop.time()))
                    if loop.time() >= observe_until:
                        break
                    try:
                        budget = remaining()
                    except NativeAdapterError:
                        break
                    timeout = min(budget - .1, observe_until - loop.time())
                    if timeout <= 0:
                        break
                    probe_script = _cef_helper_script("observe_click", {
                        "selector": args["selector"], "before": observed["before"]})
                    probe = await guarded.execute("evaluate", {
                        "tab_id": int(tab_id), "script": probe_script}, timeout=max(.01, timeout))
                    probe_task = probe.get("native_task") if isinstance(probe.get("native_task"), dict) else {}
                    observation_tasks.append(probe.get("task_id") or probe_task.get("id") or probe_task.get("taskId"))
                    if probe.get("ok") is not True or probe_task.get("state") != "succeeded":
                        continue
                    after = _native_result(probe_task)
                    if (after.get("helper") == "xiaoruo1-cef-click-observation/1"
                            and after.get("verified") is True and isinstance(after.get("after"), dict)):
                        observed.update(verified=True, after=after["after"],
                                       verification="observed_change_after_single_background_event")
                        verified = True
                        break
        if observed.get("error"):
            result = _public_error(str(observed["error"].get("code") or "foreground_required"),
                "The background helper could not verify this interaction; explicit foreground execution is required",
                task_id=task_id, unknown=observed.get("action_may_have_run") is not False,
                native_state="succeeded", native_task=task)
            return {**result, "execution_mode": "native_background", "input_semantics": "synthetic_dom_events_untrusted",
                    "native_observation_task_ids": observation_tasks,
                    "url_transition": guarded.url_transition if method == "click" else False}
        if method == "press_key" and not verified:
            result = _public_error("foreground_required",
                "The untrusted background key event produced no observed state change; explicit foreground execution is required",
                task_id=task_id, unknown=observed.get("action_may_have_run") is not False,
                native_state="succeeded", native_task=task)
            return {**result, "execution_mode": "native_background", "input_semantics": "synthetic_dom_events_untrusted",
                    "native_observation_task_ids": observation_tasks,
                    "url_transition": False}
        return {**observed, "verified": verified, "tab_id": str(tab_id), "native_task_id": task_id,
                "native_state": "succeeded", "native_terminal": True, "native_task": _public_task(task),
                "native_verification_required": not verified, "action_may_have_run": observed.get("action_may_have_run") is not False,
                "adapter": "native_ipc", "execution_mode": "native_background",
                "native_observation_task_ids": observation_tasks,
                "url_transition": guarded.url_transition if method == "click" else False,
                "verification": "observed_dom_value_or_state" if verified else "background_action_without_verified_effect"}

    @staticmethod
    def _background_task_failure(response):
        task = response.get("native_task") if isinstance(response.get("native_task"), dict) else {}
        state = task.get("state")
        return _public_error(str(task.get("errorCode") or (response.get("error") or {}).get("code") or "native_background_task_incomplete"),
            "The background task did not succeed; inspect its receipt before another write",
            task_id=response.get("task_id") or task.get("id") or task.get("taskId"),
            unknown=response.get("action_may_have_run") is not False, native_state=state, native_task=task)

    async def _observe_dom(self, client, instance, caps, tab_id, remaining, *, selector=None, max_chars=100000):
        if "browser.script" not in getattr(instance, "permissions", ()) or not self._supports_engine(caps, "chromium", "evaluate"):
            return _public_error("unsupported", "Precise CEF DOM observation requires the existing evaluate capability and browser.script permission")
        script = _cef_helper_script("read_dom", {"selector": selector, "max_chars": max_chars})
        response = await client.execute("evaluate", {"tab_id": int(tab_id), "script": script}, timeout=max(.01, remaining() - .1))
        task = response.get("native_task") if isinstance(response.get("native_task"), dict) else {}
        observed = _native_result(task)
        if response.get("ok") is not True or task.get("state") != "succeeded" or observed.get("helper") != "xiaoruo1-cef-dom-observation/1":
            return _public_error("native_dom_observation_unavailable", "Native DOM observation did not return a succeeded fixed-helper result")
        return {**observed, "dom_observation_available": True,
                "dom_observation_task_id": response.get("task_id") or task.get("taskId") or task.get("id")}

    @staticmethod
    def _input_identity(item):
        return {key: item.get(key) for key in ("selector", "tag", "type", "disabled", "accept", "multiple", "scope", "name")}

    def _remember_file_inputs(self, browser_id, generation, tab_id, observed):
        now = time.monotonic()
        self._file_observations = {key: item for key, item in self._file_observations.items() if now - item["at"] < 300}
        if len(self._file_observations) >= 512:
            self._file_observations.pop(next(iter(self._file_observations)))
        self._file_observations[(browser_id, generation, tab_id)] = {
            "at": now, "url": observed.get("url"), "complete": observed.get("observation_complete") is True,
            "inputs": {item["selector"]: self._input_identity(item) for item in observed.get("file_inputs", [])
                       if isinstance(item, dict) and isinstance(item.get("selector"), str)}}

    async def _upload_file(self, client, instance, caps, browser_id, generation, tab_id, target, payload, remaining):
        selector = payload.get("selector")
        wait = payload.get("wait_timeout_ms", 5000)
        if not isinstance(selector, str) or not selector or len(selector) > 2000 or type(wait) is not int or not 0 <= wait <= 30000:
            return _public_error("invalid_argument", "Native upload requires one bounded observed file selector and wait_timeout_ms from 0 through 30000")
        expected_url = (target or {}).get("url", "")
        if not allowed_url(expected_url):
            return _public_error("upload_origin_denied", "Upload is restricted to the existing allowed creator upload page or exact local fixture")
        previous = self._file_observations.get((browser_id, generation, tab_id))
        if not previous or time.monotonic() - previous["at"] >= 300 or not previous["complete"] or previous["url"] != expected_url or selector not in previous["inputs"]:
            return _public_error("upload_selector_unobserved", "Read this exact tab and use its current precise file_inputs selector before uploading")
        try:
            media = await asyncio.wait_for(asyncio.to_thread(read_allowed_media, payload.get("file_path")), remaining())
        except UploadError as error:
            return _public_error(error.code, str(error))
        except TimeoutError:
            return _public_error("upload_timeout_before_dispatch", "Media validation timed out before any file data was sent")
        observed = await self._observe_dom(client, instance, caps, tab_id, remaining, selector=selector)
        if (observed.get("helper") != "xiaoruo1-cef-dom-observation/1" or observed.get("observation_complete") is not True
                or observed.get("url") != expected_url):
            return _public_error("upload_observation_unavailable", "The current document could not be completely observed; no media was sent")
        inputs = [item for item in observed.get("file_inputs", []) if isinstance(item, dict) and item.get("selector") == selector]
        if observed.get("selector_match_count") != 1 or observed.get("selector_is_file") is not True or len(inputs) != 1:
            return _public_error("upload_target_ambiguous", "Selector must identify exactly one observed input[type=file]; no replacement input was guessed")
        item = inputs[0]
        if item.get("disabled") is not False or self._input_identity(item) != previous["inputs"][selector]:
            return _public_error("upload_target_changed", "The observed file input changed or became disabled; read it again before selecting media")
        if item.get("selected_files"):
            return _public_error("upload_target_already_selected", "The file input already contains media; inspect the current upload instead of replacing it")
        # Native v73 stages bytes and performs DOM.setFileInputFiles itself.
        # This adapter never implements a second upload protocol or a JS setter.
        response = await client.execute("upload_file", {
            "tab_id": int(tab_id), "selector": selector, "file_name": media["name"],
            "file_data_base64": media["data_base64"], "wait_timeout_ms": wait},
            timeout=max(.01, remaining() - .1), _allow_upload_request=True)
        task = response.get("native_task") if isinstance(response.get("native_task"), dict) else {}
        task_id = response.get("task_id") or task.get("id") or task.get("taskId")
        state = task.get("state")
        if response.get("ok") is not True or state != "succeeded" or task.get("errorCode") or task.get("error"):
            # Terminal failure can follow a control mutation. Unless transport
            # explicitly proves no dispatch, preserve the fence and never replay.
            failure = _public_error(str(task.get("errorCode") or (response.get("error") or {}).get("code") or "upload_native_task_incomplete"),
                "Native upload did not verify file selection; inspect the original task and page before any new upload",
                task_id=task_id, unknown=response.get("action_may_have_run") is not False,
                native_state=state or response.get("status"), native_task=task)
            failure.update(stages={"file_selected": None, "upload_started": None, "server_received": None, "platform_processed": None},
                           follow_up_required=True)
            return failure
        # v73 succeeded means names/count were read back from the file control.
        # Local size/hash describe the validated source, not server acceptance.
        selected_receipt = upload_receipt(task_id or "native-upload", "selected", selected=True)
        selected_receipt.pop("upload_id", None)
        selected_receipt.pop("idempotency_key", None)
        return {**selected_receipt,
                "verified": True, "tab_id": tab_id, "native_task_id": task_id,
                "native_state": state, "native_terminal": True, "native_task": _public_task(task),
                "adapter": "native_ipc", "verification": "native_file_control_names_and_count",
                "selected_file": {"name": media["name"], "size": media["size"], "type": media["mime_type"]},
                "local_media": {"name": media["name"], "size": media["size"], "sha256": media["sha256"]}}

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
