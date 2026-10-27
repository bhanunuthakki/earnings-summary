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

from compute import ir_supplement_capture, ir_supplement_revalidation
from compute.ir_supplement_capture import (
    OCRCaptureSelection,
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
from models.facts import Currency, Unit
from models.runs import StageStatus
from models.unit_convert import convert_unit
from pipeline.kpi_semantics import (
    KpiAccountingBasis,
    KpiConsolidationScope,
    KpiPeriodRole,
    KpiSourcePrecision,
    KpiUnitScale,
    normalize_source_numeric,
    parse_source_numeric,
    source_numeric_token_occurs,
)
from pipeline.locators import locate_char_span
from pipeline.run_accounting import (
    JsonValue,
    PipelineRunSuppressedError,
    end_run,
    start_run,
)
from provenance.fact_plane_v2 import ConsolidationScope
from provenance.fulltext_backfill import source_text_projection_identity
from provenance.immutable_artifact import (
    ImmutableArtifactSnapshot,
    assert_artifact_unchanged,
    read_stable_artifact,
)
from provenance.ir_reported_facts import (
    IrCandidateRetentionRequest,
    IrNativeContextRegion,
    IrOCRContextRegion,
    IrOCRRawRow,
    IrOCRRetentionSelection,
    IrRawExchange,
    IrRawRow,
    IrReportedPublicationRequest,
    find_exact_ir_source_review,
    publish_ir_reported_facts,
    retain_ir_pdf_candidates,
)
from provenance.legacy_document_evidence import require_legacy_document_evidence
from provenance.ocr_layout import OCRPageLayout
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
    # Prospective, unreviewed context. These fields do not grant publication.
    period_kind: Literal["instant", "duration", "unknown"] = "unknown"
    guidance_horizon: str | None = Field(default=None, max_length=200)
    source_definition: str | None = Field(default=None, max_length=512)
    context_witnesses: list[ir_supplement_capture.SupplementContextWitness] = Field(
        default_factory=list[ir_supplement_capture.SupplementContextWitness], max_length=8
    )
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


class _DirectEnumeratedKpiV20(_EnumeratedKpi):
    """Prospective direct PDF reporting boundary; legacy raw fields stay intact."""

    reporting_consolidation_scope: ConsolidationScope | Literal["unknown"]

    @model_validator(mode="after")
    def _require_reporting_boundary(self) -> _DirectEnumeratedKpiV20:
        if self.reporting_consolidation_scope == "unknown":
            return self
        if not any(
            "consolidation_scope" in witness.fields
            and ir_supplement_capture.supports_reporting_scope_heading(
                self.reporting_consolidation_scope, witness.quote
            )
            for witness in self.context_witnesses
        ):
            raise ValueError("direct IR reporting scope needs an explicit source boundary witness")
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
    *,
    source_scope: Literal["direct_pdf_rich"] = "direct_pdf_rich",
) -> str:
    spotlighted = spotlight(
        text, source=f"{ticker} {period_label} earnings/IR document (issuer-published)"
    )
    introduction = "There is NO list of metrics to look for — capture every labeled number in this direct issuer PDF: current reported actuals, explicit prior-period and prior-year comparatives, year-to-date actuals, and numeric management guidance or outlook. Preserve each source period, basis, scope, dimension, label, and full management wording in its exact quoted excerpt. Guidance is forward-looking, never a realized actual."
    source_token_field = '  "source_value_text": "<for EVERY numeric row, including monetary values and guidance bounds, the exact source numeric token before scale expansion; it must occur in source_excerpt>",'
    rich_fields = (
        ',\n  "reported_period_start": "<ISO date when the source states a duration start; omit when not supported>",'
        '\n  "period_kind": "instant | duration | unknown",'
        '\n  "guidance_horizon": "<literal forecast horizon, or null when unknown>",'
        '\n  "source_definition": "<preserve source definition, including change versus balance; null when unknown>",'
        '\n  "context_witnesses": [{"page": 1, "quote": "<exact context quote from supplied DOCUMENT CONTEXT>", "fields": ["accounting_basis"]}],'
        '\n  "range_bound": "<lower | upper for each endpoint of an explicit guidance range; omit for a point value>"'
    )
    numeric_field_rule = (
        ir_supplement_capture.SUPPLEMENT_NUMERIC_FIELD_INSTRUCTIONS
        + ir_supplement_capture.SUPPLEMENT_CONTEXT_WITNESS_INSTRUCTIONS
    )
    scope_rule = "  - Retain actuals, explicit comparatives, and year-to-date values with their own source period. Set period_role to current, prior_period_comparator, or prior_year_comparator as stated. Set guidance/outlook to guidance; set unresolved forecast context to unknown. Never relabel either as current."
    range_rule = "\n  - For each explicit two-endpoint guidance range, return TWO rows with the SAME verbatim source_excerpt and source label: one range_bound lower and one range_bound upper. Keep each exact endpoint token in source_value_text and the numeric value in the standard full-unit convention. Never invent a midpoint, endpoint, or realized value."
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


