"""Backfill earnings-call transcripts + commitments for the active universe.

For every ticker in `db.ACTIVE_LIST_TYPES` (portfolio + watchlist + evaluation,
non-archived), or a single ticker via `--ticker`:

  1. Compute the last N (default 6) fiscal-quarter end dates that have already
     passed, using `tracked_companies.fiscal_year_end` to map fiscal-quarter
     index → calendar quarter end.
  2. For each (fiscal_year, fiscal_quarter) with no transcript file on disk
     (neither `transcripts/raw/<T>_Q<n>_<Y>.txt` nor
     `transcripts/processed/<T>_Q<n>_<Y>.txt`), invoke
     `fetch_qa_transcript.fetch_qa()` to pull the Q&A segment from the free
     aggregator chain (roic.ai → stockanalysis.com → tickertrends.io).
  3. After all per-ticker fetches, invoke `execution/ingest_transcripts.py`
     once to register every new file into `transcripts` + `transcript_segments`
     (idempotent on sha256).
  4. For each ticker with at least one transcript row, invoke
     `execution/extract_commitments_from_transcript.py --auto --ticker X` to
     extract forward-looking management commitments from any transcripts that
     don't yet have any commitments row.

The script is idempotent at every layer:
  - File-exists check skips a (ticker, year, quarter) we've already fetched
  - Aggregator misses are logged but tolerated — coverage gaps are expected
    for delisted micro-caps and certain foreign issuers (e.g. NTDOY). Pass
    --audio-fallback to escalate a miss to the YouTube-audio + Whisper path
    (fetch_audio_transcripts); off by default since Whisper is CPU-heavy.
  - `ingest_transcripts.py` is sha256-keyed
  - `extract_commitments --auto` skips transcripts that already have commitments

Designed to run unattended:
  - Hooked into `execution/onboard_ticker.py` (final stage; fire-and-forget)
  - Cron entry point at `cron/backfill_transcripts.task.xml` (daily 04:30,
    before the earnings-calendar fetcher at 05:45).
  - `--repo-root` is honored by both subprocess phases (ingest + extract):
    they invoke the resolved root's copy of the script with `cwd=<repo_root>`
    so worktree-based runs land on the main repo's DB and transcripts dir.

Usage:
    python execution/backfill_transcripts.py                       # all active tickers
    python execution/backfill_transcripts.py --ticker NTDOY
    python execution/backfill_transcripts.py --lookback-quarters 8
    python execution/backfill_transcripts.py --skip-extract        # fetch + ingest only
    python execution/backfill_transcripts.py --dry-run             # plan only
    python execution/backfill_transcripts.py --ticker NU --audio-fallback  # miss -> Whisper
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from calendar import monthrange
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))
# Sibling scripts in execution/ — needed when this module is imported (e.g.
# from tests) rather than run directly via `python execution/backfill_transcripts.py`.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fetch_qa_transcript import (  # type: ignore[import-not-found]  # noqa: E402
    FetchQaSpec,
    fetch_qa,
)

import db  # noqa: E402

_RAW_DIR = PROJECT_ROOT / "transcripts" / "raw"
_PROCESSED_DIR = PROJECT_ROOT / "transcripts" / "processed"
_DEFAULT_LOOKBACK = 6


def _retarget_paths(repo_root: Path) -> None:
    """Override db module paths AND this script's dir constants so all reads
    hit `repo_root` instead of this script's parent. Lets worktree-based runs
    target the main repo's data dir without copying the DB."""
    global _RAW_DIR, _PROCESSED_DIR
    db.PROJECT_ROOT = str(repo_root)
    db.DATA_DIR = str(repo_root / "data")
    db.DB_PATH = str(repo_root / "data" / "portfolio.db")
    db.FMP_DIR = str(repo_root / "data" / "historical" / "fmp")
    _RAW_DIR = repo_root / "transcripts" / "raw"
    _PROCESSED_DIR = repo_root / "transcripts" / "processed"
    # Reach into fetch_qa_transcript to point its RAW_DIR at the right place too.
    import fetch_qa_transcript  # type: ignore[import-not-found]

    fetch_qa_transcript.RAW_DIR = _RAW_DIR


def _quarter_end_date(fiscal_year: int, fiscal_quarter: int, fye_month: int) -> date:
    """Calendar date when fiscal Q<q> of fiscal year <fy> ends.

    Convention: `fiscal_year` is the calendar year in which the fiscal year
    ENDS (FY2026 for a March-FYE company ends 2026-03-31; its Q1 ends 9
    months earlier — 2025-06-30).
    """
    months_before_fye = (4 - fiscal_quarter) * 3
    year = fiscal_year
    month = fye_month - months_before_fye
    while month < 1:
        month += 12
        year -= 1
    while month > 12:
        month -= 12
        year += 1
    last_day = monthrange(year, month)[1]
    return date(year, month, last_day)


