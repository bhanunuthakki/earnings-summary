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
    `backfill_transcripts.py` at 04:30 — recommend 05:00 so the FMP earnings_
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
from datetime import date
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import db  # noqa: E402
from surprise_sources import (  # noqa: E402
    SurpriseHit,
    SurpriseSource,
    default_sources,
    fetch_surprises_with_fallback,
)

_SURPRISE_DIR = PROJECT_ROOT / "data" / "surprise"
_FMP_DIR = PROJECT_ROOT / "data" / "historical" / "fmp"
_YF_SNAPSHOTS_DIR = PROJECT_ROOT / "data" / "historical" / "yfinance_snapshots"
_DEFAULT_LOOKBACK = 8


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
    output_path: str | None = None
    error: str | None = None


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
) -> TickerBackfillResult:
    result = TickerBackfillResult(ticker=ticker.upper())
    try:
        all_hits, tried = fetch_surprises_with_fallback(ticker, sources=sources)
    except Exception as e:
        # Per-ticker failure isolation — one bad ticker should not abort the
        # universe-wide backfill. Truncated to keep stderr summary readable.
        result.error = f"{type(e).__name__}: {e}"[:200]
        return result
    result.sources_tried = tried
    result.hits_total = len(all_hits)
    trimmed = _trim_to_lookback(all_hits, lookback)
    result.hits_written = len(trimmed)
    # Per-source attribution for the trimmed window — useful telemetry for
    # gauging FMP-loss impact.
    for h in trimmed:
        result.sources_per_hit[h.source_name] = result.sources_per_hit.get(h.source_name, 0) + 1
    out_path = _write_ticker_cache(ticker, trimmed, surprise_dir, dry_run)
    result.output_path = str(out_path)
    return result


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
        r = _backfill_one(ticker, sources, surprise_dir, args.lookback_quarters, args.dry_run)
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
        "per_ticker": [asdict(r) for r in results],
        "totals": {
            "hits_written": sum(r.hits_written for r in results),
            "errors": sum(1 for r in results if r.error),
        },
    }
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