def _enumerate_direct_prompt_v20(ticker: str, period_label: str, text: str, max_facts: int) -> str:
    """Add a reporting boundary to direct PDFs without changing the V16 prompt."""
    historical = _enumerate_prompt(
        ticker, period_label, text, max_facts, source_scope="direct_pdf_rich"
    )
    old = (
        '  "consolidation_scope": "consolidated | geography | segment | product | other | unknown",'
    )
    if historical.count(old) != 1:
        raise ValueError("IR historical direct prompt boundary is unavailable")
    return historical.replace(
        old,
        old
        + '\n  "reporting_consolidation_scope": "consolidated | parent_only | subsidiary | segment | security_specific | other | unknown",',
    ).replace(
        "  - One row per distinct metric, reported period, accounting basis, scope and dimensions.",
        "  - reporting_consolidation_scope is the reporting entity boundary, separate from product and geography dimensions. Use unknown unless DOCUMENT CONTEXT includes a complete positive consolidated financial-statement heading on one native line. Quote that whole heading in context_witnesses with field consolidation_scope. Product, geography, subsidiary, segment, negated, and cropped wording cannot establish a known boundary.\n"
        "  - One row per distinct metric, reported period, accounting basis, scope and dimensions.",
    )


def _enumerate_direct_prompt_ocr(ticker: str, period_label: str, text: str, max_facts: int) -> str:
    """Keep the governed direct schema while marking OCR as candidate evidence."""
    prompt = _enumerate_direct_prompt_v20(ticker, period_label, text, max_facts)
    return prompt + (
        "\nOCR SOURCE RULE: The source section is a governed OCR raster transcription. "
        "Each context page has its own source representation tag. "
        "Quote exact source-section words only. Do not infer financial facts from chart geometry, "
        "bar sizes, or unlabeled cells. OCR words alone cannot establish a consolidated "
        "statement heading. Every output is an unreviewed candidate."
    )


