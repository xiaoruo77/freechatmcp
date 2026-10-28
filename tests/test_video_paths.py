from datetime import datetime, timedelta, timezone

import pytest

from web_tools import video_paths


@pytest.fixture
def video_root(tmp_path, monkeypatch):
    root = tmp_path / "video"
    monkeypatch.setattr(video_paths, "VIDEO_ROOT", root)
    monkeypatch.setattr(video_paths, "MCP_ROOT", tmp_path / "mcp")
    return root


def test_read_only_path_does_not_create(video_root):
    result = video_paths.video_project_path("2026-10-27")
    assert result == {
        "path": str((video_root / "2026_10" / "27").resolve()),
        "date": "2026-10-27", "timezone": "Asia/Shanghai",
        "exists": False, "created": False,
    }
    assert not video_root.exists()


def test_create_and_repeat_preserve_project_contents(video_root):
    first = video_paths.video_project_path("2024-02-29", create=True)
    target = video_root / "2024_02" / "29"
    artifact = target / "existing.mp4"
    artifact.write_bytes(b"existing video fixture")
    second = video_paths.video_project_path("2024-02-29", create=True)
    assert first["exists"] and first["created"]
    assert second["exists"] and not second["created"]
    assert artifact.read_bytes() == b"existing video fixture"


@pytest.mark.parametrize("value", [
    "2026-02-29", "2026-13-01", "2026-00-01", "2026-10-00", "2026-10-32",
    "2026-1-01", "20261027", "2026_10/27", "../2026-10-27", "2026-10-27/../../",
    "2026-10-27:secret", "2026-10-27 ", " 2026-10-27", "2026-10-27\n", "",
    "２０２６-１０-２７", 20261027, True, [], {},
])
def test_invalid_dates_do_not_create_anything(video_root, value):
    with pytest.raises(ValueError, match="date"):
        video_paths.video_project_path(value, create=True)
    assert not video_root.exists()


@pytest.mark.parametrize("value", ["true", "false", 1, 0, None])
def test_create_requires_real_boolean(video_root, value):
    with pytest.raises(ValueError, match="boolean"):
        video_paths.video_project_path("2026-10-27", create=value)
    assert not video_root.exists()


def test_default_date_is_shanghai(video_root):
    before = datetime.now(timezone(timedelta(hours=8))).date().isoformat()
    result = video_paths.video_project_path()
    after = datetime.now(timezone(timedelta(hours=8))).date().isoformat()
    assert result["date"] in {before, after}
    assert result["timezone"] == "Asia/Shanghai"


def test_year_is_always_four_digits(video_root):
    result = video_paths.video_project_path("0001-01-02")
    assert result["path"] == str((video_root / "0001_01" / "02").resolve())


def test_existing_video_root_cannot_be_file(video_root):
    video_root.write_text("preserve", encoding="utf-8")
    with pytest.raises(ValueError, match="not a directory"):
        video_paths.video_project_path("2026-10-27", create=True)
    assert video_root.read_text(encoding="utf-8") == "preserve"


def test_reparse_flag_rejected_before_creation(video_root, monkeypatch):
    from pathlib import Path
    from types import SimpleNamespace

    original = Path.lstat

    def observe(path, *args, **kwargs):
        if path == video_root:
            return SimpleNamespace(st_mode=0o040755, st_file_attributes=0x400)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", observe)
    with pytest.raises(ValueError, match="reparse"):
        video_paths.video_project_path("2026-10-27", create=True)
    assert not video_root.exists()


def test_mcp_location_is_prohibited(tmp_path, monkeypatch):
    monkeypatch.setattr(video_paths, "MCP_ROOT", tmp_path)
    monkeypatch.setattr(video_paths, "VIDEO_ROOT", tmp_path / "video")
    with pytest.raises(ValueError, match="MCP"):
        video_paths.video_project_path("2026-10-27", create=True)
    assert not (tmp_path / "video").exists()


def test_target_file_is_not_overwritten(video_root):
    month = video_root / "2026_10"
    month.mkdir(parents=True)
    target = month / "27"
    target.write_text("preserve", encoding="utf-8")
    with pytest.raises(ValueError, match="not a directory"):
        video_paths.video_project_path("2026-10-27", create=True)
    assert target.read_text(encoding="utf-8") == "preserve"


def test_symlink_escape_is_rejected(video_root, tmp_path):
    external = tmp_path / "external"
    external.mkdir()
    video_root.mkdir()
    try:
        (video_root / "2026_10").symlink_to(external, target_is_directory=True)
    except OSError:
        pytest.skip("OS does not grant symlink creation to this test process")
    with pytest.raises(ValueError, match="links|escaped"):
        video_paths.video_project_path("2026-10-27", create=True)
    assert not (external / "27").exists()