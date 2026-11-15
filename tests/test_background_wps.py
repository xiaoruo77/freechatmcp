from pathlib import Path

import pytest

from web_tools.background_wps import WPSBackgroundError, checked_output, replacement_plan, text_sha256, wps_action


def test_literal_plan_requires_observed_hash_and_exact_count():
    text = "alpha beta alpha\r"
    count, output = replacement_plan(text, "alpha", "changed", 2, text_sha256(text))
    assert count == 2 and output == "changed beta changed\r"
    with pytest.raises(WPSBackgroundError, match="changed"):
        replacement_plan(text, "alpha", "changed", 2, "wrong")
    with pytest.raises(WPSBackgroundError, match="count"):
        replacement_plan(text, "alpha", "changed", 1, text_sha256(text))
    with pytest.raises(WPSBackgroundError, match="expressions"):
        replacement_plan(text, "^p", "replacement", 1, text_sha256(text))


def test_save_as_never_overwrites_source_or_existing_file(tmp_path):
    source = tmp_path / "source.docx"
    source.write_bytes(b"source")
    destination = tmp_path / "new.docx"
    assert checked_output(source, str(destination), [tmp_path]) == destination
    with pytest.raises(WPSBackgroundError):
        checked_output(source, str(source), [tmp_path])
    destination.write_bytes(b"existing")
    with pytest.raises(WPSBackgroundError):
        checked_output(source, str(destination), [tmp_path])


def test_target_must_be_exact_private_desktop_window(tmp_path):
    source = tmp_path / "source.docx"
    source.touch()
    with pytest.raises(WPSBackgroundError, match="private"):
        wps_action("read", {"hwnd": 42, "document_path": str(source)},
                   [{"hwnd": 42, "pid": 5, "desktop": "Default"}], tmp_path)


def test_arbitrary_application_actions_and_commands_are_unavailable(tmp_path):
    with pytest.raises(WPSBackgroundError):
        wps_action("execute_command", {}, [], tmp_path)
    with pytest.raises(WPSBackgroundError):
        wps_action("read", {"command": "anything"}, [], tmp_path)


@pytest.mark.asyncio
async def test_public_wps_route_never_sends_an_outside_document(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from web_mcp.direct_tools import DirectTools
    from web_mcp.paths import WorkspaceViolation
    base = tmp_path / "allowed"
    base.mkdir()
    private = tmp_path / "not-allowed.docx"
    private.write_text("test data")
    agent = object.__new__(DirectTools)
    agent.ctx = SimpleNamespace(settings=SimpleNamespace(admin_mode=True, allowed_workspaces=[base]))
    async def unexpected(*args):
        pytest.fail("Out-of-workspace paths must be rejected before the background service")
    monkeypatch.setattr("web_mcp.direct_tools.call_background", unexpected)
    with pytest.raises(WorkspaceViolation):
        await agent._background_desktop("wps", {"action": "read", "arguments": {"document_path": str(private)}})
