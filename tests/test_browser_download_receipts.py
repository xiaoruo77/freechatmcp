"""A download is credited only to its exact current browser record."""
from copy import deepcopy

import pytest

from web_tools.browser_navigation import download_record

SOURCE = "https://fixture.test/article"
EXPECTED = "https://fixture.test/download.txt"


def receipt():
    return {"download_protocol": "xiaoruo1-edge-download/1", "operation_no_replay": True,
        "download_source": {"source_tab_id": "7", "source_url": SOURCE, "expected_url": EXPECTED},
        "download": {"id": 42, "filename": "D:/Downloads/test.txt"}}


def listing(**overrides):
    return {"id": 42, "url": EXPECTED, "final_url": EXPECTED, "filename": "D:/Downloads/test.txt",
        "file_path": "D:/Downloads/test.txt", "state": "complete", "exists": True, **overrides}


@pytest.mark.parametrize("state", ["complete", "in_progress", "interrupted"])
def test_record_confirms_observation_without_promoting_incomplete_downloads(state):
    value = download_record(receipt(), source_tab_id="7", source_url=SOURCE, downloads=[listing(state=state)])
    assert value["id"] == 42 and value["state"] == state


@pytest.mark.parametrize("change", [
    {"download_protocol": "unknown"}, {"operation_no_replay": False},
    {"download_source": {"source_tab_id": "8", "source_url": SOURCE, "expected_url": EXPECTED}},
    {"download_source": {"source_tab_id": "7", "source_url": SOURCE + "/other", "expected_url": EXPECTED}},
    {"download_source": {"source_tab_id": "7", "source_url": SOURCE, "expected_url": "https://name:secret@fixture.test/file"}},
    {"download": {"id": True}}, {"download": {"id": -1}},
    {"download": {"id": 42, "filename": "C:/different.txt"}},
])
def test_unrelated_or_forged_receipts_do_not_confirm_download(change):
    data = receipt(); data.update(deepcopy(change))
    assert download_record(data, source_tab_id="7", source_url=SOURCE, downloads=[listing()]) is None


@pytest.mark.parametrize("items", [[], [listing(id=43)], [listing(), listing()],
    [listing(url="https://unrelated.test/file", final_url="https://unrelated.test/file")], [listing(state="unknown")]])
def test_missing_ambiguous_and_unrelated_current_records_are_rejected(items):
    assert download_record(receipt(), source_tab_id="7", source_url=SOURCE, downloads=items) is None
