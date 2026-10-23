"""Bounded native-page continuation for direct IR supplement extraction.

The existing LLM artifact store holds derived chunk responses. It does not prove
fact recall or document completeness. Financial writes remain in the caller.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal, cast, get_args

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from llm.structured import parse_json_payload, repair_structured_prompt
from llm_artifact_store import (
    UpsertRequest,
    artifact_is_reusable,
    compute_input_sha256,
    read_artifact,
    read_current,
    upsert,
)
from log_redact import redact
from pipeline.locators import locate_char_span
from provenance.fulltext_backfill import project_pdf_page_texts
from provenance.ocr_extraction import PDFPreflight, normalize_extracted_text
from provenance.ocr_page_review import current_reviewed_pdf_page

SupplementContextField = Literal[
    "accounting_basis",
    "consolidation_scope",
    "dimensions",
    "period_kind",
    "reported_period_start",
    "reported_period_end",
    "guidance_horizon",
    "source_definition",
    "source_unit_scale",
    "currency",
    "period_role",
    "source_row_label",
    "source_column_header",
]


class SupplementContextWitness(BaseModel):
    """Prospective raw context evidence; never a reviewed publication witness."""

    model_config = ConfigDict(extra="forbid", strict=True)

    page: int = Field(gt=0)
    quote: str = Field(min_length=1, max_length=512)
    fields: list[SupplementContextField] = Field(min_length=1)


SUPPLEMENT_CONTEXT_WITNESS_INSTRUCTIONS = (
    "context_witnesses fields must contain only these exact names: "
    + ", ".join(get_args(SupplementContextField))
    + ". Use only exact quotes on the supplied native page. Row/column witnesses "
    "support source coordinates only; they do not grant financial admission."
)


class SupplementSaturatedError(ValueError):
    """A source section needs smaller continuation units."""


class SupplementExchange(BaseModel):
    """The exact successful governed exchange, including repair/fallback identity."""

    model_config = ConfigDict(extra="forbid")

    raw_response: str
    submitted_prompt: str
    model: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    transport: str = Field(min_length=1)
    prompt_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    response_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    llm_call_id: int = Field(gt=0)
    run_id: str = Field(min_length=1)


SUPPLEMENT_NUMERIC_FIELD_INSTRUCTIONS = """Keep these three fields separate:
- value is the numeric full-unit figure. Remove currency/percent symbols only from value. Never divide value by source_unit_scale.
- source_value_text is one complete verbatim numeric token from source_excerpt, before scale expansion. Preserve its sign, parentheses, percent suffix and all internal whitespace. Do not append words such as million or billion to this token. A leading currency symbol may be retained or omitted; never omit a sign or parentheses.
- source_unit_scale records the source presentation scale; it does not change the full-unit value convention.
Synthetic format examples only; these are not issuer facts:
[{"source_excerpt":"Revenue $2,500 million","source_value_text":"2,500","value":2500000000,"unit":"actual","currency":"USD","source_unit_scale":"millions"},{"source_excerpt":"Change (7) %","source_value_text":"(7) %","value":-7,"unit":"percent","source_unit_scale":"none"},{"source_excerpt":"Users 3.25 billion","source_value_text":"3.25","value":3250000000,"unit":"count","source_unit_scale":"billions"}]
End synthetic format examples.
"""


_SUPPLEMENT_REPAIR_FEEDBACK = {
    "": "The response failed JSON, row schema or source validation.",
    "direct IR PDF numeric token must occur in its exact source quote": "direct IR PDF numeric token must occur in its exact source quote",
    "direct IR PDF value conflicts with its source token and scale": "Return the full-unit numeric value consistent with the exact source token and its presentation scale.",
    "IR context witness is not supported by supplied native context": "Use only exact quotes on pages supplied in DOCUMENT CONTEXT; leave unsupported context unknown.",
    "source numeric token must occur in source_excerpt": "source numeric token must occur in source_excerpt",
    "IR extracted quote does not occur in its native source section": "IR extracted quote does not occur in its native source section",
    "direct IR PDF guidance range repeats an endpoint": "direct IR PDF guidance range repeats an endpoint",
    "direct IR PDF guidance range requires both exact endpoints": "direct IR PDF guidance range requires both exact endpoints",
}


def repair_supplement_prompt(prompt: str, error: str = "") -> str:
    """Controlled repair-only feedback; never interpolate unknown error text."""
    feedback = _SUPPLEMENT_REPAIR_FEEDBACK.get(error, _SUPPLEMENT_REPAIR_FEEDBACK[""])
    return repair_structured_prompt(prompt) + (
        "\nSOURCE VALIDATION: " + feedback + ". "
        "Each source_excerpt must occur verbatim in EXTRACT ONLY FROM THIS SOURCE SECTION "
        "(or the supplied page when no section marker exists). Do not repair spelling, "
        "spacing, punctuation or context. "
        + SUPPLEMENT_NUMERIC_FIELD_INSTRUCTIONS
        + SUPPLEMENT_CONTEXT_WITNESS_INSTRUCTIONS
        + "source_value_text must not be a formatted or scale-expanded value. "
        "For a range, do not add a currency or percent symbol absent from that endpoint. "
        "Synthetic examples: $10-20 permits literal tokens 10 and 20, not $20; "
        "3-5% permits literal tokens 3 and 5%, not 3%. "
        "Keep currency, unit and scale in their separate fields and each numeric value in "
        "the required full-unit convention. Keep both endpoints as separate rows with "
        "the same source_excerpt and label. Omit any row you cannot support exactly."
    )


def require_native_source_quotes(rows: list[dict[str, object]], body: str) -> None:
    for row in rows:
        quote = row.get("source_excerpt")
        if (
            not isinstance(quote, str)
            or not quote
            or locate_char_span(quote, body, whitespace_only=True) is None
        ):
            raise ValueError("IR extracted quote does not occur in its native source section")


class _ChunkResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["pending", "rejected", "processed", "saturated"]
    rows: list[dict[str, object]]
    exchange: SupplementExchange
    attempts: list[SupplementExchange] = Field(min_length=1, max_length=2)
    validation_error: str | None = None


LEGACY_RELAXED_CAPTURE_VERSIONS = frozenset(
    {
        "v3-semantic-repair",
        "v4-native-spans",
        "v5-literal-endpoint-repair",
        "v6-native-hyphen-wrap",
    }
)
CURRENT_CAPTURE_VERSION = (
    "ir-supplement-native-continuation-v19-reviewed-pages-percent-cell-boundaries"
)
UNREVIEWED_CAPTURE_VERSION = "ir-supplement-native-continuation-v18-percent-cell-boundaries"


def producing_capture_version(
    conn: sqlite3.Connection, exchange: SupplementExchange, source_sha256: str
) -> str:
    """Resolve exact producing identity independently of wire compatibility.

    This establishes lineage only, not relaxed wire validation, cache reuse or
    financial admission. The artifact owner verifies the complete stored body.
    V7–V12 have no proven producing-artifact support here and fail closed.
    """
    versions = {
        "ir-supplement-native-continuation-v3-semantic-repair",
        "ir-supplement-native-continuation-v4-native-spans",
        "ir-supplement-native-continuation-v5-literal-endpoint-repair",
        "ir-supplement-native-continuation-v6-native-hyphen-wrap",
        "ir-supplement-native-continuation-v13-complete-numeric-prose-end",
        "ir-supplement-native-continuation-v14-page-bounded-sections",
        "ir-supplement-native-continuation-v15-reviewed-pages",
        "ir-supplement-native-continuation-v16-table-cell-boundaries",
        "ir-supplement-native-continuation-v17-reviewed-pages-table-cell-boundaries",
        UNREVIEWED_CAPTURE_VERSION,
        CURRENT_CAPTURE_VERSION,
    }
    db_path = str(conn.execute("PRAGMA database_list").fetchone()[2])
    if not db_path:
        raise ValueError("IR producing capture version is unbound")
    rows = conn.execute(
        "SELECT id FROM llm_artifacts WHERE llm_call_id=? AND purpose='kpi_summary_enumerate'",
        (exchange.llm_call_id,),
    ).fetchall()
    matched: set[str] = set()
    for row in rows:
        artifact = read_artifact(int(row[0]), db_path=db_path)
        if artifact is None or artifact.dirty:
            continue
        result = _ChunkResult.model_validate(artifact.content_json)
        if (
            result.status not in {"processed", "saturated"}
            or result.exchange != exchange
            or artifact.model != exchange.model
        ):
            continue
        if len(artifact.source_doc_ids) != 1:
            continue
        doc_id = artifact.source_doc_ids[0]
        source = conn.execute("SELECT sha256 FROM documents WHERE id=?", (doc_id,)).fetchone()
        if (
            source is None
            or source[0] != source_sha256
            or not artifact.scope.startswith(f"ir_supplement:{doc_id}:page:")
        ):
            continue
        call = conn.execute(
            "SELECT model,provider,transport,prompt_sha256,response_sha256,run_id,purpose,error,outcome "
            "FROM llm_calls WHERE id=?",
            (exchange.llm_call_id,),
        ).fetchone()
        expected = (
            exchange.model,
            exchange.provider,
            exchange.transport,
            exchange.prompt_sha256,
            exchange.response_sha256,
            exchange.run_id,
            "kpi_summary_enumerate",
            None,
            "success",
        )
        if call is None or tuple(call) != expected:
            raise ValueError("IR historical producing governed call metadata differs")
        if hashlib.sha256(exchange.raw_response.encode()).hexdigest() != exchange.response_sha256:
            raise ValueError("IR historical governed response bytes differ")
        if artifact.prompt_version not in versions:
            raise ValueError("IR producing capture version is unknown or unbound")
        capture_version = artifact.prompt_version.removeprefix("ir-supplement-native-continuation-")
        if capture_version in LEGACY_RELAXED_CAPTURE_VERSIONS and result.status != "processed":
            raise ValueError("IR historical producing capture is not processed")
        matched.add(capture_version)
    if len(matched) > 1:
        raise ValueError("IR historical producing capture version is ambiguous")
    if not matched:
        raise ValueError("IR producing capture version is unknown or unbound")
    return next(iter(matched))


@dataclass
class SupplementCapture:
    """Plan native pages, resume exact responses, and fail closed on gaps."""

    conn: sqlite3.Connection
    ticker: str
    document_id: int
    source_sha256: str
    model: str
    backend: str
    period_label: str
    max_input_chars: int
    max_facts: int
    max_chunks: int
    max_structured_calls: int
    prompt: Callable[[str], str]
    extract: Callable[[str, str, Callable[[SupplementExchange], None]], SupplementExchange]
    validate_rows: Callable[[object], list[dict[str, object]]]
    effective_prompt: Callable[[str], str]
    override_identity: Callable[[], str]
    refresh: bool = False
    schema_sha256: str = ""
    projection_identity: str = ""
    document_version_id: str | None = None
    native_assessment_id: str | None = field(default=None, init=False)
    reviewed_non_financial_pages: dict[int, str] = field(default_factory=dict[int, str], init=False)
    review_scope_sha256: str = field(default="", init=False)
    capture_version: str = field(default=UNREVIEWED_CAPTURE_VERSION, init=False)
    exchanges: list[SupplementExchange] = field(
        default_factory=list[SupplementExchange], init=False
    )
    processed_models: set[str] = field(default_factory=set[str], init=False)
    structured_calls_reserved: int = field(default=0, init=False)
    chunks_processed: int = field(default=0, init=False)
    _pages: tuple[str, ...] = field(default=(), init=False)
    _document_context: str = field(default="", init=False)
    _context_pages: set[int] = field(default_factory=set[int], init=False)

    def capture(self, raw_bytes: bytes) -> list[tuple[int, list[dict[str, object]]]]:
        if (
            min(self.max_input_chars, self.max_facts, self.max_chunks, self.max_structured_calls)
            < 2
        ):
            raise ValueError("IR continuation limits must each be at least 2")
        if hashlib.sha256(raw_bytes).hexdigest() != self.source_sha256:
            raise ValueError("IR continuation source SHA mismatch")
        pages = project_pdf_page_texts(raw_bytes)
        if not pages:
            raise ValueError("IR native pages unresolved (empty or image-only): none")
        self._existing_page_preflight(pages)
        self.capture_version = (
            CURRENT_CAPTURE_VERSION
            if self.reviewed_non_financial_pages
            else UNREVIEWED_CAPTURE_VERSION
        )
        unresolved = [
            str(index)
            for index, page_text in enumerate(pages, 1)
            if not page_text.strip() and index not in self.reviewed_non_financial_pages
        ]
        if unresolved:
            raise ValueError(
                "IR native pages unresolved (empty or image-only): " + ",".join(unresolved)
            )
        self._pages = pages
        # This packet supplies context, not a completeness or accounting verdict.
        # Whole pages only: omitted pages remain explicit unknown context.
        context_budget = min(32_000, self.max_input_chars // 2)
        context_pages: list[dict[str, object]] = []
        used = 0
        self._context_pages = set()
        for page, text in enumerate(pages, 1):
            if page in self.reviewed_non_financial_pages:
                continue
            entry: dict[str, object] = {
                "page": page,
                "char_start": 0,
                "char_end": len(text),
                "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
                "text": text,
            }
            size = len(json.dumps(entry, ensure_ascii=False))
            if used + size <= max(0, context_budget - 500):
                context_pages.append(entry)
                self._context_pages.add(page)
                used += size
        context: dict[str, object] = {
            "source_sha256": self.source_sha256,
            "pages": context_pages,
            "omitted_page_count": len(pages) - len(context_pages),
        }
        if self.reviewed_non_financial_pages:
            context["reviewed_non_financial_pages"] = self.reviewed_non_financial_pages
            context["review_scope_sha256"] = self.review_scope_sha256
        context["coverage"] = (
            "native context only; visual/table coverage and financial recall unknown"
        )
        self._document_context = json.dumps(context, ensure_ascii=False)
        planned: list[tuple[int, int, str]] = []
        for page, text in enumerate(pages, 1):
            if page in self.reviewed_non_financial_pages:
                continue
            # Keep bounded native tables together. Dense pages use the existing
            # fact-saturation continuation rather than an arbitrary line cutoff.
            if len(text) <= self.max_input_chars // 4:
                planned.append((page, 0, text))
                continue
            # Keep source lines intact. A line too large for a bounded section
            # needs another approved extraction path; never cut a numeric token.
            capacity = self.max_input_chars // 4
            start = 0
            body = ""
            for line in text.splitlines(keepends=True):
                if len(line) > capacity:
                    raise ValueError(f"IR native source line exceeds section budget at page {page}")
                if body and len(body) + len(line) > capacity:
                    planned.append((page, start, body))
                    start += len(body)
                    body = ""
                body += line
            if body:
                planned.append((page, start, body))
        # The structured owner can make a primary request plus one repair.
        # Reserve both logical model requests before the seam. Provider transport
        # retries/fallbacks remain governed by the existing LLM route.
        if len(planned) > self.max_chunks or len(planned) * 2 > self.max_structured_calls:
            raise ValueError(
                "IR continuation global chunk/structured-call budget exceeded before extraction"
            )
        if self.conn.in_transaction:
            raise ValueError("IR continuation requires an owned transaction boundary")
        # Validate every serialized section before any governed call.
        for page, start, body in planned:
            self._section_text(page, start, body)
        output: list[tuple[int, list[dict[str, object]]]] = []
        for page, start, body in planned:
            output.extend(self._section(page, start, body))
        return output

    def _existing_page_preflight(self, pages: tuple[str, ...]) -> None:
        """Honor retained OCR dispositions; native text can include only a footer.

        No assessment means unknown visual coverage. Even a retained native-
        sufficient assessment does not prove recall of image/table facts.
        """
        self.native_assessment_id = None
        self.reviewed_non_financial_pages = {}
        if self.document_version_id is None:
            self.review_scope_sha256 = hashlib.sha256(b"no-retained-page-assessment").hexdigest()
            return
        assessment = self.conn.execute(
            "SELECT assessment_id,input_sha256,page_count,outcome,native_output_sha256,reason_code "
            "FROM ocr_document_assessments WHERE document_version_id=? "
            "ORDER BY assessed_at DESC,assessment_id DESC LIMIT 1",
            (self.document_version_id,),
        ).fetchone()
        if assessment is None:
            self.review_scope_sha256 = hashlib.sha256(b"no-retained-page-assessment").hexdigest()
            return
        if assessment["input_sha256"] != self.source_sha256:
            raise ValueError("IR existing native-page assessment source SHA mismatch")
        rows = self.conn.execute(
            "SELECT page_number,native_character_count,native_text_sha256,requires_ocr "
            "FROM ocr_preflight_pages WHERE assessment_id=? ORDER BY page_number",
            (assessment["assessment_id"],),
        ).fetchall()
        preflight = PDFPreflight.model_validate(
            {
                "outcome": assessment["outcome"],
                "page_count": assessment["page_count"],
                "native_output_sha256": assessment["native_output_sha256"],
                "reason_code": assessment["reason_code"],
                "pages": [dict(row) for row in rows],
            }
        )
        if preflight.outcome not in {"native_sufficient", "ocr_required"}:
            raise ValueError(f"IR existing native-page coverage unresolved: {preflight.outcome}")
        if preflight.page_count != len(pages):
            raise ValueError("IR existing native-page assessment page count mismatch")
        if tuple(page.page_number for page in preflight.pages) != tuple(range(1, len(pages) + 1)):
            raise ValueError("IR existing native-page assessment page set incomplete")
        reviewed: dict[int, str] = {}
        for page, text in zip(preflight.pages, pages, strict=True):
            digest = hashlib.sha256(normalize_extracted_text(text).encode()).hexdigest()
            if digest != page.native_text_sha256:
                raise ValueError(
                    f"IR existing native-page assessment text mismatch at page {page.page_number}"
                )
            if page.requires_ocr:
                review = current_reviewed_pdf_page(
                    self.conn,
                    document_version_id=self.document_version_id,
                    assessment_id=str(assessment["assessment_id"]),
                    page_number=page.page_number,
                )
                if (
                    review is None
                    or review.disposition != "no_financial_content"
                    or review.source_sha256 != self.source_sha256
                    or review.native_text_sha256 != digest
                ):
                    raise ValueError(
                        f"IR existing native-page coverage unresolved: page {page.page_number}"
                    )
                reviewed[page.page_number] = review.review_id
        self.native_assessment_id = str(assessment["assessment_id"])
        self.reviewed_non_financial_pages = reviewed
        self.review_scope_sha256 = hashlib.sha256(
            json.dumps(
                {
                    "assessment_id": self.native_assessment_id,
                    "document_version_id": self.document_version_id,
                    "page_count": len(pages),
                    "reviewed_non_financial_pages": reviewed,
                    "source_sha256": self.source_sha256,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()

    def assert_review_scope_unchanged(self) -> None:
        """Recheck the exact assessed/reviewed page set before persistence."""
        old = (self.native_assessment_id, self.review_scope_sha256)
        self._existing_page_preflight(self._pages)
        if old != (self.native_assessment_id, self.review_scope_sha256):
            raise ValueError("IR reviewed page scope changed during capture")

    def _section_text(self, page: int, start: int, body: str) -> str:
        """Serialize the exact section and bounded native context for preflight/replay."""
        source_page = self._pages[page - 1]
        context_size = min(1200, self.max_input_chars // 16)
        local_context = (
            source_page[:context_size] + source_page[max(0, start - context_size) : start]
        )
        manifest = json.dumps(
            {
                "page": page,
                "char_start": start,
                "char_end": start + len(body),
                "line_start": source_page[:start].count("\n") + 1,
                "source_sha256": self.source_sha256,
                "section_sha256": hashlib.sha256(body.encode()).hexdigest(),
            },
            sort_keys=True,
        )
        model_text = (
            "CONTEXT ONLY — DOCUMENT CONTEXT (unreviewed native source):\n"
            + self._document_context
            + "\nCONTEXT ONLY (page header and preceding source text):\n"
            + local_context
            + "\nSOURCE SECTION MANIFEST: "
            + manifest
            + "\nEXTRACT ONLY FROM THIS SOURCE SECTION:\n"
            + body
        )
        if len(model_text) > self.max_input_chars:
            raise ValueError(f"IR section context exceeds input budget at page {page}")
        return model_text

    def _section(
        self, page: int, start: int, body: str
    ) -> list[tuple[int, list[dict[str, object]]]]:
        if self.chunks_processed >= self.max_chunks:
            raise ValueError(
                f"IR continuation chunk budget exhausted at page {page}, offset {start}"
            )
        self.chunks_processed += 1
        model_text = self._section_text(page, start, body)
        prompt = self.prompt(model_text)
        override_identity = self.override_identity()
        effective_prompt = self.effective_prompt(prompt)
        inputs: list[bytes | str] = [
            override_identity,
            effective_prompt,
            self.source_sha256,
            self.schema_sha256,
            self.projection_identity,
            self.model,
            self.backend,
            self.period_label,
            str(page),
            str(start),
            str(self.max_facts),
            str(self.max_input_chars),
            prompt,
            *[repair_supplement_prompt(prompt, error) for error in _SUPPLEMENT_REPAIR_FEEDBACK],
        ]
        version = self.capture_version
        scope = f"ir_supplement:{self.document_id}:page:{page}:section:{start}:{len(body)}"
        expected_sha = compute_input_sha256(prompt_version=version, cache_inputs=inputs)
        artifact = read_current(
            ticker=self.ticker, purpose="kpi_summary_enumerate", scope=scope, conn=self.conn
        )
        result: _ChunkResult | None = None
        if not self.refresh and artifact and artifact.input_sha256 == expected_sha:
            cached_result = _ChunkResult.model_validate(artifact.content_json)
            if cached_result.status in {"pending", "rejected"}:
                self._validated_attempts(cached_result, prompt)
                raise ValueError(
                    "IR cached section remains unresolved; explicit refresh is required: "
                    + (cached_result.validation_error or cached_result.status)
                )
            if artifact_is_reusable(artifact, input_sha256=expected_sha):
                result = cached_result
                self._validated(result, body, prompt)
                if (
                    artifact.model != result.exchange.model
                    or artifact.llm_call_id != result.exchange.llm_call_id
                ):
                    raise ValueError("IR cached artifact producing-call attribution mismatch")
        if result is None:
            if self.structured_calls_reserved + 2 > self.max_structured_calls:
                raise ValueError(
                    f"IR continuation structured-call budget exhausted at page {page}, offset {start}"
                )
            self.structured_calls_reserved += 2
            attempts: list[SupplementExchange] = []

            def store(value: _ChunkResult) -> None:
                artifact_id, _ = upsert(
                    UpsertRequest(
                        ticker=self.ticker,
                        purpose="kpi_summary_enumerate",
                        scope=scope,
                        content_json=value.model_dump(mode="json"),
                        model=value.exchange.model,
                        llm_call_id=value.exchange.llm_call_id,
                        prompt_version=version,
                        cache_inputs=inputs,
                        source_doc_ids=[self.document_id],
                        force_new_version=True,
                    ),
                    conn=self.conn,
                )
                if artifact_id is None:
                    self.conn.rollback()
                    raise RuntimeError("IR continuation response cache write failed")
                self.conn.commit()
                saved = read_current(
                    ticker=self.ticker, purpose="kpi_summary_enumerate", scope=scope, conn=self.conn
                )
                if saved is None or not artifact_is_reusable(saved, input_sha256=expected_sha):
                    raise RuntimeError("IR continuation response cache verification failed")

            def retain_raw(exchange: SupplementExchange) -> None:
                if attempts and exchange == attempts[-1]:
                    return
                if len(attempts) >= 2:
                    raise ValueError("IR section exceeds its two governed request slots")
                attempts.append(exchange)
                store(_ChunkResult(status="pending", rows=[], exchange=exchange, attempts=attempts))

            try:
                exchange = self.extract(model_text, body, retain_raw)
                retain_raw(exchange)
                if self.override_identity() != override_identity:
                    raise ValueError("IR prompt override changed during extraction")
                rows = self.validate_rows(parse_json_payload(exchange.raw_response, expect="array"))
                saturated = len(rows) >= self.max_facts
                result = _ChunkResult(
                    status="saturated" if saturated else "processed",
                    rows=[] if saturated else rows,
                    exchange=exchange,
                    attempts=attempts,
                )
                self._validated(result, body, prompt)
            except Exception as error:
                if attempts:
                    store(
                        _ChunkResult(
                            status="rejected",
                            rows=[],
                            exchange=attempts[-1],
                            attempts=attempts,
                            validation_error=redact(error),
                        )
                    )
                raise
            store(result)
        self.exchanges.append(result.exchange)
        if result.status == "processed":
            self.processed_models.add(result.exchange.model)
            return [(page, result.rows)]
        if len(body) < 2:
            raise ValueError(f"IR saturated section cannot split at page {page}, offset {start}")
        midpoint = len(body) // 2
        newline = body.rfind("\n", 0, midpoint + 1)
        if newline <= 0:
            newline = body.find("\n", midpoint)
        if newline <= 0 or newline + 1 >= len(body):
            raise ValueError(f"IR saturated source line unresolved at page {page}, offset {start}")
        cut = newline + 1
        return self._section(page, start, body[:cut]) + self._section(page, start + cut, body[cut:])

    def _validated_attempts(self, result: _ChunkResult, prompt: str) -> None:
        if result.exchange != result.attempts[-1]:
            raise ValueError("IR final governed exchange differs from retained attempts")
        for exchange in result.attempts:
            self._validated_exchange(exchange, prompt)

    def _validated_exchange(self, exchange: SupplementExchange, prompt: str) -> None:
        if exchange.submitted_prompt not in {
            prompt,
            repair_structured_prompt(prompt),
            *[repair_supplement_prompt(prompt, error) for error in _SUPPLEMENT_REPAIR_FEEDBACK],
        }:
            raise ValueError("IR final submitted prompt does not match the source section")
        effective = self.effective_prompt(exchange.submitted_prompt)
        if hashlib.sha256(effective.encode()).hexdigest() != exchange.prompt_sha256:
            raise ValueError("IR final effective prompt identity mismatch")
        if hashlib.sha256(exchange.raw_response.encode()).hexdigest() != exchange.response_sha256:
            raise ValueError("IR governed response hash mismatch")
        call = self.conn.execute(
            "SELECT model,provider,transport,prompt_sha256,response_sha256,run_id,purpose,error,outcome "
            "FROM llm_calls WHERE id=?",
            (exchange.llm_call_id,),
        ).fetchone()
        if call is None or (
            call["model"] != exchange.model
            or call["provider"] != exchange.provider
            or call["transport"] != exchange.transport
            or call["prompt_sha256"] != exchange.prompt_sha256
            or call["response_sha256"] != exchange.response_sha256
            or call["run_id"] != exchange.run_id
            or call["purpose"] != "kpi_summary_enumerate"
            or call["error"] is not None
            or call["outcome"] != "success"
        ):
            raise ValueError("IR producing governed call metadata is missing or mismatched")

    def require_context_witnesses(self, rows: list[dict[str, object]]) -> None:
        """Check proposed evidence against supplied native pages, not admission."""
        for row in rows:
            witnesses = row.get("context_witnesses", [])
            if not isinstance(witnesses, list):
                raise ValueError("IR context witness is not supported by supplied native context")
            witness_list = cast(list[object], witnesses)
            if len(witness_list) > 8:
                raise ValueError("IR context witness is not supported by supplied native context")
            for witness in witness_list:
                try:
                    item = SupplementContextWitness.model_validate(witness)
                except ValidationError:
                    raise ValueError(
                        "IR context witness is not supported by supplied native context"
                    ) from None
                if (
                    item.page not in self._context_pages
                    or locate_char_span(
                        item.quote, self._pages[item.page - 1], whitespace_only=True
                    )
                    is None
                ):
                    raise ValueError(
                        "IR context witness is not supported by supplied native context"
                    )

    def _validated(self, result: _ChunkResult, body: str, prompt: str) -> None:
        self._validated_attempts(result, prompt)
        exchange = result.exchange
        source_rows = self.validate_rows(parse_json_payload(exchange.raw_response, expect="array"))
        require_native_source_quotes(source_rows, body)
        self.require_context_witnesses(source_rows)
        if result.status == "saturated":
            if result.rows or len(source_rows) < self.max_facts:
                raise ValueError("IR saturated cache does not match its governed response")
            return
        result.rows = self.validate_rows(result.rows)
        if len(result.rows) >= self.max_facts or json.dumps(
            source_rows, sort_keys=True
        ) != json.dumps(result.rows, sort_keys=True):
            raise ValueError("IR cached rows do not match the final governed response")
        require_native_source_quotes(result.rows, body)
