"""Stages 2-3 of the KPI cascade — extract custom tier_1_kpis from LLM summaries.

Stage 1 (`fmp_derived_kpis`) handled universal financial KPIs deterministically
from FMP raw data. Stages 2/3 reach into the LLM-generated summaries already
on disk to fill in custom per-ticker metrics (GMV, ARPAC, NIM, etc.) that
aren't derivable from raw line items.

Two SourceType variants share the same pipeline shape, just different filename
suffixes and slightly different doc_type labels for provenance:

  - "earnings"  (Stage 2): `_summary.txt` + `_investor_update_summary.txt`
                           — per-quarter LLM summary of the call/investor letter
  - "ir"        (Stage 3): `_press_release_summary.txt` + `_presentation_brief.txt`
                           — IR-pipeline structured briefs

Each distinct allowlist source is processed with the full configured KPI list.
Completion records bounded processing, including empty or partial output. It does
not establish disclosure completeness or semantic admission. Exact completed
material inputs skip model work; --refresh repeats completed work without
superseding an active writer.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
import time
from collections.abc import Callable, Mapping
from contextlib import nullcontext, suppress
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from functools import partial
from pathlib import Path
from typing import Literal, cast

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    RootModel,
    TypeAdapter,
    field_validator,
    model_validator,
)

from compute import ir_supplement_capture
from compute.ir_supplement_capture import (
    SupplementCapture,
    SupplementExchange,
    SupplementSaturatedError,
)
from compute.kpi_resolver import canonical_metric_name, normalize_kpi_name
from db_paths import db_path_context
from llm.ledger import capture_call_records
from llm.prompt_ab import active_prompt_override, apply_prompt_override, edits_to_json
from llm.resolver import resolve_model_and_backend, structured_capability_profile
from llm.structured import (
    StructuredCallResult,
    call_llm_structured,
    call_llm_structured_with_raw,
    parse_json_payload,
)
from llm.untrusted import spotlight
from llm_client import FAST_CLASSIFIER_MODEL
from models.documents import SourceType, tier_for_source_type
from models.facts import Currency, FactLocator, FiscalPeriodType, LegacyEscapeHatch, Unit
from models.kpis import DefinitionOrigin
from models.runs import StageName, StageStatus
from models.unit_convert import convert_unit, same_family
from models.validation import Severity, ValidationRule
from pipeline.capture_coverage import CaptureCoverageRecord, record_coverage
from pipeline.invocation_fingerprint import file_fingerprint
from pipeline.kpi_persistence import (
    KpiExtractionManifest,
    KpiValue,
    PersistResult,
    find_or_create_kpi_definition,
    guard_llm_extracted_parent,
    persist_manifest,
    record_validation_issue,
)
from pipeline.kpi_semantics import (
    KpiAccountingBasis,
    KpiConsolidationScope,
    KpiPeriodRole,
    KpiPublicationLane,
    KpiSemanticContext,
    KpiSemanticStatus,
    KpiUnitScale,
    normalize_source_numeric,
    parse_source_numeric,
    source_numeric_token_occurs,
)
from pipeline.locators import html_span_locator, locate_char_span, verify_quote_in_source
from pipeline.restatement_detector import table_has_column
from pipeline.run_accounting import (
    JsonValue,
    PipelineRunSuppressedError,
    end_run,
    record_stage,
    start_run,
)
from pipeline.source_policy import (
    ArtifactKind,
    CollectionSource,
    authorize_collection_target_in_connection,
)
from provenance.document_evidence_pipeline import DocumentEvidenceRequest, process_document_evidence
from provenance.fulltext_backfill import project_source_text, source_text_projection_identity
from provenance.immutable_artifact import assert_artifact_unchanged, read_stable_artifact
from provenance.ir_reported_facts import (
    IrCandidateRetentionRequest,
    IrRawExchange,
    IrRawRow,
    IrReportedPublicationRequest,
    find_exact_ir_source_review,
    publish_ir_reported_facts,
    retain_ir_pdf_candidates,
)
from provenance.legacy_document_evidence import require_legacy_document_evidence
from provenance.llm_extracted_parent import resolve_parent
from provenance.reporting_entity_registry import ReportingEntityRegistry
from run_lock import hold_run_lock
from runtime.job_runtime import current_lock_claim, inherited_lock_is_valid

# Named `logger` (not `log`) to avoid colliding with the `TickerExtractionLog`
# locals named `log` throughout this module.
logger = logging.getLogger("kpi_extract_summaries")


def _persist_accounted(
    conn: sqlite3.Connection,
    *,
    directive: str,
    ticker: str,
    invocation_inputs: Mapping[str, JsonValue],
    manifest: KpiExtractionManifest,
    force: bool = False,
    pre_persist_validation: Callable[[], None] | None = None,
) -> PersistResult:
    """Persist one document manifest and close its run attempt exactly once."""
    if conn.in_transaction:
        raise ValueError("KPI manifest persistence requires an owned transaction boundary")
    run_id = start_run(
        conn,
        directive=directive,
        ticker_scope=[ticker],
        invocation_inputs=invocation_inputs,
        deduplicate_completed=True,
        force=force,
        pre_persist_validation=pre_persist_validation,
    )
    try:
        result = persist_manifest(conn, run_id=run_id, manifest=manifest, commit=False)
        end_run(conn, run_id, StageStatus.OK)
    except Exception as exc:
        conn.rollback()
        end_run(conn, run_id, StageStatus.FAILED, error_summary=f"{type(exc).__name__}: {exc}")
        raise
    return result


class _KpiSummaryValue(BaseModel):
    """Strict wire contract for one allowlisted KPI extraction."""

    model_config = ConfigDict(extra="forbid")

    value: Decimal
    unit: Literal["percent", "actual", "count", "ratio", "bps"]
    currency: Currency | None = None
    confidence: float = Field(ge=0.0, le=1.0)
    source_excerpt: str | None = Field(default=None, max_length=200)
    source_value_text: str | None = Field(default=None, min_length=1, max_length=80)
    reported_period_end: date
    period_role: KpiPeriodRole
    accounting_basis: KpiAccountingBasis
    consolidation_scope: KpiConsolidationScope
    dimensions: dict[str, str] = Field(default_factory=dict)
    source_unit_scale: KpiUnitScale
    source_row_label: str | None = Field(default=None, max_length=512)
    source_column_header: str | None = Field(default=None, max_length=512)

    @model_validator(mode="after")
    def _require_currency_for_actual(self) -> _KpiSummaryValue:
        if self.unit == "actual" and self.currency is None:
            raise ValueError("actual monetary KPI values require an explicit currency")
        if self.unit == "count" and self.source_unit_scale is not KpiUnitScale.UNKNOWN:
            if self.source_value_text is None:
                raise ValueError("COUNT values require the exact source numeric token")
            if self.source_excerpt is None or self.source_value_text not in self.source_excerpt:
                raise ValueError("source numeric token must occur in source_excerpt")
        return self


class _KpiSummaryResponse(RootModel[dict[str, _KpiSummaryValue]]):
    pass


class _EnumeratedKpi(BaseModel):
    """Strict wire contract for one capture-every-number row."""

    model_config = ConfigDict(extra="forbid")

    label: str = Field(min_length=1, max_length=200)
    value: Decimal
    unit: Literal["percent", "actual", "count", "ratio", "bps"]
    currency: Currency | None = None
    source_excerpt: str = Field(min_length=1, max_length=200)
    source_value_text: str | None = Field(default=None, min_length=1, max_length=80)
    # Prospective, unreviewed context. These fields do not grant publication.
    period_kind: Literal["instant", "duration", "unknown"] = "unknown"
    guidance_horizon: str | None = Field(default=None, max_length=200)
    source_definition: str | None = Field(default=None, max_length=512)
    context_witnesses: list[ir_supplement_capture.SupplementContextWitness] = Field(
        default_factory=list[ir_supplement_capture.SupplementContextWitness], max_length=8
    )
    reported_period_start: date | None = None
    reported_period_end: date
    period_role: KpiPeriodRole
    range_bound: Literal["lower", "upper"] | None = None
    accounting_basis: KpiAccountingBasis
    consolidation_scope: KpiConsolidationScope
    dimensions: dict[str, str] = Field(default_factory=dict)
    source_unit_scale: KpiUnitScale
    source_row_label: str | None = Field(default=None, max_length=512)
    source_column_header: str | None = Field(default=None, max_length=512)

    @field_validator("label", "source_excerpt")
    @classmethod
    def _strip_nonblank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("must not be blank")
        return stripped

    @model_validator(mode="after")
    def _require_currency_for_actual(self) -> _EnumeratedKpi:
        if self.unit == "actual" and self.currency is None:
            raise ValueError("actual monetary KPI values require an explicit currency")
        if self.unit == "count" and self.source_unit_scale is not KpiUnitScale.UNKNOWN:
            if self.source_value_text is None:
                raise ValueError("COUNT values require the exact source numeric token")
            if self.source_value_text not in self.source_excerpt:
                raise ValueError("source numeric token must occur in source_excerpt")
        if self.range_bound is not None and self.period_role is not KpiPeriodRole.GUIDANCE:
            raise ValueError("range endpoints require explicit guidance period role")
        return self


# Both extraction stages below (allowlist Stage A / enumerate Stage B) read an
# LLM-generated summary DOCUMENT, not the original filing/transcript text --
# there is no stable JSON/PDF/transcript-line anchor to build a FactLocator
# from at this layer (docs/design/provenance_clickthrough.md §3.2's
# kpi_extract_summaries.py row). Phase C's anchor-quote verification gate
# (§3.3, `_locator_for_excerpt`) now runs `pipeline.locators.
# verify_quote_in_source` against THIS summary document's own text (the one
# thing actually available at this layer) and upgrades to an `html_span`
# locator when the LLM's `source_excerpt` verifies -- these
# `_NO_LOCATOR_*` constants are now the FLOOR: what a value falls back to
# when it carries no excerpt at all, or when the excerpt fails verification
# (a hallucinated-anchor `validation_issues` row is logged in that second
# case; see `_locator_for_excerpt`), not the unconditional locator every
# value got before Phase C.
_NO_LOCATOR_ALLOWLIST = LegacyEscapeHatch(
    reason=(
        "Stage A allowlist extraction: no source_excerpt was returned, or "
        "the excerpt failed anchor-quote verification against the summary "
        "document (docs/design/provenance_clickthrough.md §3.3)"
    )
)
_NO_LOCATOR_ENUMERATE = LegacyEscapeHatch(
    reason=(
        "Stage B capture-every-number enumeration: no source_excerpt was "
        "returned, or the excerpt failed anchor-quote verification -- same "
        "gate as Stage A's _NO_LOCATOR_ALLOWLIST, tracked separately since "
        "this is the long-tail capture path rather than the curated "
        "watchlist path"
    )
)

# Per-source filename matchers + the documents.doc_type label written for each.
# Earnings: per-quarter call summary (canonical) + the MELI/NU investor-update variant.
# IR: press-release LLM summary + presentation-deck brief from the IR pipeline.
_EARNINGS_SUMMARY_RX = re.compile(
    r"^(?P<ticker>[A-Z][A-Z0-9.]*)_Q(?P<q>[1-4])_(?P<y>\d{4})_(?:investor_update_)?summary\.txt$"
)
_IR_PRESS_RELEASE_RX = re.compile(
    r"^(?P<ticker>[A-Z][A-Z0-9.]*)_Q(?P<q>[1-4])_(?P<y>\d{4})_press_release_summary\.txt$"
)
_IR_PRESENTATION_RX = re.compile(
    r"^(?P<ticker>[A-Z][A-Z0-9.]*)_Q(?P<q>[1-4])_(?P<y>\d{4})_presentation_brief\.txt$"
)


@dataclass(frozen=True)
class _SourceSpec:
    """One LLM-summary kind: filename pattern + the doc_type used for provenance."""

    name: str  # short label for telemetry / log keys
    doc_type: str  # what we write into documents.doc_type
    pattern: re.Pattern[str]


_EARNINGS_SOURCE = _SourceSpec("earnings_summary", "llm_summary", _EARNINGS_SUMMARY_RX)
_IR_PRESS_RELEASE_SOURCE = _SourceSpec(
    "ir_press_release_summary", "ir_press_release_synthesized", _IR_PRESS_RELEASE_RX
)
_IR_PRESENTATION_SOURCE = _SourceSpec(
    "ir_presentation_brief", "ir_presentation_synthesized", _IR_PRESENTATION_RX
)

_SOURCE_GROUPS: dict[str, tuple[_SourceSpec, ...]] = {
    "earnings": (_EARNINGS_SOURCE,),
    "ir": (_IR_PRESS_RELEASE_SOURCE, _IR_PRESENTATION_SOURCE),
}

# How filename Q + calendar year map to a period_end. Most tickers use the
# calendar fiscal-year mapping; the per-ticker override map handles companies
# whose fiscal year ends on a non-December month. NVO publishes H1 / 9M instead
# of Q2 / Q3, but its period end-of-month dates still align to calendar
# quarters, so no override needed.
_QUARTER_PERIOD_END: dict[int, tuple[int, int]] = {
    1: (3, 31),
    2: (6, 30),
    3: (9, 30),
    4: (12, 31),
}

# (month, day, year_offset_from_filename_label) per fiscal quarter for tickers
# with non-calendar fiscal years. year_offset is added to the filename's year
# label to get the calendar year of period_end — NOT inferred from the month,
# because that inference (the previous implementation: "month <= 2 implies the
# label year + 1") silently breaks for Oct-FYE tickers (see below). This table
# is specific to THIS module's own `.tmp/<TICKER>_Q<N>_<YYYY>_*.txt` filename
# labels, which `_list_sources` parses straight off disk — those labels are
# generated independently of (and are NOT always numerically identical to)
# `compute/transcript_ingest.py`'s fiscal-year labels for the same calendar
# quarter; see directives/data_provenance.md, "Fiscal-period stamping drift"
# for the cross-module label-drift this caused for VEEV.
#
# Jan-FYE (RBRK, VEEV): Q1-Q3 fall in the calendar year matching the filename
# label (VEEV_Q1_2026 ends 2026-04-30); Q4 rolls into label year + 1
# (VEEV_Q4_2025 ends 2026-01-31, VEEV_Q4_2026 ends 2027-01-31) — Q4's month is
# January, one calendar year ahead of the Q1-Q3 quarters of the same label.
# Oct-FYE (AMAT, TOL): all four quarters fall in the label year with NO offset
# (AMAT_Q4_2025 ends 2025-10-31, AMAT_Q1_2026 ends 2026-01-31) — critically,
# Q1's month is also January here, but must NOT roll over the way Jan-FYE's Q4
# does. A shared "month <= 2 implies +1" rule cannot represent both
# conventions at once; the explicit year_offset can.
_TICKER_QUARTER_PERIOD_END: dict[str, dict[int, tuple[int, int, int]]] = {
    "RBRK": {1: (4, 30, 0), 2: (7, 31, 0), 3: (10, 31, 0), 4: (1, 31, 1)},
    "VEEV": {1: (4, 30, 0), 2: (7, 31, 0), 3: (10, 31, 0), 4: (1, 31, 1)},
    "AMAT": {1: (1, 31, 0), 2: (4, 30, 0), 3: (7, 31, 0), 4: (10, 31, 0)},
    "TOL": {1: (1, 31, 0), 2: (4, 30, 0), 3: (7, 31, 0), 4: (10, 31, 0)},
}


@dataclass
class TickerExtractionLog:
    """Per-ticker run record persisted to data/kpi_extraction_log.json."""

    ticker: str
    stage: str = "stage_2_summaries"
    started_at: str = ""
    ended_at: str = ""
    elapsed_ms: int = 0
    quarters_attempted: list[str] = field(default_factory=list[str])
    quarters_extracted: list[str] = field(default_factory=list[str])
    quarters_skipped_no_missing: list[str] = field(default_factory=list[str])
    documents_completed: list[dict[str, JsonValue]] = field(
        default_factory=list[dict[str, JsonValue]]
    )
    kpis_inserted_total: int = 0
    error: str | None = None


_GROUP_TO_STAGE: dict[str, str] = {
    "earnings": "stage_2_summaries",
    "ir": "stage_3_ir_briefs",
}


# Bump when prompt, schema, conversion, or admission behavior changes.
_ALLOWLIST_EXTRACTOR_CONTRACT = "summary-allowlist-completion-v1"
_ALLOWLIST_PURPOSE = "kpi_summary_extract"


def extract_for_ticker(
    ticker: str,
    repo_root: Path,
    conn: sqlite3.Connection,
    refresh: bool = False,
    source_group: str = "earnings",
    *,
    requested: bool = False,
) -> TickerExtractionLog:
    ticker = ticker.upper()
    if source_group not in _SOURCE_GROUPS:
        raise ValueError(
            f"unknown source_group {source_group!r}; expected one of {list(_SOURCE_GROUPS)}"
        )
    log = TickerExtractionLog(ticker=ticker, stage=_GROUP_TO_STAGE[source_group])
    log.started_at = _now_iso_z()
    t0 = time.perf_counter()
    holdings_path = repo_root / "micro_thesis" / "holdings" / f"{ticker}.json"
    if not holdings_path.exists():
        log.error = f"no holdings JSON for {ticker}"
        return _close_log(log, t0)
    try:
        holdings_snapshot, holdings_raw = read_stable_artifact(holdings_path)
        decoded: object = json.loads(holdings_raw)
        if not isinstance(decoded, dict):
            raise ValueError("holdings JSON must be an object")
        holdings = cast("dict[str, object]", decoded)
        tier_1_names = _tier_1_names(holdings)
        if not tier_1_names:
            raise ValueError("holdings JSON has no tier_1_kpis")
        db_path = Path(conn.execute("PRAGMA database_list").fetchone()[2])
        model, backend = resolve_model_and_backend(
            _ALLOWLIST_PURPOSE,
            capability_profile=structured_capability_profile(None),
            db_path=db_path,
        )
    except Exception as exc:
        log.error = f"{type(exc).__name__}: {exc}"
        return _close_log(log, t0)

    for quarter, year, source_path, spec in _list_sources(
        repo_root, ticker, _SOURCE_GROUPS[source_group]
    ):
        period_label = f"Q{quarter} {year} [{spec.name}]"
        log.quarters_attempted.append(period_label)
        run_id: str | None = None
        try:
            source_snapshot, source_raw = read_stable_artifact(source_path)
            text = source_raw.decode("utf-8")
            period_end = _canonical_summary_period_end(
                conn, ticker, _period_end(ticker, quarter, year), source_path, spec.doc_type
            )
            doc_id = _prepare_summary_document_evidence(
                conn,
                repo_root=repo_root,
                db_path=db_path,
                ticker=ticker,
                source_path=source_path,
                captured_bytes=source_raw,
                doc_type=spec.doc_type,
                period_end=period_end,
                source_sha256=source_snapshot.file_sha256,
                requested=requested,
            )
            document_state = _allowlist_document_state(
                conn,
                doc_id,
                ticker,
                period_end,
                source_path,
                spec.doc_type,
                source_snapshot.file_sha256,
            )
            canonical_units = _canonical_units_from_holdings(
                holdings,
                tier_1_names,
                fact_units=_fact_units_by_name(conn, ticker, tier_1_names),
            )
            invocation_inputs: dict[str, JsonValue] = {
                "extractor_contract": _ALLOWLIST_EXTRACTOR_CONTRACT,
                "ticker": ticker,
                "source_group": source_group,
                "owner_requested": requested,
                "document": document_state,
                "period_end": period_end.isoformat(),
                "fiscal_period_type": f"Q{quarter}",
                "source_sha256": source_snapshot.file_sha256,
                "holdings_sha256": holdings_snapshot.file_sha256,
                "requested_kpis": tier_1_names,
                "canonical_units": {name: unit.value for name, unit in canonical_units.items()},
                "purpose": _ALLOWLIST_PURPOSE,
                "model": model,
                "backend": backend,
                "allow_admission": False,
            }
            run_id = start_run(
                conn,
                directive=f"extract_kpis_from_{source_group}",
                ticker_scope=[ticker],
                invocation_inputs=invocation_inputs,
                deduplicate_completed=not refresh,
                force=False,
            )
            extracted = _llm_extract(
                ticker,
                period_label,
                tier_1_names,
                text,
                model=model,
                backend=backend,
                run_id=run_id,
                db_path=db_path,
            )
            if not conn.in_transaction:
                conn.execute("BEGIN IMMEDIATE")
            assert_artifact_unchanged(source_snapshot)
            assert_artifact_unchanged(holdings_snapshot)
            if document_state != _allowlist_document_state(
                conn,
                doc_id,
                ticker,
                period_end,
                source_path,
                spec.doc_type,
                source_snapshot.file_sha256,
            ) or canonical_units != _canonical_units_from_holdings(
                holdings,
                tier_1_names,
                fact_units=_fact_units_by_name(conn, ticker, tier_1_names),
            ):
                raise ValueError("summary extraction database inputs changed before persistence")
            if (model, backend) != resolve_model_and_backend(
                _ALLOWLIST_PURPOSE,
                capability_profile=structured_capability_profile(None),
                db_path=db_path,
            ):
                raise ValueError("summary extraction model route changed before persistence")
            manifest = _build_manifest(
                ticker,
                period_end,
                FiscalPeriodType(f"Q{quarter}"),
                doc_id,
                extracted,
                canonical_units,
                conn=conn,
                source_text=text,
            )
            manifest.model_name = model
            result = persist_manifest(conn, run_id=run_id, manifest=manifest, commit=False)
            disposition: dict[str, JsonValue] = {
                "document_id": doc_id,
                "requested_kpis": tier_1_names,
                "returned_kpis": list(extracted),
                "unreturned_kpis": [name for name in tier_1_names if name not in extracted],
                "unreturned_disposition": "unknown",
                "completion_scope": "bounded_processing",
                "issuer_acquisition_completeness": "not_asserted_derived_summary",
                "inserted": result.inserted,
                "skipped_existing": result.skipped_existing,
                "validation_issues": result.validation_issues,
            }
            record_stage(
                conn,
                run_id,
                ticker,
                StageName.PARSE,
                StageStatus.OK,
                period_end=period_end,
                error_msg=json.dumps(disposition, sort_keys=True),
                commit=False,
            )
            end_run(conn, run_id, StageStatus.OK)
            log.documents_completed.append(disposition)
            log.kpis_inserted_total += result.inserted
            log.quarters_extracted.append(period_label)
        except PipelineRunSuppressedError as exc:
            if exc.status is not StageStatus.OK:
                raise
            log.quarters_skipped_no_missing.append(period_label)
        except Exception as exc:
            conn.rollback()
            if run_id is not None:
                try:
                    end_run(
                        conn,
                        run_id,
                        StageStatus.FAILED,
                        error_summary=f"{type(exc).__name__}: {exc}",
                    )
                except Exception:
                    conn.rollback()
                    logger.exception("summary extraction failure accounting failed")
            log.error = f"{period_label}: {type(exc).__name__}: {exc}"
            break
    return _close_log(log, t0)


def _prepare_summary_document_evidence(
    conn: sqlite3.Connection,
    *,
    repo_root: Path,
    db_path: Path,
    ticker: str,
    source_path: Path,
    captured_bytes: bytes,
    doc_type: str,
    period_end: datetime,
    source_sha256: str,
    requested: bool,
) -> int:
    """Sequence existing registration and deterministic evidence under one DB owner."""
    if conn.in_transaction:
        raise RuntimeError("summary evidence preparation requires an idle connection")
    authorization = authorize_collection_target_in_connection(
        conn,
        ticker,
        requested=requested,
        source=CollectionSource.IR,
        artifact_kind=ArtifactKind.IR_DOCUMENT,
    )
    if not authorization.allowed:
        raise ValueError("summary evidence source policy denied: " + authorization.status.value)
    configured_database = os.environ.get("EARNINGS_SUMMARY_DB_PATH", "").strip()
    owns_database = (
        bool(configured_database) and Path(configured_database).resolve() == db_path.resolve()
    )
    inherited = owns_database and (
        current_lock_claim(repo_root, "portfolio-db") is not None
        or inherited_lock_is_valid(repo_root, "portfolio-db")
    )
    with (
        nullcontext()
        if inherited
        else hold_run_lock(db_path, owner="summary-document-evidence", timeout_s=0)
    ):
        document_id = _ensure_summary_document_row(
            conn,
            ticker,
            period_end,
            source_path,
            doc_type,
            captured_bytes=captured_bytes,
        )
        # An invalid explicit selection must not be repaired or replaced.
        if (
            conn.execute(
                "SELECT 1 FROM legacy_document_evidence_binding_revisions WHERE legacy_document_id=?",
                (document_id,),
            ).fetchone()
            is not None
        ):
            require_legacy_document_evidence(
                conn,
                document_id=document_id,
                ticker=ticker,
                source_type=SourceType.LLM_EXTRACTED.value,
                doc_type=doc_type,
                period_end=period_end,
                source_sha256=source_sha256,
            )
        result = process_document_evidence(
            conn,
            DocumentEvidenceRequest(
                repo_root=repo_root,
                ticker=ticker,
                document_id=document_id,
                source_type=SourceType.LLM_EXTRACTED,
                requested=requested,
                apply=True,
                batch_size=1,
            ),
        )
    if len(result.items) != 1 or result.items[0].document_id != document_id:
        raise ValueError("summary evidence preparation did not select the exact document")
    item = result.items[0]
    if item.status not in {"extracted", "already-covered"}:
        raise ValueError("summary evidence preparation incomplete: " + ",".join(item.findings))
    # Missing issuer-inventory linkage remains degraded. A derived summary is
    # not an issuer disclosure; the unchanged readiness gate checks its evidence.
    return document_id


def _allowlist_document_state(
    conn: sqlite3.Connection,
    doc_id: int,
    ticker: str,
    period_end: datetime,
    path: Path,
    doc_type: str,
    sha: str,
) -> dict[str, JsonValue]:
    row = conn.execute(
        "SELECT id,ticker,source_type,doc_type,period_end,sha256,parent_document_id "
        "FROM documents WHERE id = ?",
        (doc_id,),
    ).fetchone()
    if row is None:
        raise ValueError("summary document no longer exists")
    match = resolve_parent(
        conn,
        ticker=ticker,
        doc_type=doc_type,
        file_path=str(path).replace("\\", "/"),
        period_end=period_end,
    )
    expected_parent = match.parent_document_id if match is not None else None
    if (
        str(row["ticker"]).upper() != ticker
        or row["source_type"] != SourceType.LLM_EXTRACTED.value
        or row["doc_type"] != doc_type
        or row["sha256"] != sha
        or datetime.fromisoformat(str(row["period_end"])) != period_end
        or row["parent_document_id"] != expected_parent
    ):
        raise ValueError("summary document identity or parent binding does not match source")
    parent: dict[str, JsonValue] | None = None
    if expected_parent is not None:
        parent_row = conn.execute(
            "SELECT id,ticker,source_type,doc_type,period_end,sha256,file_path "
            "FROM documents WHERE id=?",
            (expected_parent,),
        ).fetchone()
        if parent_row is None:
            raise ValueError("summary parent document no longer exists")
        parent = {
            key: (None if parent_row[key] is None else str(parent_row[key]))
            for key in (
                "id",
                "ticker",
                "source_type",
                "doc_type",
                "period_end",
                "sha256",
                "file_path",
            )
        }
    readiness = require_legacy_document_evidence(
        conn,
        document_id=doc_id,
        ticker=ticker,
        source_type=SourceType.LLM_EXTRACTED.value,
        doc_type=doc_type,
        period_end=period_end,
        source_sha256=sha,
    )
    return {
        "evidence_document_version_id": readiness.document_version_id,
        "evidence_node_id": readiness.evidence_node_id,
        "evidence_extraction_run_id": readiness.extraction_run_id,
        "evidence_binding_revision_id": readiness.binding_revision_id,
        "id": doc_id,
        "ticker": ticker,
        "source_type": str(row["source_type"]),
        "doc_type": doc_type,
        "period_end": period_end.isoformat(),
        "sha256": sha,
        "parent_document_id": expected_parent,
        "parent": parent,
    }


def _list_sources(
    repo_root: Path, ticker: str, specs: tuple[_SourceSpec, ...]
) -> list[tuple[int, int, Path, _SourceSpec]]:
    """Walk `.tmp/`, return (quarter, year, path, spec) for every match. Oldest first."""
    tmp = repo_root / ".tmp"
    if not tmp.exists():
        return []
    out: list[tuple[int, int, Path, _SourceSpec]] = []
    for p in tmp.iterdir():
        if not p.is_file():
            continue
        for spec in specs:
            m = spec.pattern.match(p.name)
            if not m or m.group("ticker") != ticker:
                continue
            out.append((int(m.group("q")), int(m.group("y")), p, spec))
            break
    out.sort(key=lambda x: (x[1], x[0], x[3].name))
    return out


def _tier_1_names(holdings: dict[str, object]) -> list[str]:
    candidate = holdings.get("tier_1_kpis")
    if not isinstance(candidate, list):
        return []
    raw = cast("list[object]", candidate)
    out: list[str] = []
    for item in raw:
        if isinstance(item, dict):
            k = cast("dict[str, object]", item)
            name = str(k.get("name", "")).strip()
            if name:
                out.append(name)
    return out


def _fact_units_by_name(conn: sqlite3.Connection, ticker: str, names: list[str]) -> dict[str, Unit]:
    """Dominant recorded ``kpi_facts`` unit per metric, for names that have facts.

    The recorded fact unit is the metric's true reported-value unit — used to
    veto a break-rule's *comparison* unit (a YoY-growth ``percent`` declared for
    a dollar LEVEL) from being adopted as the metric's canonical unit. Names with
    no facts, or a stored unit outside the :class:`Unit` enum, are omitted.
    Returns the modal unit per name (ties broken by the latest period).
    """
    out: dict[str, Unit] = {}
    for name in names:
        row = conn.execute(
            "SELECT f.unit FROM kpi_facts f "
            "JOIN kpi_definitions d ON d.id = f.kpi_definition_id "
            "WHERE d.ticker = ? AND d.name = ? AND f.unit IS NOT NULL "
            "GROUP BY f.unit ORDER BY COUNT(*) DESC, MAX(f.period_end) DESC LIMIT 1",
            (ticker.upper(), name),
        ).fetchone()
        if row is None or row[0] is None:
            continue
        try:
            out[name] = Unit(str(row[0]))
        except ValueError:
            continue
    return out


def _canonical_units_from_holdings(
    holdings: dict[str, object],
    kpi_names: list[str],
    *,
    fact_units: dict[str, Unit] | None = None,
) -> dict[str, Unit]:
    """Map each extraction KPI name to its break-rule's declared Unit.

    The break-rules (`break_rules` + `business_model_rules`) declare the unit each
    threshold is expressed in — the authoritative unit the metric should be stored
    in, which `persist_manifest` reconciles the LLM's per-call unit to. They're
    keyed by their own `kpi_name`, which may spell the metric differently from the
    tier_1 label the extractor pulls ("Net New Subscription ARR (USD millions)" vs
    "Net new subscription ARR ($)"); both normalize to the same key, so we match on
    the parenthetical-insensitive normalized name. Only real `Unit` values are
    kept — a rule unit outside the enum (e.g. a derived "percent_decel" transform)
    has no dimensional meaning here and is skipped.

    A rule's unit equals the metric's reported-value unit ONLY for a direct
    level/rate threshold — NOT for a YoY-growth or deceleration rule, whose unit
    is ``percent`` while the metric itself is a dollar LEVEL (the AWS RPO shape).
    When ``fact_units`` supplies a metric's recorded value unit and the rule unit
    is in a DIFFERENT dimensional family, the rule unit is a *comparison* unit and
    is dropped — the metric keeps its real value unit. A same-family rule unit is
    still adopted (the ratio→percent / dollar→millions normalization #320 added).
    Without a fact unit (first extraction) the rule unit is used as before.
    """
    fact_units = fact_units or {}
    rule_unit_by_norm: dict[str, Unit] = {}
    for key in ("break_rules", "business_model_rules"):
        raw = holdings.get(key)
        if not isinstance(raw, list):
            continue
        for entry in cast("list[object]", raw):
            if not isinstance(entry, dict):
                continue
            rule = cast("dict[str, object]", entry)
            rule_name = str(rule.get("kpi_name", "")).strip()
            rule_unit = str(rule.get("unit", "")).strip()
            if not rule_name or not rule_unit:
                continue
            try:
                unit = Unit(rule_unit)
            except ValueError:
                continue
            # First rule wins per metric; break_rules precede business_model_rules,
            # matching the evaluator's universal-then-business-model ordering.
            rule_unit_by_norm.setdefault(normalize_kpi_name(rule_name), unit)
    out: dict[str, Unit] = {}
    for name in kpi_names:
        unit = rule_unit_by_norm.get(normalize_kpi_name(name))
        if unit is None:
            continue
        fact_unit = fact_units.get(name)
        if fact_unit is not None and not same_family(unit, fact_unit):
            # The rule's threshold is a *change* of the metric (e.g. YoY growth in
            # percent), not its level (dollars). Don't let it override the real
            # value unit; the LLM's extracted unit is used unchanged downstream.
            logger.info(
                {
                    "event": "canonical_unit_dropped_cross_family",
                    "kpi_name": name,
                    "rule_unit": unit.value,
                    "fact_unit": fact_unit.value,
                }
            )
            continue
        out[name] = unit
    return out


def _period_end(ticker: str, quarter: int, year: int) -> datetime:
    """Map a (filename year, fiscal quarter) pair to the actual period_end date.

    Most tickers report on the calendar fiscal year (Q4 FY{year} ends
    {year}-12-31). Tickers in `_TICKER_QUARTER_PERIOD_END` use a non-calendar
    fiscal year — for those, the override's explicit `year_offset` determines
    the calendar year (NOT an inference from the month: Jan-FYE's Q4 and
    Oct-FYE's Q1 both land in January but roll over differently — see that
    table's docstring).
    """
    overrides = _TICKER_QUARTER_PERIOD_END.get(ticker.upper())
    if overrides and quarter in overrides:
        month, day, year_offset = overrides[quarter]
        return datetime(year + year_offset, month, day)
    month, day = _QUARTER_PERIOD_END[quarter]
    return datetime(year, month, day)


def _ensure_summary_document_row(
    conn: sqlite3.Connection,
    ticker: str,
    period_end: datetime,
    path: Path,
    doc_type: str,
    *,
    captured_bytes: bytes | None = None,
) -> int:
    """Insert documents row for the summary file if not already present, return id.

    Resolves ``parent_document_id`` via ``provenance.llm_extracted_parent`` (the
    earnings-call transcript / IR document this summary was generated from) so
    every fresh row satisfies data_provenance.md §2 at write time. A row whose
    parent can't be derived (no eligible primary document on file for this
    ticker/period yet) is still inserted — a summary file already exists and
    kpi_facts needs the FK — but flagged via ``guard_llm_extracted_parent``
    rather than silently left unlinked.
    """
    raw = path.read_bytes() if captured_bytes is None else captured_bytes
    sha = hashlib.sha256(raw).hexdigest()
    existing = conn.execute("SELECT id FROM documents WHERE sha256 = ?", (sha,)).fetchone()
    if existing is not None:
        return int(existing["id"])
    file_path_str = str(path).replace("\\", "/")
    match = resolve_parent(
        conn, ticker=ticker, doc_type=doc_type, file_path=file_path_str, period_end=period_end
    )
    parent_document_id = match.parent_document_id if match is not None else None
    canonical_period_end = match.parent_period_end if match is not None else period_end
    common_args = (
        ticker,
        SourceType.LLM_EXTRACTED.value,
        doc_type,
        canonical_period_end,
        file_path_str,
        sha,
        datetime.now(UTC),
        "ok",
        len(raw),
        parent_document_id,
    )
    if table_has_column(conn, "documents", "source_quality_tier"):
        tier = tier_for_source_type(SourceType.LLM_EXTRACTED).value
        cur = conn.execute(
            """
            INSERT INTO documents
              (ticker, source_type, doc_type, period_end, file_path, sha256,
               fetched_at, fetch_status, raw_bytes_size, parent_document_id, source_quality_tier)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (*common_args, tier),
        )
    else:
        cur = conn.execute(
            """
            INSERT INTO documents
              (ticker, source_type, doc_type, period_end, file_path, sha256,
               fetched_at, fetch_status, raw_bytes_size, parent_document_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            common_args,
        )
    doc_id = int(cur.lastrowid) if cur.lastrowid is not None else 0
    guard_llm_extracted_parent(
        conn,
        source_type=SourceType.LLM_EXTRACTED,
        parent_document_id=parent_document_id,
        ticker=ticker,
        doc_id=doc_id,
    )
    conn.commit()
    return doc_id


def _canonical_summary_period_end(
    conn: sqlite3.Connection,
    ticker: str,
    fallback_period_end: datetime,
    path: Path,
    doc_type: str,
) -> datetime:
    """Use the resolved source's period when summary lineage is deterministic.

    The filename fiscal map remains a write-time fallback for newly-created
    unlinked summaries. It is never used to force a legacy parent match.
    """
    match = resolve_parent(
        conn,
        ticker=ticker,
        doc_type=doc_type,
        file_path=str(path).replace("\\", "/"),
        period_end=fallback_period_end,
    )
    return match.parent_period_end if match is not None else fallback_period_end


def _llm_extract(
    ticker: str,
    period_label: str,
    kpi_names: list[str],
    summary_text: str,
    *,
    model: str | None = None,
    backend: str | None = None,
    run_id: str | None = None,
    db_path: Path | None = None,
) -> dict[str, dict[str, object]]:
    """Single Haiku call. Returns {kpi_name: {value, unit, confidence,
    source_excerpt?}}.

    Prompt is generic across all three source shapes (earnings call summary /
    press release / presentation brief). Each has its own structure, but the
    extraction contract — pull the as-reported current-quarter value of each
    named KPI — is the same. `source_excerpt` is the verbatim snippet the value
    was read from; it lands in kpi_facts.source_excerpt so report tooltips and
    the source drawer can show the supporting quote (P3.2).
    """
    if not kpi_names:
        return {}
    names_block = "\n".join(f"- {n}" for n in kpi_names)
    prompt = f"""You are extracting structured KPI values from a quarterly research document for {ticker} ({period_label}).

The document is one of: an LLM-summarised earnings call (sections: Financial Highlights table, Operational Highlights, Management Outlook, Q&A), an LLM-summarised IR press release (Headline Results table, Key Business Metrics, Guidance, Capital Allocation), or an LLM-distilled presentation brief (Management Narrative, Highlighted Metrics, Strategic Initiatives, Forward-Looking Slides). Whichever it is, current-quarter actuals live in the headline table / metrics section; *guidance* for the NEXT quarter or full year is reported separately and is NOT what we want here.

For EACH of the KPI names below, find the value reported FOR THIS QUARTER (not guidance for next quarter). Return a JSON object keyed by the EXACT name I gave you. Values:
  - "value": numeric only, no unit symbols (e.g. 12.5 not "12.5%").
  - "unit": EXACTLY ONE of these tokens — "percent", "actual", "count", "ratio", "bps":
      * "percent"  — rates, margins, growth, retention (e.g. NRR 120% → value 120; NIM 17.8% → 17.8). Drop the % sign; do NOT divide by 100.
      * "actual"   — ALL monetary amounts. Report the FULL figure in the issuer's base currency units, NEVER pre-scaled. "$1.2 billion" → value 1200000000; "$115M net new ARR" → 115000000; ARPAC "$11.20" → 11.20. Never use a currency code or a scaled token as the unit — always expand to the whole number with unit "actual".
      * "count"    — headcount / customers / subscribers / accounts, as the full integer (e.g. "12.5 million customers" → 12500000).
      * "ratio"    — unitless multiples reported with an "x" (e.g. coverage 1.8x → 1.8).
      * "bps"      — only when the document states the metric in basis points.
  - "currency": required ISO code for every "actual" monetary value (for example "USD", "BRL", or "EUR"); omit it for counts and rates. Do not infer a currency.
    Use only those five unit tokens; if unsure between "actual" and a scaled form, always pick "actual" with the full figure.
  - "confidence": float 0.0-1.0; lower if you had to estimate from context
  - "source_excerpt": the VERBATIM snippet (under 200 characters) copied character-for-character from the document that contains the reported value — the sentence or table line you read it from. Never paraphrase or reformat; if you cannot quote it exactly, omit this field.
  - "source_value_text": for every count, the EXACT numeric token copied from source BEFORE expansion (for example "12.5" from "12.5 million customers"). It must occur verbatim in source_excerpt.
  - "reported_period_end": ISO date for the value itself, not the document publication date.
  - "period_role": EXACTLY ONE of "current" | "prior_period_comparator" | "prior_year_comparator" | "guidance" | "unknown".
  - "accounting_basis": EXACTLY ONE of "gaap" | "non_gaap" | "management" | "unknown". Operational KPIs explicitly defined by management use "management".
  - "consolidation_scope": EXACTLY ONE of "consolidated" | "geography" | "segment" | "product" | "other" | "unknown".
  - "dimensions": an object of explicit qualifiers, for example {{"geography":"Brazil"}} or {{"customer_type":"active"}}; use {{}} only when the source is consolidated/unqualified.
  - "source_unit_scale": EXACTLY ONE of "none" | "thousands" | "millions" | "billions" | "unknown", describing the source presentation before numeric expansion.
  - "source_row_label" and "source_column_header": verbatim table coordinates when present; otherwise omit them.

If a KPI is not disclosed in the document, OMIT IT from the response. Do not guess.

KPI names to extract:
{names_block}

Document text:
\"\"\"
{summary_text}
\"\"\"

Return ONLY the JSON object — no markdown fence, no commentary."""

    parsed = call_llm_structured(
        prompt,
        purpose="kpi_summary_extract",
        ticker=ticker,
        model=model,
        backend=backend,
        run_id=run_id,
        db_path=db_path,
        expect="object",
        schema=TypeAdapter(_KpiSummaryResponse),
    )
    validated = _KpiSummaryResponse.model_validate(parsed)
    return {
        key: value.model_dump(mode="python", exclude_none=True)
        for key, value in validated.root.items()
        if key in kpi_names
    }


def parse_decimal_value(raw: object) -> Decimal | None:
    """Parse an LLM-returned KPI value to Decimal, or None if unparseable.

    Haiku occasionally returns a non-numeric string ("N/A", "~17%", "n.m.") for a
    KPI it can't find a clean figure for. ``Decimal(str(raw))`` then raises
    ``decimal.InvalidOperation`` (an ArithmeticError, NOT a ValueError), so
    catching only (TypeError, ValueError) let one bad value abort the WHOLE
    ticker's extraction. Returning None lets the caller skip just that KPI.
    """
    try:
        return Decimal(str(raw))
    except (TypeError, ValueError, InvalidOperation):
        return None


def _locator_for_excerpt(
    *,
    excerpt: str | None,
    doc_id: int,
    source_text: str | None,
    conn: sqlite3.Connection | None,
    ticker: str,
    period_end: datetime,
    kpi_name: str,
    default: LegacyEscapeHatch,
) -> FactLocator | LegacyEscapeHatch:
    """Phase C anchor-quote verification gate (docs/design/
    provenance_clickthrough.md §3.3), shared by Stage A's allowlist path and
    Stage B's on-disk-summary enumerate path.

    ``source_text`` is the SAME LLM-generated summary document the excerpt was
    read from (registered as ``doc_id`` by ``_ensure_summary_document_row``) --
    not the original filing/transcript, per this module's own long-documented
    gap (see the module-level ``_NO_LOCATOR_*`` docstrings). Verifying against
    THAT document is still a real, non-fabricated anchor: the peek can open
    ``doc_id`` and show the reader the exact sentence, even though it's one
    hop removed from the primary source.

    - No excerpt at all -> ``default`` (today's behavior, unchanged: an
      honest gap, not a hallucination).
    - Excerpt present and verified -> an ``html_span`` locator (best-effort
      char span via ``pipeline.locators.locate_char_span``).
    - Excerpt present but NOT found verbatim -> ``default``, PLUS a
      ``validation_issues`` row (``rule=hallucinated_anchor``) when ``conn``
      is given -- the hallucination signal this gate exists to surface.
    """
    if excerpt is None or source_text is None:
        return default
    if verify_quote_in_source(excerpt, source_text):
        span = locate_char_span(excerpt, source_text)
        return html_span_locator(
            doc_id=doc_id,
            quote=excerpt,
            char_start=span[0] if span is not None else None,
            char_end=span[1] if span is not None else None,
        )
    if conn is not None:
        try:
            record_validation_issue(
                conn,
                run_id=f"kpi_extract_summaries:{ticker}:{period_end.date().isoformat()}",
                source_doc_id=doc_id,
                ticker=ticker,
                severity=Severity.WARN,
                rule=ValidationRule.HALLUCINATED_ANCHOR,
                raw_value=excerpt,
                expected=kpi_name,
            )
        except sqlite3.Error:
            logger.warning(
                {
                    "event": "hallucinated_anchor_log_failed",
                    "ticker": ticker,
                    "kpi_name": kpi_name,
                }
            )
    return default


def _build_manifest(
    ticker: str,
    period_end: datetime,
    fpt: FiscalPeriodType,
    doc_id: int,
    extracted: dict[str, dict[str, object]],
    canonical_units: dict[str, Unit] | None = None,
    *,
    conn: sqlite3.Connection | None = None,
    source_text: str | None = None,
) -> KpiExtractionManifest:
    values: list[KpiValue] = []
    for name, payload in extracted.items():
        v = parse_decimal_value(payload.get("value"))
        if v is None:
            continue
        unit_raw = str(payload.get("unit") or "actual")
        try:
            unit = Unit(unit_raw)
        except ValueError:
            unit = Unit.ACTUAL
        currency_raw = payload.get("currency")
        currency = _currency_for_monetary_value(currency_raw, unit, name)
        conf_raw = payload.get("confidence", 0.85)
        try:
            confidence = (
                max(0.0, min(1.0, float(conf_raw)))
                if isinstance(conf_raw, (str, int, float))
                else 0.85
            )
        except ValueError:
            confidence = 0.85
        excerpt_raw = payload.get("source_excerpt")
        excerpt = (
            excerpt_raw.strip()[:1024]
            if isinstance(excerpt_raw, str) and excerpt_raw.strip()
            else None
        )
        locator = _locator_for_excerpt(
            excerpt=excerpt,
            doc_id=doc_id,
            source_text=source_text,
            conn=conn,
            ticker=ticker,
            period_end=period_end,
            kpi_name=name,
            default=_NO_LOCATOR_ALLOWLIST,
        )
        values.append(
            KpiValue(
                name=name,
                value=v,
                unit=unit,
                currency=currency,
                confidence=confidence,
                source_excerpt=excerpt,
                source_value_text=(
                    str(payload["source_value_text"])
                    if payload.get("source_value_text") is not None
                    else None
                ),
                locator=locator,
                semantic_context=_semantic_context_for_payload(
                    name=name,
                    payload=payload,
                    expected_period_end=period_end,
                    allow_admission=False,
                ),
            )
        )

    return KpiExtractionManifest(
        ticker=ticker,
        period_end=period_end,
        fiscal_period_type=fpt,
        source_doc_id=doc_id,
        primary_source=SourceType.LLM_EXTRACTED,
        model_name=FAST_CLASSIFIER_MODEL,
        canonical_units=canonical_units or {},
        values=values,
    )


# Public deterministic construction seam for callers and regression tests.
build_manifest = _build_manifest


# ---------------------------------------------------------------------------
# Stage B — capture-every-number enumerate mode (no allowlist).
#
# The allowlist path above asks for a FIXED set of tier_1 names and silently
# drops every other number in the document (the program's G1 blocker — "everything
# unlisted stays as prose in the brief and is dropped"). This path inverts that:
# it asks the LLM to enumerate EVERY labeled numeric fact in the document, no
# allowlist, and lands them via the SAME persist_manifest contract but with
# origin=CAPTURE — so each raw label is canonicalized (collapsing variants onto an
# existing definition or minting a clean CAPTURE one) and the long tail becomes
# askable / DIY-pickable / DCF-resolvable. Idempotent (persist dedups on the
# source doc; we also skip the LLM call entirely for an already-captured doc) and
# budget-capped per doc (input chars + max facts).
#
# Scope note: this enumerates the on-disk per-quarter brief, which is where the
# unlisted numbers already sit as prose (G1). Reading the RAW filing/IR text via
# filing_text_fetcher for numbers the brief ITSELF lost is the natural next
# increment — deferred until the pilot shows whether the briefs are lossy enough
# to warrant it (keeps this pass bounded + high-precision).
# ---------------------------------------------------------------------------

# Per-doc budget for the enumerate pass.
_CAPTURE_MAX_FACTS = 200
_CAPTURE_MAX_INPUT_CHARS = 80_000

_CAPTURE_STAGE: dict[str, str] = {
    "earnings": "stage_b_capture_earnings",
    "ir": "stage_b_capture_ir",
}


def _enumerate_prompt(
    ticker: str,
    period_label: str,
    text: str,
    max_facts: int,
    *,
    source_scope: Literal["summary_current", "direct_pdf_rich"] = "summary_current",
) -> str:
    spotlighted = spotlight(
        text, source=f"{ticker} {period_label} earnings/IR document (issuer-published)"
    )
    introduction = (
        "There is NO list of metrics to look for — capture every labeled number that is a reported ACTUAL for THIS period: revenue and each revenue line, margins, growth rates, customer/subscriber/account counts, ARPU/ARPAC, take rate, GMV, deposits, loans, NPL, efficiency ratio, capex, free cash flow, headcount, and anything else stated."
        if source_scope == "summary_current"
        else "There is NO list of metrics to look for — capture every labeled number in this direct issuer PDF: current reported actuals, explicit prior-period and prior-year comparatives, year-to-date actuals, and numeric management guidance or outlook. Preserve each source period, basis, scope, dimension, label, and full management wording in its exact quoted excerpt. Guidance is forward-looking, never a realized actual."
    )
    source_token_field = (
        '  "source_value_text": "<for counts, the exact numeric token before scale expansion; it must occur in source_excerpt>",'
        if source_scope == "summary_current"
        else '  "source_value_text": "<for EVERY numeric row, including monetary values and guidance bounds, the exact source numeric token before scale expansion; it must occur in source_excerpt>",'
    )
    rich_fields = (
        (
            ',\n  "reported_period_start": "<ISO date when the source states a duration start; omit when not supported>",'
            '\n  "period_kind": "instant | duration | unknown",'
            '\n  "guidance_horizon": "<literal forecast horizon, or null when unknown>",'
            '\n  "source_definition": "<preserve source definition, including change versus balance; null when unknown>",'
            '\n  "context_witnesses": [{"page": 1, "quote": "<exact context quote from supplied DOCUMENT CONTEXT>", "fields": ["accounting_basis"]}],'
            '\n  "range_bound": "<lower | upper for each endpoint of an explicit guidance range; omit for a point value>"'
        )
        if source_scope == "direct_pdf_rich"
        else ""
    )
    numeric_field_rule = (
        ir_supplement_capture.SUPPLEMENT_NUMERIC_FIELD_INSTRUCTIONS
        + ir_supplement_capture.SUPPLEMENT_CONTEXT_WITNESS_INSTRUCTIONS
        if source_scope == "direct_pdf_rich"
        else ""
    )
    scope_rule = (
        "  - THIS-period reported actuals only. Skip guidance/outlook for a future period and explicit prior-period comparatives. Classify period_role before deciding; never relabel a comparator as current."
        if source_scope == "summary_current"
        else "  - Retain actuals, explicit comparatives, and year-to-date values with their own source period. Set period_role to current, prior_period_comparator, or prior_year_comparator as stated. Set guidance/outlook to guidance; set unresolved forecast context to unknown. Never relabel either as current."
    )
    range_rule = (
        ""
        if source_scope == "summary_current"
        else "\n  - For each explicit two-endpoint guidance range, return TWO rows with the SAME verbatim source_excerpt and source label: one range_bound lower and one range_bound upper. Keep each exact endpoint token in source_value_text and the numeric value in the standard full-unit convention. Never invent a midpoint, endpoint, or realized value."
    )
    return f"""You are enumerating EVERY reported numeric fact for {ticker} ({period_label}) from the document below.

{introduction}

Return a JSON ARRAY. Each element:
{{
  "label": "<the metric's name as a clean noun phrase — e.g. 'Gross merchandise volume', NOT a whole sentence>",
  "value": <numeric only, no unit symbols>,
  "unit": EXACTLY ONE of "percent" | "actual" | "count" | "ratio" | "bps",
  "currency": "ISO currency code required for every actual monetary value (e.g. USD, BRL, DKK); omit for counts and rates",
  "source_excerpt": "<verbatim snippet under 200 characters, copied character-for-character, that contains the value>",
{source_token_field}
  "reported_period_end": "<ISO date for this value>",
  "period_role": "current | prior_period_comparator | prior_year_comparator | guidance | unknown",
  "accounting_basis": "gaap | non_gaap | management | unknown",
  "consolidation_scope": "consolidated | geography | segment | product | other | unknown",
  "dimensions": {{"<axis>":"<member>"}},
  "source_unit_scale": "none | thousands | millions | billions | unknown",
  "source_row_label": "<verbatim table row label when present>",
  "source_column_header": "<verbatim table column header when present>"{rich_fields}
}}

Unit conventions (identical to our standard):
  - "percent" — rates/margins/growth/retention (NRR 120% -> 120; NIM 17.8% -> 17.8). Drop the % sign; do NOT divide by 100.
  - "actual"  — ALL monetary amounts, as the FULL figure in the issuer's base currency, NEVER pre-scaled ("$1.2 billion" -> 1200000000; ARPAC "$11.20" -> 11.20).
    Include its explicit ISO "currency" code; never infer one.
  - "count"   — headcount/customers/subscribers/accounts as the full integer ("12.5 million customers" -> 12500000).
  - "ratio"   — unitless multiples reported with an "x" (coverage 1.8x -> 1.8).
  - "bps"     — only when the document states the metric in basis points.

{numeric_field_rule}
Rules:
{scope_rule}{range_rule}
  - For continuation inputs with CONTEXT ONLY and EXTRACT ONLY FROM THIS SOURCE SECTION blocks, use context for headers, currency, units, period, accounting basis, scope and source definitions only. Enumerate and quote facts only from the source section.
  - For direct PDF input, preserve instant versus duration, duration start, average versus ending level, flow change versus balance, forecast horizon and source definition. Do not invent a start date or accounting basis. Use unknown/null when the supplied source does not establish a field. Management authorship alone does not establish accounting_basis management. Use context_witnesses only for exact supplied context quotes, with their page and supported field names; these are unreviewed evidence, never admission.
  - One row per distinct metric, reported period, accounting basis, scope and dimensions. Preserve repeated labels for distinct scopes. Do not invent — if you cannot quote it verbatim, omit it.
  - Return at most {max_facts} rows. Return ONLY the JSON array — no markdown fence, no commentary.

Document text:
{spotlighted}"""


def _validated_enumerated_rows(
    raw: object,
    *,
    source_scope: Literal["summary_current", "direct_pdf_rich"],
    saturation_limit: int | None = None,
) -> list[_EnumeratedKpi]:
    rows = TypeAdapter(list[_EnumeratedKpi]).validate_python(raw)
    if source_scope == "summary_current":
        return rows
    ranges: dict[tuple[object, ...], dict[str, Decimal]] = {}
    for row in rows:
        if row.source_value_text is None or not source_numeric_token_occurs(
            row.source_value_text, row.source_excerpt
        ):
            raise ValueError("direct IR PDF numeric token must occur in its exact source quote")
        source_number = parse_source_numeric(row.source_value_text)
        expected_value: Decimal | None = None
        if row.unit == "actual" and row.source_unit_scale is not KpiUnitScale.UNKNOWN:
            source_unit = (
                Unit.ACTUAL
                if row.source_unit_scale is KpiUnitScale.NONE
                else Unit(row.source_unit_scale.value)
            )
            expected_value = convert_unit(source_number, source_unit, Unit.ACTUAL)
        elif row.unit == "count" and row.source_unit_scale is not KpiUnitScale.UNKNOWN:
            expected_value = normalize_source_numeric(
                source_number, unit=Unit.COUNT, unit_scale=row.source_unit_scale
            )
        elif row.unit in {"percent", "ratio", "bps"}:
            expected_value = source_number
        # A word such as decrease can supply a sign absent from the literal token.
        # Preserve that source interpretation; never permit a magnitude mismatch.
        if expected_value is not None and (
            abs(row.value) != abs(expected_value)
            or (expected_value < 0 and row.value != expected_value)
        ):
            raise ValueError("direct IR PDF value conflicts with its source token and scale")
        if row.range_bound is None:
            continue
        key = (
            row.label,
            row.source_excerpt,
            row.reported_period_start,
            row.reported_period_end,
            row.accounting_basis,
            row.consolidation_scope,
            tuple(sorted(row.dimensions.items())),
            row.unit,
            row.currency,
            row.source_unit_scale,
        )
        bounds = ranges.setdefault(key, {})
        if row.range_bound in bounds:
            raise ValueError("direct IR PDF guidance range repeats an endpoint")
        bounds[row.range_bound] = row.value
    if (saturation_limit is None or len(rows) < saturation_limit) and any(
        set(bounds) != {"lower", "upper"} or bounds["lower"] >= bounds["upper"]
        for bounds in ranges.values()
    ):
        raise ValueError("direct IR PDF guidance range requires both exact endpoints")
    return rows


def _llm_extract_enumerate(
    ticker: str,
    period_label: str,
    text: str,
    *,
    max_facts: int = _CAPTURE_MAX_FACTS,
    model: str | None = None,
    backend: str | None = None,
    db_path: Path | None = None,
    run_id: str | None = None,
    exchange_observer: Callable[[StructuredCallResult[list[_EnumeratedKpi]]], None] | None = None,
    source_scope: Literal["summary_current", "direct_pdf_rich"] = "summary_current",
    source_guardrail: Callable[[object], tuple[bool, str]] | None = None,
    raw_response_observer: Callable[[str, str], None] | None = None,
) -> list[dict[str, object]]:
    """One governed structured call — enumerate labeled this-period numeric facts (no
    allowlist). Returns a list of ``{label, value, unit, source_excerpt}`` dicts.

    The unit vocabulary + money convention mirror ``_llm_extract`` exactly, so a
    captured value reconciles/validates identically to a watchlist one; the only
    difference is the absence of a name allowlist.
    """
    if not text.strip():
        return []
    # Spotlight the untrusted document text: it is issuer-published / IR / filing
    # prose that can carry adversarial instructions. Wrapping it in tamper-evident
    # markers marks it as DATA, not instructions (sec-llm; the enumerate path is a
    # no-allowlist capture so the doc text reaches the model verbatim).
    prompt = _enumerate_prompt(ticker, period_label, text, max_facts, source_scope=source_scope)
    if exchange_observer is not None:
        exchange = call_llm_structured_with_raw(
            prompt,
            purpose="kpi_summary_enumerate",
            expect="array",
            schema=TypeAdapter(list[_EnumeratedKpi]),
            domain_guardrail=source_guardrail,
            raw_response_observer=raw_response_observer,
            repair_prompt=(
                (lambda error: ir_supplement_capture.repair_supplement_prompt(prompt, error))
                if source_guardrail is not None
                else None
            ),
            ticker=ticker,
            run_id=run_id,
            model=model,
            backend=backend,
            db_path=db_path,
        )
        exchange_observer(exchange)
        parsed = exchange.value
    else:
        parsed = call_llm_structured(
            prompt,
            purpose="kpi_summary_enumerate",
            model=model,
            backend=backend,
            db_path=db_path,
            expect="array",
            schema=TypeAdapter(list[_EnumeratedKpi]),
        )
    validated = _validated_enumerated_rows(
        parsed, source_scope=source_scope, saturation_limit=max_facts
    )
    if len(validated) >= max_facts:
        raise SupplementSaturatedError(
            "IR capture reached its fact limit; chunk coverage is unknown"
        )
    out: list[dict[str, object]] = []
    for item in validated:
        out.append(item.model_dump(mode="python"))
        if len(out) >= max_facts:
            break
    return out


def _build_capture_manifest(
    ticker: str,
    period_end: datetime,
    fpt: FiscalPeriodType,
    doc_id: int,
    rows: list[dict[str, object]],
    *,
    primary_source: SourceType = SourceType.LLM_EXTRACTED,
    locate_quote: Callable[[str | None], FactLocator | None] | None = None,
    conn: sqlite3.Connection | None = None,
    source_text: str | None = None,
) -> KpiExtractionManifest:
    """Convert enumerated rows to a CAPTURE manifest. Names are passed verbatim;
    persist_manifest (origin=CAPTURE) canonicalizes them on write.

    ``primary_source`` is the tier the captured facts ingest at. The on-disk
    brief/press path leaves the default ``LLM_EXTRACTED`` (the brief itself is an
    LLM artifact). The supplement/data-pack PDF path passes ``IR_DOC`` — the PDF
    is the issuer's own document, so its enumerated values outrank the LLM brief
    long tail on read (the tier-aware loader prefers IR_DOC over LLM_EXTRACTED for
    the same definition+period), exactly as the audited IR spreadsheet does.

    ``locate_quote`` (provenance click-through Phase B, §3.2): the supplement
    PDF path passes a ``pipeline.locators.PdfQuoteLocator`` so each value whose
    ``source_excerpt`` is found verbatim in the source PDF persists with a
    renderable ``pdf_slide`` locator (page number + bbox where
    ``page.search_for`` derives one) instead of the escape hatch.

    ``source_text``/``conn`` (Phase C, §3.3): when ``locate_quote`` is absent
    or returns nothing for a given row, ``_locator_for_excerpt`` verifies the
    excerpt against ``source_text`` (the on-disk brief itself) instead — the
    on-disk-summary enumerate path (``capture_for_ticker``) passes these; the
    PDF-supplement path leaves them ``None`` since ``locate_quote`` already
    covers it. An excerpt that can't be located/verified by EITHER path keeps
    the escape hatch — an honest legacy row beats a fabricated anchor.
    """
    values: list[KpiValue] = []
    for row in rows:
        name = str(row.get("label") or "").strip()[:200]
        if not name:
            continue
        v = parse_decimal_value(row.get("value"))
        if v is None:
            continue
        try:
            unit = Unit(str(row.get("unit") or "actual"))
        except ValueError:
            unit = Unit.ACTUAL
        currency = _currency_for_monetary_value(row.get("currency"), unit, name)
        excerpt_raw = row.get("source_excerpt")
        excerpt = (
            excerpt_raw.strip()[:1024]
            if isinstance(excerpt_raw, str) and excerpt_raw.strip()
            else None
        )
        located = locate_quote(excerpt) if locate_quote is not None else None
        if located is None:
            located_or_hatch = _locator_for_excerpt(
                excerpt=excerpt,
                doc_id=doc_id,
                source_text=source_text,
                conn=conn,
                ticker=ticker,
                period_end=period_end,
                kpi_name=name,
                default=_NO_LOCATOR_ENUMERATE,
            )
        else:
            located_or_hatch = located
        values.append(
            KpiValue(
                name=name,
                value=v,
                unit=unit,
                currency=currency,
                confidence=0.85,
                source_excerpt=excerpt,
                source_value_text=(
                    str(row["source_value_text"])
                    if row.get("source_value_text") is not None
                    else None
                ),
                locator=located_or_hatch,
                semantic_context=_semantic_context_for_payload(
                    name=name,
                    payload=row,
                    expected_period_end=period_end,
                    allow_admission=primary_source is not SourceType.LLM_EXTRACTED,
                ),
            )
        )
    return KpiExtractionManifest(
        ticker=ticker,
        period_end=period_end,
        fiscal_period_type=fpt,
        source_doc_id=doc_id,
        primary_source=primary_source,
        model_name=FAST_CLASSIFIER_MODEL,
        origin=DefinitionOrigin.CAPTURE,
        values=values,
    )


def _doc_already_captured(conn: sqlite3.Connection, doc_id: int) -> bool:
    """Avoid rebilling documents that already have captured facts.

    This legacy idempotency check does not prove extraction completeness.
    Partial and older capped captures remain unknown until explicitly refreshed.
    """
    row = conn.execute(
        "SELECT 1 FROM kpi_facts WHERE source_doc_id = ? LIMIT 1", (doc_id,)
    ).fetchone()
    return row is not None


def capture_for_ticker(
    ticker: str,
    repo_root: Path,
    conn: sqlite3.Connection,
    *,
    source_group: str = "ir",
    refresh: bool = False,
    max_facts_per_doc: int = _CAPTURE_MAX_FACTS,
    max_input_chars: int = _CAPTURE_MAX_INPUT_CHARS,
) -> TickerExtractionLog:
    """Enumerate-mode (no allowlist) capture for one ticker's on-disk briefs.

    Mirrors :func:`extract_for_ticker`'s source discovery + doc registration but
    enumerates every numeric fact and persists with origin=CAPTURE. No holdings
    JSON required (there's no allowlist). Idempotent: an already-captured doc is
    skipped without an LLM call unless ``refresh``.
    """
    ticker = ticker.upper()
    if source_group not in _SOURCE_GROUPS:
        raise ValueError(
            f"unknown source_group {source_group!r}; expected one of {list(_SOURCE_GROUPS)}"
        )
    log = TickerExtractionLog(ticker=ticker, stage=_CAPTURE_STAGE[source_group])
    log.started_at = _now_iso_z()
    t0 = time.perf_counter()
    if conn.in_transaction:
        log.error = "KPI capture requires an owned transaction boundary before source preparation"
        return _close_log(log, t0)

    sources = _list_sources(repo_root, ticker, _SOURCE_GROUPS[source_group])
    # Coverage accounting (PR3): every enumerated fact is captured, dropped by the
    # persist guards, or unparseable — surfaced in the capture coverage log.
    cov_seen = cov_captured = cov_dropped = cov_unparseable = 0
    for quarter, year, source_path, spec in sources:
        period_end = _canonical_summary_period_end(
            conn, ticker, _period_end(ticker, quarter, year), source_path, spec.doc_type
        )
        period_label = f"Q{quarter} {year} [{spec.name}]"
        log.quarters_attempted.append(period_label)
        try:
            doc_id = _ensure_summary_document_row(
                conn, ticker, period_end, source_path, spec.doc_type
            )
            if not refresh and _doc_already_captured(conn, doc_id):
                log.quarters_skipped_no_missing.append(period_label)
                continue
            text = _complete_capture_text(source_path.read_text(encoding="utf-8"), max_input_chars)
            rows = _llm_extract_enumerate(ticker, period_label, text, max_facts=max_facts_per_doc)
            if not rows:
                continue
            manifest = _build_capture_manifest(
                ticker,
                period_end,
                FiscalPeriodType(f"Q{quarter}"),
                doc_id,
                rows,
                conn=conn,
                source_text=text,
            )
            # Source-anchor validation belongs to this preparation phase, not
            # the following atomic financial manifest transaction.
            conn.commit()
            result = _persist_accounted(
                conn,
                directive=f"capture_kpis_from_{source_group}",
                ticker=ticker,
                invocation_inputs={
                    "document_id": doc_id,
                    "period_end": period_end.isoformat(),
                    "source_group": source_group,
                    "source": file_fingerprint(source_path, root=repo_root),
                    "refresh": refresh,
                    "max_facts_per_doc": max_facts_per_doc,
                    "max_input_chars": max_input_chars,
                },
                manifest=manifest,
                force=refresh,
            )
            log.kpis_inserted_total += result.inserted
            log.quarters_extracted.append(period_label)
            cov_seen += len(rows)
            cov_captured += result.inserted
            cov_dropped += result.validation_issues
            # Rows the LLM returned but that didn't survive to a value (bad value
            # token / blank label) are dropped before persist — count them.
            cov_unparseable += len(rows) - len(manifest.values)
        except PipelineRunSuppressedError:
            raise
        except Exception as e:
            log.error = f"{period_label}: {type(e).__name__}: {e}"
            break

    if cov_seen:
        skipped = {"unparseable_or_blank": cov_unparseable} if cov_unparseable else {}
        record_coverage(
            repo_root,
            CaptureCoverageRecord(
                stage="enumerate_capture",
                ticker=ticker,
                source=source_group,
                seen=cov_seen,
                captured=cov_captured,
                dropped_validation=cov_dropped,
                skipped=skipped,
            ),
        )
    return _close_log(log, t0)


# ---------------------------------------------------------------------------
# Stage B — capture-every-number for IR supplement / data-pack PDFs.
#
# Unlike capture_for_ticker (which enumerates the on-disk `.tmp/` briefs), the
# `ir_supplement` doc kind has NO LLM brief handler — process_ir_documents never
# summarizes it (directives/intake_documents.md), so the brief-driven capture
# never sees it and its numbers stay locked in the PDF. This path reads each
# registered supplement PDF straight from its `documents` row, runs the SAME
# enumerate prompt + canonicalize-on-write contract, and persists at IR_DOC tier
# (the PDF is the issuer's own document) — mirroring the audited IR-spreadsheet
# capture path (ir_pipeline/ingest.py) but for the prose/table PDF shape.
#
# Scope: PDF supplements only (the LLM/PDF path). A spreadsheet (.xlsx) supplement
# is the deterministic ir_pipeline.ingest domain and is left for that path.
# ---------------------------------------------------------------------------

# IR-doc kinds whose every labeled number we enumerate from the PDF itself.
_IR_CAPTURE_PDF_DOC_TYPES: tuple[str, ...] = ("ir_supplement",)


def _complete_capture_text(text: str, max_input_chars: int) -> str:
    """Reject incomplete inputs before capture can be marked as complete."""
    if not text.strip():
        raise ValueError("IR capture source has no extractable text; OCR may be required")
    if len(text) > max_input_chars:
        raise ValueError(
            f"IR capture source exceeds input limit ({len(text)} > {max_input_chars}); "
            "full-document capture requires a larger approved budget or chunked extraction"
        )
    return text


def _read_pdf_text(path: str) -> str:
    """Use the full-text owner's source projection for KPI candidates."""
    return project_source_text(Path(path).read_bytes(), path, "application/pdf")


def _fiscal_period_type_for(ticker: str, period_end: datetime) -> FiscalPeriodType:
    """Fiscal quarter for a period-end date, honouring non-calendar fiscal years.

    Most issuers are calendar-FY (Mar-31 = Q1 … Dec-31 = Q4). Tickers in
    ``_TICKER_QUARTER_PERIOD_END`` (RBRK/VEEV Jan FYE, AMAT/TOL Oct FYE) report
    a period-end that doesn't line up with the calendar-quarter mapping — so we
    reverse that override map (matching on (month, day) only; year_offset is
    irrelevant here since period_end's own year is already known) to recover
    the fiscal label the analyst series already uses (a naive calendar mapping
    would fork the supplement's values onto a different fiscal axis).
    """
    overrides = _TICKER_QUARTER_PERIOD_END.get(ticker.upper())
    if overrides:
        for quarter, (month, day, _year_offset) in overrides.items():
            if period_end.month == month and period_end.day == day:
                return FiscalPeriodType(f"Q{quarter}")
    return FiscalPeriodType(f"Q{(period_end.month - 1) // 3 + 1}")


def _coerce_period_end(raw: object) -> datetime | None:
    """Parse a ``documents.period_end`` cell (datetime or ISO string) → datetime."""
    if isinstance(raw, datetime):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            return datetime.fromisoformat(raw.strip())
        except ValueError:
            return None
    return None


def _semantic_context_for_payload(
    *,
    name: str,
    payload: Mapping[str, object],
    expected_period_end: datetime,
    allow_admission: bool,
) -> KpiSemanticContext:
    """Admit only a source-classified current-period, fully-scoped KPI value."""
    raw_end = payload.get("reported_period_end")
    if isinstance(raw_end, datetime):
        reported_end = raw_end.date()
    elif isinstance(raw_end, date):
        reported_end = raw_end
    elif isinstance(raw_end, str):
        try:
            reported_end = date.fromisoformat(raw_end[:10])
        except ValueError:
            reported_end = None
    else:
        reported_end = None
    try:
        period_role = KpiPeriodRole(str(payload.get("period_role") or "unknown"))
    except ValueError:
        period_role = KpiPeriodRole.UNKNOWN
    try:
        basis = KpiAccountingBasis(str(payload.get("accounting_basis") or "unknown"))
    except ValueError:
        basis = KpiAccountingBasis.UNKNOWN
    try:
        scope = KpiConsolidationScope(str(payload.get("consolidation_scope") or "unknown"))
    except ValueError:
        scope = KpiConsolidationScope.UNKNOWN
    try:
        unit_scale = KpiUnitScale(str(payload.get("source_unit_scale") or "unknown"))
    except ValueError:
        unit_scale = KpiUnitScale.UNKNOWN
    dimensions_raw = payload.get("dimensions")
    dimensions = (
        {
            str(key): str(value)
            for key, value in cast("dict[object, object]", dimensions_raw).items()
        }
        if isinstance(dimensions_raw, dict)
        else {}
    )
    reason: str | None = None
    if not allow_admission:
        reason = "synthesized_source_requires_primary_review"
    elif period_role is not KpiPeriodRole.CURRENT:
        reason = f"period_role_{period_role.value}"
    elif reported_end != expected_period_end.date():
        reason = "reported_period_mismatch"
    elif basis is KpiAccountingBasis.UNKNOWN:
        reason = "accounting_basis_unknown"
    elif scope is KpiConsolidationScope.UNKNOWN:
        reason = "consolidation_scope_unknown"
    elif unit_scale is KpiUnitScale.UNKNOWN:
        reason = "unit_scale_unknown"
    elif (
        scope
        in {
            KpiConsolidationScope.GEOGRAPHY,
            KpiConsolidationScope.SEGMENT,
            KpiConsolidationScope.PRODUCT,
        }
        and not dimensions
    ):
        reason = "scope_dimensions_missing"
    status = KpiSemanticStatus.ADMITTED if reason is None else KpiSemanticStatus.QUARANTINED
    return KpiSemanticContext(
        metric_name_as_reported=name,
        reported_period_end=reported_end,
        period_role=period_role,
        publication_lane={
            KpiPeriodRole.CURRENT: KpiPublicationLane.CURRENT_ACTUAL,
            KpiPeriodRole.PRIOR_PERIOD_COMPARATOR: KpiPublicationLane.COMPARATOR,
            KpiPeriodRole.PRIOR_YEAR_COMPARATOR: KpiPublicationLane.COMPARATOR,
            KpiPeriodRole.GUIDANCE: KpiPublicationLane.GUIDANCE_TARGET,
            KpiPeriodRole.UNKNOWN: KpiPublicationLane.UNCLASSIFIED,
        }[period_role],
        accounting_basis=basis,
        consolidation_scope=scope,
        dimensions=dimensions,
        unit_scale=unit_scale,
        source_row_label=(
            str(payload["source_row_label"]) if payload.get("source_row_label") else None
        ),
        source_column_header=(
            str(payload["source_column_header"]) if payload.get("source_column_header") else None
        ),
        status=status,
        reason_code=reason,
    )


def _currency_for_monetary_value(raw: object, unit: Unit, name: str) -> Currency | None:
    """Accept an explicit extractor currency; never supply a ticker default."""
    if unit not in {Unit.ACTUAL, Unit.THOUSANDS, Unit.MILLIONS, Unit.BILLIONS}:
        return None
    if isinstance(raw, Currency):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError(f"monetary KPI {name!r} has no explicit source currency")
    try:
        return Currency(raw.strip().upper())
    except ValueError as exc:
        raise ValueError(f"monetary KPI {name!r} has unsupported currency {raw!r}") from exc


def _list_ir_capture_pdf_docs(
    conn: sqlite3.Connection,
    repo_root: Path,
    ticker: str,
    doc_types: tuple[str, ...],
) -> list[tuple[int, Path, datetime]]:
    """Return ``(doc_id, pdf_path, period_end)`` for this ticker's capture-eligible
    IR PDF docs, oldest first.

    Drops rows that can't anchor a per-period capture: a non-PDF file (a `.xlsx`
    supplement is the deterministic spreadsheet path's job) or a NULL/unparseable
    ``period_end``. Relative ``file_path`` values are resolved against ``repo_root``
    (IR documents live in the MAIN checkout, not the worktree)."""
    placeholders = ",".join("?" for _ in doc_types)
    rows = conn.execute(
        f"SELECT id, file_path, period_end FROM documents "
        f"WHERE ticker = ? AND source_type = ? AND doc_type IN ({placeholders}) "
        f"ORDER BY period_end, id",
        (ticker.upper(), SourceType.IR_DOC.value, *doc_types),
    ).fetchall()
    out: list[tuple[int, Path, datetime]] = []
    for r in rows:
        raw_path = str(r["file_path"]) if r["file_path"] else ""
        if not raw_path.lower().endswith(".pdf"):
            continue
        period_end = _coerce_period_end(r["period_end"])
        if period_end is None:
            continue
        path = Path(raw_path)
        if not path.is_absolute():
            path = repo_root / path
        out.append((int(r["id"]), path, period_end))
    return out


def _require_distinct_capture_identities(
    conn: sqlite3.Connection, manifest: KpiExtractionManifest
) -> None:
    """Block contexts the current KPI fact identity cannot retain separately.

    Native chunk artifacts retain each candidate. This guard does not expand
    the financial persistence owner's identity or silently rename source labels.
    """
    # Use the writer's evolving registry in a reversible savepoint. Novel aliases
    # must resolve as they would after earlier definitions in the same manifest.
    conn.execute("SAVEPOINT ir_capture_identity_preflight")
    try:
        identities: dict[int, str] = {}
        planned_fact_counts: dict[int, int] = {}
        for value in manifest.values:
            name = canonical_metric_name(
                conn,
                manifest.ticker,
                value.name,
                value.unit,
                planned_fact_counts=planned_fact_counts,
            )
            definition_id = find_or_create_kpi_definition(
                conn,
                ticker=manifest.ticker,
                name=name,
                unit=value.unit,
                primary_source=manifest.primary_source,
                origin=manifest.origin,
            )
            planned_fact_counts[definition_id] = planned_fact_counts.get(definition_id, 0) + 1
            context = value.semantic_context
            signature = json.dumps(
                {
                    "value": str(value.value),
                    "unit": value.unit.value,
                    "currency": value.currency.value if value.currency else None,
                    "reported_period_end": context.reported_period_end.isoformat()
                    if context and context.reported_period_end
                    else None,
                    "period_role": context.period_role.value if context else None,
                    "accounting_basis": context.accounting_basis.value if context else None,
                    "scope": context.consolidation_scope.value if context else None,
                    "dimensions": context.dimensions if context else {},
                    "unit_scale": context.unit_scale.value if context else None,
                },
                sort_keys=True,
            )
            previous = identities.setdefault(definition_id, signature)
            if previous != signature:
                raise ValueError(
                    f"IR distinct source contexts collide in current KPI persistence: {name}; "
                    "candidates retained in native chunk artifacts; financial writes blocked"
                )
    finally:
        conn.execute("ROLLBACK TO SAVEPOINT ir_capture_identity_preflight")
        conn.execute("RELEASE SAVEPOINT ir_capture_identity_preflight")


def retain_and_publish_reviewed_ir_source(
    conn: sqlite3.Connection,
    *,
    document_version_id: str,
    source_sha256: str,
    raw_pdf_bytes: bytes,
    page_rows: list[tuple[int, list[dict[str, object]]]],
    exchanges: list[SupplementExchange],
    max_facts_per_doc: int,
    extractor_config_sha256: str,
    extraction_started_at: datetime,
    precommit_validation: Callable[[], None] | None = None,
) -> None:
    """Retain every raw candidate; publish only a pre-existing exact review."""
    responses = tuple(
        IrRawExchange(
            raw_response=exchange.raw_response,
            response_sha256=exchange.response_sha256,
            prompt_sha256=exchange.prompt_sha256,
            model=exchange.model,
            provider=exchange.provider,
            transport=exchange.transport,
            llm_call_id=exchange.llm_call_id,
        )
        for exchange in exchanges
    )
    retained_exchanges = {response.llm_call_id: response for response in responses}
    capture_versions = [
        ir_supplement_capture.historical_capture_version(conn, exchange, source_sha256)
        or "v13-complete-numeric-prose-end"
        for exchange in exchanges
    ]
    if len(set(capture_versions)) > 1:
        raise ValueError("IR governed exchanges mix producing capture versions")
    capture_version = capture_versions[0] if capture_versions else "v13-complete-numeric-prose-end"
    historical = capture_version != "v13-complete-numeric-prose-end"

    def retained_rows(raw: object) -> list[_EnumeratedKpi]:
        if historical:
            # Keep old raw wire failures inspectable, not silently corrected or
            # admitted. Only immutable producing artifacts select this path.
            return TypeAdapter(list[_EnumeratedKpi]).validate_python(raw)
        return _validated_enumerated_rows(
            raw, source_scope="direct_pdf_rich", saturation_limit=max_facts_per_doc
        )

    processed: list[tuple[SupplementExchange, list[dict[str, object]]]] = []
    for exchange in exchanges:
        parsed: object = parse_json_payload(exchange.raw_response, expect="array")
        source_rows = retained_rows(parsed)
        if len(source_rows) < max_facts_per_doc:
            if not isinstance(parsed, list) or any(
                not isinstance(row, dict) for row in cast("list[object]", parsed)
            ):
                raise ValueError("IR governed response is not an array of raw rows")
            processed.append((exchange, cast("list[dict[str, object]]", parsed)))
    if len(processed) != len(page_rows):
        raise ValueError("IR page sections and governed responses do not match")
    native_pages = ir_supplement_capture.project_pdf_page_texts(raw_pdf_bytes)
    raw_rows: list[IrRawRow] = []
    for (page, rows), (exchange, original) in zip(page_rows, processed, strict=True):
        validated = [item.model_dump(mode="json") for item in retained_rows(original)]
        normalized_rows = [item.model_dump(mode="json") for item in retained_rows(rows)]
        if validated != normalized_rows:
            raise ValueError("IR page rows differ from their governed raw response")
        for ordinal, original_row in enumerate(original):
            quote = original_row.get("source_excerpt")
            if not isinstance(quote, str) or page < 1 or page > len(native_pages):
                raise ValueError("IR native quote or page is missing")
            native_page = native_pages[page - 1]
            span = locate_char_span(quote, native_page, whitespace_only=True)
            if span is None:
                raise ValueError("IR native quote span is missing or ambiguous")
            raw_rows.append(
                IrRawRow(
                    page_number=page,
                    response_sha256=exchange.response_sha256,
                    exchange_sha256=retained_exchanges[exchange.llm_call_id].exchange_sha256,
                    response_row_ordinal=ordinal,
                    raw_row=original_row,
                    native_source_quote=native_page[slice(*span)],
                    native_char_start=span[0],
                    native_char_end=span[1],
                )
            )
    completed_at = datetime.now(UTC)
    population = retain_ir_pdf_candidates(
        conn,
        IrCandidateRetentionRequest(
            document_version_id=document_version_id,
            source_sha256=source_sha256,
            extractor_name="ir-supplement-native-continuation",
            extractor_code_version=capture_version,
            extractor_config_sha256=extractor_config_sha256,
            extraction_started_at=extraction_started_at,
            extraction_completed_at=completed_at,
            responses=responses,
            rows=tuple(raw_rows),
        ),
        raw_pdf_bytes=raw_pdf_bytes,
        page_projection=ir_supplement_capture.project_pdf_page_texts,
        precommit_validation=precommit_validation,
    )
    reviewed = find_exact_ir_source_review(conn, population, raw_pdf_bytes=raw_pdf_bytes)
    if reviewed is None:
        logger.info(
            {
                "event": "ir_source_candidate_disposition",
                "document_version_id": document_version_id,
                "candidate_count": len(population.candidate_node_ids),
                "source_publication_count": 0,
                "disposition": "pending_source_context_review",
            }
        )
        return
    review_node_id, review = reviewed
    accepted = tuple(
        entry.fact for entry in review.entries if entry.disposition == "accepted" and entry.fact
    )
    if not accepted:
        logger.info(
            {
                "event": "ir_source_candidate_disposition",
                "document_version_id": document_version_id,
                "candidate_count": len(population.candidate_node_ids),
                "source_publication_count": 0,
                "disposition": "reviewed_no_accepted_source_facts",
            }
        )
        return
    now = datetime.now(UTC)
    issuer_row = conn.execute(
        "SELECT issuer_id FROM evidence_document_versions WHERE document_version_id=?",
        (document_version_id,),
    ).fetchone()
    if issuer_row is None:
        raise ValueError("IR reviewed source document disappeared")
    subject = ReportingEntityRegistry(conn).canonicalize_recorded_subject(
        str(issuer_row[0]), knowledge_at=now
    )
    if subject.reporting_entity_id is None or subject.material_dissent:
        raise ValueError("IR reviewed source subject is unresolved")
    receipt = publish_ir_reported_facts(
        conn,
        IrReportedPublicationRequest(
            document_version_id=document_version_id,
            source_sha256=source_sha256,
            subject_binding_revision_id=subject.binding_revision_id,
            reporting_entity_id=subject.reporting_entity_id,
            scope_security_id=subject.security_id,
            concept_namespace="issuer-ir-pdf-source:" + str(issuer_row[0]),
            extractor_name="ir-supplement-native-continuation",
            extractor_code_version=capture_version,
            extractor_config_sha256=extractor_config_sha256,
            candidate_population_sha256=population.candidate_population_sha256,
            review_node_id=review_node_id,
            extraction_started_at=extraction_started_at,
            extraction_completed_at=completed_at,
            knowledge_at=now,
            recorded_at=now,
            candidates=accepted,
        ),
        raw_pdf_bytes=raw_pdf_bytes,
        precommit_validation=precommit_validation,
    )
    logger.info(
        {
            "event": "ir_source_candidate_disposition",
            "document_version_id": document_version_id,
            "candidate_count": len(population.candidate_node_ids),
            "reviewed_count": len(review.entries),
            "source_publication_count": len(receipt.observation_ids),
            "disposition": "reviewed_source_only",
            "exact_replay": receipt.exact_replay,
        }
    )


def capture_for_ir_pdf_docs(
    ticker: str,
    repo_root: Path,
    conn: sqlite3.Connection,
    *,
    doc_types: tuple[str, ...] = _IR_CAPTURE_PDF_DOC_TYPES,
    refresh: bool = False,
    max_facts_per_doc: int = _CAPTURE_MAX_FACTS,
    max_input_chars: int = _CAPTURE_MAX_INPUT_CHARS,
    max_chunks: int = 256,
    max_structured_calls: int = 256,
) -> TickerExtractionLog:
    """Process bounded native-page sections from registered supplement PDFs.

    The existing derived-artifact cache resumes exact source/model/prompt inputs.
    Raw candidates are retained after all planned sections pass. Only an exact
    manual source-context review can publish source-only facts. The legacy
    ``max_facts_per_doc`` parameter is a per-section saturation limit; exceeding
    it requires smaller source sections. Blank/image-only pages, failed sections,
    exhausted budgets, and ungrounded quotes remain unresolved. Processing every
    page does not prove financial fact recall.
    """
    ticker = ticker.upper()
    log = TickerExtractionLog(ticker=ticker, stage="stage_b_capture_ir_supplement")
    log.started_at = _now_iso_z()
    t0 = time.perf_counter()
    if conn.in_transaction:
        log.error = "KPI capture requires an owned transaction boundary before source preparation"
        return _close_log(log, t0)

    for doc_id, pdf_path, period_end in _list_ir_capture_pdf_docs(
        conn, repo_root, ticker, doc_types
    ):
        period_label = f"{period_end.date().isoformat()} [ir_supplement]"
        log.quarters_attempted.append(period_label)
        extraction_run_id: str | None = None
        try:
            if not pdf_path.exists():
                logger.info(
                    {"event": "ir_supplement_pdf_missing", "ticker": ticker, "path": str(pdf_path)}
                )
                raise FileNotFoundError("IR capture source is missing")
            snapshot, raw_bytes = read_stable_artifact(pdf_path)
            document = conn.execute(
                "SELECT doc_type FROM documents WHERE id=?", (doc_id,)
            ).fetchone()
            if document is None:
                raise ValueError("IR source document is not registered")
            evidence = require_legacy_document_evidence(
                conn,
                document_id=doc_id,
                ticker=ticker,
                source_type=SourceType.IR_DOC.value,
                doc_type=str(document[0]),
                period_end=period_end,
                source_sha256=snapshot.file_sha256,
            )
            db_path = Path(conn.execute("PRAGMA database_list").fetchone()[2])
            model, backend = resolve_model_and_backend(
                "kpi_summary_enumerate",
                capability_profile=structured_capability_profile(None),
                db_path=db_path,
            )

            def effective_prompt(prompt: str, *, db_path: Path = db_path) -> str:
                with db_path_context(db_path):
                    return apply_prompt_override("kpi_summary_enumerate", None, prompt)

            def override_identity(*, db_path: Path = db_path) -> str:
                edits = active_prompt_override("kpi_summary_enumerate", db_path=db_path) or ()
                return hashlib.sha256(edits_to_json(edits).encode()).hexdigest()

            def extract_section(
                text: str,
                source_body: str,
                retain_raw: Callable[[SupplementExchange], None],
                *,
                model: str = model,
                backend: str = backend,
                db_path: Path = db_path,
                period_label: str = period_label,
                run_id: str,
                context_guardrail: Callable[[list[dict[str, object]]], None] | None = None,
            ) -> SupplementExchange:
                if (model, backend) != resolve_model_and_backend(
                    "kpi_summary_enumerate",
                    capability_profile=structured_capability_profile(None),
                    db_path=db_path,
                ):
                    raise ValueError("IR continuation model route changed during extraction")
                exchange: StructuredCallResult[list[_EnumeratedKpi]] | None = None

                with db_path_context(db_path), capture_call_records() as captured:

                    def attributed(submitted_prompt: str, raw_response: str) -> SupplementExchange:
                        response_sha = hashlib.sha256(raw_response.encode()).hexdigest()
                        producing = next(
                            (
                                record
                                for record in reversed(captured())
                                if record.purpose == "kpi_summary_enumerate"
                                and record.run_id == run_id
                                and record.error is None
                                and record.outcome == "success"
                                and record.response_sha256 == response_sha
                            ),
                            None,
                        )
                        if producing is None or not producing.provider or not producing.transport:
                            raise ValueError(
                                "IR actual successful governed call cannot be attributed"
                            )
                        final_prompt_sha = producing.prompt_sha256
                        call = conn.execute(
                            "SELECT id FROM llm_calls WHERE run_id=? AND purpose=? AND model=? "
                            "AND prompt_sha256=? AND response_sha256=? AND provider=? AND transport=? "
                            "AND error IS NULL AND outcome='success' ORDER BY id DESC LIMIT 1",
                            (
                                run_id,
                                "kpi_summary_enumerate",
                                producing.model,
                                final_prompt_sha,
                                response_sha,
                                producing.provider,
                                producing.transport,
                            ),
                        ).fetchone()
                        if call is None:
                            raise ValueError(
                                "IR producing governed call was not retained in the selected database"
                            )
                        return SupplementExchange(
                            raw_response=raw_response,
                            submitted_prompt=submitted_prompt,
                            model=producing.model,
                            provider=producing.provider,
                            transport=producing.transport,
                            prompt_sha256=final_prompt_sha,
                            response_sha256=response_sha,
                            llm_call_id=int(call[0]),
                            run_id=run_id,
                        )

                    def observe_raw(submitted_prompt: str, raw_response: str) -> None:
                        retain_raw(attributed(submitted_prompt, raw_response))

                    def observe(value: StructuredCallResult[list[_EnumeratedKpi]]) -> None:
                        nonlocal exchange
                        exchange = value
                        # Backward-compatible successful-exchange observers also
                        # retain before post-schema domain checks in this owner.
                        observe_raw(value.prompt, value.raw_response)

                    def source_guardrail(value: object) -> tuple[bool, str]:
                        try:
                            rows = _validated_enumerated_rows(
                                value,
                                source_scope="direct_pdf_rich",
                                saturation_limit=max_facts_per_doc,
                            )
                            ir_supplement_capture.require_native_source_quotes(
                                [row.model_dump(mode="json") for row in rows], source_body
                            )
                            if context_guardrail is None:
                                raise ValueError("IR context validator is unavailable")
                            context_guardrail([row.model_dump(mode="json") for row in rows])
                        except ValueError as error:
                            return False, str(error)
                        return True, ""

                    with suppress(SupplementSaturatedError):
                        _llm_extract_enumerate(
                            ticker,
                            period_label,
                            text,
                            max_facts=max_facts_per_doc,
                            model=model,
                            backend=backend,
                            db_path=db_path,
                            run_id=run_id,
                            exchange_observer=observe,
                            source_scope="direct_pdf_rich",
                            source_guardrail=source_guardrail,
                            raw_response_observer=observe_raw,
                        )
                    if exchange is None:
                        raise ValueError("IR governed structured exchange is unavailable")
                    return attributed(exchange.prompt, exchange.raw_response)

            def validate_rows(raw: object) -> list[dict[str, object]]:
                return [
                    row.model_dump(mode="json")
                    for row in _validated_enumerated_rows(
                        raw,
                        source_scope="direct_pdf_rich",
                        saturation_limit=max_facts_per_doc,
                    )
                ]

            prompt_sha256 = hashlib.sha256(
                _enumerate_prompt(
                    ticker,
                    period_label,
                    "",
                    max_facts_per_doc,
                    source_scope="direct_pdf_rich",
                ).encode()
            ).hexdigest()
            schema_sha256 = hashlib.sha256(
                json.dumps(TypeAdapter(list[_EnumeratedKpi]).json_schema(), sort_keys=True).encode()
            ).hexdigest()
            projection_identity = source_text_projection_identity(str(pdf_path), "application/pdf")
            extraction_run_id = start_run(
                conn,
                directive="capture_ir_supplement_sections",
                ticker_scope=[ticker],
                invocation_inputs={
                    "document_id": doc_id,
                    "source_sha256": snapshot.file_sha256,
                    "period_end": period_end.isoformat(),
                    "model": model,
                    "backend": backend,
                    "max_input_chars": max_input_chars,
                    "max_facts_per_doc": max_facts_per_doc,
                    "source_scope": "direct_pdf_rich",
                    "max_chunks": max_chunks,
                    "max_structured_calls": max_structured_calls,
                    "continuation_version": "ir-supplement-native-continuation-v13-complete-numeric-prose-end",
                    "prompt_sha256": prompt_sha256,
                    "schema_sha256": schema_sha256,
                    "projection_identity": projection_identity,
                },
            )
            continuation = SupplementCapture(
                conn=conn,
                ticker=ticker,
                document_id=doc_id,
                source_sha256=snapshot.file_sha256,
                model=model,
                backend=backend,
                period_label=period_label,
                max_input_chars=max_input_chars,
                max_facts=max_facts_per_doc,
                max_chunks=max_chunks,
                max_structured_calls=max_structured_calls,
                prompt=partial(
                    _enumerate_prompt,
                    ticker,
                    period_label,
                    max_facts=max_facts_per_doc,
                    source_scope="direct_pdf_rich",
                ),
                extract=partial(extract_section, run_id=extraction_run_id),
                effective_prompt=effective_prompt,
                override_identity=override_identity,
                validate_rows=validate_rows,
                refresh=refresh,
                schema_sha256=schema_sha256,
                projection_identity=projection_identity,
                document_version_id=evidence.document_version_id,
            )
            continuation.extract = partial(
                extract_section,
                run_id=extraction_run_id,
                context_guardrail=continuation.require_context_witnesses,
            )
            source_started_at = datetime.now(UTC)
            page_rows = continuation.capture(raw_bytes)
            assert_artifact_unchanged(snapshot)
            source_config_sha256 = hashlib.sha256(
                json.dumps(
                    {
                        "model": model,
                        "backend": backend,
                        "prompt_sha256": prompt_sha256,
                        "schema_sha256": schema_sha256,
                        "projection_identity": projection_identity,
                        "prompt_override_identity": override_identity(),
                        "max_input_chars": max_input_chars,
                        "max_facts_per_doc": max_facts_per_doc,
                        "source_scope": "direct_pdf_rich",
                    },
                    sort_keys=True,
                ).encode()
            ).hexdigest()
            retain_and_publish_reviewed_ir_source(
                conn,
                document_version_id=evidence.document_version_id,
                source_sha256=snapshot.file_sha256,
                raw_pdf_bytes=raw_bytes,
                page_rows=page_rows,
                exchanges=continuation.exchanges,
                max_facts_per_doc=max_facts_per_doc,
                extractor_config_sha256=source_config_sha256,
                extraction_started_at=source_started_at,
                precommit_validation=partial(assert_artifact_unchanged, snapshot),
            )
            assert_artifact_unchanged(snapshot)
            end_run(conn, extraction_run_id, StageStatus.OK)
            extraction_run_id = None
        except PipelineRunSuppressedError as exc:
            if exc.status is StageStatus.OK:
                log.quarters_skipped_no_missing.append(period_label)
                continue
            raise
        except Exception as e:
            if extraction_run_id is not None:
                end_run(
                    conn,
                    extraction_run_id,
                    StageStatus.FAILED,
                    error_summary=f"{type(e).__name__}: {e}",
                )
            log.error = f"{period_label}: {type(e).__name__}: {e}"
            break

    return _close_log(log, t0)


def _now_iso_z() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _close_log(log: TickerExtractionLog, t0: float) -> TickerExtractionLog:
    log.ended_at = _now_iso_z()
    log.elapsed_ms = int((time.perf_counter() - t0) * 1000)
    return log


def write_log(repo_root: Path, results: list[TickerExtractionLog]) -> Path:
    """Append/overwrite ticker entries in `data/kpi_extraction_log.json`."""
    log_path = repo_root / "data" / "kpi_extraction_log.json"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    decoded: object = json.loads(log_path.read_text(encoding="utf-8")) if log_path.exists() else {}
    existing: dict[str, object] = (
        cast("dict[str, object]", decoded) if isinstance(decoded, dict) else {}
    )
    for r in results:
        ticker_log = existing.get(r.ticker)
        if not isinstance(ticker_log, dict):
            ticker_log = {}
            existing[r.ticker] = ticker_log
        cast("dict[str, object]", ticker_log)[r.stage] = asdict(r)
    log_path.write_text(json.dumps(existing, indent=2), encoding="utf-8")
    return log_path
