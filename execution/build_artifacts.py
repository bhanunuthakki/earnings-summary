"""Build the unified research artifacts for a ticker.

Emits three canonical files under output/research/{TICKER}/{DATE}_*:
  - {DATE}_workspace.html     tabbed workspace report (HTML, primary deliverable;
                              self-contained — opens via file:// or /reports/<ticker>)
  - {DATE}_report.md          markdown source (diff-friendly)
  - {DATE}_sections.json      frontend section payloads (consumed by /api/research/<ticker>)

It also snapshots the standalone document, shared body, and manifest under
output/research/{TICKER}/artifacts/{ARTIFACT_ID}/ so deep links remain immutable
when a same-day report is regenerated.

The DCF workbook is referenced, not re-rendered: the canonical copy lives at
dcf/{TICKER}.xlsx (refreshed by execution/refresh_dcf.py).

Usage:
    python execution/build_artifacts.py --ticker GOOG
    python execution/build_artifacts.py --ticker GOOG --repo-root /abs/path
    python execution/build_artifacts.py --all-tracked

For non-tracked tickers, pass --allow-untracked. The script emits a JSON event
log to stderr (one event per emitted file) and a path summary to stdout.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from contextlib import nullcontext, suppress
from datetime import UTC, date, datetime
from pathlib import Path

try:
    from _lib import PROJECT_ROOT
except ImportError:  # ``python -m execution.build_artifacts``
    from execution._lib import PROJECT_ROOT

import db
import ticker_settings
from compute.key_metrics import (
    extract_for_ticker as _extract_key_metrics,
)
from compute.peer_selection import (
    extract_for_ticker as _extract_peer_selection,
)
from compute.segment_definitions import (
    extract_for_ticker as _extract_segment_definitions,
)
from db_paths import db_path_context, require_db_path
from dcf.input_evidence import canonical_digest
from instrument_store import get_instrument_kind
from models.companies import InstrumentType
from pipeline.db_writer_gate import hold_writer_gate
from provenance.source_read_context import SourceReadContext
from report.artifacts import (
    RenderedReportBody,
    ReportCandidateReview,
    ReportCaptureUnavailable,
    ReportGenerationContext,
    find_matching_candidate_artifact,
    load_report_artifact_index,
    load_report_candidate_review,
    load_report_capture_receipt,
    load_report_file_capture_receipt,
    persist_report_artifact,
    resolve_report_model_binding,
    save_report_candidate_sidecars,
)
from report.builder import build_report
from report.file_capture import (
    CAPTURE_WRITE_SETS,
    FileCaptureMismatchError,
    ReportFileCaptureSession,
    hold_capture_write_set,
    replay_file_capture,
)
from report.generation_context import DBSnapshotRef
from report.models import ReportFlavor
from report.read_capture import (
    CaptureLimits,
    ReadCaptureSession,
    ReadCaptureUnavailable,
    replay_read_capture,
)
from report.render_clock import render_now
from report.renderers.etf_markdown import render as render_etf_markdown
from report.renderers.markdown import render as render_markdown
from report.renderers.sections_json import render as render_sections_json
from report.renderers.workspace_data import (
    filter_important_print_vs_guide,
    parse_print_vs_guide,
)
from report.renderers.workspace_html import (
    render_report_body,
    render_standalone_report,
)
from report.sections import (
    appendix,
    bear_case,
    earnings,
    exec_compensation,
    qa_roster,
    saydo,
    segments,
    thesis,
    valuation,
)
from report.sections import financials as financials_section_mod
from report.sections import snapshot as snapshot_section_mod
from report.sections.etf_holdings import build_etf_brief
from research.decision_brief import (
    assess_candidate_decision_brief,
    persist_decision_brief_readiness,
)
from research.report_contract import report_read_snapshot
from runtime.python_process import managed_python_prefix
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite

# Ticker-specific extractors that auto-populate data/ticker_specific/<T>/
# Each entry maps a ticker to a list of (script_name, extra_args) pairs.
# The dispatcher runs them subprocess-isolated before build_report so the
# per-ticker enhancement JSON is fresh when the bear-case prompt reads it
# via _ticker_specific_md. Idempotent: each extractor is responsible for
# skipping when its own freshness check passes.
_TICKER_SPECIFIC_EXTRACTORS: dict[str, list[tuple[str, list[str]]]] = {
    "NVO": [("extract_nvo_patent_timeline_state.py", [])],
    # Add per-ticker entries here. Keep the script idempotent — the dispatcher
    # will fire on every build for the ticker; cost discipline lives in the
    # script's own freshness check, not here.
}


def _run_ticker_specific_extractors(ticker: str, repo_root: Path) -> None:
    """Subprocess-isolate per-ticker enhancement extractors.

    Failures don't abort the build — they emit a diagnostic event so the
    bear-case prompt falls back to its universal shape without the
    per-ticker context for that ticker. Skipped silently for tickers
    not in _TICKER_SPECIFIC_EXTRACTORS.
    """
    entries = _TICKER_SPECIFIC_EXTRACTORS.get(ticker.upper())
    if not entries:
        return
    for script_name, extra_args in entries:
        cmd = [
            *managed_python_prefix(PROJECT_ROOT),
            str(PROJECT_ROOT / "execution" / script_name),
            "--repo-root",
            str(repo_root),
            "--db",
            str(require_db_path()),
            *extra_args,
        ]
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(repo_root),
                capture_output=True,
                text=True,
                timeout=300,
            )
            _emit(
                "ticker_specific_extractor",
                {
                    "ticker": ticker,
                    "script": script_name,
                    "exit_code": proc.returncode,
                    "stderr_tail": (proc.stderr or "")[-300:],
                },
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            _emit(
                "ticker_specific_extractor_failed",
                {
                    "ticker": ticker,
                    "script": script_name,
                    "error": f"{type(exc).__name__}: {exc}",
                },
            )


def _ensure_segment_definitions(ticker: str, repo_root: Path) -> None:
    """Idempotently populate data/segment_definitions/<T>.json before render.

    Hashes the latest form_10k JSON; if the cache already matches the sha,
    returns in milliseconds. Otherwise fires one Haiku call (~20s) and
    writes the cache. Failures don't abort the build — they emit a
    diagnostic and leave the tooltips empty for this ticker.
    """
    db_path = require_db_path()
    if not db_path.exists():
        _emit("segment_defs_skipped", {"ticker": ticker, "reason": "no DB"})
        return
    conn = connect_sqlite(str(db_path), role=SQLiteConnectionRole.READ_ONLY)
    conn.row_factory = sqlite3.Row
    try:
        result = _extract_segment_definitions(ticker, repo_root, conn)
        _emit(
            "segment_defs_refreshed",
            {
                "ticker": ticker,
                "fiscal_year": result.fiscal_year,
                "definitions_found": sum(1 for v in result.definitions.values() if v),
                "skipped": result.skipped_reason,
            },
        )
    except Exception as e:
        _emit(
            "segment_defs_error",
            {
                "ticker": ticker,
                "error": f"{type(e).__name__}: {e}",
            },
        )
    finally:
        conn.close()


def _ensure_peer_selection(ticker: str, repo_root: Path) -> None:
    """Idempotently populate data/peer_selection/<T>.json before render (LLM
    builds only).

    Keyed on a hash of the LLM inputs (name + business description + segments +
    sector/industry); a fast no-op when unchanged. On a fresh input it fires one
    peer_selection call (~Sonnet) for business-model comparables and a best-effort
    free-tier FMP fetch of each suggestion's fundamentals. Failures don't abort
    the build — the renderer falls back to the FMP sector/cap screen for this
    ticker (directives/peer_selection_llm.md)."""
    db_path = require_db_path()
    if not db_path.exists():
        _emit("peer_selection_skipped", {"ticker": ticker, "reason": "no DB"})
        return
    conn = connect_sqlite(str(db_path), role=SQLiteConnectionRole.READ_ONLY)
    conn.row_factory = sqlite3.Row
    try:
        result = _extract_peer_selection(ticker, repo_root, conn)
        _emit(
            "peer_selection_refreshed",
            {
                "ticker": ticker,
                "suggested": len(result.suggestions),
                "fetched": len(result.fetched_peers),
                "fetched_complete": len(result.fetched_complete),
                "skipped": result.skipped_reason,
            },
        )
    except Exception as e:
        _emit("peer_selection_error", {"ticker": ticker, "error": f"{type(e).__name__}: {e}"})
    finally:
        conn.close()


def _ensure_key_metrics(ticker: str, repo_root: Path) -> None:
    """Idempotently populate data/key_metrics/<T>.json before render (LLM builds
    only).

    Keyed on a hash of the LLM inputs (name + business description + the picker
    catalog vocabulary); a fast no-op when unchanged. On a fresh input it fires
    one key_metrics call (~Sonnet) ranking the metrics MOST important to the
    business over its available extract vocabulary. Failures don't abort the
    build — the DIY picker's preselect row falls back to the deterministic
    tier-graded baseline for this ticker (directives/key_metrics_picker.md)."""
    db_path = require_db_path()
    if not db_path.exists():
        _emit("key_metrics_skipped", {"ticker": ticker, "reason": "no DB"})
        return
    conn = connect_sqlite(str(db_path), role=SQLiteConnectionRole.READ_ONLY)
    conn.row_factory = sqlite3.Row
    try:
        result = _extract_key_metrics(ticker, repo_root, conn)
        _emit(
            "key_metrics_refreshed",
            {
                "ticker": ticker,
                "metrics": len(result.metrics),
                "skipped": result.skipped_reason,
            },
        )
    except Exception as e:
        _emit("key_metrics_error", {"ticker": ticker, "error": f"{type(e).__name__}: {e}"})
    finally:
        conn.close()


def configure_artifact_runtime(repo_root: Path) -> None:
    """Keep artifact roots separate from the explicitly configured database."""
    database_path = require_db_path()
    db.set_db_path(database_path, state_root=repo_root)
    db.PROJECT_ROOT = str(repo_root)
    db.DATA_DIR = str(repo_root / "data")
    db.FMP_DIR = str(repo_root / "data" / "historical" / "fmp")


def main() -> int:
    args = _parse_args()
    repo_root = args.repo_root.resolve()
    source_state_root = getattr(args, "source_state_root", None)
    source_context = (
        SourceReadContext.for_product_state_root(source_state_root)
        if source_state_root is not None
        else None
    )
    database_path = require_db_path(args.db_path)
    db.set_db_path(database_path)
    os.environ["EARNINGS_SUMMARY_DB_PATH"] = str(database_path)
    configure_artifact_runtime(repo_root)
    if args.candidate:
        result = build_candidate_artifact(
            args.ticker,
            repo_root,
            db_path=database_path,
            source_context=source_context,
            model_run_id=args.model_run_id,
            expected_current_artifact_id=args.expected_current_artifact_id,
        )
        print(json.dumps([result], indent=2, default=str))
        return 0
    tickers = _resolve_tickers(repo_root, args)
    if not tickers:
        print("[]")
        return 0

    flavor = ReportFlavor(args.flavor)
    summary: list[dict[str, object]] = []
    for ticker in tickers:
        if args.regenerate_purpose is not None:
            summary.append(
                _regenerate_native_purpose(
                    ticker,
                    repo_root,
                    purpose=args.regenerate_purpose,
                    force_budget_bypass=args.force_budget_bypass,
                    source_context=source_context,
                )
            )
            continue
        result = _build_one(
            ticker,
            repo_root,
            enable_llm=args.enable_llm,
            news_days=args.news_days,
            news_cache_ttl_days=args.news_cache_ttl_days,
            refresh_news=args.refresh_news,
            flavor=flavor,
            trigger=args.trigger,
            force_budget_bypass=args.force_budget_bypass,
            force_refresh=args.force_refresh,
            source_context=source_context,
        )
        summary.append(result)
    print(json.dumps(summary, indent=2, default=str))
    return 0


def build_candidate_artifact(
    ticker: str,
    repo_root: Path,
    *,
    db_path: Path | None = None,
    source_context: SourceReadContext | None = None,
    model_run_id: int | None = None,
    cutoff_at: datetime | None = None,
    expected_current_artifact_id: str | None = None,
) -> dict[str, object]:
    symbol = ticker.strip().upper()
    active_db = require_db_path(db_path)
    with db_path_context(active_db), hold_capture_write_set(repo_root, CAPTURE_WRITE_SETS):
        if _resolve_kind(repo_root, symbol) is InstrumentType.ETF:
            raise ValueError("candidate_etf_unsupported")
        configure_artifact_runtime(repo_root)
        read_session = ReadCaptureSession(active_db, limits=CaptureLimits())
        file_session = ReportFileCaptureSession(repo_root=repo_root)
        conn = read_session.open()
        conn.row_factory = sqlite3.Row
        read_session.begin_capture()
        binding = resolve_report_model_binding(
            repo_root, symbol, conn=conn, model_run_id=model_run_id
        )
        if isinstance(binding, ReportCaptureUnavailable):
            read_session.abort()
            return {
                "status": "capture_unavailable",
                "ticker": symbol,
                "reason": binding.reason_code,
            }

        # Check for active candidate reuse if cutoff_at was not explicitly provided
        if cutoff_at is None:
            active_candidates = [
                item
                for item in sorted(
                    load_report_artifact_index(repo_root).items,
                    key=lambda it: (it.generated_at, it.artifact_id),
                    reverse=True,
                )
                if item.ticker == symbol
                and item.publication_status == "draft_preview"
                and item.reader_mode == "shared_body"
                and isinstance(item.generation_context, ReportGenerationContext)
                and item.body_path is not None
                and (repo_root / item.standalone_path).is_file()
                and (repo_root / item.body_path).is_file()
            ]
            if active_candidates:
                latest_cand = active_candidates[0]
                cand_ctx = latest_cand.generation_context
                if (
                    isinstance(cand_ctx, ReportGenerationContext)
                    and cand_ctx.model == binding
                    and (
                        expected_current_artifact_id is None
                        or cand_ctx.expected_current_artifact_id == expected_current_artifact_id
                    )
                ):
                    cand_capture = load_report_capture_receipt(
                        repo_root, symbol, latest_cand.artifact_id
                    )
                    cand_file_receipt = load_report_file_capture_receipt(
                        repo_root, symbol, latest_cand.artifact_id
                    )
                    cand_review = load_report_candidate_review(
                        repo_root, symbol, latest_cand.artifact_id
                    )
                    if (
                        cand_capture is not None
                        and cand_file_receipt is not None
                        and cand_review is not None
                    ):
                        try:
                            replay_read_capture(active_db, cand_capture, limits=CaptureLimits())
                            replay_file_capture(cand_file_receipt, repo_root)
                            read_session.abort()
                            return {
                                "status": "reused",
                                "reused": True,
                                "ticker": symbol,
                                "artifact_id": latest_cand.artifact_id,
                                "context_sha256": cand_ctx.sha256,
                                "body_sha256": latest_cand.body_sha256,
                                "candidate_review": cand_review.model_dump(mode="json"),
                            }
                        except (ReadCaptureUnavailable, FileCaptureMismatchError, OSError):
                            pass

        for sel in (
            "quarterly_summaries_tmp_v1",
            "saydo_cards_tmp_v1",
            "transcripts_dual_dir_v1",
            "ir_briefs_tmp_v1",
            "fmp_price_chart_v1",
        ):
            file_session.record_directory_selection(sel, symbol)

        candidate_file_targets: list[str] = [
            f"micro_thesis/holdings/{symbol}.json",
            f".tmp/news_cache/{symbol}.json",
            f"dcf/{symbol}.xlsx",
            f"dcf/{symbol}_assumptions.json",
            f"data/dcf_assumptions/{symbol}.json",
            f"data/bear_case/{symbol}.json",
            f"data/peer_selection/{symbol}.json",
            f"data/peer_fundamentals/{symbol}.json",
            f"data/segments/{symbol}.json",
            f"data/segment_definitions/{symbol}.json",
            f"data/filing_intelligence/{symbol}.json",
            f"data/qa_topics/{symbol}.json",
        ]
        tmp_dir = repo_root / ".tmp"
        if tmp_dir.is_dir():
            for p in sorted(tmp_dir.iterdir()):
                if p.is_file() and (
                    p.name.upper().startswith(f"{symbol}_")
                    or p.name.upper().startswith(f"SAYDO_{symbol}_")
                ):
                    candidate_file_targets.append(f".tmp/{p.name}")
        for subdir in ("processed", "raw"):
            tr_dir = repo_root / "transcripts" / subdir
            if tr_dir.is_dir():
                for p in sorted(tr_dir.iterdir()):
                    if p.is_file() and p.name.upper().startswith(f"{symbol}_"):
                        candidate_file_targets.append(f"transcripts/{subdir}/{p.name}")
        for target in candidate_file_targets:
            with suppress(FileNotFoundError):
                file_session.read_bytes(target)

        effective_cutoff = cutoff_at or render_now()
        with file_session:
            spec = build_report(
                ticker=symbol,
                repo_root=repo_root,
                model_link=f"dcf/{symbol}.xlsx",
                enable_llm=False,
                refresh_news=False,
                force_refresh=False,
                conn=conn,
                db_path=active_db,
                source_context=source_context,
                model_run_id=binding.row_id,
                candidate_mode=True,
                cutoff_at=effective_cutoff,
            )

            # Render body while conn is still inside read_session to capture any panel reads
            temp_body = render_report_body(
                spec,
                conn=conn,
                candidate_mode=True,
                generation_context_sha256="__CANDIDATE_CONTEXT_PLACEHOLDER__",
            )
        capture_receipt = read_session.finish()
        file_receipt = file_session.seal()

        generation_context = ReportGenerationContext(
            schema_version="report_generation.v2",
            model=binding,
            cutoff_at=effective_cutoff,
            source_manifest_sha256=file_receipt.receipt_digest,
            evidence_snapshot_id=f"capture:{binding.row_id}:{file_receipt.receipt_digest[:16]}",
            evidence_member_sha256=file_receipt.receipt_digest,
            recipe_versions=(("workspace_html", "v1"),),
            expected_current_artifact_id=expected_current_artifact_id,
            db_snapshot=DBSnapshotRef(
                schema_sha256=capture_receipt.schema_sha256,
                consumed_rows_sha256=canonical_digest(
                    [s.model_dump(mode="json") for s in capture_receipt.statements]
                ),
            ),
            report_date=spec.generation_date,
            coverage_role=("evaluation" if spec.flavor is ReportFlavor.EVALUATION else "portfolio"),
            source_authority_sha256=hashlib.sha256(str(repo_root).encode("utf-8")).hexdigest(),
        )
        existing = find_matching_candidate_artifact(
            repo_root, ticker=symbol, context_sha256=generation_context.sha256
        )
        if existing is not None:
            existing_review = load_report_candidate_review(repo_root, symbol, existing.artifact_id)
            return {
                "status": "reused",
                "reused": True,
                "ticker": symbol,
                "artifact_id": existing.artifact_id,
                "context_sha256": generation_context.sha256,
                "body_sha256": existing.body_sha256,
                "candidate_review": (
                    existing_review.model_dump(mode="json") if existing_review else None
                ),
            }

        final_body_html = temp_body.body_html.replace(
            "__CANDIDATE_CONTEXT_PLACEHOLDER__", generation_context.sha256
        )
        report_body = RenderedReportBody.from_html(
            ticker=temp_body.ticker,
            report_date=temp_body.report_date,
            body_html=final_body_html,
            sections=temp_body.sections,
            interaction_manifest=temp_body.interaction_manifest,
            fiscal_period_label=temp_body.fiscal_period_label,
        )
        standalone_html = render_standalone_report(
            spec,
            report_body,
            candidate_mode=True,
            generation_context_sha256=generation_context.sha256,
        )
        temp_dir = repo_root / ".tmp"
        temp_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", suffix=".html", dir=temp_dir, delete=False
        ) as handle:
            handle.write(standalone_html)
            tmp_standalone = Path(handle.name)
        try:
            artifact_ref = persist_report_artifact(
                repo_root=repo_root,
                body=report_body,
                standalone_path=tmp_standalone,
                generated_at=generation_context.cutoff_at,
                coverage_role=generation_context.coverage_role or "portfolio",
                title=f"{symbol} Full Research Brief",
                publication_status="draft_preview",
                generation_context=generation_context,
            )
        finally:
            tmp_standalone.unlink(missing_ok=True)

        with connect_sqlite(active_db, role=SQLiteConnectionRole.READ_ONLY) as assess_conn:
            readiness = assess_candidate_decision_brief(
                assess_conn,
                repo_root=repo_root,
                artifact=artifact_ref,
                as_of=generation_context.cutoff_at,
                source_context=source_context,
            )
        if readiness.context_review_sha256 is None:
            readiness = readiness.model_copy(
                update={"context_review_sha256": generation_context.sha256}
            )
        readiness_path = persist_decision_brief_readiness(repo_root, readiness)
        is_ready = readiness.status == "verified" and not readiness.reason_codes
        candidate_url = f"/reports/{symbol}?artifact_id={artifact_ref.artifact_id}&standalone=1"
        review = ReportCandidateReview(
            artifact_id=artifact_ref.artifact_id,
            body_sha256=str(artifact_ref.body_sha256),
            workspace_sha256=artifact_ref.workspace_sha256,
            context_sha256=generation_context.sha256,
            model_sha256=generation_context.model_sha256,
            evidence_sha256=generation_context.evidence_sha256,
            content_receipt_path=str(readiness_path.relative_to(repo_root).as_posix()),
            content_receipt_sha256=hashlib.sha256(readiness_path.read_bytes()).hexdigest(),
            verification_contract="decision_brief.v1",
            evaluated_at=generation_context.cutoff_at,
            ready=is_ready,
            reason_codes=tuple(readiness.reason_codes),
            url=candidate_url,
        )
        save_report_candidate_sidecars(
            repo_root,
            ticker=symbol,
            artifact_id=artifact_ref.artifact_id,
            capture_receipt=capture_receipt,
            file_capture_receipt=file_receipt,
            readiness=readiness,
            candidate_review=review,
        )
        return {
            "status": "ok",
            "reused": False,
            "ticker": symbol,
            "artifact_id": artifact_ref.artifact_id,
            "context_sha256": generation_context.sha256,
            "body_sha256": artifact_ref.body_sha256,
            "candidate_review": review.model_dump(mode="json"),
        }


def _regenerate_native_purpose(
    ticker: str,
    repo_root: Path,
    *,
    purpose: str,
    force_budget_bypass: bool,
    source_context: SourceReadContext | None = None,
) -> dict[str, object]:
    """Run exactly one purpose producer without rendering a report."""
    configure_artifact_runtime(repo_root)
    effective_bypass = force_budget_bypass or ticker_settings.get_bypass_budget(
        ticker,
        db_path=require_db_path(),
    )
    db_path = require_db_path()
    conn = (
        connect_sqlite(db_path, role=SQLiteConnectionRole.READ_ONLY) if db_path.exists() else None
    )
    if conn is not None:
        conn.row_factory = sqlite3.Row
    try:
        if purpose == "bear_case":
            thesis_section = thesis.build(ticker, repo_root, conn=conn)
            financials_section = financials_section_mod.build(
                ticker, repo_root, conn=conn, source_context=source_context
            )
            segments_section = segments.build(ticker, repo_root, conn=conn)
            earnings_section = earnings.build(
                ticker,
                repo_root,
                enable_llm=False,
                conn=conn,
            )
            section = bear_case.build(
                ticker=ticker,
                repo_root=repo_root,
                enable_llm=True,
                thesis=thesis_section,
                financials=financials_section,
                segments=segments_section,
                earnings=earnings_section,
                force_refresh=True,
                force_budget_bypass=effective_bypass,
                conn=conn,
            )
        elif purpose == "qa_topics":
            earnings_section = earnings.build(
                ticker,
                repo_root,
                enable_llm=False,
                conn=conn,
            )
            section = qa_roster.build(
                appendix=appendix.build(earnings_section),
                ticker=ticker,
                repo_root=repo_root,
                enable_llm=True,
                force_budget_bypass=effective_bypass,
                force_refresh=True,
            )
        elif purpose == "valuation_basis":
            section = valuation.build(
                ticker=ticker,
                repo_root=repo_root,
                enable_llm=True,
                force_budget_bypass=effective_bypass,
                force_refresh=True,
                conn=conn,
            )
        elif purpose == "saydo_filter":
            section = saydo.build(ticker, repo_root, conn=conn)
            generated = 0
            for card in section.cards:
                rows = parse_print_vs_guide(card)
                if not rows:
                    continue
                filter_important_print_vs_guide(
                    ticker=ticker,
                    repo_root=repo_root,
                    card=card,
                    rows=rows,
                    enable_llm=True,
                    force_refresh=True,
                    strict_refresh=True,
                )
                generated += 1
            if generated == 0:
                raise RuntimeError(f"saydo_filter has no parseable commitments for {ticker}")
        elif purpose == "exec_comp_alignment":
            section = exec_compensation.build(
                ticker,
                repo_root,
                enable_llm=True,
                force_budget_bypass=effective_bypass,
                conn=conn,
            )
        else:
            raise ValueError(f"unsupported native artifact purpose: {purpose}")
    finally:
        if conn is not None:
            conn.close()
    return {
        "ticker": ticker,
        "purpose": purpose,
        "status": section.status.value,
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--db-path", type=Path, help="Explicit existing portfolio database")
    parser.add_argument("--candidate", action="store_true")
    parser.add_argument("--model-run-id", type=int)
    parser.add_argument("--expected-current-artifact-id", type=str)
    g = parser.add_mutually_exclusive_group(required=True)
    g.add_argument("--ticker", help="Single ticker to build for")
    g.add_argument(
        "--all-tracked", action="store_true", help="Build for all rows in tracked_companies"
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=PROJECT_ROOT,
        help="Repo root containing data/, transcripts/, micro_thesis/. Default: this repo.",
    )
    parser.add_argument(
        "--source-state-root",
        type=Path,
        help="Explicit product state root for sealed financial source-byte reads.",
    )
    parser.add_argument(
        "--allow-untracked",
        action="store_true",
        help="Build even if ticker not in tracked_companies",
    )
    parser.add_argument(
        "--enable-llm",
        action="store_true",
        help="Run the §8 recent-developments and §9 bear-case LLM calls. Routes "
        "through llm_client: Claude CLI first, Gemini Flash fallback if Claude fails. "
        "Without --enable-llm, both sections are stubbed.",
    )
    parser.add_argument(
        "--news-days",
        type=int,
        default=7,
        help="Lookback window (days) for the §8 recent-developments WebSearch. Default 7.",
    )
    parser.add_argument(
        "--news-cache-ttl-days",
        type=int,
        default=7,
        help="How long the §8 news cache stays fresh between regenerations. Default 7.",
    )
    parser.add_argument(
        "--refresh-news",
        action="store_true",
        help="Force a fresh WebSearch for §8 (bypasses the cache for this build).",
    )
    parser.add_argument(
        "--force-refresh",
        action="store_true",
        help="Bypass the on-disk cache for the IR-anchor LLM sections (bear case §7 + "
        "recent developments §8) so they re-run with the latest fetched documents.",
    )
    parser.add_argument(
        "--regenerate-purpose",
        choices=(
            "bear_case",
            "exec_comp_alignment",
            "qa_topics",
            "saydo_filter",
            "valuation_basis",
        ),
        default=None,
        help=(
            "Internal dirty-artifact drain seam: run only the named purpose "
            "producer so unrelated LLM sections remain cache-contained."
        ),
    )
    parser.add_argument(
        "--flavor",
        choices=[f.value for f in ReportFlavor],
        default=ReportFlavor.PORTFOLIO.value,
        help=(
            "Brief shape. 'portfolio' (default) renders the full Snapshot at §1. "
            "'evaluation' renders an EvaluationSnapshot (3y quick-categorization "
            "data table) at §1 instead — for new-name screening."
        ),
    )
    parser.add_argument(
        "--trigger",
        choices=("earnings", "news_refresh", "manual", "on_demand", "daily_worker"),
        default="manual",
        help=(
            "What triggered this brief build. Logged to brief_provenance_log for audit. "
            "Daily worker passes 'daily_worker'; refresh_news.py passes 'news_refresh'."
        ),
    )
    parser.add_argument(
        "--renderer",
        choices=("default", "workspace", "both"),
        default="workspace",
        help=(
            "Compatibility no-op. The legacy long-form renderer was retired; "
            "every value emits only {DATE}_workspace.html via the tabbed "
            "workspace renderer. 'default'/'both' remain accepted so existing "
            "scripts and scheduled tasks don't crash."
        ),
    )
    parser.add_argument(
        "--force-budget-bypass",
        action="store_true",
        help=(
            "Ignore per-purpose LLM budget caps for this build — the 'run anyway' "
            "override. Analyses a skip-mode cap would forgo are run regardless."
        ),
    )
    return parser.parse_args()


def _resolve_tickers(repo_root: Path, args: argparse.Namespace) -> list[str]:
    if args.ticker:
        ticker = args.ticker.upper()
        if not args.allow_untracked and not _is_tracked(repo_root, ticker):
            _emit(
                "warn_untracked",
                {"ticker": ticker, "hint": "pass --allow-untracked to build anyway"},
            )
        return [ticker]
    return _all_tracked(repo_root)


def _is_tracked(repo_root: Path, ticker: str) -> bool:
    db_path = require_db_path()
    if not db_path.exists():
        return False
    conn = connect_sqlite(str(db_path), role=SQLiteConnectionRole.READ_ONLY)
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM tracked_companies WHERE ticker = ? LIMIT 1", (ticker,))
    row = cursor.fetchone()
    conn.close()
    return row is not None


def _all_tracked(repo_root: Path) -> list[str]:
    db_path = require_db_path()
    if not db_path.exists():
        return []
    conn = connect_sqlite(str(db_path), role=SQLiteConnectionRole.READ_ONLY)
    cursor = conn.cursor()
    cursor.execute(
        f"SELECT DISTINCT ticker FROM tracked_companies "
        f"WHERE list_type IN {db.ACTIVE_LIST_TYPES_SQL} ORDER BY ticker"
    )
    rows = cursor.fetchall()
    conn.close()
    return [r[0] for r in rows]


def _build_one(
    ticker: str,
    repo_root: Path,
    enable_llm: bool,
    news_days: int = 7,
    news_cache_ttl_days: int = 7,
    refresh_news: bool = False,
    flavor: ReportFlavor = ReportFlavor.PORTFOLIO,
    trigger: str = "manual",
    force_budget_bypass: bool = False,
    force_refresh: bool = False,
    source_context: SourceReadContext | None = None,
    model_run_id: int | None = None,
) -> dict[str, object]:
    out_dir = repo_root / "output" / "research" / ticker
    out_dir.mkdir(parents=True, exist_ok=True)
    today = date.today().isoformat()
    workspace_html_path = out_dir / f"{today}_workspace.html"
    md_path = out_dir / f"{today}_report.md"
    json_path = out_dir / f"{today}_sections.json"
    # The DCF model is the canonical redesigned workbook at dcf/<T>.xlsx (refreshed
    # by execution/refresh_dcf.py). The brief references it rather than re-rendering
    # a per-date copy here; the dashboard serves it via the /dcf/<T> route.
    canonical_dcf = repo_root / "dcf" / f"{ticker}.xlsx"

    configure_artifact_runtime(repo_root)

    # Cross-asset dispatch: ETF tickers get the ETF brief shape (no DCF,
    # no segments, no 10-K narrative). See directives/cross_asset_data_model.md.
    if _resolve_kind(repo_root, ticker) is InstrumentType.ETF:
        return _build_etf(ticker, repo_root, md_path, today)

    # Refresh quarterly_artifacts (audio/transcript/release/slides + step_* flags)
    # from the filesystem before the provenance section reads them. This is what
    # links the coverage matrix to the DB — without this, new .tmp summaries and
    # SayDo files don't land in the table until something else syncs. The sync
    # writes, so it runs inside the chain's cross-process writer window (the
    # daily brief worker overlaps sibling build steps on a bounded pool).
    with hold_writer_gate(require_db_path(), owner=f"build_artifacts:sync:{ticker}"):
        db.scan_and_sync_artifacts(ticker)
    _emit("synced_quarterly_artifacts", {"ticker": ticker})

    # This extractor can call an LLM when the source changes. Cached definitions
    # remain available to acquisition-only builds without an implicit LLM call.
    if enable_llm:
        _ensure_segment_definitions(ticker, repo_root)

    # Per-ticker enhancement extractors (e.g. extract_nvo_patent_timeline.py).
    # Auto-populates data/ticker_specific/<T>/ so the bear-case prompt's
    # ticker_specific block is fresh. Subprocess-isolated; failures land
    # in stderr but don't abort the build.
    if enable_llm:
        _run_ticker_specific_extractors(ticker, repo_root)

    # LLM business-model peer selection (the §4 peer-comp generator). LLM build
    # only — it makes an LLM call + a free-tier FMP fetch; input-sha-keyed so
    # it's a fast no-op when the inputs are unchanged. The renderer degrades to
    # the FMP screen when the cache is absent, so this is purely additive.
    if enable_llm:
        _ensure_peer_selection(ticker, repo_root)
        # LLM key-metrics preselect for the DIY picker (key_metrics_picker.md).
        # Same discipline: LLM build only, input-sha-keyed no-op when unchanged,
        # renderer degrades to the tier-graded baseline when the cache is absent.
        _ensure_key_metrics(ticker, repo_root)

    # Persistent per-ticker override: the dashboard's "always ignore caps" toggle
    # (ticker_settings.bypass_budget) ORs with the one-shot --force-budget-bypass.
    effective_bypass = force_budget_bypass or ticker_settings.get_bypass_budget(
        ticker, db_path=require_db_path()
    )
    report_db_path = require_db_path()
    report_conn = (
        connect_sqlite(report_db_path, role=SQLiteConnectionRole.READ_ONLY)
        if report_db_path.exists()
        else None
    )
    if report_conn is not None:
        report_conn.row_factory = sqlite3.Row
    try:
        # One caller-owned READ_ONLY connection + read snapshot spans section
        # building, per-metric provenance, and the P3 panel reads that go
        # through the caller connection, so those reads reflect one database
        # version. This is a bounded seam, NOT full-generation capture: the
        # prompt-quality sources tab (guesses repo/data/portfolio.db), the
        # graded-sell base rate, Markdown standing rules, and the comment
        # boot file each re-open their own handles and stay obligations for
        # FULL capture later. An explicit `model_run_id` pins the valuation
        # basis to one dcf_runs.id; None keeps the historical latest-run
        # semantics. The connection is closed exactly once below even if
        # rendering raises.
        snapshot_ctx = (
            report_read_snapshot(report_conn) if report_conn is not None else nullcontext()
        )
        with snapshot_ctx:
            spec = build_report(
                ticker=ticker,
                repo_root=repo_root,
                model_link=f"dcf/{ticker}.xlsx",
                enable_llm=enable_llm,
                news_days=news_days,
                news_cache_ttl_days=news_cache_ttl_days,
                refresh_news=refresh_news,
                flavor=flavor,
                force_budget_bypass=effective_bypass,
                force_refresh=force_refresh,
                conn=report_conn,
                db_path=report_db_path,
                source_context=source_context,
                model_run_id=model_run_id,
            )
            per_metric_provenance = _collect_per_metric_provenance(
                ticker, repo_root, conn=report_conn, model_run_id=model_run_id
            )
            report_body = render_report_body(spec, conn=report_conn)
    finally:
        if report_conn is not None:
            report_conn.close()

    workspace_html_path.write_text(render_standalone_report(spec, report_body), encoding="utf-8")
    _emit(
        "wrote_workspace_html",
        {"ticker": ticker, "path": str(workspace_html_path)},
    )

    md_path.write_text(render_markdown(spec), encoding="utf-8")
    _emit("wrote_markdown", {"ticker": ticker, "path": str(md_path)})

    json_path.write_text(render_sections_json(spec), encoding="utf-8")
    _emit("wrote_sections_json", {"ticker": ticker, "path": str(json_path)})

    sections_status = {
        "snapshot": spec.snapshot.status.value,
        "company_description": spec.company_description.status.value,
        "thesis": spec.thesis.status.value,
        "financials": spec.financials.status.value,
        "segments": spec.segments.status.value,
        "earnings": spec.earnings.status.value,
        "saydo": spec.saydo.status.value,
        "ir_docs": spec.ir_docs.status.value,
        "recent_developments": spec.recent_developments.status.value,
        "bear_case": spec.bear_case.status.value,
        "provenance": spec.provenance.status.value,
    }
    # Provenance rows always point at the workspace HTML — the only rendered
    # report — so audit consumers can deep-link. The INSERT writes to the
    # database, so it runs inside the chain's cross-process writer window.
    canonical_artifact = workspace_html_path
    with hold_writer_gate(require_db_path(), owner=f"build_artifacts:provenance:{ticker}"):
        provenance_ref = log_brief_provenance(
            repo_root=repo_root,
            ticker=ticker,
            generation_date=today,
            sections_status=sections_status,
            per_metric=per_metric_provenance,
            trigger=trigger,
            artifact_path=canonical_artifact,
        )
    artifact_ref = persist_report_artifact(
        repo_root=repo_root,
        body=report_body,
        standalone_path=workspace_html_path,
        generated_at=datetime.now(UTC),
        coverage_role=("evaluation" if spec.flavor is ReportFlavor.EVALUATION else "portfolio"),
        title=f"{ticker} Full Research Brief",
        provenance_ref=provenance_ref,
    )
    _emit(
        "wrote_report_manifest",
        {
            "ticker": ticker,
            "artifact_id": artifact_ref.artifact_id,
            "manifest_path": artifact_ref.manifest_path,
            "body_path": artifact_ref.body_path,
        },
    )

    return {
        "ticker": ticker,
        "workspace_html": str(workspace_html_path),
        "report_md": str(md_path),
        "sections_json": str(json_path),
        "report_body": str(repo_root / (artifact_ref.body_path or "")),
        "report_manifest": str(repo_root / artifact_ref.manifest_path),
        "dcf_xlsx": str(canonical_dcf),
        "section_status": sections_status,
    }


def _collect_per_metric_provenance(
    ticker: str,
    repo_root: Path,
    *,
    conn: sqlite3.Connection | None = None,
    model_run_id: int | None = None,
) -> dict[str, dict[str, object]]:
    """Collate per-line-item provenance across the sections wired for it.

    Today: snapshot (DCF valuation) + financials (8 quarterly line items).
    Other sections are expansion territory — sparse merge keeps the dict
    shape stable. Each section's builder returns {} when it has no facts
    to surface, so the unioned dict is naturally sparse for new tickers
    or synthetic test environments.
    """
    merged: dict[str, dict[str, object]] = {}
    merged.update(
        snapshot_section_mod.build_per_metric(
            ticker, repo_root, conn=conn, model_run_id=model_run_id
        )
    )
    merged.update(financials_section_mod.build_per_metric(ticker, repo_root, conn=conn))
    return merged


def log_brief_provenance(
    *,
    repo_root: Path,
    ticker: str,
    generation_date: str,
    sections_status: dict[str, str],
    trigger: str,
    artifact_path: Path,
    per_metric: dict[str, dict[str, object]] | None = None,
    db_path: Path | None = None,
) -> int | None:
    """Append a `brief_provenance_log` row for the render.

    `sources_used` is a JSON envelope with two keys:

      * `sections` — section-level status snapshot (LIVE / STUB / etc.),
        unchanged from the original §section-level audit.
      * `per_metric` — per-line-item provenance: which fact row drove
        each rendered metric. Sparse: only metrics whose section opted
        into provenance reporting (snapshot + financials today) appear.
        Each value is `{"source": <tier>, "fact_id": <id>,
        "fetched_at": <iso>, ...}` — see
        `load_financial_fact_provenance` / `snapshot.build_per_metric`
        for the exact shape per source.

    Silently skips when the table is missing (synthetic environments
    without migrations applied).
    """
    db_path = require_db_path(db_path)
    if not db_path.exists():
        return None
    conn = connect_sqlite(str(db_path), role=SQLiteConnectionRole.WRITER, schema_preflight=True)
    try:
        table_present = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='brief_provenance_log' LIMIT 1"
        ).fetchone()
        if table_present is None:
            return None
        try:
            rel_artifact = str(artifact_path.relative_to(repo_root))
        except ValueError:
            rel_artifact = str(artifact_path)
        sources_used: dict[str, object] = {"sections": sections_status}
        if per_metric:
            sources_used["per_metric"] = per_metric
        cursor = conn.execute(
            "INSERT INTO brief_provenance_log "
            "(ticker, generation_date, sources_used, sections_status, "
            " trigger, artifact_path) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                ticker.upper(),
                generation_date,
                json.dumps(sources_used, sort_keys=True),
                json.dumps(sections_status, sort_keys=True),
                trigger,
                rel_artifact,
            ),
        )
        conn.commit()
        provenance_ref = int(cursor.lastrowid) if cursor.lastrowid is not None else None
        _emit(
            "wrote_provenance_log",
            {
                "ticker": ticker,
                "generation_date": generation_date,
                "trigger": trigger,
                "per_metric_count": len(per_metric or {}),
            },
        )
        return provenance_ref
    except sqlite3.Error as exc:
        _emit(
            "provenance_log_failed",
            {"ticker": ticker, "error": f"{type(exc).__name__}: {exc}"},
        )
        return None
    finally:
        conn.close()


def _emit(event: str, payload: dict[str, object]) -> None:
    """One JSON line per event to stderr."""
    sys.stderr.write(json.dumps({"event": event, **payload}) + "\n")


def _resolve_kind(repo_root: Path, ticker: str) -> InstrumentType | None:
    """Look up tracked_companies.instrument_type for `ticker`."""
    db_path = require_db_path()
    if not db_path.exists():
        return None
    conn = connect_sqlite(str(db_path), role=SQLiteConnectionRole.READ_ONLY)
    conn.row_factory = sqlite3.Row
    try:
        return get_instrument_kind(conn, ticker)
    finally:
        conn.close()


def _build_etf(ticker: str, repo_root: Path, md_path: Path, today: str) -> dict[str, object]:
    """ETF brief build path — markdown only for the MVP.

    HTML / workspace / xlsx renderers are equity-shaped and not adapted yet;
    they'll graduate to multi-kind dispatch in a follow-up. For now the
    markdown brief is the canary deliverable that proves the dispatch works.
    """
    brief = build_etf_brief(ticker, repo_root)
    md_path.write_text(render_etf_markdown(brief), encoding="utf-8")
    _emit("wrote_etf_markdown", {"ticker": ticker, "path": str(md_path)})
    return {
        "ticker": ticker,
        "report_md": str(md_path),
        "instrument_kind": InstrumentType.ETF.value,
        "section_status": {
            "etf_profile": brief.profile.status.value,
            "etf_holdings": brief.holdings.status.value,
        },
    }


if __name__ == "__main__":
    raise SystemExit(main())
