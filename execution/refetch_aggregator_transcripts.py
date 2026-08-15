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
from datetime import date, datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import fetch_qa_transcript  # type: ignore[import-not-found]  # noqa: E402

import db  # noqa: E402
from models.companies import ListType  # noqa: E402
from models.documents import DocType, SourceType  # noqa: E402
from pipeline.queries import open_db  # noqa: E402
from pipeline.source_policy import (  # noqa: E402
    SOURCE_POLICY_CONFIG,
    ArtifactKind,
    CollectionSource,
    CollectionTarget,
    select_collection_targets,
)
from pipeline.transcript_acquisition import (  # noqa: E402
    COMBINED_SOURCE_REGIME_IDENTITY,
    AuthorizedTranscriptArtifact,
    TranscriptAcquisitionDeniedError,
    authorize_transcript_request,
)
from transcripts.acquisition_semantics import (  # noqa: E402
    TRANSCRIPT_ACQUISITION_POLICY_VERSION,
    ExistingArtifactBehavior,
    TranscriptAcquisitionAuthorization,
    TranscriptAcquisitionEntrypoint,
    TranscriptAcquisitionRequest,
    TranscriptAuthorizationStatus,
    TranscriptProvider,
)

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
    """Latest policy-bounded aggregator_roic quarters for each authorized ticker."""
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
    bound = SOURCE_POLICY_CONFIG.reported_quarter_window.max_quarters
    bounded: list[tuple[str, int, int]] = []
    for ticker in sorted(scope_tickers):
        ticker_periods = sorted(
            (item for item in out if item[0] == ticker),
            key=lambda item: (item[1], item[2]),
            reverse=True,
        )
        bounded.extend(sorted(ticker_periods[:bound]))
    return bounded


def _scope_tickers(
    conn: sqlite3.Connection, scope: str, explicit: list[str] | None
) -> frozenset[str]:
    list_types = {
        "portfolio_evaluation": ("portfolio", "evaluation"),
        "portfolio": ("portfolio",),
        "evaluation": ("evaluation",),
        "all_active": ("portfolio", "watchlist", "evaluation"),
    }[scope]
    if explicit:
        list_types = ("portfolio", "evaluation", "watchlist", "index_member")
    placeholders = ", ".join("?" for _ in list_types)
    rows = conn.execute(
        f"SELECT ticker,list_type FROM tracked_companies WHERE list_type IN ({placeholders}) "  # nosec B608 -- placeholders are generated only from the closed role tuple
        f"AND archived_at IS NULL AND COALESCE(instrument_type, '') != 'etf'",
        list_types,
    ).fetchall()
    explicit_tickers = frozenset(t.strip().upper() for t in explicit or [] if t.strip())
    targets = tuple(
        CollectionTarget(
            ticker=str(row[0]),
            coverage_role=ListType(str(row[1])),
            requested=bool(explicit_tickers),
        )
        for row in rows
        if not explicit_tickers or str(row[0]).upper() in explicit_tickers
    )
    selection = select_collection_targets(
        targets,
        source=CollectionSource.TRANSCRIPT,
        artifact_kind=ArtifactKind.TEXT_TRANSCRIPT,
    )
    for item in selection.denied:
        sys.stderr.write(
            json.dumps(
                {
                    "event": "source_collection_policy_denied",
                    "ticker": item.target.ticker,
                    "coverage_role": item.target.coverage_role.value,
                    "source": CollectionSource.TRANSCRIPT.value,
                    "artifact_kind": ArtifactKind.TEXT_TRANSCRIPT.value,
                    "reason": item.decision.reason.value,
                },
                sort_keys=True,
            )
            + "\n"
        )
    return frozenset(item.target.ticker for item in selection.allowed)


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
    db_path: Path,
    owner_requested: bool,
    authorized_artifact: AuthorizedTranscriptArtifact | None = None,
) -> QuarterResult:
    del conn, ticker, year, quarter, tracked, dry_run, repo_root, db_path, owner_requested
    if authorized_artifact is None:
        raise TranscriptAcquisitionDeniedError(
            "aggregator refetch has no canonical authorized artifact"
        )
    raise TranscriptAcquisitionDeniedError(
        "aggregator refetch remains denied by the canonical provider policy"
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

    selected_db_path = repo_root / "data" / "portfolio.db"
    conn = open_db(str(selected_db_path))
    try:
        explicit = [t.strip() for t in args.tickers.split(",")] if args.tickers else None
        scope_tickers = _scope_tickers(conn, args.scope, explicit)
        quarters = _roic_quarters_in_scope(scope_tickers)
        sys.stderr.write(
            json.dumps({"event": "plan", "tickers": len(scope_tickers), "quarters": len(quarters)})
            + "\n"
        )

        denied: list[TranscriptAcquisitionAuthorization] = []
        for ticker, year, quarter in quarters:
            receipt = authorize_transcript_request(
                conn,
                TranscriptAcquisitionRequest(
                    entrypoint=TranscriptAcquisitionEntrypoint.REFETCH_AGGREGATOR_TRANSCRIPTS,
                    canonical_ticker=ticker,
                    fiscal_year=year,
                    fiscal_quarter=quarter,
                    as_of=date.today(),
                    source_type=SourceType.IR_DOC,
                    document_type=DocType.EARNINGS_CALL_TRANSCRIPT,
                    provider=TranscriptProvider.ROIC,
                    owner_requested=explicit is not None,
                    existing_artifact=False,
                    existing_artifact_behavior=ExistingArtifactBehavior.REFRESH,
                    source_policy_version=TRANSCRIPT_ACQUISITION_POLICY_VERSION,
                    source_regime_identity=COMBINED_SOURCE_REGIME_IDENTITY,
                ),
            )
            if receipt.status is TranscriptAuthorizationStatus.DENIED:
                denied.append(receipt)
        if denied:
            for receipt in denied:
                sys.stderr.write(
                    json.dumps(
                        {
                            "event": "transcript_acquisition_denied",
                            "entrypoint": receipt.request.entrypoint.value,
                            "ticker": receipt.request.canonical_ticker,
                            "provider": receipt.request.provider.value,
                            "reason": receipt.reason.value,
                            "idempotency_key": receipt.idempotency_key,
                        },
                        sort_keys=True,
                    )
                    + "\n"
                )
            return 2

        results: list[QuarterResult] = []
        for i, (ticker, year, quarter) in enumerate(quarters):
            r = _process_one(
                conn,
                ticker,
                year,
                quarter,
                scope_tickers,
                args.dry_run,
                repo_root,
                selected_db_path,
                explicit is not None,
            )
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
