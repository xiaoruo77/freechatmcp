"""A click continuation requires an observed anchor and fresh exact-tab facts."""
from __future__ import annotations

from copy import deepcopy
import json

import pytest

from test_edge_recovery_receipts import Runtime, BROWSER, GEN, START
from web_tools.browser_navigation import PROTOCOL, navigation_target
from web_tools.unified_browser import UnifiedBrowserService


DESTINATION = "https://fixture.test/catalog/62"


def proof(*, expected=DESTINATION, final=None, target_id="7", created=False,
          source_id="7", source_url=START, frame_id=0, status="complete"):
    final = expected if final is None else final
    return {"ok": True, "verified": False,
        "background_protocol": "xiaoruo1-edge-dom-background/1",
        "tab_id": source_id, "navigation_protocol": PROTOCOL,
        "operation_no_replay": True,
        "navigation_source": {"source_tab_id": source_id, "source_url": source_url,
            "expected_url": expected, "target": "_blank" if created else "_self",
            "frame_id": frame_id},
        "navigation_target": {"tab_id": target_id, "url": final, "created": created,
            "status": status, "navigation_verified": True}}


def facts(value):
    target = value["navigation_target"]
    return [{"tab_id": target["tab_id"], "url": target["url"], "active": False}]


def validate(value, tabs=None, **options):
    return navigation_target(value, source_tab_id="7", source_url=START,
        tabs=facts(value) if tabs is None else tabs, **options)


@pytest.mark.parametrize("expected,final,created,target_id,status", [
    (DESTINATION, DESTINATION, False, "7", "complete"),
    ("https://publisher.test/start", "https://publisher.test/start", False, "7", "complete"),
    ("https://publisher.test/start", "https://publisher.test/article/record", False, "7", "complete"),
    (DESTINATION, DESTINATION, True, "30", "complete"),
    ("https://publisher.test/article", "https://publisher.test/article", True, "30", "loading"),
    (DESTINATION, DESTINATION, False, "7", "loading"),
])
def test_valid_expected_navigation_has_one_exact_read_continuation(expected, final, created, target_id, status):
    value = proof(expected=expected, final=final, created=created, target_id=target_id, status=status)
    assert validate(value) == {"tab_id": target_id, "url": final, "created": created,
        "page_read_required": True, "navigation_status": status}


def test_explicit_frame_uses_its_observed_anchor_receipt_only():
    value = proof(frame_id=4)
    assert validate(value, frame_id=4)["tab_id"] == "7"
    assert validate(value) is None


def test_http_to_https_upgrade_preserves_expected_host_path_and_query():
    value = proof(expected="http://publisher.test/article?id=62",
                  final="https://publisher.test/article?id=62")
    assert validate(value) is not None


def test_native_submit_confirmation_requires_exact_endpoint_and_source_tab():
    value = proof(expected="https://fixture.test/search", final="https://fixture.test/search?q=fixture")
    value["dispatch_semantics"] = "dom_submit_observed_navigation"
    assert validate(value)["tab_id"] == "7"

    different_endpoint = deepcopy(value)
    different_endpoint["navigation_target"]["url"] = "https://fixture.test/account"
    assert validate(different_endpoint) is None

    opened_window = proof(expected="https://fixture.test/search", created=True, target_id="30")
    opened_window["dispatch_semantics"] = "dom_submit_observed_navigation"
    assert validate(opened_window) is None


