"""Read-only benchmark for the dashboard Overview database workload."""

from __future__ import annotations

import shutil
import sqlite3
import statistics
import time
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TypedDict

from dashboard.inbox import collect_inbox
from dashboard.upcoming import render_upcoming_strip
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
    fresh_connection_per_request: ScenarioResult
    request_scoped_reused_connection: ScenarioResult
    connection_open_reduction: int


_FIXED_NOW = datetime(2026, 8, 8, 12, tzinfo=UTC)


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


def _open(db_path: Path) -> sqlite3.Connection:
    return connect_sqlite(
        db_path,
        role=SQLiteConnectionRole.READ_ONLY,
        schema_preflight=True,
    )


def _measure(repo_root: Path, iterations: int, *, reuse_connection: bool) -> ScenarioResult:
    db_path = repo_root / "data" / "portfolio.db"
    samples: list[float] = []
    receipts: list[OverviewReceipt] = []
    opens = 0
    shared = _open(db_path) if reuse_connection else None
    if shared is not None:
        opens += 1
    try:
        for _ in range(iterations):
            conn = shared or _open(db_path)
            if shared is None:
                opens += 1
            started = time.perf_counter()
            try:
                receipts.append(_overview_read(conn, repo_root))
            finally:
                samples.append((time.perf_counter() - started) * 1_000)
                if shared is None:
                    conn.close()
    finally:
        if shared is not None:
            shared.close()
    if any(receipt != receipts[0] for receipt in receipts[1:]):
        raise RuntimeError("overview workload changed across benchmark iterations")
    warm = samples[1:] or samples
    return {
        "connection_opens": opens,
        "first_request_ms": round(samples[0], 3),
        "warm_median_ms": round(statistics.median(warm), 3),
        "request_ms": [round(sample, 3) for sample in samples],
    }


def _copy_stable(source: Path, destination: Path) -> bool:
    before = (source.stat().st_size, source.stat().st_mtime_ns)
    shutil.copy2(source, destination)
    source_wal = Path(f"{source}-wal")
    if source_wal.exists():
        shutil.copy2(source_wal, Path(f"{destination}-wal"))
    return before == (source.stat().st_size, source.stat().st_mtime_ns)


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


def benchmark_production_overview(
    source_db: Path, iterations: int
) -> OverviewBenchmarkResult:
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
        writer = _install_wal_marker(db_path)
        wal_bytes = Path(f"{db_path}-wal").stat().st_size
        proof = _open(db_path)
        try:
            journal_mode = str(proof.execute("PRAGMA journal_mode").fetchone()[0])
            marker_visible = proof.execute(
                "SELECT value FROM benchmark_wal_marker"
            ).fetchone()[0] == 1
            fresh = _measure(repo_root, iterations, reuse_connection=False)
            reused = _measure(repo_root, iterations, reuse_connection=True)
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
        "fresh_connection_per_request": fresh,
        "request_scoped_reused_connection": reused,
        "connection_open_reduction": fresh["connection_opens"] - reused["connection_opens"],
    }


__all__ = ["benchmark_production_overview"]
