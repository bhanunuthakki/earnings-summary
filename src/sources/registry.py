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
from dataclasses import dataclass, field
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
    NOT_FOUND = "not_found"  # source returned empty for this ticker/kind
    TIER_RESTRICTED = "tier_restricted"  # 403 / Legacy Endpoint / etc.
    RATE_LIMITED = "rate_limited"  # 429
    ERROR = "error"  # network / parse / unknown
    SKIPPED = "skipped"  # adapter chose not to call (e.g. cache hit)


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


@dataclass(frozen=True, slots=True)
class PendingSourceCall:
    """One ``source_calls`` row queued for a batched insert (see
    :func:`log_calls_batch`). Mirrors :func:`log_call`'s parameters so a
    high-volume caller can accumulate per-call outcomes and flush them in a
    single transaction."""

    source_name: str
    kind: str
    ticker: str | None
    status: CallStatus | str
    latency_ms: int | None = None
    http_code: int | None = None
    record_count: int | None = None
    notes: str | None = None


def log_calls_batch(calls: list[PendingSourceCall]) -> None:
    """Insert many ``source_calls`` rows in one transaction. Best-effort —
    never raises (a logging failure must not cascade into the caller's data
    path).

    The FMP fetcher logs one row per logical endpoint per ticker per run;
    opening a fresh connection per row (as :func:`log_call` does) would
    reintroduce the per-call fsync cost the ``fmp_endpoint_status`` batching was
    built to avoid, so callers in a tight loop accumulate ``PendingSourceCall``
    and flush once here. All rows share one ``called_at`` stamp (the flush
    time), which is adequate for the per-(source, kind) rollup that consumes
    them.
    """
    if not calls or not _DB_PATH.exists():
        return
    stamp = datetime.now().isoformat(timespec="seconds")
    rows = [
        (
            c.source_name,
            c.kind,
            c.ticker.upper() if c.ticker else None,
            stamp,
            c.latency_ms,
            c.status.value if isinstance(c.status, CallStatus) else str(c.status),
            c.http_code,
            c.record_count,
            (c.notes or "")[:256] or None,
        )
        for c in calls
    ]
    try:
        conn = sqlite3.connect(str(_DB_PATH))
        try:
            conn.executemany(
                """
                INSERT INTO source_calls
                    (source_name, kind, ticker, called_at, latency_ms,
                     status, http_code, record_count, notes)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            conn.commit()
        finally:
            conn.close()
    except sqlite3.Error:
        return


# ---------------------------------------------------------------------------
# Read side — the consumer the module docstring promised as a follow-up.
# Makes external-fetch volume, error rates, and cache-skip rate measurable
# (you can't keep caching honest if you can't see it). Consumed by
# execution/show_source_calls.py.
# ---------------------------------------------------------------------------

# Statuses that represent a failed/blocked external call (vs. a clean OK, an
# empty NOT_FOUND, or a deliberate SKIPPED cache hit).
_ERROR_STATUSES: frozenset[str] = frozenset(
    {CallStatus.ERROR, CallStatus.RATE_LIMITED, CallStatus.TIER_RESTRICTED}
)


@dataclass(frozen=True, slots=True)
class SourceCallSummary:
    """Per-(source, kind) rollup of source_calls. ``cache_skip_rate`` is the
    share of attempts that never hit the network (a cache hit / deliberate
    skip); ``error_rate`` is the share that failed or were blocked."""

    source_name: str
    kind: str
    total: int
    ok: int
    skipped: int
    not_found: int
    errors: int
    error_rate: float
    cache_skip_rate: float
    p50_latency_ms: int | None
    p95_latency_ms: int | None
    total_records: int


def _empty_int_list() -> list[int]:
    # Typed factory so the dataclass field is list[int], not list[Unknown].
    return []


@dataclass(slots=True)
class _Acc:
    """Mutable per-(source, kind) accumulator used while folding rows."""

    total: int = 0
    ok: int = 0
    skipped: int = 0
    not_found: int = 0
    errors: int = 0
    records: int = 0
    latencies: list[int] = field(default_factory=_empty_int_list)


def _percentile(sorted_vals: list[int], pct: float) -> int | None:
    if not sorted_vals:
        return None
    # Nearest-rank percentile — adequate for an operational latency readout.
    rank = max(0, min(len(sorted_vals) - 1, round(pct * (len(sorted_vals) - 1))))
    return sorted_vals[rank]


def summarize_source_calls(
    *,
    since: str | None = None,
    db_path: Path | None = None,
) -> list[SourceCallSummary]:
    """Aggregate ``source_calls`` into a per-(source_name, kind) summary.

    ``since`` is an inclusive ISO lower bound on ``called_at`` (e.g. ``'2026-06-01'``).
    Read-only and best-effort: a missing DB / table yields ``[]`` rather than
    raising, so a reporting call never takes down a caller. Rows are sorted by
    descending total so the busiest source/kind reads first.
    """
    path = db_path or _DB_PATH
    if not Path(path).exists():
        return []
    sql = "SELECT source_name, kind, status, latency_ms, record_count FROM source_calls"
    params: tuple[str, ...] = ()
    if since is not None:
        sql += " WHERE called_at >= ?"
        params = (since,)
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            rows = conn.execute(sql, params).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return []

    groups: dict[tuple[str, str], _Acc] = {}
    for source_name, kind, status, latency_ms, record_count in rows:
        acc = groups.setdefault((str(source_name), str(kind)), _Acc())
        acc.total += 1
        if status == CallStatus.OK:
            acc.ok += 1
        elif status == CallStatus.SKIPPED:
            acc.skipped += 1
        elif status == CallStatus.NOT_FOUND:
            acc.not_found += 1
        elif status in _ERROR_STATUSES:
            acc.errors += 1
        if record_count is not None:
            acc.records += int(record_count)
        if latency_ms is not None:
            acc.latencies.append(int(latency_ms))

    summaries = [
        SourceCallSummary(
            source_name=source_name,
            kind=kind,
            total=acc.total,
            ok=acc.ok,
            skipped=acc.skipped,
            not_found=acc.not_found,
            errors=acc.errors,
            error_rate=(acc.errors / acc.total) if acc.total else 0.0,
            cache_skip_rate=(acc.skipped / acc.total) if acc.total else 0.0,
            p50_latency_ms=_percentile(sorted(acc.latencies), 0.50),
            p95_latency_ms=_percentile(sorted(acc.latencies), 0.95),
            total_records=acc.records,
        )
        for (source_name, kind), acc in groups.items()
    ]
    summaries.sort(key=lambda s: s.total, reverse=True)
    return summaries
