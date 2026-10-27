from __future__ import annotations

import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EXTENSION_ROOT = ROOT / "10_web_deploy" / "edge_extension"


def test_edge_extension_read_path_is_bounded_and_versioned():
    manifest = json.loads((EXTENSION_ROOT / "manifest.json").read_text(encoding="utf-8"))
    source = (EXTENSION_ROOT / "background.js").read_text(encoding="utf-8")

    # The extension version is intentionally bumped when the bridge protocol
    # changes.  Pinning an old literal here made a valid deployed extension
    # fail the suite and masked the actual bounded-read assertions below.
    assert re.fullmatch(r"\d+\.\d+\.\d+", str(manifest["version"]))
    assert "downloads" in manifest["permissions"]
    assert manifest["content_scripts"][0]["js"] == ["reader.js"]
    assert "READ_LOAD_GRACE_MS = 2500" in source
    assert "READ_CONTENT_TIMEOUT_MS = 6000" in source
    assert "READ_SCRIPT_TIMEOUT_MS = 8000" in source
    assert "READ_FALLBACK_TIMEOUT_MS = 6000" in source
    assert "READ_RECOVERY_LOAD_TIMEOUT_MS = 5000" in source
    assert "READ_RECOVERY_MESSAGE_TIMEOUT_MS = 4000" in source
    assert "OPEN_TAB_READABLE_WAIT_MS = 20000" in source
    assert "READ_STARTUP_URL_WAIT_MS = 5000" in source
    assert "waitForReadablePageUrl" in source
    assert "isTransientStartupUrl" in source
    assert "recoverReadFromDuplicate" in source
    assert "waitForComplete(id, READ_LOAD_GRACE_MS)" in source
    assert "page_status: tab.status || \"unknown\"" in source
    assert "readViaContentScript" in source
    assert "waitForNewDownload" in source
    assert "downloadSummary" in source
    assert "list_downloads: listDownloads" in source
    assert "world: \"MAIN\"" in source
    assert "clickData = await executeMain" in source
    assert "installDownloadSafetyHooks" in source
    assert "select_option: selectOption" in source
    assert "function selectOptionInPage" in source


def test_edge_extension_reader_is_local_and_bounded():
    source = (EXTENSION_ROOT / "reader.js").read_text(encoding="utf-8")

    assert "chrome.runtime.onMessage.addListener" in source
    assert "__xiaoruo1ReaderInstalled" in source
    assert "Math.min(100000" in source
    assert "const scope = root" in source
    assert "data-testid" in source


def test_browser_read_request_rejects_unknown_mode():
    import pytest
    from pydantic import ValidationError

    from web_tools.browser_bridge import ReadRequest

    assert ReadRequest(mode="fast").mode == "fast"
    with pytest.raises(ValidationError):
        ReadRequest(mode="unbounded")