@pytest.mark.parametrize("variant", [
    "protocol_missing", "protocol_unknown", "no_replay_missing", "no_replay_false",
    "no_replay_integer", "source_missing", "target_missing", "wrong_source_tab",
    "wrong_source_url", "wrong_frame", "boolean_frame", "missing_frame",
    "unsafe_expected", "credential_expected", "unsafe_final", "credential_final",
    "unexpected_final_origin", "target_id_missing", "target_id_empty", "target_id_overlong",
    "wrong_existing_target", "created_same_source", "created_missing", "created_integer",
    "created_none", "navigation_unverified", "navigation_verified_integer", "status_unknown",
    "fresh_target_missing", "fresh_target_duplicated", "fresh_target_active",
    "fresh_target_active_unknown", "fresh_target_url_mismatch", "http_upgrade_wrong_path",
    "http_upgrade_wrong_query", "http_upgrade_wrong_host", "https_downgrade",
])
def test_malformed_navigation_receipts_never_release_a_write_fence(variant):
    value = proof()
    tabs = facts(value)
    source, target = value["navigation_source"], value["navigation_target"]
    if variant == "protocol_missing": value.pop("navigation_protocol")
    elif variant == "protocol_unknown": value["navigation_protocol"] = "unrecognized/1"
    elif variant == "no_replay_missing": value.pop("operation_no_replay")
    elif variant == "no_replay_false": value["operation_no_replay"] = False
    elif variant == "no_replay_integer": value["operation_no_replay"] = 1
    elif variant == "source_missing": value.pop("navigation_source")
    elif variant == "target_missing": value.pop("navigation_target")
    elif variant == "wrong_source_tab": source["source_tab_id"] = "8"
    elif variant == "wrong_source_url": source["source_url"] = "https://fixture.test/unobserved"
    elif variant == "wrong_frame": source["frame_id"] = 4
    elif variant == "boolean_frame": source["frame_id"] = False
    elif variant == "missing_frame": source.pop("frame_id")
    elif variant == "unsafe_expected": source["expected_url"] = "javascript:alert(1)"
    elif variant == "credential_expected": source["expected_url"] = "https://user:secret@fixture.test/catalog/62"
    elif variant == "unsafe_final": target["url"] = tabs[0]["url"] = "file:///local/private"
    elif variant == "credential_final": target["url"] = tabs[0]["url"] = "https://user:secret@fixture.test/catalog/62"
    elif variant == "unexpected_final_origin": target["url"] = tabs[0]["url"] = "https://unobserved.test/record"
    elif variant == "target_id_missing": target.pop("tab_id")
    elif variant == "target_id_empty": target["tab_id"] = ""
    elif variant == "target_id_overlong": target["tab_id"] = "x" * 129
    elif variant == "wrong_existing_target": target["tab_id"] = tabs[0]["tab_id"] = "30"
    elif variant == "created_same_source": target["created"] = True
    elif variant == "created_missing": target.pop("created")
    elif variant == "created_integer": target["created"] = 1
    elif variant == "created_none": target["created"] = None
    elif variant == "navigation_unverified": target["navigation_verified"] = False
    elif variant == "navigation_verified_integer": target["navigation_verified"] = 1
    elif variant == "status_unknown": target["status"] = "unknown"
    elif variant == "fresh_target_missing": tabs.clear()
    elif variant == "fresh_target_duplicated": tabs.append(deepcopy(tabs[0]))
    elif variant == "fresh_target_active": tabs[0]["active"] = True
    elif variant == "fresh_target_active_unknown": tabs[0].pop("active")
    elif variant == "fresh_target_url_mismatch": tabs[0]["url"] = "https://fixture.test/other"
    elif variant.startswith("http_upgrade_"):
        source["expected_url"] = "http://publisher.test/article?id=62"
        target["url"] = tabs[0]["url"] = {
            "http_upgrade_wrong_path": "https://publisher.test/unrelated?id=62",
            "http_upgrade_wrong_query": "https://publisher.test/article?id=99",
            "http_upgrade_wrong_host": "https://unobserved.test/article?id=62",
        }[variant]
    elif variant == "https_downgrade":
        source["expected_url"] = "https://publisher.test/article"
        target["url"] = tabs[0]["url"] = "http://publisher.test/article"
    assert validate(value, tabs) is None


@pytest.fixture
def setup(tmp_path):
    runtime = Runtime()
    service = UnifiedBrowserService(tmp_path / "receipts.sqlite3", runtime.discover,
        runtime.dispatch, background_check=runtime.session)
    return runtime, service


@pytest.mark.parametrize("created,expected,status", [
    (False, DESTINATION, "complete"),
    (False, "https://publisher.test/article", "loading"),
    (True, "https://publisher.test/article", "complete"),
])
async def test_a_single_click_completes_with_exact_next_target_without_replay(setup, created, expected, status):
    runtime, service = setup
    target_id = "30" if created else "7"
    receipt = proof(expected=expected, target_id=target_id, created=created, status=status)
    async def navigate(method, payload):
        assert method == "click"
        if created:
            runtime.tabs.append({"tab_id": target_id, "window_id": "1", "active": False, "url": expected})
        else:
            runtime.tabs[0]["url"] = expected
        return deepcopy(receipt)
    runtime.handler = navigate
    args = {"execution_mode": "extension_background", "expected_generation": GEN, "selector": "#observed-link"}
    first = await service.control(BROWSER, "click", "7", args, "navigation-once-0001", True, 2000)
    assert first["ok"] and first["status"] == "completed", first
    assert first["tab_id"] == "7"
    assert first["next_target"]["tab_id"] == target_id
    assert first["result"]["navigation_target"]["url"] == expected
    assert first["next_target"]["browser_id"] == BROWSER
    assert first["next_target"]["expected_generation"] == GEN
    assert first["next_target"]["read_required"] is True
    row = service.receipts.get("navigation-once-0001")
    assert row["tab_id"] == "7" and row["state"] == "completed"
    assert json.loads(row["result"])["next_target"]["tab_id"] == target_id
    duplicate = await service.control(BROWSER, "click", "7", args, "navigation-once-0001", True, 2000)
    assert duplicate["duplicate_call"] and duplicate["redispatched"] is False
    assert len(runtime.writes) == 1


