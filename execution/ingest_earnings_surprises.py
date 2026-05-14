"""Ingest `data/surprise/<TICKER>_surprises.json` files into `earnings_surprises`.

Companion to `execution/backfill_earnings_surprises.py`:
  1. The backfill script writes per-ticker JSON caches under data/surprise/.
  2. This script walks those caches and upserts records keyed by
     (ticker, release_date) into the `earnings_surprises` table.

Idempotent: re-running with the same JSON yields zero net changes. When the
backfill picks up fresher data (e.g. a previously-yfinance record gets
displaced by a new FMP record), the upsert overwrites the row in place — the
unique index on (ticker, release_date) keeps schema integrity.

Designed to run after `backfill_earnings_surprises.py` in the daily cron chain
(Phase D will wire this up). For now it's invokable standalone:

    python execution/ingest_earnings_surprises.py             # all cache files
    python execution/ingest_earnings_surprises.py --ticker WIX
    python execution/ingest_earnings_surprises.py --dry-run   # plan only
    python execution/ingest_earnings_surprises.py --repo-root /path/to/main/repo
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import cast

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import db  # noqa: E402

_SURPRISE_DIR = PROJECT_ROOT / "data" / "surprise"

# Schema column list — single source of truth for the upsert statement.
# `release_date` is part of the conflict key, not the SET clause.
_UPSERT_COLUMNS: tuple[str, ...] = (
    "ticker", "release_date",
    "eps_estimate", "eps_actual",
    "revenue_estimate", "revenue_actual",
    "eps_surprise_pct", "revenue_surprise_pct",
    "num_analysts_eps", "num_analysts_revenue",
    "source_name", "source_url",
    "fetched_at",
)
_UPDATABLE_COLUMNS: tuple[str, ...] = tuple(
    c for c in _UPSERT_COLUMNS if c not in ("ticker", "release_date")
)

# Decimal-typed columns need value-equivalent comparison (SQLite's NUMERIC
# affinity strips trailing zeros: "-1800.00" → -1800), not string compare.
_DECIMAL_COLUMNS: frozenset[str] = frozenset({
    "eps_estimate", "eps_actual",
    "revenue_estimate", "revenue_actual",
    "eps_surprise_pct", "revenue_surprise_pct",
})

# fetched_at always drifts across backfill runs (regenerated per fetch). We
# still WRITE the new timestamp via the upsert — we just don't count its
# drift as a "real" change in the inserted/updated/unchanged telemetry.
_TELEMETRY_IGNORE: frozenset[str] = frozenset({"fetched_at"})


def _retarget_paths(repo_root: Path) -> Path:
    """Override db module paths AND module-local dir constants for worktree runs."""
    global _SURPRISE_DIR
    db.PROJECT_ROOT = str(repo_root)
    db.DATA_DIR = str(repo_root / "data")
    db.DB_PATH = str(repo_root / "data" / "portfolio.db")
    db.FMP_DIR = str(repo_root / "data" / "historical" / "fmp")
    _SURPRISE_DIR = repo_root / "data" / "surprise"
    return _SURPRISE_DIR


@dataclass
class TickerIngestResult:
    ticker: str
    cache_path: str
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    skipped_malformed: int = 0
    errors: list[str] = field(default_factory=list)


def _candidate_caches(surprise_dir: Path, restrict_ticker: str | None) -> list[Path]:
    """Return cache files to ingest, optionally filtered to a single ticker.

    Cache filename pattern matches the backfill script: <TICKER>_surprises.json.
    """
    if not surprise_dir.exists():
        return []
    if restrict_ticker is not None:
        path = surprise_dir / f"{restrict_ticker.upper()}_surprises.json"
        return [path] if path.exists() else []
    return sorted(surprise_dir.glob("*_surprises.json"))


def _parse_record(rec: object) -> dict[str, object] | None:
    """Validate one JSON record into a column dict ready for binding.

    Returns None for malformed input. We coerce nothing here — the source layer
    already serialized Decimals as strings; SQLite accepts strings into NUMERIC
    columns transparently.
    """
    if not isinstance(rec, dict):
        return None
    d = cast("dict[str, object]", rec)
    release_date = d.get("release_date")
    ticker = d.get("ticker")
    source_name = d.get("source_name")
    if not (isinstance(release_date, str) and isinstance(ticker, str) and
            isinstance(source_name, str)):
        return None
    # fetched_at is required by the schema; if absent, fall back to "now" so the
    # row can still land — better than rejecting a record over missing telemetry.
    fetched_at = d.get("fetched_at")
    if not isinstance(fetched_at, str):
        fetched_at = datetime.now(UTC).replace(microsecond=0).isoformat()
    out: dict[str, object] = {
        "ticker": ticker.upper(),
        "release_date": release_date,
        "source_name": source_name,
        "source_url": d.get("source_url") if isinstance(d.get("source_url"), str) else None,
        "fetched_at": fetched_at,
    }
    # Decimal-bearing columns: source-of-truth shape is `str | None` (Decimal
    # serialized) — we pass through whatever the JSON has, SQLite coerces.
    for k in (
        "eps_estimate", "eps_actual",
        "revenue_estimate", "revenue_actual",
        "eps_surprise_pct", "revenue_surprise_pct",
    ):
        v = d.get(k)
        out[k] = v if isinstance(v, str) else None
    # Analyst-count columns: optional ints
    for k in ("num_analysts_eps", "num_analysts_revenue"):
        v = d.get(k)
        out[k] = v if isinstance(v, int) and not isinstance(v, bool) else None
    return out


def _upsert_sql() -> str:
    """Build the parameterized upsert. Returns rows modified per `cursor.rowcount`."""
    cols = ", ".join(_UPSERT_COLUMNS)
    placeholders = ", ".join("?" for _ in _UPSERT_COLUMNS)
    set_clause = ",\n            ".join(
        f"{c} = excluded.{c}" for c in _UPDATABLE_COLUMNS
    )
    return (
        f"INSERT INTO earnings_surprises ({cols}) VALUES ({placeholders}) "
        f"ON CONFLICT(ticker, release_date) DO UPDATE SET\n            {set_clause}"
    )


def _values_match(col: str, existing: object, incoming: object) -> bool:
    """Compare one column's stored vs incoming value, with the right semantics:

    - Decimal columns: SQLite's NUMERIC affinity strips trailing zeros, so
      string-compare misclassifies "-1800.00" (incoming) ≠ "-1800" (stored).
      Coerce both sides to Decimal and compare numerically.
    - Everything else: str(...) equality is sufficient.
    - None on both sides always matches.
    """
    if existing is None and incoming is None:
        return True
    if existing is None or incoming is None:
        return False
    if col in _DECIMAL_COLUMNS:
        try:
            return Decimal(str(existing)) == Decimal(str(incoming))
        except InvalidOperation:
            # Fall through to string compare if either side isn't decimal-parseable
            pass
    return str(existing) == str(incoming)


def _row_exists_and_matches(
    conn: sqlite3.Connection, parsed: dict[str, object]
) -> bool:
    """Return True if a row already exists for (ticker, release_date) AND all
    meaningful updatable columns match the incoming record. Drives the
    inserted/updated/unchanged telemetry — the upsert itself runs unconditionally.

    `fetched_at` is excluded because it drifts across backfill runs without
    representing a real change. Decimal columns use value-equivalent comparison
    to handle SQLite's NUMERIC normalization.
    """
    cur = conn.execute(
        f"SELECT {', '.join(_UPDATABLE_COLUMNS)} FROM earnings_surprises "
        f"WHERE ticker = ? AND release_date = ?",
        (parsed["ticker"], parsed["release_date"]),
    )
    row = cur.fetchone()
    if row is None:
        return False
    for col in _UPDATABLE_COLUMNS:
        if col in _TELEMETRY_IGNORE:
            continue
        if not _values_match(col, row[col], parsed[col]):
            return False
    return True


def ingest_one_ticker(
    conn: sqlite3.Connection, cache_path: Path, *, dry_run: bool
) -> TickerIngestResult:
    """Ingest one ticker's cache file. Idempotent. Returns per-record telemetry."""
    ticker = cache_path.stem.replace("_surprises", "").upper()
    result = TickerIngestResult(ticker=ticker, cache_path=str(cache_path))
    try:
        payload_raw = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        result.errors.append(f"read/parse: {type(e).__name__}: {e}"[:200])
        return result
    if not isinstance(payload_raw, dict):
        result.errors.append("payload root is not an object")
        return result
    payload = cast("dict[str, object]", payload_raw)
    records = payload.get("records")
    if not isinstance(records, list):
        result.errors.append("payload.records is missing or not a list")
        return result

    sql = _upsert_sql()
    for rec_raw in cast("list[object]", records):
        parsed = _parse_record(rec_raw)
        if parsed is None:
            result.skipped_malformed += 1
            continue
        already_matches = _row_exists_and_matches(conn, parsed)
        if dry_run:
            if already_matches:
                result.unchanged += 1
            else:
                # Can't distinguish insert vs update without writing; for the
                # dry-run summary, lump both as "would change".
                result.updated += 1
            continue
        # Detect insert vs update by checking row existence BEFORE the upsert.
        cur_check = conn.execute(
            "SELECT 1 FROM earnings_surprises WHERE ticker = ? AND release_date = ?",
            (parsed["ticker"], parsed["release_date"]),
        )
        existed = cur_check.fetchone() is not None
        values = tuple(parsed[c] for c in _UPSERT_COLUMNS)
        conn.execute(sql, values)
        if not existed:
            result.inserted += 1
        elif already_matches:
            result.unchanged += 1
        else:
            result.updated += 1
    return result


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--ticker", help="Single ticker to ingest")
    p.add_argument("--dry-run", action="store_true", help="Plan only — no DB writes")
    p.add_argument(
        "--repo-root", type=Path, default=PROJECT_ROOT,
        help="Repo root containing data/. Default: this repo.",
    )
    args = p.parse_args()

    if args.repo_root.resolve() != PROJECT_ROOT:
        surprise_dir = _retarget_paths(args.repo_root.resolve())
    else:
        surprise_dir = _SURPRISE_DIR

    caches = _candidate_caches(surprise_dir, args.ticker)
    if not caches:
        print(json.dumps({"event": "no_caches", "surprise_dir": str(surprise_dir)}))
        return 0

    conn = db.get_connection()
    try:
        results: list[TickerIngestResult] = []
        print(
            f"[ingest_earnings_surprises] caches={len(caches)}  "
            f"dry_run={args.dry_run}  surprise_dir={surprise_dir}",
            file=sys.stderr,
        )
        for cache in caches:
            r = ingest_one_ticker(conn, cache, dry_run=args.dry_run)
            results.append(r)
            err_tag = f" ERR={r.errors[0]}" if r.errors else ""
            print(
                f"  {r.ticker:6s}  insert={r.inserted:2d}  update={r.updated:2d}  "
                f"unchanged={r.unchanged:2d}  malformed={r.skipped_malformed:2d}"
                f"{err_tag}",
                file=sys.stderr,
            )
        if not args.dry_run:
            conn.commit()
    finally:
        conn.close()

    summary = {
        "caches_scanned": len(caches),
        "dry_run": args.dry_run,
        "per_ticker": [asdict(r) for r in results],
        "totals": {
            "inserted": sum(r.inserted for r in results),
            "updated": sum(r.updated for r in results),
            "unchanged": sum(r.unchanged for r in results),
            "malformed": sum(r.skipped_malformed for r in results),
            "errors": sum(1 for r in results if r.errors),
        },
    }
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
