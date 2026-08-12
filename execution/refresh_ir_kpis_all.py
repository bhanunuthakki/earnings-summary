"""Batch-refresh every configured ticker's IR-spreadsheet KPIs (scheduled entry point).

``execution/refresh_ir_kpis.py`` refreshes ONE ticker from its investor-relations
historical-data spreadsheet (the issuer's own audited KPI series, ingested at
IR_DOC tier so they supersede the LLM brief values). This orchestrator runs that
refresh for every ticker that EITHER has a persisted parser config
(``micro_thesis/ir_config/<T>.json``, via ``ir_pipeline.config.configured_tickers``)
OR has a downloaded IR spreadsheet on disk (``data/ir_spreadsheets/<T>/*.xlsx``)
but no config yet. The first set refreshes via ``--discover`` (re-resolve the
current spreadsheet URL); the second is bootstrapped via ``--file`` so its config
is auto-built from the file on first run. This lifts the configured-only ceiling
(historically NU-only): any ticker with a discoverable IR spreadsheet feeds
kpi_facts, with no manual config step.

It is the scheduled companion to the single-ticker CLI: a weekly Windows
Task-Scheduler cron (``cron/refresh_ir_kpis.task.xml``) invokes it. Configured
tickers' spreadsheets refresh automatically and idempotently — re-ingesting an
unchanged spreadsheet is a no-op (the document is sha256-keyed), so the exact
cadence is not load-bearing; a weekly poll simply catches each new quarter's
file within a week of publication. Tickers WITHOUT a config are skipped (a
ticker's first refresh must run the single-ticker CLI with ``--url``/``--file``
to generate its config — see ``refresh_ir_kpis.py``).

Resilience contract (the load-bearing behavior):

  * Each ticker runs as a subprocess-isolated child (``python
    execution/refresh_ir_kpis.py --ticker <T> --discover``), so a hung headless
    browser or a crash in one ticker's discovery is contained and killable via a
    per-ticker timeout — it cannot take down the batch.
  * The batch NEVER aborts early: a failed or timed-out ticker is logged and the
    remaining tickers still run.
  * Per-ticker outcome is logged — rows ingested on success (parsed from the
    child's JSON stdout) or the failure reason — under a per-ticker header.
  * Exit code = count of failed tickers (0 = all good), reported only AFTER every
    ticker has been attempted. This lets cron / monitoring detect partial failure.

``--discover`` needs the optional ``ir`` extra (a headless browser renders the JS
results-center to resolve the spreadsheet URL): ``pip install -e .[ir] &&
playwright install chromium``. A missing extra surfaces as a per-ticker failure
(the child exits non-zero with the ImportError in its stderr), not a batch crash.

This orchestrates ``refresh_ir_kpis.py`` via subprocess (process isolation,
matching ``run_morning_pipeline.py``) rather than importing its ``main()``; the
child uses the managed interpreter/bootstrap prefix so it never depends on PATH
resolution differing from the parent's.

Usage:
    python execution/refresh_ir_kpis_all.py                 # all configured tickers
    python execution/refresh_ir_kpis_all.py --quarters 12
    python execution/refresh_ir_kpis_all.py --tickers NU     # restrict (intersect w/ configured)
    python execution/refresh_ir_kpis_all.py --timeout 600
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import cast

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from ir_pipeline.config import configured_tickers  # noqa: E402
from pipeline.source_policy import (  # noqa: E402
    SOURCE_POLICY_CONFIG,
    ArtifactKind,
    CollectionSource,
    authorize_stored_collection_target,
)
from runtime.python_process import managed_python_prefix  # noqa: E402

# How many recent quarters each child ingests. Mirrors refresh_ir_kpis.py's own
# default so the batch path and the manual path behave identically.
_DEFAULT_QUARTERS = SOURCE_POLICY_CONFIG.reported_quarter_window.max_quarters

# Per-ticker wall-clock cap. A child renders a headless browser (mz adapter's
# goto uses a 60s networkidle timeout), downloads the spreadsheet (120s urllib
# timeout), then parses + ingests. 5 min is generous headroom for one ticker;
# a hang beyond it is killed and counted as a failure, and the next ticker runs.
_DEFAULT_TIMEOUT_S = 300


class TickerStatus(StrEnum):
    """Outcome of one ticker's refresh."""

    OK = "ok"
    FAILED = "failed"


