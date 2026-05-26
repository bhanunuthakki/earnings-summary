"""End-to-end quarterly refresh orchestrator.

Stitches every existing source-specific processor into one ordered pipeline
that is safe to run on a cron. Each stage is independently idempotent —
re-running with no new data is a no-op. Stages requiring an LLM (IR-doc KPI
extraction, commitment extraction from transcripts) are NOT auto-executed;
they get surfaced in `pending_llm_work` so the user can attack them in a
follow-up Claude session.

Per-source bespoke logic:
  - FMP fundamentals (income/balance/cashflow/segments/10-K/10-Q): run
    extract_facts dispatchers -> financial_facts + segment_facts
  - IR transcripts (ir_transcript PDFs): ingest into transcripts +
    transcript_segments via ingest_existing_ir_transcript
  - FMP financial_facts: derive standard KPIs (Op Margin, Net Margin,
    Gross Margin, Revenue YoY USD)
  - Outstanding management_commitments: match against any new kpi_facts
    rows that have arrived since the last refresh
  - Re-run thesis evaluator and write history
  - Surface pending IR PDFs (no kpi_facts join) + transcripts (no
    commitments yet) for the user/LLM to process

Stage statuses are a closed enum (StageStatus) — no freeform strings.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path

from compute.as_reported import extract_as_reported_facts
from compute.balance_sheet import extract_balance_sheet_facts
from compute.cashflow import extract_cashflow_facts
from compute.fmp_derived_kpis import derive_for_ticker
from compute.income_statement import extract_income_statement_facts
from compute.say_do import match_pending as match_commitments_pending
from compute.segment_oi_10k import extract_segment_oi_facts
from compute.segments import extract_segment_facts
from compute.segments_nu import extract_nu_segment_facts
from compute.thesis_evaluator import (
    evaluate_ticker_thesis,
    persist_verdict,
)
from compute.transcript_ingest import (
    ingest_existing_ir_transcript,
)
from models.kpis import BreachStatus
from pipeline.sec_xbrl import CIK_MAP
from pipeline.sec_xbrl import ingest_for_ticker as ingest_sec_for_ticker
from timeseries.signal_writer import compute_and_persist_signals

_FACT_EXTRACTOR_DISPATCH: dict[str, Callable[[sqlite3.Connection, int, Path], int]] = {
    "fmp_income_statement": extract_income_statement_facts,
    "fmp_balance_sheet": extract_balance_sheet_facts,
    "fmp_cashflow": extract_cashflow_facts,
    "fmp_segment_product": extract_segment_facts,
    "fmp_segment_geographic": extract_segment_facts,
    "fmp_as_reported_income": extract_as_reported_facts,
    "fmp_as_reported_balance": extract_as_reported_facts,
    "fmp_as_reported_cashflow": extract_as_reported_facts,
    "fmp_as_reported_financial": extract_as_reported_facts,
    "fmp_10k_json": extract_segment_oi_facts,
    "fmp_10q_json": extract_segment_oi_facts,
}

# Per-ticker extractor overrides (ticker, doc_type) -> Extractor.
# NU's 20-F uses IFRS segment-note conventions that the generic
# segment_oi_10k walker can't decode — see compute/segments_nu.py.
_TICKER_EXTRACTOR_OVERRIDE: dict[
    tuple[str, str], Callable[[sqlite3.Connection, int, Path], int]
] = {
    ("NU", "fmp_10k_json"): extract_nu_segment_facts,
    ("NU", "fmp_10q_json"): extract_nu_segment_facts,
}


class StageName(StrEnum):
    """Closed enum of refresh stages; order matters and matches the pipeline DAG."""

    FETCH_SEC_XBRL = "fetch_sec_xbrl"
    EXTRACT_FMP_FACTS = "extract_fmp_facts"
    INGEST_IR_TRANSCRIPTS = "ingest_ir_transcripts"
    DERIVE_FMP_KPIS = "derive_fmp_kpis"
    MATCH_COMMITMENTS = "match_commitments"
    EVALUATE_THESIS = "evaluate_thesis"
    PERSIST_TIMESERIES_SIGNALS = "persist_timeseries_signals"
    SURFACE_PENDING_LLM = "surface_pending_llm"


class StageStatus(StrEnum):
    """Per-stage outcome enum for the quarterly refresh DAG.

    Distinct from `models.runs.StageStatus` (which is the run/stage_transitions
    enum) — this one adds NEEDS_LLM for stages that surface follow-up work.
    Import as `RefreshStageStatus` from external modules where the run-level
    enum is also in scope.
    """

    OK = "ok"
    SKIPPED = "skipped"
    FAILED = "failed"
    NEEDS_LLM = "needs_llm"


@dataclass(frozen=True)
class StageResult:
    """One stage's outcome for one ticker."""

    name: StageName
    status: StageStatus
    rows_processed: int
    notes: str


