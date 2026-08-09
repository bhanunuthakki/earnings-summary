"""Post-earnings IR-transcript scan — catch the issuer's official transcript fast.

A company publishes the official earnings-call transcript on its OWN IR site
days-to-weeks before the free aggregators index the call (``issuer_ir`` is the
first link of the aggregator chain — see ``ir_pipeline.transcript``). This scan
re-checks the IR site DAILY for a short window after each tracked ticker's last
earnings date and stops as soon as that quarter's transcript is fetched +
ingested.

It is distinct from ``backfill_transcripts.py`` (a broad daily sweep over the
last 6 quarters of every active ticker, idempotent on raw OR processed): this is
a FOCUSED, windowed re-check of just the *latest reported* quarter, idempotent on
the **processed** artifact (``transcripts/processed/<T>_Q<n>_<Y>.txt`` — the
canonical file the Q&A roster reads), so it keeps retrying through the window
until the official transcript actually lands and is ingested.

Per ticker:

  1. Last earnings date (``sources.earnings_calendar.last_earnings_date``).
     Outside ``[earnings, earnings + window_days]`` → skip (not in a window).
  2. Latest reported fiscal quarter (``recent_fiscal_quarters`` via
     ``tracked_companies.fiscal_year_end``).
  3. Processed file already present → skip (already ingested — the stop signal).
  4. Raw file present but not yet processed → flag for ingest (a prior fetch).
  5. Otherwise ``fetch_qa(...)`` — the issuer_ir-first chain re-checks the IR
     site and writes the raw Q&A file.

After the per-ticker pass, if anything was fetched (or is awaiting ingest),
invoke ``execution/ingest_transcripts.py`` once to promote raw → processed +
register the ``transcripts`` rows.

Idempotent + best-effort: an issuer that hasn't posted yet is a no-op that
retries tomorrow, and one ticker's failure never aborts the batch.

Cron: ``cron/scan_ir_transcripts.task.xml`` (daily 02:15, before the protected
03:00-05:00 LLM window and the brief worker at 06:30).

Usage:
    python execution/scan_ir_transcripts.py
    python execution/scan_ir_transcripts.py --ticker NU
    python execution/scan_ir_transcripts.py --window-days 14 --dry-run
    python execution/scan_ir_transcripts.py --repo-root /path/to/main/repo
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from runtime.python_process import managed_python_prefix  # noqa: E402

# Sibling scripts in execution/ — needed when this module is imported (e.g. from
# tests) rather than run directly via `python execution/scan_ir_transcripts.py`.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from backfill_transcripts import recent_fiscal_quarters  # noqa: E402
from fetch_qa_transcript import FetchQaSpec, fetch_qa  # noqa: E402

import db  # noqa: E402
from sources.earnings_calendar import last_earnings_date  # noqa: E402

DEFAULT_WINDOW_DAYS = 14


def _retarget_paths(repo_root: Path) -> None:
    """Point db + the fetch helper's RAW_DIR at `repo_root` (mirrors
    backfill_transcripts). Lets a worktree-based run target the MAIN repo's data
    dir / DB / transcripts without copying anything. The production cron runs
    from the main repo, so this is a no-op there.
    """
    db.PROJECT_ROOT = str(repo_root)
    db.DATA_DIR = str(repo_root / "data")
    db.DB_PATH = str(repo_root / "data" / "portfolio.db")
    db.FMP_DIR = str(repo_root / "data" / "historical" / "fmp")
    import fetch_qa_transcript

    fetch_qa_transcript.RAW_DIR = repo_root / "transcripts" / "raw"


def due_for_scan(last_earnings: date | None, today: date, window_days: int) -> bool:
    """True when `today` is within `[last_earnings, last_earnings + window_days]`.

    A missing last-earnings date (no FMP calendar cache) or a date outside the
    window means we are NOT in a post-earnings re-check window.
    """
    if last_earnings is None:
        return False
    delta = (today - last_earnings).days
    return 0 <= delta <= window_days


def _transcript_name(ticker: str, year: int, quarter: int) -> str:
    return f"{ticker.upper()}_Q{quarter}_{year}.txt"


def _processed_exists(repo_root: Path, ticker: str, year: int, quarter: int) -> bool:
    name = _transcript_name(ticker, year, quarter)
    return (repo_root / "transcripts" / "processed" / name).exists()


def _raw_exists(repo_root: Path, ticker: str, year: int, quarter: int) -> bool:
    name = _transcript_name(ticker, year, quarter)
    return (repo_root / "transcripts" / "raw" / name).exists()


@dataclass
class TickerScanResult:
    ticker: str
    status: str  # see scan_one for the vocabulary
    quarter: str = ""  # "Q<n>_<year>" once a target quarter is known
    detail: str = ""


def scan_one(
    ticker: str,
    fye_month: int,
    repo_root: Path,
    today: date,
    window_days: int,
    dry_run: bool,
) -> TickerScanResult:
    """Decide + (unless dry-run) perform this ticker's post-earnings scan.

    Status vocabulary: ``out_of_window`` · ``no_fiscal_quarter`` ·
    ``already_ingested`` · ``pending_ingest`` (raw on disk, awaiting promotion) ·
    ``would_fetch`` (dry-run) · ``fetched`` · ``not_published_yet`` · ``error``.
    """
    last = last_earnings_date(repo_root, ticker)
    if not due_for_scan(last, today, window_days):
        last_s = last.isoformat() if last else "none"
        return TickerScanResult(ticker, "out_of_window", detail=f"last_earnings={last_s}")

    quarters = recent_fiscal_quarters(fye_month, today, 1)
    if not quarters:
        return TickerScanResult(ticker, "no_fiscal_quarter")
    year, quarter = quarters[0]
    qlabel = f"Q{quarter}_{year}"

    if _processed_exists(repo_root, ticker, year, quarter):
        return TickerScanResult(ticker, "already_ingested", qlabel)
    if dry_run:
        return TickerScanResult(ticker, "would_fetch", qlabel, detail=f"last_earnings={last}")
    # A raw file from a prior day's fetch is awaiting ingest — don't re-fetch
    # (fetch_qa would skip on the existing raw anyway); flag it for promotion.
    if _raw_exists(repo_root, ticker, year, quarter):
        return TickerScanResult(ticker, "pending_ingest", qlabel)

    try:
        hit = fetch_qa(FetchQaSpec(ticker=ticker, year=year, quarter=quarter), force=False)
    except Exception as e:  # aggregator/issuer scraping is fragile — isolate one ticker
        return TickerScanResult(ticker, "error", qlabel, detail=f"{type(e).__name__}: {e}"[:200])
    if hit is None:
        return TickerScanResult(ticker, "not_published_yet", qlabel)
    return TickerScanResult(ticker, "fetched", qlabel, detail=hit.source_name)


def _resolve_tickers(arg_ticker: str | None) -> list[tuple[str, int]]:
    """Return [(ticker, fye_month), ...] for the scan scope, sorted by ticker.

    Default scope is the active universe (``db.ACTIVE_LIST_TYPES``); ``--ticker``
    narrows to one. Mirrors ``backfill_transcripts._resolve_tickers``' parse of
    the ``MMDD`` ``fiscal_year_end`` into a calendar month.
    """
    conn = db.get_connection()
    try:
        if arg_ticker:
            cur = conn.execute(
                "SELECT ticker, fiscal_year_end FROM tracked_companies "
                "WHERE ticker = ? AND archived_at IS NULL",
                (arg_ticker.upper(),),
            )
        else:
            cur = conn.execute(
                f"SELECT ticker, fiscal_year_end FROM tracked_companies "
                f"WHERE list_type IN {db.ACTIVE_LIST_TYPES_SQL} "
                f"AND archived_at IS NULL "
                f"ORDER BY ticker"
            )
        rows = cur.fetchall()
    finally:
        conn.close()

    out: list[tuple[str, int]] = []
    for r in rows:
        fye_raw = r["fiscal_year_end"]
        if not isinstance(fye_raw, str) or len(fye_raw) < 2:
            sys.stderr.write(
                f"[skip] {r['ticker']}: fiscal_year_end missing/malformed ({fye_raw!r})\n"
            )
            continue
        try:
            month = int(fye_raw[:2])
        except ValueError:
            sys.stderr.write(f"[skip] {r['ticker']}: fiscal_year_end={fye_raw!r} not parseable\n")
            continue
        if not 1 <= month <= 12:
            sys.stderr.write(f"[skip] {r['ticker']}: fiscal_year_end month {month} out of range\n")
            continue
        out.append((str(r["ticker"]), month))
    return out


def _run_ingest(repo_root: Path, dry_run: bool) -> int:
    """Run ingest_transcripts.py to promote new raw files → processed + DB rows.

    Invokes `repo_root`'s copy of the script with `cwd=repo_root` (the same
    rationale as backfill_transcripts) so a worktree run lands on the main repo.
    """
    if dry_run:
        print("  [dry-run] would invoke ingest_transcripts.py", file=sys.stderr)
        return 0
    cmd = [
        *managed_python_prefix(PROJECT_ROOT),
        str(repo_root / "execution" / "ingest_transcripts.py"),
    ]
    proc = subprocess.run(cmd, cwd=str(repo_root))
    return proc.returncode


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--ticker", help="Single ticker to scan (default: the active universe)")
    p.add_argument(
        "--window-days",
        type=int,
        default=DEFAULT_WINDOW_DAYS,
        help=f"Days after the last earnings date to keep re-checking (default {DEFAULT_WINDOW_DAYS})",
    )
    p.add_argument(
        "--skip-ingest", action="store_true", help="Skip the post-fetch ingest_transcripts.py call"
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Plan only — report which tickers are in-window without fetching",
    )
    p.add_argument(
        "--repo-root",
        type=Path,
        default=PROJECT_ROOT,
        help="Repo root containing data/, transcripts/. Default: this repo. "
        "Worktree-based runs should pass the main repo path.",
    )
    args = p.parse_args()

    repo_root = args.repo_root.resolve()
    if repo_root != PROJECT_ROOT:
        _retarget_paths(repo_root)

    today = date.today()
    tickers = _resolve_tickers(args.ticker)
    if not tickers:
        print(json.dumps({"event": "no_tickers"}))
        return 0

    print(
        f"[scan_ir_transcripts] scope={len(tickers)} tickers  "
        f"window={args.window_days}d  today={today.isoformat()}",
        file=sys.stderr,
    )
    results: list[TickerScanResult] = []
    for ticker, fye_month in tickers:
        r = scan_one(ticker, fye_month, repo_root, today, args.window_days, args.dry_run)
        results.append(r)
        if r.status != "out_of_window":
            print(f"  {ticker:6s} {r.status:18s} {r.quarter} {r.detail}".rstrip(), file=sys.stderr)

    need_ingest = any(r.status in ("fetched", "pending_ingest") for r in results)
    ingest_rc: int | None = None
    if need_ingest and not args.skip_ingest:
        print("[scan_ir_transcripts] running ingest_transcripts.py", file=sys.stderr)
        ingest_rc = _run_ingest(repo_root, args.dry_run)

    status_counts: dict[str, int] = {}
    for r in results:
        status_counts[r.status] = status_counts.get(r.status, 0) + 1

    summary = {
        "today": today.isoformat(),
        "window_days": args.window_days,
        "dry_run": args.dry_run,
        "tickers_scanned": len(tickers),
        "status_counts": status_counts,
        "ingest_rc": ingest_rc,
        "in_window": [asdict(r) for r in results if r.status != "out_of_window"],
    }
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
