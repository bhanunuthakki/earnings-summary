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
     `compute.say_do_extractor.extract_for_transcript`. Idempotent at three
     layers — a transcript is skipped when it already has a commitment row,
     when a commitment_scan_log row records a prior scan (zero-commitment
     scans included: they were re-scanned daily at full LLM cost before the
     marker existed), or when its ticker has no kpi_definitions catalog
     (the call's outcome is predetermined).

LLM governance: calls go through the canonical `call_llm` entry point with
purpose="saydo_commitment_extract" (LLM_MODELS pin, llm_budgets cap, ledger
attribution). Never revert to the private `_call_claude` — that bypass made
this extractor the repo's single largest ungoverned cost line (~$150/mo of
purpose=NULL ledger rows).

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
import sqlite3
import sys
from collections.abc import Callable
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import db  # noqa: E402
from compute.say_do import (  # noqa: E402
    CommitmentExtractionManifest,
    persist_manifest,
)
from compute.say_do_extractor import (  # noqa: E402
    extract_for_transcript,
    record_scan,
    transcripts_pending_extraction,
)
from llm.prompt_versions import prompt_version_for  # noqa: E402
from llm_client import call_llm  # noqa: E402
from pipeline.queries import open_db  # noqa: E402
from provenance.selection import selected_transcripts_relation  # noqa: E402

log = logging.getLogger("extract_commitments")

# The governed purpose key: LLM_MODELS pin + llm_budgets row + ledger
# attribution all key off this. Registered in src/llm/cli.py and
# src/llm/prompt_versions.py; budget seeded by migration 0129.
PURPOSE = "saydo_commitment_extract"


def _governed_llm_call(ticker: str) -> Callable[[str], str]:
    """Per-ticker LLM callable for extract_for_transcript: routes through the
    canonical entry point so purpose/ticker land on every ledger row."""

    def _call(prompt: str) -> str:
        return call_llm(prompt, purpose=PURPOSE, ticker=ticker)

    return _call


def _list_pending(conn: sqlite3.Connection, ticker: str | None) -> list[dict[str, object]]:
    """Return transcripts (one row per transcript) that --auto would target.

    Mirrors transcripts_pending_extraction's selection (no commitments, never
    scanned, ticker has a KPI catalog) but carries segment counts for the
    human-readable listing."""
    pending = {tid for tid, _tk, _pe in transcripts_pending_extraction(conn, ticker=ticker)}
    if not pending:
        return []
    transcripts_relation = selected_transcripts_relation(conn).sql
    sql = (
        "SELECT t.id, t.ticker, t.period_end, t.fiscal_period_type, "
        "       (SELECT COUNT(*) FROM transcript_segments s WHERE s.transcript_id = t.id) AS segments "
        f"FROM {transcripts_relation} t "
        f"WHERE t.id IN ({','.join('?' * len(pending))}) "
        "ORDER BY t.ticker, t.period_end"
    )
    cur = conn.execute(sql, tuple(pending))
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
    conn: sqlite3.Connection, *, ticker: str | None, transcript_id: int | None, max_n: int
) -> list[tuple[int, str]]:
    """Pick which transcripts to auto-extract from.

    --transcript-id wins; otherwise pull the pending list (optionally filtered
    by --ticker) and cap at --max."""
    if transcript_id is not None:
        transcripts_relation = selected_transcripts_relation(conn).sql
        cur = conn.execute(
            f"SELECT id, ticker FROM {transcripts_relation} WHERE id = ?",
            (transcript_id,),
        )
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
    conn: sqlite3.Connection,
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
    version = prompt_version_for(PURPOSE)
    for tid, tk in targets:
        try:
            manifest = extract_for_transcript(conn, tid, llm_call=_governed_llm_call(tk))
        except Exception as e:  # surface in report rather than abort the batch
            # No scan marker on failure: a parse/call error is retryable
            # tomorrow, unlike a real zero-commitment scan.
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
        if dry_run:
            results.append(
                {
                    "transcript_id": tid,
                    "ticker": tk,
                    "extracted": n_extracted,
                    "inserted": 0,
                }
            )
            continue

        if n_extracted == 0:
            record_scan(conn, tid, n_extracted=0, prompt_version=version)
            results.append(
                {
                    "transcript_id": tid,
                    "ticker": tk,
                    "extracted": 0,
                    "inserted": 0,
                }
            )
            continue

        ids = persist_manifest(conn, manifest)
        record_scan(conn, tid, n_extracted=n_extracted, prompt_version=version)
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


def main(argv: list[str] | None = None) -> int:
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
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="[extract_commitments] %(message)s")

    # An explicit --db must also reach the governed LLM cost ledger. The --auto
    # path calls `call_llm(purpose="saydo_commitment_extract")`, which resolves
    # the llm_calls DB (plus the budget check and the LLM_MODELS pin) from the
    # `db.DB_PATH` process global — NOT from `open_db(args.db)`. Without this sync
    # an explicit --db (from a worktree / non-default cwd) would land the cost row
    # in the default DB, the "no such table: llm_calls" split-brain. Per the
    # db.set_db_path contract this also re-points DATA_DIR / FMP_DIR.
    db.set_db_path(args.db)

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
