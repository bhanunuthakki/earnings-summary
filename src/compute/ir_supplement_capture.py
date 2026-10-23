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
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from llm.structured import parse_json_payload, repair_structured_prompt
from llm_artifact_store import (
    UpsertRequest,
    artifact_is_reusable,
    compute_input_sha256,
    read_current,
    upsert,
)
from log_redact import redact
from provenance.fulltext_backfill import project_pdf_page_texts
from provenance.ocr_extraction import PDFPreflight, normalize_extracted_text


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


def repair_supplement_prompt(prompt: str) -> str:
    """Controlled repair-only source feedback; the primary prompt stays exact."""
    return repair_structured_prompt(prompt) + (
        "\nSOURCE VALIDATION: The previous result did not satisfy the source contract. "
        "Each source_excerpt must occur verbatim in EXTRACT ONLY FROM THIS SOURCE SECTION "
        "(or the supplied page when no section marker exists). Do not repair spelling, "
        "spacing, punctuation or context. Omit any row you cannot support exactly."
    )


def require_native_source_quotes(rows: list[dict[str, object]], body: str) -> None:
    for row in rows:
        quote = row.get("source_excerpt")
        if not isinstance(quote, str) or not quote or quote not in body:
            raise ValueError("IR extracted quote does not occur in its native source section")


class _ChunkResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["pending", "rejected", "processed", "saturated"]
    rows: list[dict[str, object]]
    exchange: SupplementExchange
    attempts: list[SupplementExchange] = Field(min_length=1, max_length=2)
    validation_error: str | None = None


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
    exchanges: list[SupplementExchange] = field(
        default_factory=list[SupplementExchange], init=False
    )
    processed_models: set[str] = field(default_factory=set[str], init=False)
    structured_calls_reserved: int = field(default=0, init=False)
    chunks_processed: int = field(default=0, init=False)
    _pages: tuple[str, ...] = field(default=(), init=False)

    def capture(self, raw_bytes: bytes) -> list[tuple[int, list[dict[str, object]]]]:
        if (
            min(self.max_input_chars, self.max_facts, self.max_chunks, self.max_structured_calls)
            < 2
        ):
            raise ValueError("IR continuation limits must each be at least 2")
        if hashlib.sha256(raw_bytes).hexdigest() != self.source_sha256:
            raise ValueError("IR continuation source SHA mismatch")
        pages = project_pdf_page_texts(raw_bytes)
        unresolved = [str(index) for index, text in enumerate(pages, 1) if not text.strip()]
        if not pages or unresolved:
            raise ValueError(
                "IR native pages unresolved (empty or image-only): " + ",".join(unresolved)
            )
        self._existing_page_preflight(pages)
        self._pages = pages
        planned: list[tuple[int, int, str]] = []
        for page, text in enumerate(pages, 1):
            if len(text) <= self.max_input_chars:
                planned.append((page, 0, text))
                continue
            # Keep source lines intact. A line too large for a bounded section
            # needs another approved extraction path; never cut a numeric token.
            capacity = self.max_input_chars // 2
            start = 0
            body = ""
            for line in text.splitlines(keepends=True):
                if len(line) > capacity:
                    raise ValueError(f"IR native source line exceeds section budget at page {page}")
                if len(body) + len(line) > capacity:
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
        output: list[tuple[int, list[dict[str, object]]]] = []
        for page, start, body in planned:
            output.extend(self._section(page, start, body))
        return output

    def _existing_page_preflight(self, pages: tuple[str, ...]) -> None:
        """Honor retained OCR dispositions; native text can include only a footer.

        No assessment means unknown visual coverage. Even a retained native-
        sufficient assessment does not prove recall of image/table facts.
        """
        if self.document_version_id is None:
            return
        assessment = self.conn.execute(
            "SELECT assessment_id,input_sha256,page_count,outcome,native_output_sha256,reason_code "
            "FROM ocr_document_assessments WHERE document_version_id=? "
            "ORDER BY assessed_at DESC,assessment_id DESC LIMIT 1",
            (self.document_version_id,),
        ).fetchone()
        if assessment is None:
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
        if preflight.outcome != "native_sufficient":
            raise ValueError(f"IR existing native-page coverage unresolved: {preflight.outcome}")
        if preflight.page_count != len(pages):
            raise ValueError("IR existing native-page assessment page count mismatch")
        for page, text in zip(preflight.pages, pages, strict=True):
            digest = hashlib.sha256(normalize_extracted_text(text).encode()).hexdigest()
            if digest != page.native_text_sha256:
                raise ValueError(
                    f"IR existing native-page assessment text mismatch at page {page.page_number}"
                )
        self.native_assessment_id = str(assessment["assessment_id"])

    def _section(
        self, page: int, start: int, body: str
    ) -> list[tuple[int, list[dict[str, object]]]]:
        if self.chunks_processed >= self.max_chunks:
            raise ValueError(
                f"IR continuation chunk budget exhausted at page {page}, offset {start}"
            )
        self.chunks_processed += 1
        source_page = self._pages[page - 1]
        model_text = body
        if start != 0 or body != source_page:
            context_size = min(1200, self.max_input_chars // 8)
            context = source_page[:context_size] + source_page[max(0, start - context_size) : start]
            model_text = (
                "CONTEXT ONLY (page header and preceding source text):\n"
                + context
                + "\nEXTRACT ONLY FROM THIS SOURCE SECTION:\n"
                + body
            )
            if len(model_text) > self.max_input_chars:
                raise ValueError(f"IR section context exceeds input budget at page {page}")
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
            repair_supplement_prompt(prompt),
        ]
        version = "ir-supplement-native-continuation-v3-semantic-repair"
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
            repair_supplement_prompt(prompt),
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

    def _validated(self, result: _ChunkResult, body: str, prompt: str) -> None:
        self._validated_attempts(result, prompt)
        exchange = result.exchange
        source_rows = self.validate_rows(parse_json_payload(exchange.raw_response, expect="array"))
        require_native_source_quotes(source_rows, body)
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