@dataclass(frozen=True)
class PendingWorkItem:
    """LLM-needed work surfaced for a follow-up session."""

    kind: str  # "ir_pdf_kpi_extraction" or "transcript_commitment_extraction"
    document_id: int | None
    transcript_id: int | None
    file_path: str | None
    period_end: datetime | None
    note: str


@dataclass(frozen=True)
class TickerRefreshReport:
    """Per-ticker rollup."""

    ticker: str
    stages: tuple[StageResult, ...]
    breach_status: BreachStatus | None
    breach_status_changed: bool
    pending_work: tuple[PendingWorkItem, ...]


@dataclass(frozen=True)
class RefreshReport:
    """Top-level cron output."""

    run_id: str
    started_at: datetime
    ended_at: datetime
    tickers: tuple[TickerRefreshReport, ...]


def _safe_extract(
    extractor: Callable[[sqlite3.Connection, int, Path], int],
    conn: sqlite3.Connection,
    doc_id: int,
    project_root: Path,
) -> int:
    """Wrap an extractor; convert known failure modes into a rowcount of 0."""
    try:
        return extractor(conn, doc_id, project_root)
    except (ValueError, OSError, KeyError, json.JSONDecodeError):
        return 0


def _stage_fetch_sec_xbrl(
    conn: sqlite3.Connection, *, ticker: str, project_root: Path
) -> StageResult:
    """Hit SEC companyfacts API + persist new accessions/financial_facts. Idempotent.

    Skipped for tickers absent from CIK_MAP (e.g., FLKR ETF). Network failures
    return FAILED with the error in notes — does not raise out of the DAG.
    """
    if ticker.upper() not in CIK_MAP:
        return StageResult(
            name=StageName.FETCH_SEC_XBRL, status=StageStatus.SKIPPED,
            rows_processed=0, notes="no CIK in CIK_MAP",
        )
    try:
        stats = ingest_sec_for_ticker(conn, ticker=ticker, project_root=project_root)
    except (OSError, ValueError, KeyError) as e:
        return StageResult(
            name=StageName.FETCH_SEC_XBRL, status=StageStatus.FAILED,
            rows_processed=0, notes=f"{type(e).__name__}: {e}"[:200],
        )
    return StageResult(
        name=StageName.FETCH_SEC_XBRL, status=StageStatus.OK,
        rows_processed=stats.facts_inserted,
        notes=f"{stats.accessions_inserted} accessions; {stats.facts_inserted} new facts",
    )


def _stage_extract_fmp_facts(
    conn: sqlite3.Connection, *, ticker: str, project_root: Path
) -> StageResult:
    """Run every fact-producing FMP extractor for the ticker. Idempotent (INSERT OR IGNORE)."""
    placeholders = ",".join("?" for _ in _FACT_EXTRACTOR_DISPATCH)
    # Order matters: segment extractors gate on financial_facts.revenue from the
    # income-statement extractor — see compute/segments._passes_reconciliation —
    # so income_statement docs must commit first within this stage.
    cur = conn.execute(
        f"SELECT id, doc_type FROM documents "
        f"WHERE ticker = ? AND doc_type IN ({placeholders}) "
        f"ORDER BY CASE doc_type "
        f"  WHEN 'fmp_income_statement' THEN 0 "
        f"  WHEN 'fmp_as_reported_income' THEN 1 "
        f"  ELSE 2 END, id",
        (ticker.upper(), *_FACT_EXTRACTOR_DISPATCH.keys()),
    )
    docs = cur.fetchall()
    if not docs:
        return StageResult(
            name=StageName.EXTRACT_FMP_FACTS, status=StageStatus.SKIPPED,
            rows_processed=0, notes="no FMP fact-producing documents",
        )
    inserted = 0
    failed = 0
    for row in docs:
        doc_id = int(row["id"])
        doc_type = row["doc_type"]
        extractor = (
            _TICKER_EXTRACTOR_OVERRIDE.get((ticker.upper(), doc_type))
            or _FACT_EXTRACTOR_DISPATCH[doc_type]
        )
        n = _safe_extract(extractor, conn, doc_id, project_root)
        if n < 0:
            failed += 1
        else:
            inserted += n
    status = StageStatus.OK if failed == 0 else StageStatus.FAILED
    return StageResult(
        name=StageName.EXTRACT_FMP_FACTS, status=status,
        rows_processed=inserted,
        notes=f"{len(docs)} docs scanned; {inserted} new fact rows; {failed} failures",
    )


