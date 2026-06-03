"""Batch IR-document discovery + fetch across the portfolio + evaluation list.

The scheduled companion to the single-ticker CLIs. For every active-universe
ticker in the ``documents`` roster (``tracked_companies.list_type`` in
``portfolio`` / ``evaluation`` — i.e. ``db.BRIEFED_LIST_TYPES``), it runs two
subprocess-isolated stages:

  1. ``discover_ir_documents.py --ticker <T>`` — headless-crawl the issuer's IR
     site and (re-)write its URL manifest.
  2. ``fetch_ir_documents.py --ticker <T> --categorize --calendar <id>`` —
     download the manifest's documents into staging and register them at the
     canonical path (``documents`` table + ir_narrative-visible layout). The
     ``--calendar`` is the FYE-derived id (``ir_uploads.calendar_id_from_fye``),
     so even a ticker not yet in ``ISSUER_REGISTRY`` registers best-effort.

Because the roster is read from the DB at run time, **newly-added evaluation
companies are picked up automatically** on the next run — no per-ticker config.

Resilience contract (mirrors ``refresh_ir_kpis_all.py``):

  * Each ticker's stages run as subprocess-isolated children with per-stage
    timeouts, so a hung headless browser or a crash in one ticker is contained
    and killable — it cannot take down the batch.
  * The batch NEVER aborts early: a failed/timed-out ticker is logged and the
    remaining tickers still run.
  * A ticker with no resolvable IR URL is ``SKIPPED`` (best-effort, NOT a failure).
  * Exit code = count of FAILED tickers (0 = all good), reported only AFTER every
    ticker has been attempted — so cron/monitoring can detect partial failure.

The discover stage needs the optional ``ir`` extra (headless browser):
``pip install -e .[ir] && playwright install chromium``. A missing extra surfaces
as a per-ticker failure (the child exits non-zero), not a batch crash.

Usage:
    python execution/discover_ir_documents_all.py
    python execution/discover_ir_documents_all.py --tickers NU ORCL
    python execution/discover_ir_documents_all.py --max-quarters 12 --discover-timeout 600
    python execution/discover_ir_documents_all.py --skip-download   # discover only
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import cast

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from ir_uploads import calendar_id_from_fye  # noqa: E402

# Roster = the "briefed" active universe (portfolio + evaluation). Hardcoded to
# match db.BRIEFED_LIST_TYPES (the import is avoided to keep this orchestrator
# light + test-isolated, like execution/run_triggers.py).
_ROSTER_LIST_TYPES_SQL = "('portfolio', 'evaluation')"
_DEFAULT_QUARTERS = 8
_DEFAULT_DISCOVER_TIMEOUT_S = 300  # headless browser render
_DEFAULT_DOWNLOAD_TIMEOUT_S = 300  # download + content-classify + register


class TickerStatus(StrEnum):
    OK = "ok"
    FAILED = "failed"
    SKIPPED = "skipped"  # no resolvable IR URL — best-effort, not a failure


@dataclass(slots=True)
class _TickerResult:
    ticker: str
    status: TickerStatus
    discovered: int | None
    downloaded: int | None
    elapsed_seconds: float
    error: str | None = None


def _resolve_roster(db_path: Path, requested: list[str] | None) -> tuple[list[str], list[str]]:
    """Return ``(selected, skipped_not_in_roster)``.

    ``selected`` are the portfolio+evaluation tickers to process; with a
    ``--tickers`` filter, the intersection is taken and requested tickers outside
    the roster are surfaced (logged, never run).
    """
    roster: list[str] = []
    if db_path.exists():
        try:
            conn = sqlite3.connect(str(db_path))
            conn.row_factory = sqlite3.Row
            try:
                rows = conn.execute(
                    "SELECT ticker FROM tracked_companies "
                    f"WHERE list_type IN {_ROSTER_LIST_TYPES_SQL} AND archived_at IS NULL "
                    "ORDER BY ticker"
                ).fetchall()
            finally:
                conn.close()
            roster = [str(r["ticker"]).upper() for r in rows]
        except sqlite3.Error:
            roster = []
    roster_set = set(roster)
    if requested:
        wanted = [t.upper() for t in requested]
        selected = sorted({t for t in wanted if t in roster_set})
        skipped = sorted({t for t in wanted if t not in roster_set})
        return selected, skipped
    return sorted(roster_set), []


def _ticker_fye(db_path: Path, ticker: str) -> str | None:
    """``tracked_companies.fiscal_year_end`` ("MM-DD") for ``ticker``, or None."""
    if not db_path.exists():
        return None
    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "SELECT fiscal_year_end FROM tracked_companies WHERE ticker = ? LIMIT 1",
                (ticker.upper(),),
            ).fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    val = row["fiscal_year_end"]
    return str(val) if val else None


def _json_field(stdout: str, key: str) -> object:
    """Pull ``key`` from the last balanced top-level JSON object in ``stdout``."""
    s = stdout.strip()
    start, end = s.rfind("{"), s.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        obj = json.loads(s[start : end + 1])
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict):
        return None
    return cast("dict[str, object]", obj).get(key)


def _fail(ticker: str, reason: str, elapsed: float) -> _TickerResult:
    sys.stderr.write(f"\n!!! [{ticker}] FAILED - {reason}\n")
    sys.stdout.write(f"[{ticker}] FAILED - {reason} ({elapsed}s)\n")
    return _TickerResult(ticker, TickerStatus.FAILED, None, None, elapsed, error=reason)


def _run_child(argv: list[str], timeout_s: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout_s,
        check=False,
    )


def _run_one(
    ticker: str,
    *,
    repo_root: Path,
    db_path: Path,
    quarters: int,
    discover_timeout: int,
    download_timeout: int,
    skip_download: bool,
) -> _TickerResult:
    """Discover then fetch one ticker, subprocess-isolated. Never raises."""
    sys.stdout.write(f"\n{'=' * 72}\n=== IR-document discovery - {ticker}\n{'=' * 72}\n")
    sys.stdout.flush()
    t0 = time.monotonic()

    # --- Stage 1: discover ---
    discover_argv = [
        sys.executable,
        str(PROJECT_ROOT / "execution" / "discover_ir_documents.py"),
        "--ticker",
        ticker,
        "--max-quarters",
        str(quarters),
        "--repo-root",
        str(repo_root),
        "--db",
        str(db_path),
    ]
    try:
        disc = _run_child(discover_argv, discover_timeout)
    except subprocess.TimeoutExpired:
        return _fail(ticker, f"discover timed out after {discover_timeout}s", _elapsed(t0))
    except OSError as exc:
        return _fail(ticker, f"discover spawn failed: {exc}", _elapsed(t0))
    if disc.stdout:
        sys.stdout.write(disc.stdout if disc.stdout.endswith("\n") else disc.stdout + "\n")
    if disc.stderr:
        sys.stderr.write(disc.stderr)
    if disc.returncode != 0:
        return _fail(ticker, f"discover exited {disc.returncode}", _elapsed(t0))

    status = _json_field(disc.stdout, "status")
    discovered = _json_field(disc.stdout, "discovered")
    discovered_n = discovered if isinstance(discovered, int) else None
    if status == "no_ir_url":
        sys.stdout.write(f"[{ticker}] SKIPPED - no resolvable IR URL\n")
        return _TickerResult(ticker, TickerStatus.SKIPPED, 0, 0, _elapsed(t0))

    if skip_download:
        sys.stdout.write(f"[{ticker}] OK (discovered={discovered_n}, download skipped)\n")
        return _TickerResult(ticker, TickerStatus.OK, discovered_n, None, _elapsed(t0))

    # --- Stage 2: fetch + categorize ---
    calendar = calendar_id_from_fye(_ticker_fye(db_path, ticker))
    fetch_argv = [
        sys.executable,
        str(PROJECT_ROOT / "execution" / "fetch_ir_documents.py"),
        "--ticker",
        ticker,
        "--categorize",
        "--calendar",
        calendar,
        "--repo-root",
        str(repo_root),
        "--db",
        str(db_path),
    ]
    try:
        fetch = _run_child(fetch_argv, download_timeout)
    except subprocess.TimeoutExpired:
        return _fail(ticker, f"fetch timed out after {download_timeout}s", _elapsed(t0))
    except OSError as exc:
        return _fail(ticker, f"fetch spawn failed: {exc}", _elapsed(t0))
    if fetch.stdout:
        sys.stdout.write(fetch.stdout if fetch.stdout.endswith("\n") else fetch.stdout + "\n")
    if fetch.stderr:
        sys.stderr.write(fetch.stderr)
    if fetch.returncode != 0:
        return _fail(ticker, f"fetch exited {fetch.returncode}", _elapsed(t0))

    downloaded = _json_field(fetch.stdout, "downloaded")
    downloaded_n = downloaded if isinstance(downloaded, int) else None
    elapsed = _elapsed(t0)
    sys.stdout.write(
        f"[{ticker}] OK (discovered={discovered_n}, downloaded={downloaded_n}, {elapsed}s)\n"
    )
    return _TickerResult(ticker, TickerStatus.OK, discovered_n, downloaded_n, elapsed)


def _elapsed(t0: float) -> float:
    return round(time.monotonic() - t0, 3)


def _summarize(
    results: list[_TickerResult], *, skipped_not_in_roster: list[str], elapsed_seconds: float
) -> dict[str, object]:
    return {
        "tickers": [
            {
                "ticker": r.ticker,
                "status": r.status.value,
                "discovered": r.discovered,
                "downloaded": r.downloaded,
                "elapsed_seconds": r.elapsed_seconds,
                "error": r.error,
            }
            for r in results
        ],
        "skipped_not_in_roster": skipped_not_in_roster,
        "ok": sum(1 for r in results if r.status is TickerStatus.OK),
        "skipped": sum(1 for r in results if r.status is TickerStatus.SKIPPED),
        "failed": sum(1 for r in results if r.status is TickerStatus.FAILED),
        "discovered": sum(r.discovered or 0 for r in results),
        "downloaded": sum(r.downloaded or 0 for r in results),
        "elapsed_seconds": elapsed_seconds,
    }


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    repo_root = cast("Path", args.repo_root)
    db_path = cast("Path", args.db) if args.db else repo_root / "data" / "portfolio.db"
    requested = cast("list[str] | None", args.tickers)

    t0 = time.monotonic()
    selected, skipped_not_in_roster = _resolve_roster(db_path, requested)
    for t in skipped_not_in_roster:
        sys.stdout.write(f"[{t}] not in the portfolio/evaluation roster — skipped\n")

    if not selected:
        summary = _summarize(
            [], skipped_not_in_roster=skipped_not_in_roster, elapsed_seconds=_elapsed(t0)
        )
        sys.stdout.write("\n" + json.dumps(summary, indent=2) + "\n")
        sys.stdout.write("No roster tickers to process.\n")
        return 0

    sys.stdout.write(f"IR-document discovery for {len(selected)} ticker(s): {selected}\n")
    results = [
        _run_one(
            t,
            repo_root=repo_root,
            db_path=db_path,
            quarters=cast("int", args.max_quarters),
            discover_timeout=cast("int", args.discover_timeout),
            download_timeout=cast("int", args.download_timeout),
            skip_download=cast("bool", args.skip_download),
        )
        for t in selected
    ]

    summary = _summarize(
        results, skipped_not_in_roster=skipped_not_in_roster, elapsed_seconds=_elapsed(t0)
    )
    sys.stdout.write("\n" + json.dumps(summary, indent=2) + "\n")
    return sum(1 for r in results if r.status is TickerStatus.FAILED)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--tickers", nargs="*", help="Restrict to these (intersected with the roster)")
    p.add_argument("--max-quarters", type=int, default=_DEFAULT_QUARTERS)
    p.add_argument("--discover-timeout", type=int, default=_DEFAULT_DISCOVER_TIMEOUT_S)
    p.add_argument("--download-timeout", type=int, default=_DEFAULT_DOWNLOAD_TIMEOUT_S)
    p.add_argument("--skip-download", action="store_true", help="Discover + write manifests only")
    p.add_argument(
        "--db", type=Path, help="portfolio.db path (default: <repo-root>/data/portfolio.db)"
    )
    p.add_argument("--repo-root", type=Path, default=PROJECT_ROOT)
    return p.parse_args(argv)


if __name__ == "__main__":
    sys.exit(main())
