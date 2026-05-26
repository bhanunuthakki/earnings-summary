"""Tests for the brief_provenance_log writer in execution/build_artifacts.py.

The logger appends one row per render with (ticker, generation_date,
sections_status JSON, trigger, artifact_path). Silently no-ops when the
table is missing so synthetic test environments aren't forced to apply
migration 0023 just to instantiate the renderer.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from execution.build_artifacts import _log_brief_provenance  # noqa: E402


def _make_db(db_path: Path, *, with_table: bool) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        if with_table:
            conn.execute(
                """
                CREATE TABLE brief_provenance_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ticker TEXT NOT NULL,
                    generation_date TEXT NOT NULL,
                    generated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    sources_used TEXT NOT NULL,
                    sections_status TEXT NOT NULL,
                    trigger TEXT NOT NULL,
                    artifact_path TEXT NOT NULL
                )
                """
            )
        conn.commit()
    finally:
        conn.close()


def test_writer_inserts_one_row_per_call(tmp_path: Path) -> None:
    repo_root = tmp_path
    (repo_root / "data").mkdir()
    db_path = repo_root / "data" / "portfolio.db"
    _make_db(db_path, with_table=True)

    artifact = repo_root / "output" / "research" / "GOOG" / "2026-05-26_workspace.html"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("<html/>", encoding="utf-8")

    _log_brief_provenance(
        repo_root=repo_root,
        ticker="GOOG",
        generation_date="2026-05-26",
        sections_status={"snapshot": "LIVE", "thesis": "LIVE", "bear_case": "STUB"},
        trigger="manual",
        artifact_path=artifact,
    )

    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(
            "SELECT ticker, generation_date, sections_status, trigger, artifact_path "
            "FROM brief_provenance_log"
        ).fetchall()
    finally:
        conn.close()
    assert len(rows) == 1
    ticker, gen_date, sections_status_json, trigger, artifact_path = rows[0]
    assert ticker == "GOOG"
    assert gen_date == "2026-05-26"
    assert trigger == "manual"
    parsed_status = json.loads(sections_status_json)
    assert parsed_status["snapshot"] == "LIVE"
    assert parsed_status["bear_case"] == "STUB"
    # Path is relative to repo_root for portability.
    assert artifact_path == "output/research/GOOG/2026-05-26_workspace.html" or (
        artifact_path.endswith("2026-05-26_workspace.html")
    )


def test_writer_silently_skips_when_table_missing(tmp_path: Path) -> None:
    """A repo without the 0023 migration applied still builds reports — the
    logger must not crash the render."""
    repo_root = tmp_path
    (repo_root / "data").mkdir()
    db_path = repo_root / "data" / "portfolio.db"
    _make_db(db_path, with_table=False)

    artifact = repo_root / "out.html"
    artifact.write_text("<html/>", encoding="utf-8")

    # Should not raise.
    _log_brief_provenance(
        repo_root=repo_root,
        ticker="META",
        generation_date="2026-05-26",
        sections_status={"snapshot": "LIVE"},
        trigger="daily_worker",
        artifact_path=artifact,
    )


def test_writer_skips_when_db_missing(tmp_path: Path) -> None:
    """No DB file at all (cold-start) — silently no-op."""
    repo_root = tmp_path
    (repo_root / "data").mkdir()
    artifact = repo_root / "out.html"
    artifact.write_text("<html/>", encoding="utf-8")

    _log_brief_provenance(
        repo_root=repo_root,
        ticker="NU",
        generation_date="2026-05-26",
        sections_status={},
        trigger="manual",
        artifact_path=artifact,
    )


def test_multiple_renders_append_separate_rows(tmp_path: Path) -> None:
    """Re-rendering the same ticker on the same day still appends — the
    table is an immutable audit log, not a (ticker, date) upsert."""
    repo_root = tmp_path
    (repo_root / "data").mkdir()
    db_path = repo_root / "data" / "portfolio.db"
    _make_db(db_path, with_table=True)
    artifact = repo_root / "out.html"
    artifact.write_text("<html/>", encoding="utf-8")

    for trig in ("manual", "earnings", "news_refresh"):
        _log_brief_provenance(
            repo_root=repo_root,
            ticker="AMZN",
            generation_date="2026-05-26",
            sections_status={"snapshot": "LIVE"},
            trigger=trig,
            artifact_path=artifact,
        )

    conn = sqlite3.connect(str(db_path))
    try:
        triggers = [
            r[0]
            for r in conn.execute(
                "SELECT trigger FROM brief_provenance_log "
                "WHERE ticker = 'AMZN' ORDER BY id"
            ).fetchall()
        ]
    finally:
        conn.close()
    assert triggers == ["manual", "earnings", "news_refresh"]
