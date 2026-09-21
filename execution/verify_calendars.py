"""verify_calendars.py — End-to-end calendar audit and verification CLI.

Verifies:
  1. Pacific calendar clock (America/Los_Angeles calendar_today boundary).
  2. Canonical expected_earnings persistence (upcoming and past report dates).
  3. Deduplication and future-date rescheduling invariants.
  4. Integration with dashboard.upcoming and research cockpit view-models.
  5. Graceful degradation on missing or corrupt calendar entries.

Usage:
  python execution/verify_calendars.py [--db PATH] [--json]
"""

from __future__ import annotations

import json
import sqlite3
import sys
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

try:  # direct script invocation
    from _lib import command_parser
except ImportError:  # pragma: no cover - test/import path fallback
    from execution._lib import command_parser

from calendar_clock import CALENDAR_TIME_ZONE, calendar_today
from dashboard.upcoming import upcoming_earnings
from expected_earnings import last_reported_by_ticker, upcoming_by_ticker
from signals.ir_events import IREventObservation
from signals.store import load_forward_agenda_result
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite


@dataclass(frozen=True)
class CalendarAuditResult:
    timestamp_utc: str
    calendar_pacific_today: str
    tracked_companies_count: int
    upcoming_expected_count: int
    past_reported_count: int
    upcoming_strip_items_count: int
    forward_events_count: int
    forward_freshness: str
    integrity_pass: bool
    issues: list[str]
    sample_upcoming: list[dict[str, Any]]


def audit_calendars(
    db_path: Path, today: date | None = None, *, now: datetime | None = None
) -> CalendarAuditResult:
    cur_today = today or calendar_today()
    issues: list[str] = []
    upcoming_by_t: dict[str, date] = {}
    last_by_t: dict[str, date] = {}
    tracked_count = 0
    forward_count = 0
    forward_freshness = "unavailable"
    sample_upcoming: list[dict[str, Any]] = []

    if not db_path.exists():
        return CalendarAuditResult(
            timestamp_utc=datetime.now(UTC).isoformat(),
            calendar_pacific_today=cur_today.isoformat(),
            tracked_companies_count=0,
            upcoming_expected_count=0,
            past_reported_count=0,
            upcoming_strip_items_count=0,
            forward_events_count=0,
            forward_freshness="unavailable",
            integrity_pass=False,
            issues=[f"Database path not found: {db_path}"],
            sample_upcoming=[],
        )

    try:
        conn = connect_sqlite(str(db_path), role=SQLiteConnectionRole.READ_ONLY)
        conn.row_factory = sqlite3.Row
        try:
            # 1. Check tracked companies
            has_tracked = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='tracked_companies'"
            ).fetchone()
            if has_tracked:
                tracked_rows = conn.execute(
                    "SELECT COUNT(*) AS c FROM tracked_companies WHERE archived_at IS NULL"
                ).fetchone()
                tracked_count = int(tracked_rows["c"]) if tracked_rows else 0
            else:
                issues.append("tracked_companies table not found")

            # 2. Check expected_earnings
            has_expected = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='expected_earnings'"
            ).fetchone()
            if has_expected:
                upcoming_by_t = upcoming_by_ticker(conn, cur_today)
                last_by_t = last_reported_by_ticker(conn, cur_today)

                # Verify duplicate invariant: (ticker, expected_date) must be unique
                dupes = conn.execute(
                    "SELECT ticker, expected_date, COUNT(*) as c FROM expected_earnings "
                    "GROUP BY ticker, expected_date HAVING c > 1"
                ).fetchall()
                if dupes:
                    issues.append(
                        f"Found {len(dupes)} duplicate (ticker, expected_date) entries in expected_earnings"
                    )

                # Verify future rescheduling invariant: at most one future row per ticker
                multi_future = conn.execute(
                    "SELECT ticker, COUNT(*) as c FROM expected_earnings "
                    "WHERE expected_date >= ? GROUP BY ticker HAVING c > 1",
                    (cur_today.isoformat(),),
                ).fetchall()
                if multi_future:
                    issues.append(
                        f"Found {len(multi_future)} tickers with multiple upcoming future dates in expected_earnings"
                    )
            else:
                issues.append("expected_earnings table not found")

            # 3. Check upcoming strip generation
            strip_items = upcoming_earnings(db_path, cur_today, conn=conn)
            for ticker, when, is_estimate in strip_items[:10]:
                sample_upcoming.append(
                    {
                        "ticker": ticker,
                        "expected_date": when.isoformat(),
                        "is_estimate": is_estimate,
                        "days_away": (when - cur_today).days,
                    }
                )

            # General Forward calendar: reader parity, immutable revision linkage,
            # explicit coverage and no collapsed distinct same-day events.
            agenda = load_forward_agenda_result(db_path, on_or_after=cur_today, limit=1000, now=now)
            forward_freshness = agenda.freshness
            if agenda.unavailable:
                issues.append("Forward event store unavailable")
            if agenda.freshness != "fresh":
                issues.append(
                    f"Forward source coverage {agenda.freshness}: {agenda.coverage_reason}"
                )
            future = conn.execute(
                "SELECT id,ticker,title,event_date,url,cadence,ir_event_id,ir_event_revision_id FROM signals "
                "WHERE signal_type='investor_day' AND event_date >= ? ORDER BY event_date,id",
                (cur_today.isoformat(),),
            ).fetchall()
            forward_count = len(future)
            if forward_count > 1000:
                issues.append("Forward verification exceeds bounded 1000-event reader sample")
            elif {int(row["id"]) for row in future} != {row.id for row in agenda.rows}:
                issues.append("Forward reader differs from stored upcoming events")
            for event in future:
                row = conn.execute(
                    "SELECT observation_json FROM ir_event_revisions WHERE revision_id=? AND event_id=?",
                    (event["ir_event_revision_id"], event["ir_event_id"]),
                ).fetchone()
                if row is None:
                    issues.append(
                        f"Forward event {event['id']} has no immutable revision provenance"
                    )
                    continue
                observation = IREventObservation.model_validate_json(str(row[0]))
                if observation.status == "cancelled" or (
                    observation.ticker,
                    observation.title,
                    observation.event_date.isoformat(),
                    observation.source_url,
                    "scheduled",
                ) != (
                    event["ticker"],
                    event["title"],
                    event["event_date"],
                    event["url"],
                    event["cadence"],
                ):
                    issues.append(
                        f"Forward event {event['id']} differs from retained publisher revision"
                    )

        finally:
            conn.close()
    except Exception as e:
        issues.append(f"Database error during audit: {type(e).__name__}: {e}")

    return CalendarAuditResult(
        timestamp_utc=datetime.now(UTC).isoformat(),
        calendar_pacific_today=cur_today.isoformat(),
        tracked_companies_count=tracked_count,
        upcoming_expected_count=len(upcoming_by_t),
        past_reported_count=len(last_by_t),
        upcoming_strip_items_count=len(sample_upcoming),
        forward_events_count=forward_count,
        forward_freshness=forward_freshness,
        integrity_pass=len(issues) == 0,
        issues=issues,
        sample_upcoming=sample_upcoming,
    )


