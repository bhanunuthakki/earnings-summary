"""Persist forward-looking commitments extracted from transcript_segments.

Three modes:

  1. `--list-pending [--ticker X]`: print transcripts that have no commitments
     yet (one row per transcript, with segment counts).

  2. `--apply <manifest.json>`: validate (Pydantic) and persist into
     management_commitments. Each commitment must reference a real
     transcript_segments.id. Used for hand-authored / in-session-LLM manifests.

  3. `--auto [--ticker X | --transcript-id N] [--max N] [--dry-run]`:
     AUTOMATED extraction. For every pending transcript, call the LLM to
     extract forward-looking commitments and persist them. Wires through
     `compute.say_do_extractor.extract_for_transcript`. Idempotent —
     transcripts already with at least one commitment row are skipped.

Manifest shape (for --apply):
    {
      "commitments": [
        {
          "ticker": "MELI",
          "period_made": "2024-09-30",
          "transcript_segment_id": 42,
          "period_target": "2024-12-31",
          "kpi_name": "Revenue YoY Growth (USD)",
          "comparator": "ge",
          "target_value": "30",
          "unit": "percent",
          "narrative": "We expect strong Q4 momentum continuing into the holiday season..."
        }
      ]
    }
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from compute.say_do import (  # noqa: E402
    CommitmentExtractionManifest,
    persist_manifest,
)
from compute.say_do_extractor import (  # noqa: E402
    extract_for_transcript,
    transcripts_pending_extraction,
)
from llm_client import _call_claude  # noqa: E402
from pipeline.queries import open_db  # noqa: E402

log = logging.getLogger("extract_commitments")


def _list_pending(conn, ticker: str | None) -> list[dict[str, object]]:
    """Return transcripts (one row per transcript) that have no commitments yet."""
    sql = (
        "SELECT t.id, t.ticker, t.period_end, t.fiscal_period_type, "
        "       (SELECT COUNT(*) FROM transcript_segments s WHERE s.transcript_id = t.id) AS segments "
        "FROM transcripts t "
        "WHERE NOT EXISTS ("
        "  SELECT 1 FROM management_commitments mc "
        "  JOIN transcript_segments s ON s.id = mc.transcript_segment_id "
        "  WHERE s.transcript_id = t.id"
        ") "
    )
    params: tuple[str, ...] = ()
    if ticker is not None:
        sql += "AND t.ticker = ? "
        params = (ticker.upper(),)
    sql += "ORDER BY t.ticker, t.period_end"
    cur = conn.execute(sql, params)
    return [
        {
            "transcript_id": int(row["id"]),
            "ticker": row["ticker"],
            "period_end": row["period_end"][:10] if row["period_end"] else None,
            "fiscal_period_type": row["fiscal_period_type"],
            "segments": int(row["segments"]),
        }
        for row in cur.fetchall()
    ]


def _resolve_auto_targets(
    conn, *, ticker: str | None, transcript_id: int | None, max_n: int
) -> list[tuple[int, str]]:
    """Pick which transcripts to auto-extract from.

    --transcript-id wins; otherwise pull the pending list (optionally filtered
    by --ticker) and cap at --max."""
    if transcript_id is not None:
        cur = conn.execute("SELECT id, ticker FROM transcripts WHERE id = ?", (transcript_id,))
        row = cur.fetchone()
        if row is None:
            return []
        return [(int(row["id"]), row["ticker"])]

    pending = transcripts_pending_extraction(conn, ticker=ticker)
    targets = [(tid, tk) for tid, tk, _ in pending]
    if max_n > 0:
        targets = targets[:max_n]
    return targets


def _run_auto(
    conn,
    *,
    ticker: str | None,
    transcript_id: int | None,
    max_n: int,
    dry_run: bool,
) -> dict[str, object]:
    """Auto-extract for each target. Returns a structured run report."""
    targets = _resolve_auto_targets(conn, ticker=ticker, transcript_id=transcript_id, max_n=max_n)
    results: list[dict[str, object]] = []
    total_inserted = 0
    for tid, tk in targets:
        try:
            manifest = extract_for_transcript(conn, tid, llm_call=_call_claude)
        except Exception as e:  # noqa: BLE001 — surface in report rather than abort
            log.warning("extract failed for transcript_id=%d ticker=%s: %s", tid, tk, e)
            results.append(
                {
                    "transcript_id": tid,
                    "ticker": tk,
                    "extracted": 0,
                    "inserted": 0,
                    "error": f"{type(e).__name__}: {e}"[:200],
                }
            )
            continue

        n_extracted = len(manifest.commitments)
        if dry_run or n_extracted == 0:
            results.append(
                {
                    "transcript_id": tid,
                    "ticker": tk,
                    "extracted": n_extracted,
                    "inserted": 0,
                }
            )
            continue

        ids = persist_manifest(conn, manifest)
        total_inserted += len(ids)
        results.append(
            {
                "transcript_id": tid,
                "ticker": tk,
                "extracted": n_extracted,
                "inserted": len(ids),
                "commitment_ids": ids,
            }
        )
    return {
        "targets": len(targets),
        "total_inserted": total_inserted,
        "dry_run": dry_run,
        "results": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--list-pending", action="store_true")
    group.add_argument("--apply", type=Path, help="Manifest JSON path")
    group.add_argument(
        "--auto",
        action="store_true",
        help="Auto-extract via LLM for every transcript with no commitments yet",
    )
    parser.add_argument("--ticker", help="Restrict --list-pending or --auto to one ticker")
    parser.add_argument(
        "--transcript-id",
        type=int,
        help="--auto only: extract for one specific transcript (overrides --ticker)",
    )
    parser.add_argument(
        "--max",
        type=int,
        default=0,
        help="--auto only: cap targets to first N transcripts (0 = no cap)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="--auto only: extract + report but do not persist",
    )
    parser.add_argument("--db", default=str(PROJECT_ROOT / "data" / "portfolio.db"))
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="[extract_commitments] %(message)s")

    conn = open_db(args.db)
    try:
        if args.list_pending:
            pending = _list_pending(conn, args.ticker)
            print(json.dumps({"pending_transcripts": len(pending), "rows": pending}, indent=2))
            return 0

        if args.auto:
            report = _run_auto(
                conn,
                ticker=args.ticker,
                transcript_id=args.transcript_id,
                max_n=args.max,
                dry_run=args.dry_run,
            )
            print(json.dumps(report, indent=2))
            return 0

        with open(args.apply, encoding="utf-8") as f:
            payload = json.load(f)
        manifest = CommitmentExtractionManifest.model_validate(payload)
        ids = persist_manifest(conn, manifest)
        print(json.dumps({"inserted": len(ids), "commitment_ids": ids}, indent=2))
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
