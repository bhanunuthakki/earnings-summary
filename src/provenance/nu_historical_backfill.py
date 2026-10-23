"""Exact NU Q2 2025 source publication; semantic admission remains separate."""

from __future__ import annotations

import io
import json
import re
import sqlite3
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Literal

import pypdf
from pydantic import BaseModel, ConfigDict, Field, field_validator

from provenance.evidence_ledger import EvidenceLedger, EvidenceLocator, EvidenceNode, ExtractionRun
from provenance.evidence_native_candidates import (
    read_verified_local_evidence_bytes,
    select_evidence_native_candidates_by_id,
)
from provenance.fact_plane_v2 import (
    CanonicalJSONObject,
    ExtractionRunCompletenessSealV2,
    FactCellV2,
    ReportedFactObservationV2,
)
from provenance.fulltext_extractor_identity import (
    OFFICE_FULLTEXT_EXTRACTOR,
    PDF_FULLTEXT_EXTRACTOR,
    PDF_FULLTEXT_PYPDF_VERSION,
    select_approved_fulltext_run,
)
from provenance.issuer_registry import IssuerRegistry
from provenance.ooxml_extraction import extract_office_nodes
from provenance.reporting_entity_registry import ReportingEntityRegistry
from provenance.source_fact_repository import (
    PublicationReceipt,
    ReportedSourceFact,
    SourceFactPublication,
    SourceFactRepository,
)
from runtime.deterministic import parse_instant, sha256_text

WORKBOOK_SHA = "1f8f1fe5e3859bc09526c44102ff66a3608a5a40026b8539dcf0794f5ed62594"
DECK_SHA = "96748fa5c8fff32dd1e784d79e24e92c1f382f5fb49d3ac86eef1bc7e6f41bf0"
_URL_ROOT = "https://api.mziq.com/mzfilemanager/v2/d/59a081d2-0d63-4bb5-b786-4c07ae26bc74/"
WORKBOOK_URL = _URL_ROOT + "5c818a2c-27a4-80cd-ea93-202347860b1e?origin=2"
DECK_URL = _URL_ROOT + "94e05ef8-27f0-a330-8bf9-0c005fc24c3e?origin=2"
SHEET = "NPLs (Inadimplência)"
_RECIPE = "nu-q2-2025-reviewed-ir.v1"
_END = datetime(2025, 6, 30, tzinfo=UTC)