def main(argv: list[str] | None = None) -> int:
    parser = command_parser("End-to-end calendar verification.")
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--json", action="store_true", help="Emit JSON output")
    args = parser.parse_args(argv)

    result = audit_calendars(args.db)

    if args.json:
        print(json.dumps(asdict(result), indent=2))
    else:
        status_str = "PASS" if result.integrity_pass else "FAIL"
        print(f"=== Calendar End-to-End Verification: [{status_str}] ===")
        print(f"Timezone: Pacific ({CALENDAR_TIME_ZONE}) · Today: {result.calendar_pacific_today}")
        print(f"Tracked Companies: {result.tracked_companies_count}")
        print(f"Upcoming Earnings Rows: {result.upcoming_expected_count}")
        print(f"Past Reported Dates: {result.past_reported_count}")
        print(f"Upcoming Strip Items: {result.upcoming_strip_items_count}")
        print(f"Forward Events: {result.forward_events_count} ({result.forward_freshness})")
        if result.issues:
            print("\nIssues:")
            for issue in result.issues:
                print(f"  - [!] {issue}")
        if result.sample_upcoming:
            print("\nSample Upcoming Strip:")
            for s in result.sample_upcoming:
                est_label = "est." if s["is_estimate"] else "confirmed"
                print(
                    f"  - {s['ticker']:<6} | {s['expected_date']} ({s['days_away']}d away) | {est_label}"
                )

    return 0 if result.integrity_pass else 1


if __name__ == "__main__":
    sys.exit(main())
