"""Status registry for the control-panel dashboard.

Assembles one `DashboardRow` per tracked ticker (portfolio + evaluation only —
watchlist is intentionally out of scope). The row carries everything a single
table row needs to render: last FMP date, last transcript period, last build
mtime, open comment count, breach state. Pure reads, no mutations.

The dashboard endpoint in `execution/comments_server.py` calls
`build_dashboard_rows` once per request — the cost is one tracked_companies
scan plus ~4 cheap per-ticker queries / filesystem lookups, well under the
chat endpoint's budget.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

import comments
from compute.thesis_evaluation_episodes import episode_history_source
from models.companies import Company
from pipeline.queries import BRIEFED_LIST_TYPES, tracked_companies_for_user
from provenance.selection import selected_transcripts_relation


@dataclass(frozen=True)
class TranscriptStatus:
    """Snapshot of the most recent transcript ingest for a ticker."""

    period_end: str | None  # ISO date (YYYY-MM-DD)
    has_qa_section: bool | None
    call_date: str | None  # ISO date


@dataclass(frozen=True)
class DashboardRow:
    """One ticker's row in the dashboard."""

    ticker: str
    list_type: str  # "portfolio" or "evaluation"
    fmp_last_pulled: str | None  # ISO datetime — most recent fmp_endpoint_status.last_pulled
    last_transcript: TranscriptStatus | None
    last_build_at: str | None  # ISO datetime (UTC)
    open_comments_count: int
    breach_status: str | None  # overall_status from thesis_evaluations

    def to_dict(self) -> dict[str, object]:
        return {
            "ticker": self.ticker,
            "list_type": self.list_type,
            "fmp_last_pulled": self.fmp_last_pulled,
            "last_transcript": (
                {
                    "period_end": self.last_transcript.period_end,
                    "has_qa_section": self.last_transcript.has_qa_section,
                    "call_date": self.last_transcript.call_date,
                }
                if self.last_transcript
                else None
            ),
            "last_build_at": self.last_build_at,
            "open_comments_count": self.open_comments_count,
            "breach_status": self.breach_status,
        }


def build_dashboard_rows(
    conn: sqlite3.Connection,
    repo_root: Path,
) -> dict[str, list[DashboardRow]]:
    """Return `{"portfolio": [...], "evaluation": [...]}` sorted by ticker.

    Both keys are always present (possibly empty lists).
    """
    companies = tracked_companies_for_user(
        conn,
        list_types=BRIEFED_LIST_TYPES,
    )
    grouped: dict[str, list[DashboardRow]] = {"portfolio": [], "evaluation": []}
    for company in companies:
        row = _build_one_row(conn, repo_root, company)
        grouped[company.list_type.value].append(row)
    for rows in grouped.values():
        rows.sort(key=lambda r: r.ticker)
    return grouped


def _build_one_row(conn: sqlite3.Connection, repo_root: Path, company: Company) -> DashboardRow:
    return DashboardRow(
        ticker=company.ticker,
        list_type=company.list_type.value,
        fmp_last_pulled=_last_fmp_pulled_for(conn, company.ticker),
        last_transcript=_last_transcript_for(conn, company.ticker),
        last_build_at=_last_build_at(repo_root, company.ticker),
        open_comments_count=_open_comments_count(repo_root, company.ticker),
        breach_status=_latest_breach_status(conn, company.ticker),
    )


def _last_fmp_pulled_for(conn: sqlite3.Connection, ticker: str) -> str | None:
    """When did the FMP fetcher most recently hit any endpoint for this ticker.

    `fmp_endpoint_status.last_pulled` is per (ticker, endpoint, period). The
    MAX across all of a ticker's endpoints is the user's mental model of
    "when did I last refresh FMP for this name." NOTE: this is the fetch
    timestamp, NOT `tracked_companies.fmp_data_upto`, which is the latest
    fiscal-period in the fetched payload (often a forward quarter).
    """
    cur = conn.execute(
        "SELECT MAX(last_pulled) AS last_pulled FROM fmp_endpoint_status WHERE ticker = ?",
        (ticker.upper(),),
    )
    row = cur.fetchone()
    if row is None:
        return None
    value = row["last_pulled"]
    return str(value) if value else None


def _last_transcript_for(conn: sqlite3.Connection, ticker: str) -> TranscriptStatus | None:
    transcripts = selected_transcripts_relation(conn).sql
    cur = conn.execute(
        f"SELECT period_end, has_qa_section, call_date FROM {transcripts} "  # nosec B608 -- trusted internal SQL shape; values remain bound
        "WHERE ticker = ? AND period_end IS NOT NULL "
        "ORDER BY period_end DESC LIMIT 1",
        (ticker.upper(),),
    )
    row = cur.fetchone()
    if row is None:
        return None
    period_end = row["period_end"]
    call_date = row["call_date"]
    qa = row["has_qa_section"]
    return TranscriptStatus(
        period_end=str(period_end)[:10] if period_end else None,
        has_qa_section=bool(qa) if qa is not None else None,
        call_date=str(call_date)[:10] if call_date else None,
    )


def _last_build_at(repo_root: Path, ticker: str) -> str | None:
    research_dir = repo_root / "output" / "research" / ticker.upper()
    if not research_dir.exists():
        return None
    matches = sorted(research_dir.glob("*_workspace.html"))
    if not matches:
        return None
    latest = matches[-1]
    mtime = datetime.fromtimestamp(latest.stat().st_mtime, tz=UTC)
    return mtime.isoformat()


def _open_comments_count(repo_root: Path, ticker: str) -> int:
    base = repo_root / "data" / "report_comments" / ticker.upper()
    if not base.exists():
        return 0
    files = sorted(base.glob("*.json"))
    if not files:
        return 0
    # YYYY-MM-DD.json sorts lexicographically into chronological order.
    latest = files[-1]
    try:
        report_date = date.fromisoformat(latest.stem)
    except ValueError:
        return 0
    return len(comments.list_comments(repo_root, ticker, report_date, status="open"))


def _latest_breach_status(conn: sqlite3.Connection, ticker: str) -> str | None:
    source = episode_history_source(conn)
    cur = conn.execute(
        f"SELECT overall_status FROM {source.relation} WHERE ticker = ? "
        f"ORDER BY {source.latest_checked_column} DESC LIMIT 1",  # nosec B608 -- trusted closed relation
        (ticker.upper(),),
    )
    row = cur.fetchone()
    return row["overall_status"] if row else None
