"""Tests for execution/backfill_transcripts.py — subprocess phase targeting.

Focuses on the worktree-vs-main-repo path bug: when the script is run from a
worktree with `--repo-root <main>`, the `_run_ingest` and `_run_extract`
subprocesses must invoke the main repo's copy of the helper scripts AND set
`cwd=<main>`. Otherwise the helpers' own `Path(__file__).resolve().parents[1]`
lands in the worktree and `db.py` resolves to the worktree's stub DB.
"""

from __future__ import annotations

import hashlib
import importlib.util
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _load_module() -> Any:
    src = PROJECT_ROOT / "execution" / "backfill_transcripts.py"
    spec = importlib.util.spec_from_file_location("backfill_transcripts", src)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["backfill_transcripts"] = mod
    spec.loader.exec_module(mod)
    return mod


class _FakeProc:
    def __init__(self, returncode: int = 0) -> None:
        self.returncode = returncode


def test_run_ingest_uses_repo_root_for_cwd_and_script_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mod = _load_module()
    repo_root = tmp_path / "main-repo"
    repo_root.mkdir()

    captured: dict[str, Any] = {}

    def fake_run(cmd: list[str], **kwargs: Any) -> _FakeProc:
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return _FakeProc(returncode=0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(mod.subprocess, "run", fake_run)

    rc = mod._run_ingest(repo_root, dry_run=False)
    assert rc == 0
    assert captured["kwargs"]["cwd"] == str(repo_root)
    assert captured["cmd"][1] == str(PROJECT_ROOT / "execution" / "sqlite_bootstrap.py")
    assert captured["cmd"][2] == str(repo_root / "execution" / "ingest_transcripts.py")
    assert captured["cmd"][-1] == "--no-promote"


def test_run_extract_uses_repo_root_for_cwd_and_script_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mod = _load_module()
    repo_root = tmp_path / "main-repo"
    repo_root.mkdir()

    captured: dict[str, Any] = {}

    def fake_run(cmd: list[str], **kwargs: Any) -> _FakeProc:
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return _FakeProc(returncode=0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(mod.subprocess, "run", fake_run)

    rc = mod._run_extract(repo_root, "AAPL", dry_run=False)
    assert rc == 0
    assert captured["kwargs"]["cwd"] == str(repo_root)
    assert captured["cmd"][1] == str(PROJECT_ROOT / "execution" / "sqlite_bootstrap.py")
    assert captured["cmd"][2] == str(
        repo_root / "execution" / "extract_commitments_from_transcript.py"
    )
    assert "--auto" in captured["cmd"]
    assert "AAPL" in captured["cmd"]


def test_run_ingest_dry_run_skips_subprocess(monkeypatch: pytest.MonkeyPatch) -> None:
    mod = _load_module()

    def boom(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("subprocess.run must not be called in dry-run")

    monkeypatch.setattr(mod.subprocess, "run", boom)
    assert mod._run_ingest(Path("/nonexistent"), dry_run=True) == 0


def test_run_extract_dry_run_skips_subprocess(monkeypatch: pytest.MonkeyPatch) -> None:
    mod = _load_module()

    def boom(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("subprocess.run must not be called in dry-run")

    monkeypatch.setattr(mod.subprocess, "run", boom)
    assert mod._run_extract(Path("/nonexistent"), "AAPL", dry_run=True) == 0


def test_commitment_extraction_is_limited_to_newly_ingested_tickers() -> None:
    mod = _load_module()
    results = [
        mod.TickerBackfillResult("AAPL", 9, fetched=["Q2_2026"]),
        mod.TickerBackfillResult("MSFT", 6, skipped_existing=["Q2_2026"]),
        mod.TickerBackfillResult("NVDA", 1, aggregator_misses=["Q2_2026"]),
    ]

    assert mod._newly_ingested_tickers(results, ingest_rc=0) == ["AAPL"]
    assert mod._newly_ingested_tickers(results, ingest_rc=None) == []
    assert mod._newly_ingested_tickers(results, ingest_rc=1) == []


def test_no_new_fetches_produce_no_commitment_extraction_scope() -> None:
    mod = _load_module()
    results = [
        mod.TickerBackfillResult("AAPL", 9, skipped_existing=["Q2_2026"]),
        mod.TickerBackfillResult("MSFT", 6, aggregator_misses=["Q2_2026"]),
    ]

    assert mod._newly_ingested_tickers(results, ingest_rc=None) == []


def test_ingest_child_failure_is_terminal() -> None:
    mod = _load_module()

    assert mod._terminal_exit_code(None, []) == 0
    assert mod._terminal_exit_code(0, []) == 0
    assert mod._terminal_exit_code(7, []) == 7
    assert mod._terminal_exit_code(0, [{"ticker": "NU", "rc": 9}]) == 1
    assert mod._terminal_exit_code(None, [], acquisition_errors=2) == 1
    assert mod._terminal_exit_code(8, [], acquisition_errors=2) == 8


@pytest.mark.parametrize(
    "recorded",
    ["../outside.txt", "transcripts/raw/../outside.txt", "C:/outside.txt"],
)
def test_backfill_rejects_non_relative_recorded_paths(
    recorded: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mod = _load_module()
    outside = tmp_path / "outside.txt"
    outside.write_text("bound transcript", encoding="utf-8")
    digest = hashlib.sha256(outside.read_bytes()).hexdigest()
    db_path = tmp_path / "portfolio.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE documents (id INTEGER PRIMARY KEY, ticker TEXT, file_path TEXT, sha256 TEXT);
        CREATE TABLE transcripts (
            id INTEGER PRIMARY KEY, document_id INTEGER, ticker TEXT,
            fiscal_period_type TEXT, period_end TEXT
        );
        """
    )
    conn.execute("INSERT INTO documents VALUES (1, 'NU', ?, ?)", (recorded, digest))
    conn.execute("INSERT INTO transcripts VALUES (1, 1, 'NU', 'Q1', '2026-03-31')")
    conn.commit()
    conn.close()

    def connect() -> sqlite3.Connection:
        connection = sqlite3.connect(db_path)
        connection.row_factory = sqlite3.Row
        return connection

    monkeypatch.setattr(mod.db, "PROJECT_ROOT", str(tmp_path))
    monkeypatch.setattr(mod.db, "get_connection", connect)
    assert mod._has_ingested_evidence("NU", 2026, 1, 12) is False


def test_backfill_stop_requires_exact_db_path_and_sha(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mod = _load_module()
    raw = tmp_path / "transcripts" / "raw" / "NU_Q1_2026.txt"
    raw.parent.mkdir(parents=True)
    raw.write_text("bound transcript", encoding="utf-8")
    digest = hashlib.sha256(raw.read_bytes()).hexdigest()
    db_path = tmp_path / "portfolio.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE documents (
            id INTEGER PRIMARY KEY, ticker TEXT, file_path TEXT, sha256 TEXT
        );
        CREATE TABLE transcripts (
            id INTEGER PRIMARY KEY, document_id INTEGER, ticker TEXT,
            fiscal_period_type TEXT, period_end TEXT
        );
        """
    )
    conn.execute(
        "INSERT INTO documents VALUES (1, 'NU', 'transcripts/raw/NU_Q1_2026.txt', ?)",
        (digest,),
    )
    conn.execute("INSERT INTO transcripts VALUES (1, 1, 'NU', 'Q1', '2026-03-31')")
    conn.commit()
    conn.close()

    def connect() -> sqlite3.Connection:
        connection = sqlite3.connect(db_path)
        connection.row_factory = sqlite3.Row
        return connection

    monkeypatch.setattr(mod.db, "PROJECT_ROOT", str(tmp_path))
    monkeypatch.setattr(mod.db, "get_connection", connect)
    assert mod._has_ingested_evidence("NU", 2026, 1, 12) is True
    raw.write_text("mutated", encoding="utf-8")
    assert mod._has_ingested_evidence("NU", 2026, 1, 12) is False
