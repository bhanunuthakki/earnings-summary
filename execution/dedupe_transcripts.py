"""One-time backward sweep + prune of duplicate `transcripts` rows.

Incident: on 2026-07-25, review of D2.3/D2.4 transcript work found the same
real earnings call registered as TWO (sometimes three) `transcripts` rows —
one per (ticker, fiscal_period_type, period_end) is the intended invariant,
but nothing enforced it. A full-database query found 29 duplicate period
groups across 8 tickers (CRM, DHR, GOOG, NSP, NVDA, SNOW, TSM, WIX), from two
proximate causes:
  1. A 2026-05-19 bulk backfill left a `.pdf` (rich, manually-sourced) and a
     `.txt` (thin, promoted aggregator Q&A dump) sitting in the same
     directory for the same `<TICKER>_Q<N>_<YYYY>` stem; `ingest_transcripts.py`
     walks both and ingested each as an independent row.
  2. `fetch_qa_transcript.py`'s banner used to stamp a wall-clock `Built at`
     timestamp into the hashed file content, so re-fetching byte-identical
     Q&A text still hashed differently — defeating the sha256 idempotency
     check across a same-day `refetch_aggregator_transcripts.py` debugging
     session (6 runs in 38 minutes).

Both are now fixed at the ingest layer (`src/compute/transcript_ingest.py`'s
reliability-ranked period guard; the header no longer carries a timestamp).
This script is the one-time cleanup for what already landed, plus a
provenance backfill so the guard has real `transcripts.source` values to
compare against on every future run instead of NULL.

For every (ticker, fiscal_period_type, period_end) group with more than one
row: classify each row's source (src/transcripts/source_reliability.py),
keep the highest-ranked (richer segment count, then more recent fetch, then
lowest id as a final deterministic tiebreak), supersede the rest while
retaining their transcript, document, and segment evidence. Every row
with a NULL `source` (duplicate or not) gets backfilled by the same
classifier, so the sweep also leaves single, non-duplicated transcripts
correctly labeled.

Dry-run by default: reports every group and the keep/supersede/backfill plan
without writing anything. Pass --apply to actually mutate the database.

Usage:
    python execution/dedupe_transcripts.py                 # dry-run report
    python execution/dedupe_transcripts.py --apply
    python execution/dedupe_transcripts.py --ticker NVDA --apply
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from compute.transcript_ingest import read_transcript_text, supersede_transcripts  # noqa: E402
from pipeline.queries import open_db  # noqa: E402
from transcripts.source_reliability import (  # noqa: E402
    UNKNOWN_LEGACY,
    classify_transcript_source,
    reliability_rank,
)

_MANIFEST_DIR = PROJECT_ROOT / ".tmp" / "dedupe_transcripts"


@dataclass
class SupersededRow:
    transcript_id: int
    document_id: int
    file_path: str
    source: str
    segment_count: int
    broken_file: bool


@dataclass
class GroupReport:
    ticker: str
    fiscal_period_type: str | None
    period_end: str | None
    kept_transcript_id: int
    kept_source: str
    kept_segment_count: int
    superseded: list[SupersededRow]


def _read_text_safe(project_root: Path, rel_path: str) -> str | None:
    abs_path = project_root / rel_path
    try:
        return read_transcript_text(abs_path)
    except (OSError, ValueError):
        return None


def _classify_row(project_root: Path, row: sqlite3.Row, seg_count: int) -> tuple[str, bool]:
    """Return (source, broken_file). Trusts an already-stamped source (e.g. a
    row written after the ingest-time guard shipped); classifies from scratch
    otherwise."""
    stored = row["stored_source"]
    if stored:
        return str(stored), False
    text = _read_text_safe(project_root, row["file_path"])
    if text is None:
        return UNKNOWN_LEGACY, True
    source = classify_transcript_source(
        Path(row["file_path"]), text, is_ir_transcript_doc=(row["doc_type"] == "ir_transcript")
    )
    return source, False


def _sweep(
    conn: sqlite3.Connection, project_root: Path, ticker: str | None
) -> tuple[list[GroupReport], dict[int, str], int]:
    """Returns (duplicate_group_reports, backfill_map[transcript_id]=source, solo_count)."""
    sql = (
        "SELECT t.id AS transcript_id, t.document_id, t.ticker, t.fiscal_period_type, "
        "       t.period_end, t.source AS stored_source, "
        "       d.file_path, d.doc_type, d.fetched_at "
        "FROM transcripts t JOIN documents d ON d.id = t.document_id"
    )
    params: tuple[str, ...] = ()
    if ticker is not None:
        sql += " WHERE t.ticker = ?"
        params = (ticker.upper(),)

    rows = conn.execute(sql, params).fetchall()

    groups: dict[tuple[str, str | None, str | None], list[sqlite3.Row]] = defaultdict(list)
    for row in rows:
        key = (row["ticker"], row["fiscal_period_type"], row["period_end"])
        groups[key].append(row)

    group_reports: list[GroupReport] = []
    backfill: dict[int, str] = {}
    solo_count = 0

    for (ticker_key, fpt, period_end), members in groups.items():
        classified: list[tuple[sqlite3.Row, str, int, bool]] = []
        for row in members:
            seg_count = conn.execute(
                "SELECT COUNT(*) FROM transcript_segments WHERE transcript_id = ?",
                (row["transcript_id"],),
            ).fetchone()[0]
            source, broken = _classify_row(project_root, row, int(seg_count))
            if not row["stored_source"]:
                backfill[int(row["transcript_id"])] = source
            classified.append((row, source, int(seg_count), broken))

        if len(members) == 1:
            solo_count += 1
            continue

        def _sort_key(item: tuple[sqlite3.Row, str, int, bool]) -> tuple[int, int, str, int]:
            row, source, seg_count, _broken = item
            return (
                reliability_rank(source),
                seg_count,
                str(row["fetched_at"] or ""),
                -int(row["transcript_id"]),
            )

        classified.sort(key=_sort_key, reverse=True)
        winner_row, winner_source, winner_seg_count, _ = classified[0]
        losers = classified[1:]

        group_reports.append(
            GroupReport(
                ticker=ticker_key,
                fiscal_period_type=fpt,
                period_end=period_end,
                kept_transcript_id=int(winner_row["transcript_id"]),
                kept_source=winner_source,
                kept_segment_count=winner_seg_count,
                superseded=[
                    SupersededRow(
                        transcript_id=int(row["transcript_id"]),
                        document_id=int(row["document_id"]),
                        file_path=str(row["file_path"]),
                        source=source,
                        segment_count=seg_count,
                        broken_file=broken,
                    )
                    for row, source, seg_count, broken in losers
                ],
            )
        )

    return group_reports, backfill, solo_count


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--ticker", default=None, help="Restrict the sweep to one ticker")
    p.add_argument("--apply", action="store_true", help="Supersede losers and backfill source")
    p.add_argument("--db", default=str(PROJECT_ROOT / "data" / "portfolio.db"))
    p.add_argument("--repo-root", type=Path, default=PROJECT_ROOT)
    args = p.parse_args()

    conn = open_db(args.db)
    try:
        group_reports, backfill, solo_count = _sweep(conn, args.repo_root.resolve(), args.ticker)

        for g in group_reports:
            sys.stderr.write(
                json.dumps(
                    {
                        "event": "transcript_dedupe_group",
                        "ticker": g.ticker,
                        "fiscal_period_type": g.fiscal_period_type,
                        "period_end": g.period_end,
                        "kept_transcript_id": g.kept_transcript_id,
                        "kept_source": g.kept_source,
                        "superseded_count": len(g.superseded),
                    }
                )
                + "\n"
            )

        superseded_rows = sum(len(g.superseded) for g in group_reports)
        backfilled_rows = len(backfill)

        if args.apply:
            for transcript_id, source in backfill.items():
                conn.execute(
                    "UPDATE transcripts SET source = ? WHERE id = ?", (source, transcript_id)
                )
            for g in group_reports:
                supersede_transcripts(
                    conn,
                    winner_transcript_id=g.kept_transcript_id,
                    loser_transcript_ids=[row.transcript_id for row in g.superseded],
                )
            conn.commit()

        _MANIFEST_DIR.mkdir(parents=True, exist_ok=True)
        report_path = _MANIFEST_DIR / f"{'apply' if args.apply else 'dry_run'}.json"
        report_path.write_text(
            json.dumps(
                {
                    "applied": args.apply,
                    "ticker_scope": args.ticker,
                    "duplicate_groups": len(group_reports),
                    "rows_superseded": superseded_rows,
                    "rows_backfilled": backfilled_rows,
                    "solo_transcripts_untouched": solo_count,
                    "groups": [asdict(g) for g in group_reports],
                },
                indent=2,
                default=str,
            ),
            encoding="utf-8",
        )

        summary = {
            "applied": args.apply,
            "duplicate_groups": len(group_reports),
            "rows_superseded": superseded_rows if args.apply else 0,
            "rows_would_supersede": superseded_rows,
            "rows_backfilled": backfilled_rows if args.apply else 0,
            "rows_would_backfill": backfilled_rows,
            "solo_transcripts": solo_count,
            "report_path": str(report_path.relative_to(args.repo_root.resolve())),
        }
        print(json.dumps(summary, indent=2))
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