async def test_an_unrelated_destination_stays_unverified_and_blocks_replay(setup):
    runtime, service = setup
    async def unproven(method, payload):
        runtime.tabs[0]["url"] = "https://unobserved.test/record"
        return proof(final="https://unobserved.test/record")
    runtime.handler = unproven
    args = {"execution_mode": "extension_background", "expected_generation": GEN, "selector": "#observed-link"}
    first = await service.control(BROWSER, "click", "7", args, "navigation-unproven-0001", True, 2000)
    assert not first["ok"] and first["status"] == "dispatched_unverified"
    assert "next_target" not in first
    second = await service.control(BROWSER, "click", "7", args, "navigation-unproven-0002", True, 2000)
    assert second["error"]["code"] == "browser_target_fenced"
    assert len(runtime.writes) == 1


@pytest.mark.parametrize("expected", [DESTINATION, "https://publisher.test/article"])
async def test_context_destruction_is_recovered_only_with_complete_navigation_proof(setup, expected):
    runtime, service = setup
    async def navigation_after_context_loss(method, payload):
        runtime.tabs[0]["url"] = expected
        return {**proof(expected=expected), "ok": False, "action_may_have_run": True,
            "error": {"code": "extension_background_context_changed", "phase": "script_dispatch"}}
    runtime.handler = navigation_after_context_loss
    args = {"execution_mode": "extension_background", "expected_generation": GEN, "selector": "#observed-link"}
    recovered = await service.control(BROWSER, "click", "7", args, "navigation-context-proven-0001", True, 2000)
    assert recovered["ok"] and recovered["status"] == "completed"
    assert recovered["result"]["recovered_after_navigation"] is True
    assert recovered["result"]["original_script_error"]["code"] == "extension_background_context_changed"
    assert recovered["next_target"]["tab_id"] == "7" and recovered["next_target"]["read_required"] is True
    assert len(runtime.writes) == 1


async def test_bare_verified_flag_cannot_override_an_invalid_navigation_receipt(setup):
    runtime, service = setup
    async def unrelated(method, payload):
        runtime.tabs[0]["url"] = "https://unobserved.test/record"
        return {**proof(final="https://unobserved.test/record"), "verified": True}
    runtime.handler = unrelated
    response = await service.control(BROWSER, "click", "7", {
        "execution_mode": "extension_background", "expected_generation": GEN, "selector": "#observed-link"},
        "navigation-invalid-bare-verified", True, 2000)
    assert response["status"] == "dispatched_unverified" and not response["ok"]
    assert "next_target" not in response and len(runtime.writes) == 1


async def test_old_context_loss_without_navigation_proof_cannot_be_promoted_by_changed_url(setup):
    runtime, service = setup
    async def lost_context(method, payload):
        runtime.tabs[0]["url"] = DESTINATION
        return {"ok": False, "verified": False, "action_may_have_run": True,
            "tab_id": "7", "background_protocol": "xiaoruo1-edge-dom-background/1",
            "error": {"code": "extension_background_context_changed"}}
    runtime.handler = lost_context
    original = await service.control(BROWSER, "click", "7", {
        "execution_mode": "extension_background", "expected_generation": GEN, "selector": "#observed-link"},
        "navigation-old-context-0001", True, 2000)
    assert original["status"] == "outcome_unknown" and "next_target" not in original
    runtime.handler = None
    observation = await service.control(BROWSER, "read", "7", {"execution_mode": "extension_background"})
    recovered = await service.control(BROWSER, "reconcile", "7", {
        "operation_id": "navigation-old-context-0001", "observation_id": observation["observation_id"],
        "decision": "verified", "evidence": "The changed URL alone does not prove how the old unknown click completed."}, confirm=True)
    assert recovered["error"]["code"] == "browser_reconciliation_page_changed"
    assert service.receipts.get("navigation-old-context-0001")["state"] == "outcome_unknown"
    assert len(runtime.writes) == 1
