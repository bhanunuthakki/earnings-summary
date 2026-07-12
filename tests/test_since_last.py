"""The "since you last looked" headline band (navigation_ia §4 PR3): the
aggregation builder over signals/decisions/documents/expected_earnings, its
doorway rendering, the quiet-window fallback, and the ``GET
/api/panel/since_last`` route (400 on a bad ``since``, 200 fragment on a
good one)."""

from __future__ import annotations

import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from alembic.config import Config

from alembic import command

if TYPE_CHECKING:
    from flask.testing import FlaskClient

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from pipeline.since_last import build_since_last, render_since_last_band  # noqa: E402

PRIOR_HEAD = "0059_kpi_facts_restatement"

# ``decisions`` predates the 0059 stamp (db.init_db() territory) — same gap
# test_open_loops.py documents — so a stamp+upgrade fixture never creates it;
# hand-build the modern (0130-extended) shape, PLUS ``outcome_at`` (which that
# fixture's DDL omits — it never needed grading) since this module reads it.
_DECISIONS_DDL = """
CREATE TABLE decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker VARCHAR(16),
    recommendation_kind VARCHAR(32) NOT NULL,
    conviction VARCHAR(16),
    outcome_label VARCHAR(16) NOT NULL DEFAULT 'pending',
    outcome_at DATETIME,
    decided_by VARCHAR(16) NOT NULL DEFAULT 'advisor',
    scope VARCHAR(16) NOT NULL DEFAULT 'ticker',
    falsifier TEXT,
    size_usd FLOAT,
    user_notes TEXT,
    made_at DATETIME NOT NULL,
    created_at DATETIME NOT NULL
);
"""

# ``documents`` (0002) predates the 0059 stamp too — same gap, hand-built
# verbatim from its migration. ``expected_earnings`` does NOT need the same
# treatment: 0031 drops it and 0082 (after the 0059 stamp) unconditionally
# recreates it, so the stamp+upgrade fixture already has the real table.
_DOCUMENTS_DDL = """
CREATE TABLE documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker VARCHAR NOT NULL,
    source_type VARCHAR NOT NULL,
    doc_type VARCHAR NOT NULL,
    period_start DATETIME,
    period_end DATETIME,
    file_path VARCHAR NOT NULL,
    sha256 VARCHAR(64) NOT NULL UNIQUE,
    fetched_at DATETIME NOT NULL,
    fetch_status VARCHAR NOT NULL,
    http_code INTEGER,
    raw_bytes_size INTEGER NOT NULL,
    source_url VARCHAR,
    parent_document_id INTEGER
);
"""


def _cfg(db_path: Path) -> Config:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    db = tmp_path / "since_last.db"
    cfg = _cfg(db)
    command.stamp(cfg, PRIOR_HEAD)
    command.upgrade(cfg, "head")
    conn = sqlite3.connect(str(db))
    try:
        conn.executescript(_DECISIONS_DDL + _DOCUMENTS_DDL)
        conn.commit()
    finally:
        conn.close()
    return db


def _insert_signal(db_path: Path, ticker: str, created_at: str) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "INSERT INTO signals (ticker, signal_type, title, published_at, created_at) "
            "VALUES (?, 'general_news', 'headline', ?, ?)",
            (ticker, created_at, created_at),
        )
        conn.commit()
    finally:
        conn.close()


def _insert_decision(
    db_path: Path,
    *,
    ticker: str = "NU",
    created_at: str,
    falsifier: str | None = None,
    outcome_at: str | None = None,
    outcome_label: str = "pending",
) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "INSERT INTO decisions (ticker, recommendation_kind, decided_by, falsifier, "
            "made_at, created_at, outcome_at, outcome_label) "
            "VALUES (?, 'add', 'owner', ?, ?, ?, ?, ?)",
            (ticker, falsifier, created_at, created_at, outcome_at, outcome_label),
        )
        conn.commit()
    finally:
        conn.close()


def _insert_document(db_path: Path, ticker: str, fetched_at: str) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "INSERT INTO documents (ticker, source_type, doc_type, file_path, sha256, "
            "fetched_at, fetch_status, raw_bytes_size) "
            "VALUES (?, 'sec', '10-Q', 'x/y.pdf', ?, ?, 'ok', 10)",
            (ticker, f"sha-{ticker}-{fetched_at}", fetched_at),
        )
        conn.commit()
    finally:
        conn.close()


def _insert_expected_earnings(db_path: Path, ticker: str, first_seen_at: str) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "INSERT INTO expected_earnings (ticker, expected_date, detected_source, "
            "first_seen_at, last_seen_at) VALUES (?, '2026-08-01', 'fmp', ?, ?)",
            (ticker, first_seen_at, first_seen_at),
        )
        conn.commit()
    finally:
        conn.close()


SINCE = datetime(2026, 7, 10, 0, 0, 0)
NOW = datetime(2026, 7, 11, 0, 0, 0)
IN_WINDOW = "2026-07-10 12:00:00"
IN_WINDOW_T = "2026-07-10T12:00:00.500000"  # decisions' now_iso() shape
BEFORE_WINDOW = "2026-07-09 12:00:00"
AFTER_WINDOW = "2026-07-11 12:00:00"


