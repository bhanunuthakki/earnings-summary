"""Generic source-call provenance log.

Every adapter in `sources/` writes one row per attempt (success OR failure) to
the `source_calls` table. The log lets future "intelligent routing" decisions
look back at:

  - Which sources were reachable last week / month / year
  - What tiers exposed which endpoints over time (FMP downgrade detection)
  - Per-source latency distributions
  - Which fallbacks actually saved a brief

For now this is a write-many / read-rarely log. The router that consumes it
will be a follow-up; this module just maintains the schema cleanly.

Usage from an adapter:

    from sources.registry import log_call, CallStatus

    started = time.monotonic()
    try:
        data = _fetch_from_yfinance(ticker)
        log_call(
            source_name="yfinance",
            kind="live_price",
            ticker=ticker,
            status=CallStatus.OK,
            latency_ms=int((time.monotonic() - started) * 1000),
            record_count=1 if data else 0,
        )
        return data
    except Exception as e:
        log_call(
            source_name="yfinance",
            kind="live_price",
            ticker=ticker,
            status=CallStatus.ERROR,
            latency_ms=int((time.monotonic() - started) * 1000),
            notes=f"{type(e).__name__}: {e}"[:256],
        )
        return None
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from enum import StrEnum
from pathlib import Path

# Module-level DB path; overridable for tests via `set_db_path()`.
# Defaults to <project_root>/data/portfolio.db. We resolve project_root from
# this file's location (src/sources/registry.py -> ../../).
_DB_PATH: Path = Path(__file__).resolve().parents[2] / "data" / "portfolio.db"


def set_db_path(path: Path) -> None:
    """Override the DB path used by `log_call`. Used by tests and worktree runs."""
    global _DB_PATH
    _DB_PATH = path


class CallStatus(StrEnum):
    """Closed enum for source_calls.status. Keep stable — the log is queried."""

    OK = "ok"
    NOT_FOUND = "not_found"          # source returned empty for this ticker/kind
    TIER_RESTRICTED = "tier_restricted"  # 403 / Legacy Endpoint / etc.
    RATE_LIMITED = "rate_limited"    # 429
    ERROR = "error"                  # network / parse / unknown
    SKIPPED = "skipped"              # adapter chose not to call (e.g. cache hit)


def log_call(
    *,
    source_name: str,
    kind: str,
    ticker: str | None,
    status: CallStatus | str,
    latency_ms: int | None = None,
    http_code: int | None = None,
    record_count: int | None = None,
    notes: str | None = None,
) -> None:
    """Insert one row into source_calls. Best-effort — never raises.

    A logging-failure must not cascade into the caller's data path. If the DB
    is missing or locked we drop the row silently.
    """
    if not _DB_PATH.exists():
        return
    status_str = status.value if isinstance(status, CallStatus) else str(status)
    try:
        conn = sqlite3.connect(str(_DB_PATH))
        try:
            conn.execute(
                """
                INSERT INTO source_calls
                    (source_name, kind, ticker, called_at, latency_ms,
                     status, http_code, record_count, notes)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    source_name,
                    kind,
                    ticker.upper() if ticker else None,
                    datetime.now().isoformat(timespec="seconds"),
                    latency_ms,
                    status_str,
                    http_code,
                    record_count,
                    (notes or "")[:256] or None,
                ),
            )
            conn.commit()
        finally:
            conn.close()
    except sqlite3.Error:
        # Logging must not crash the fetch path. Silently drop.
        return