def _stage_ingest_ir_transcripts(
    conn: sqlite3.Connection, *, ticker: str, project_root: Path
) -> StageResult:
    """Backfill ir_transcript PDFs into transcripts + transcript_segments."""
    cur = conn.execute(
        "SELECT id, file_path FROM documents "
        "WHERE ticker = ? AND doc_type = 'ir_transcript' "
        "AND id NOT IN (SELECT document_id FROM transcripts)",
        (ticker.upper(),),
    )
    docs = cur.fetchall()
    if not docs:
        return StageResult(
            name=StageName.INGEST_IR_TRANSCRIPTS, status=StageStatus.SKIPPED,
            rows_processed=0, notes="no unprocessed ir_transcript docs",
        )
    processed = 0
    failed = 0
    for row in docs:
        doc_id = int(row["id"])
        abs_path = (project_root / row["file_path"]).resolve()
        if not abs_path.exists():
            failed += 1
            continue
        try:
            ingest_existing_ir_transcript(conn, document_id=doc_id, file_path=abs_path)
            processed += 1
        except (ValueError, OSError):
            failed += 1
    status = StageStatus.OK if failed == 0 else StageStatus.FAILED
    return StageResult(
        name=StageName.INGEST_IR_TRANSCRIPTS, status=status,
        rows_processed=processed,
        notes=f"{processed} new transcripts wired up; {failed} failed",
    )


def _stage_derive_kpis(conn: sqlite3.Connection, *, ticker: str) -> StageResult:
    """Derive standard KPIs from FMP financial_facts for the ticker."""
    try:
        emitted, inserted = derive_for_ticker(conn, ticker)
    except (ValueError, KeyError) as e:
        return StageResult(
            name=StageName.DERIVE_FMP_KPIS, status=StageStatus.FAILED,
            rows_processed=0, notes=f"{type(e).__name__}: {e}"[:200],
        )
    if emitted == 0:
        return StageResult(
            name=StageName.DERIVE_FMP_KPIS, status=StageStatus.SKIPPED,
            rows_processed=0,
            notes="no quarterly fmp_income_statement data on disk",
        )
    return StageResult(
        name=StageName.DERIVE_FMP_KPIS, status=StageStatus.OK,
        rows_processed=inserted,
        notes=f"{emitted} rows derived; {inserted} new (others already present)",
    )


def _stage_match_commitments(
    conn: sqlite3.Connection, *, ticker: str
) -> StageResult:
    """Match outstanding management_commitments against any newly-arrived kpi_facts."""
    results = match_commitments_pending(conn, ticker=ticker)
    if not results:
        return StageResult(
            name=StageName.MATCH_COMMITMENTS, status=StageStatus.SKIPPED,
            rows_processed=0, notes="no pending commitments",
        )
    by_outcome: dict[str, int] = {}
    for r in results:
        by_outcome[r.outcome.value] = by_outcome.get(r.outcome.value, 0) + 1
    summary = ", ".join(f"{k}={v}" for k, v in sorted(by_outcome.items()))
    return StageResult(
        name=StageName.MATCH_COMMITMENTS, status=StageStatus.OK,
        rows_processed=len(results), notes=f"matched {len(results)}: {summary}",
    )


@dataclass(frozen=True)
class _ThesisEvalOutcome:
    status: BreachStatus | None
    changed: bool
    notes: str


