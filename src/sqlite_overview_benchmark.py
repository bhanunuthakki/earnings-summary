"""Read-only benchmark for the dashboard Overview database workload."""

from __future__ import annotations

import shutil
import sqlite3
import statistics
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import ParamSpec, TypedDict
from unittest.mock import patch

from dashboard.inbox import collect_inbox
from dashboard.upcoming import render_upcoming_strip
from pipeline.dashboard_status import build_dashboard_rows
from pipeline.open_loops import render_open_loops_band
from pipeline.research_cockpit import build_cockpit_rows
from pipeline.tier_runner import tier_coverage_summary
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite


class OverviewReceipt(TypedDict):
    cockpit_rows: int
    inbox_items: int
    coverage_rows: int
    upcoming_bytes: int
    open_loops_bytes: int


class ScenarioResult(TypedDict):
    connection_opens: int
    first_request_ms: float
    warm_median_ms: float
    request_ms: list[float]


class WalReadResult(TypedDict):
    journal_mode: str
    committed_marker_visible: bool
    wal_bytes: int


class OverviewBenchmarkResult(TypedDict):
    mode: str
    source_db: str
    source_bytes: int
    iterations: int
    wal_read: WalReadResult
    staged_cache_files: list[str]
    measurement_order: list[str]
    legacy_component_owned: ScenarioResult
    request_scoped_per_request: ScenarioResult
    connection_open_reduction: int
    receipts_equal: bool
    workload_receipt: OverviewReceipt


_FIXED_NOW = datetime(2026, 8, 8, 12, tzinfo=UTC)
_MATERIALIZED_CACHE_PATHS: tuple[tuple[str, ...], ...] = (
    ("data", "candidate_fit.json"),
    ("data", "cockpit_fundamentals.json"),
    ("data", "etf_score.json"),
    ("data", "portfolio_weights.json"),
)
_P = ParamSpec("_P")


class _OpenCounter:
    def __init__(self) -> None:
        self.total = 0

    def increment(self) -> None:
        self.total += 1


def _counted_connect(
    connect: Callable[_P, sqlite3.Connection],
    counter: _OpenCounter,
) -> Callable[_P, sqlite3.Connection]:
    def counted(*args: _P.args, **kwargs: _P.kwargs) -> sqlite3.Connection:
        counter.increment()
        return connect(*args, **kwargs)

    return counted


def _overview_read(conn: sqlite3.Connection, repo_root: Path) -> OverviewReceipt:
    """Exercise the DB-reading portion of ``/api/panel/overview``."""
    db_path = repo_root / "data" / "portfolio.db"
    rows = build_cockpit_rows(conn, repo_root, now=_FIXED_NOW)
    coverage = tier_coverage_summary(repo_root, now=_FIXED_NOW.replace(tzinfo=None), conn=conn)
    inbox = collect_inbox(db_path, limit=14, now=_FIXED_NOW, conn=conn)
    upcoming = render_upcoming_strip(db_path, _FIXED_NOW.date(), conn=conn)
    open_loops = render_open_loops_band(db_path, now=_FIXED_NOW, conn=conn)
    return {
        "cockpit_rows": sum(len(group) for group in rows.values()),
        "inbox_items": len(inbox),
        "coverage_rows": sum(group["total"] for group in coverage.values()),
        "upcoming_bytes": len(upcoming.encode()),
        "open_loops_bytes": len(open_loops.encode()),
    }


def _legacy_overview_read(repo_root: Path) -> OverviewReceipt:
    """Exercise the component-owned connection pattern replaced by the route.

    Each component receives no borrowed connection and therefore owns its full
    connection lifetime, including any nested inbox-ranking or open-loop reads.
    The cockpit has always required an explicit connection, so this adapter
    models its former component-owned open directly.
    """
    db_path = repo_root / "data" / "portfolio.db"
    cockpit_conn = _open(db_path)
    try:
        rows = build_cockpit_rows(cockpit_conn, repo_root, now=_FIXED_NOW)
    finally:
        cockpit_conn.close()
    coverage = tier_coverage_summary(repo_root, now=_FIXED_NOW.replace(tzinfo=None))
    inbox = collect_inbox(db_path, limit=14, now=_FIXED_NOW)
    upcoming = render_upcoming_strip(db_path, _FIXED_NOW.date())
    open_loops = render_open_loops_band(db_path, now=_FIXED_NOW)
    return {
        "cockpit_rows": sum(len(group) for group in rows.values()),
        "inbox_items": len(inbox),
        "coverage_rows": sum(group["total"] for group in coverage.values()),
        "upcoming_bytes": len(upcoming.encode()),
        "open_loops_bytes": len(open_loops.encode()),
    }


def _open(db_path: Path) -> sqlite3.Connection:
    return connect_sqlite(
        db_path,
        role=SQLiteConnectionRole.READ_ONLY,
        schema_preflight=True,
    )


def _request_scoped_overview_read(repo_root: Path) -> OverviewReceipt:
    db_path = repo_root / "data" / "portfolio.db"
    conn = _open(db_path)
    try:
        return _overview_read(conn, repo_root)
    finally:
        conn.close()


def _scenario_result(samples: list[float], opens: int) -> ScenarioResult:
    warm = samples[1:] or samples
    return {
        "connection_opens": opens,
        "first_request_ms": round(samples[0], 3),
        "warm_median_ms": round(statistics.median(warm), 3),
        "request_ms": [round(sample, 3) for sample in samples],
    }


