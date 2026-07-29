"""Contract tests for the allowlist-only weekly filesystem cleanup CLI."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from _pytest.capture import CaptureFixture

from execution import run_weekly_cleanup as cleanup

NOW = datetime(2026, 7, 27, 20, 0, tzinfo=UTC)


def _age(path: Path, days: int) -> None:
    timestamp = (NOW - timedelta(days=days)).timestamp()
    os.utime(path, (timestamp, timestamp))


def _run(root: Path, *args: str) -> cleanup.CleanupSummary:
    return cleanup.run(["--repo-root", str(root), "--now", NOW.isoformat(), *args])


def test_dry_run_is_allowlist_only_and_reports_jsonl(
    tmp_path: Path, capsys: CaptureFixture[str]
) -> None:
    old_log = tmp_path / ".tmp" / "cron_logs" / "old.log"
    old_log.parent.mkdir(parents=True)
    old_log.write_text("old", encoding="utf-8")
    _age(old_log, 31)
    protected = tmp_path / "data" / "portfolio.db"
    protected.parent.mkdir()
    protected.write_text("never touch", encoding="utf-8")
    _age(protected, 90)
    checkpoint = tmp_path / ".tmp" / "cron_logs" / "state.json"
    checkpoint.write_text("{}", encoding="utf-8")
    _age(checkpoint, 90)
    lock = tmp_path / ".tmp" / "cron_runs" / "job_locks" / "weekly.lock"
    lock.parent.mkdir(parents=True)
    lock.write_text("locked", encoding="utf-8")
    _age(lock, 90)

    summary = _run(tmp_path)

    assert old_log.exists()
    assert protected.exists()
    assert checkpoint.exists()
    assert lock.exists()
    assert summary.mode == "dry_run"
    assert summary.idempotency_key == "weekly_cleanup:2026-W31:weekly-cleanup-v1"
    assert summary.would_delete == 1
    assert summary.deleted == 0
    assert summary.bytes == len("old")
    event = json.loads(capsys.readouterr().err.splitlines()[0])
    assert event["event"] == "cleanup_candidate"
    assert event["policy"] == "cron_logs_30d"


def test_apply_removes_old_allowlisted_files_and_is_idempotent(tmp_path: Path) -> None:
    old_run = tmp_path / ".tmp" / "cron_runs" / "nested" / "old.json"
    old_pdf = tmp_path / ".tmp" / "pdf_pages" / "old.png"
    fresh_log = tmp_path / ".tmp" / "cron_logs" / "fresh.log"
    for path in (old_run, old_pdf, fresh_log):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"abc")
    _age(old_run, 31)
    _age(old_pdf, 31)
    _age(fresh_log, 30)

    summary = _run(tmp_path, "--apply")

    assert not old_run.exists()
    assert not old_pdf.exists()
    assert fresh_log.exists()
    assert summary.deleted == 2
    assert summary.would_delete == 0
    assert summary.bytes == 6
    assert not (tmp_path / ".tmp" / "cron_runs" / "nested").exists()
    again = _run(tmp_path, "--apply")
    assert again.deleted == 0
    assert again.would_delete == 0


def test_news_cache_uses_payload_timestamp_and_preserves_bad_payloads(tmp_path: Path) -> None:
    cache = tmp_path / ".tmp" / "news_cache"
    cache.mkdir(parents=True)
    old = cache / "old.json"
    fresh = cache / "fresh.json"
    invalid = cache / "invalid.json"
    missing = cache / "missing.json"
    old.write_text(
        json.dumps({"cached_at": (NOW - timedelta(days=8)).isoformat()}), encoding="utf-8"
    )
    fresh.write_text(
        json.dumps({"cached_at": (NOW - timedelta(days=6)).isoformat()}), encoding="utf-8"
    )
    invalid.write_text("not json", encoding="utf-8")
    missing.write_text("{}", encoding="utf-8")
    # Deliberately old mtimes: bad/missing timestamps must not fall back to mtime.
    for path in (old, fresh, invalid, missing):
        _age(path, 90)

    summary = _run(tmp_path, "--apply")

    assert not old.exists()
    assert fresh.exists()
    assert invalid.exists()
    assert missing.exists()
    policy = summary.policies["news_cache_7d"]
    assert policy.deleted == 1
    assert policy.skipped_invalid == 2


def test_main_cache_policy_removes_only_old_cache_entries_and_excludes_claude(
    tmp_path: Path,
) -> None:
    old_pyc = tmp_path / "src" / "__pycache__" / "module.cpython-311.pyc"
    old_pytest = tmp_path / ".pytest_cache" / "v" / "cache" / "nodeids"
    old_ruff = tmp_path / ".ruff_cache" / "0" / "blob"
    protected_worktree = tmp_path / ".claude" / "worktrees" / "other" / "__pycache__" / "keep.pyc"
    protected_venv = tmp_path / "venv" / "lib" / "__pycache__" / "keep.pyc"
    source = tmp_path / "src" / "keep.py"
    for path in (old_pyc, old_pytest, old_ruff, protected_worktree, protected_venv, source):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"cache")
        _age(path, 8)

    summary = _run(tmp_path, "--apply")

    assert not old_pyc.exists()
    assert not old_pytest.exists()
    assert not old_ruff.exists()
    assert protected_worktree.exists()
    assert protected_venv.exists()
    assert source.exists()
    assert summary.policies["main_python_caches_7d"].deleted == 3


def test_symlink_is_never_followed_or_deleted(tmp_path: Path) -> None:
    target = tmp_path / "outside.log"
    target.write_text("do not follow", encoding="utf-8")
    _age(target, 90)
    link = tmp_path / ".tmp" / "cron_logs" / "linked.log"
    link.parent.mkdir(parents=True)
    try:
        link.symlink_to(target)
    except OSError:
        # Symlink creation can be unavailable on locked-down Windows hosts.
        return

    summary = _run(tmp_path, "--apply")

    assert link.is_symlink()
    assert target.exists()
    assert summary.deleted == 0
    assert summary.policies["cron_logs_30d"].skipped_unsafe == 1


def test_temp_audio_is_explicitly_skipped_without_qa_database_access(tmp_path: Path) -> None:
    audio = tmp_path / ".tmp" / "temp_audio_MSFT_Q1_2026.wav"
    audio.parent.mkdir(parents=True)
    audio.write_bytes(b"audio")
    _age(audio, 90)

    summary = _run(tmp_path, "--apply")

    assert audio.exists()
    assert summary.policies["temp_audio_qa_guard"].skipped_qa_unverified == 1


def test_main_fails_loudly_when_an_eligible_file_cannot_be_deleted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: CaptureFixture[str]
) -> None:
    old_log = tmp_path / ".tmp" / "cron_logs" / "locked.log"
    old_log.parent.mkdir(parents=True)
    old_log.write_text("locked", encoding="utf-8")
    _age(old_log, 31)

    original_unlink = Path.unlink

    def fail_target(path: Path, missing_ok: bool = False) -> None:
        if path == old_log:
            raise PermissionError("in use")
        original_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", fail_target)
    exit_code = cleanup.main(["--repo-root", str(tmp_path), "--now", NOW.isoformat(), "--apply"])

    assert exit_code == 1
    summary = json.loads(capsys.readouterr().out)
    assert summary["policies"]["cron_logs_30d"]["skipped_error"] == 1
    assert old_log.exists()
