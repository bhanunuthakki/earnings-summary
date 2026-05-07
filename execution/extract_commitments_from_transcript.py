"""Persist forward-looking commitments extracted from transcript_segments.

Two-step protocol mirroring extract_kpis_from_ir.py:

  1. `--list-pending --ticker X`: print transcripts for ticker X that have
     no commitments yet, plus segment counts. The user (or LLM) reads each
     transcript and populates a manifest JSON of {commitments: [...]}.

  2. `--apply <manifest.json>`: validate (Pydantic) and persist into
     management_commitments. Each commitment must reference a real
     transcript_segments.id.

Manifest shape:
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
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from compute.say_do import (  # noqa: E402
    CommitmentExtractionManifest,
    persist_manifest,
)
from pipeline.queries import open_db  # noqa: E402


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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--list-pending", action="store_true")
    group.add_argument("--apply", type=Path, help="Manifest JSON path")
    parser.add_argument("--ticker", help="Restrict --list-pending to one ticker")
    parser.add_argument("--db", default=str(PROJECT_ROOT / "data" / "portfolio.db"))
    args = parser.parse_args()

    conn = open_db(args.db)
    try:
        if args.list_pending:
            pending = _list_pending(conn, args.ticker)
            print(json.dumps({"pending_transcripts": len(pending), "rows": pending}, indent=2))
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
