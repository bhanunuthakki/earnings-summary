"""
execution/truncate_peer_fmp_cache.py
------------------------------------
One-time trimmer that brings existing index_member peer files in
data/historical/fmp/ down to the shallow peer contract
(save_fmp_data.PEER_ENDPOINT_ALLOWLIST). Peers were historically fetched at
the full ~67-endpoint / 100-quarter depth, which grew the cache to 9.1 GB
(2026-07-30 DB-size audit); their consumers read only 9 file families at
shallow depth.

Per index_member ticker (and ONLY index_member — a ticker that also carries
any other list_type row is left untouched):
  - files outside the 9 allowlisted families are DELETED
  - the 5 depth-limited families are truncated to their newest N records
  - the 4 keep-as-is families (profile, peers, TTM metrics/ratios) are untouched

Dry-run by default: prints the plan and byte counts, changes nothing. Pass
--apply to execute. Idempotent — a rerun finds nothing left to trim.

Deliberately NOT touched:
  - fmp_endpoint_status rows for deleted endpoints (current-state grid; the
    cacher no longer queues those endpoints for peers, so stale rows are inert)
  - data/historical/fmp_snapshots/ (separate history store)

Expect one full non-deduplicated track_comp_metrics run after --apply: its
source-files fingerprint hashes file bytes, and truncation changes them.

Usage:
  python execution/truncate_peer_fmp_cache.py             # dry run, all peers
  python execution/truncate_peer_fmp_cache.py --apply
  python execution/truncate_peer_fmp_cache.py --tickers ZS,DDOG --apply
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import cast

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))
import db as portfolio_db  # noqa: E402

FMP_DIR = PROJECT_ROOT / "data" / "historical" / "fmp"

# Local mirror of save_fmp_data.PEER_ENDPOINT_ALLOWLIST (not imported: that
# module exits at import when FMP_API_KEY is unset, and trimming must work
# offline). tests/test_peer_fmp_depth.py guards the two against drifting.
KEEP_FULL: frozenset[str] = frozenset(
    {"profile", "peers", "key_metrics_ttm", "financial_ratios_ttm"}
)
TRUNCATE_DEPTH: dict[str, int] = {
    "income_statement_quarterly": 9,
    "key_metrics_quarterly": 4,
    "balance_sheet_quarterly": 1,
    "analyst_estimates_annual": 3,
    "historical_market_cap": 90,
}


@dataclass
class TickerResult:
    ticker: str
    deleted_files: int = 0
    deleted_bytes: int = 0
    truncated_files: int = 0
    truncated_bytes: int = 0  # bytes reclaimed by truncation
    kept_files: int = 0
    skipped_files: int = 0  # unreadable / non-list payloads left alone


def _log_event(event: str, **fields: object) -> None:
    print(json.dumps({"event": event, **fields}, default=str), file=sys.stderr)


def _load_list(path: Path) -> list[dict[str, object]] | None:
    """The file's JSON body as a record list; None when absent/bad/non-list."""
    try:
        raw: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, list):
        return None
    return [cast("dict[str, object]", r) for r in cast("list[object]", raw) if isinstance(r, dict)]


def process_ticker(fmp_dir: Path, ticker: str, *, apply: bool) -> TickerResult:
    """Trim one peer's cache files to the shallow contract. Pure filesystem —
    the caller owns ticker selection (which rows are actually index_member)."""
    t = ticker.upper()
    result = TickerResult(ticker=t)
    prefix = f"{t}_"
    for path in sorted(fmp_dir.glob(f"{prefix}*.json")):
        suffix = path.name[len(prefix) : -len(".json")]
        size = path.stat().st_size

        if suffix in KEEP_FULL:
            result.kept_files += 1
            continue

        if suffix in TRUNCATE_DEPTH:
            depth = TRUNCATE_DEPTH[suffix]
            records = _load_list(path)
            if records is None:
                # Non-list payloads (error envelopes, empty dicts) carry no
                # depth to trim; leave them for the fetcher's own machinery.
                result.skipped_files += 1
                continue
            if len(records) <= depth:
                result.kept_files += 1
                continue
            # Newest-first, matching every consumer's own sort.
            records.sort(key=lambda r: str(r.get("date") or ""), reverse=True)
            if apply:
                path.write_text(json.dumps(records[:depth], indent=2), encoding="utf-8")
                result.truncated_bytes += size - path.stat().st_size
            else:
                # Dry-run estimate: assume bytes scale with record count.
                result.truncated_bytes += int(size * (1 - depth / len(records)))
            result.truncated_files += 1
            continue

        # Outside the peer contract entirely.
        if apply:
            path.unlink()
        result.deleted_files += 1
        result.deleted_bytes += size
    return result


def _index_member_tickers() -> list[str]:
    """index_member tickers that carry NO other list_type row (belt and
    braces: a name promoted to portfolio/watchlist must never be trimmed)."""
    conn = portfolio_db.get_connection()
    try:
        rows = conn.execute(
            """
            SELECT DISTINCT ticker FROM tracked_companies
            WHERE list_type = 'index_member'
              AND ticker NOT IN (
                  SELECT ticker FROM tracked_companies WHERE list_type != 'index_member'
              )
            ORDER BY ticker
            """
        ).fetchall()
        return [str(r["ticker"]).upper() for r in rows]
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Trim index_member peer FMP cache files to the shallow peer contract"
    )
    parser.add_argument(
        "--apply", action="store_true", help="Actually delete/truncate (default: dry run)"
    )
    parser.add_argument(
        "--tickers",
        help="Comma-separated subset to process (still must be index_member-only rows)",
    )
    args = parser.parse_args()

    peers = _index_member_tickers()
    if args.tickers:
        wanted = {t.strip().upper() for t in args.tickers.split(",") if t.strip()}
        unknown = wanted - set(peers)
        if unknown:
            print(
                f"Refusing: not index_member-only tickers: {sorted(unknown)}",
                file=sys.stderr,
            )
            sys.exit(1)
        peers = [t for t in peers if t in wanted]

    mode = "apply" if args.apply else "dry_run"
    _log_event("start", mode=mode, peer_count=len(peers), fmp_dir=FMP_DIR)

    totals = TickerResult(ticker="_total")
    for ticker in peers:
        r = process_ticker(FMP_DIR, ticker, apply=args.apply)
        totals.deleted_files += r.deleted_files
        totals.deleted_bytes += r.deleted_bytes
        totals.truncated_files += r.truncated_files
        totals.truncated_bytes += r.truncated_bytes
        totals.kept_files += r.kept_files
        totals.skipped_files += r.skipped_files
        if r.deleted_files or r.truncated_files:
            _log_event(
                "ticker_trimmed",
                ticker=r.ticker,
                deleted_files=r.deleted_files,
                deleted_bytes=r.deleted_bytes,
                truncated_files=r.truncated_files,
                truncated_bytes=r.truncated_bytes,
            )

    reclaimed = totals.deleted_bytes + totals.truncated_bytes
    summary = {
        "mode": mode,
        "peer_count": len(peers),
        "deleted_files": totals.deleted_files,
        "deleted_bytes": totals.deleted_bytes,
        "truncated_files": totals.truncated_files,
        "truncated_bytes_reclaimed": totals.truncated_bytes,
        "kept_files": totals.kept_files,
        "skipped_files": totals.skipped_files,
        "total_bytes_reclaimed": reclaimed,
        "total_gb_reclaimed": round(reclaimed / 1024**3, 2),
        "already_done": totals.deleted_files == 0 and totals.truncated_files == 0,
    }
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
