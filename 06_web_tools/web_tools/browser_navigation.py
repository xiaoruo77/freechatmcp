"""Validate authenticated extension navigation receipts against fresh tab facts."""
from __future__ import annotations

from urllib.parse import urlsplit
from datetime import datetime

PROTOCOL = "xiaoruo1-edge-navigation/1"


def safe_url(value):
    if not isinstance(value, str) or not value or len(value) > 4000:
        return None
    try:
        url = urlsplit(value)
        _ = url.port
        return url if url.scheme in {"http", "https"} and url.hostname and not url.username and not url.password else None
    except ValueError:
        return None


def _origin(url):
    return url.scheme, url.hostname, url.port or (443 if url.scheme == "https" else 80)


def navigation_target(data, *, source_tab_id, source_url, tabs, frame_id=0, allow_active=False):
    """Return one exact continuation only when every independent fact agrees.

    A changed URL alone never proves a click. The expected destination must
    originate in the extension's fixed observed-anchor preflight, and the tab
    must still belong to the selected authenticated connection.
    """
    if not isinstance(data, dict) or data.get("navigation_protocol") != PROTOCOL or data.get("operation_no_replay") is not True:
        return None
    source, target = data.get("navigation_source"), data.get("navigation_target")
    if not isinstance(source, dict) or not isinstance(target, dict):
        return None
    expected, before, after = safe_url(source.get("expected_url")), safe_url(source_url), safe_url(target.get("url"))
    if not expected or not before or not after:
        return None
    if (str(source.get("source_tab_id", "")) != str(source_tab_id) or source.get("source_url") != source_url
            or type(source.get("frame_id")) is not int or source["frame_id"] != frame_id
            or target.get("navigation_verified") is not True or target.get("status") not in {"complete", "loading"}
            or type(target.get("created")) is not bool):
        return None
    # Native submits may add query parameters, but a different endpoint is
    # insufficient evidence for a submit whose renderer disappeared.
    if data.get("dispatch_semantics") == "dom_submit_observed_navigation" and (
            target["created"] or frame_id != 0 or source.get("target") not in {"_self", "_top"}
            or _origin(after) != _origin(expected) or after.path != expected.path
            or target["url"] == source_url):
        return None
    # Redirects within an observed anchor's intended origin are accepted.
    https_upgrade = (expected.scheme == "http" and after.scheme == "https"
        and expected.hostname == after.hostname and expected.port in {None, 80} and after.port in {None, 443}
        and expected.path == after.path and expected.query == after.query)
    if target["url"] != source["expected_url"] and _origin(after) != _origin(expected) and not https_upgrade:
        return None
    target_id = target.get("tab_id")
    if not isinstance(target_id, str) or not target_id or len(target_id) > 128:
        return None
    if target["created"] is False and target_id != str(source_tab_id):
        return None
    if target["created"] is False and target["url"] == source_url and source["expected_url"] != source_url:
        return None
    if target["created"] is True and target_id == str(source_tab_id):
        return None
    matches = [tab for tab in tabs if isinstance(tab, dict) and str(tab.get("tab_id", tab.get("id", ""))) == target_id]
    if len(matches) != 1 or matches[0].get("url") != target["url"] or (matches[0].get("active") is not False and not allow_active) or matches[0].get("pending_url") or matches[0].get("pendingUrl"):
        return None
    return {"tab_id": target_id, "url": target["url"], "created": target["created"],
            "page_read_required": True, "navigation_status": target["status"]}


def download_record(data, *, source_tab_id, source_url, downloads):
    """Confirm a fixed download receipt with the browser's current download list."""
    if not isinstance(data, dict) or data.get("download_protocol") != "xiaoruo1-edge-download/1" or data.get("operation_no_replay") is not True:
        return None
    source, claimed = data.get("download_source"), data.get("download")
    if not isinstance(source, dict) or not isinstance(claimed, dict):
        return None
    expected = source.get("expected_url")
    if (str(source.get("source_tab_id", "")) != str(source_tab_id) or source.get("source_url") != source_url
            or safe_url(expected) is None or type(claimed.get("id")) is not int or claimed["id"] < 0):
        return None
    matches = [item for item in downloads if isinstance(item, dict) and type(item.get("id")) is int and item["id"] == claimed["id"]]
    if len(matches) != 1:
        return None
    item = matches[0]
    if (expected not in {item.get("url"), item.get("final_url")} or item.get("state") not in {"complete", "in_progress", "interrupted"}
            or (claimed.get("filename") and item.get("filename") != claimed["filename"])):
        return None
    return {key: item.get(key) for key in ("id", "state", "filename", "file_path", "url", "final_url", "exists", "bytes_received", "total_bytes")}


def current_navigation(data, *, source_tab_id, source_url, tabs, frame_id=0, allow_active=False):
    """Finish an observed navigation with later browser facts, never a new click."""
    if not isinstance(data, dict) or data.get("action_may_have_run") is not True or data.get("operation_no_replay") is not True:
        return None
    target = data.get("navigation_target")
    failure = data.get("error")
    code = failure.get("code") if isinstance(failure, dict) else None
    if (not isinstance(target, dict) or target.get("navigation_verified") is not False
            or code not in {"browser_navigation_pending", "browser_click_result_unknown", "extension_background_context_changed", "extension_dom_context_changed"}):
        return None
    matches = [tab for tab in tabs if isinstance(tab, dict) and str(tab.get("tab_id", tab.get("id", ""))) == target.get("tab_id")]
    if len(matches) != 1 or matches[0].get("pending_url") or matches[0].get("pendingUrl"):
        return None
    current = matches[0]
    candidate = {**data, "navigation_target": {**target, "url": current.get("url"),
        "status": current.get("status", target.get("status")), "navigation_verified": True}}
    verified = navigation_target(candidate, source_tab_id=source_tab_id, source_url=source_url,
        tabs=tabs, frame_id=frame_id, allow_active=allow_active)
    return {"continuation": verified, "receipt": candidate} if verified else None


def current_download(data, *, source_tab_id, source_url, downloads, baseline_ids, dispatched_at):
    if baseline_ids is None or not isinstance(data, dict) or data.get("action_may_have_run") is not True:
        return None
    source = data.get("download_source")
    if not isinstance(source, dict) or data.get("download_protocol") != "xiaoruo1-edge-download/1" or data.get("operation_no_replay") is not True:
        return None
    if str(source.get("source_tab_id", "")) != str(source_tab_id) or source.get("source_url") != source_url or safe_url(source.get("expected_url")) is None:
        return None
    candidates = []
    for item in downloads:
        if not isinstance(item, dict) or type(item.get("id")) is not int or item["id"] in baseline_ids:
            continue
        try:
            started = datetime.fromisoformat(str(item.get("start_time", "")).replace("Z", "+00:00")).timestamp()
        except (ValueError, TypeError):
            continue
        if started < dispatched_at or source["expected_url"] not in {item.get("url"), item.get("final_url")}:
            continue
        candidates.append(item)
    if len(candidates) != 1:
        return None
    candidate = {**data, "download": candidates[0]}
    verified = download_record(candidate, source_tab_id=source_tab_id, source_url=source_url, downloads=downloads)
    return verified
