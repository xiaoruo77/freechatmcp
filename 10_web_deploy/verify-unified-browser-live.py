"""Live verification through Xiaoruo1's public MCP only.

Never launches a browser, creates a profile, evaluates arbitrary scripts, uses
native input, requests debugger permission, or touches pre-existing tabs.
Exact private structured replies are saved locally; stdout is a compact summary.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import uuid

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "10_web_deploy/validation_runs/browser-unification-20261027/live"
FIXTURE = "https://mcp.example.com/upload-test/fixture.html"
BROWSERS = {
    "edge": "browser_1545f16e-ae3a-4ae3-a528-40d5384e5a2c",
    "cef": "browser_2f853da4-31dd-5c4c-8038-7c4e191ddbcf",
}
WRITE_ACTIONS = {"open_tab", "close_tab", "type", "click", "scroll"}


def decoded(reply):
    value = reply.structuredContent
    if value is None:
        texts = [part.text for part in reply.content if part.type == "text"]
        if len(texts) == 1:
            value = json.loads(texts[0])
    return value


def inner(value):
    """Unwrap the public operation envelope, retaining exact replies separately."""
    if isinstance(value, dict):
        envelope = value.get("result")
        if isinstance(envelope, dict) and isinstance(envelope.get("data"), dict):
            return envelope["data"]
    return value


class Harness:
    def __init__(self, session, run):
        self.session = session
        self.run = run
        self.calls = []
        self.checks = []
        self.owned = {}
        self.uncertain = set()
        self.originals = {}
        self.counter = 0
        self.resume_operations = {}

    def save(self):
        self.run.mkdir(parents=True, exist_ok=True)
        (self.run / "evidence.json").write_text(json.dumps({
            "fixture_url": FIXTURE, "calls": self.calls,
            "checks": self.checks, "owned_tabs": self.owned,
            "uncertain_browsers": sorted(self.uncertain),
            "original_tabs": self.originals,
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    async def call(self, label, tool, arguments):
        self.counter += 1
        reply = await self.session.call_tool(tool, arguments)
        data = decoded(reply)
        self.calls.append({"sequence": self.counter, "label": label,
            "utc": datetime.now(timezone.utc).isoformat(), "tool": tool,
            "arguments": arguments, "isError": bool(reply.isError),
            "structuredContent": data})
        self.save()
        return inner(data)

    def check(self, name, passed, detail=None):
        self.checks.append({"name": name, "passed": passed if passed is None else bool(passed),
                            "status": "uncertain" if passed is None else "passed" if passed else "failed",
                            "detail": detail})
        self.save()

    async def discover(self, label="discovery"):
        return await self.call(label, "browser_instances", {"timeout": 10000})

    async def control(self, label, browser, action, tab=None, args=None, operation=None):
        request = {"browser_id": browser, "action": action, "timeout": 30000,
                   "arguments": dict(args or {})}
        if tab is not None:
            request["tab_id"] = tab
        if action in WRITE_ACTIONS:
            if browser in self.uncertain:
                raise RuntimeError("write_blocked_after_unknown")
            found = await self.discover(label + "_generation")
            matches = [b for b in found.get("browsers", []) if b.get("browser_id") == browser and b.get("connected")]
            if len(matches) != 1:
                raise RuntimeError("browser_unavailable")
            request["arguments"]["expected_generation"] = matches[0]["connection_generation"]
            request.update(operation_id=operation or self.resume_operations.pop((browser, action), None) or "live_" + uuid.uuid4().hex, confirm=True)
        result = await self.call(label, "browser_control", request)
        # A pre-dispatch desktop rejection may be retried twice with the SAME key
        # and fingerprint after a public readiness check. Unknown is never replayed.
        for attempt in range(2):
            evidence = result.get("evidence", {})
            if action not in WRITE_ACTIONS or evidence.get("executed") is not False:
                break
            code = (result.get("error") or {}).get("code", "")
            if not code.startswith(("windows_", "desktop_", "upload_desktop_", "browser_transport_unavailable")):
                break
            status = await self.call(label + "_readiness", "windows_control_status", {})
            status_data = status.get("result", {}).get("data", status)
            if isinstance(status_data, str):
                try:
                    status_data = json.loads(status_data)
                except json.JSONDecodeError:
                    status_data = {}
            if isinstance(status_data, dict) and status_data.get("state") == "user" and attempt == 0:
                await asyncio.sleep(min(15, max(.2, float(status_data.get("wait_seconds", 10))) + .3))
                status_data = await self.call(label + "_readiness_after_wait", "windows_control_status", {})
            if not isinstance(status_data, dict) or status_data.get("state") not in {"ready", "ai"}:
                break
            result = await self.call(label + "_same_key_retry_" + str(attempt + 1), "browser_control", request)
        if action in WRITE_ACTIONS and (result.get("evidence", {}).get("executed") is None or result.get("status") in {"outcome_unknown", "dispatched_unverified"}):
            self.uncertain.add(browser)
            self.save()
        return result

    async def read(self, label, browser, tab, args=None):
        return await self.control(label, browser, "read", tab,
            {"max_chars": 20000, "settle_ms": 300, **(args or {})})

    async def tabs(self, label, browser):
        value = await self.control(label, browser, "list_tabs")
        return value.get("result", {}).get("tabs", [])

    @staticmethod
    def tab_map(tabs):
        return {str(t.get("tab_id", t.get("id", ""))): t.get("url") for t in tabs}

    async def verify_browser(self, name, browser):
        original_tabs = await self.tabs(name + "_original_tabs", browser)
        self.originals[browser] = self.tab_map(original_tabs)
        self.save()
        opened = await self.control(name + "_open_owned_tab", browser, "open_tab", args={"url": FIXTURE})
        self.check(name + "_owned_tab_opened", opened.get("ok") is True and opened.get("evidence", {}).get("verified") is True)
        tab = str((opened.get("result") or {}).get("tab_id", ""))
        if not tab:
            return
        if tab in self.originals[browser]:
            raise RuntimeError("open_returned_existing_tab")
        self.owned[browser] = tab
        self.save()
        page = await self.read(name + "_initial_read", browser, tab, {"include_frames": True})
        wire = page.get("result") or {}
        self.check(name + "_basic_read", page.get("ok") is True and "Unified browser interaction fixture" in wire.get("text", ""))
        if browser in self.uncertain:
            return
        text = "live-" + name + "-" + uuid.uuid4().hex[:8]
        typed = await self.control(name + "_basic_type", browser, "type", tab, {"by": "css", "selector": "#dynamic-input", "text": text, "clear": True})
        self.check(name + "_basic_type", typed.get("ok") is True and typed.get("evidence", {}).get("verified") is True)
        if browser in self.uncertain or typed.get("ok") is not True:
            await self.read(name + "_post_type_inspect", browser, tab)
            return
        clicked = await self.control(name + "_basic_click", browser, "click", tab, {"by": "css", "selector": "#dynamic-button"})
        await asyncio.sleep(.3)
        changed = await self.read(name + "_changed_read", browser, tab, {"selector": "#dynamic-state"})
        changed_text = (changed.get("result") or {}).get("text", "").strip()
        self.check(name + "_basic_click_changed", clicked.get("ok") is True and changed.get("ok") is True and changed_text == text,
                   {"expected": text, "observed": changed_text})
        if browser in self.uncertain:
            return
        if name == "edge":
            frames = wire.get("frames", [])
            frame = next((f for f in frames if f.get("frame_id") != 0 and "frame-input" in json.dumps(f)), None)
            if frame is None:
                frame = next((f for f in frames if f.get("frame_id") != 0), None)
            self.check("edge_frame_discovered", frame is not None, frames)
            if frame:
                await self.scoped("edge_frame", browser, tab, {"frame_id": frame["frame_id"]}, "frame")
            if browser not in self.uncertain:
                await self.scoped("edge_shadow", browser, tab, {"shadow_path": ["#shadow-host"]}, "shadow")
            if browser not in self.uncertain:
                scroll = await self.control("edge_scroll", browser, "scroll", tab, {"direction": "down", "pixels": 3000})
                scroll_wire = scroll.get("result") or {}
                self.check("edge_scroll_changed", scroll.get("ok") is True and (scroll_wire.get("after") or {}).get("y", 0) > (scroll_wire.get("before") or {}).get("y", 0), scroll_wire)
            status = await self.call("edge_debugger_default_status", "browser_debug_control", {"browser_id": browser, "tab_id": tab, "action": "status", "timeout": 10000})
            status_wire = status.get("result") or {}
            self.check("edge_debugger_default_ungranted", status.get("ok") is True and status_wire.get("available") is False and status_wire.get("attached") is False and status_wire.get("reason") in {"debugger_permission_not_granted", "debugger_api_unavailable"}, status_wire)
        else:
            # Read-only new capability probes. CEF 1.8.4 must refuse unsupported
            # scope/debug functionality rather than claiming success.
            for cap, args in (("frame", {"frame_id": 1}), ("shadow", {"shadow_path": ["#shadow-host"]})):
                probe = await self.read("cef_unsupported_" + cap, browser, tab, args)
                self.check("cef_" + cap + "_truthful_refusal", probe.get("ok") is False and probe.get("evidence", {}).get("executed") is False, probe.get("error"))
            debug = await self.call("cef_debugger_unsupported", "browser_debug_control", {"browser_id": browser, "tab_id": tab, "action": "status", "timeout": 10000})
            self.check("cef_debugger_truthful_refusal", debug.get("ok") is False and debug.get("evidence", {}).get("executed") is False, debug.get("error"))

    async def scoped(self, label, browser, tab, scope, prefix):
        before = await self.read(label + "_before", browser, tab, scope)
        expected = label + "-" + uuid.uuid4().hex[:8]
        self.check(label + "_initial_read", before.get("ok") is True and prefix + " unchanged" in (before.get("result") or {}).get("text", ""))
        typed = await self.control(label + "_type", browser, "type", tab, {**scope, "by": "css", "selector": "#" + prefix + "-input", "text": expected, "clear": True})
        self.check(label + "_typed", typed.get("ok") is True)
        if browser in self.uncertain or typed.get("ok") is not True:
            await self.read(label + "_inspect", browser, tab, scope)
            return
        click = await self.control(label + "_click", browser, "click", tab, {**scope, "by": "css", "selector": "#" + prefix + "-button"})
        after = await self.read(label + "_after", browser, tab, {**scope, "selector": "#" + prefix + "-state"})
        observed = (after.get("result") or {}).get("text", "").strip()
        self.check(label + "_click_changed", click.get("ok") is True and after.get("ok") is True and observed == expected, {"expected": expected, "observed": observed})

    async def finish(self):
        for name, browser in BROWSERS.items():
            if browser not in self.originals:
                continue
            current = self.tab_map(await self.tabs(name + "_final_tabs_before_cleanup", browser))
            changes = [{"tab_id": tab, "before": url, "after": current.get(tab), "attribution": "external_change_or_unproven"}
                       for tab, url in self.originals[browser].items() if current.get(tab) != url]
            self.check(name + "_original_tab_urls_preserved", None if changes else True,
                       {"changed": changes, "own_write_targets": [self.owned.get(browser)], "interpretation": "unchanged" if not changes else "uncertain_external_changes_no_write_sent_to_these_tabs"})
            tab = self.owned.get(browser)
            if tab and browser not in self.uncertain:
                closed = await self.control(name + "_close_owned_tab", browser, "close_tab", tab)
                self.check(name + "_owned_tab_closed", closed.get("ok") is True)
            elif tab:
                self.check(name + "_owned_tab_left_for_inspection", True, {"tab_id": tab, "reason": "uncertain_write_no_cleanup_write"})
        self.save()


async def main():
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--discover-only", action="store_true")
    parser.add_argument("--resume-preflight", type=Path)
    parser.add_argument("--edge-only", action="store_true")
    args = parser.parse_args()
    if args.edge_only:
        BROWSERS.pop("cef", None)
    run = OUT / datetime.now().strftime("%Y%m%d-%H%M%S")
    token = (ROOT / "08_web_data/web_agent.token").read_text(encoding="utf-8").strip()
    async with streamablehttp_client("http://127.0.0.1:48271/mcp", headers={"Authorization": "Bearer " + token}) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            h = Harness(session, run)
            if args.resume_preflight:
                prior_path = args.resume_preflight.resolve(strict=True)
                prior_path.relative_to(OUT.resolve())
                prior = json.loads(prior_path.read_text(encoding="utf-8"))
                for call in prior.get("calls", []):
                    request = call.get("arguments", {})
                    result = inner(call.get("structuredContent", {}))
                    if call.get("tool") == "browser_control" and request.get("action") == "open_tab" and result.get("evidence", {}).get("executed") is False:
                        h.resume_operations[(request["browser_id"], "open_tab")] = request["operation_id"]
            initial = await h.discover("initial_instances")
            cef = [b for b in initial.get("browsers", []) if b.get("connected") and b.get("manual_instance") is True and b.get("engine") == "cef"]
            if len(cef) == 1 and not args.edge_only:
                BROWSERS["cef"] = cef[0]["browser_id"]
            if args.discover_only:
                browsers = [{k: b.get(k) for k in ("browser_id", "connection_generation", "adapter", "version", "extension_version", "supported_actions", "capabilities", "tabs")} for b in initial.get("browsers", [])]
                print(json.dumps({"output": str(run / "evidence.json"), "browsers": browsers}, ensure_ascii=False))
                return
            try:
                for name, browser in BROWSERS.items():
                    try:
                        await h.verify_browser(name, browser)
                    except Exception as exc:
                        h.check(name + "_harness_completed", False, type(exc).__name__)
            finally:
                await h.finish()
            failed = [check["name"] for check in h.checks if check["passed"] is False]
            uncertain_checks = [check["name"] for check in h.checks if check["passed"] is None]
            summary = {"ok": not failed and not h.uncertain and not uncertain_checks,
                       "checks": len(h.checks), "failed": failed, "uncertain_checks": uncertain_checks,
                       "uncertain_browsers": sorted(h.uncertain), "owned_tabs": h.owned,
                       "output": str(run / "evidence.json")}
            (run / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())