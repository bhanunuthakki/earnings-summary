"""Tests for src/dashboard/upcoming.py — the Home rail's upcoming-earnings
strip (the surviving piece of the retired /digest page).

The calendar-vs-estimate selection logic ports unchanged from the digest's
"Upcoming this week" section, so this file carries that coverage forward:
calendar rows win, the +91d estimate is fallback-only, watchlist names stay
excluded. Each test gets a fresh tmp_path SQLite DB stamped at the prior
alembic head and upgraded, mirroring tests/test_dashboard_feed.py.
"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from pathlib import Path

import pytest
from alembic.config import Config

from alembic import command
from dashboard.upcoming import render_upcoming_strip, upcoming_earnings

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PRIOR_HEAD = "0059_kpi_facts_restatement"
TODAY = date(2026, 5, 27)


def _build_config(db_path: Path) -> Config:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    db = tmp_path / "dashboard_upcoming.db"
    cfg = _build_config(db)
    command.stamp(cfg, PRIOR_HEAD)
    command.upgrade(cfg, "head")
    return db


def _seed_calendar(db_path: Path) -> None:
    """Create + seed tracked_companies + earnings_surprises (init_db tables not
    in the stamp-at-0059 fixture) for the upcoming-earnings estimate."""
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(
            "CREATE TABLE IF NOT EXISTS tracked_companies (ticker TEXT, name TEXT, "
            "list_type TEXT, archived_at TEXT, fiscal_year_end TEXT);"
            "CREATE TABLE IF NOT EXISTS earnings_surprises (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "ticker TEXT, release_date TEXT, eps_estimate REAL, eps_actual REAL);"
        )
        conn.executemany(
            "INSERT INTO tracked_companies (ticker, name, list_type, archived_at) VALUES (?,?,?,?)",
            [
                ("NU", "Nu Holdings", "portfolio", None),
                ("ORCL", "Oracle", "evaluation", None),
                ("ZZ", "Watch Co", "watchlist", None),  # excluded by list_type
            ],
        )
        # NU: last release 80d before TODAY -> est +91 = TODAY+11 (within 14d) -> shown.
        # ORCL: last release 5d before TODAY -> est +91 ~ TODAY+86 (beyond horizon) -> hidden.
        # ZZ: within horizon but a watchlist name -> hidden.
        conn.executemany(
            "INSERT INTO earnings_surprises (ticker, release_date) VALUES (?,?)",
            [
                ("NU", (TODAY - timedelta(days=80)).isoformat()),
                ("ORCL", (TODAY - timedelta(days=5)).isoformat()),
                ("ZZ", (TODAY - timedelta(days=80)).isoformat()),
            ],
        )
        conn.commit()
    finally:
        conn.close()


def _seed_expected_earnings(db_path: Path, rows: list[tuple[str, date]]) -> None:
    """Insert real calendar rows (the 0082 table exists via the alembic fixture)."""
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executemany(
            "INSERT INTO expected_earnings (ticker, expected_date, detected_source) "
            "VALUES (?, ?, 'fmp')",
            [(t, d.isoformat()) for t, d in rows],
        )
        conn.commit()
    finally:
        conn.close()


# ----------------------------------------------------------------------------
# upcoming_earnings — calendar-first selection (ported from the digest tests)
# ----------------------------------------------------------------------------


def test_estimates_tracked_next_earnings(db_path: Path) -> None:
    """Tracked names whose estimated next earnings (latest release + ~1
    quarter) land within the horizon surface; far-out estimates and
    non-portfolio/evaluation names stay out. (No expected_earnings rows
    seeded: every name exercises the est. fallback path.)"""
    est = TODAY - timedelta(days=80) + timedelta(days=91)
    assert upcoming_earnings(db_path, TODAY) == []  # nothing seeded yet
    _seed_calendar(db_path)
    assert upcoming_earnings(db_path, TODAY) == [("NU", est, True)]


def test_prefers_real_calendar_dates(db_path: Path) -> None:
    """A name with an expected_earnings row shows the REAL date — not the
    estimate — and is never re-estimated."""
    _seed_calendar(db_path)  # NU's est would be TODAY+11
    real = TODAY + timedelta(days=4)
    _seed_expected_earnings(db_path, [("NU", real)])
    assert upcoming_earnings(db_path, TODAY) == [("NU", real, False)]


def test_calendar_beyond_horizon_suppresses_estimate(db_path: Path) -> None:
    """When the calendar KNOWS the next date is beyond the horizon, the name
    is hidden — not re-estimated into the window by the +91d fallback."""
    _seed_calendar(db_path)  # NU's est (TODAY+11) would land in the horizon
    _seed_expected_earnings(db_path, [("NU", TODAY + timedelta(days=40))])
    assert upcoming_earnings(db_path, TODAY) == []


def test_mixes_calendar_dates_and_estimate_fallback(db_path: Path) -> None:
    """Calendar-owned names render real dates; names without a calendar row
    keep the labelled estimate; watchlist names stay excluded even with a
    row. Date-ascending order."""
    _seed_calendar(db_path)  # NU: surprises only -> est TODAY+11
    _seed_expected_earnings(
        db_path,
        [("ORCL", TODAY + timedelta(days=2)), ("ZZ", TODAY + timedelta(days=3))],
    )
    est = TODAY - timedelta(days=80) + timedelta(days=91)
    assert upcoming_earnings(db_path, TODAY) == [
        ("ORCL", TODAY + timedelta(days=2), False),
        ("NU", est, True),
    ]


# ----------------------------------------------------------------------------
# render_upcoming_strip — the compact Home-rail markup
# ----------------------------------------------------------------------------


def test_strip_renders_nothing_when_quiet(db_path: Path) -> None:
    """No upcoming names → no strip at all (the rail stays clean)."""
    assert render_upcoming_strip(db_path, TODAY) == ""
    assert render_upcoming_strip(None, TODAY) == ""


def test_strip_renders_compact_rows(db_path: Path) -> None:
    _seed_calendar(db_path)
    real = TODAY + timedelta(days=2)
    _seed_expected_earnings(db_path, [("ORCL", real)])
    est = TODAY - timedelta(days=80) + timedelta(days=91)

    html = render_upcoming_strip(db_path, TODAY)

    assert 'class="up-strip"' in html
    assert "Upcoming earnings" in html
    # Real calendar date: plain ISO date, no estimate chip on that row.
    assert f'<span class="up-date">{real.isoformat()}</span>' in html
    # Estimate fallback: ~-prefixed date + the est. chip.
    assert f'<span class="up-date">~{est.isoformat()}</span>' in html
    assert '<span class="up-est">est.</span>' in html
    # Tickers carry the shell hover mini-card hook (inert off the shell).
    assert 'data-peek-ticker="ORCL"' in html
    assert 'data-peek-ticker="NU"' in html
    # Date order: ORCL (real, sooner) before NU (estimate, later).
    assert html.index('data-peek-ticker="ORCL"') < html.index('data-peek-ticker="NU"')
    # The watchlist name never appears.
    assert "ZZ" not in html


def test_strip_rows_carry_prep_notes_in_the_tooltip(db_path: Path) -> None:
    """P4.4 preserved compactly: each row's title leads with what the date is,
    then the owner's open watch items / questions for that name."""
    from user_state.notes import create_note

    _seed_calendar(db_path)
    create_note(
        ticker="NU",
        kind="watch",
        body="Ask about deposit franchise costs on the call.",
        db_path=db_path,
    )
    html = render_upcoming_strip(db_path, TODAY)
    assert "est. next earnings — watch: Ask about deposit franchise costs" in html
