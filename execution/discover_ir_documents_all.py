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
import concurrent.futures
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

import ir_fetch_status  # noqa: E402
from ir_uploads import calendar_id_from_fye  # noqa: E402
from log_redact import redact  # noqa: E402
from runtime.python_process import ensure_managed_python_argv, managed_python_prefix  # noqa: E402
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite  # noqa: E402

# Roster = the "briefed" active universe (portfolio + evaluation). Hardcoded to
# match db.BRIEFED_LIST_TYPES (the import is avoided to keep this orchestrator
# light + test-isolated, like execution/run_triggers.py).
_ROSTER_LIST_TYPES_SQL = "('portfolio', 'evaluation')"
_DEFAULT_QUARTERS = 8
_DEFAULT_DISCOVER_TIMEOUT_S = 300  # headless browser render
# Download + content-classify + register. Generous on purpose: deep-history names (AMZN, V,
# WIX) have 50-100 docs to pull, and the old 300s timed them out mid-download → 0 registered
# (AMZN recovered 0->49 once given room). A light or bot-blocked name returns fast (nothing
# to download), so a high cap is harmless — it only bites when there is genuinely a lot to
# fetch. The automated full-sweep + failing-rescan crons inherit this default, so AMZN-class
# names now self-heal instead of timing out on every scheduled run.
_DEFAULT_DOWNLOAD_TIMEOUT_S = 420
_DEFAULT_PROCESS_TIMEOUT_S = 300
_DEFAULT_DISCOVERY_WORKERS = 3
_DEFAULT_TICKER_DEADLINE_S = 600
_DEFAULT_WHOLE_RUN_DEADLINE_S = 1800
_CHECKPOINT_VERSION = 1


class TickerStatus(StrEnum):
    OK = "ok"
    FAILED = "failed"
    SKIPPED = "skipped"  # no resolvable IR URL — best-effort, not a failure


@dataclass(slots=True)
class TickerResult:
    ticker: str
    status: TickerStatus
    discovered: int | None
    downloaded: int | None
    elapsed_seconds: float
    error: str | None = None
    processed: bool = False  # post-registration stage ran (anchor + summaries + brief_dirty)


@dataclass(slots=True)
class DiscoveryResult:
    """Ticker-scoped crawl result handed to the serialized mutation phase."""

    ticker: str
    status: TickerStatus
    discovered: int | None
    elapsed_seconds: float
    stdout: str = ""
    stderr: str = ""
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
            conn = connect_sqlite(str(db_path), role=SQLiteConnectionRole.READ_ONLY)
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
        conn = connect_sqlite(str(db_path), role=SQLiteConnectionRole.READ_ONLY)
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


def _set_brief_dirty(db_path: Path, ticker: str) -> bool:
    """Flag the ticker for the daily ``--enable-llm`` rebuild (best-effort)."""
    if not db_path.exists():
        return False
    try:
        conn = connect_sqlite(str(db_path), role=SQLiteConnectionRole.WRITER, schema_preflight=True)
        try:
            conn.execute(
                "UPDATE tracked_companies SET brief_dirty = 1 WHERE ticker = ?",
                (ticker.upper(),),
            )
            conn.commit()
        finally:
            conn.close()
    except sqlite3.Error:
        return False
    return True


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


def _fail(
    ticker: str,
    reason: str,
    elapsed: float,
    *,
    discovered: int | None = None,
    downloaded: int | None = None,
) -> TickerResult:
    sys.stderr.write(f"\n!!! [{ticker}] FAILED - {reason}\n")
    sys.stdout.write(f"[{ticker}] FAILED - {reason} ({elapsed}s)\n")
    return TickerResult(
        ticker,
        TickerStatus.FAILED,
        discovered,
        downloaded,
        elapsed,
        error=reason,
    )


def _run_child(argv: list[str], timeout_s: float) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ensure_managed_python_argv(PROJECT_ROOT, argv),
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout_s,
        check=False,
    )


def _run_tolerant(argv: list[str], timeout_s: float, label: str) -> bool:
    """Run a best-effort sub-step; echo output, log+swallow any failure. Returns ok."""
    try:
        proc = _run_child(argv, timeout_s)
    except (subprocess.TimeoutExpired, OSError) as exc:
        sys.stderr.write(f"{label} FAILED: {redact(exc)} (tolerated)\n")
        return False
    if proc.stdout:
        safe_stdout = redact(proc.stdout)
        sys.stdout.write(safe_stdout if safe_stdout.endswith("\n") else safe_stdout + "\n")
    if proc.stderr:
        sys.stderr.write(redact(proc.stderr))
    if proc.returncode != 0:
        sys.stderr.write(f"{label} rc={proc.returncode} (tolerated)\n")
        return False
    return True


