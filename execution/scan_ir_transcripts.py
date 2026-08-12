"""Post-earnings IR-transcript scan — catch the issuer's official transcript fast.

A company publishes the official earnings-call transcript on its OWN IR site
days-to-weeks before the free aggregators index the call (``issuer_ir`` is the
first link of the aggregator chain — see ``ir_pipeline.transcript``). This scan
re-checks the IR site DAILY for a short window after each tracked ticker's last
earnings date and stops as soon as that quarter's transcript is fetched and
ingested. Scheduled scope is portfolio-only; evaluation names require an
explicit ``--ticker`` request, while watchlist and index-member names are denied.

It is distinct from ``backfill_transcripts.py`` (a bounded five-quarter text
backfill for portfolio names): this is a FOCUSED, windowed re-check of
just the *latest reported* quarter. Its stop signal is the exact DB-bound path
and SHA, so it keeps retrying until the official transcript is truly ingested.

Per ticker:

  1. Last earnings date (``sources.earnings_calendar.last_earnings_date``).
     Outside ``[earnings, earnings + window_days]`` → skip (not in a window).
  2. Latest reported fiscal quarter (``recent_fiscal_quarters`` via
     ``tracked_companies.fiscal_year_end``).
  3. Exact ticker/period DB receipt + recorded path + SHA means ingested.
  4. Raw file present without that receipt is pending ingest.
  5. Otherwise ``fetch_qa(...)`` — the issuer_ir-first chain re-checks the IR
     site and writes the raw Q&A file.

After the per-ticker pass, if anything was fetched (or is awaiting ingest),
invoke ``execution/ingest_transcripts.py --no-promote`` once to register rows
while preserving the immutable raw evidence path.

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
import hashlib
import json
import subprocess
import sys
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from compute.evidence_snapshot import snapshot_recorded_evidence  # noqa: E402
from models.companies import ListType  # noqa: E402
from pipeline.source_policy import (  # noqa: E402
    SOURCE_POLICY_CONFIG,
    ArtifactKind,
    CollectionSource,
    CollectionTarget,
    select_collection_targets,
)
from runtime.python_process import managed_python_prefix  # noqa: E402

# Sibling scripts in execution/ — needed when this module is imported (e.g. from
# tests) rather than run directly via `python execution/scan_ir_transcripts.py`.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from backfill_transcripts import quarter_end_date, recent_fiscal_quarters  # noqa: E402
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


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _recorded_evidence_path(repo_root: Path, recorded: str) -> Path | None:
    candidate = Path(recorded)
    if candidate.is_absolute() or ".." in candidate.parts:
        return None
    parts = candidate.parts
    if len(parts) < 3 or parts[0] != "transcripts" or parts[1] not in {"raw", "processed"}:
        return None
    root = repo_root.resolve()
    intended = (root / parts[0] / parts[1]).resolve()
    lexical = root / candidate
    current = lexical
    while True:
        try:
            attributes = getattr(current.lstat(), "st_file_attributes", 0)
        except OSError:
            attributes = 0
        if current.is_symlink() or (isinstance(attributes, int) and bool(attributes & 0x400)):
            return None
        if current == root:
            break
        if root not in current.parents:
            return None
        current = current.parent
    try:
        resolved = lexical.resolve(strict=True)
        resolved.relative_to(intended)
        return resolved
    except (OSError, ValueError):
        return None


def _ingested_evidence_exists(
    repo_root: Path,
    ticker: str,
    year: int,
    quarter: int,
    fye_month: int,
) -> bool:
    """Return true only for DB-bound bytes at the exact ticker and fiscal period."""
    period_end = quarter_end_date(year, quarter, fye_month).isoformat()
    conn = db.get_connection()
    try:
        rows = conn.execute(
            "SELECT d.file_path, d.sha256 FROM documents AS d "
            "JOIN transcripts AS t ON t.document_id = d.id "
            "WHERE UPPER(d.ticker) = ? AND UPPER(t.ticker) = ? "
            "AND t.fiscal_period_type = ? AND date(t.period_end) = date(?)",
            (ticker.upper(), ticker.upper(), f"Q{quarter}", period_end),
        ).fetchall()
    finally:
        conn.close()
    root = repo_root.resolve()
    for row in rows:
        snapshot = snapshot_recorded_evidence(root, str(row["file_path"]))
        if snapshot is not None and snapshot.sha256 == str(row["sha256"]):
            return True
    return False


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
    db_path: Path,
    owner_requested: bool,
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

    latest_quarter_count = min(1, SOURCE_POLICY_CONFIG.reported_quarter_window.max_quarters)
    quarters = recent_fiscal_quarters(fye_month, today, latest_quarter_count)
    if not quarters:
        return TickerScanResult(ticker, "no_fiscal_quarter")
    year, quarter = quarters[0]
    qlabel = f"Q{quarter}_{year}"

    if _ingested_evidence_exists(repo_root, ticker, year, quarter, fye_month):
        return TickerScanResult(ticker, "already_ingested", qlabel)
    if dry_run:
        return TickerScanResult(ticker, "would_fetch", qlabel, detail=f"last_earnings={last}")
    # A raw file from a prior day's fetch is awaiting ingest — don't re-fetch
    # (fetch_qa would skip on the existing raw anyway); flag it for promotion.
    if _raw_exists(repo_root, ticker, year, quarter):
        return TickerScanResult(ticker, "pending_ingest", qlabel)

    try:
        hit = fetch_qa(
            FetchQaSpec(ticker=ticker, year=year, quarter=quarter),
            force=False,
            db_path=db_path,
            owner_requested=owner_requested,
        )
    except Exception as e:  # aggregator/issuer scraping is fragile — isolate one ticker
        return TickerScanResult(ticker, "error", qlabel, detail=f"{type(e).__name__}: {e}"[:200])
    if hit is None:
        return TickerScanResult(ticker, "not_published_yet", qlabel)
    return TickerScanResult(ticker, "fetched", qlabel, detail=hit.source_name)


def _resolve_tickers(arg_ticker: str | None) -> list[tuple[str, int]]:
    """Return policy-authorized transcript work in company-priority order."""
    conn = db.get_connection()
    try:
        if arg_ticker:
            cur = conn.execute(
                "SELECT ticker, fiscal_year_end, list_type FROM tracked_companies "
                "WHERE ticker = ? AND archived_at IS NULL",
                (arg_ticker.upper(),),
            )
        else:
            cur = conn.execute(
                "SELECT ticker, fiscal_year_end, list_type FROM tracked_companies "
                "WHERE archived_at IS NULL ORDER BY ticker"
            )
        rows = cur.fetchall()
    finally:
        conn.close()

    months_by_ticker: dict[str, int] = {}
    targets: list[CollectionTarget] = []
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
        ticker = str(r["ticker"]).upper()
        try:
            role = ListType(str(r["list_type"]))
        except ValueError:
            continue
        months_by_ticker[ticker] = month
        targets.append(
            CollectionTarget(
                ticker=ticker,
                coverage_role=role,
                requested=arg_ticker is not None,
            )
        )
    selection = select_collection_targets(
        tuple(targets),
        source=CollectionSource.TRANSCRIPT,
        artifact_kind=ArtifactKind.TEXT_TRANSCRIPT,
    )
    for item in selection.denied:
        sys.stderr.write(
            json.dumps(
                {
                    "event": "source_collection_policy_denied",
                    "ticker": item.target.ticker,
                    "coverage_role": item.target.coverage_role.value,
                    "source": CollectionSource.TRANSCRIPT.value,
                    "artifact_kind": ArtifactKind.TEXT_TRANSCRIPT.value,
                    "reason": item.decision.reason.value,
                },
                sort_keys=True,
            )
            + "\n"
        )
    return [
        (item.target.ticker, months_by_ticker[item.target.ticker]) for item in selection.allowed
    ]


def _run_ingest(repo_root: Path, dry_run: bool) -> int:
    """Run ingest_transcripts.py without moving immutable evidence paths.

    Invokes `repo_root`'s copy of the script with `cwd=repo_root` (the same
    rationale as backfill_transcripts) so a worktree run lands on the main repo.
    """
    if dry_run:
        print("  [dry-run] would invoke ingest_transcripts.py", file=sys.stderr)
        return 0
    cmd = [
        *managed_python_prefix(PROJECT_ROOT),
        str(repo_root / "execution" / "ingest_transcripts.py"),
        "--no-promote",
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
    selected_db_path = repo_root / "data" / "portfolio.db"
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
        r = scan_one(
            ticker,
            fye_month,
            repo_root,
            today,
            args.window_days,
            args.dry_run,
            selected_db_path,
            args.ticker is not None,
        )
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
    return _terminal_exit_code(
        ingest_rc,
        scan_errors=sum(1 for result in results if result.status == "error"),
    )


def _terminal_exit_code(ingest_rc: int | None, scan_errors: int = 0) -> int:
    """Preserve a child-ingest failure for Scheduler and human operators."""
    if ingest_rc not in (None, 0):
        return ingest_rc
    return 1 if scan_errors else 0


if __name__ == "__main__":
    sys.exit(main())