def recent_fiscal_quarters(fye_month: int, today: date, n: int) -> list[tuple[int, int]]:
    """Return up to `n` (fiscal_year, fiscal_quarter) pairs whose period_end
    is <= today, most recent first."""
    out: list[tuple[int, int]] = []
    # Walk fiscal years from a year ahead (Apple's FYE 9 means Q1 of next
    # fiscal year can end in the current calendar year) backwards.
    for y in range(today.year + 1, today.year - 5, -1):
        for q in (4, 3, 2, 1):
            end = _quarter_end_date(y, q, fye_month)
            if end <= today:
                out.append((y, q))
                if len(out) == n:
                    return out
    return out


@dataclass
class TickerBackfillResult:
    ticker: str
    fye_month: int
    fetched: list[str] = field(default_factory=list)
    skipped_existing: list[str] = field(default_factory=list)
    aggregator_misses: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def _qlabel(year: int, quarter: int) -> str:
    return f"Q{quarter}_{year}"


def _has_transcript_file(ticker: str, year: int, quarter: int) -> bool:
    name = f"{ticker}_Q{quarter}_{year}.txt"
    return (_RAW_DIR / name).exists() or (_PROCESSED_DIR / name).exists()


def _try_audio_fallback(ticker: str, year: int, quarter: int) -> bool:
    """Escalate an aggregator miss to the YouTube-audio + Whisper fallback.

    Lazy-imports ``fetch_audio_transcripts`` so yt-dlp / faster-whisper stay
    optional — the default backfill path never imports them. Returns True when a
    transcript file was produced. CPU Whisper is slow (minutes per call), which
    is why this is opt-in via ``--audio-fallback`` rather than the unattended
    default.
    """
    try:
        import fetch_audio_transcripts as fat  # type: ignore[import-not-found]
    except Exception as e:  # optional heavy deps (yt-dlp / whisper) may be absent
        sys.stderr.write(
            f"[audio-fallback] unavailable ({type(e).__name__}: {e}); "
            f"install yt-dlp + faster-whisper to enable.\n"
        )
        return False
    # Resolve ffmpeg the way fetch_audio_transcripts' CLI does (FFMPEG_LOCATION
    # env, then the Windows default, else PATH) without reaching into its
    # private helper.
    ffmpeg_env = os.environ.get("FFMPEG_LOCATION")
    if ffmpeg_env:
        ffmpeg: Path | None = Path(ffmpeg_env)
    elif os.name == "nt" and Path("C:/ffmpeg/bin").exists():
        ffmpeg = Path("C:/ffmpeg/bin")
    else:
        ffmpeg = None
    try:
        res = fat.fetch_and_transcribe(
            fat.FetchSpec(ticker=ticker, year=year, quarter=quarter), ffmpeg
        )
    except Exception as e:  # audio fetch/transcribe is best-effort
        sys.stderr.write(
            f"[audio-fallback] {ticker} Q{quarter} {year} failed: {type(e).__name__}: {e}\n"
        )
        return False
    return res is not None


def _backfill_one(
    ticker: str,
    fye_month: int,
    lookback: int,
    today: date,
    dry_run: bool,
    audio_fallback: bool = False,
) -> TickerBackfillResult:
    result = TickerBackfillResult(ticker=ticker, fye_month=fye_month)
    quarters = recent_fiscal_quarters(fye_month, today, lookback)
    for y, q in quarters:
        label = _qlabel(y, q)
        if _has_transcript_file(ticker, y, q):
            result.skipped_existing.append(label)
            continue
        if dry_run:
            result.aggregator_misses.append(f"{label} [dry-run]")
            continue
        try:
            hit = fetch_qa(FetchQaSpec(ticker=ticker, year=y, quarter=q), force=False)
        except Exception as e:
            result.errors.append(f"{label}: {type(e).__name__}: {e}"[:200])
            continue
        if hit is not None:
            result.fetched.append(label)
        elif audio_fallback and _try_audio_fallback(ticker, y, q):
            result.fetched.append(f"{label} [audio]")
        else:
            result.aggregator_misses.append(label)
    return result


def _resolve_tickers(arg_ticker: str | None) -> list[tuple[str, int]]:
    """Return [(ticker, fye_month), ...] sorted by ticker."""
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
                f"[skip] {r['ticker']}: fiscal_year_end is missing/malformed ({fye_raw!r})\n"
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
        out.append((r["ticker"], month))
    return out


def _run_ingest(repo_root: Path, dry_run: bool) -> int:
    """Run execution/ingest_transcripts.py to pick up newly-fetched files.

    Invokes `repo_root`'s copy of the script (not this script's own dir) so the
    subprocess's `Path(__file__).resolve().parents[1]` lands at the resolved
    repo root — otherwise a worktree-based run would target the worktree's
    stub data dir instead of the main repo's real DB and transcripts.
    """
    if dry_run:
        print("  [dry-run] would invoke ingest_transcripts.py", file=sys.stderr)
        return 0
    cmd = [sys.executable, str(repo_root / "execution" / "ingest_transcripts.py")]
    proc = subprocess.run(cmd, cwd=str(repo_root))
    return proc.returncode


