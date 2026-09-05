"""Backfill EPS / Revenue surprise records for the active universe.

For every ticker in `db.ACTIVE_LIST_TYPES` (portfolio + watchlist + evaluation,
non-archived), or a single ticker via `--ticker`:

  1. Walk the `surprise_sources` chain (FMP earnings_calendar on disk →
     yfinance.earnings_dates) and merge by release_date with first-source-wins
     priority.
  2. Trim to the most recent `--lookback-quarters` reported quarters (default 8).
  3. Write the merged list to `data/surprise/<TICKER>_surprises.json`
     (gitignored — same family as `data/historical/fmp/`).

The script is idempotent — re-running rewrites the per-ticker file with the
latest merged view. There is no in-place patching of the existing cache
because both sources are bulk endpoints; a full rewrite is cheaper and avoids
stale-record drift.

Designed to run unattended:
  - Daily cron hook (Phase D will wire this into the same scheduler that runs
    `backfill_transcripts.py` at 02:00 — recommend 05:00 so the FMP earnings_
    calendar fetch ahead of it has time to land).

Usage:
    python execution/backfill_earnings_surprises.py                  # all active tickers
    python execution/backfill_earnings_surprises.py --ticker WIX
    python execution/backfill_earnings_surprises.py --lookback-quarters 12
    python execution/backfill_earnings_surprises.py --dry-run        # plan only
    python execution/backfill_earnings_surprises.py --repo-root /path/to/main/repo
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import db  # noqa: E402
from earnings_surprise_store import cache_generation_identity  # noqa: E402
from pipeline.data_coverage_dispositions import (  # noqa: E402
    EARNINGS_SURPRISE_POLICY_NAME,
    EARNINGS_SURPRISE_POLICY_VERSION,
    CoverageArtifactKind,
    CoverageAttempt,
    CoverageAttemptStatus,
    CoverageDispositionStatus,
    DataCoverageDispositionRequest,
    append_data_coverage_disposition,
    policy_config_sha256,
    recent_completed_fiscal_quarters,
)
from surprise_sources import (  # noqa: E402
    SurpriseHit,
    SurpriseSource,
    default_sources,
    fetch_surprises_with_outcomes,
)

_SURPRISE_DIR = PROJECT_ROOT / "data" / "surprise"
_FMP_DIR = PROJECT_ROOT / "data" / "historical" / "fmp"
_YF_SNAPSHOTS_DIR = PROJECT_ROOT / "data" / "historical" / "yfinance_snapshots"
_DEFAULT_LOOKBACK = 8
_POLICY_NAME = EARNINGS_SURPRISE_POLICY_NAME
_POLICY_VERSION = EARNINGS_SURPRISE_POLICY_VERSION
_MAX_RELEASE_LAG_DAYS = 110


def _retarget_paths(repo_root: Path) -> tuple[Path, Path, Path]:
    """Override db module paths AND module-local dir constants for worktree runs.

    Returns the resolved (surprise_dir, fmp_dir, yf_snapshots_dir) so the
    caller doesn't have to re-derive them.
    """
    global _SURPRISE_DIR, _FMP_DIR, _YF_SNAPSHOTS_DIR
    db.PROJECT_ROOT = str(repo_root)
    db.DATA_DIR = str(repo_root / "data")
    db.DB_PATH = str(repo_root / "data" / "portfolio.db")
    db.FMP_DIR = str(repo_root / "data" / "historical" / "fmp")
    _SURPRISE_DIR = repo_root / "data" / "surprise"
    _FMP_DIR = repo_root / "data" / "historical" / "fmp"
    _YF_SNAPSHOTS_DIR = repo_root / "data" / "historical" / "yfinance_snapshots"
    return _SURPRISE_DIR, _FMP_DIR, _YF_SNAPSHOTS_DIR


@dataclass
class TickerBackfillResult:
    ticker: str
    sources_tried: list[str] = field(default_factory=list[str])
    hits_total: int = 0
    hits_written: int = 0
    sources_per_hit: dict[str, int] = field(default_factory=dict[str, int])
    source_release_dates: dict[str, list[str]] = field(default_factory=dict[str, list[str]])
    output_path: str | None = None
    error: str | None = None
    coverage_dispositions: list[str] = field(default_factory=list[str])


def _resolve_tickers(arg_ticker: str | None) -> list[str]:
    """Return active-universe tickers (or [arg_ticker] if specified)."""
    conn = db.get_connection()
    try:
        if arg_ticker:
            cur = conn.execute(
                "SELECT ticker FROM tracked_companies WHERE ticker = ? AND archived_at IS NULL",
                (arg_ticker.upper(),),
            )
        else:
            cur = conn.execute(
                f"SELECT ticker FROM tracked_companies "
                f"WHERE list_type IN {db.ACTIVE_LIST_TYPES_SQL} "
                f"AND archived_at IS NULL "
                f"ORDER BY ticker"
            )
        return [row["ticker"] for row in cur.fetchall()]
    finally:
        conn.close()


def _trim_to_lookback(hits: list[SurpriseHit], lookback_quarters: int) -> list[SurpriseHit]:
    """Keep the most recent `lookback_quarters` hits by release_date (oldest-first
    output preserved). Lookback ≤ 0 returns the full list."""
    if lookback_quarters <= 0 or len(hits) <= lookback_quarters:
        return hits
    return hits[-lookback_quarters:]


def _write_ticker_cache(
    ticker: str, hits: list[SurpriseHit], surprise_dir: Path, dry_run: bool
) -> Path:
    """Write `<ticker>_surprises.json` atomically. Returns the resolved output path."""
    out = surprise_dir / f"{ticker.upper()}_surprises.json"
    if dry_run:
        return out
    surprise_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "ticker": ticker.upper(),
        "generated_at": date.today().isoformat(),
        "record_count": len(hits),
        "records": [h.to_json() for h in hits],
    }
    payload["cache_generation_id"] = cache_generation_identity(payload, cache_path=str(out))
    tmp = out.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp.replace(out)
    return out


def _backfill_one(
    ticker: str,
    sources: list[SurpriseSource],
    surprise_dir: Path,
    lookback: int,
    dry_run: bool,
    persist_coverage: bool = False,
) -> TickerBackfillResult:
    result = TickerBackfillResult(ticker=ticker.upper())
    all_hits: list[SurpriseHit] = []
    try:
        all_hits, source_outcomes = fetch_surprises_with_outcomes(ticker, sources=sources)
    except Exception as e:
        # Per-ticker failure isolation — one bad ticker should not abort the
        # universe-wide backfill. Truncated to keep stderr summary readable.
        result.error = f"{type(e).__name__}: {e}"[:200]
    else:
        result.sources_tried = [outcome.source_name for outcome in source_outcomes]
        result.source_release_dates = {
            outcome.source_name: [hit.release_date.isoformat() for hit in outcome.hits]
            for outcome in source_outcomes
        }
        result.hits_total = len(all_hits)
        trimmed = _trim_to_lookback(all_hits, lookback)
        result.hits_written = len(trimmed)
        # Per-source attribution for the trimmed window — useful telemetry for
        # gauging FMP-loss impact.
        for h in trimmed:
            result.sources_per_hit[h.source_name] = result.sources_per_hit.get(h.source_name, 0) + 1
        out_path = _write_ticker_cache(ticker, trimmed, surprise_dir, dry_run)
        result.output_path = str(out_path)
    if persist_coverage and not dry_run:
        try:
            result.coverage_dispositions = _persist_surprise_coverage(
                result,
                hits=all_hits,
                as_of=date.today(),
                lookback=lookback,
            )
        except Exception as exc:
            result.error = f"coverage disposition: {type(exc).__name__}: {exc}"[:200]
    return result


def _fye_month(ticker: str) -> int:
    conn = db.get_connection()
    try:
        row = conn.execute(
            "SELECT fiscal_year_end FROM tracked_companies "
            "WHERE UPPER(ticker)=? AND archived_at IS NULL",
            (ticker.upper(),),
        ).fetchone()
    finally:
        conn.close()
    raw = None if row is None else row["fiscal_year_end"]
    if not isinstance(raw, str) or len(raw) < 2:
        raise ValueError(f"{ticker}: fiscal_year_end is unavailable")
    month = int(raw[:2])
    if not 1 <= month <= 12:
        raise ValueError(f"{ticker}: fiscal_year_end month is invalid")
    return month


def _assign_release_dates(
    releases: list[date], targets: tuple[tuple[int, int, date], ...]
) -> dict[date, tuple[int, int, date]]:
    assigned: dict[date, tuple[int, int, date]] = {}
    for release in releases:
        candidates = [
            target
            for target in targets
            if target[2] < release and (release - target[2]).days <= _MAX_RELEASE_LAG_DAYS
        ]
        if candidates:
            assigned[release] = max(candidates, key=lambda target: target[2])
    return assigned


def _persist_surprise_coverage(
    result: TickerBackfillResult,
    *,
    hits: list[SurpriseHit],
    as_of: date,
    lookback: int,
) -> list[str]:
    fye_month = _fye_month(result.ticker)
    targets = recent_completed_fiscal_quarters(
        fye_month=fye_month,
        as_of=as_of,
        limit=lookback if lookback > 0 else _DEFAULT_LOOKBACK,
    )
    hit_assignments = _assign_release_dates([hit.release_date for hit in hits], targets)
    source_assignments = {
        source: _assign_release_dates([date.fromisoformat(item) for item in releases], targets)
        for source, releases in result.source_release_dates.items()
    }
    hits_by_target = {
        target: hit
        for hit in hits
        for release, target in hit_assignments.items()
        if hit.release_date == release
    }
    providers = tuple(result.sources_tried)
    policy_sha = policy_config_sha256(
        policy_name=_POLICY_NAME,
        policy_version=_POLICY_VERSION,
        providers=providers,
    )
    observed_at = datetime.now(UTC)
    persisted: list[str] = []
    conn = db.get_connection()
    try:
        for fiscal_year, fiscal_quarter, period_end in targets:
            target = (fiscal_year, fiscal_quarter, period_end)
            hit = hits_by_target.get(target)
            attempts = tuple(
                CoverageAttempt(
                    provider=source,
                    status=(
                        CoverageAttemptStatus.SOURCE_HIT
                        if target in assignments.values()
                        else CoverageAttemptStatus.SOURCE_MISS
                    ),
                )
                for source, assignments in source_assignments.items()
            )
            if result.error is not None:
                status = CoverageDispositionStatus.OPERATIONAL_ERROR
                reason = "surprise_source_refresh_failed"
                attempts = (
                    CoverageAttempt(
                        provider="surprise_chain",
                        status=CoverageAttemptStatus.FAILED,
                    ),
                )
                evidence_reference = None
                evidence_sha256 = None
                retry_after = observed_at + timedelta(days=1)
            elif hit is None:
                status = CoverageDispositionStatus.PROVIDER_COVERAGE_GAP
                reason = "no_admitted_surprise_observation"
                evidence_reference = None
                evidence_sha256 = None
                retry_after = observed_at + timedelta(days=1)
            else:
                # A provider hit is not database completeness. The ingest job
                # writes SATISFIED only after immutable observation + current
                # projection identities both exist.
                continue
            disposition = append_data_coverage_disposition(
                conn,
                DataCoverageDispositionRequest(
                    artifact_kind=CoverageArtifactKind.EARNINGS_SURPRISE,
                    ticker=result.ticker,
                    fiscal_year=fiscal_year,
                    fiscal_quarter=fiscal_quarter,
                    period_end=period_end,
                    status=status,
                    reason_code=reason,
                    attempts=attempts,
                    policy_name=_POLICY_NAME,
                    policy_version=_POLICY_VERSION,
                    policy_config_sha256=policy_sha,
                    evidence_reference=evidence_reference,
                    evidence_sha256=evidence_sha256,
                    observed_at=observed_at,
                    retry_after=retry_after,
                ),
            )
            persisted.append(f"Q{fiscal_quarter}_{fiscal_year}:{disposition.request.status.value}")
        conn.commit()
    finally:
        conn.close()
    return persisted


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--ticker", help="Single ticker to backfill (overrides active-universe scope)")
    p.add_argument(
        "--lookback-quarters",
        type=int,
        default=_DEFAULT_LOOKBACK,
        help=f"Keep the most recent N reported quarters per ticker (default {_DEFAULT_LOOKBACK}, "
        f"0 = keep all source history)",
    )
    p.add_argument("--dry-run", action="store_true", help="Plan only — do not write files")
    p.add_argument(
        "--repo-root",
        type=Path,
        default=PROJECT_ROOT,
        help="Repo root containing data/. Default: this repo. Worktree-based runs "
        "should pass the main repo path.",
    )
    args = p.parse_args()

    if args.repo_root.resolve() != PROJECT_ROOT:
        surprise_dir, fmp_dir, yf_snapshots_dir = _retarget_paths(args.repo_root.resolve())
    else:
        surprise_dir, fmp_dir, yf_snapshots_dir = _SURPRISE_DIR, _FMP_DIR, _YF_SNAPSHOTS_DIR

    tickers = _resolve_tickers(args.ticker)
    if not tickers:
        print(json.dumps({"event": "no_tickers"}))
        return 0

    # yf_snapshots_dir enables revenue actual-vs-estimate on the yfinance
    # fallback where our own point-in-time archive allows (estimates-widening);
    # an absent/empty archive degrades to the pre-existing EPS-only behavior.
    sources = default_sources(fmp_dir=fmp_dir, yf_snapshots_dir=yf_snapshots_dir)
    results: list[TickerBackfillResult] = []
    print(
        f"[backfill_earnings_surprises] scope={len(tickers)} tickers  "
        f"lookback={args.lookback_quarters}q  fmp_dir={fmp_dir}",
        file=sys.stderr,
    )
    for ticker in tickers:
        r = _backfill_one(
            ticker,
            sources,
            surprise_dir,
            args.lookback_quarters,
            args.dry_run,
            True,
        )
        results.append(r)
        attribution = " ".join(f"{k}={v}" for k, v in sorted(r.sources_per_hit.items()))
        print(
            f"  {ticker:6s}  total={r.hits_total:3d}  written={r.hits_written:2d}  "
            f"{attribution}  {('ERR ' + r.error) if r.error else ''}",
            file=sys.stderr,
        )

    summary = {
        "tickers_scanned": len(tickers),
        "lookback_quarters": args.lookback_quarters,
        "dry_run": args.dry_run,
        "terminal_status": "partial_failure" if any(r.error for r in results) else "completed",
        "per_ticker": [asdict(r) for r in results],
        "totals": {
            "hits_written": sum(r.hits_written for r in results),
            "errors": sum(1 for r in results if r.error),
        },
    }
    print(json.dumps(summary, indent=2))
    return 2 if any(r.error for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