def _stage_evaluate_thesis(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    holdings_dir: Path,
    run_id: str,
) -> tuple[StageResult, _ThesisEvalOutcome]:
    """Re-run the thesis evaluator + capture whether breach_status changed."""
    prior_row = conn.execute(
        "SELECT breach_status FROM thesis_state WHERE ticker = ?", (ticker.upper(),)
    ).fetchone()
    prior_status = (
        BreachStatus(prior_row["breach_status"])
        if prior_row is not None and prior_row["breach_status"] is not None
        else None
    )
    try:
        verdict = evaluate_ticker_thesis(
            conn, ticker=ticker, holdings_dir=holdings_dir
        )
    except FileNotFoundError:
        return (
            StageResult(
                name=StageName.EVALUATE_THESIS, status=StageStatus.SKIPPED,
                rows_processed=0, notes="no holdings spec for this ticker",
            ),
            _ThesisEvalOutcome(status=None, changed=False, notes="no holdings spec"),
        )
    except (ValueError, KeyError) as e:
        return (
            StageResult(
                name=StageName.EVALUATE_THESIS, status=StageStatus.FAILED,
                rows_processed=0, notes=f"{type(e).__name__}: {e}"[:200],
            ),
            _ThesisEvalOutcome(status=None, changed=False, notes="eval failed"),
        )
    persist_verdict(conn, verdict, run_id=run_id)
    new_status = verdict.overall_status
    changed = prior_status is not new_status
    return (
        StageResult(
            name=StageName.EVALUATE_THESIS, status=StageStatus.OK,
            rows_processed=1,
            notes=(
                f"{prior_status.value if prior_status else 'none'} -> {new_status.value}"
                + (" (CHANGED)" if changed else "")
            ),
        ),
        _ThesisEvalOutcome(status=new_status, changed=changed, notes=""),
    )


def _stage_persist_timeseries_signals(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    project_root: Path,
    run_id: str,
) -> StageResult:
    """Run the time-series primitives over every metric in scope, persist signals.

    Idempotent via the timeseries_signals unique constraint — each refresh
    overwrites the prior row per (ticker, metric, signal). Best-effort: a
    missing table (migration not applied) or any unexpected error degrades
    to SKIPPED rather than failing the DAG, since signal persistence is a
    derived view and not load-bearing for the brief.
    """
    try:
        n = compute_and_persist_signals(
            ticker=ticker, db=conn, repo_root=project_root, run_id=run_id
        )
    except sqlite3.OperationalError as e:
        return StageResult(
            name=StageName.PERSIST_TIMESERIES_SIGNALS, status=StageStatus.SKIPPED,
            rows_processed=0, notes=f"table missing — run migration: {e}"[:200],
        )
    except (ValueError, KeyError, json.JSONDecodeError) as e:
        return StageResult(
            name=StageName.PERSIST_TIMESERIES_SIGNALS, status=StageStatus.FAILED,
            rows_processed=0, notes=f"{type(e).__name__}: {e}"[:200],
        )
    if n == 0:
        return StageResult(
            name=StageName.PERSIST_TIMESERIES_SIGNALS, status=StageStatus.SKIPPED,
            rows_processed=0, notes="no metrics with sufficient data",
        )
    return StageResult(
        name=StageName.PERSIST_TIMESERIES_SIGNALS, status=StageStatus.OK,
        rows_processed=n, notes=f"{n} signal rows upserted",
    )


_IR_PDF_DOC_TYPES: tuple[str, ...] = (
    "ir_presentation", "ir_press_release", "ir_supplement", "ir_investor_update",
)


