"""Evidence checks for inactive Edge tabs, independent of desktop input leases."""
from __future__ import annotations

from urllib.parse import urlsplit

PROTOCOL = "xiaoruo1-edge-dom-background/1"
DOM_PROTOCOL = "xiaoruo1-edge-dom/1"
DOM_ACTIONS = {"open_tab", "close_tab", "activate_tab", "navigate", "read", "click", "type", "press", "select", "scroll", "wait", "screenshot"}
ACTIONS = {"open_tab", "close_tab", "navigate", "read", "click", "type", "press", "select", "scroll", "wait"}


def _http_url(value):
    try:
        parsed = urlsplit(value) if isinstance(value, str) and len(value) <= 4000 else None
        return parsed if (parsed and parsed.scheme in {"http", "https"} and parsed.hostname
                          and not parsed.username and not parsed.password) else None
    except ValueError:
        return None


def navigation_verified(data, requested_url, actual_url, *, action, protocol=PROTOCOL):
    """A normal redirect can differ from the request; exact fresh tab evidence wins."""
    return (isinstance(data, dict) and data.get("background_protocol" if protocol == PROTOCOL else "dom_protocol") == protocol
            and data.get("navigation_verified") is True and data.get("requested_url") == requested_url
            and data.get("final_url") == actual_url and data.get("url") == actual_url
            and data.get("verified") is True and _http_url(requested_url) is not None
            and _http_url(actual_url) is not None and (action != "open_tab" or data.get("created") is True))


def guarded_click_transition(data, before_url, observed_url):
    """Only recorded same-origin Edge navigation can support explicit reconciliation."""
    if not isinstance(data, dict):
        return False
    guard = data.get("background_execution")
    guard = guard if isinstance(guard, dict) else {}
    before, after = _http_url(before_url), _http_url(observed_url)
    try:
        same_origin = bool(before and after and before.scheme == after.scheme
            and before.hostname == after.hostname and before.port == after.port)
    except ValueError:
        return False
    initial, final = guard.get("before"), guard.get("after")
    return (same_origin and data.get("background_protocol") == PROTOCOL
            and data.get("input_semantics") == "synthetic_dom_events_untrusted"
            and data.get("url_transition") is True and data.get("before_url") == before_url
            and data.get("after_url") == observed_url and guard.get("protocol") == PROTOCOL
            and guard.get("target_inactive") is True and guard.get("no_system_input") is True
            and isinstance(initial, dict) and isinstance(final, dict) and initial == final
            and isinstance(initial.get("active_tabs"), dict) and bool(initial["active_tabs"])
            and "focused_window_id" in initial)


def identity(browser, action, error):
    if (browser.get("adapter") != "authenticated_extension" or browser.get("browser_kind") != "edge"
            or browser.get("stable_identity") is not True or browser.get("background_protocol") != PROTOCOL):
        raise error("extension_background_upgrade_required", "Edge background control requires the updated authenticated Xiaoruo1 extension; reload version 1.9.1 or later")
    if action not in ACTIONS or action not in browser.get("background_actions", []):
        raise error("extension_background_action_unsupported", "This action is unavailable without activating Edge; no foreground fallback was attempted")


def snapshot(value, tab_id, phase, error, *, expected=None, allow_missing=False, independent_target=False):
    if not isinstance(value, dict) or value.get("background_protocol") != PROTOCOL:
        raise error("extension_background_state_unverified", "Edge did not return the background protocol evidence")
    state = value.get("background_state")
    state = state if isinstance(state, dict) else {}
    active, focused = state.get("active_tabs"), state.get("focused_window_id")
    tabs = value.get("tabs")
    if (not isinstance(active, dict) or not active or len(active) > 100
            or any(not isinstance(k, str) or not k or not isinstance(v, str) or not v for k, v in active.items())
            or "focused_window_id" not in state or (focused is not None and focused not in active)
            or not isinstance(tabs, list)):
        raise error("extension_background_state_unverified", "Edge active-window evidence is incomplete")
    actual = {}
    for tab in tabs:
        if isinstance(tab, dict) and tab.get("active") is True:
            window = str(tab.get("window_id", ""))
            if window in actual:
                raise error("extension_background_state_unverified", "Ambiguous active tabs in an Edge window")
            actual[window] = str(tab.get("tab_id", tab.get("id", "")))
    if actual != active:
        raise error("extension_background_state_unverified", "Edge active-tab evidence disagrees with its tab list")
    changed = expected is not None and (active != expected["active_tabs"] or focused != expected["focused_window_id"])
    if changed and not independent_target:
        raise error("extension_background_active_changed", "The user's active tab or window changed; no focus restoration or replay was attempted")
    target_window_id = None
    if tab_id:
        targets = [t for t in tabs if isinstance(t, dict) and str(t.get("tab_id", t.get("id", ""))) == tab_id]
        if not targets and allow_missing:
            pass
        elif len(targets) != 1 or targets[0].get("active") is not False or tab_id in active.values():
            raise error("foreground_required", "The target must remain an explicit inactive Edge tab; the user's current tab was not changed")
        else:
            target_window_id = str(targets[0].get("window_id", ""))
            if independent_target and expected and expected.get("target_window_id") and target_window_id != expected["target_window_id"]:
                raise error("extension_background_target_moved", "The observed target changed windows; inspect it before another action")
    return {"phase": phase, "active_tabs": dict(active), "focused_window_id": focused,
        **({"target_window_id": target_window_id, "unrelated_user_activity_observed": bool(changed)} if independent_target else {})}
