"""Policy-bounded text-transcript backfill and commitment extraction.

Scheduled runs cover non-archived portfolio names. An evaluation name runs only
when explicitly selected with ``--ticker``; watchlist and index members do not
enter transcript collection.

  1. Compute the last N (default and maximum 5) fiscal-quarter end dates that have already
     passed, using `tracked_companies.fiscal_year_end` to map fiscal-quarter
     index → calendar quarter end.
  2. For each period with no exact DB/path/SHA evidence receipt, ingest an
     existing local file or invoke
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
  - Exact DB/path/SHA evidence skips a period already ingested
  - Aggregator misses are logged but tolerated. Audio/webcast extraction is
    excluded by source policy; this runner fetches text transcripts only.
  - `ingest_transcripts.py` is sha256-keyed
  - `extract_commitments --auto` skips transcripts that already have commitments

Designed to run unattended:
  - Hooked into `execution/onboard_ticker.py` (final stage; fire-and-forget)
  - Cron entry point at `cron/backfill_transcripts.task.xml` (daily 02:00,
    before the earnings-calendar fetcher at 05:45).
  - `--repo-root` is honored by both subprocess phases (ingest + extract):
    they invoke the resolved root's copy of the script with `cwd=<repo_root>`
    so worktree-based runs land on the main repo's DB and transcripts dir.

Usage:
    python execution/backfill_transcripts.py                       # automatic portfolio tickers
    python execution/backfill_transcripts.py --ticker NTDOY
    python execution/backfill_transcripts.py --lookback-quarters 5
    python execution/backfill_transcripts.py --skip-extract        # fetch + ingest only
    python execution/backfill_transcripts.py --dry-run             # plan only
"""

from __future__ import annotations

import argparse
import hashlib
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

# Sibling scripts in execution/ — needed when this module is imported (e.g.
# from tests) rather than run directly via `python execution/backfill_transcripts.py`.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fetch_qa_transcript import (  # type: ignore[import-not-found]  # noqa: E402
    FetchQaSpec,
    FetchQaStatus,
    fetch_qa,
)

import db  # noqa: E402

_RAW_DIR = PROJECT_ROOT / "transcripts" / "raw"
_PROCESSED_DIR = PROJECT_ROOT / "transcripts" / "processed"
_DEFAULT_LOOKBACK = SOURCE_POLICY_CONFIG.reported_quarter_window.max_quarters


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


def quarter_end_date(fiscal_year: int, fiscal_quarter: int, fye_month: int) -> date:
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
    for y in range(today.year + 1, today.year - _DEFAULT_LOOKBACK, -1):
        for q in (4, 3, 2, 1):
            end = quarter_end_date(y, q, fye_month)
            if end <= today:
                out.append((y, q))
                if len(out) == n:
                    return out
    return out


@dataclass
class TickerBackfillResult:
    ticker: str
    fye_month: int
    fetched: list[str] = field(default_factory=list[str])
    skipped_existing: list[str] = field(default_factory=list[str])
    aggregator_misses: list[str] = field(default_factory=list[str])
    errors: list[str] = field(default_factory=list[str])


def _qlabel(year: int, quarter: int) -> str:
    return f"Q{quarter}_{year}"


def _local_transcript_file_exists(ticker: str, year: int, quarter: int) -> bool:
    name = f"{ticker}_Q{quarter}_{year}.txt"
    return (_RAW_DIR / name).exists() or (_PROCESSED_DIR / name).exists()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _recorded_evidence_path(root: Path, recorded: str) -> Path | None:
    candidate = Path(recorded)
    if candidate.is_absolute() or ".." in candidate.parts:
        return None
    parts = candidate.parts
    if len(parts) < 3 or parts[0] != "transcripts" or parts[1] not in {"raw", "processed"}:
        return None
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