def _stage_surface_pending_llm(
    conn: sqlite3.Connection, *, ticker: str
) -> tuple[StageResult, list[PendingWorkItem]]:
    """Find IR PDFs without kpi_facts AND transcripts without management_commitments.

    Returns the stage result + a list of work items the user/LLM should pick up
    in a follow-up session.
    """
    items: list[PendingWorkItem] = []

    placeholders = ",".join("?" for _ in _IR_PDF_DOC_TYPES)
    cur = conn.execute(
        f"SELECT d.id, d.file_path, d.period_end FROM documents d "
        f"WHERE d.ticker = ? AND d.source_type = 'ir_doc' "
        f"AND d.doc_type IN ({placeholders}) "
        f"AND NOT EXISTS (SELECT 1 FROM kpi_facts kf WHERE kf.source_doc_id = d.id) "
        f"ORDER BY d.period_end DESC, d.id",
        (ticker.upper(), *_IR_PDF_DOC_TYPES),
    )
    for row in cur.fetchall():
        pe = row["period_end"]
        if isinstance(pe, str):
            pe = datetime.fromisoformat(pe)
        items.append(
            PendingWorkItem(
                kind="ir_pdf_kpi_extraction",
                document_id=int(row["id"]),
                transcript_id=None,
                file_path=row["file_path"],
                period_end=pe,
                note="LLM should read the PDF and emit a KpiExtractionManifest",
            )
        )

    cur = conn.execute(
        "SELECT t.id, t.period_end, "
        "       (SELECT COUNT(*) FROM transcript_segments s WHERE s.transcript_id = t.id) AS segs "
        "FROM transcripts t "
        "WHERE t.ticker = ? "
        "AND NOT EXISTS ("
        "  SELECT 1 FROM management_commitments mc "
        "  JOIN transcript_segments s ON s.id = mc.transcript_segment_id "
        "  WHERE s.transcript_id = t.id"
        ") "
        "ORDER BY t.period_end DESC",
        (ticker.upper(),),
    )
    for row in cur.fetchall():
        pe = row["period_end"]
        if isinstance(pe, str):
            pe = datetime.fromisoformat(pe)
        items.append(
            PendingWorkItem(
                kind="transcript_commitment_extraction",
                document_id=None,
                transcript_id=int(row["id"]),
                file_path=None,
                period_end=pe,
                note=f"transcript has {int(row['segs'])} segments; LLM should emit a CommitmentExtractionManifest",
            )
        )

    status = StageStatus.NEEDS_LLM if items else StageStatus.SKIPPED
    return (
        StageResult(
            name=StageName.SURFACE_PENDING_LLM, status=status,
            rows_processed=len(items),
            notes=f"{len(items)} pending items for LLM follow-up",
        ),
        items,
    )


def refresh_ticker(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    project_root: Path,
    holdings_dir: Path,
    run_id: str,
    fetch_sec: bool = False,
) -> TickerRefreshReport:
    """Run the full refresh DAG for one ticker.

    `fetch_sec=True` opts the ticker into a network round-trip to SEC's
    companyfacts API at the start of the DAG. Default off so the cron stays
    network-free unless explicitly invoked with --fetch-sec.
    """
    stages: list[StageResult] = []
    if fetch_sec:
        stages.append(_stage_fetch_sec_xbrl(conn, ticker=ticker, project_root=project_root))
    stages.append(_stage_extract_fmp_facts(conn, ticker=ticker, project_root=project_root))
    stages.append(_stage_ingest_ir_transcripts(conn, ticker=ticker, project_root=project_root))
    stages.append(_stage_derive_kpis(conn, ticker=ticker))
    stages.append(_stage_match_commitments(conn, ticker=ticker))
    eval_stage, eval_outcome = _stage_evaluate_thesis(
        conn, ticker=ticker, holdings_dir=holdings_dir, run_id=run_id
    )
    stages.append(eval_stage)
    stages.append(
        _stage_persist_timeseries_signals(
            conn, ticker=ticker, project_root=project_root, run_id=run_id
        )
    )
    pending_stage, pending_items = _stage_surface_pending_llm(conn, ticker=ticker)
    stages.append(pending_stage)

    return TickerRefreshReport(
        ticker=ticker.upper(),
        stages=tuple(stages),
        breach_status=eval_outcome.status,
        breach_status_changed=eval_outcome.changed,
        pending_work=tuple(pending_items),
    )


def refresh_portfolio(
    conn: sqlite3.Connection,
    *,
    tickers: list[str],
    project_root: Path,
    holdings_dir: Path,
    run_id: str,
    fetch_sec: bool = False,
) -> RefreshReport:
    """Run refresh_ticker across the requested ticker scope."""
    started_at = datetime.now()
    reports = [
        refresh_ticker(
            conn, ticker=t, project_root=project_root,
            holdings_dir=holdings_dir, run_id=run_id, fetch_sec=fetch_sec,
        )
        for t in tickers
    ]
    return RefreshReport(
        run_id=run_id, started_at=started_at, ended_at=datetime.now(),
        tickers=tuple(reports),
    )
