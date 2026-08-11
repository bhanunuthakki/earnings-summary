"""Cross-surface calendar integrity regressions (BHA-8)."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, date, datetime
from pathlib import Path

from calendar_clock import calendar_today
from execution.build_earnings_calendar import (
    CalendarEvent,
    CalendarRow,
    read_calendar_events,
    render_calendar_row,
)
from pipeline.diet_panel import render_diet_panel
from signals.store import record_investor_day

from ._signals_fixtures import signals_only


def test_calendar_date_uses_pacific_business_day_at_utc_boundary() -> None:
    # 00:30 UTC is still the prior business date in Los Angeles. Both calendar
    # surfaces must include an event on Aug 10 instead of advancing to Aug 11.
    now = datetime(2026, 8, 11, 0, 30, tzinfo=UTC)
    assert calendar_today(now) == date(2026, 8, 10)


def test_earnings_cache_rejects_cross_company_rows(tmp_path: Path) -> None:
    calendar_dir = tmp_path / "data" / "historical" / "fmp"
    calendar_dir.mkdir(parents=True)
    (calendar_dir / "NU_earnings_calendar.json").write_text(
        json.dumps(
            [
                {"symbol": "NVO", "date": "2026-08-12", "time": "bmo"},
                {"symbol": "nu", "date": "2026-08-13", "time": "amc"},
            ]
        ),
        encoding="utf-8",
    )

    events = read_calendar_events(tmp_path, "NU")

    assert [(event.date, event.time) for event in events] == [(date(2026, 8, 13), "amc")]


def test_earnings_destination_is_ticker_scoped_and_missing_is_explicit() -> None:
    event = CalendarEvent(date=date(2026, 8, 12), time="bmo", eps_est=None, rev_est=None)
    linked = CalendarRow(
        "NU",
        "Nu Holdings",
        "portfolio",
        ("2026-08-10", "research/NU/2026-08-10_workspace.html"),
        event,
    )
    unavailable = CalendarRow("NVO", "Novo Nordisk", "portfolio", None, event)

    linked_html = render_calendar_row(linked, date(2026, 8, 10), kind="upcoming")
    unavailable_html = render_calendar_row(unavailable, date(2026, 8, 10), kind="upcoming")

    assert 'href="research/NU/2026-08-10_workspace.html"' in linked_html
    assert "No report available" in unavailable_html
    assert "<a " not in unavailable_html


def test_general_event_links_source_and_missing_source_is_explicit(tmp_path: Path) -> None:
    db_path = tmp_path / "events.db"
    signals_only(db_path)
    conn = sqlite3.connect(str(db_path))
    try:
        record_investor_day(
            conn,
            "NU",
            date(2026, 8, 11),
            "Nubank Investor Day",
            firm="Nu IR",
            url="https://ir.nu/investor-day",
        )
        record_investor_day(
            conn,
            "META",
            date(2026, 8, 12),
            "Meta Investor Day",
            firm="Meta IR",
        )
    finally:
        conn.close()

    html = render_diet_panel(db_path, today=date(2026, 8, 10))

    assert 'href="https://ir.nu/investor-day"' in html
    assert "Nubank Investor Day" in html
    assert '<span class="muted">Meta Investor Day · Source unavailable</span>' in html


def test_general_calendar_empty_state_is_semantic(tmp_path: Path) -> None:
    db_path = tmp_path / "empty.db"
    signals_only(db_path)
    html = render_diet_panel(db_path, today=date(2026, 8, 10))

    assert 'data-calendar-state="empty"' in html
    assert 'role="status"' in html
    assert "No investor days on the calendar" in html


def test_general_calendar_unavailable_is_not_reported_as_empty(tmp_path: Path) -> None:
    html = render_diet_panel(tmp_path / "missing.db", today=date(2026, 8, 10))

    assert 'data-calendar-state="unavailable"' in html
    assert 'role="alert"' in html
    assert "Calendar unavailable" in html
    assert 'data-calendar-state="empty"' not in html
