"""Tests for execution/fetch_ir_from_urls.py — the browser-assisted URL fallback.

Bridges explicitly-provided document URLs (sourced by a real browser for sites the
headless crawler can't crack) into the manifest -> download+register -> anchor
pipeline. The download/categorize step (process_ticker) is mocked here; this guards
the wiring around it (manifest write, brief_dirty, status, URL dedup).
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from execution import fetch_ir_from_urls as mod  # noqa: E402
from ir_pipeline.manifest import load_manifest  # noqa: E402


def _make_db(db: Path) -> None:
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE tracked_companies (ticker TEXT, fiscal_year_end TEXT, brief_dirty INTEGER DEFAULT 0)"
    )
    conn.execute("INSERT INTO tracked_companies (ticker, fiscal_year_end) VALUES ('NOW', '12-31')")
    conn.execute(
        "CREATE TABLE ir_fetch_status (ticker TEXT PRIMARY KEY, last_attempt_at TEXT, "
        "last_status TEXT, discovered INTEGER, downloaded INTEGER, reason TEXT, updated_at TEXT)"
    )
    conn.commit()
    conn.close()


def _noop(*_a: object, **_k: object) -> None:
    return None


def _install_fake_process(
    monkeypatch: pytest.MonkeyPatch, *, downloaded: int, captured: dict[str, object]
) -> None:
    def _fake_process(
        ticker: str, *, root: Path, db_path: Path, categorize: bool, calendar: str | None
    ) -> dict[str, object]:
        captured["calendar"] = calendar
        captured["categorize"] = categorize
        captured["root"] = root
        return {"ticker": ticker, "status": "done", "downloaded": downloaded, "failed": 0}

    monkeypatch.setattr("execution.fetch_ir_from_urls.process_ticker", _fake_process)
    monkeypatch.setattr("execution.fetch_ir_from_urls._run_narrative", _noop)


def test_register_urls_writes_manifest_registers_and_anchors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "data" / "portfolio.db"
    _make_db(db)
    captured: dict[str, object] = {}
    _install_fake_process(monkeypatch, downloaded=2, captured=captured)
    urls = [
        "https://s205.q4cdn.com/x/ER-Q1-FY26.pdf",
        "https://s205.q4cdn.com/x/ServiceNow-1Q26-Investor-Presentation.pdf",
    ]

    n = mod.register_urls("now", urls, repo_root=tmp_path, db_path=db, process=True)

    assert n == 2
    assert captured["categorize"] is True  # docs are content-classified, not trusted blindly
    # The provided URLs are persisted to the canonical manifest.
    assert {e.url for e in load_manifest(tmp_path, "NOW")} == set(urls)
    conn = sqlite3.connect(str(db))
    brief_dirty = conn.execute(
        "SELECT brief_dirty FROM tracked_companies WHERE ticker='NOW'"
    ).fetchone()[0]
    status = conn.execute(
        "SELECT last_status, discovered, downloaded FROM ir_fetch_status WHERE ticker='NOW'"
    ).fetchone()
    conn.close()
    assert brief_dirty == 1  # queued for the daily --enable-llm rebuild
    assert status == ("ok", 2, 2)  # recovery surfaced in the dashboard's coverage tab


def test_register_urls_no_downloads_marks_failed_and_skips_anchor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "data" / "portfolio.db"
    _make_db(db)
    captured: dict[str, object] = {}
    _install_fake_process(monkeypatch, downloaded=0, captured=captured)

    n = mod.register_urls("NOW", ["https://blocked.example/x.pdf"], repo_root=tmp_path, db_path=db)

    assert n == 0
    conn = sqlite3.connect(str(db))
    brief_dirty = conn.execute(
        "SELECT brief_dirty FROM tracked_companies WHERE ticker='NOW'"
    ).fetchone()[0]
    last_status = conn.execute(
        "SELECT last_status FROM ir_fetch_status WHERE ticker='NOW'"
    ).fetchone()[0]
    conn.close()
    assert brief_dirty == 0  # nothing landed → don't churn the brief
    assert last_status == "failed"


def test_collect_urls_dedups_args_and_file(tmp_path: Path) -> None:
    f = tmp_path / "urls.txt"
    f.write_text("# a comment\nhttps://x/b.pdf\nhttps://x/a.pdf\n\n", encoding="utf-8")
    urls = mod.collect_urls(["https://x/a.pdf"], f)
    assert urls == [
        "https://x/a.pdf",
        "https://x/b.pdf",
    ]  # arg first, file adds new, comment skipped
