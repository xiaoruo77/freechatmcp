"""Restricted CDP actions for one private-desktop Edge instance.

The internal debugger endpoint is loopback only and never returned publicly.
The owning process must have the dedicated profile and a verified private HWND.
"""
from __future__ import annotations

import base64
import json
from pathlib import Path
import time
import uuid
from urllib.parse import urlsplit
from urllib.request import Request, ProxyHandler, build_opener

PORT = 48913


def _verify(profile: Path, windows: list[dict]) -> None:
    import psutil
    owners = [c for c in psutil.net_connections("tcp") if c.status == "LISTEN" and c.laddr.port == PORT]
    if not owners or any(c.laddr.ip != "127.0.0.1" or not c.pid for c in owners):
        raise RuntimeError("Private Edge debugger is unavailable or not loopback-only")
    private_pids = {w["pid"] for w in windows}
    for connection in owners:
        process = psutil.Process(connection.pid)
        args = process.cmdline()
        expected = "--user-data-dir=" + str(profile)
        if process.name().lower() != "msedge.exe" or expected.casefold() not in [a.casefold() for a in args] or process.pid not in private_pids:
            raise RuntimeError("Edge owner/profile/private-desktop verification failed")


def _http(path: str, *, method="GET"):
    request = Request(f"http://127.0.0.1:{PORT}" + path, method=method)
    with build_opener(ProxyHandler({})).open(request, timeout=5) as response:
        data = response.read(4 * 1024 * 1024 + 1)
    if len(data) > 4 * 1024 * 1024:
        raise ValueError("Edge metadata response exceeds limit")
    return json.loads(data)


class CDP:
    def __init__(self, target):
        import websocket
        url = target["webSocketDebuggerUrl"]
        parsed = urlsplit(url)
        if parsed.scheme != "ws" or parsed.hostname != "127.0.0.1" or parsed.port != PORT:
            raise RuntimeError("Unexpected private Edge debugger endpoint")
        self.ws = websocket.create_connection(url, timeout=10, suppress_origin=True, http_no_proxy=["127.0.0.1"])
        self.index = 0

    def command(self, method, params=None):
        self.index += 1
        self.ws.send(json.dumps({"id":self.index,"method":method,"params":params or {}}))
        for _ in range(200):
            result = json.loads(self.ws.recv())
            if result.get("id") == self.index:
                if "error" in result:raise RuntimeError("Private Edge protocol operation failed")
                return result.get("result",{})
        raise TimeoutError("Private Edge response was not received")

    def close(self):
        self.ws.close()


def edge_action(action: str, args: dict, *, data_root: Path, windows: list[dict]) -> dict:
    profile = data_root / "background_desktop" / "edge-profile"
    _verify(profile, windows)
    allowed = {
        "status":set(),"list_tabs":set(),"open_tab":{"url"},"navigate":{"tab_id","url"},
        "read":{"tab_id","max_chars"},"click":{"tab_id","selector"},
        "type":{"tab_id","selector","text"},"screenshot":{"tab_id"},
    }
    if action not in allowed or set(args) - allowed[action]:
        raise ValueError("Unsupported background Edge action/argument")
    pages = [p for p in _http("/json/list") if p.get("type") == "page"]
    def public(p):return {"tab_id":p["id"],"title":p.get("title"),"url":p.get("url")}
    if action in {"status","list_tabs"}:
        return {"ok":True,"browser":"edge_background","tabs":[public(p) for p in pages],"host_input_used":False}
    if action in {"open_tab","navigate"}:
        url = args.get("url","")
        if not isinstance(url,str) or urlsplit(url).scheme not in {"http","https"} or len(url)>8192:
            raise ValueError("Only an explicit HTTP(S) page URL is accepted")
        if action == "open_tab":
            from urllib.parse import quote
            return {"ok":True,"tab":public(_http("/json/new?"+quote(url,safe=""),method="PUT")),"host_input_used":False}
    selected = [p for p in pages if p["id"] == args.get("tab_id")]
    if len(selected) != 1:raise ValueError("Use an explicit current background Edge tab_id")
    page = selected[0]
    connection = CDP(page)
    try:
        if action == "navigate":
            result = connection.command("Page.navigate",{"url":url})
            if result.get("errorText"):raise RuntimeError("Page navigation failed: "+result["errorText"])
            return {"ok":True,"tab_id":page["id"],"url":url,"verification_required":True,"host_input_used":False}
        if action == "screenshot":
            result = connection.command("Page.captureScreenshot",{"format":"png","captureBeyondViewport":False})
            raw = base64.b64decode(result["data"],validate=True)
            if not 100 < len(raw) <= 12*1024*1024:raise RuntimeError("Page capture size is invalid")
            path = data_root / "background_desktop" / "captures" / (uuid.uuid4().hex+".png")
            path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(raw)
            return {"ok":True,"tab_id":page["id"],"capture":{"path":str(path),"mime_type":"image/png","size_bytes":len(raw),
                "capture_method":"Edge Page.captureScreenshot","capture_scope":"private_edge_page","host_input_used":False}}
        if action == "read":
            limit = max(100,min(int(args.get("max_chars",10000)),40000))
            expression = "JSON.stringify({title:document.title,url:location.href,text:(document.body?.innerText||'').slice(0,"+str(limit)+"),controls:Array.from(document.querySelectorAll('a,button,input,textarea,select')).slice(0,80).map(e=>({tag:e.tagName,text:(e.innerText||e.getAttribute('aria-label')||'').slice(0,150),id:e.id,type:e.type,placeholder:e.placeholder}))})"
        else:
            selector=args.get("selector")
            if not isinstance(selector,str) or not 1<=len(selector)<=2000:raise ValueError("A specific observed CSS selector is required")
            lookup="const e=document.querySelector("+json.dumps(selector)+");if(!e)throw Error('target missing');"
            if action=="click": expression="(()=>{"+lookup+"e.click();return JSON.stringify({sent:true,verification_required:true})})()"
            else:
                text=args.get("text")
                if not isinstance(text,str) or len(text)>40000:raise ValueError("Text is too long")
                expression="(()=>{"+lookup+"if(!['INPUT','TEXTAREA'].includes(e.tagName)||e.type==='password')throw Error('unsupported target');const setter=Object.getOwnPropertyDescriptor(e.tagName==='INPUT'?HTMLInputElement.prototype:HTMLTextAreaElement.prototype,'value').set;setter.call(e,"+json.dumps(text)+");e.dispatchEvent(new Event('input',{bubbles:true}));e.dispatchEvent(new Event('change',{bubbles:true}));return JSON.stringify({sent:true,verification_required:true})})()"
        result = connection.command("Runtime.evaluate",{"expression":expression,"returnByValue":True})
        if result.get("exceptionDetails"):raise RuntimeError("Private page target operation failed")
        data=json.loads(result.get("result",{}).get("value","{}"))
        return {"ok":True,"tab_id":page["id"],"result":data,"host_input_used":False}
    finally:
        connection.close()