def _has_ingested_evidence(ticker: str, year: int, quarter: int, fye_month: int) -> bool:
    """Require the exact fiscal-period DB receipt, path, and bytes."""
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
    root = Path(db.PROJECT_ROOT).resolve()
    for row in rows:
        snapshot = snapshot_recorded_evidence(root, str(row["file_path"]))
        if snapshot is not None and snapshot.sha256 == str(row["sha256"]):
            return True
    return False


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
            fat.FetchSpec(ticker=ticker, year=year, quarter=quarter),
            ffmpeg,
            db_path=Path(db.DB_PATH),
            owner_requested=True,
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
    db_path: Path,
    owner_requested: bool,
    audio_fallback: bool = False,
) -> TickerBackfillResult:
    if lookback < 1 or lookback > _DEFAULT_LOOKBACK:
        raise ValueError(f"lookback must be between 1 and {_DEFAULT_LOOKBACK}")
    result = TickerBackfillResult(ticker=ticker, fye_month=fye_month)
    quarters = recent_fiscal_quarters(fye_month, today, lookback)
    for y, q in quarters:
        label = _qlabel(y, q)
        if _has_ingested_evidence(ticker, y, q, fye_month):
            result.skipped_existing.append(label)
            continue
        if _local_transcript_file_exists(ticker, y, q):
            result.fetched.append(f"{label} [pending_ingest]")
            continue
        if dry_run:
            result.aggregator_misses.append(f"{label} [dry-run]")
            continue
        try:
            hit = fetch_qa(
                FetchQaSpec(ticker=ticker, year=y, quarter=q),
                force=False,
                db_path=db_path,
                owner_requested=owner_requested,
            )
        except Exception as e:
            result.errors.append(f"{label}: {type(e).__name__}: {e}"[:200])
            continue
        if hit.status in {FetchQaStatus.ACQUIRED, FetchQaStatus.IDEMPOTENT_REPLAY}:
            result.fetched.append(label)
        elif hit.status == FetchQaStatus.DENIED:
            result.errors.append(f"{label}: transcript acquisition denied")
        elif audio_fallback and _try_audio_fallback(ticker, y, q):
            result.fetched.append(f"{label} [audio]")
        else:
            result.aggregator_misses.append(label)
    return result


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
    """Run execution/ingest_transcripts.py to pick up newly-fetched files.

    Invokes `repo_root`'s copy of the script (not this script's own dir) so the
    subprocess's `Path(__file__).resolve().parents[1]` lands at the resolved
    repo root — otherwise a worktree-based run would target the worktree's
    stub data dir instead of the main repo's real DB and transcripts.
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
        *managed_python_prefix(PROJECT_ROOT),
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


def _newly_ingested_tickers(
    results: list[TickerBackfillResult], ingest_rc: int | None
) -> list[str]:
    """Return tickers whose newly fetched transcripts were ingested successfully.

    The daily backfill is an acquisition job, not an all-universe commitment
    rebuild.  Restricting the LLM phase to this run's new inputs keeps the job
    bounded and prevents overlap with the 02:15 scan and 03:00 protected window.
    """
    if ingest_rc != 0:
        return []
    return [result.ticker for result in results if result.fetched]


def _terminal_exit_code(
    ingest_rc: int | None,
    extract_results: list[dict[str, object]],
    acquisition_errors: int = 0,
) -> int:
    """Preserve a child-ingest failure for Scheduler and human operators."""
    if ingest_rc not in (None, 0):
        return ingest_rc
    if acquisition_errors or any(item.get("rc") != 0 for item in extract_results):
        return 1
    return 0


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument(
        "--ticker",
        help="Owner-requested stored portfolio/evaluation ticker",
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
        help="Deprecated compatibility flag; rejected because webcasts/audio are excluded",
    )
    p.add_argument(
        "--repo-root",
        type=Path,
        default=PROJECT_ROOT,
        help="Repo root containing data/, transcripts/. Default: this repo. "
        "Worktree-based runs should pass the main repo path.",
    )
    args = p.parse_args()
    if args.lookback_quarters < 1 or args.lookback_quarters > _DEFAULT_LOOKBACK:
        p.error(f"--lookback-quarters must be between 1 and {_DEFAULT_LOOKBACK}")
    if args.audio_fallback:
        p.error("--audio-fallback is excluded by the text-transcript collection policy")

    repo_root = args.repo_root.resolve()
    if repo_root != PROJECT_ROOT:
        _retarget_paths(repo_root)

    today = date.today()
    selected_db_path = repo_root / "data" / "portfolio.db"
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
            selected_db_path,
            args.ticker is not None,
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

    # Extract commitments only for tickers whose transcripts were newly fetched
    # and successfully ingested during this invocation. Existing transcripts are
    # handled idempotently when first acquired, not rescanned every morning.
    extract_results: list[dict[str, object]] = []
    if not args.skip_extract and not args.dry_run:
        for ticker in _newly_ingested_tickers(per_ticker, ingest_rc):
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
    return _terminal_exit_code(
        ingest_rc,
        extract_results,
        acquisition_errors=sum(len(result.errors) for result in per_ticker),
    )


if __name__ == "__main__":
    sys.exit(main())
