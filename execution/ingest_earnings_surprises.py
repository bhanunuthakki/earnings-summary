"""Ingest cached earnings surprises into an immutable ledger and latest projection.

Every valid source record is first appended to ``earnings_surprise_observations``.
The newest observation for each ``(ticker, release_date)`` is then projected into
the backward-compatible ``earnings_surprises`` table. Invalid inputs receive an
immutable quarantine disposition and make the CLI exit non-zero.
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
from earnings_surprise_store import (  # noqa: E402
    EarningsSurpriseRecordV1,
    append_observation,
    observation_identity,
    quarantine_payload,
    validate_source_record,
)

_SURPRISE_DIR = PROJECT_ROOT / "data" / "surprise"

_UPSERT_COLUMNS: tuple[str, ...] = (
    "ticker",
    "release_date",
    "eps_estimate",
    "eps_actual",
    "revenue_estimate",
    "revenue_actual",
    "eps_surprise_pct",
    "revenue_surprise_pct",
    "num_analysts_eps",
    "num_analysts_revenue",
    "source_name",
    "source_url",
    "fetched_at",
    "source_observation_id",
)
_UPDATABLE_COLUMNS: tuple[str, ...] = tuple(
    column for column in _UPSERT_COLUMNS if column not in ("ticker", "release_date")
)
_DECIMAL_COLUMNS: frozenset[str] = frozenset(
    {
        "eps_estimate",
        "eps_actual",
        "revenue_estimate",
        "revenue_actual",
        "eps_surprise_pct",
        "revenue_surprise_pct",
    }
)
_TELEMETRY_IGNORE: frozenset[str] = frozenset({"fetched_at", "source_observation_id"})


def _retarget_paths(repo_root: Path) -> Path:
    """Override database and cache paths for an explicit checkout."""
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
    observations_inserted: int = 0
    observation_duplicates: int = 0
    errors: list[str] = field(default_factory=list[str])


def _candidate_caches(surprise_dir: Path, restrict_ticker: str | None) -> list[Path]:
    if not surprise_dir.exists():
        return []
    if restrict_ticker is not None:
        path = surprise_dir / f"{restrict_ticker.upper()}_surprises.json"
        return [path] if path.exists() else []
    return sorted(surprise_dir.glob("*_surprises.json"))


def _parse_record(
    rec: object, *, ticker_hint: str | None = None
) -> EarningsSurpriseRecordV1 | None:
    """Compatibility helper around the governed Pydantic boundary."""
    hint = ticker_hint
    if hint is None and isinstance(rec, dict):
        raw = cast("dict[str, object]", rec)
        candidate = raw.get("ticker")
        hint = candidate if isinstance(candidate, str) else ""
    return validate_source_record(cast(object, rec), ticker_hint=hint or "").record


def _upsert_sql() -> str:
    cols = ", ".join(_UPSERT_COLUMNS)
    placeholders = ", ".join("?" for _ in _UPSERT_COLUMNS)
    set_clause = ",\n            ".join(
        f"{column} = excluded.{column}" for column in _UPDATABLE_COLUMNS
    )
    return (
        f"INSERT INTO earnings_surprises ({cols}) VALUES ({placeholders}) "
        f"ON CONFLICT(ticker, release_date) DO UPDATE SET\n            {set_clause}"
    )


def _values_match(column: str, existing: object, incoming: object) -> bool:
    if existing is None and incoming is None:
        return True
    if existing is None or incoming is None:
        return False
    if column in _DECIMAL_COLUMNS:
        try:
            return Decimal(str(existing)) == Decimal(str(incoming))
        except InvalidOperation:
            pass
    return str(existing) == str(incoming)


def _projection_payload(record: EarningsSurpriseRecordV1, observation_id: str) -> dict[str, object]:
    payload = record.model_dump(mode="json")
    payload["source_observation_id"] = observation_id
    return payload


def _row_exists_and_matches(conn: sqlite3.Connection, parsed: dict[str, object]) -> bool:
    row = conn.execute(
        f"SELECT {', '.join(_UPDATABLE_COLUMNS)} FROM earnings_surprises "
        "WHERE ticker = ? AND release_date = ?",
        (parsed["ticker"], parsed["release_date"]),
    ).fetchone()
    if row is None:
        return False
    return all(
        column in _TELEMETRY_IGNORE or _values_match(column, row[column], parsed[column])
        for column in _UPDATABLE_COLUMNS
    )


def _as_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _should_project(conn: sqlite3.Connection, parsed: dict[str, object]) -> bool:
    row = conn.execute(
        "SELECT fetched_at FROM earnings_surprises WHERE ticker = ? AND release_date = ?",
        (parsed["ticker"], parsed["release_date"]),
    ).fetchone()
    if row is None:
        return True
    return _as_utc(str(parsed["fetched_at"])) >= _as_utc(str(row["fetched_at"]))


def _quarantine_cache_failure(
    conn: sqlite3.Connection,
    *,
    dry_run: bool,
    raw_payload: object,
    ticker: str,
    cache_path: Path,
    reason_code: str,
    error: str,
) -> None:
    if dry_run:
        return
    quarantine_payload(
        conn,
        raw_payload=raw_payload,
        ticker_hint=ticker,
        cache_path=str(cache_path),
        record_ordinal=-1,
        reason_code=reason_code,
        reason_details={"error": error[:1000]},
    )


def ingest_one_ticker(
    conn: sqlite3.Connection, cache_path: Path, *, dry_run: bool
) -> TickerIngestResult:
    """Ingest one cache atomically within the caller-owned transaction."""
    ticker = cache_path.stem.replace("_surprises", "").upper()
    result = TickerIngestResult(ticker=ticker, cache_path=str(cache_path))
    try:
        raw_text = cache_path.read_text(encoding="utf-8")
    except OSError as exc:
        error = f"{type(exc).__name__}: {exc}"
        _quarantine_cache_failure(
            conn,
            dry_run=dry_run,
            raw_payload=None,
            ticker=ticker,
            cache_path=cache_path,
            reason_code="cache_read_failed",
            error=error,
        )
        result.errors.append(f"read: {error}"[:200])
        return result
    try:
        payload_raw = cast(object, json.loads(raw_text))
    except json.JSONDecodeError as exc:
        error = f"{type(exc).__name__}: {exc}"
        _quarantine_cache_failure(
            conn,
            dry_run=dry_run,
            raw_payload=raw_text,
            ticker=ticker,
            cache_path=cache_path,
            reason_code="cache_json_invalid",
            error=error,
        )
        result.errors.append(f"read/parse: {error}"[:200])
        return result
    if not isinstance(payload_raw, dict):
        error = "payload root is not an object"
        _quarantine_cache_failure(
            conn,
            dry_run=dry_run,
            raw_payload=payload_raw,
            ticker=ticker,
            cache_path=cache_path,
            reason_code="cache_root_invalid",
            error=error,
        )
        result.errors.append(error)
        return result
    payload = cast("dict[str, object]", payload_raw)
    records = payload.get("records")
    if not isinstance(records, list):
        error = "payload.records is missing or not a list"
        _quarantine_cache_failure(
            conn,
            dry_run=dry_run,
            raw_payload=payload,
            ticker=ticker,
            cache_path=cache_path,
            reason_code="cache_records_invalid",
            error=error,
        )
        result.errors.append(error)
        return result

    sql = _upsert_sql()
    for ordinal, rec_raw in enumerate(cast("list[object]", records)):
        disposition = validate_source_record(rec_raw, ticker_hint=ticker)
        record = disposition.record
        if record is None:
            result.skipped_malformed += 1
            if not dry_run:
                quarantine_payload(
                    conn,
                    raw_payload=rec_raw,
                    ticker_hint=ticker,
                    cache_path=str(cache_path),
                    record_ordinal=ordinal,
                    reason_code=disposition.reason_code or "schema_validation_failed",
                    reason_details=disposition.reason_details,
                )
            continue

        observation_id = observation_identity(record)
        if not dry_run:
            observation_id, observation_inserted = append_observation(
                conn,
                record=record,
                raw_payload=rec_raw,
                cache_path=str(cache_path),
                record_ordinal=ordinal,
            )
            if observation_inserted:
                result.observations_inserted += 1
            else:
                result.observation_duplicates += 1
        parsed = _projection_payload(record, observation_id)
        already_matches = _row_exists_and_matches(conn, parsed)
        should_project = _should_project(conn, parsed)
        if dry_run:
            if already_matches or not should_project:
                result.unchanged += 1
            else:
                result.updated += 1
            continue
        if not should_project:
            result.unchanged += 1
            continue

        existed = (
            conn.execute(
                "SELECT 1 FROM earnings_surprises WHERE ticker = ? AND release_date = ?",
                (parsed["ticker"], parsed["release_date"]),
            ).fetchone()
            is not None
        )
        conn.execute(sql, tuple(parsed[column] for column in _UPSERT_COLUMNS))
        if not existed:
            result.inserted += 1
        elif already_matches:
            result.unchanged += 1
        else:
            result.updated += 1
    return result


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--ticker", help="Single ticker to ingest")
    parser.add_argument("--dry-run", action="store_true", help="Plan only; no database writes")
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=PROJECT_ROOT,
        help="Repo root containing data/. Default: this repo.",
    )
    args = parser.parse_args()

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
            json.dumps(
                {
                    "event": "earnings_surprise_ingest_started",
                    "caches": len(caches),
                    "dry_run": args.dry_run,
                    "surprise_dir": str(surprise_dir),
                }
            ),
            file=sys.stderr,
        )
        for cache in caches:
            result = ingest_one_ticker(conn, cache, dry_run=args.dry_run)
            results.append(result)
            print(
                json.dumps(
                    {
                        "event": "earnings_surprise_cache_processed",
                        **asdict(result),
                    }
                ),
                file=sys.stderr,
            )
        if not args.dry_run:
            conn.commit()
    finally:
        conn.close()

    partial = any(result.errors or result.skipped_malformed for result in results)
    summary = {
        "caches_scanned": len(caches),
        "dry_run": args.dry_run,
        "terminal_status": "partial_failure" if partial else "completed",
        "per_ticker": [asdict(result) for result in results],
        "totals": {
            "inserted": sum(result.inserted for result in results),
            "updated": sum(result.updated for result in results),
            "unchanged": sum(result.unchanged for result in results),
            "quarantined": sum(result.skipped_malformed for result in results),
            "observations_inserted": sum(result.observations_inserted for result in results),
            "observation_duplicates": sum(result.observation_duplicates for result in results),
            "errors": sum(1 for result in results if result.errors),
        },
    }
    print(json.dumps(summary, indent=2))
    return 2 if partial else 0


if __name__ == "__main__":
    sys.exit(main())
