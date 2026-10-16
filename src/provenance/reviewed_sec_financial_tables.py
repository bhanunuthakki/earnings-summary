"""Publish a closed, manually reviewed SEC financial population.

The review retains exact raw tokens, source units, definitions and context nodes.
This closes only the supplied required fact population. It neither classifies an
issuer archive nor admits canonical financial metrics. Those authorities remain
in the document-coverage and exact-source ontology workflows.
"""

from __future__ import annotations

import hashlib
import html
import json
import re
import sqlite3
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Literal, Self
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator

from pipeline.kpi_semantics import parse_source_numeric
from provenance.evidence_ledger import EvidenceLedger, EvidenceLocator, EvidenceNode, ExtractionRun
from provenance.evidence_native_candidates import (
    resolve_local_storage_uri,
    select_evidence_native_candidates_by_id,
)
from provenance.fact_plane_v2 import (
    AccountingBasis,
    CanonicalJSONObject,
    ExtractionRunCompletenessSealV2,
    FactCellV2,
    FiscalPeriod,
    ReportedFactObservationV2,
)
from provenance.fulltext_backfill import verify_native_html_replay
from provenance.fulltext_extractor_identity import STRUCTURED_WEB_ARCHIVE_FULLTEXT_EXTRACTOR
from provenance.issuer_registry import IssuerRegistry
from provenance.reporting_entity_registry import ReportingEntityRegistry
from provenance.source_fact_repository import (
    ReportedSourceFact,
    SourceFactPublication,
    SourceFactRepository,
)

_RECIPE = "reviewed-sec-financial-tables.v2"
_NAME = "reviewed-sec-financial-tables"
SecSourceKind = Literal["sec_20f", "sec_6k"]


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _time(value: object) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


