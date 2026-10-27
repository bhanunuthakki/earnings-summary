"""Capture issuer-reported facts from registered IR supplement PDFs.

The module retains the governed PDF candidate extraction and review path. It
does not read generated summaries or persist summary-derived KPI values.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from functools import partial
from pathlib import Path
from typing import Literal, cast

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
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
from models.documents import SourceType
from models.facts import Currency
from models.runs import StageStatus
from pipeline.kpi_semantics import (
    KpiAccountingBasis,
    KpiConsolidationScope,
    KpiPeriodRole,
    KpiSourcePrecision,
    KpiUnitScale,
)
from pipeline.locators import locate_char_span
from pipeline.run_accounting import (
    JsonValue,
    PipelineRunSuppressedError,
    end_run,
    start_run,
)
from provenance.fulltext_backfill import source_text_projection_identity
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
from provenance.reporting_entity_registry import ReportingEntityRegistry

# Named `logger` (not `log`) to avoid colliding with the `TickerExtractionLog`
# locals named `log` throughout this module.
logger = logging.getLogger("kpi_extract_summaries")


class _EnumeratedKpi(BaseModel):
    """Strict wire contract for one capture-every-number row."""

    model_config = ConfigDict(extra="forbid")

    label: str = Field(min_length=1, max_length=200)
    value: Decimal
    unit: Literal["percent", "actual", "count", "ratio", "bps"]
    currency: Currency | None = None
    source_excerpt: str = Field(min_length=1, max_length=200)
    source_value_text: str | None = Field(default=None, min_length=1, max_length=80)
    reported_period_start: date | None = None
    reported_period_end: date
    source_precision: KpiSourcePrecision | None = None
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


@dataclass
class TickerExtractionLog:
    """Per-ticker original IR PDF capture run record."""

    ticker: str
    stage: str = "stage_b_capture_ir_supplement"
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


# Per-document limits for the original IR PDF candidate pass.
_CAPTURE_MAX_FACTS = 200
_CAPTURE_MAX_INPUT_CHARS = 80_000


def _enumerate_prompt(
    ticker: str,
    period_label: str,
    text: str,
    max_facts: int,
) -> str:
    spotlighted = spotlight(text, source=f"{ticker} {period_label} direct issuer PDF")
    return f"""You are enumerating EVERY reported numeric fact for {ticker} ({period_label}) from this direct issuer PDF.

There is NO list of metrics to look for — capture every labeled number in this direct issuer PDF: current reported actuals, explicit prior-period and prior-year comparatives, year-to-date actuals, and numeric management guidance or outlook. Preserve each source period, basis, scope, dimension, label, and full management wording in its exact quoted excerpt. Guidance is forward-looking, never a realized actual.

Return a JSON ARRAY. Each element:
{{
  "label": "<the metric's name as a clean noun phrase>",
  "value": <numeric only, no unit symbols>,
  "unit": EXACTLY ONE of "percent" | "actual" | "count" | "ratio" | "bps",
  "currency": "ISO currency code required for every actual monetary value; omit for counts and rates",
  "source_excerpt": "<verbatim snippet under 200 characters that contains the value>",
  "source_value_text": "<for every numeric row, the exact source token before scale expansion; it must occur in source_excerpt>",
  "reported_period_start": "<ISO date when the source states a duration start; omit when not supported>",
  "reported_period_end": "<ISO date for this value>",
  "period_role": "current | prior_period_comparator | prior_year_comparator | guidance | unknown",
  "range_bound": "lower | upper for each endpoint of an explicit guidance range; omit for a point value",
  "accounting_basis": "gaap | non_gaap | management | unknown",
  "consolidation_scope": "consolidated | geography | segment | product | other | unknown",
  "dimensions": {{"<axis>":"<member>"}},
  "source_unit_scale": "none | thousands | millions | billions | unknown",
  "source_row_label": "<verbatim table row label when present>",
  "source_column_header": "<verbatim table column header when present>"
}}