def _run_process_stage(
    ticker: str,
    *,
    repo_root: Path,
    db_path: Path,
    summaries: bool,
    timeout_s: float,
    deadline_at: float | None = None,
) -> bool:
    """Feed newly-registered docs into the LLM pipeline. Best-effort; never raises.

    Always: ``ir_narrative.py`` (cheap pypdf extraction → ``data/ir_narrative/<T>/``,
    the IR anchor every ``--enable-llm`` prompt reads) + flip ``brief_dirty`` so the
    daily brief rebuild surfaces the docs. With ``summaries``: also
    ``process_ir_documents.py`` (LLM per-doc summaries feeding the press-release /
    transcript sections — the cost step, opt-in).
    """
    first_timeout = _remaining_timeout(timeout_s, deadline_at)
    if first_timeout is None:
        sys.stderr.write(f"[{ticker}] process deadline exhausted before ir_narrative\n")
        return False
    nar = _run_tolerant(
        [
            *managed_python_prefix(PROJECT_ROOT),
            str(PROJECT_ROOT / "src" / "compute" / "ir_narrative.py"),
            "--ticker",
            ticker,
            "--repo-root",
            str(repo_root),
        ],
        first_timeout,
        f"[{ticker}] ir_narrative",
    )
    if summaries:
        summary_timeout = _remaining_timeout(timeout_s, deadline_at)
        if summary_timeout is None:
            sys.stderr.write(f"[{ticker}] process deadline exhausted before summaries\n")
            return False
        _run_tolerant(
            [
                *managed_python_prefix(PROJECT_ROOT),
                str(PROJECT_ROOT / "execution" / "process_ir_documents.py"),
                "--ticker",
                ticker,
            ],
            summary_timeout,
            f"[{ticker}] process_ir_documents",
        )
    dirty = _set_brief_dirty(db_path, ticker)
    return nar and dirty


def _remaining_timeout(stage_timeout_s: float, deadline_at: float | None) -> float | None:
    """Return a positive timeout bounded by an optional monotonic deadline."""
    if deadline_at is None:
        return stage_timeout_s
    remaining = deadline_at - time.monotonic()
    if remaining <= 0:
        return None
    return min(stage_timeout_s, remaining)


