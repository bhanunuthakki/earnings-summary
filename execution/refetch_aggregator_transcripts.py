"""Re-pull aggregator-sourced earnings-call transcripts with the fixed roic.ai
DOM extractor (2026-07-25).

Why this script exists: `src/aggregator_sources.py`'s old `_strip_html` +
letter-prefix heuristic silently collapsed real multi-speaker calls to ONE
turn (verified: NU_Q1_2026 — 55k chars — collapsed to a single "Operator"
turn). The fix (`_TranscriptMessageParser`) reads roic.ai's actual DOM
(`data-cy="transcripts_call_message"` / `data-transcript-speaker-name="true"`)
for genuine per-turn speaker attribution. This script re-fetches every
`.tmp/transcript_index.json` entry recorded with `source=aggregator_roic` for
a ticker scope and re-ingests it.

Only `aggregator_roic` entries are in scope: the DOM fix is roic-specific.
`aggregator_stockanalysis` / `aggregator_tickertrends` / `issuer_ir` /
`yt_dlp_whisper_search` entries are left untouched (documented residual gap
— see the PR description this script shipped with).

Supersede-vs-skip is decided centrally by `ingest_one`'s reliability-ranked
period guard (`src/compute/transcript_ingest.py` +
`src/transcripts/source_reliability.py`), not by this script: it used to
carry its own old/new comparison (segment-count only, >=2 floor) and would
leave a stray duplicate `documents`/`transcripts` row behind on every run
that didn't clear that floor — the root cause of the 2026-07-25 NSP/DHR
transcript-duplication incident (six runs in one debugging session, each
minting a new row). The central guard also weighs source reliability, not
just segment count, so a low-tier aggregator re-fetch can no longer replace
a higher-tier manual/IR transcript just because it happens to parse into
more raw segments.

Idempotent: safe to re-run. A ticker/quarter already superseded in a prior
run is simply re-verified (fetch + ingest again; sha256-keyed at the byte
level, reliability-ranked at the period level — an unchanged re-fetch is a
no-op at the ingest layer either way).

Usage:
    python execution/refetch_aggregator_transcripts.py --scope portfolio_evaluation
    python execution/refetch_aggregator_transcripts.py --tickers NU,MELI,NVDA
    python execution/refetch_aggregator_transcripts.py --tickers NU --dry-run
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import fetch_qa_transcript  # type: ignore[import-not-found]  # noqa: E402
from fetch_qa_transcript import (  # type: ignore[import-not-found]  # noqa: E402
    FetchQaSpec,
    fetch_qa,
)

import db  # noqa: E402
from compute.transcript_ingest import ingest_evidence_file  # noqa: E402
from pipeline.queries import open_db  # noqa: E402

_TRANSCRIPT_INDEX = PROJECT_ROOT / ".tmp" / "transcript_index.json"
_MANIFEST_DIR = PROJECT_ROOT / ".tmp" / "refetch_aggregator_transcripts"


@dataclass
class QuarterResult:
    ticker: str
    year: int
    quarter: int
    old_document_id: int | None
    old_segment_count: int | None
    old_distinct_speakers: int | None
    new_document_id: int | None
    new_segment_count: int | None
    new_distinct_speakers: int | None
    superseded: bool
    status: str  # ok | fetch_miss | fetch_error | no_improvement | dry_run


def _retarget(repo_root: Path) -> None:
    global _TRANSCRIPT_INDEX, _MANIFEST_DIR
    db.PROJECT_ROOT = str(repo_root)
    db.DATA_DIR = str(repo_root / "data")
    db.DB_PATH = str(repo_root / "data" / "portfolio.db")
    db.FMP_DIR = str(repo_root / "data" / "historical" / "fmp")
    fetch_qa_transcript.RAW_DIR = repo_root / "transcripts" / "raw"
    _TRANSCRIPT_INDEX = repo_root / ".tmp" / "transcript_index.json"
    _MANIFEST_DIR = repo_root / ".tmp" / "refetch_aggregator_transcripts"


def _roic_quarters_in_scope(scope_tickers: frozenset[str]) -> list[tuple[str, int, int]]:
    """[(ticker, year, quarter), ...] for every aggregator_roic index entry in scope."""
    if not _TRANSCRIPT_INDEX.exists():
        return []
    data = json.loads(_TRANSCRIPT_INDEX.read_text(encoding="utf-8"))
    out: list[tuple[str, int, int]] = []
    for key, entry in data.items():
        if not isinstance(entry, dict) or entry.get("source") != "aggregator_roic":
            continue
        parts = key.split("_")
        if len(parts) != 3:
            continue
        ticker, year_s, q_s = parts
        ticker = ticker.upper()
        if ticker not in scope_tickers:
            continue
        if not q_s.upper().startswith("Q"):
            continue
        try:
            year, quarter = int(year_s), int(q_s[1:])
        except ValueError:
            continue
        out.append((ticker, year, quarter))
    out.sort()
    return out


def _scope_tickers(
    conn: sqlite3.Connection, scope: str, explicit: list[str] | None
) -> frozenset[str]:
    if explicit:
        return frozenset(t.upper() for t in explicit)
    list_types = {
        "portfolio_evaluation": ("portfolio", "evaluation"),
        "portfolio": ("portfolio",),
        "evaluation": ("evaluation",),
        "all_active": ("portfolio", "watchlist", "evaluation"),
    }[scope]
    placeholders = ", ".join("?" for _ in list_types)
    rows = conn.execute(
        f"SELECT ticker FROM tracked_companies WHERE list_type IN ({placeholders}) "
        f"AND archived_at IS NULL AND COALESCE(instrument_type, '') != 'etf'",
        list_types,
    ).fetchall()
    return frozenset(str(r[0]).upper() for r in rows)


def _existing_txt_document(
    conn: sqlite3.Connection, ticker: str, rel_path: str
) -> tuple[int, int] | None:
    """Return (document_id, transcript_id) for the row currently backed by `rel_path`, if any.

    Reporting only — supersede-vs-skip is decided centrally by `ingest_one`'s
    reliability-ranked period guard, not here.
    """
    row = conn.execute(
        "SELECT id FROM documents WHERE ticker = ? AND file_path = ?", (ticker, rel_path)
    ).fetchone()
    if row is None:
        return None
    doc_id = int(row[0])
    trow = conn.execute("SELECT id FROM transcripts WHERE document_id = ?", (doc_id,)).fetchone()
    return (doc_id, int(trow[0])) if trow is not None else (doc_id, -1)


def _segment_stats(conn: sqlite3.Connection, transcript_id: int) -> tuple[int, int]:
    """(segment_count, distinct_non_null_speaker_count)."""
    n = conn.execute(
        "SELECT COUNT(*) FROM transcript_segments WHERE transcript_id = ?", (transcript_id,)
    ).fetchone()[0]
    d = conn.execute(
        "SELECT COUNT(DISTINCT speaker) FROM transcript_segments "
        "WHERE transcript_id = ? AND speaker IS NOT NULL",
        (transcript_id,),
    ).fetchone()[0]
    return int(n), int(d)


def _process_one(
    conn: sqlite3.Connection,
    ticker: str,
    year: int,
    quarter: int,
    tracked: frozenset[str],
    dry_run: bool,
    repo_root: Path,
) -> QuarterResult:
    processed_rel = f"transcripts/processed/{ticker}_Q{quarter}_{year}.txt"
    raw_rel = f"transcripts/raw/{ticker}_Q{quarter}_{year}.txt"

    old = _existing_txt_document(conn, ticker, processed_rel) or _existing_txt_document(
        conn, ticker, raw_rel
    )
    old_doc_id, old_transcript_id = old if old else (None, None)
    old_n, old_d = (
        _segment_stats(conn, old_transcript_id)
        if old_transcript_id not in (None, -1)
        else (None, None)
    )

    if dry_run:
        return QuarterResult(
            ticker=ticker,
            year=year,
            quarter=quarter,
            old_document_id=old_doc_id,
            old_segment_count=old_n,
            old_distinct_speakers=old_d,
            new_document_id=None,
            new_segment_count=None,
            new_distinct_speakers=None,
            superseded=False,
            status="dry_run",
        )

    try:
        fetched = fetch_qa(FetchQaSpec(ticker=ticker, year=year, quarter=quarter), force=True)
    except Exception as e:
        return QuarterResult(
            ticker=ticker,
            year=year,
            quarter=quarter,
            old_document_id=old_doc_id,
            old_segment_count=old_n,
            old_distinct_speakers=old_d,
            new_document_id=None,
            new_segment_count=None,
            new_distinct_speakers=None,
            superseded=False,
            status=f"fetch_error: {type(e).__name__}: {e}"[:200],
        )
    if fetched is None:
        return QuarterResult(
            ticker=ticker,
            year=year,
            quarter=quarter,
            old_document_id=old_doc_id,
            old_segment_count=old_n,
            old_distinct_speakers=old_d,
            new_document_id=None,
            new_segment_count=None,
            new_distinct_speakers=None,
            superseded=False,
            status="fetch_miss",
        )

    try:
        result = ingest_evidence_file(
            conn,
            file_path=fetched.output_path,
            allowed_root=repo_root / "transcripts" / "raw",
            project_root=repo_root,
            tracked_tickers=tracked,
        )
    except Exception as e:
        return QuarterResult(
            ticker=ticker,
            year=year,
            quarter=quarter,
            old_document_id=old_doc_id,
            old_segment_count=old_n,
            old_distinct_speakers=old_d,
            new_document_id=None,
            new_segment_count=None,
            new_distinct_speakers=None,
            superseded=False,
            status=f"ingest_error: {type(e).__name__}: {e}"[:200],
        )
    if result is None or result.transcript_id is None:
        return QuarterResult(
            ticker=ticker,
            year=year,
            quarter=quarter,
            old_document_id=old_doc_id,
            old_segment_count=old_n,
            old_distinct_speakers=old_d,
            new_document_id=None,
            new_segment_count=None,
            new_distinct_speakers=None,
            superseded=False,
            status="ingest_no_result",
        )

    new_n, new_d = _segment_stats(conn, result.transcript_id)
    # ingest_one already decided supersede-vs-skip via the reliability-ranked
    # period guard (source tier, then segment-count richness); this just
    # reports what it decided.
    superseded = (
        not result.skipped_existing and old_doc_id is not None and old_doc_id != result.document_id
    )
    if result.skipped_existing:
        status = "no_improvement" if old_doc_id is not None else "skipped_lower_reliability"
    else:
        status = "ok"
    return QuarterResult(
        ticker=ticker,
        year=year,
        quarter=quarter,
        old_document_id=old_doc_id,
        old_segment_count=old_n,
        old_distinct_speakers=old_d,
        new_document_id=result.document_id,
        new_segment_count=new_n,
        new_distinct_speakers=new_d,
        superseded=superseded,
        status=status,
    )


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument(
        "--scope",
        choices=["portfolio_evaluation", "portfolio", "evaluation", "all_active"],
        default="portfolio_evaluation",
    )
    p.add_argument("--tickers", type=str, default=None, help="Comma-separated override for --scope")
    p.add_argument("--repo-root", type=Path, default=PROJECT_ROOT)
    p.add_argument("--sleep-s", type=float, default=0.75, help="Politeness delay between fetches")
    p.add_argument("--dry-run", action="store_true", help="Report the plan; no network, no writes")
    args = p.parse_args()

    repo_root = args.repo_root.resolve()
    if repo_root != PROJECT_ROOT:
        _retarget(repo_root)

    conn = open_db(str(Path(db.DB_PATH)))
    try:
        explicit = [t.strip() for t in args.tickers.split(",")] if args.tickers else None
        scope_tickers = _scope_tickers(conn, args.scope, explicit)
        quarters = _roic_quarters_in_scope(scope_tickers)
        sys.stderr.write(
            json.dumps({"event": "plan", "tickers": len(scope_tickers), "quarters": len(quarters)})
            + "\n"
        )

        results: list[QuarterResult] = []
        for i, (ticker, year, quarter) in enumerate(quarters):
            r = _process_one(conn, ticker, year, quarter, scope_tickers, args.dry_run, repo_root)
            results.append(r)
            sys.stderr.write(json.dumps({"event": "quarter_done", **asdict(r)}) + "\n")
            if not args.dry_run and i + 1 < len(quarters):
                time.sleep(args.sleep_s)

        _MANIFEST_DIR.mkdir(parents=True, exist_ok=True)
        run_id = datetime.now().strftime("%Y%m%dT%H%M%S")
        manifest_path = _MANIFEST_DIR / f"{run_id}.json"
        manifest_path.write_text(
            json.dumps([asdict(r) for r in results], indent=2, default=str), encoding="utf-8"
        )

        by_status: dict[str, int] = {}
        for r in results:
            by_status[r.status] = by_status.get(r.status, 0) + 1
        summary = {
            "run_id": run_id,
            "scope": args.scope,
            "tickers_in_scope": sorted(scope_tickers),
            "quarters_planned": len(quarters),
            "superseded": sum(1 for r in results if r.superseded),
            "by_status": by_status,
            "manifest_path": str(manifest_path.relative_to(repo_root)),
        }
        print(json.dumps(summary, indent=2))
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
