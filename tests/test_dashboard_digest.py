"""Tests for src/dashboard/digest.py â€” the morning digest renderer.

Each test gets a fresh tmp_path SQLite DB stamped at the prior alembic
head and upgraded so the 5 Personal CIO tables exist exactly as
production creates them. Mirrors the pattern in
``test_alerts_store.py`` and ``test_user_state_crud.py``.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, date, datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path

import pytest
from alembic.config import Config

from alembic import command
from alerts import fire_alert, queue_action
from dashboard import render_morning_digest

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
    db = tmp_path / "dashboard_digest.db"
    cfg = _build_config(db)
    command.stamp(cfg, PRIOR_HEAD)
    command.upgrade(cfg, "head")
    return db


# ----------------------------------------------------------------------------
# Empty-state
# ----------------------------------------------------------------------------


def test_empty_db_renders_quiet_message(db_path: Path) -> None:
    html = render_morning_digest(TODAY, db_path=db_path)
    assert "Nothing changed in the last 24h" in html
    # Even with no activity, the digest is still a complete document
    assert "<!doctype html>" in html
    assert "</html>" in html
    # Upcoming renders unconditionally; the old standalone ledger/open-items/
    # outstanding sections folded into the unified stream (PR3) and are gone.
    assert "Upcoming this week" in html
    assert "Recent thesis changes" not in html
    assert "Open items" not in html
    assert "Outstanding queued actions" not in html


def _seed_calendar(db_path: Path) -> None:
    """Create + seed tracked_companies + earnings_surprises (init_db tables not in
    the stamp-at-0059 fixture) for the upcoming-earnings estimate."""
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


def test_upcoming_section_estimates_tracked_next_earnings(db_path: Path) -> None:
    """The 'Upcoming this week' section surfaces tracked names whose estimated next
    earnings (latest release + ~1 quarter) land within the horizon â€” and excludes
    far-out estimates and non-portfolio/evaluation names."""
    _seed_calendar(db_path)
    html = render_morning_digest(TODAY, db_path=db_path)
    assert "est. next earnings" in html
    assert "NU" in html
    assert (TODAY - timedelta(days=80) + timedelta(days=91)).isoformat() in html  # est date
    # ORCL is beyond the horizon; ZZ is a watchlist name â€” neither appears as upcoming.
    assert "est. next earnings</span></li>" in html
    # Exactly one upcoming item (NU).
    assert html.count("upcoming-item") == 1


# ----------------------------------------------------------------------------
# Open items (P4.4)
# ----------------------------------------------------------------------------


def test_journal_notes_fold_into_the_stream(db_path: Path) -> None:
    """PR3: open journal notes are STANDING items â€” they appear in the stream
    (kind chip + body) regardless of the 24h window, instead of a separate
    'Open items' section. Notes are stamped now() by the store, so render for
    today to keep them under the until-bound."""
    from datetime import UTC as _UTC
    from datetime import datetime as _datetime

    from user_state.notes import create_note

    create_note(
        ticker="NU",
        kind="watch",
        body="Watch risk-adjusted NIM trajectory back above 10%.",
        db_path=db_path,
    )
    create_note(
        ticker=None,
        kind="question",
        body="What is the policy-mix drift this quarter?",
        db_path=db_path,
    )
    html = render_morning_digest(_datetime.now(_UTC).date(), db_path=db_path)
    assert "Open items" not in html  # the old section is gone
    assert "Watch risk-adjusted NIM trajectory" in html
    assert "policy-mix drift" in html
    # The note kind renders as the item's chip.
    assert "watch" in html
    assert "question" in html


def test_upcoming_earnings_leads_with_prep_notes(db_path: Path) -> None:
    """Each upcoming-earnings row carries the owner's open watch items for
    that name (P4.4: earnings prep starts from the owner's own questions)."""
    from user_state.notes import create_note

    _seed_calendar(db_path)
    create_note(
        ticker="NU",
        kind="watch",
        body="Ask about deposit franchise costs on the call.",
        db_path=db_path,
    )
    html = render_morning_digest(TODAY, db_path=db_path)
    assert "prep-notes" in html
    assert "Ask about deposit franchise costs" in html


# ----------------------------------------------------------------------------
# Populated DB
# ----------------------------------------------------------------------------


def test_two_alerts_in_window_render_as_cards(db_path: Path) -> None:
    fired_at = datetime.combine(TODAY, datetime.min.time(), tzinfo=UTC).replace(hour=8)
    alert_a = fire_alert(
        ticker="GOOG",
        trigger_kind="kpi_inflection",
        fired_at=fired_at,
        evidence_json=json.dumps(
            {"summary": "Cloud margin breach", "memo": "Reset cloud-margin tier"}
        ),
        signature_sha="sig-a",
        db_path=db_path,
    )
    alert_b = fire_alert(
        ticker="META",
        trigger_kind="material_news",
        fired_at=fired_at,
        evidence_json=json.dumps({"summary": "Reality Labs guidance cut"}),
        signature_sha="sig-b",
        db_path=db_path,
    )
    queue_action(
        alert_id=alert_a.id,
        action_kind="thesis_update",
        payload={"ticker": "GOOG", "body": "Cloud margin watch-item activated"},
        db_path=db_path,
    )
    queue_action(
        alert_id=alert_b.id,
        action_kind="bear_append",
        payload={"ticker": "META", "body": "RL guidance hint"},
        db_path=db_path,
    )

    html = render_morning_digest(TODAY, db_path=db_path)

    # Both tickers appear as cards
    assert "GOOG" in html
    assert "META" in html
    # The trigger kinds appear
    assert "kpi_inflection" in html
    assert "material_news" in html
    # The card's at-a-glance line renders for both: alert_a from its explicit
    # ``memo``; alert_b falls back to its ``summary`` (the card reads the
    # per-trigger summary fields), so neither shows the "memo pending"
    # placeholder.
    assert "Reset cloud-margin tier" in html
    assert "Reality Labs guidance cut" in html
    assert "memo pending" not in html
    # Queued action bodies render
    assert "Cloud margin watch-item activated" in html
    assert "RL guidance hint" in html
    # And the action-kind labels
    assert "Thesis update" in html
    assert "Bear-case append" in html
    # Empty-state message does NOT appear in the populated case
    assert "Nothing fired in the last 24h" not in html


def test_stale_drafts_surface_once_without_duplicating_windowed_alerts(
    db_path: Path,
) -> None:
    """PR3: a queued action whose parent alert is in the window renders ONCE,
    nested in that alert's card; a pending draft on an OLDER alert surfaces as
    a standalone Draft stream item â€” drafts are standing, so the old one
    appears even though its alert fired outside the window (the separate
    'Outstanding' section is gone)."""
    from datetime import UTC as _UTC
    from datetime import datetime as _datetime

    render_day = _datetime.now(_UTC).date()
    fired_today = datetime.combine(render_day, datetime.min.time(), tzinfo=UTC).replace(hour=8)
    fired_old = datetime.combine(render_day - timedelta(days=10), datetime.min.time(), tzinfo=UTC)

    new_alert = fire_alert(
        ticker="GOOG",
        trigger_kind="kpi_inflection",
        fired_at=fired_today,
        evidence_json=json.dumps({"summary": "new"}),
        signature_sha="sig-new",
        db_path=db_path,
    )
    old_alert = fire_alert(
        ticker="NU",
        trigger_kind="saydo_due",
        fired_at=fired_old,
        evidence_json=json.dumps({"summary": "old"}),
        signature_sha="sig-old",
        db_path=db_path,
    )
    queue_action(
        alert_id=new_alert.id,
        action_kind="thesis_update",
        payload={"ticker": "GOOG", "body": "covered in section two"},
        db_path=db_path,
    )
    queue_action(
        alert_id=old_alert.id,
        action_kind="earnings_prep_append",
        payload={"ticker": "NU", "body": "old draft still outstanding"},
        db_path=db_path,
    )

    html = render_morning_digest(render_day, db_path=db_path)

    # The new alert's action appears exactly once (nested in its alert card).
    assert html.count("covered in section two") == 1
    # The old alert's pending draft surfaces as a standalone Draft item even
    # though its parent alert fired outside the window.
    assert "old draft still outstanding" in html
    assert "Outstanding queued actions" not in html


def test_naive_utc_fired_at_in_window_renders(db_path: Path) -> None:
    """Regression for the tz naive/aware crash.

    Production triggers persist ``fired_at`` as NAIVE-UTC
    (``datetime.now(UTC).replace(tzinfo=None)``) â€” unlike the aware fixtures
    the other tests here use. Before the fix, the renderer compared that naive
    value against an aware window bound and raised ``TypeError: can't compare
    offset-naive and offset-aware datetimes``, so the morning digest crashed the
    instant a real alert landed. This exercises the true production timestamp
    shape end-to-end.
    """
    fired_naive = datetime.combine(TODAY, datetime.min.time()).replace(hour=8)
    assert fired_naive.tzinfo is None  # guard: this is the production shape
    alert = fire_alert(
        ticker="NU",
        trigger_kind="earnings_tone",
        fired_at=fired_naive,
        evidence_json=json.dumps({"summary": "Risk-adjusted NIM down 100bps QoQ"}),
        signature_sha="sig-naive-utc",
        db_path=db_path,
    )
    queue_action(
        alert_id=alert.id,
        action_kind="thesis_update",
        payload={"ticker": "NU", "body": "Re-underwrite the NIM trajectory"},
        db_path=db_path,
    )

    html = render_morning_digest(TODAY, db_path=db_path)

    assert "NU" in html
    assert "earnings_tone" in html
    assert "Risk-adjusted NIM down 100bps QoQ" in html
    assert "Re-underwrite the NIM trajectory" in html
    assert "Nothing fired in the last 24h" not in html


# ----------------------------------------------------------------------------
# Document validity
# ----------------------------------------------------------------------------


class _StructureCheck(HTMLParser):
    """Stack-based tag-balance checker. HTML5 lets the parser accept
    unclosed void elements (meta/link/etc.) but every non-void open
    must have a matching close. We track that explicitly.
    """

    _VOID = frozenset(
        {
            "area",
            "base",
            "br",
            "col",
            "embed",
            "hr",
            "img",
            "input",
            "link",
            "meta",
            "source",
            "track",
            "wbr",
        }
    )

    def __init__(self) -> None:
        super().__init__()
        self.stack: list[str] = []
        self.seen_html = False
        self.seen_body = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "html":
            self.seen_html = True
        if tag == "body":
            self.seen_body = True
        if tag not in self._VOID:
            self.stack.append(tag)

    def handle_endtag(self, tag: str) -> None:
        # Pop until we find the matching tag (HTML5 allows implicit closes)
        while self.stack and self.stack[-1] != tag:
            self.stack.pop()
        if self.stack and self.stack[-1] == tag:
            self.stack.pop()


def test_html_is_a_complete_document(db_path: Path) -> None:
    """Structural sanity-parse the digest HTML.

    Uses the stdlib HTML5-tolerant parser (xml.etree rejects HTML5
    boolean attributes like ``crossorigin`` on <link>, which we emit
    legitimately). The check is: it parses, has an <html> + <body>,
    and the non-void tag stack closes back to empty.
    """
    html = render_morning_digest(TODAY, db_path=db_path)
    assert html.startswith("<!doctype html>")
    checker = _StructureCheck()
    checker.feed(html)
    assert checker.seen_html
    assert checker.seen_body
    assert checker.stack == [], f"unclosed tags remaining: {checker.stack}"


def test_alerts_outside_window_are_filtered_out(db_path: Path) -> None:
    """An alert fired well after the render date must not appear in the
    digest (catches the re-render-historical-date case)."""
    far_future = datetime.combine(TODAY + timedelta(days=10), datetime.min.time(), tzinfo=UTC)
    fire_alert(
        ticker="GOOG",
        trigger_kind="kpi_inflection",
        fired_at=far_future,
        evidence_json=json.dumps({"summary": "future"}),
        signature_sha="sig-future",
        db_path=db_path,
    )
    html = render_morning_digest(TODAY, db_path=db_path)
    # The future alert's signature shouldn't surface
    assert "future" not in html or "Nothing fired" in html


def test_alerts_older_than_window_are_filtered_out(db_path: Path) -> None:
    """An alert fired well before the window start should not appear."""
    old = datetime.combine(TODAY - timedelta(days=10), datetime.min.time(), tzinfo=UTC)
    fire_alert(
        ticker="GOOG",
        trigger_kind="kpi_inflection",
        fired_at=old,
        evidence_json=json.dumps({"summary": "very-old-alert-text"}),
        signature_sha="sig-old",
        db_path=db_path,
    )
    html = render_morning_digest(TODAY, db_path=db_path)
    assert "very-old-alert-text" not in html


def test_thesis_ledger_entries_fold_into_the_stream(db_path: Path) -> None:
    """PR3: ledger entries created in the window appear as stream items with
    their entry-kind labels â€” the standalone 'Recent thesis changes' section
    is gone."""
    from user_state.ledger import append_entry

    append_entry(
        ticker="NU",
        entry_kind="thesis_update",
        body="ROE inflected below the 18% floor",
        db_path=db_path,
    )
    append_entry(
        ticker="GOOG",
        entry_kind="bear_append",
        body="Cloud margin softening confirmed",
        db_path=db_path,
    )

    from datetime import UTC as _UTC
    from datetime import datetime as _datetime

    html = render_morning_digest(_datetime.now(_UTC).date(), db_path=db_path)

    assert "Recent thesis changes" not in html
    assert "ROE inflected below the 18% floor" in html
    assert "Cloud margin softening confirmed" in html
    # Cross-holding: both tickers and their entry-kind labels render.
    assert "Thesis update" in html
    assert "Bear-case append" in html


def test_digest_stream_carries_unread_tracking_markup(db_path: Path) -> None:
    """Inbox v2: the digest is one of the three unread-tracked surfaces — the
    stream tags itself, cards carry comparable timestamps, and the page embeds
    INBOX_JS (accents only here; the count badge is the Home rail's)."""
    from datetime import UTC as _UTC
    from datetime import datetime as _datetime

    from user_state.ledger import append_entry

    append_entry(
        ticker="NU",
        entry_kind="thesis_update",
        body="Unread-tracking probe entry.",
        db_path=db_path,
    )
    html = render_morning_digest(_datetime.now(_UTC).date(), db_path=db_path)
    assert 'data-ix-surface="digest"' in html
    assert 'data-when="' in html
    assert "ix-last-seen:" in html


def test_duplicate_ledger_bodies_dedupe_in_the_stream(db_path: Path) -> None:
    """PR3 regression: the old digest showed one NU thesis update three times
    because consecutive ledger rows carried the same narrative. Near-identical
    bodies collapse to the newest row."""
    from datetime import UTC as _UTC
    from datetime import datetime as _datetime

    from user_state.ledger import append_entry

    for _ in range(3):
        append_entry(
            ticker="NU",
            entry_kind="thesis_update",
            body="Risk-adjusted NIM contracted 100 bps QoQ to 9.5% - seasonal.",
            db_path=db_path,
        )

    html = render_morning_digest(_datetime.now(_UTC).date(), db_path=db_path)
    assert html.count("Risk-adjusted NIM contracted 100 bps QoQ") == 1