def _measure_ab(
    repo_root: Path,
    iterations: int,
) -> tuple[ScenarioResult, ScenarioResult, OverviewReceipt, list[str]]:
    samples: dict[str, list[float]] = {"legacy": [], "request_scoped": []}
    opens: dict[str, int] = {"legacy": 0, "request_scoped": 0}
    receipts: list[OverviewReceipt] = []
    order_receipt: list[str] = []
    counter = _OpenCounter()
    measured: dict[str, Callable[[Path], OverviewReceipt]] = {
        "legacy": _legacy_overview_read,
        "request_scoped": _request_scoped_overview_read,
    }
    counted_connect = _counted_connect(sqlite3.connect, counter)
    with patch.object(sqlite3, "connect", counted_connect):
        for iteration in range(iterations):
            order = (
                ("legacy", "request_scoped") if iteration % 2 == 0 else ("request_scoped", "legacy")
            )
            order_receipt.append(",".join(order))
            for scenario in order:
                opens_before = counter.total
                started = time.perf_counter()
                receipt = measured[scenario](repo_root)
                elapsed_ms = (time.perf_counter() - started) * 1_000
                samples[scenario].append(elapsed_ms)
                opens[scenario] += counter.total - opens_before
                receipts.append(receipt)

    if any(receipt != receipts[0] for receipt in receipts[1:]):
        raise RuntimeError("overview workload changed across benchmark iterations")
    return (
        _scenario_result(samples["legacy"], opens["legacy"]),
        _scenario_result(samples["request_scoped"], opens["request_scoped"]),
        receipts[0],
        order_receipt,
    )


def _copy_stable(source: Path, destination: Path) -> bool:
    before = (source.stat().st_size, source.stat().st_mtime_ns)
    shutil.copy2(source, destination)
    source_wal = Path(f"{source}-wal")
    if source_wal.exists():
        shutil.copy2(source_wal, Path(f"{destination}-wal"))
    return before == (source.stat().st_size, source.stat().st_mtime_ns)


def _stage_materialized_inputs(source_db: Path, repo_root: Path) -> list[str]:
    """Copy the exact disk inputs read by the Overview composition."""
    if source_db.parent.name.lower() != "data":
        return []
    source_root = source_db.parent.parent
    staged: list[str] = []

    def stage(relative: Path) -> None:
        source = source_root / relative
        if not source.is_file():
            return
        destination = repo_root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        staged.append(relative.as_posix())

    for parts in _MATERIALIZED_CACHE_PATHS:
        stage(Path(*parts))

    db_path = repo_root / "data" / "portfolio.db"
    conn = _open(db_path)
    tickers: set[str]
    try:
        base_rows = build_dashboard_rows(conn, repo_root)
        tickers = {row.ticker.upper() for rows in base_rows.values() for row in rows}
    except sqlite3.Error:
        tickers = set()
    finally:
        conn.close()
    for ticker in sorted(tickers):
        stage(Path("data", "historical", "fmp", f"{ticker}_profile.json"))
        stage(Path("data", "historical", "fmp", f"{ticker}_earnings_calendar.json"))
        stage(Path("data", "valuation_basis", f"{ticker}.json"))
    return sorted(staged)


def _install_wal_marker(db_path: Path) -> sqlite3.Connection:
    """Put a committed marker in the staged copy's WAL, not in the source DB."""
    writer = sqlite3.connect(db_path)
    writer.execute("PRAGMA journal_mode = WAL")
    writer.execute("PRAGMA wal_autocheckpoint = 0")
    writer.execute("CREATE TABLE IF NOT EXISTS benchmark_wal_marker(value INTEGER NOT NULL)")
    writer.execute("DELETE FROM benchmark_wal_marker")
    writer.execute("INSERT INTO benchmark_wal_marker VALUES (1)")
    writer.commit()
    return writer


def benchmark_production_overview(source_db: Path, iterations: int) -> OverviewBenchmarkResult:
    """Benchmark a private production-derived copy without writing to ``source_db``."""
    if iterations <= 0:
        raise ValueError("iterations must be positive")
    source_db = source_db.resolve()
    if not source_db.is_file():
        raise FileNotFoundError(source_db)
    with TemporaryDirectory(prefix="overview-sqlite-") as temp:
        repo_root = Path(temp)
        db_path = repo_root / "data" / "portfolio.db"
        db_path.parent.mkdir()
        if not _copy_stable(source_db, db_path):
            raise RuntimeError("source database changed while its benchmark copy was staged")
        staged_cache_files = _stage_materialized_inputs(source_db, repo_root)
        writer = _install_wal_marker(db_path)
        wal_bytes = Path(f"{db_path}-wal").stat().st_size
        proof = _open(db_path)
        try:
            journal_mode = str(proof.execute("PRAGMA journal_mode").fetchone()[0])
            marker_visible = (
                proof.execute("SELECT value FROM benchmark_wal_marker").fetchone()[0] == 1
            )
            legacy, scoped, receipt, measurement_order = _measure_ab(repo_root, iterations)
        finally:
            proof.close()
            writer.close()
    return {
        "mode": "production_overview_read",
        "source_db": str(source_db),
        "source_bytes": source_db.stat().st_size,
        "iterations": iterations,
        "wal_read": {
            "journal_mode": journal_mode,
            "committed_marker_visible": marker_visible,
            "wal_bytes": wal_bytes,
        },
        "staged_cache_files": staged_cache_files,
        "measurement_order": measurement_order,
        "legacy_component_owned": legacy,
        "request_scoped_per_request": scoped,
        "connection_open_reduction": (legacy["connection_opens"] - scoped["connection_opens"]),
        "receipts_equal": True,
        "workload_receipt": receipt,
    }


__all__ = ["benchmark_production_overview"]
