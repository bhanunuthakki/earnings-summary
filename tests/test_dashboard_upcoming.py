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
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from dashboard.upcoming import UPCOMING_CSS, render_upcoming_strip, upcoming_earnings
from earnings_surprise_store import EarningsSurpriseRecordV1, append_observation

PRIOR_HEAD = "0059_kpi_facts_restatement"
TODAY = date(2026, 5, 27)


@pytest.fixture
def db_path(tmp_path: Path, migrated_db: Callable[..., Path]) -> Path:
    return migrated_db(tmp_path / "dashboard_upcoming.db", stamp=PRIOR_HEAD)


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
        releases = [
            ("NU", TODAY - timedelta(days=80)),
            ("ORCL", TODAY - timedelta(days=5)),
            ("ZZ", TODAY - timedelta(days=80)),
        ]
        for ordinal, (ticker, release_date) in enumerate(releases):
            record = EarningsSurpriseRecordV1(
                ticker=ticker,
                release_date=release_date,
                source_name="test",
                fetched_at=datetime(
                    TODAY.year, TODAY.month, TODAY.day, tzinfo=UTC
                ),
            )
            observation_id, _ = append_observation(
                conn,
                record=record,
                raw_payload=record.model_dump(mode="json"),
                cache_path="test/dashboard_upcoming",
                record_ordinal=ordinal,
            )
            conn.execute(
                "INSERT INTO earnings_surprises "
                "(ticker, release_date, source_name, fetched_at, source_observation_id) "
                "VALUES (?, ?, 'test', ?, ?)",
                (
                    ticker,
                    release_date.isoformat(),
                    record.fetched_at.isoformat(),
                    observation_id,
                ),
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

    assert 'class="up-strip k-card k-card-dense"' in html
    assert 'class="up-strip-head k-card-row-title"' in html
    assert 'class="up-strip-sub k-card-meta"' in html
    assert "Upcoming earnings" in html
    # Wave B (B2): the strip root carries the informative one-line summary the
    # shell hoists into its collapsed <details> header — count in horizon +
    # the next reporter (ORCL, the soonest row).
    next_md = real.strftime("%m-%d")
    assert f'data-up-summary="Upcoming earnings · 2 in 14d — next ORCL {next_md}"' in html
    # Real calendar date: plain ISO date, no estimate chip on that row.
    assert f'<span class="up-date">{real.isoformat()}</span>' in html
    # Estimate fallback: ~-prefixed date + the est. chip.
    assert f'<span class="up-date">~{est.isoformat()}</span>' in html
    assert '<span class="up-est">est.</span>' in html
    # Tickers carry the shell hover mini-card hook (inert off the shell).
    assert 'data-peek-ticker="ORCL"' in html
    assert 'data-peek-ticker="NU"' in html
    # Tier order beats date order (Wave 2): NU (portfolio) leads even though
    # ORCL (evaluation) reports sooner — the owner's book first. The summary
    # line still names the SOONEST reporter regardless of tier.
    assert html.index('data-peek-ticker="NU"') < html.index('data-peek-ticker="ORCL"')
    assert html.index("Portfolio") < html.index('data-peek-ticker="NU"')
    assert html.index('data-peek-ticker="NU"') < html.index("Evaluation")
    # The watchlist name never appears.
    assert "ZZ" not in html


def test_strip_card_css_only_adds_layout() -> None:
    rule = UPCOMING_CSS.split(".up-strip {", 1)[1].split("}", 1)[0]
    assert "margin-bottom: var(--sp-2)" in rule
    for property_name in ("background:", "border:", "border-radius:", "padding:", "box-shadow:"):
        assert property_name not in rule


def test_strip_renders_watch_items_inline_as_ask_doorways(db_path: Path) -> None:
    """P4.4 made actionable: each name's open watch items render INLINE beneath
    the row as ``data-ask-q`` doorways (the shell's ``goAsk`` opens Ask scoped to
    the name) — the link from the earnings lane to "things to watch out for" —
    instead of being buried in a hover tooltip."""
    from user_state.notes import create_note

    _seed_calendar(db_path)
    create_note(
        ticker="NU",
        kind="watch",
        body="Ask about deposit franchise costs on the call.",
        db_path=db_path,
    )
    html = render_upcoming_strip(db_path, TODAY)
    # Inline doorway: a button carrying the watch item as an Ask query scoped to
    # the ticker, with the body visible (no longer hidden in the row title).
    assert 'class="up-watch-item"' in html
    assert 'data-ask-q="Ask about deposit franchise costs on the call. (NU)"' in html
    assert "Ask about deposit franchise costs on the call." in html
    assert 'class="up-watch-kind k-chip">watch<' in html


def test_strip_surfaces_open_questions_too(db_path: Path) -> None:
    """Open questions (the other lead-kind) surface as doorways alongside watch
    items — they are equally a "thing to watch out for" heading into the call."""
    from user_state.notes import create_note

    _seed_calendar(db_path)
    create_note(
        ticker="NU",
        kind="question",
        body="Did NIM expansion stall this quarter?",
        db_path=db_path,
    )
    html = render_upcoming_strip(db_path, TODAY)
    assert 'data-ask-q="Did NIM expansion stall this quarter? (NU)"' in html
    assert 'class="up-watch-kind k-chip">question<' in html


def test_strip_caps_watch_items_with_overflow_hint(db_path: Path) -> None:
    """At most ``_PREP_NOTES_PER_TICKER`` doorways render; the rest collapse into
    a muted, non-interactive "+N more" hint."""
    from dashboard.upcoming import _PREP_NOTES_PER_TICKER
    from user_state.notes import create_note

    _seed_calendar(db_path)
    n = _PREP_NOTES_PER_TICKER + 2
    for i in range(n):
        create_note(ticker="NU", kind="watch", body=f"Watch item number {i}.", db_path=db_path)
    html = render_upcoming_strip(db_path, TODAY)
    assert html.count('class="up-watch-item"') == _PREP_NOTES_PER_TICKER
    # Wave 2: the hint is compact ("+N") — the chips share the row's
    # horizontal lane with the date now, not a block of their own.
    assert f'class="up-watch-more muted">+{n - _PREP_NOTES_PER_TICKER}</span>' in html


def test_strip_row_without_notes_has_no_watch_block(db_path: Path) -> None:
    """Hide-don't-stub: a name with no open notes still renders its ticker + date
    row, but no inline watch block."""
    _seed_calendar(db_path)  # NU surfaces on the estimate path; no notes seeded
    html = render_upcoming_strip(db_path, TODAY)
    assert 'data-peek-ticker="NU"' in html
    assert 'class="up-watch"' not in html


# --------------------------------------------------------------------------- #
# Wave 2 (surface_density_jit_redesign.md, walkthrough #2/#4): tier bands,
# the in-row chip lane, and the on-demand prep chip.
# --------------------------------------------------------------------------- #


def _seed_active_signal(db_path: Path, ticker: str) -> None:
    """Give ``ticker`` a derived active-valuation signal (an unexpired
    research hot-flag)."""
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS research_hot_flags "
            "(id INTEGER PRIMARY KEY AUTOINCREMENT, ticker TEXT, set_at TEXT, expires_at TEXT)"
        )
        conn.execute(
            "INSERT INTO research_hot_flags (ticker, set_at, expires_at) VALUES (?, ?, ?)",
            (ticker, TODAY.isoformat(), (TODAY + timedelta(days=30)).isoformat()),
        )
        conn.commit()
    finally:
        conn.close()


