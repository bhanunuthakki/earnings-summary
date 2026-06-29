"""insight_notes migration (0118) + the synthesis store + FTS/LIKE search."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic.config import Config

from alembic import command
from capture import ingest
from capture.matcher import build_roster_index
from synthesis import insights

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PRIOR_HEAD = "0059_kpi_facts_restatement"
ROSTER = build_roster_index(symbols=["NU"], phrases={"nubank": "NU"})


def _cfg(db_path: Path) -> Config:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    db = tmp_path / "ledger.db"
    cfg = _cfg(db)
    command.stamp(cfg, PRIOR_HEAD)
    command.upgrade(cfg, "head")
    return db


def test_insight_table_exists(db_path: Path) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()
    assert "insight_notes" in names


def test_record_supersedes_prior_and_tracks_watermark(db_path: Path) -> None:
    id1 = insights.record_insight(
        scope_key="NU",
        kind="stance",
        body_md="constructive",
        source_note_ids=[1, 2],
        watermark_id=2,
        db_path=db_path,
    )
    id2 = insights.record_insight(
        scope_key="NU",
        kind="stance",
        body_md="more constructive",
        source_note_ids=[1, 2, 3],
        watermark_id=3,
        db_path=db_path,
    )
    current = insights.list_insights(kind="stance", scope_key="NU", db_path=db_path)
    assert len(current) == 1
    assert current[0].id == id2
    assert current[0].body_md == "more constructive"
    assert current[0].source_note_ids == [1, 2, 3]
    superseded = insights.list_insights(
        kind="stance", scope_key="NU", status="superseded", db_path=db_path
    )
    assert [r.id for r in superseded] == [id1]
    assert insights.current_watermark("NU", "stance", db_path=db_path) == 3


def test_search_musings_finds_by_content(db_path: Path) -> None:
    ingest.ingest_capture(
        channel="tray", text="Nubank NPL formation is the worry", roster=ROSTER, db_path=db_path
    )
    ingest.ingest_capture(
        channel="tray", text="the macro tape feels heavy", roster=ROSTER, db_path=db_path
    )
    hits = insights.search_musings("NPL", db_path=db_path)
    assert len(hits) == 1
    assert insights.search_musings("zzznotpresent", db_path=db_path) == []
    assert insights.search_musings("  ", db_path=db_path) == []


def test_record_rejects_unknown_kind(db_path: Path) -> None:
    with pytest.raises(ValueError, match="unknown insight kind"):
        insights.record_insight(
            scope_key="x",
            kind="bogus",
            body_md="b",
            source_note_ids=[],
            watermark_id=None,
            db_path=db_path,
        )
