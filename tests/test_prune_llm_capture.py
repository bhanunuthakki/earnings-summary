from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import pytest

from execution import prune_llm_capture
from llm import capture


def _capture_file(directory: Path, day: str, size: int) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"capture_{day}.jsonl"
    path.write_bytes(b"x" * size)
    return path


def _result(captured: str) -> dict[str, object]:
    parsed = json.loads(captured)
    assert isinstance(parsed, dict)
    return cast("dict[str, object]", parsed)


def test_pruner_fails_loud_without_deleting_in_window_capture(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    archive = tmp_path / "private-capture"
    recent = _capture_file(archive, "2099-01-01", 11)
    monkeypatch.setenv(capture.CAPTURE_ARCHIVE_DIR_ENV, str(archive))

    exit_code = prune_llm_capture.main(
        [
            "--repo-root",
            str(tmp_path),
            "--retention-days",
            "90",
            "--max-total-bytes",
            "10",
        ]
    )

    streams = capsys.readouterr()
    assert exit_code == 1
    assert recent.exists()
    assert _result(streams.out) == {
        "archive_count": 2,
        "deleted_files": 0,
        "retention_days": 90,
        "remaining_bytes": 11,
        "max_total_bytes": 10,
        "over_limit": True,
    }
    assert "exceeds configured byte ceiling" in streams.err


def test_pruner_applies_age_retention_before_byte_ceiling(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    archive = tmp_path / "private-capture"
    expired = _capture_file(archive, "2020-01-01", 20)
    recent = _capture_file(archive, "2099-01-01", 5)
    monkeypatch.setenv(capture.CAPTURE_ARCHIVE_DIR_ENV, str(archive))

    exit_code = prune_llm_capture.main(
        [
            "--repo-root",
            str(tmp_path),
            "--retention-days",
            "90",
            "--max-total-bytes",
            "10",
        ]
    )

    result = _result(capsys.readouterr().out)
    assert exit_code == 0
    assert not expired.exists()
    assert recent.exists()
    assert result["deleted_files"] == 1
    assert result["remaining_bytes"] == 5
    assert result["over_limit"] is False


def test_archive_bytes_ignores_unrecognized_files(tmp_path: Path) -> None:
    _capture_file(tmp_path, "2099-01-01", 7)
    (tmp_path / "notes.jsonl").write_bytes(b"x" * 100)

    assert capture.capture_archive_bytes(tmp_path, strict=True) == 7


def test_pruner_fails_loud_when_configured_archive_is_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv(capture.CAPTURE_ARCHIVE_DIR_ENV, str(tmp_path / "missing"))

    exit_code = prune_llm_capture.main(
        [
            "--repo-root",
            str(tmp_path),
            "--max-total-bytes",
            "10",
        ]
    )

    assert exit_code == 1
    assert "FileNotFoundError" in capsys.readouterr().err


def test_strict_archive_audit_rejects_a_file_root(tmp_path: Path) -> None:
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("x", encoding="utf-8")

    with pytest.raises(NotADirectoryError):
        capture.capture_archive_bytes(blocker, strict=True)