class _Closed(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ReviewedSecNode(_Closed):
    node_id: str
    kind: str
    text: str
    locator: EvidenceLocator
    recorded_at: datetime


class ReviewedSecHtmlEvidence(_Closed):
    document_version_id: str
    fulltext_run_id: str
    issuer_id: str
    ticker: str
    source_url: str
    source_kind: SecSourceKind
    blob_sha256: str
    nodes: tuple[ReviewedSecNode, ...]

    def require_nodes(self, ids: tuple[str, ...]) -> tuple[ReviewedSecNode, ...]:
        if not ids or len(ids) != len(set(ids)):
            raise ValueError("source review requires unique exact node identities")
        by_id = {node.node_id: node for node in self.nodes}
        selected = tuple(by_id.get(identity) for identity in ids)
        if any(
            node is None
            or node.kind not in {"table_cell", "passage", "table_row", "section", "table"}
            for node in selected
        ):
            raise ValueError("review node does not belong to the exact substantive SEC source")
        return tuple(node for node in selected if node is not None)


def assert_sec_source_identity(
    *,
    source_kind: SecSourceKind,
    form_type: str,
    accession_number: str,
    sec_cik: str,
    source_url: str,
) -> None:
    """Verify the native SEC form, CIK and accession without changing its kind."""
    if form_type != {"sec_20f": "20-F", "sec_6k": "6-K"}[source_kind]:
        raise ValueError("reviewed SEC source kind does not match its native form")
    if len(sec_cik) != 10 or not sec_cik.isdecimal():
        raise ValueError("reviewed SEC source requires an exact normalized CIK")
    url = urlsplit(source_url)
    prefix = f"/Archives/edgar/data/{int(sec_cik)}/{accession_number.replace('-', '')}/"
    if (
        url.scheme != "https"
        or url.hostname != "www.sec.gov"
        or url.query
        or url.fragment
        or not url.path.startswith(prefix)
        or len(url.path) <= len(prefix)
    ):
        raise ValueError("reviewed SEC source URL does not match its CIK and accession")


def assert_reviewed_calendar_period_context(
    *, text: str, period_end: datetime, period_start: datetime | None = None
) -> None:
    """Require a visible date and, for flows, the calendar duration header.

    Node selection and column interpretation remain explicit source review.
    This guard rejects a review whose dates or duration lack a source witness.
    """
    normalized = " ".join(text.split()).lower()
    named = re.search(
        rf"{period_end.strftime('%B').lower()}\s+0?{period_end.day}(?!\d)", normalized
    )
    numeric = re.search(
        rf"0?{period_end.day}[./-]0?{period_end.month}[./-]{period_end.year}", normalized
    )
    numeric_us = re.search(
        rf"0?{period_end.month}[./-]0?{period_end.day}[./-]{period_end.year}", normalized
    )
    iso = period_end.strftime("%Y-%m-%d") in normalized
    if not (str(period_end.year) in normalized and (named or numeric or numeric_us or iso)):
        raise ValueError("reviewed SEC period end has no exact source context witness")
    if period_start is None:
        if re.search(r"(?:months?|year)\s+(?:period\s+)?ended", normalized):
            raise ValueError("instant source review uses a duration header")
        return
    months = (period_end.year - period_start.year) * 12 + period_end.month - period_start.month + 1
    if (
        period_start.day != 1
        or period_start.year != period_end.year
        or period_end.day not in {30, 31}
    ):
        raise ValueError("unsupported reviewed SEC calendar duration")
    markers = {
        3: r"(?:three|3)[ -]months?",
        6: r"(?:six|6)[ -]months?",
        9: r"(?:nine|9)[ -]months?",
        12: r"(?:year|twelve[ -]months?|12[ -]months?)",
    }
    if (
        months not in markers
        or re.search(markers[months], normalized) is None
        or "ended" not in normalized
    ):
        raise ValueError("reviewed SEC duration has no exact source context witness")


def load_reviewed_sec_html_evidence(
    conn: sqlite3.Connection,
    *,
    document_version_id: str,
    fulltext_run_id: str,
    source_kind: SecSourceKind,
    accession_number: str,
    sec_cik: str,
    source_doc_sha256: str,
    content_roots: tuple[Path, ...],
    knowledge_cutoff: datetime,
) -> ReviewedSecHtmlEvidence:
    """Read exact local bytes and replay the complete qualified HTML hierarchy."""
    row = conn.execute(
        "SELECT d.issuer_id,d.ticker,d.form_type,d.accession_number,d.blob_sha256,d.recorded_at,o.source_url,o.retrieved_at,o.blob_sha256 "
        "FROM evidence_document_versions d JOIN evidence_source_observations o ON o.observation_id=d.observation_id WHERE d.document_version_id=?",
        (document_version_id,),
    ).fetchone()
    if (
        row is None
        or str(row[3]) != accession_number
        or str(row[4]) != source_doc_sha256
        or str(row[8]) != source_doc_sha256
    ):
        raise ValueError("reviewed SEC document identity does not match immutable capture")
    assert_sec_source_identity(
        source_kind=source_kind,
        form_type=str(row[2]),
        accession_number=accession_number,
        sec_cik=sec_cik,
        source_url=str(row[6]),
    )
    resolved = IssuerRegistry(conn).resolve_identifier(
        "sec_cik", sec_cik, knowledge_at=knowledge_cutoff
    )
    if resolved.issuer_id != str(row[0]) or resolved.material_dissent:
        raise ValueError("reviewed SEC CIK does not identify the captured issuer")
    run = conn.execute(
        "SELECT document_version_id,input_sha256,extractor_name,extractor_code_version,extractor_config_sha256,outcome,completed_at FROM evidence_extraction_runs WHERE extraction_run_id=?",
        (fulltext_run_id,),
    ).fetchone()
    identity = STRUCTURED_WEB_ARCHIVE_FULLTEXT_EXTRACTOR
    if (
        run is None
        or tuple(run[:6])
        != (
            document_version_id,
            source_doc_sha256,
            identity.name,
            identity.code_version,
            identity.config_sha256,
            "succeeded",
        )
        or run[6] is None
    ):
        raise ValueError("reviewed SEC source requires the exact qualified HTML extraction")
    if max(_time(row[5]), _time(row[7]), _time(run[6])) > knowledge_cutoff:
        raise ValueError("reviewed SEC capture is newer than the review cutoff")
    candidate = select_evidence_native_candidates_by_id(
        conn, document_version_ids=(document_version_id,)
    )[0]
    path = resolve_local_storage_uri(candidate.storage_uri, allowed_roots=content_roots)
    if path is None:
        raise ValueError("reviewed SEC source is outside approved content roots")
    raw = path.read_bytes()
    if len(raw) != candidate.byte_size or hashlib.sha256(raw).hexdigest() != source_doc_sha256:
        raise ValueError("reviewed SEC raw byte commitment does not match")
    source = raw.decode("utf-8", errors="strict")
    nodes: list[ReviewedSecNode] = []
    rows = conn.execute(
        "SELECT node_id,node_kind,text,locator_json,locator_sha256,recorded_at FROM evidence_nodes WHERE extraction_run_id=? ORDER BY node_id",
        (fulltext_run_id,),
    ).fetchall()
    for node_id, kind, text, locator_json, locator_sha256, recorded_at in rows:
        if locator_json is None:
            raise ValueError("reviewed SEC native node requires an exact locator")
        locator = EvidenceLocator.model_validate_json(str(locator_json))
        if (
            locator.canonical_json != str(locator_json)
            or locator.canonical_sha256 != str(locator_sha256)
            or locator.source_ref != str(row[6])
        ):
            raise ValueError("reviewed SEC node locator commitment does not match")
        if kind in {"passage", "table_cell"} and (
            locator.char_start is None
            or locator.char_end is None
            or source[locator.char_start : locator.char_end] != str(text)
        ):
            raise ValueError("reviewed SEC source node does not replay from raw bytes")
        stamp = _time(recorded_at)
        if stamp > knowledge_cutoff:
            raise ValueError("reviewed SEC node is newer than the review cutoff")
        nodes.append(
            ReviewedSecNode(
                node_id=str(node_id),
                kind=str(kind),
                text=str(text),
                locator=locator,
                recorded_at=stamp,
            )
        )
    verify_native_html_replay(
        raw, str(row[6]), tuple((node.kind, node.text, node.locator) for node in nodes)
    )
    return ReviewedSecHtmlEvidence(
        document_version_id=document_version_id,
        fulltext_run_id=fulltext_run_id,
        issuer_id=str(row[0]),
        ticker=str(row[1]),
        source_url=str(row[6]),
        source_kind=source_kind,
        blob_sha256=source_doc_sha256,
        nodes=tuple(nodes),
    )


class ReviewedSecNumericFragment(_Closed):
    node_id: str = Field(min_length=1, max_length=128)
    char_start: int = Field(ge=0)
    char_end: int = Field(gt=0)
    raw_text: str = Field(min_length=1, max_length=128)

    @model_validator(mode="after")
    def _span(self) -> Self:
        if self.char_end <= self.char_start:
            raise ValueError("reviewed SEC numeric fragment requires a nonempty span")
        return self


class ReviewedSecFinancialFact(_Closed):
    fact_key: str = Field(min_length=1, max_length=128)
    concept_name: str = Field(min_length=1, max_length=256)
    document_version_id: str = Field(min_length=1, max_length=128)
    fulltext_run_id: str = Field(min_length=1, max_length=128)
    source_kind: SecSourceKind
    source_doc_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    accession_number: str = Field(pattern=r"^\d{10}-\d{2}-\d{6}$")
    sec_cik: str = Field(pattern=r"^\d{10}$")
    value_node_id: str = Field(min_length=1, max_length=128)
    value_locator_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    raw_lexical_value: str = Field(min_length=1, max_length=128)
    value_capture_kind: Literal["table_cell", "table_cell_fragments", "passage_numeric_span"] = (
        "table_cell"
    )
    value_fragments: tuple[ReviewedSecNumericFragment, ...] = Field(default=(), max_length=10)
    unit_key: str = Field(min_length=1, max_length=64)
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    period_start: datetime | None = None
    period_end: datetime
    fiscal_period: FiscalPeriod
    accounting_basis: AccountingBasis
    definition_text: str = Field(min_length=1)
    definition_node_ids: tuple[str, ...] = Field(min_length=1, max_length=100)
    context_node_ids: tuple[str, ...] = Field(min_length=1, max_length=100)

    @field_validator("period_start", "period_end")
    @classmethod
    def _aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("reviewed periods require timezone-aware clocks")
        return None if value is None else value.astimezone(UTC)

    @model_validator(mode="after")
    def _coordinates(self) -> Self:
        if self.period_start is not None and self.period_start > self.period_end:
            raise ValueError("reviewed period start follows its end")
        if not parse_source_numeric(html.unescape(self.raw_lexical_value)).is_finite():
            raise ValueError("reviewed value requires a finite source token")
        if self.value_capture_kind == "table_cell" and self.value_fragments:
            raise ValueError("whole table-cell captures cannot supply numeric fragments")
        if self.value_capture_kind != "table_cell" and not self.value_fragments:
            raise ValueError("reviewed SEC numeric span capture requires exact fragments")
        if self.value_capture_kind == "passage_numeric_span" and (
            len(self.value_fragments) != 1 or self.value_fragments[0].node_id != self.value_node_id
        ):
            raise ValueError("narrative numeric capture requires one exact passage token")
        if (
            self.value_fragments
            and "".join(fragment.raw_text for fragment in self.value_fragments)
            != self.raw_lexical_value
        ):
            raise ValueError("reviewed SEC numeric fragments do not reconstruct its raw value")
        self.normalization()
        return self

    def normalization(self) -> tuple[str, Decimal]:
        if self.currency is not None:
            allowed = {
                self.currency: Decimal(1),
                f"{self.currency}_thousands": Decimal(1000),
                f"{self.currency}_millions": Decimal(1000000),
                f"{self.currency}_billions": Decimal(1000000000),
            }
            if self.unit_key not in allowed:
                raise ValueError("reviewed monetary unit must match its explicit currency")
            return self.currency, allowed[self.unit_key]
        allowed = {
            "shares": ("shares", Decimal(1)),
            "shares_millions": ("shares", Decimal(1000000)),
            "pure": ("pure", Decimal(1)),
            "percent": ("pure", Decimal("0.01")),
        }
        if self.unit_key not in allowed:
            raise ValueError("unsupported reviewed SEC nonmonetary source unit")
        return allowed[self.unit_key]


class ReviewedSecFinancialRejection(_Closed):
    fact_key: str = Field(min_length=1, max_length=128)
    reason_code: str = Field(min_length=1)


class ReviewedSecFinancialRequest(_Closed):
    schema_version: Literal["reviewed_sec_financial_tables.v1"] = "reviewed_sec_financial_tables.v1"
    ticker: str = Field(min_length=1, max_length=16)
    reviewed_by: str = Field(min_length=1)
    reviewed_at: datetime
    review_evidence: str = Field(min_length=1)
    expected_fact_keys: tuple[str, ...] = Field(min_length=1, max_length=100)
    facts: tuple[ReviewedSecFinancialFact, ...] = Field(min_length=1, max_length=100)
    rejections: tuple[ReviewedSecFinancialRejection, ...] = ()
    review_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    content_roots: tuple[Path, ...] = Field(min_length=1)
    recorded_at: datetime
    apply: bool = False
    expected_plan_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @field_validator("reviewed_at", "recorded_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("review clocks require a timezone")
        return value.astimezone(UTC)

    @property
    def canonical_review_json(self) -> str:
        payload = self.model_dump(
            mode="json",
            exclude={
                "review_sha256",
                "content_roots",
                "recorded_at",
                "apply",
                "expected_plan_sha256",
            },
        )
        return _json(payload)

    @model_validator(mode="after")
    def _sealed(self) -> Self:
        expected = self.expected_fact_keys
        keys = tuple(fact.fact_key for fact in self.facts) + tuple(
            item.fact_key for item in self.rejections
        )
        if (
            len(expected) != len(set(expected))
            or len(keys) != len(set(keys))
            or set(keys) != set(expected)
        ):
            raise ValueError(
                "reviewed facts and rejections must exactly close the expected population"
            )
        if self.review_sha256 != _sha(self.canonical_review_json):
            raise ValueError("reviewed SEC review SHA-256 does not match its sealed payload")
        if self.reviewed_at > self.recorded_at or any(
            fact.period_end > self.reviewed_at for fact in self.facts
        ):
            raise ValueError("reviewed SEC review clocks are inconsistent")
        if self.apply and self.expected_plan_sha256 is None:
            raise ValueError("reviewed SEC apply requires the exact dry-run plan SHA-256")
        return self


class ReviewedSecFinancialResult(_Closed):
    mode: Literal["dry_run", "apply"]
    plan_sha256: str
    review_sha256: str
    captured_count: int
    rejected_count: int
    source_population_complete: bool
    archive_coverage: Literal["not_performed"] = "not_performed"
    canonical_admission: Literal["not_performed"] = "not_performed"
    publication_id: str | None = None
    exact_replay: bool = False
    observation_ids: tuple[tuple[str, str], ...] = ()


def publish_reviewed_sec_financial_tables(
    conn: sqlite3.Connection, request: ReviewedSecFinancialRequest
) -> ReviewedSecFinancialResult:
    """Validate a sealed source review, then publish its bounded native population."""
    request = ReviewedSecFinancialRequest.model_validate(request.model_dump(mode="json"))
    if conn.in_transaction:
        raise ValueError("reviewed SEC publication requires an idle connection")
    conn.execute("BEGIN IMMEDIATE" if request.apply else "BEGIN")
    try:
        result = _publish(conn, request)
        if request.apply:
            conn.commit()
        else:
            conn.rollback()
        return result
    except Exception:
        conn.rollback()
        raise


def _publish(
    conn: sqlite3.Connection, request: ReviewedSecFinancialRequest
) -> ReviewedSecFinancialResult:
    evidences: dict[str, ReviewedSecHtmlEvidence] = {}
    prepared: list[ReportedSourceFact] = []
    claim_nodes: list[EvidenceNode] = []
    runs: dict[str, ExtractionRun] = {}
    seals: list[ExtractionRunCompletenessSealV2] = []
    observations: list[tuple[str, str]] = []
    for fact in request.facts:
        evidence = evidences.get(fact.document_version_id)
        if evidence is None:
            evidence = load_reviewed_sec_html_evidence(
                conn,
                document_version_id=fact.document_version_id,
                fulltext_run_id=fact.fulltext_run_id,
                source_kind=fact.source_kind,
                accession_number=fact.accession_number,
                sec_cik=fact.sec_cik,
                source_doc_sha256=fact.source_doc_sha256,
                content_roots=request.content_roots,
                knowledge_cutoff=request.reviewed_at,
            )
            evidences[fact.document_version_id] = evidence
        elif (evidence.fulltext_run_id, evidence.source_kind, evidence.blob_sha256) != (
            fact.fulltext_run_id,
            fact.source_kind,
            fact.source_doc_sha256,
        ):
            raise ValueError("reviewed SEC document has conflicting source commitments")
        assert_sec_source_identity(
            source_kind=fact.source_kind,
            form_type={"sec_20f": "20-F", "sec_6k": "6-K"}[evidence.source_kind],
            accession_number=fact.accession_number,
            sec_cik=fact.sec_cik,
            source_url=evidence.source_url,
        )
        if evidence.ticker.upper() != request.ticker.upper():
            raise ValueError("reviewed SEC ticker conflicts with the captured source")
        value_node = evidence.require_nodes((fact.value_node_id,))[0]
        if value_node.locator.canonical_sha256 != fact.value_locator_sha256:
            raise ValueError("reviewed SEC value locator does not match")
        fragments = fact.value_fragments
        cell_nodes = (
            tuple(
                sorted(
                    (
                        node
                        for node in evidence.nodes
                        if node.kind == "table_cell"
                        and html.unescape(node.text).strip()
                        and (
                            node.locator.table_name,
                            node.locator.table_row_index,
                            node.locator.table_column_index,
                        )
                        == (
                            value_node.locator.table_name,
                            value_node.locator.table_row_index,
                            value_node.locator.table_column_index,
                        )
                    ),
                    key=lambda node: (
                        node.locator.char_start or 0,
                        node.locator.char_end or 0,
                        node.node_id,
                    ),
                )
            )
            if value_node.kind == "table_cell"
            else ()
        )
        if fact.value_capture_kind == "table_cell" and (
            len(cell_nodes) != 1 or cell_nodes[0].node_id != value_node.node_id
        ):
            raise ValueError(
                "split SEC table cells require their complete exact numeric fragment sequence"
            )
        if fact.value_capture_kind == "table_cell_fragments" and tuple(
            fragment.node_id for fragment in fragments
        ) != tuple(node.node_id for node in cell_nodes):
            raise ValueError("reviewed SEC numeric fragments omit part of the exact source cell")
        if fact.value_capture_kind == "table_cell":
            if (
                value_node.kind != "table_cell"
                or value_node.text.strip() != fact.raw_lexical_value.strip()
            ):
                raise ValueError("reviewed SEC value must match its exact numeric table cell")
        else:
            expected_kind = (
                "passage" if fact.value_capture_kind == "passage_numeric_span" else "table_cell"
            )
            if value_node.kind != expected_kind:
                raise ValueError("reviewed SEC numeric span source kind does not match")
            previous_end = -1
            for fragment in fragments:
                node = evidence.require_nodes((fragment.node_id,))[0]
                locator = node.locator
                if (
                    node.kind != expected_kind
                    or locator.char_start is None
                    or locator.char_end is None
                    or not (
                        locator.char_start
                        <= fragment.char_start
                        < fragment.char_end
                        <= locator.char_end
                    )
                    or fragment.char_start < previous_end
                    or node.text[
                        fragment.char_start - locator.char_start : fragment.char_end
                        - locator.char_start
                    ]
                    != fragment.raw_text
                ):
                    raise ValueError(
                        "reviewed SEC numeric fragment does not replay from its exact source span"
                    )
                if expected_kind == "table_cell" and (
                    locator.table_name,
                    locator.table_row_index,
                    locator.table_column_index,
                ) != (
                    value_node.locator.table_name,
                    value_node.locator.table_row_index,
                    value_node.locator.table_column_index,
                ):
                    raise ValueError("reviewed SEC numeric fragments cross table cells")
                if expected_kind == "table_cell" and fragment.raw_text != node.text.strip():
                    raise ValueError(
                        "reviewed SEC numeric fragments must retain each complete source token"
                    )
                if expected_kind == "passage" and (
                    (
                        fragment.char_start > locator.char_start
                        and node.text[fragment.char_start - locator.char_start - 1].isdigit()
                    )
                    or (
                        fragment.char_end < locator.char_end
                        and node.text[fragment.char_end - locator.char_start].isdigit()
                    )
                ):
                    raise ValueError("reviewed SEC narrative token truncates a source number")
                previous_end = fragment.char_end
        definitions = evidence.require_nodes(fact.definition_node_ids)
        contexts = evidence.require_nodes(fact.context_node_ids)
        if fact.value_capture_kind != "passage_numeric_span":
            if value_node.locator.table_name is None or not any(
                node.locator.table_name == value_node.locator.table_name
                and node.locator.table_row_index == value_node.locator.table_row_index
                for node in definitions
            ):
                raise ValueError("reviewed SEC definition lacks the exact value row witness")
            if not any(
                node.locator.table_name == value_node.locator.table_name for node in contexts
            ):
                raise ValueError("reviewed SEC context lacks the exact value table witness")
        elif not any(node.node_id == value_node.node_id for node in definitions):
            raise ValueError("reviewed narrative definition requires the exact value passage")
        context_text = "\n".join(node.text for node in contexts)
        assert_reviewed_calendar_period_context(
            text=context_text, period_end=fact.period_end, period_start=fact.period_start
        )
        unit_text = context_text.lower()
        scale = fact.unit_key.rsplit("_", 1)[-1] if "_" in fact.unit_key else None
        if (
            (fact.currency is not None and fact.currency.lower() not in unit_text)
            or (scale is not None and scale.rstrip("s") not in unit_text)
            or (
                fact.currency is None
                and fact.unit_key.startswith("shares")
                and "shares" not in unit_text
            )
        ):
            raise ValueError("reviewed SEC unit has no exact source context witness")
        if fact.definition_text != "\n".join(node.text for node in definitions):
            raise ValueError("reviewed SEC definition wording must match its exact source nodes")
        subject = ReportingEntityRegistry(conn).canonicalize_recorded_subject(
            evidence.issuer_id, knowledge_at=request.reviewed_at
        )
        if subject.reporting_entity_id is None or subject.material_dissent:
            raise ValueError("reviewed SEC source requires one undisputed reporting entity")
        unit, multiplier = fact.normalization()
        raw_value = parse_source_numeric(html.unescape(fact.raw_lexical_value))
        definition: dict[str, JsonValue] = {
            "label": fact.concept_name,
            "wording": fact.definition_text,
            "unit_key": fact.unit_key,
            "currency": fact.currency,
            "accounting_basis": fact.accounting_basis,
            "consolidation_scope": "consolidated",
            "period_kind": "duration" if fact.period_start else "instant",
            "normalization": {"unit_key": unit, "multiplier": str(multiplier)},
        }
        definition_sha = _sha(_json(definition))
        run_id = "reviewed-sec-run:" + _sha(request.review_sha256 + fact.document_version_id)
        node_id = "reviewed-sec-node:" + _sha(run_id + fact.fact_key)
        stamp = request.reviewed_at
        runs[run_id] = ExtractionRun(
            extraction_run_id=run_id,
            idempotency_key=run_id,
            document_version_id=fact.document_version_id,
            input_sha256=evidence.blob_sha256,
            extractor_name=_NAME,
            extractor_config_sha256=request.review_sha256,
            extractor_code_version=_RECIPE,
            output_sha256=request.review_sha256,
            started_at=stamp,
            completed_at=stamp,
            outcome="succeeded",
        )
        claim_nodes.append(
            EvidenceNode(
                node_id=node_id,
                evidence_key=node_id,
                revision=1,
                extraction_run_id=run_id,
                node_kind="claim",
                text=fact.model_dump_json(),
                locator=value_node.locator,
                recorded_at=stamp,
            )
        )
        if not any(node.node_id == "reviewed-sec-review:" + _sha(run_id) for node in claim_nodes):
            review_node_id = "reviewed-sec-review:" + _sha(run_id)
            claim_nodes.append(
                EvidenceNode(
                    node_id=review_node_id,
                    evidence_key=review_node_id,
                    revision=1,
                    extraction_run_id=run_id,
                    node_kind="claim",
                    text=request.canonical_review_json,
                    locator=None,
                    recorded_at=stamp,
                )
            )
        cell = FactCellV2(
            fact_cell_id="reviewed-sec-cell:pending",
            idempotency_key="reviewed-sec-cell:pending",
            reporting_entity_id=subject.reporting_entity_id,
            concept_namespace="reviewed-sec:" + fact.sec_cik,
            # Native meaning includes the exact witnessed qualifying headers.
            # The short source row label remains in definition["label"].
            concept_name=fact.definition_text,
            taxonomy_name="issuer-reported-table",
            taxonomy_version="source-definition:" + definition_sha,
            accounting_basis=fact.accounting_basis,
            consolidation_scope="consolidated",
            period_kind="duration" if fact.period_start else "instant",
            period_start=fact.period_start,
            period_end=fact.period_end,
            fiscal_year=fact.period_end.year,
            fiscal_period=fact.fiscal_period,
            unit_key=unit,
            currency=fact.currency,
            effective_at=fact.period_end,
            knowledge_at=stamp,
            recorded_at=stamp,
        )
        # V3 identity excludes taxonomy versions and capture clocks. Source
        # definition versions stay on each observation, while repeated captures
        # reuse the first-seen cell through SourceFactRepository.
        cell_id = "reviewed-sec-cell:" + cell.derive_semantic_key()
        cell = cell.model_copy(update={"fact_cell_id": cell_id, "idempotency_key": cell_id})
        observation_id = "reviewed-sec-observation:" + _sha(run_id + fact.fact_key)
        locator = CanonicalJSONObject(
            root={
                "recipe": _RECIPE,
                "review_sha256": request.review_sha256,
                "reviewed_by": request.reviewed_by,
                "review_evidence": request.review_evidence,
                "source_kind": fact.source_kind,
                "accession_number": fact.accession_number,
                "sec_cik": fact.sec_cik,
                "native_locator": value_node.locator.model_dump(mode="json", exclude_none=True),
                "fulltext_run_id": fact.fulltext_run_id,
                "source_node_ids": list(
                    dict.fromkeys(
                        (fact.value_node_id, *(fragment.node_id for fragment in fragments))
                    )
                ),
                "value_capture_kind": fact.value_capture_kind,
                "numeric_fragments": [fragment.model_dump(mode="json") for fragment in fragments],
                "definition_node_ids": list(fact.definition_node_ids),
                "context_nodes": [
                    {
                        "node_id": node.node_id,
                        "text": node.text,
                        "locator_sha256": node.locator.canonical_sha256,
                    }
                    for node in contexts
                ],
                "source_definition_sha256": definition_sha,
                "definition": definition,
                "normalization": {
                    "raw_unit_key": fact.unit_key,
                    "raw_numeric_value": str(raw_value),
                    "multiplier": str(multiplier),
                    "target_unit_key": unit,
                },
            }
        )
        observation = ReportedFactObservationV2(
            observation_id=observation_id,
            idempotency_key=observation_id,
            fact_cell_id=cell.fact_cell_id,
            observation_kind="reported",
            value_kind="numeric",
            numeric_value=format(raw_value * multiplier, "f"),
            raw_lexical_value=fact.raw_lexical_value,
            method_name=_NAME,
            method_version=_RECIPE,
            method_config_sha256=request.review_sha256,
            revision_kind="initial",
            effective_at=fact.period_end,
            knowledge_at=stamp,
            recorded_at=stamp,
            document_version_id=fact.document_version_id,
            evidence_node_id=node_id,
            source_locator=locator,
            source_entry_sha256=_sha(fact.model_dump_json()),
            subject_binding_revision_id=subject.binding_revision_id,
            source_taxonomy_version="source-definition:" + definition_sha,
        )
        prepared.append(ReportedSourceFact(cell=cell, observation=observation))
        observations.append((fact.fact_key, observation_id))
    plan_sha = _sha(
        _json(
            {
                "review_sha256": request.review_sha256,
                "facts": [item.model_dump(mode="json") for item in prepared],
                "rejections": [item.model_dump(mode="json") for item in request.rejections],
            }
        )
    )
    if request.apply and request.expected_plan_sha256 != plan_sha:
        raise ValueError("reviewed SEC dry-run plan changed")
    result = ReviewedSecFinancialResult(
        mode="apply" if request.apply else "dry_run",
        plan_sha256=plan_sha,
        review_sha256=request.review_sha256,
        captured_count=len(prepared),
        rejected_count=len(request.rejections),
        source_population_complete=not request.rejections,
        observation_ids=tuple(observations),
    )
    if not request.apply:
        return result
    ledger = EvidenceLedger(conn)
    for run in runs.values():
        ledger.persist(run)
    for node in claim_nodes:
        ledger.persist(node)
    for run_id in runs:
        count = sum(node.extraction_run_id == run_id for node in claim_nodes)
        seals.append(
            ExtractionRunCompletenessSealV2(
                extraction_seal_id="reviewed-sec-seal:" + _sha(run_id),
                idempotency_key="reviewed-sec-seal:" + _sha(run_id),
                extraction_run_id=run_id,
                expected_node_count=count,
                completeness_policy_name="closed_reviewed_financial_population_not_document_coverage",
                completeness_policy_version=_RECIPE,
                completeness_policy_sha256=request.review_sha256,
                knowledge_at=request.reviewed_at,
                recorded_at=request.reviewed_at,
            )
        )
    publication_id = "reviewed-sec-publication:" + request.review_sha256
    receipt = SourceFactRepository(conn).publish(
        SourceFactPublication(
            publication_id=publication_id,
            idempotency_key=publication_id,
            created_at=request.reviewed_at,
            recorded_at=request.reviewed_at,
            reported_facts=tuple(prepared),
            extraction_seals=tuple(seals),
        )
    )
    return result.model_copy(
        update={"publication_id": receipt.publication_id, "exact_replay": receipt.exact_replay}
    )


class ReviewedSecNodeCommitment(_Closed):
    local_key: str = Field(pattern=r"^html:\d+:(?:passage|section|table|table_row|table_cell)$")
    locator_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    text_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ReviewedSecFinancialSelector(_Closed):
    """Private source selection before native document and node IDs exist.

    The coordinates use qualified extractor local keys in node-ID fields.
    Binding replaces only those local keys and the two capture IDs. No values,
    signs, definitions, units or period coordinates are inferred or changed.
    """

    schema_version: Literal["reviewed_sec_financial_selector.v1"] = (
        "reviewed_sec_financial_selector.v1"
    )
    source_url: str = Field(min_length=1)
    source_doc_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_kind: SecSourceKind
    accession_number: str = Field(pattern=r"^\d{10}-\d{2}-\d{6}$")
    sec_cik: str = Field(pattern=r"^\d{10}$")
    coordinates: CanonicalJSONObject
    node_commitments: tuple[ReviewedSecNodeCommitment, ...] = Field(min_length=1, max_length=250)


def bind_reviewed_sec_financial_selector(
    evidence: ReviewedSecHtmlEvidence, selector: ReviewedSecFinancialSelector
) -> ReviewedSecFinancialFact:
    """Bind exact source selectors to an independently verified native capture."""
    selector = ReviewedSecFinancialSelector.model_validate(selector.model_dump(mode="json"))
    if (evidence.blob_sha256, evidence.source_url, evidence.source_kind) != (
        selector.source_doc_sha256,
        selector.source_url,
        selector.source_kind,
    ):
        raise ValueError("reviewed SEC selector does not match its captured source")
    by_local_key = {
        f"html:{node.locator.filing_ordinal}:{node.kind}": node
        for node in evidence.nodes
        if node.locator.filing_ordinal is not None
    }
    selected: dict[str, str] = {}
    for commitment in selector.node_commitments:
        node = by_local_key.get(commitment.local_key)
        if (
            node is None
            or node.locator.canonical_sha256 != commitment.locator_sha256
            or _sha(node.text) != commitment.text_sha256
        ):
            raise ValueError("reviewed SEC selector node commitment does not replay")
        if commitment.local_key in selected:
            raise ValueError("reviewed SEC selector repeats a source node commitment")
        selected[commitment.local_key] = node.node_id
    payload = dict(selector.coordinates.root)
    value_key = payload.get("value_node_id")
    if not isinstance(value_key, str) or value_key not in selected:
        raise ValueError("reviewed SEC selector value node lacks a commitment")
    payload["value_node_id"] = selected[value_key]
    for key in ("definition_node_ids", "context_node_ids"):
        ids = payload.get(key)
        if not isinstance(ids, list) or any(
            not isinstance(identity, str) or identity not in selected for identity in ids
        ):
            raise ValueError("reviewed SEC selector supporting nodes lack exact commitments")
        payload[key] = [selected[str(identity)] for identity in ids]
    fragments = payload.get("value_fragments", [])
    if not isinstance(fragments, list):
        raise ValueError("reviewed SEC selector requires typed numeric fragments")
    bound_fragments: list[JsonValue] = []
    for fragment in fragments:
        if (
            not isinstance(fragment, dict)
            or not isinstance(fragment.get("node_id"), str)
            or str(fragment["node_id"]) not in selected
        ):
            raise ValueError("reviewed SEC selector numeric fragment lacks an exact commitment")
        bound_fragments.append({**fragment, "node_id": selected[str(fragment["node_id"])]})
    payload["value_fragments"] = bound_fragments
    return ReviewedSecFinancialFact.model_validate(
        {
            **payload,
            "document_version_id": evidence.document_version_id,
            "fulltext_run_id": evidence.fulltext_run_id,
            "source_kind": selector.source_kind,
            "source_doc_sha256": selector.source_doc_sha256,
            "accession_number": selector.accession_number,
            "sec_cik": selector.sec_cik,
        }
    )