@dataclass(slots=True, frozen=True)
class _TickerJob:
    """One ticker's refresh plan.

    ``file_path is None`` → refresh via ``--discover`` (a configured ticker
    re-resolves its current spreadsheet URL). ``file_path`` set → bootstrap an
    un-configured ticker from that on-disk spreadsheet via ``--file`` (its parser
    config is auto-built on first run).
    """

    ticker: str
    file_path: Path | None


@dataclass(slots=True)
class _TickerResult:
    """Outcome of running one ticker's refresh child.

    ``exit_code`` is None when the child never produced one (timeout or spawn
    failure); ``error`` carries the human-readable reason in that case.
    ``rows_inserted`` is parsed from the child's JSON stdout on success, or None
    when the child exited 0 but its output could not be parsed.
    """

    ticker: str
    status: TickerStatus
    exit_code: int | None
    rows_inserted: int | None
    elapsed_seconds: float
    error: str | None = None


def _spreadsheet_tickers(repo_root: Path) -> dict[str, Path]:
    """``{TICKER: newest .xlsx}`` for every ticker with a downloaded IR spreadsheet.

    The fetch pipeline drops files under ``data/ir_spreadsheets/<TICKER>/``. A
    ticker with a spreadsheet here but no parser config can still be refreshed —
    its config is auto-built from the file on first run — which is what lifts the
    configured-only (historically NU-only) ceiling. Excel lock files (``~$``) are
    ignored; the newest by mtime wins when a folder holds several quarters.
    """
    base = repo_root / "data" / "ir_spreadsheets"
    if not base.is_dir():
        return {}
    out: dict[str, Path] = {}
    for sub in sorted(base.iterdir()):
        if not sub.is_dir():
            continue
        files = [p for p in sub.glob("*.xlsx") if p.is_file() and not p.name.startswith("~$")]
        if files:
            out[sub.name.upper()] = max(files, key=lambda p: p.stat().st_mtime)
    return out


def _resolve_tickers(
    *, repo_root: Path, db_path: Path, requested: list[str] | None
) -> tuple[list[_TickerJob], list[str]]:
    """Return ``(jobs, skipped)``.

    The refresh universe is every CONFIGURED ticker (refreshed via ``--discover``)
    plus every ticker with a downloaded IR spreadsheet but no config (bootstrapped
    via ``--file``); a configured ticker always discovers even if a file is also on
    disk. ``skipped`` are explicitly-requested tickers in neither set (logged,
    never run). With no ``--tickers`` filter the whole universe runs.
    """
    configured = set(configured_tickers(repo_root))
    on_disk = _spreadsheet_tickers(repo_root)

    def _job(ticker: str) -> _TickerJob:
        # Configured → discover (file_path None); else bootstrap from the on-disk file.
        return _TickerJob(ticker, None if ticker in configured else on_disk.get(ticker))

    universe = configured | set(on_disk)
    candidates = sorted({t.upper() for t in requested}) if requested else sorted(universe)
    selected: list[str] = []
    skipped: list[str] = []
    for ticker in candidates:
        authorization = authorize_stored_collection_target(
            db_path,
            ticker,
            requested=requested is not None,
            source=CollectionSource.IR,
            artifact_kind=ArtifactKind.IR_DOCUMENT,
        )
        if not authorization.allowed or ticker not in universe:
            skipped.append(ticker)
            continue
        selected.append(ticker)
    return [_job(t) for t in selected], skipped


