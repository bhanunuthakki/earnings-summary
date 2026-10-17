"""Status registry for the control-panel dashboard.

Assembles one `DashboardRow` per tracked ticker (portfolio + evaluation only —
watchlist is intentionally out of scope). The row carries everything a single
table row needs to render: last FMP date, last transcript period, last build
mtime, open comment count, breach state. Pure reads, no mutations.

One build is bounded, not per-ticker: a single roster read plus three grouped
queries (FMP pull recency, latest transcript, latest verdict) and one
filesystem pass per artifact family (`output/research` workspace files,
`data/report_comments` stores). The statement count and directory walks do not
scale with roster size (audit `.tmp/audit/performance-serving.md` F2). The
dashboard endpoint in `execution/comments_server.py` and the cockpit row
builder both call `build_dashboard_rows` once per build.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
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
    if not companies:
        return grouped
    # Every lookup keys on the uppercased ticker, exactly like the per-ticker
    # queries this batched form replaced.
    tickers = [company.ticker.upper() for company in companies]
    fmp_last_pulled = _last_fmp_pulled_by_ticker(conn, tickers)
    last_transcript = _last_transcript_by_ticker(conn, tickers)
    breach_status = _latest_breach_status_by_ticker(conn, tickers)
    last_build_at = _last_build_at_by_ticker(repo_root, tickers)
    open_comments_count = _open_comments_count_by_ticker(repo_root, tickers)
    for company in companies:
        ticker = company.ticker.upper()
        grouped[company.list_type.value].append(
            _assemble_row(
                company,
                fmp_last_pulled=fmp_last_pulled.get(ticker),
                last_transcript=last_transcript.get(ticker),
                last_build_at=last_build_at.get(ticker),
                open_comments_count=open_comments_count.get(ticker, 0),
                breach_status=breach_status.get(ticker),
            )
        )
    for rows in grouped.values():
        rows.sort(key=lambda r: r.ticker)
    return grouped


def _placeholders(values: list[str]) -> str:
    """Comma-joined bind placeholders for a roster-sized IN list."""
    return ",".join("?" for _ in values)


def _assemble_row(
    company: Company,
    *,
    fmp_last_pulled: str | None,
    last_transcript: TranscriptStatus | None,
    last_build_at: str | None,
    open_comments_count: int,
    breach_status: str | None,
) -> DashboardRow:
    """Assemble one row from the pre-fetched per-ticker values."""
    return DashboardRow(
        ticker=company.ticker,
        list_type=company.list_type.value,
        fmp_last_pulled=fmp_last_pulled,
        last_transcript=last_transcript,
        last_build_at=last_build_at,
        open_comments_count=open_comments_count,
        breach_status=breach_status,
    )


def _last_fmp_pulled_by_ticker(
    conn: sqlite3.Connection, tickers: list[str]
) -> dict[str, str | None]:
    """ticker → most recent `fmp_endpoint_status.last_pulled` (one grouped query).

    `last_pulled` is per (ticker, endpoint, period). The MAX across all of a
    ticker's endpoints is the user's mental model of "when did I last refresh
    FMP for this name." NOTE: this is the fetch timestamp, NOT
    `tracked_companies.fmp_data_upto`, which is the latest fiscal-period in
    the fetched payload (often a forward quarter).
    """
    cur = conn.execute(
        "SELECT ticker, MAX(last_pulled) AS last_pulled "
        "FROM fmp_endpoint_status "
        f"WHERE ticker IN ({_placeholders(tickers)}) "  # nosec B608 -- fixed statement shape; values remain bound
        "GROUP BY ticker",
        tickers,
    )
    return {
        str(row["ticker"]): (str(row["last_pulled"]) if row["last_pulled"] else None) for row in cur
    }


def _last_transcript_by_ticker(
    conn: sqlite3.Connection, tickers: list[str]
) -> dict[str, TranscriptStatus]:
    """ticker → most recent transcript status (one windowed query).

    The latest row per ticker is the one with the greatest `period_end`
    (`period_end IS NOT NULL` rows only, as before). Ties within one period
    keep the same unspecified order the per-ticker `ORDER BY period_end DESC
    LIMIT 1` had; adding a tiebreaker column would break the minimal-schema
    degradation contract, which does not guarantee an `id` column.
    """
    transcripts = selected_transcripts_relation(conn).sql
    cur = conn.execute(
        "SELECT ticker, period_end, has_qa_section, call_date FROM ("
        "  SELECT ticker, period_end, has_qa_section, call_date,"
        "  ROW_NUMBER() OVER (PARTITION BY ticker ORDER BY period_end DESC) AS rn"
        f"  FROM {transcripts}"  # nosec B608 -- trusted closed relation; values stay bound
        f"  WHERE ticker IN ({_placeholders(tickers)}) AND period_end IS NOT NULL"
        ") WHERE rn = 1",
        tickers,
    )
    return {str(row["ticker"]): _transcript_status(row) for row in cur}


def _transcript_status(row: sqlite3.Row) -> TranscriptStatus:
    period_end = row["period_end"]
    call_date = row["call_date"]
    qa = row["has_qa_section"]
    return TranscriptStatus(
        period_end=str(period_end)[:10] if period_end else None,
        has_qa_section=bool(qa) if qa is not None else None,
        call_date=str(call_date)[:10] if call_date else None,
    )


def _latest_breach_status_by_ticker(conn: sqlite3.Connection, tickers: list[str]) -> dict[str, str]:
    """ticker → latest thesis verdict (one windowed query).

    The latest row per ticker is the one with the greatest
    `last_checked_at` (or `evaluated_at` on the pre-0014 legacy relation).
    Ties within one timestamp keep the same unspecified order the
    per-ticker `ORDER BY ... DESC LIMIT 1` had; no optional tiebreaker
    column is referenced because the minimal-schema degradation contract
    does not guarantee one.
    """
    source = episode_history_source(conn)
    cur = conn.execute(
        "SELECT ticker, overall_status FROM ("
        "  SELECT ticker, overall_status,"
        f"  ROW_NUMBER() OVER (PARTITION BY ticker ORDER BY {source.latest_checked_column} DESC) AS rn"  # nosec B608 -- trusted closed relation; values stay bound
        f"  FROM {source.relation}"
        f"  WHERE ticker IN ({_placeholders(tickers)})"
        ") WHERE rn = 1",
        tickers,
    )
    return {str(row["ticker"]): row["overall_status"] for row in cur}


def _latest_by_parent_name(paths: Iterable[Path]) -> dict[str, Path]:
    """Group a one-pass glob result by directory name, newest filename wins."""
    latest: dict[str, Path] = {}
    for path in paths:
        parent = path.parent.name
        current = latest.get(parent)
        if current is None or path.name > current.name:
            latest[parent] = path
    return latest


def _last_build_at_by_ticker(repo_root: Path, tickers: list[str]) -> dict[str, str]:
    """ticker → ISO mtime of the newest `*_workspace.html` under the ticker's
    `output/research/<T>` directory.

    One glob pass over `output/research/*/*_workspace.html` replaces the
    per-ticker directory walk; the winner per ticker is the
    lexicographically-latest filename, the same "newest report date wins"
    rule the per-ticker `sorted(glob("*_workspace.html"))[-1]` applied.
    """
    research_root = repo_root / "output" / "research"
    if not research_root.is_dir():
        return {}
    latest = _latest_by_parent_name(research_root.glob("*/*_workspace.html"))
    out: dict[str, str] = {}
    for ticker in tickers:
        winner = latest.get(ticker)
        if winner is None:
            continue
        mtime = datetime.fromtimestamp(winner.stat().st_mtime, tz=UTC)
        out[ticker] = mtime.isoformat()
    return out


def _open_comments_count_by_ticker(repo_root: Path, tickers: list[str]) -> dict[str, int]:
    """ticker → open-comment count on that ticker's newest report date.

    One glob pass over `data/report_comments/*/*.json` finds each ticker's
    lexicographically-latest store file (`YYYY-MM-DD.json` sorts into
    chronological order, so the newest report date wins, as before). Only the
    winner file is read and only via the shared comment-store reader, so
    unreadable/invalid stores keep failing closed exactly like the
    per-ticker walk.
    """
    base = repo_root / "data" / "report_comments"
    if not base.is_dir():
        return {}
    latest = _latest_by_parent_name(base.glob("*/*.json"))
    out: dict[str, int] = {}
    for ticker in tickers:
        winner = latest.get(ticker)
        if winner is None:
            continue
        try:
            report_date = date.fromisoformat(winner.stem)
        except ValueError:
            # Newest name is not a `YYYY-MM-DD.json` store file: the same
            # zero-count the per-ticker walk reported for it.
            continue
        out[ticker] = len(comments.list_comments(repo_root, ticker, report_date, status="open"))
    return out
