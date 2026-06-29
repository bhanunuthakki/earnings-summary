"""Audit + re-sync the thesis_state content mirror against on-disk holdings JSON.

`thesis_state.raw_json` + `thesis` are a cache of `micro_thesis/holdings/<T>.json`,
first seeded once by migration 0008 (2026-05-03). The evaluator (`persist_verdict`)
only maintains `breach_status`/`last_updated`, so before #PR the *content* mirror
froze at migration time and silently drifted as holdings files were edited — most
visibly NU, whose row was a `stub_regenerated_from_corruption` stub while the file
held the full current Intact thesis.

This is the backfill + audit tool for that drift. Read-only by default:

    python execution/sync_thesis_state.py                 # audit all, human summary
    python execution/sync_thesis_state.py --json          # audit all, JSON
    python execution/sync_thesis_state.py --ticker NU     # audit one
    python execution/sync_thesis_state.py --apply         # re-ingest every drifted row
    python execution/sync_thesis_state.py --ticker NU --apply

`--apply` re-mirrors each drifted row from its file via
`compute.thesis_evaluator.refresh_thesis_mirror` (which touches only
thesis/raw_json/ingested_at — never breach_status), then re-audits to confirm
the rows came back CLEAN. Going forward the evaluator keeps the mirror fresh on
every run (persist_verdict now takes holdings_dir), so this is a one-time repair
plus an ongoing drift check.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import cast

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from compute.thesis_evaluator import refresh_thesis_mirror  # noqa: E402
from pipeline.queries import open_db  # noqa: E402

_HOLDINGS_DIR = PROJECT_ROOT / "micro_thesis" / "holdings"
_STUB_STATUS = "stub_regenerated_from_corruption"


class DriftKind(StrEnum):
    """How a thesis_state row relates to its on-disk holdings file.

    Ordered most-broken → clean; `classify_row` reports the single most-specific
    kind. All kinds except CLEAN and NO_FILE are re-ingestable (`needs_sync`).
    """

    NO_FILE = "no_file"  # row exists, holdings JSON missing — cannot re-ingest
    BAD_JSON = "bad_json"  # raw_json doesn't parse — re-ingest overwrites it
    PLACEHOLDER = "placeholder"  # raw_json == '{}' — created by evaluator, never seeded
    STUB = "stub"  # raw_json carries _status: stub_regenerated_from_corruption
    THESIS_DRIFT = "thesis_drift"  # raw_json.thesis != file.thesis
    BODY_DRIFT = "body_drift"  # thesis same, rest of payload differs
    CLEAN = "clean"  # mirror == file


@dataclass(frozen=True, slots=True)
class DriftRecord:
    ticker: str
    kind: DriftKind
    detail: str

    @property
    def needs_sync(self) -> bool:
        return self.kind not in (DriftKind.CLEAN, DriftKind.NO_FILE)


def classify_row(
    ticker: str,
    db_thesis: object,
    raw_json: object,
    holdings_dir: Path,
) -> DriftRecord:
    """Classify one thesis_state row against its on-disk holdings file."""
    path = holdings_dir / f"{ticker.upper()}.json"
    if not path.exists():
        return DriftRecord(ticker, DriftKind.NO_FILE, f"no file at {path.name}")

    raw_str = raw_json if isinstance(raw_json, str) else ""
    try:
        parsed: object = json.loads(raw_str) if raw_str else {}
    except json.JSONDecodeError:
        return DriftRecord(ticker, DriftKind.BAD_JSON, "raw_json does not parse")

    if not isinstance(parsed, dict) or parsed == {}:
        return DriftRecord(ticker, DriftKind.PLACEHOLDER, "raw_json is the '{}' placeholder")
    stored = cast("dict[str, object]", parsed)

    file_payload = cast("dict[str, object]", json.loads(path.read_text(encoding="utf-8")))
    file_thesis = file_payload.get("thesis")

    if stored.get("_status") == _STUB_STATUS:
        return DriftRecord(
            ticker,
            DriftKind.STUB,
            f"_status={_STUB_STATUS}; file verdict={file_payload.get('verdict')!r}",
        )
    if stored.get("thesis") != file_thesis or db_thesis != file_thesis:
        return DriftRecord(ticker, DriftKind.THESIS_DRIFT, "raw_json.thesis != file.thesis")
    if stored != file_payload:
        return DriftRecord(ticker, DriftKind.BODY_DRIFT, "thesis same, payload body stale")
    return DriftRecord(ticker, DriftKind.CLEAN, "mirror matches file")


def audit_thesis_state(
    conn: sqlite3.Connection,
    holdings_dir: Path,
    *,
    ticker: str | None = None,
) -> list[DriftRecord]:
    """Classify every thesis_state row (or one ticker) against its file."""
    if ticker:
        rows = conn.execute(
            "SELECT ticker, thesis, raw_json FROM thesis_state WHERE ticker = ?",
            (ticker.upper(),),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT ticker, thesis, raw_json FROM thesis_state ORDER BY ticker"
        ).fetchall()
    return [classify_row(r["ticker"], r["thesis"], r["raw_json"], holdings_dir) for r in rows]


def sync_drifted(
    conn: sqlite3.Connection,
    holdings_dir: Path,
    records: list[DriftRecord],
    *,
    ingested_at: datetime | None = None,
) -> list[str]:
    """Re-ingest every drifted row; commit once. Returns the tickers re-synced."""
    stamp = ingested_at or datetime.now()
    synced: list[str] = []
    for rec in records:
        if not rec.needs_sync:
            continue
        refresh_thesis_mirror(conn, rec.ticker, holdings_dir, ingested_at=stamp)
        synced.append(rec.ticker)
    if synced:
        conn.commit()
    return synced


def _record_to_dict(rec: DriftRecord) -> dict[str, object]:
    return {
        "ticker": rec.ticker,
        "kind": rec.kind.value,
        "needs_sync": rec.needs_sync,
        "detail": rec.detail,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=str(PROJECT_ROOT / "data" / "portfolio.db"))
    ap.add_argument("--holdings-dir", default=str(_HOLDINGS_DIR), type=Path)
    ap.add_argument("--ticker", default=None, help="Audit/sync a single ticker")
    ap.add_argument(
        "--apply",
        action="store_true",
        help="Re-ingest drifted rows (default: read-only audit)",
    )
    ap.add_argument("--json", action="store_true", help="Emit JSON instead of a table")
    args = ap.parse_args()

    conn = open_db(args.db)
    try:
        records = audit_thesis_state(conn, args.holdings_dir, ticker=args.ticker)
        drifted = [r for r in records if r.needs_sync]

        synced: list[str] = []
        post: list[DriftRecord] = []
        if args.apply and drifted:
            synced = sync_drifted(conn, args.holdings_dir, drifted)
            # Re-audit the rows we touched to prove they came back CLEAN.
            post = [
                rec for t in synced for rec in audit_thesis_state(conn, args.holdings_dir, ticker=t)
            ]

        if args.json:
            print(
                json.dumps(
                    {
                        "db": args.db,
                        "applied": args.apply,
                        "total": len(records),
                        "drifted": [_record_to_dict(r) for r in drifted],
                        "synced": synced,
                        "post_sync": [_record_to_dict(r) for r in post],
                    },
                    indent=2,
                )
            )
        else:
            counts: dict[str, int] = {}
            for r in records:
                counts[r.kind.value] = counts.get(r.kind.value, 0) + 1
            print(f"thesis_state rows audited: {len(records)}  (db={args.db})")
            for kind in DriftKind:
                if counts.get(kind.value):
                    print(f"  {kind.value:13s} {counts[kind.value]}")
            if drifted:
                print(f"\nDrifted rows ({len(drifted)}):")
                for r in drifted:
                    print(f"  {r.ticker:6s} {r.kind.value:13s} {r.detail}")
            else:
                print("\nNo drift - mirror is faithful to every holdings file.")
            if args.apply:
                print(f"\nRe-ingested {len(synced)} row(s): {', '.join(synced) or '(none)'}")
                still = [r for r in post if r.needs_sync]
                if still:
                    print(f"  WARNING: still drifted after sync: {[r.ticker for r in still]}")
                elif synced:
                    print("  Verified: all re-ingested rows now CLEAN.")
            elif drifted:
                print("\nRe-run with --apply to re-ingest these rows.")

        # Non-zero exit when (audit-only) drift remains, so a cron/CI guard can
        # alert. After --apply, exit reflects any rows that failed to come clean.
        residual = [r for r in post if r.needs_sync] if args.apply else drifted
        return 1 if residual else 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