def _rows_from_stdout(stdout: str) -> int | None:
    """Pull ``rows_inserted`` from the child's JSON stdout; None if unparseable.

    The child prints exactly one ``json.dumps(..., indent=2)`` object on success,
    so the slice from the first ``{`` to the last ``}`` is that object.
    """
    s = stdout.strip()
    start, end = s.find("{"), s.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        obj = json.loads(s[start : end + 1])
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict):
        return None
    val = cast("dict[str, object]", obj).get("rows_inserted")
    return val if isinstance(val, int) else None


def _fail(
    ticker: str,
    reason: str,
    elapsed: float,
    *,
    exit_code: int | None = None,
) -> _TickerResult:
    """Emit a prominent failure banner (stderr) + a status line (stdout)."""
    sys.stderr.write(f"\n!!! [{ticker}] FAILED - {reason}\n")
    sys.stdout.write(f"[{ticker}] FAILED - {reason} ({elapsed}s)\n")
    return _TickerResult(
        ticker=ticker,
        status=TickerStatus.FAILED,
        exit_code=exit_code,
        rows_inserted=None,
        elapsed_seconds=elapsed,
        error=reason,
    )


def _run_one(
    job: _TickerJob,
    *,
    repo_root: Path,
    db_path: Path,
    quarters: int,
    timeout_s: int,
    owner_requested: bool,
) -> _TickerResult:
    """Refresh one ticker as a subprocess-isolated child; echo its output.

    A configured ticker (``job.file_path is None``) refreshes via ``--discover``;
    an un-configured ticker with an on-disk spreadsheet is bootstrapped via
    ``--file`` so its config is auto-built. Never raises. ``subprocess.run`` uses
    ``check=False`` so a non-zero child exit returns a CompletedProcess rather than
    raising; the only runtime exceptions left are ``TimeoutExpired`` and ``OSError``
    (spawn failure), both caught and turned into a failed ``_TickerResult``. This is
    what guarantees the caller's loop never aborts early.
    """
    ticker = job.ticker
    argv = [
        *managed_python_prefix(PROJECT_ROOT),
        str(PROJECT_ROOT / "execution" / "refresh_ir_kpis.py"),
        "--ticker",
        ticker,
    ]
    if job.file_path is not None:
        argv += ["--file", str(job.file_path)]
    else:
        argv += ["--discover"]
    argv += [
        "--quarters",
        str(quarters),
        "--repo-root",
        str(repo_root),
        "--db",
        str(db_path),
    ]
    if owner_requested:
        argv.append("--owner-requested")
    sys.stdout.write(f"\n{'=' * 72}\n=== IR-spreadsheet refresh - {ticker}\n{'=' * 72}\n")
    sys.stdout.flush()

    t0 = time.monotonic()
    try:
        proc = subprocess.run(
            argv,
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return _fail(ticker, f"timed out after {timeout_s}s", round(time.monotonic() - t0, 3))
    except OSError as exc:
        return _fail(ticker, f"spawn failed: {exc}", round(time.monotonic() - t0, 3))

    elapsed = round(time.monotonic() - t0, 3)
    if proc.stdout:
        sys.stdout.write(proc.stdout)
        if not proc.stdout.endswith("\n"):
            sys.stdout.write("\n")
    if proc.stderr:
        sys.stderr.write(proc.stderr)

    if proc.returncode == 0:
        rows = _rows_from_stdout(proc.stdout)
        shown = rows if rows is not None else "?"
        sys.stdout.write(f"[{ticker}] OK (rows_inserted={shown}, exit 0, {elapsed}s)\n")
        return _TickerResult(
            ticker=ticker,
            status=TickerStatus.OK,
            exit_code=0,
            rows_inserted=rows,
            elapsed_seconds=elapsed,
        )
    return _fail(ticker, f"exited {proc.returncode}", elapsed, exit_code=proc.returncode)


def _summarize(
    results: list[_TickerResult], *, skipped: list[str], elapsed_seconds: float
) -> dict[str, object]:
    """Build the final summary dict: per-ticker outcomes + rollup counts."""
    return {
        "tickers": [
            {
                "ticker": r.ticker,
                "status": r.status.value,
                "rows_inserted": r.rows_inserted,
                "exit_code": r.exit_code,
                "elapsed_seconds": r.elapsed_seconds,
                "error": r.error,
            }
            for r in results
        ],
        "skipped_no_config": skipped,
        "ok": sum(1 for r in results if r.status is TickerStatus.OK),
        "failed": sum(1 for r in results if r.status is TickerStatus.FAILED),
        "rows_inserted": sum(r.rows_inserted or 0 for r in results),
        "elapsed_seconds": elapsed_seconds,
    }


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--quarters",
        type=int,
        default=_DEFAULT_QUARTERS,
        help=f"Recent quarters each ticker ingests (default {_DEFAULT_QUARTERS}). "
        f"Forwarded to refresh_ir_kpis.py --quarters.",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=_DEFAULT_TIMEOUT_S,
        help=f"Per-ticker subprocess wall-clock cap in seconds "
        f"(default {_DEFAULT_TIMEOUT_S}). A ticker exceeding it is killed and "
        f"counted as failed; the batch continues.",
    )
    parser.add_argument(
        "--tickers",
        nargs="*",
        help="Restrict to these tickers (intersected with the refresh universe = "
        "configured tickers + tickers with an on-disk IR spreadsheet; a requested "
        "ticker in neither is skipped, not run). Default: the whole universe.",
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=PROJECT_ROOT,
        help="Repo root holding micro_thesis/ir_config + data/portfolio.db "
        "(forwarded to each child as --repo-root). Default: this repo.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    repo_root = cast("Path", args.repo_root)
    quarters = cast("int", args.quarters)
    timeout_s = cast("int", args.timeout)
    requested = cast("list[str] | None", args.tickers)
    bound = SOURCE_POLICY_CONFIG.reported_quarter_window.max_quarters
    if quarters < 1 or quarters > bound:
        sys.stderr.write(f"--quarters must be between 1 and {bound}\n")
        return 2
    db_path = repo_root / "data" / "portfolio.db"

    t0 = time.monotonic()
    jobs, skipped = _resolve_tickers(
        repo_root=repo_root,
        db_path=db_path,
        requested=requested,
    )
    for t in skipped:
        sys.stdout.write(
            f"[{t}] SKIPPED - no IR config (micro_thesis/ir_config/{t}.json) and no "
            f"spreadsheet on disk (data/ir_spreadsheets/{t}/); run refresh_ir_kpis.py "
            "--url/--file to bootstrap one\n"
        )

    if not jobs:
        summary = _summarize([], skipped=skipped, elapsed_seconds=round(time.monotonic() - t0, 3))
        sys.stdout.write("\n" + json.dumps(summary, indent=2) + "\n")
        sys.stdout.write("No tickers to refresh.\n")
        return 0

    selected = [j.ticker for j in jobs]
    sys.stdout.write(f"Refreshing IR-spreadsheet KPIs for {len(jobs)} ticker(s): {selected}\n")

    # Always-attempt contract: iterate every selected ticker with no early return.
    # _run_one never raises, so a failure / timeout in one ticker cannot stop the
    # next from running.
    results = [
        _run_one(
            j,
            repo_root=repo_root,
            db_path=db_path,
            quarters=quarters,
            timeout_s=timeout_s,
            owner_requested=requested is not None,
        )
        for j in jobs
    ]

    summary = _summarize(results, skipped=skipped, elapsed_seconds=round(time.monotonic() - t0, 3))
    sys.stdout.write("\n" + json.dumps(summary, indent=2) + "\n")

    # Exit code = number of failed tickers, reported only after all ran.
    return sum(1 for r in results if r.status is TickerStatus.FAILED)


if __name__ == "__main__":
    sys.exit(main())
