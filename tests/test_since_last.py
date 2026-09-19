"""The "since you last looked" headline band (navigation_ia §4 PR3): the
aggregation builder over signals/decisions/documents/expected_earnings, its
doorway rendering, the quiet-window fallback, and the ``GET
/api/panel/since_last`` route (400 on a bad ``since``, 200 fragment on a
good one)."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

import comments_server
import pytest

from pipeline.since_last import build_since_last, render_since_last_band

if TYPE_CHECKING:
    from flask.testing import FlaskClient

PROJECT_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def db_path(tmp_path: Path, migrated_db: Callable[..., Path]) -> Path:
    db = tmp_path / "since_last.db"
    migrated_db(db)
    return db


def _insert_signal(
    db_path: Path, ticker: str, created_at: str, *, source_feed: str = "edgar_8k"
) -> None:
    # EDGAR-fed by default: since 2026-07-30 headline news (non-EDGAR
    # general_news) is excluded from the diet page AND this band's count.
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "INSERT INTO signals (ticker, signal_type, title, published_at, created_at, "
            "source_feed) VALUES (?, 'general_news', 'headline', ?, ?, ?)",
            (ticker, created_at, created_at, source_feed),
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


def test_headline_news_signals_do_not_count(db_path: Path) -> None:
    """The band's Signals doorway opens the diet page, which no longer shows
    headline news (2026-07-30) — so a non-EDGAR general_news row must not
    inflate the count either."""
    _insert_signal(db_path, "NU", IN_WINDOW, source_feed="fmp_stock_news")
    story = build_since_last(db_path, since=SINCE, now=NOW)
    assert story.is_quiet


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