def _run_extract(repo_root: Path, ticker: str, dry_run: bool) -> int:
    """Run extract_commitments_from_transcript.py --auto --ticker X for one ticker.

    Same repo_root rationale as `_run_ingest`.
    """
    if dry_run:
        print(
            f"  [dry-run] would invoke extract_commitments --auto --ticker {ticker}",
            file=sys.stderr,
        )
        return 0
    cmd = [
        sys.executable,
        str(repo_root / "execution" / "extract_commitments_from_transcript.py"),
        "--auto",
        "--ticker",
        ticker,
    ]
    proc = subprocess.run(cmd, cwd=str(repo_root))
    return proc.returncode


def _ticker_has_transcripts(ticker: str) -> bool:
    """Return True if there's at least one transcripts row for this ticker."""
    conn = db.get_connection()
    try:
        cur = conn.execute("SELECT 1 FROM transcripts WHERE ticker = ? LIMIT 1", (ticker.upper(),))
        return cur.fetchone() is not None
    finally:
        conn.close()


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument(
        "--ticker",
        help="Single ticker to backfill (overrides the default active-universe scope)",
    )
    p.add_argument(
        "--lookback-quarters",
        type=int,
        default=_DEFAULT_LOOKBACK,
        help=f"How many recent fiscal quarters to attempt per ticker (default {_DEFAULT_LOOKBACK})",
    )
    p.add_argument(
        "--skip-ingest", action="store_true", help="Skip the post-fetch ingest_transcripts.py call"
    )
    p.add_argument(
        "--skip-extract",
        action="store_true",
        help="Skip the post-ingest commitment-extraction calls",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Plan only — print what WOULD be fetched/ingested/extracted",
    )
    p.add_argument(
        "--audio-fallback",
        action="store_true",
        help="On an aggregator miss, escalate to the YouTube-audio + Whisper "
        "fallback (fetch_audio_transcripts). Off by default — Whisper is "
        "CPU-heavy (minutes per call) and needs yt-dlp + faster-whisper.",
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

    per_ticker: list[TickerBackfillResult] = []
    print(
        f"[backfill_transcripts] scope={len(tickers)} tickers  "
        f"lookback={args.lookback_quarters}q  today={today.isoformat()}",
        file=sys.stderr,
    )
    for ticker, fye_month in tickers:
        r = _backfill_one(
            ticker,
            fye_month,
            args.lookback_quarters,
            today,
            args.dry_run,
            audio_fallback=args.audio_fallback,
        )
        per_ticker.append(r)
        print(
            f"  {ticker:6s} fye={fye_month:02d}  "
            f"fetched={len(r.fetched)}  "
            f"skipped_existing={len(r.skipped_existing)}  "
            f"misses={len(r.aggregator_misses)}  "
            f"errors={len(r.errors)}",
            file=sys.stderr,
        )

    any_fetched = any(r.fetched for r in per_ticker)
    ingest_rc: int | None = None
    if any_fetched and not args.skip_ingest:
        print("[backfill_transcripts] running ingest_transcripts.py", file=sys.stderr)
        ingest_rc = _run_ingest(repo_root, args.dry_run)
    elif args.skip_ingest:
        print("[backfill_transcripts] --skip-ingest set; skipping ingest", file=sys.stderr)
    else:
        print("[backfill_transcripts] no new fetches; skipping ingest", file=sys.stderr)

    # Extract commitments only for tickers that now have transcripts.
    extract_results: list[dict[str, object]] = []
    if not args.skip_extract and not args.dry_run:
        for ticker, _ in tickers:
            if not _ticker_has_transcripts(ticker):
                continue
            print(f"[backfill_transcripts] extracting commitments for {ticker}", file=sys.stderr)
            rc = _run_extract(repo_root, ticker, args.dry_run)
            extract_results.append({"ticker": ticker, "rc": rc})
    elif args.skip_extract:
        print("[backfill_transcripts] --skip-extract set; skipping commitments", file=sys.stderr)

    summary = {
        "today": today.isoformat(),
        "tickers_scanned": len(tickers),
        "lookback_quarters": args.lookback_quarters,
        "dry_run": args.dry_run,
        "per_ticker": [asdict(r) for r in per_ticker],
        "ingest_rc": ingest_rc,
        "extract_results": extract_results,
        "totals": {
            "fetched": sum(len(r.fetched) for r in per_ticker),
            "skipped_existing": sum(len(r.skipped_existing) for r in per_ticker),
            "aggregator_misses": sum(len(r.aggregator_misses) for r in per_ticker),
            "errors": sum(len(r.errors) for r in per_ticker),
        },
    }
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