def _run_discovery(
    ticker: str,
    *,
    repo_root: Path,
    db_path: Path,
    quarters: int,
    timeout_s: float,
) -> DiscoveryResult:
    """Run the network-heavy crawl without touching shared database state.

    The child writes only ``.tmp/ir_url_manifest/<ticker>_urls.json``. Those
    ticker-scoped paths have disjoint ownership, so they may be produced in
    parallel. Registration, canonical document writes, and status persistence
    remain serialized in the parent process.
    """
    t0 = time.monotonic()
    discover_argv = [
        *managed_python_prefix(PROJECT_ROOT),
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
        disc = _run_child(discover_argv, timeout_s)
    except subprocess.TimeoutExpired:
        return DiscoveryResult(
            ticker,
            TickerStatus.FAILED,
            None,
            _elapsed(t0),
            error=f"discover timed out after {round(timeout_s, 3)}s",
        )
    except OSError as exc:
        return DiscoveryResult(
            ticker,
            TickerStatus.FAILED,
            None,
            _elapsed(t0),
            error=f"discover spawn failed: {redact(exc)}",
        )

    status = _json_field(disc.stdout, "status")
    discovered = _json_field(disc.stdout, "discovered")
    discovered_n = discovered if isinstance(discovered, int) else None
    if disc.returncode != 0:
        return DiscoveryResult(
            ticker,
            TickerStatus.FAILED,
            discovered_n,
            _elapsed(t0),
            stdout=disc.stdout,
            stderr=disc.stderr,
            error=f"discover exited {disc.returncode}",
        )
    if status == "no_ir_url":
        return DiscoveryResult(
            ticker,
            TickerStatus.SKIPPED,
            0,
            _elapsed(t0),
            stdout=disc.stdout,
            stderr=disc.stderr,
        )
    return DiscoveryResult(
        ticker,
        TickerStatus.OK,
        discovered_n,
        _elapsed(t0),
        stdout=disc.stdout,
        stderr=disc.stderr,
    )


def _emit_discovery_output(discovery: DiscoveryResult) -> None:
    sys.stdout.write(f"\n{'=' * 72}\n=== IR-document discovery - {discovery.ticker}\n{'=' * 72}\n")
    if discovery.stdout:
        safe_stdout = redact(discovery.stdout)
        sys.stdout.write(safe_stdout if safe_stdout.endswith("\n") else safe_stdout + "\n")
    if discovery.stderr:
        sys.stderr.write(redact(discovery.stderr))


def _finish_discovery(
    discovery: DiscoveryResult,
    *,
    repo_root: Path,
    db_path: Path,
    skip_download: bool,
    process: bool,
    summaries: bool,
    download_timeout: float,
    process_timeout: float,
    ticker_deadline: float,
    whole_deadline_at: float | None,
) -> TickerResult:
    """Serialize fetch/register/process for one completed discovery result."""
    ticker = discovery.ticker
    _emit_discovery_output(discovery)
    if discovery.status is TickerStatus.FAILED:
        return _fail(
            ticker,
            discovery.error or "discover failed",
            discovery.elapsed_seconds,
            discovered=discovery.discovered,
        )
    if discovery.status is TickerStatus.SKIPPED:
        sys.stdout.write(f"[{ticker}] SKIPPED - no resolvable IR URL\n")
        return TickerResult(
            ticker,
            TickerStatus.SKIPPED,
            0,
            0,
            discovery.elapsed_seconds,
        )
    if skip_download:
        sys.stdout.write(f"[{ticker}] OK (discovered={discovery.discovered}, download skipped)\n")
        return TickerResult(
            ticker,
            TickerStatus.OK,
            discovery.discovered,
            None,
            discovery.elapsed_seconds,
        )

    active_remaining = ticker_deadline - discovery.elapsed_seconds
    if active_remaining <= 0:
        return _fail(
            ticker,
            f"per-ticker deadline exhausted after discovery ({ticker_deadline}s)",
            discovery.elapsed_seconds,
            discovered=discovery.discovered,
        )
    ticker_deadline_at = time.monotonic() + active_remaining
    effective_deadline = ticker_deadline_at
    if whole_deadline_at is not None:
        effective_deadline = min(effective_deadline, whole_deadline_at)
    fetch_timeout = _remaining_timeout(download_timeout, effective_deadline)
    if fetch_timeout is None:
        return _fail(
            ticker,
            "whole-run deadline exhausted before fetch",
            discovery.elapsed_seconds,
            discovered=discovery.discovered,
        )

    calendar = calendar_id_from_fye(_ticker_fye(db_path, ticker))
    fetch_argv = [
        *managed_python_prefix(PROJECT_ROOT),
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
    finish_t0 = time.monotonic()
    try:
        fetch = _run_child(fetch_argv, fetch_timeout)
    except subprocess.TimeoutExpired:
        return _fail(
            ticker,
            f"fetch timed out after {round(fetch_timeout, 3)}s (bounded deadline)",
            round(discovery.elapsed_seconds + _elapsed(finish_t0), 3),
            discovered=discovery.discovered,
        )
    except OSError as exc:
        return _fail(
            ticker,
            f"fetch spawn failed: {redact(exc)}",
            round(discovery.elapsed_seconds + _elapsed(finish_t0), 3),
            discovered=discovery.discovered,
        )
    if fetch.stdout:
        safe_stdout = redact(fetch.stdout)
        sys.stdout.write(safe_stdout if safe_stdout.endswith("\n") else safe_stdout + "\n")
    if fetch.stderr:
        sys.stderr.write(redact(fetch.stderr))
    if fetch.returncode != 0:
        return _fail(
            ticker,
            f"fetch exited {fetch.returncode}",
            round(discovery.elapsed_seconds + _elapsed(finish_t0), 3),
            discovered=discovery.discovered,
        )

    downloaded = _json_field(fetch.stdout, "downloaded")
    downloaded_n = downloaded if isinstance(downloaded, int) else None
    processed = False
    if process and downloaded_n:
        process_budget = _remaining_timeout(process_timeout, effective_deadline)
        if process_budget is not None:
            processed = _run_process_stage(
                ticker,
                repo_root=repo_root,
                db_path=db_path,
                summaries=summaries,
                timeout_s=process_budget,
                deadline_at=effective_deadline,
            )
        else:
            sys.stderr.write(f"[{ticker}] process skipped: bounded deadline exhausted\n")

    elapsed = round(discovery.elapsed_seconds + _elapsed(finish_t0), 3)
    sys.stdout.write(
        f"[{ticker}] OK (discovered={discovery.discovered}, downloaded={downloaded_n}, "
        f"processed={processed}, {elapsed}s)\n"
    )
    return TickerResult(
        ticker,
        TickerStatus.OK,
        discovery.discovered,
        downloaded_n,
        elapsed,
        processed=processed,
    )


def _run_one(
    ticker: str,
    *,
    repo_root: Path,
    db_path: Path,
    quarters: int,
    discover_timeout: float,
    download_timeout: float,
    skip_download: bool,
    process: bool = True,
    summaries: bool = False,
    process_timeout: float = _DEFAULT_PROCESS_TIMEOUT_S,
    ticker_deadline: float = _DEFAULT_TICKER_DEADLINE_S,
) -> TickerResult:
    """Discover then fetch one ticker, subprocess-isolated. Never raises."""
    discovery = _run_discovery(
        ticker,
        repo_root=repo_root,
        db_path=db_path,
        quarters=quarters,
        timeout_s=min(discover_timeout, ticker_deadline),
    )
    return _finish_discovery(
        discovery,
        repo_root=repo_root,
        db_path=db_path,
        skip_download=skip_download,
        process=process,
        summaries=summaries,
        download_timeout=download_timeout,
        process_timeout=process_timeout,
        ticker_deadline=ticker_deadline,
        whole_deadline_at=None,
    )


def _elapsed(t0: float) -> float:
    return round(time.monotonic() - t0, 3)


def _summarize(
    results: list[TickerResult], *, skipped_not_in_roster: list[str], elapsed_seconds: float
) -> dict[str, object]:
    return {
        "tickers": [
            {
                "ticker": r.ticker,
                "status": r.status.value,
                "discovered": r.discovered,
                "downloaded": r.downloaded,
                "processed": r.processed,
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
        "processed": sum(1 for r in results if r.processed),
        "elapsed_seconds": elapsed_seconds,
    }


def _reason_for(result: TickerResult) -> str | None:
    """Human-readable why-this-name-has-no-docs, for the status log + dashboard."""
    if result.status is TickerStatus.FAILED:
        return result.error
    if result.status is TickerStatus.SKIPPED:
        return "no resolvable IR URL"
    if result.status is TickerStatus.OK and not result.downloaded:
        return "crawl found no new documents (site may be bot-protected)"
    return None


def run_ticker(
    ticker: str,
    *,
    repo_root: Path,
    db_path: Path,
    quarters: int = _DEFAULT_QUARTERS,
    discover_timeout: float = _DEFAULT_DISCOVER_TIMEOUT_S,
    download_timeout: float = _DEFAULT_DOWNLOAD_TIMEOUT_S,
    skip_download: bool = False,
    process: bool = True,
    summaries: bool = False,
    process_timeout: float = _DEFAULT_PROCESS_TIMEOUT_S,
    ticker_deadline: float = _DEFAULT_TICKER_DEADLINE_S,
    record_status: bool = True,
) -> TickerResult:
    """Full single-ticker IR chain (discover → fetch+register → process), then
    persist the crawl outcome to ``ir_fetch_status``.

    The public entry reused by the batch loop AND by onboarding — so a freshly
    added name is crawled, registered, queued for ``--enable-llm`` (brief_dirty),
    and recorded on day one without waiting for the weekly roster sweep. Never
    raises (wraps the subprocess-isolated ``_run_one``); status bookkeeping is
    best-effort.
    """
    result = _run_one(
        ticker,
        repo_root=repo_root,
        db_path=db_path,
        quarters=quarters,
        discover_timeout=discover_timeout,
        download_timeout=download_timeout,
        skip_download=skip_download,
        process=process,
        summaries=summaries,
        process_timeout=process_timeout,
        ticker_deadline=ticker_deadline,
    )
    if record_status:
        ir_fetch_status.record_attempt(
            db_path,
            ticker,
            status=result.status.value,
            discovered=result.discovered,
            downloaded=result.downloaded,
            reason=_reason_for(result),
        )
    return result


def _discovery_to_checkpoint(result: DiscoveryResult) -> dict[str, object]:
    """Serialize only bounded metadata; never persist child output or URLs."""
    return {
        "ticker": result.ticker,
        "status": result.status.value,
        "discovered": result.discovered,
        "elapsed_seconds": result.elapsed_seconds,
        "error": result.error,
    }


def _discovery_from_checkpoint(value: object) -> DiscoveryResult | None:
    if not isinstance(value, dict):
        return None
    row = cast("dict[str, object]", value)
    ticker = row.get("ticker")
    status = row.get("status")
    elapsed = row.get("elapsed_seconds")
    if not isinstance(ticker, str) or not isinstance(status, str):
        return None
    if not isinstance(elapsed, int | float):
        return None
    try:
        parsed_status = TickerStatus(status)
    except ValueError:
        return None
    discovered = row.get("discovered")
    error = row.get("error")
    return DiscoveryResult(
        ticker=ticker,
        status=parsed_status,
        discovered=discovered if isinstance(discovered, int) else None,
        elapsed_seconds=float(elapsed),
        error=error if isinstance(error, str) else None,
    )


def _result_to_checkpoint(result: TickerResult) -> dict[str, object]:
    return {
        "ticker": result.ticker,
        "status": result.status.value,
        "discovered": result.discovered,
        "downloaded": result.downloaded,
        "elapsed_seconds": result.elapsed_seconds,
        "error": result.error,
        "processed": result.processed,
    }


def _result_from_checkpoint(value: object) -> TickerResult | None:
    if not isinstance(value, dict):
        return None
    row = cast("dict[str, object]", value)
    ticker = row.get("ticker")
    status = row.get("status")
    elapsed = row.get("elapsed_seconds")
    if not isinstance(ticker, str) or not isinstance(status, str):
        return None
    if not isinstance(elapsed, int | float):
        return None
    try:
        parsed_status = TickerStatus(status)
    except ValueError:
        return None
    discovered = row.get("discovered")
    downloaded = row.get("downloaded")
    error = row.get("error")
    return TickerResult(
        ticker=ticker,
        status=parsed_status,
        discovered=discovered if isinstance(discovered, int) else None,
        downloaded=downloaded if isinstance(downloaded, int) else None,
        elapsed_seconds=float(elapsed),
        error=error if isinstance(error, str) else None,
        processed=row.get("processed") is True,
    )


def _load_checkpoint(
    path: Path, signature: dict[str, object]
) -> tuple[dict[str, DiscoveryResult], dict[str, TickerResult]]:
    if not path.exists():
        return {}, {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        sys.stderr.write(f"IR checkpoint ignored: {redact(exc)}\n")
        return {}, {}
    if not isinstance(raw, dict):
        return {}, {}
    state = cast("dict[str, object]", raw)
    if state.get("version") != _CHECKPOINT_VERSION or state.get("signature") != signature:
        sys.stderr.write("IR checkpoint ignored: run parameters changed\n")
        return {}, {}
    discoveries: dict[str, DiscoveryResult] = {}
    raw_discoveries = state.get("discoveries")
    if isinstance(raw_discoveries, dict):
        for ticker, value in cast("dict[str, object]", raw_discoveries).items():
            parsed = _discovery_from_checkpoint(value)
            if (
                parsed is not None
                and parsed.ticker == ticker
                and parsed.status is not TickerStatus.FAILED
            ):
                discoveries[ticker] = parsed
    results: dict[str, TickerResult] = {}
    raw_results = state.get("results")
    if isinstance(raw_results, dict):
        for ticker, value in cast("dict[str, object]", raw_results).items():
            parsed = _result_from_checkpoint(value)
            if parsed is not None and parsed.ticker == ticker:
                results[ticker] = parsed
    return discoveries, results


def _save_checkpoint(
    path: Path,
    signature: dict[str, object],
    discoveries: dict[str, DiscoveryResult],
    results: dict[str, TickerResult],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": _CHECKPOINT_VERSION,
        "signature": signature,
        "discoveries": {
            ticker: _discovery_to_checkpoint(discoveries[ticker]) for ticker in sorted(discoveries)
        },
        "results": {ticker: _result_to_checkpoint(results[ticker]) for ticker in sorted(results)},
    }
    pending = path.with_suffix(path.suffix + ".pending")
    pending.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    pending.replace(path)


def _record_result(db_path: Path, result: TickerResult) -> None:
    ir_fetch_status.record_attempt(
        db_path,
        result.ticker,
        status=result.status.value,
        discovered=result.discovered,
        downloaded=result.downloaded,
        reason=_reason_for(result),
    )


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    repo_root = cast("Path", args.repo_root)
    db_path = cast("Path", args.db) if args.db else repo_root / "data" / "portfolio.db"
    requested = cast("list[str] | None", args.tickers)

    t0 = time.monotonic()
    selected, skipped_not_in_roster = _resolve_roster(db_path, requested)
    for t in skipped_not_in_roster:
        sys.stdout.write(f"[{t}] not in the portfolio/evaluation roster — skipped\n")

    if cast("bool", args.only_failing):
        gaps = set(ir_fetch_status.gap_tickers(db_path, selected))
        dropped = [t for t in selected if t not in gaps]
        selected = [t for t in selected if t in gaps]
        if dropped:
            sys.stdout.write(
                f"--only-failing: {len(dropped)} name(s) already have IR docs, "
                f"rescanning {len(selected)} gap(s): {selected}\n"
            )

    if not selected:
        summary = _summarize(
            [], skipped_not_in_roster=skipped_not_in_roster, elapsed_seconds=_elapsed(t0)
        )
        sys.stdout.write("\n" + json.dumps(summary, indent=2) + "\n")
        sys.stdout.write("No roster tickers to process.\n")
        return 0

    sys.stdout.write(f"IR-document discovery for {len(selected)} ticker(s): {selected}\n")
    whole_deadline_at = t0 + cast("float", args.whole_run_deadline)
    checkpoint_path = (
        cast("Path", args.checkpoint)
        if args.checkpoint
        else repo_root / ".tmp" / "ir_document_discovery_all" / "state.json"
    )
    signature: dict[str, object] = {
        "selected": selected,
        "quarters": cast("int", args.max_quarters),
        "discover_timeout": cast("float", args.discover_timeout),
        "download_timeout": cast("float", args.download_timeout),
        "process_timeout": cast("float", args.process_timeout),
        "per_ticker_deadline": cast("float", args.per_ticker_deadline),
        "skip_download": cast("bool", args.skip_download),
        "no_process": cast("bool", args.no_process),
        "summaries": cast("bool", args.summaries),
        "db_path": str(db_path.resolve()),
    }
    discoveries: dict[str, DiscoveryResult] = {}
    completed: dict[str, TickerResult] = {}
    if not cast("bool", args.no_resume):
        discoveries, completed = _load_checkpoint(checkpoint_path, signature)
        discoveries = {t: r for t, r in discoveries.items() if t in selected}
        completed = {t: r for t, r in completed.items() if t in selected}
        if discoveries or completed:
            sys.stdout.write(
                f"Resuming checkpoint: {len(discoveries)} crawled, "
                f"{len(completed)} fully processed\n"
            )

    pending_discovery = [t for t in selected if t not in discoveries and t not in completed]
    deadline_hit = False
    if pending_discovery:
        workers = min(cast("int", args.discovery_workers), len(pending_discovery))
        executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=workers,
            thread_name_prefix="ir-discovery",
        )
        futures = {
            executor.submit(
                _run_discovery,
                ticker,
                repo_root=repo_root,
                db_path=db_path,
                quarters=cast("int", args.max_quarters),
                timeout_s=min(
                    cast("float", args.discover_timeout),
                    cast("float", args.per_ticker_deadline),
                    max(0.001, whole_deadline_at - time.monotonic()),
                ),
            ): ticker
            for ticker in pending_discovery
        }
        try:
            timeout = max(0.001, whole_deadline_at - time.monotonic())
            for future in concurrent.futures.as_completed(futures, timeout=timeout):
                discovery = future.result()
                discoveries[discovery.ticker] = discovery
                _save_checkpoint(checkpoint_path, signature, discoveries, completed)
        except TimeoutError:
            deadline_hit = True
            sys.stderr.write("Whole-run deadline reached during discovery; pending work canceled\n")
            for future in futures:
                future.cancel()
        finally:
            executor.shutdown(wait=True, cancel_futures=True)

    results_by_ticker = dict(completed)
    for ticker in selected:
        if ticker in results_by_ticker:
            sys.stdout.write(f"[{ticker}] resumed from checkpoint\n")
            continue
        discovery = discoveries.get(ticker)
        if discovery is None:
            results_by_ticker[ticker] = TickerResult(
                ticker,
                TickerStatus.FAILED,
                None,
                None,
                _elapsed(t0),
                error="whole-run deadline canceled discovery",
            )
            deadline_hit = True
            continue
        if time.monotonic() >= whole_deadline_at:
            results_by_ticker[ticker] = TickerResult(
                ticker,
                TickerStatus.FAILED,
                discovery.discovered,
                None,
                _elapsed(t0),
                error="whole-run deadline exhausted before serialized mutation",
            )
            deadline_hit = True
            continue
        result = _finish_discovery(
            discovery,
            repo_root=repo_root,
            db_path=db_path,
            skip_download=cast("bool", args.skip_download),
            process=not cast("bool", args.no_process),
            summaries=cast("bool", args.summaries),
            download_timeout=cast("float", args.download_timeout),
            process_timeout=cast("float", args.process_timeout),
            ticker_deadline=cast("float", args.per_ticker_deadline),
            whole_deadline_at=whole_deadline_at,
        )
        results_by_ticker[ticker] = result
        _record_result(db_path, result)
        if result.status is TickerStatus.FAILED:
            if result.error and "deadline" in result.error:
                deadline_hit = True
            continue
        completed[ticker] = result
        _save_checkpoint(checkpoint_path, signature, discoveries, completed)

    results = [results_by_ticker[t] for t in selected]
    if not deadline_hit and len(completed) == len(selected):
        checkpoint_path.unlink(missing_ok=True)
    else:
        _save_checkpoint(checkpoint_path, signature, discoveries, completed)

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
    p.add_argument("--discover-timeout", type=float, default=_DEFAULT_DISCOVER_TIMEOUT_S)
    p.add_argument("--download-timeout", type=float, default=_DEFAULT_DOWNLOAD_TIMEOUT_S)
    p.add_argument(
        "--discovery-workers",
        type=int,
        default=_DEFAULT_DISCOVERY_WORKERS,
        help="Concurrent ticker-scoped discovery children (default: %(default)s)",
    )
    p.add_argument(
        "--per-ticker-deadline",
        type=float,
        default=_DEFAULT_TICKER_DEADLINE_S,
        help="Maximum active seconds across a ticker's discover/fetch/process chain",
    )
    p.add_argument(
        "--whole-run-deadline",
        type=float,
        default=_DEFAULT_WHOLE_RUN_DEADLINE_S,
        help="Maximum wall-clock seconds for the entire batch",
    )
    p.add_argument("--skip-download", action="store_true", help="Discover + write manifests only")
    p.add_argument(
        "--only-failing",
        action="store_true",
        help=(
            "Rescan only roster names with ZERO registered IR docs (the periodic "
            "retry of bot-protected / failed crawlers — see the failing-crawler cron)"
        ),
    )
    p.add_argument(
        "--no-process",
        action="store_true",
        help="Skip the post-registration stage (no ir_narrative anchor refresh / brief_dirty)",
    )
    p.add_argument(
        "--summaries",
        action="store_true",
        help="Also run process_ir_documents.py (LLM per-doc summaries — the cost step)",
    )
    p.add_argument(
        "--process-timeout",
        type=float,
        default=_DEFAULT_PROCESS_TIMEOUT_S,
        help="Per-step timeout for the process stage (s)",
    )
    p.add_argument(
        "--checkpoint",
        type=Path,
        help="Resume checkpoint (default: <repo-root>/.tmp/ir_document_discovery_all/state.json)",
    )
    p.add_argument(
        "--no-resume",
        action="store_true",
        help="Ignore an existing compatible checkpoint and start a fresh batch",
    )
    p.add_argument(
        "--db", type=Path, help="portfolio.db path (default: <repo-root>/data/portfolio.db)"
    )
    p.add_argument("--repo-root", type=Path, default=PROJECT_ROOT)
    args = p.parse_args(argv)
    positive_fields = (
        "discover_timeout",
        "download_timeout",
        "discovery_workers",
        "per_ticker_deadline",
        "whole_run_deadline",
        "process_timeout",
    )
    for field in positive_fields:
        if cast("float", getattr(args, field)) <= 0:
            p.error(f"--{field.replace('_', '-')} must be greater than zero")
    return args


if __name__ == "__main__":
    sys.exit(main())