def test_strip_tiers_active_valuation_between_portfolio_and_evaluation(db_path: Path) -> None:
    """An evaluation name with an unexpired hot-flag ranks in the middle
    "Active valuation" band — derived, never a manual strip flag."""
    _seed_calendar(db_path)
    real = TODAY + timedelta(days=2)
    _seed_expected_earnings(db_path, [("ORCL", real)])
    _seed_active_signal(db_path, "ORCL")

    html = render_upcoming_strip(db_path, TODAY)

    assert "Active valuation" in html
    # NU (portfolio) first, then ORCL under the Active band, and no Evaluation
    # band (nothing left for it — an empty tier renders no header).
    assert html.index('data-peek-ticker="NU"') < html.index("Active valuation")
    assert html.index("Active valuation") < html.index('data-peek-ticker="ORCL"')
    assert "Evaluation" not in html


def test_every_row_carries_the_on_demand_prep_chip(db_path: Path) -> None:
    """Walkthrough #4: the earnings memo is a chip created on demand — every
    upcoming row carries the peek doorway; nothing is pre-generated."""
    _seed_calendar(db_path)
    html = render_upcoming_strip(db_path, TODAY)
    assert 'data-peek-url="/api/peek/earnings-prep?ticker=NU"' in html
    assert 'data-peek-title="Earnings prep — NU"' in html


def test_watch_chips_render_inside_the_row_lane(db_path: Path) -> None:
    """Walkthrough #2: the chips sit in the horizontal space right of the
    ticker (inside .up-chips, inside .up-row) — not stacked beneath the row."""
    from user_state.notes import create_note

    _seed_calendar(db_path)
    create_note(ticker="NU", kind="watch", body="Watch the NIM print.", db_path=db_path)
    html = render_upcoming_strip(db_path, TODAY)
    row_start = html.index('<div class="up-row"')
    row_end = html.index("</div>", html.index('class="up-date"', row_start))
    row = html[row_start:row_end]
    assert 'class="up-chips"' in row
    assert 'class="up-watch-item"' in row
    assert 'data-peek-url="/api/peek/earnings-prep?ticker=NU"' in row
    # The old below-the-row list is gone.
    assert '<ul class="up-watch">' not in html