class _Closed(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class NuHistoricalSource(_Closed):
    document_version_id: str = Field(min_length=1)
    fulltext_run_id: str = Field(min_length=1)


class NuHistoricalBackfillRequest(_Closed):
    workbook: NuHistoricalSource
    deck: NuHistoricalSource
    content_roots: tuple[Path, ...] = Field(min_length=1)
    reviewed_by: str = Field(min_length=1)
    reviewed_at: datetime

    @field_validator("reviewed_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("reviewed_at must include a timezone")
        return value.astimezone(UTC)


class _Node(_Closed):
    node_id: str
    kind: str
    text: str
    locator: EvidenceLocator


class NuHistoricalBackfillPlan(_Closed):
    publication: SourceFactPublication
    runs: tuple[ExtractionRun, ...]
    nodes: tuple[EvidenceNode, ...]
    plan_sha256: str
    canonical_admission: Literal["not_performed"] = "not_performed"
    coverage: Literal["three_reviewed_Q2_2025_observations_only"] = (
        "three_reviewed_Q2_2025_observations_only"
    )


def _source(
    conn: sqlite3.Connection,
    source: NuHistoricalSource,
    request: NuHistoricalBackfillRequest,
    *,
    workbook: bool,
) -> tuple[str, str, tuple[_Node, ...]]:
    expected_sha, expected_url = (WORKBOOK_SHA, WORKBOOK_URL) if workbook else (DECK_SHA, DECK_URL)
    candidate = select_evidence_native_candidates_by_id(
        conn, document_version_ids=(source.document_version_id,)
    )[0]
    row = conn.execute(
        "SELECT d.issuer_id,d.ticker,d.form_type,d.recorded_at,o.source_kind,o.retrieved_at "
        "FROM evidence_document_versions d JOIN evidence_source_observations o ON o.observation_id=d.observation_id WHERE d.document_version_id=?",
        (source.document_version_id,),
    ).fetchone()
    if (
        row is None
        or (
            str(row[1]),
            str(row[2]),
            candidate.blob_sha256,
            candidate.source_ref,
        )
        != ("NU", "IR", expected_sha, expected_url)
        or str(row[4]) not in {"ir_doc", "ir_document"}
    ):
        raise ValueError("NU historical source identity does not match the reviewed version")
    if max(parse_instant(row[3]), parse_instant(row[5])) > request.reviewed_at:
        raise ValueError("NU historical capture is newer than its review")
    identity = OFFICE_FULLTEXT_EXTRACTOR if workbook else PDF_FULLTEXT_EXTRACTOR
    if (
        select_approved_fulltext_run(
            conn, source.document_version_id, identity, observed_through=request.reviewed_at
        )
        != source.fulltext_run_id
    ):
        raise ValueError("NU historical source requires its exact approved fulltext run")
    raw = read_verified_local_evidence_bytes(
        conn,
        storage_uri=candidate.storage_uri,
        expected_sha256=expected_sha,
        expected_byte_size=candidate.byte_size,
        allowed_roots=request.content_roots,
        document_version_id=source.document_version_id,
    ).raw_bytes
    nodes = tuple(
        _Node(
            node_id=str(node[0]),
            kind=str(node[1]),
            text=str(node[2]),
            locator=EvidenceLocator.model_validate_json(str(node[3])),
        )
        for node in conn.execute(
            "SELECT node_id,node_kind,text,locator_json FROM evidence_nodes WHERE extraction_run_id=? AND node_kind<>'document' ORDER BY node_id",
            (source.fulltext_run_id,),
        )
    )
    actual = {(node.kind, node.text, node.locator.canonical_sha256) for node in nodes}
    if workbook:
        expected = {
            (node.node_kind, node.text, node.locator.canonical_sha256)
            for node in extract_office_nodes(expected_url, raw, "xlsx")
        }
    else:
        if pypdf.__version__ != PDF_FULLTEXT_PYPDF_VERSION:
            raise ValueError("NU historical PDF parser is not the approved runtime")
        reader = pypdf.PdfReader(io.BytesIO(raw))
        if reader.is_encrypted and (
            not reader.decrypt("") or reader.are_permissions_valid is not True
        ):
            raise ValueError("NU historical PDF permissions cannot be verified")
        expected = {
            (
                "pdf_page",
                text,
                EvidenceLocator(source_ref=expected_url, page_number=number).canonical_sha256,
            )
            for number, page in enumerate(reader.pages, 1)
            if (text := (page.extract_text() or "")).strip()
        }
    if len(actual) != len(nodes) or actual != expected:
        raise ValueError("NU historical fulltext does not replay from exact raw bytes")
    return str(row[0]), str(row[4]), nodes


def _one(nodes: tuple[_Node, ...], *, page: int | None = None, row: int | None = None) -> _Node:
    selected = (
        [node for node in nodes if node.kind == "pdf_page" and node.locator.page_number == page]
        if page is not None
        else [
            node
            for node in nodes
            if node.kind == "table_row"
            and node.locator.sheet_name == SHEET
            and re.fullmatch(rf"[A-Z]+{row}:[A-Z]+{row}", node.locator.cell_range or "")
        ]
    )
    if len(selected) != 1:
        raise ValueError("NU historical exact page or worksheet row is missing or ambiguous")
    return selected[0]


def prepare_nu_historical_backfill(
    conn: sqlite3.Connection, request: NuHistoricalBackfillRequest
) -> NuHistoricalBackfillPlan:
    """Replay both immutable sources and prepare exactly three scoped observations."""
    issuer, workbook_kind, workbook = _source(conn, request.workbook, request, workbook=True)
    deck_issuer, deck_kind, deck = _source(conn, request.deck, request, workbook=False)
    identity = IssuerRegistry(conn).resolve_identifier(
        "sec_cik", "0001691493", knowledge_at=request.reviewed_at
    )
    subject = ReportingEntityRegistry(conn).canonicalize_recorded_subject(
        issuer, knowledge_at=request.reviewed_at
    )
    if (
        identity.material_dissent
        or identity.issuer_id != issuer
        or deck_issuer != issuer
        or subject.material_dissent
        or subject.reporting_entity_id is None
    ):
        raise ValueError("NU historical sources require one undisputed issuer and reporting entity")
    pages = {number: _one(deck, page=number) for number in (15, 16, 35)}
    rows = {number: _one(workbook, row=number) for number in (9, 10, 12, 13, 15)}
    if (
        pages[15].text.count("9.2%") != 1
        or "Holdings" not in pages[15].text
        or "annualized" not in pages[15].text
        or not any(period in pages[15].text for period in ("Q2'25", "Q2\u201925"))
    ):
        raise ValueError("NU historical annualized Holdings NIM witnesses changed")
    if (
        "Brazil" not in pages[16].text
        or "excluding SMEs" not in pages[16].text
        or "excluding SMEs" not in pages[35].text
    ):
        raise ValueError("NU historical Brazil consumer-ex-SME scope witnesses changed")
    for row_number, address, expected in (
        (9, "D9", "30/06/2025"),
        (10, "B10", "Brazil Consumer Credit Portfolio"),
        (12, "B12", "15-90 days NPL"),
        (13, "B13", "90+ NPL"),
        (15, "B15", "Includes information of Brazilian operation only."),
    ):
        match = re.search(
            rf'(?:^| \| ){address}: ("(?:[^"\\]|\\.)*")(?:$| \| )', rows[row_number].text
        )
        if match is None or json.loads(match[1]) != expected:
            raise ValueError("NU historical exact worksheet period/label/scope witness changed")
    request_sha = sha256_text(request.model_dump_json())
    prepared: list[ReportedSourceFact] = []
    claim_nodes: list[EvidenceNode] = []
    runs: dict[str, ExtractionRun] = {}
    facts = (
        (
            "risk_adjusted_nim",
            request.deck,
            pages[15],
            "9.2%",
            "0.092",
            "Risk-Adjusted Net Interest Margin; annualized; Nu Holdings",
            (pages[15], pages[35]),
        ),
        (
            "npl_15_90_days",
            request.workbook,
            rows[12],
            "0.044",
            "0.044",
            "15-90 days NPL; Brazil consumer credit card and loans excluding SMEs",
            (rows[9], rows[10], rows[12], rows[15], pages[16], pages[35]),
        ),
        (
            "npl_90_plus_days",
            request.workbook,
            rows[13],
            "0.066",
            "0.066",
            "90+ days NPL; Brazil consumer credit card and loans excluding SMEs",
            (rows[9], rows[10], rows[13], rows[15], pages[16], pages[35]),
        ),
    )
    for key, source, value_node, raw, value, meaning, contexts in facts:
        if key != "risk_adjusted_nim":
            match = re.search(
                rf"(?:^| \| )D{12 if key == 'npl_15_90_days' else 13}: raw=([^ |]+)(?:$| \| )",
                value_node.text,
            )
            if match is None or Decimal(match[1]) != Decimal(raw):
                raise ValueError("NU historical exact worksheet value changed")
            raw = match[1]
        definition = {
            "meaning": meaning,
            "context_nodes": [node.model_dump(mode="json") for node in contexts],
            "period": "Q2 2025",
            "normalization": "percent_to_pure" if key == "risk_adjusted_nim" else "pure_unchanged",
        }
        definition_sha = sha256_text(json.dumps(definition, sort_keys=True))
        run_id = "nu-history-run:" + sha256_text(request_sha + source.document_version_id)
        runs[run_id] = ExtractionRun(
            extraction_run_id=run_id,
            idempotency_key=run_id,
            document_version_id=source.document_version_id,
            input_sha256=DECK_SHA if key == "risk_adjusted_nim" else WORKBOOK_SHA,
            extractor_name="nu-historical-backfill",
            extractor_config_sha256=request_sha,
            extractor_code_version=_RECIPE,
            output_sha256=request_sha,
            started_at=request.reviewed_at,
            completed_at=request.reviewed_at,
            outcome="succeeded",
        )
        node_id = "nu-history-node:" + sha256_text(run_id + key)
        locator = value_node.locator.model_copy(
            update={
                "cell_range": None,
                "cell_address": None
                if key == "risk_adjusted_nim"
                else f"D{12 if key == 'npl_15_90_days' else 13}",
            }
        )
        claim_nodes.append(
            EvidenceNode(
                node_id=node_id,
                evidence_key=node_id,
                revision=1,
                extraction_run_id=run_id,
                node_kind="claim",
                text=json.dumps(definition, sort_keys=True),
                locator=locator,
                recorded_at=request.reviewed_at,
            )
        )
        cell = FactCellV2(
            fact_cell_id="pending",
            idempotency_key="pending",
            reporting_entity_id=subject.reporting_entity_id,
            concept_namespace="nu-reviewed-ir",
            concept_name=meaning,
            taxonomy_name="issuer-reported-management-ratio",
            taxonomy_version="source-definition:" + definition_sha,
            accounting_basis="management",
            consolidation_scope="other",
            period_kind="duration" if key == "risk_adjusted_nim" else "instant",
            period_start=datetime(2025, 4, 1, tzinfo=UTC) if key == "risk_adjusted_nim" else None,
            period_end=_END,
            fiscal_year=2025,
            fiscal_period="Q2",
            unit_key="pure",
            currency=None,
            effective_at=_END,
            knowledge_at=request.reviewed_at,
            recorded_at=request.reviewed_at,
        )
        cell_id = "nu-history-cell:" + cell.derive_semantic_key()
        cell = cell.model_copy(update={"fact_cell_id": cell_id, "idempotency_key": cell_id})
        observation_id = "nu-history-observation:" + sha256_text(run_id + key)
        source_locator = CanonicalJSONObject.model_validate(
            {
                "recipe": _RECIPE,
                "reviewed_by": request.reviewed_by,
                "source_kind": deck_kind if key == "risk_adjusted_nim" else workbook_kind,
                "fulltext_run_id": source.fulltext_run_id,
                "native_locator": locator.model_dump(mode="json", exclude_none=True),
                "source_node_ids": [value_node.node_id],
                "definition": definition,
                "source_definition_sha256": definition_sha,
                "consolidated_npl_comparability": "not_comparable"
                if key != "risk_adjusted_nim"
                else "not_applicable",
                "historical_public_witness_admission": "not_performed",
            }
        )
        observation = ReportedFactObservationV2(
            observation_id=observation_id,
            idempotency_key=observation_id,
            fact_cell_id=cell_id,
            observation_kind="reported",
            value_kind="numeric",
            numeric_value=value,
            raw_lexical_value=raw,
            method_name="nu-historical-backfill",
            method_version=_RECIPE,
            method_config_sha256=request_sha,
            revision_kind="initial",
            effective_at=_END,
            knowledge_at=request.reviewed_at,
            recorded_at=request.reviewed_at,
            document_version_id=source.document_version_id,
            evidence_node_id=node_id,
            source_locator=source_locator,
            source_entry_sha256=sha256_text(source_locator.canonical_json + raw),
            subject_binding_revision_id=subject.binding_revision_id,
            source_taxonomy_version=cell.taxonomy_version or "",
        )
        prepared.append(ReportedSourceFact(cell=cell, observation=observation))
    seals = tuple(
        ExtractionRunCompletenessSealV2(
            extraction_seal_id="nu-history-seal:" + sha256_text(run_id),
            idempotency_key="nu-history-seal:" + sha256_text(run_id),
            extraction_run_id=run_id,
            expected_node_count=sum(node.extraction_run_id == run_id for node in claim_nodes),
            completeness_policy_name="three_reviewed_observations_not_document_coverage",
            completeness_policy_version=_RECIPE,
            completeness_policy_sha256=request_sha,
            knowledge_at=request.reviewed_at,
            recorded_at=request.reviewed_at,
        )
        for run_id in runs
    )
    publication_id = "nu-history-publication:" + request_sha
    publication = SourceFactPublication(
        publication_id=publication_id,
        idempotency_key=publication_id,
        created_at=request.reviewed_at,
        recorded_at=request.reviewed_at,
        reported_facts=tuple(prepared),
        extraction_seals=seals,
    )
    payload = {
        "publication": publication.model_dump(mode="json"),
        "runs": [run.model_dump(mode="json") for run in runs.values()],
        "nodes": [node.model_dump(mode="json") for node in claim_nodes],
    }
    return NuHistoricalBackfillPlan(
        publication=publication,
        runs=tuple(runs.values()),
        nodes=tuple(claim_nodes),
        plan_sha256=sha256_text(json.dumps(payload, sort_keys=True)),
    )


def publish_nu_historical_backfill(
    conn: sqlite3.Connection, request: NuHistoricalBackfillRequest, *, expected_plan_sha256: str
) -> PublicationReceipt:
    """Recheck the prepared graph, then publish atomically; caller owns lock/commit."""
    plan = prepare_nu_historical_backfill(conn, request)
    if plan.plan_sha256 != expected_plan_sha256:
        raise ValueError("NU historical dry-run plan changed")
    started_transaction = not conn.in_transaction
    if started_transaction:
        conn.execute("BEGIN IMMEDIATE")
    conn.execute("SAVEPOINT nu_historical_backfill")
    try:
        ledger = EvidenceLedger(conn)
        for record in (*plan.runs, *plan.nodes):
            ledger.persist(record)
        receipt = SourceFactRepository(conn).publish(plan.publication)
        conn.execute("RELEASE SAVEPOINT nu_historical_backfill")
        return receipt
    except BaseException:
        conn.execute("ROLLBACK TO SAVEPOINT nu_historical_backfill")
        conn.execute("RELEASE SAVEPOINT nu_historical_backfill")
        if started_transaction:
            conn.rollback()
        raise