Rules:
  - Retain actuals, explicit comparatives, and year-to-date values with their own source period. Set period_role to current, prior_period_comparator, or prior_year_comparator as stated. Set guidance/outlook to guidance; set unresolved forecast context to unknown. Never relabel either as current.
  - For each explicit two-endpoint guidance range, return TWO rows with the SAME verbatim source_excerpt and source label: one range_bound lower and one range_bound upper. Keep each exact endpoint token in source_value_text and the numeric value in the standard full-unit convention. Never invent a midpoint, endpoint, or realized value.
  - For continuation inputs, use context only for headers, currency, units and period. Enumerate and quote facts only from the source section.
  - One row per distinct metric, reported period, accounting basis, scope and dimensions. Preserve repeated labels for distinct scopes. Do not invent; omit a fact when you cannot quote it verbatim.
  - Return at most {max_facts} rows. Return ONLY the JSON array with no markdown fence or commentary.

Document text:
{spotlighted}"""


def _validated_enumerated_rows(
    raw: object,
    *,
    saturation_limit: int | None = None,
) -> list[_EnumeratedKpi]:
    rows = TypeAdapter(list[_EnumeratedKpi]).validate_python(raw)
    ranges: dict[tuple[object, ...], dict[str, Decimal]] = {}
    for row in rows:
        if row.source_value_text is None or row.source_value_text not in row.source_excerpt:
            raise ValueError("direct IR PDF numeric token must occur in its exact source quote")
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
    prompt = _enumerate_prompt(ticker, period_label, text, max_facts)
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
    validated = _validated_enumerated_rows(parsed, saturation_limit=max_facts)
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


def _doc_already_captured(conn: sqlite3.Connection, doc_id: int) -> bool:
    """Avoid rebilling documents that already have captured facts.

    This legacy idempotency check does not prove extraction completeness.
    Partial and older capped captures remain unknown until explicitly refreshed.
    """
    row = conn.execute(
        "SELECT 1 FROM kpi_facts WHERE source_doc_id = ? LIMIT 1", (doc_id,)
    ).fetchone()
    return row is not None


# ---------------------------------------------------------------------------
# Capture-every-number candidate extraction for IR supplement PDFs.
#
# The `ir_supplement` doc kind has no brief handler. This path reads each
# registered supplement PDF from its `documents` row and submits a bounded,
# source-backed candidate population for review.
#
# Scope: PDF supplements only (the LLM/PDF path). A spreadsheet (.xlsx) supplement
# is the deterministic ir_pipeline.ingest domain and is left for that path.
# ---------------------------------------------------------------------------

# IR-doc kinds whose every labeled number we enumerate from the PDF itself.
_IR_CAPTURE_PDF_DOC_TYPES: tuple[str, ...] = ("ir_supplement",)


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
    processed: list[tuple[SupplementExchange, list[dict[str, object]]]] = []
    for exchange in exchanges:
        parsed: object = parse_json_payload(exchange.raw_response, expect="array")
        source_rows = _validated_enumerated_rows(
            parsed,
            saturation_limit=max_facts_per_doc,
        )
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
        validated = [item.model_dump(mode="json") for item in _validated_enumerated_rows(original)]
        if validated != rows:
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
            extractor_code_version="v6-native-hyphen-wrap",
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
            extractor_code_version="v6-native-hyphen-wrap",
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
                                saturation_limit=max_facts_per_doc,
                            )
                            ir_supplement_capture.require_native_source_quotes(
                                [row.model_dump(mode="json") for row in rows], source_body
                            )
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
                        saturation_limit=max_facts_per_doc,
                    )
                ]

            prompt_sha256 = hashlib.sha256(
                _enumerate_prompt(
                    ticker,
                    period_label,
                    "",
                    max_facts_per_doc,
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
                    "max_chunks": max_chunks,
                    "max_structured_calls": max_structured_calls,
                    "continuation_version": "ir-supplement-native-continuation-v6-native-hyphen-wrap",
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