# ---------------------------------------------------------------------------
# Builder — quiet fallback + missing-table guard
# ---------------------------------------------------------------------------


def test_quiet_line_when_nothing_happened(db_path: Path) -> None:
    story = build_since_last(db_path, since=SINCE, now=NOW)
    assert story.is_quiet
    html = render_since_last_band(story)
    assert "Quiet since your last look" in html
    assert "1d ago" in html
    assert "cc-open-loops" in html


def test_never_raises_without_schema(tmp_path: Path) -> None:
    bare = tmp_path / "bare.db"
    sqlite3.connect(str(bare)).close()  # a DB with no tables at all
    story = build_since_last(bare, since=SINCE, now=NOW)
    assert story.is_quiet
    assert "Quiet since your last look" in render_since_last_band(story)


# ---------------------------------------------------------------------------
# Each substrate contributes when present, and only in-window
# ---------------------------------------------------------------------------


def test_signals_contribute_count_and_tickers(db_path: Path) -> None:
    _insert_signal(db_path, "NU", IN_WINDOW)
    _insert_signal(db_path, "MELI", IN_WINDOW)
    _insert_signal(db_path, "OUT", BEFORE_WINDOW)
    _insert_signal(db_path, "OUT2", AFTER_WINDOW)
    html = render_since_last_band(build_since_last(db_path, since=SINCE, now=NOW))
    assert "Signals" in html
    assert ">2<" in html
    assert "MELI" in html and "NU" in html
    assert "OUT" not in html
    assert 'href="/#diet"' in html


def test_falsifiers_armed_counts_owner_decisions_with_falsifier(db_path: Path) -> None:
    _insert_decision(db_path, ticker="NU", created_at=IN_WINDOW_T, falsifier="thesis breaks if X")
    _insert_decision(db_path, ticker="MELI", created_at=IN_WINDOW_T, falsifier=None)
    html = render_since_last_band(build_since_last(db_path, since=SINCE, now=NOW))
    assert "Falsifiers armed" in html
    assert ">1<" in html
    assert "Decisions logged" in html
    assert ">2<" in html
    assert 'href="/#decisions_record"' in html


def test_decisions_graded_counts_outcome_in_window(db_path: Path) -> None:
    _insert_decision(
        db_path,
        created_at=BEFORE_WINDOW,
        outcome_at=IN_WINDOW,
        outcome_label="correct",
    )
    # A pending grade in-window must NOT count as graded.
    _insert_decision(db_path, created_at=IN_WINDOW_T, outcome_at=IN_WINDOW, outcome_label="pending")
    html = render_since_last_band(build_since_last(db_path, since=SINCE, now=NOW))
    assert "Decisions graded" in html
    assert ">1<" in html


def test_documents_contribute_and_doorway_targets_ticker(db_path: Path) -> None:
    _insert_document(db_path, "NU", IN_WINDOW)
    _insert_document(db_path, "OUT", BEFORE_WINDOW)
    html = render_since_last_band(build_since_last(db_path, since=SINCE, now=NOW))
    assert "New documents" in html
    assert ">1<" in html
    assert 'href="/#holding=NU"' in html


def test_earnings_scheduled_contributes(db_path: Path) -> None:
    _insert_expected_earnings(db_path, "RBRK", IN_WINDOW)
    html = render_since_last_band(build_since_last(db_path, since=SINCE, now=NOW))
    assert "Earnings scheduled" in html
    assert ">1<" in html
    assert 'href="/#holding=RBRK"' in html


def test_every_nonzero_item_is_a_doorway(db_path: Path) -> None:
    _insert_signal(db_path, "NU", IN_WINDOW)
    _insert_decision(db_path, created_at=IN_WINDOW_T, falsifier="x")
    _insert_document(db_path, "NU", IN_WINDOW)
    _insert_expected_earnings(db_path, "NU", IN_WINDOW)
    story = build_since_last(db_path, since=SINCE, now=NOW)
    assert len(story.items) >= 4
    for item in story.items:
        assert item.href.startswith("/#")


# ---------------------------------------------------------------------------
# Route: GET /api/panel/since_last
# ---------------------------------------------------------------------------

sys.path.insert(0, str(PROJECT_ROOT / "execution"))
import comments_server  # noqa: E402


@pytest.fixture
def client(tmp_path: Path) -> FlaskClient:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    sqlite3.connect(str(data_dir / "portfolio.db")).close()
    app = comments_server.create_app(tmp_path)
    return app.test_client()


def test_route_rejects_bad_since(client: FlaskClient) -> None:
    resp = client.get("/api/panel/since_last?since=not-a-date")
    assert resp.status_code == 400
    resp_missing = client.get("/api/panel/since_last")
    assert resp_missing.status_code == 400


def test_route_200_fragment_on_good_since(client: FlaskClient) -> None:
    resp = client.get("/api/panel/since_last?since=2026-07-10T00:00:00")
    assert resp.status_code == 200
    assert resp.mimetype == "text/html"
    # A schema-less fixture DB degrades to the quiet line — never a 500.
    assert b"Quiet since your last look" in resp.data