def _validated_enumerated_rows(
    raw: object,
    *,
    saturation_limit: int | None = None,
    prospective_direct: bool = False,
    source_scope: Literal["direct_pdf_rich"] = "direct_pdf_rich",
) -> list[_EnumeratedKpi]:
    rows: list[_EnumeratedKpi] = (
        cast(list[_EnumeratedKpi], TypeAdapter(list[_DirectEnumeratedKpiV20]).validate_python(raw))
        if prospective_direct
        else TypeAdapter(list[_EnumeratedKpi]).validate_python(raw)
    )
    ranges: dict[tuple[object, ...], dict[str, Decimal]] = {}
    for row in rows:
        if row.source_value_text is None or not source_numeric_token_occurs(
            row.source_value_text, row.source_excerpt, row_label=row.source_row_label
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
    source_guardrail: Callable[[object], tuple[bool, str]] | None = None,
    raw_response_observer: Callable[[str, str], None] | None = None,
    prospective_direct: bool = False,
    prompt_builder: Callable[[str, str, str, int], str] | None = None,
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
    prompt = (
        (prompt_builder or _enumerate_direct_prompt_v20)(ticker, period_label, text, max_facts)
        if prospective_direct
        else _enumerate_prompt(ticker, period_label, text, max_facts)
    )
    schema: TypeAdapter[list[_EnumeratedKpi]] = (
        cast(TypeAdapter[list[_EnumeratedKpi]], TypeAdapter(list[_DirectEnumeratedKpiV20]))
        if prospective_direct
        else TypeAdapter(list[_EnumeratedKpi])
    )
    if exchange_observer is not None:
        exchange = call_llm_structured_with_raw(
            prompt,
            purpose="kpi_summary_enumerate",
            expect="array",
            schema=schema,
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
            schema=schema,
        )
    validated = _validated_enumerated_rows(
        parsed,
        saturation_limit=max_facts,
        prospective_direct=prospective_direct,
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
# Historical revalidation is selected by exact document and governed V16 run.
# It can inspect an issuer press-release PDF captured by that prior explicit route.
_IR_REVALIDATION_PDF_DOC_TYPES: tuple[str, ...] = ("ir_supplement", "ir_press_release")


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
    revalidation_source: tuple[
        SupplementCapture,
        ir_supplement_revalidation.RevalidationManifest,
        ir_supplement_revalidation.RevalidationReceipt,
    ]
    | None = None,
    caller_transaction: bool = False,
    require_existing: bool = False,
    ocr_capture: SupplementCapture | None = None,
) -> None:
    """Retain every raw candidate; publish only a pre-existing exact review."""
    revalidated = (
        ir_supplement_revalidation.read_revalidated_capture(
            revalidation_source[0],
            raw_pdf_bytes,
            revalidation_source[1],
            revalidation_source[2],
            _in_progress=caller_transaction,
        )
        if revalidation_source is not None
        else None
    )
    if revalidated is not None and (
        page_rows != revalidated.page_rows or exchanges != revalidated.exchanges
    ):
        raise ValueError("IR revalidated candidate inputs differ from the processing receipt")
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
    if ocr_capture is not None and revalidated is not None:
        raise ValueError("IR OCR candidates cannot use native historical revalidation")
    capture_versions = (
        [
            ir_supplement_revalidation.REVALIDATION_PRODUCER_VERSION.removeprefix(
                "ir-supplement-native-continuation-"
            )
        ]
        * len(exchanges)
        if revalidated is not None
        else [
            ir_supplement_capture.producing_capture_version(
                conn,
                exchange,
                source_sha256,
                expected_ocr_input_sha256=(
                    ocr_capture.exchange_input_sha256[exchange.llm_call_id]
                    if ocr_capture is not None
                    else None
                ),
            )
            for exchange in exchanges
        ]
    )
    if len(set(capture_versions)) > 1:
        raise ValueError("IR governed exchanges mix producing capture versions")
    if not capture_versions:
        raise ValueError("IR producing capture version is unbound")
    capture_version = capture_versions[0]
    if ocr_capture is not None and capture_version != ir_supplement_capture.OCR_CAPTURE_VERSION:
        raise ValueError("IR OCR candidate producer identity differs")
    historical = capture_version in ir_supplement_capture.LEGACY_RELAXED_CAPTURE_VERSIONS

    def retained_rows(raw: object) -> list[_EnumeratedKpi]:
        if historical:
            # Keep old raw wire failures inspectable, not silently corrected or
            # admitted. Only immutable producing artifacts select this path.
            return TypeAdapter(list[_EnumeratedKpi]).validate_python(raw)
        return _validated_enumerated_rows(
            raw,
            saturation_limit=max_facts_per_doc,
            prospective_direct=capture_version
            in {
                "v20-reporting-scope",
                "v21-reviewed-pages-reporting-scope",
                ir_supplement_capture.OCR_CAPTURE_VERSION,
            },
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
    raw_rows: list[IrRawRow | IrOCRRawRow] = []
    for (page, rows), (exchange, original) in zip(page_rows, processed, strict=True):
        validated = [item.model_dump(mode="json") for item in retained_rows(original)]
        normalized_rows = [item.model_dump(mode="json") for item in retained_rows(rows)]
        if validated != normalized_rows:
            raise ValueError("IR page rows differ from their governed raw response")
        for ordinal, original_row in enumerate(original):
            quote = original_row.get("source_excerpt")
            if not isinstance(quote, str) or page < 1 or page > len(native_pages):
                raise ValueError("IR candidate quote or page is missing")
            if ocr_capture is not None and ocr_capture.is_ocr_page(page):
                source_page, source_start, source_body = ocr_capture.exchange_sections[
                    exchange.llm_call_id
                ]
                if source_page != page:
                    raise ValueError("IR OCR producing section page differs from candidate")
                start, end, ordinals = ocr_capture.ocr_quote_words(
                    page, quote, start=source_start, body=source_body
                )
                projection = next(
                    item for item in ocr_capture.ocr_projections() if item.page_number == page
                )
                words_by_ordinal = {
                    word.ordinal: word
                    for word in OCRPageLayout.model_validate_json(projection.layout_json).words
                }
                context_regions: list[IrOCRContextRegion | IrNativeContextRegion] = []
                raw_witnesses = original_row.get("context_witnesses", [])
                if not isinstance(raw_witnesses, list):
                    raise ValueError("IR OCR candidate context witnesses are invalid")
                for raw_witness in cast("list[object]", raw_witnesses):
                    item = ir_supplement_capture.SupplementContextWitness.model_validate(
                        raw_witness
                    )
                    if ocr_capture.is_ocr_page(item.page):
                        witness_start, witness_end, witness_ordinals = ocr_capture.ocr_quote_words(
                            item.page, item.quote
                        )
                        witness_projection = next(
                            value
                            for value in ocr_capture.ocr_projections()
                            if value.page_number == item.page
                        )
                        witness_words = {
                            word.ordinal: word
                            for word in OCRPageLayout.model_validate_json(
                                witness_projection.layout_json
                            ).words
                        }
                        context_regions.append(
                            IrOCRContextRegion(
                                page_number=item.page,
                                exact_quote=item.quote,
                                field_names=tuple(item.fields),
                                char_start=witness_start,
                                char_end=witness_end,
                                words=tuple(witness_words[number] for number in witness_ordinals),
                            )
                        )
                    else:
                        if item.page > len(native_pages):
                            raise ValueError("IR OCR candidate context page is missing")
                        witness_span = locate_char_span(
                            item.quote, native_pages[item.page - 1], whitespace_only=True
                        )
                        if witness_span is None:
                            raise ValueError("IR OCR candidate native context is missing")
                        context_regions.append(
                            IrNativeContextRegion(
                                page_number=item.page,
                                exact_quote=item.quote,
                                field_names=tuple(item.fields),
                                char_start=witness_span[0],
                                char_end=witness_span[1],
                            )
                        )
                raw_rows.append(
                    IrOCRRawRow(
                        page_number=page,
                        response_sha256=exchange.response_sha256,
                        exchange_sha256=retained_exchanges[exchange.llm_call_id].exchange_sha256,
                        response_row_ordinal=ordinal,
                        raw_row=original_row,
                        ocr_source_quote=quote,
                        ocr_char_start=start,
                        ocr_char_end=end,
                        words=tuple(words_by_ordinal[number] for number in ordinals),
                        ocr_page_manifest=ocr_capture.ocr_page_manifest(page),
                        context_regions=tuple(context_regions),
                    )
                )
                continue
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
    selected_ocr = (
        IrOCRRetentionSelection.model_validate(
            ocr_capture.ocr_selection.model_dump(exclude={"document_id"})
        )
        if ocr_capture is not None and ocr_capture.ocr_selection is not None
        else None
    )
    population = retain_ir_pdf_candidates(
        conn,
        IrCandidateRetentionRequest(
            document_version_id=document_version_id,
            source_sha256=source_sha256,
            extractor_name=(
                "ir-supplement-ocr-continuation"
                if ocr_capture is not None
                else "ir-supplement-native-continuation"
            ),
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
        caller_transaction=caller_transaction,
        require_existing=require_existing,
        historical_revalidation=revalidated is not None,
        ocr_repo_root=ocr_capture.repo_root if ocr_capture is not None else None,
        ocr_selection=selected_ocr,
    )
    if revalidated is not None:
        logger.info(
            {
                "event": "ir_source_candidate_disposition",
                "document_version_id": document_version_id,
                "candidate_count": len(population.candidate_node_ids),
                "source_publication_count": 0,
                "disposition": "revalidated_source_candidates_only",
                "processing_artifact_id": revalidated.receipt.artifact_id,
            }
        )
        return
    reviewed = find_exact_ir_source_review(
        conn,
        population,
        raw_pdf_bytes=raw_pdf_bytes,
        ocr_repo_root=ocr_capture.repo_root if ocr_capture is not None else None,
        require_ocr=ocr_capture is not None,
    )
    if reviewed is None:
        logger.info(
            {
                "event": "ir_source_candidate_disposition",
                "document_version_id": document_version_id,
                "candidate_count": len(population.candidate_node_ids),
                "source_publication_count": 0,
                "disposition": (
                    "pending_source_context_review_ocr"
                    if ocr_capture is not None
                    else "pending_source_context_review"
                ),
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
            extractor_name=(
                "ir-supplement-ocr-continuation"
                if ocr_capture is not None
                else "ir-supplement-native-continuation"
            ),
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
        ocr_repo_root=ocr_capture.repo_root if ocr_capture is not None else None,
        ocr_selection=selected_ocr,
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
    ocr_selection: OCRCaptureSelection | None = None,
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

    capture_docs = _list_ir_capture_pdf_docs(conn, repo_root, ticker, doc_types)
    if ocr_selection is not None:
        capture_docs = [item for item in capture_docs if item[0] == ocr_selection.document_id]
        if len(capture_docs) != 1:
            log.error = "IR OCR selected document is not capture eligible for this ticker"
            return _close_log(log, t0)
    for doc_id, pdf_path, period_end in capture_docs:
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
            if ocr_selection is not None and (
                ocr_selection.document_version_id != evidence.document_version_id
                or ocr_selection.source_sha256 != snapshot.file_sha256
            ):
                raise ValueError("IR OCR selection differs from exact registered source")
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
                quote_guardrail: Callable[[list[dict[str, object]], str], None] | None = None,
                prompt_builder: Callable[[str, str, str, int], str] | None = None,
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
                                prospective_direct=True,
                            )
                            (quote_guardrail or ir_supplement_capture.require_native_source_quotes)(
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
                            source_guardrail=source_guardrail,
                            raw_response_observer=observe_raw,
                            prospective_direct=True,
                            prompt_builder=prompt_builder,
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
                        prospective_direct=True,
                    )
                ]

            prompt_sha256 = hashlib.sha256(
                _enumerate_direct_prompt_v20(
                    ticker,
                    period_label,
                    "",
                    max_facts_per_doc,
                ).encode()
            ).hexdigest()
            ocr_prompt_sha256 = (
                hashlib.sha256(
                    _enumerate_direct_prompt_ocr(
                        ticker, period_label, "", max_facts_per_doc
                    ).encode()
                ).hexdigest()
                if ocr_selection is not None
                else None
            )
            schema_sha256 = hashlib.sha256(
                json.dumps(
                    TypeAdapter(list[_DirectEnumeratedKpiV20]).json_schema(), sort_keys=True
                ).encode()
            ).hexdigest()
            projection_identity = source_text_projection_identity(str(pdf_path), "application/pdf")
            ocr_manifest = (
                ocr_selection.model_dump(mode="json") if ocr_selection is not None else None
            )
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
                    "continuation_version": (
                        ir_supplement_capture.UNREVIEWED_CAPTURE_VERSION.removeprefix(
                            "ir-supplement-native-continuation-"
                        )
                        + "-or-"
                        + ir_supplement_capture.CURRENT_CAPTURE_VERSION.removeprefix(
                            "ir-supplement-native-continuation-"
                        )
                    ),
                    "prompt_sha256": prompt_sha256,
                    **(
                        {"ocr_prompt_sha256": ocr_prompt_sha256}
                        if ocr_prompt_sha256 is not None
                        else {}
                    ),
                    "schema_sha256": schema_sha256,
                    "projection_identity": projection_identity,
                    **({"ocr_selection": ocr_manifest} if ocr_manifest is not None else {}),
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
                    _enumerate_direct_prompt_v20,
                    ticker,
                    period_label,
                    max_facts=max_facts_per_doc,
                ),
                ocr_prompt=(
                    partial(
                        _enumerate_direct_prompt_ocr,
                        ticker,
                        period_label,
                        max_facts=max_facts_per_doc,
                    )
                    if ocr_selection is not None
                    else None
                ),
                extract=partial(extract_section, run_id=extraction_run_id),
                effective_prompt=effective_prompt,
                override_identity=override_identity,
                validate_rows=validate_rows,
                refresh=refresh,
                schema_sha256=schema_sha256,
                projection_identity=projection_identity,
                document_version_id=evidence.document_version_id,
                repo_root=repo_root if ocr_selection is not None else None,
                ocr_selection=ocr_selection,
            )
            continuation.extract = partial(
                extract_section,
                run_id=extraction_run_id,
                context_guardrail=continuation.require_context_witnesses,
            )
            if ocr_selection is not None:
                capture_run_id = extraction_run_id

                def extract_ocr_section(
                    page: int,
                    start: int,
                    text: str,
                    source_body: str,
                    retain_raw: Callable[[SupplementExchange], None],
                    *,
                    _continuation: SupplementCapture = continuation,
                    _run_id: str = capture_run_id,
                ) -> SupplementExchange:
                    return extract_section(
                        text,
                        source_body,
                        retain_raw,
                        run_id=_run_id,
                        context_guardrail=_continuation.require_context_witnesses,
                        quote_guardrail=lambda rows, body: _continuation.require_source_quotes(
                            rows, body, page=page, start=start
                        ),
                        prompt_builder=_enumerate_direct_prompt_ocr,
                    )

                continuation.extract_ocr = extract_ocr_section
            source_started_at = datetime.now(UTC)
            page_rows = continuation.capture(raw_bytes)
            assert_artifact_unchanged(snapshot)
            continuation.assert_review_scope_unchanged()
            source_config_sha256 = hashlib.sha256(
                json.dumps(
                    {
                        "model": model,
                        "backend": backend,
                        "prompt_sha256": prompt_sha256,
                        **(
                            {"ocr_prompt_sha256": ocr_prompt_sha256}
                            if ocr_prompt_sha256 is not None
                            else {}
                        ),
                        "schema_sha256": schema_sha256,
                        "projection_identity": projection_identity,
                        "prompt_override_identity": override_identity(),
                        "max_input_chars": max_input_chars,
                        "max_facts_per_doc": max_facts_per_doc,
                        "source_scope": "direct_pdf_rich",
                        **(
                            {"review_scope_sha256": continuation.review_scope_sha256}
                            if continuation.reviewed_non_financial_pages
                            else {}
                        ),
                        **(
                            {
                                "ocr_selection": ocr_manifest,
                                "ocr_review_scope_sha256": continuation.review_scope_sha256,
                            }
                            if ocr_manifest is not None
                            else {}
                        ),
                    },
                    sort_keys=True,
                ).encode()
            ).hexdigest()

            def validate_capture_source(
                _snapshot: ImmutableArtifactSnapshot = snapshot,
                _continuation: ir_supplement_capture.SupplementCapture = continuation,
            ) -> None:
                assert_artifact_unchanged(_snapshot)
                _continuation.assert_review_scope_unchanged()

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
                precommit_validation=validate_capture_source,
                ocr_capture=continuation if ocr_selection is not None else None,
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


def revalidate_ir_pdf_doc(
    manifest: ir_supplement_revalidation.RevalidationManifest,
    repo_root: Path,
    conn: sqlite3.Connection,
) -> ir_supplement_revalidation.RevalidationReceipt:
    """Reprocess exact V16 responses with current strict validation and no model call."""
    if conn.in_transaction:
        raise ValueError("IR revalidation requires an owned transaction boundary")
    matches = [
        (path, period_end)
        for doc_id, path, period_end in _list_ir_capture_pdf_docs(
            conn, repo_root, manifest.ticker, _IR_REVALIDATION_PDF_DOC_TYPES
        )
        if doc_id == manifest.document_id
    ]
    if len(matches) != 1:
        raise ValueError("IR revalidation registered source is unavailable")
    pdf_path, period_end = matches[0]
    snapshot, raw_bytes = read_stable_artifact(pdf_path)
    if snapshot.file_sha256 != manifest.source_sha256:
        raise ValueError("IR revalidation source SHA differs")
    document = conn.execute(
        "SELECT doc_type FROM documents WHERE id=?", (manifest.document_id,)
    ).fetchone()
    if document is None:
        raise ValueError("IR revalidation source document is missing")
    evidence = require_legacy_document_evidence(
        conn,
        document_id=manifest.document_id,
        ticker=manifest.ticker,
        source_type=SourceType.IR_DOC.value,
        doc_type=str(document[0]),
        period_end=period_end,
        source_sha256=snapshot.file_sha256,
    )
    if evidence.document_version_id != manifest.document_version_id:
        raise ValueError("IR revalidation document version differs")
    if manifest.period_end != period_end.isoformat():
        raise ValueError("IR revalidation reported period differs")

    def forbidden_extract(
        _text: str, _body: str, _retain: Callable[[SupplementExchange], None]
    ) -> SupplementExchange:
        raise AssertionError("IR revalidation cannot invoke a provider")

    max_facts_per_doc = manifest.max_facts_per_doc
    max_input_chars = manifest.max_input_chars
    schema_sha256 = hashlib.sha256(
        json.dumps(TypeAdapter(list[_EnumeratedKpi]).json_schema(), sort_keys=True).encode()
    ).hexdigest()
    capture = SupplementCapture(
        conn=conn,
        ticker=manifest.ticker,
        document_id=manifest.document_id,
        source_sha256=snapshot.file_sha256,
        model=manifest.requested_model,
        backend=manifest.requested_backend,
        period_label=f"{period_end.date().isoformat()} [ir_supplement]",
        max_input_chars=max_input_chars,
        max_facts=max_facts_per_doc,
        max_chunks=manifest.max_chunks,
        max_structured_calls=manifest.max_structured_calls,
        prompt=partial(
            _enumerate_prompt,
            manifest.ticker,
            f"{period_end.date().isoformat()} [ir_supplement]",
            max_facts=max_facts_per_doc,
        ),
        extract=forbidden_extract,
        effective_prompt=lambda prompt: prompt,
        override_identity=lambda: hashlib.sha256(b"[]").hexdigest(),
        validate_rows=lambda raw: [
            row.model_dump(mode="json")
            for row in _validated_enumerated_rows(raw, saturation_limit=max_facts_per_doc)
        ],
        schema_sha256=schema_sha256,
        projection_identity=source_text_projection_identity(str(pdf_path), "application/pdf"),
        document_version_id=evidence.document_version_id,
    )
    code_hash = hashlib.sha256()
    for path in (
        Path(ir_supplement_capture.__file__),
        Path(ir_supplement_revalidation.__file__),
        Path(__file__),
        Path(__file__).parents[1] / "pipeline" / "kpi_semantics.py",
    ):
        data = path.read_bytes()
        code_hash.update(len(data).to_bytes(8, "big"))
        code_hash.update(data)
    started_at = datetime.now(UTC)
    retained_in_transaction = False

    def retain_for_receipt(
        receipt: ir_supplement_revalidation.RevalidationReceipt,
        *,
        caller_transaction: bool,
        require_existing: bool,
    ) -> None:
        nonlocal retained_in_transaction
        revalidated = ir_supplement_revalidation.read_revalidated_capture(
            capture, raw_bytes, manifest, receipt, _in_progress=caller_transaction
        )
        config_sha = hashlib.sha256(
            json.dumps(
                {
                    "processing_artifact_id": receipt.artifact_id,
                    "validator_version": receipt.validator_version,
                    "validator_code_sha256": receipt.validator_code_sha256,
                    "producing_capture_version": receipt.producing_capture_version,
                    "source_sha256": receipt.source_sha256,
                    "projection_identity": capture.projection_identity,
                    "schema_sha256": schema_sha256,
                },
                sort_keys=True,
            ).encode()
        ).hexdigest()

        def precommit_validation() -> None:
            assert_artifact_unchanged(snapshot)
            capture.assert_review_scope_unchanged()
            ir_supplement_revalidation.read_revalidated_capture(
                capture, raw_bytes, manifest, receipt, _in_progress=caller_transaction
            )

        retain_and_publish_reviewed_ir_source(
            conn,
            document_version_id=manifest.document_version_id,
            source_sha256=manifest.source_sha256,
            raw_pdf_bytes=raw_bytes,
            page_rows=revalidated.page_rows,
            exchanges=revalidated.exchanges,
            max_facts_per_doc=max_facts_per_doc,
            extractor_config_sha256=config_sha,
            extraction_started_at=started_at,
            precommit_validation=precommit_validation,
            revalidation_source=(capture, manifest, receipt),
            caller_transaction=caller_transaction,
            require_existing=require_existing,
        )
        if caller_transaction:
            retained_in_transaction = True

    receipt = ir_supplement_revalidation.revalidate_historical_sections(
        capture,
        raw_bytes,
        manifest,
        validator_code_sha256=code_hash.hexdigest(),
        before_commit=lambda: assert_artifact_unchanged(snapshot),
        retain_candidates=lambda receipt: retain_for_receipt(
            receipt, caller_transaction=True, require_existing=False
        ),
    )
    if not retained_in_transaction:
        retain_for_receipt(receipt, caller_transaction=False, require_existing=True)
    return receipt


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
