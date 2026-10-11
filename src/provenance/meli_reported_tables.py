"""Bounded, source-native MELI 10-Q table publication.

This publishes three reported source concepts, not canonical metric bindings or
whole-document completeness. The closed population is current H1 NIMAL and the
current available-assets and total-debt rows of the issuer's reconciliation.
Definition wording and table headers remain part of each source commitment.
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
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator

from provenance.evidence_ledger import EvidenceLedger, EvidenceLocator, EvidenceNode, ExtractionRun
from provenance.evidence_native_candidates import (
    resolve_local_storage_uri,
    select_evidence_native_candidates_by_id,
)
from provenance.fact_plane_v2 import (
    CanonicalJSONObject,
    ExtractionRunCompletenessSealV2,
    FactCellV2,
    ReportedFactObservationV2,
)
from provenance.fulltext_backfill import verify_native_html_replay
from provenance.fulltext_extractor_identity import STRUCTURED_WEB_ARCHIVE_FULLTEXT_EXTRACTOR
from provenance.issuer_registry import IssuerRegistry
from provenance.reporting_entity_registry import ReportingEntityRegistry
from provenance.sec_native_capture import SecNativeCaptureError, load_captured_sec_filing_package
from provenance.source_fact_repository import (
    ReportedSourceFact,
    SourceFactPublication,
    SourceFactRepository,
)

_RECIPE = "meli-10q-current-h1-reported-tables.v2"
_NORMALIZATION_RECIPE = "meli-reported-unit-normalization.v1"
_NAME = "meli-reported-tables"
_CIK = "0001099590"
Metric = Literal["nimal", "available_cash_and_investments", "total_debt_and_leases"]
_METRICS: tuple[Metric, ...] = ("nimal", "available_cash_and_investments", "total_debt_and_leases")
_LABELS = {
    "nimal": "NIMAL (9)",
    "available_cash_and_investments": "Cash and cash equivalents(1), short-term investments(2) and long-term investments(3)",
    "total_debt_and_leases": "Total debt",
}
_NIMAL_DEFINITION = (
    "represents the annualized ratio between the total credits revenues (excluding the results of sale of loans receivables) "
    "less funding costs and provision for doubtful accounts for the period (excluding the results of sale of loans receivables) "
    "and total average gross loans receivable for the period."
)
_CASH_DEFINITIONS = (
    "Includes cash and cash equivalents (excluding cash and cash equivalents restricted due to management restriction policies).",
    "Excludes time deposits, foreign debt securities and foreign government debt securities restricted and held in guarantee.",
    "Excludes foreign government debt securities restricted and held in guarantee, investments held in VIEs as a consequence of securitization transactions and equity securities held at cost.",
)
_DEBT_DEFINITION = (
    "We define net debt as total debt which includes current and non-current loans payable and other financial liabilities "
    "and current and non-current operating lease liabilities"
)
_CURRENCY_DEFINITION = "These unaudited interim condensed consolidated financial statements are stated in U.S. dollars, except for where otherwise indicated."


class _Closed(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ReportedTableRequest(_Closed):
    inventory_key: str = Field(min_length=1)
    accession_number: str = Field(pattern=r"^\d{10}-\d{2}-\d{6}$")
    document_version_id: str = Field(min_length=1, max_length=128)
    fulltext_run_id: str = Field(min_length=1, max_length=128)
    content_roots: tuple[Path, ...] = Field(min_length=1)
    recorded_at: datetime
    apply: bool = False

    @field_validator("recorded_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("recorded_at requires a timezone")
        return value.astimezone(UTC)


class TableDisposition(_Closed):
    metric: Metric
    status: Literal["captured", "rejected"]
    reason_code: str
    numeric_value: str | None = None
    raw_lexical_value: str | None = None
    source_node_ids: tuple[str, ...] = ()
    definition_node_ids: tuple[str, ...] = ()
    source_definition_sha256: str | None = None
    unit_key: str | None = None
    currency: str | None = None
    normalized_unit_key: Literal["USD", "ratio"] | None = None
    normalized_numeric_value: str | None = None
    normalization_multiplier: str | None = None
    period_start: datetime | None = None
    period_end: datetime


class ReportedTableResult(_Closed):
    mode: Literal["dry_run", "apply"]
    recipe: Literal["meli-10q-current-h1-reported-tables.v2"] = _RECIPE
    document_version_id: str
    extraction_run_id: str
    population: tuple[TableDisposition, ...]
    captured_count: int
    rejected_count: int
    source_population_complete: bool
    canonical_admission: Literal["not_performed"] = "not_performed"
    publication_id: str | None = None
    exact_replay: bool = False


class ReportedTableNode(_Closed):
    node_id: str
    kind: str
    text: str
    locator: EvidenceLocator


class ReportedTableEvidence(_Closed):
    issuer_id: str
    source_url: str
    blob_sha256: str
    source: str
    period_start: datetime
    period_end: datetime
    nodes: tuple[ReportedTableNode, ...]
    recorded_at: datetime


class ReportedTableRejectionError(ValueError):
    """A source population member that cannot be safely interpreted."""


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def clean_reported_text(value: str) -> str:
    return " ".join(html.unescape(value).split())


def _time(value: object) -> datetime:
    parsed = datetime.fromisoformat(str(value))
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def load_reported_table_evidence(
    conn: sqlite3.Connection, request: ReportedTableRequest
) -> ReportedTableEvidence:
    try:
        package = load_captured_sec_filing_package(
            conn, inventory_key=request.inventory_key, accession_number=request.accession_number
        )
    except SecNativeCaptureError as error:
        raise ValueError(str(error)) from error
    primary = package[0]
    if primary.document_version_id != request.document_version_id:
        raise ValueError("requested version is not the current captured primary filing")
    url = urlsplit(primary.source_url)
    if (
        url.scheme != "https"
        or url.hostname != "www.sec.gov"
        or not url.path.startswith(
            "/Archives/edgar/data/1099590/" + request.accession_number.replace("-", "") + "/"
        )
    ):
        raise ValueError("MELI reported tables require the SEC primary filing source")
    row = conn.execute(
        "SELECT d.form_type,d.period_start,d.period_end,d.recorded_at,d.issuer_id,d.accession_number,o.source_url,o.retrieved_at "
        "FROM evidence_document_versions d JOIN evidence_source_observations o ON o.observation_id=d.observation_id "
        "WHERE d.document_version_id=?",
        (request.document_version_id,),
    ).fetchone()
    if row is None or row[0] != "10-Q" or row[2] is None:
        raise ValueError("MELI H1 recipe requires an explicitly dated 10-Q")
    if tuple(row[4:7]) != (primary.issuer_id, request.accession_number, primary.source_url):
        raise ValueError("native document issuer/accession/source conflicts with inventory")
    end = _time(row[2])
    # The admitted NIMAL row must separately prove the exact six-month header.
    # SEC submissions commonly omit a start date; do not invent capture metadata.
    start = datetime(end.year, 1, 1, tzinfo=UTC) if row[1] is None else _time(row[1])
    if (start.month, start.day, end.month, end.day) != (1, 1, 6, 30) or start.year != end.year:
        raise ValueError(
            "unsupported fiscal period: only an explicitly dated H1 filing is qualified"
        )
    run = conn.execute(
        "SELECT document_version_id,input_sha256,extractor_name,extractor_code_version,extractor_config_sha256,outcome,completed_at FROM evidence_extraction_runs WHERE extraction_run_id=?",
        (request.fulltext_run_id,),
    ).fetchone()
    identity = STRUCTURED_WEB_ARCHIVE_FULLTEXT_EXTRACTOR
    if run is None or tuple(run[:6]) != (
        request.document_version_id,
        primary.blob_sha256,
        identity.name,
        identity.code_version,
        identity.config_sha256,
        "succeeded",
    ):
        raise ValueError("exact qualified native HTML extraction is required")
    if max(_time(row[3]), _time(row[7]), _time(run[6])) > request.recorded_at:
        raise ValueError("source evidence is newer than publication cutoff")
    cik = IssuerRegistry(conn).resolve_identifier("sec_cik", _CIK, knowledge_at=request.recorded_at)
    if cik.issuer_id != primary.issuer_id or cik.material_dissent:
        raise ValueError("MELI CIK conflicts with captured issuer")
    candidate = select_evidence_native_candidates_by_id(
        conn, document_version_ids=(request.document_version_id,)
    )[0]
    path = resolve_local_storage_uri(
        candidate.storage_uri, allowed_roots=tuple(p.resolve() for p in request.content_roots)
    )
    if path is None:
        raise ValueError("native source is outside approved local content roots")
    raw = path.read_bytes()
    if len(raw) != primary.byte_size or hashlib.sha256(raw).hexdigest() != primary.blob_sha256:
        raise ValueError("native source byte commitment mismatch")
    source = raw.decode("utf-8", errors="strict")
    rows = conn.execute(
        "SELECT node_id,node_kind,text,locator_json,locator_sha256 FROM evidence_nodes WHERE extraction_run_id=? ORDER BY node_id",
        (request.fulltext_run_id,),
    ).fetchall()
    nodes: list[ReportedTableNode] = []
    for node_id, kind, text, locator_json, locator_sha in rows:
        if locator_json is None:
            raise ValueError("native node lacks locator")
        locator = EvidenceLocator.model_validate_json(str(locator_json))
        if locator.canonical_sha256 != str(locator_sha) or locator.source_ref != primary.source_url:
            raise ValueError("native node locator commitment mismatch")
        if kind in {"passage", "table_cell"} and (
            locator.char_start is None
            or locator.char_end is None
            or source[locator.char_start : locator.char_end] != str(text)
        ):
            raise ValueError("native text does not replay from source bytes")
        nodes.append(
            ReportedTableNode(node_id=str(node_id), kind=str(kind), text=str(text), locator=locator)
        )
    verify_native_html_replay(
        raw, primary.source_url, tuple((n.kind, n.text, n.locator) for n in nodes)
    )
    return ReportedTableEvidence(
        issuer_id=primary.issuer_id,
        source_url=primary.source_url,
        blob_sha256=primary.blob_sha256,
        source=source,
        period_start=start,
        period_end=end,
        nodes=tuple(nodes),
        recorded_at=request.recorded_at,
    )


def _ordered(
    nodes: tuple[ReportedTableNode, ...] | list[ReportedTableNode],
) -> list[ReportedTableNode]:
    return sorted(nodes, key=lambda n: (n.locator.char_start or 0, n.node_id))


def reported_table_cells(
    evidence: ReportedTableEvidence, table: str, row: int
) -> list[tuple[str, tuple[ReportedTableNode, ...]]]:
    groups: dict[int, list[ReportedTableNode]] = {}
    for node in evidence.nodes:
        loc = node.locator
        if (
            node.kind == "table_cell"
            and loc.table_name == table
            and loc.table_row_index == row
            and loc.table_column_index is not None
        ):
            groups.setdefault(loc.table_column_index, []).append(node)
    return [
        (text, tuple(_ordered(nodes)))
        for _, nodes in sorted(groups.items())
        if (text := clean_reported_text("".join(n.text for n in _ordered(nodes))))
    ]


def _row(
    evidence: ReportedTableEvidence, metric: Metric
) -> tuple[str, int, list[tuple[str, tuple[ReportedTableNode, ...]]]]:
    groups: dict[tuple[str, int], dict[int, list[ReportedTableNode]]] = {}
    for node in evidence.nodes:
        loc = node.locator
        if (
            node.kind == "table_cell"
            and loc.table_name is not None
            and loc.table_row_index is not None
            and loc.table_column_index is not None
        ):
            groups.setdefault((loc.table_name, loc.table_row_index), {}).setdefault(
                loc.table_column_index, []
            ).append(node)
    matches: list[tuple[str, int, list[tuple[str, tuple[ReportedTableNode, ...]]]]] = []
    for (table, row), columns in groups.items():
        cells = [
            (text, tuple(_ordered(nodes)))
            for _, nodes in sorted(columns.items())
            if (text := clean_reported_text("".join(n.text for n in _ordered(nodes))))
        ]
        if cells and cells[0][0] == _LABELS[metric]:
            matches.append((table, row, cells))
    if len(matches) != 1:
        raise ReportedTableRejectionError("row_missing_or_ambiguous")
    return matches[0]


def reported_table_passage(
    evidence: ReportedTableEvidence,
    phrase: str,
    *,
    after: int | None = None,
    before: int | None = None,
) -> tuple[ReportedTableNode, ...]:
    # Span elements split management definitions. Reassemble only one native DOM
    # parent; never stitch unrelated pages or matches together.
    groups: dict[str, list[ReportedTableNode]] = {}
    for node in evidence.nodes:
        loc = node.locator
        if node.kind != "passage" or loc.filing_section_key_raw is None or loc.char_start is None:
            continue
        if (after is not None and loc.char_start <= after) or (
            before is not None and loc.char_start >= before
        ):
            continue
        parent = loc.filing_section_key_raw.rsplit("/", 1)[0]
        groups.setdefault(parent, []).append(node)
    matches = [
        tuple(_ordered(nodes))
        for nodes in groups.values()
        if phrase in clean_reported_text("".join(n.text for n in _ordered(nodes)))
    ]
    if len(matches) != 1:
        raise ReportedTableRejectionError("definition_missing_or_ambiguous")
    return matches[0]


def _extract(evidence: ReportedTableEvidence, metric: Metric) -> TableDisposition:
    table, row, cells = _row(evidence, metric)
    header = [
        n
        for n in evidence.nodes
        if n.kind == "table_cell"
        and n.locator.table_name == table
        and n.locator.table_row_index is not None
        and n.locator.table_row_index < row
    ]
    period_headers = [text for text, _ in reported_table_cells(evidence, table, 2)]
    year_headers = [text for text, _ in reported_table_cells(evidence, table, 3)]
    unit_headers = [text for text, _ in reported_table_cells(evidence, table, 4)]
    year = evidence.period_end.year
    numeric = [
        (text, nodes) for text, nodes in cells[1:] if re.fullmatch(r"\d[\d,]*(?:\.\d+)?", text)
    ]
    if metric == "nimal":
        if (
            period_headers != ["Six Months Ended June 30,", "Three Months Ended June 30,"]
            or year_headers != [str(year), str(year - 1), str(year), str(year - 1)]
            or unit_headers != ["(In millions, except percentages) (1)"] * 2
            or len(numeric) != 4
            or sum(text == "%" for text, _ in cells) != 4
        ):
            raise ReportedTableRejectionError("unsupported_period_or_unit_columns")
        definitions = reported_table_passage(
            evidence,
            _NIMAL_DEFINITION,
            after=max(n.locator.char_start or 0 for _, ns in cells for n in ns),
        )
        unit, currency, start = "percent", None, evidence.period_start
    else:
        if (
            period_headers != [f"June 30, {year}", f"December 31, {year - 1}"]
            or year_headers != ["(In millions)"]
            or len(numeric) != 2
        ):
            raise ReportedTableRejectionError("unsupported_period_or_unit_columns")
        currency_nodes = reported_table_passage(evidence, _CURRENCY_DEFINITION)
        if metric == "available_cash_and_investments":
            after = max(n.locator.char_start or 0 for _, ns in cells for n in ns)
            definitions = tuple(
                n
                for phrase in _CASH_DEFINITIONS
                for n in reported_table_passage(evidence, phrase, after=after)
            )
        else:
            definitions = reported_table_passage(
                evidence,
                _DEBT_DEFINITION,
                before=min(n.locator.char_start or 0 for _, ns in cells for n in ns),
            )
            table_labels = {
                reported_table_cells(evidence, table, i)[0][0]
                for i in range(1, row)
                if reported_table_cells(evidence, table, i)
            }
            if not {
                "Current Loans payable and other financial liabilities",
                "Non-current Loans payable and other financial liabilities",
                "Current Operating lease liabilities",
                "Non-current Operating lease liabilities",
            }.issubset(table_labels):
                raise ReportedTableRejectionError("debt_reconciliation_scope_missing")
        definitions += currency_nodes
        unit, currency, start = "USD_millions", "USD", None
    lexical, value_nodes = numeric[0]
    value = Decimal(lexical.replace(",", ""))
    if metric == "nimal" and value > 100:
        raise ReportedTableRejectionError("percentage_out_of_range")
    source_nodes = tuple(
        dict.fromkeys(n.node_id for n in (*header, *(n for _, ns in cells for n in ns)))
    )
    definition_nodes = tuple(dict.fromkeys(n.node_id for n in definitions))
    definition_sha = _sha(
        _json(
            {
                "recipe": _RECIPE,
                "metric": metric,
                "label": _LABELS[metric],
                "wording": [clean_reported_text(n.text) for n in definitions],
                "unit": unit,
                "basis": "management",
                "period_kind": "duration" if start else "instant",
            }
        )
    )
    # Scale conversion follows the exact retained table header. It changes only
    # representation; source tokens/units stay in the disposition and locator.
    normalized_unit, multiplier = (
        ("ratio", Decimal("0.01")) if metric == "nimal" else ("USD", Decimal("1000000"))
    )
    # The numeric cell comes first so the observation's locator is the value's
    # exact span, with all supporting headers/definitions retained separately.
    source_nodes = tuple(dict.fromkeys((*[n.node_id for n in value_nodes], *source_nodes)))
    return TableDisposition(
        metric=metric,
        status="captured",
        reason_code="exact_reported_row",
        numeric_value=str(value),
        raw_lexical_value=lexical,
        source_node_ids=source_nodes,
        definition_node_ids=definition_nodes,
        source_definition_sha256=definition_sha,
        unit_key=unit,
        currency=currency,
        normalized_unit_key=normalized_unit,
        normalized_numeric_value=format(value * multiplier, "f"),
        normalization_multiplier=str(multiplier),
        period_start=start,
        period_end=evidence.period_end,
    )


def publish_meli_reported_tables(
    conn: sqlite3.Connection, request: ReportedTableRequest
) -> ReportedTableResult:
    """Dry-run or atomically publish the exact three-member scoped population."""
    if conn.in_transaction:
        raise ValueError("reported-table publication requires an idle connection")
    # All authority reads share one snapshot. A concurrent writer can cause a
    # failed SQLite read-to-write upgrade, never publication against mixed reads.
    conn.execute("BEGIN")
    try:
        result = _publish_in_snapshot(conn, request)
        if request.apply:
            conn.commit()
        else:
            conn.rollback()
        return result
    except Exception:
        conn.rollback()
        raise


def _publish_in_snapshot(
    conn: sqlite3.Connection, request: ReportedTableRequest
) -> ReportedTableResult:
    evidence = load_reported_table_evidence(conn, request)
    identity = {
        "recipe": _RECIPE,
        "document": request.document_version_id,
        "sha": evidence.blob_sha256,
        "fulltext_run": request.fulltext_run_id,
    }
    config_sha = _sha(_json(identity))
    run_id = "meli-table-run:" + config_sha
    existing = conn.execute(
        "SELECT completed_at FROM evidence_extraction_runs WHERE extraction_run_id=?", (run_id,)
    ).fetchone()
    stamp = evidence.recorded_at if existing is None else _time(existing[0])
    if stamp > request.recorded_at:
        raise ValueError("existing extraction is newer than requested cutoff")
    subject = ReportingEntityRegistry(conn).canonicalize_recorded_subject(
        evidence.issuer_id, knowledge_at=stamp
    )
    if subject.reporting_entity_id is None or subject.material_dissent:
        raise ValueError("reported tables require one undisputed reporting entity")
    population: list[TableDisposition] = []
    for metric in _METRICS:
        try:
            population.append(_extract(evidence, metric))
        except ReportedTableRejectionError as error:
            population.append(
                TableDisposition(
                    metric=metric,
                    status="rejected",
                    reason_code=str(error),
                    period_end=evidence.period_end,
                )
            )
    captured = sum(p.status == "captured" for p in population)
    result = ReportedTableResult(
        mode="apply" if request.apply else "dry_run",
        document_version_id=request.document_version_id,
        extraction_run_id=run_id,
        population=tuple(population),
        captured_count=captured,
        rejected_count=len(population) - captured,
        source_population_complete=captured == len(_METRICS),
    )
    if not request.apply:
        return result
    output = _json([p.model_dump(mode="json") for p in population])
    nodes_by_id = {node.node_id: node for node in evidence.nodes}
    facts: list[ReportedSourceFact] = []
    try:
        ledger = EvidenceLedger(conn)
        ledger.persist(
            ExtractionRun(
                extraction_run_id=run_id,
                idempotency_key=run_id,
                document_version_id=request.document_version_id,
                input_sha256=evidence.blob_sha256,
                extractor_name=_NAME,
                extractor_config_sha256=config_sha,
                extractor_code_version=_RECIPE,
                output_sha256=_sha(output),
                started_at=stamp,
                completed_at=stamp,
                outcome="succeeded",
            )
        )
        for item in population:
            node_id = "meli-table-node:" + _sha(run_id + item.metric)
            source_node = nodes_by_id[item.source_node_ids[0]] if item.source_node_ids else None
            ledger.persist(
                EvidenceNode(
                    node_id=node_id,
                    evidence_key=node_id,
                    revision=1,
                    extraction_run_id=run_id,
                    node_kind="claim",
                    text=item.model_dump_json(),
                    locator=None if source_node is None else source_node.locator,
                    recorded_at=stamp,
                )
            )
            if item.status == "rejected":
                continue
            if (
                source_node is None
                or item.unit_key is None
                or item.source_definition_sha256 is None
                or item.numeric_value is None
                or item.raw_lexical_value is None
                or item.normalized_unit_key is None
                or item.normalized_numeric_value is None
                or item.normalization_multiplier is None
            ):
                raise ValueError("captured disposition has incomplete source identity")
            normalization: dict[str, JsonValue] = {
                "recipe": _NORMALIZATION_RECIPE,
                "raw_unit_key": item.unit_key,
                "raw_numeric_value": item.numeric_value,
                "raw_lexical_value": item.raw_lexical_value,
                "target_unit_key": item.normalized_unit_key,
                "multiplier": item.normalization_multiplier,
                "normalized_numeric_value": item.normalized_numeric_value,
            }
            normalized_definition_sha = _sha(
                _json(
                    {
                        "raw_source_definition_sha256": item.source_definition_sha256,
                        "normalization_recipe": _NORMALIZATION_RECIPE,
                        "raw_unit_key": item.unit_key,
                        "target_unit_key": item.normalized_unit_key,
                        "multiplier": item.normalization_multiplier,
                    }
                )
            )
            cell_id = "meli-table-cell:" + _sha(
                _json(
                    {
                        "entity": subject.reporting_entity_id,
                        "metric": item.metric,
                        "definition": normalized_definition_sha,
                        "end": item.period_end.isoformat(),
                        "start": None
                        if item.period_start is None
                        else item.period_start.isoformat(),
                    }
                )
            )
            taxonomy_version = "source-definition:" + normalized_definition_sha
            cell = FactCellV2(
                fact_cell_id=cell_id,
                idempotency_key=cell_id,
                reporting_entity_id=subject.reporting_entity_id,
                concept_namespace="issuer-reported-table:sec:1099590",
                concept_name=item.metric,
                taxonomy_name="issuer-reported-table",
                taxonomy_version=taxonomy_version,
                accounting_basis="management",
                consolidation_scope="consolidated",
                period_kind="duration" if item.period_start else "instant",
                period_start=item.period_start,
                period_end=item.period_end,
                fiscal_year=item.period_end.year,
                fiscal_period="H1" if item.period_start else "Q2",
                unit_key=item.normalized_unit_key,
                currency=item.currency,
                effective_at=item.period_end,
                knowledge_at=stamp,
                recorded_at=stamp,
            )
            observation_id = "meli-table-observation:" + _sha(run_id + item.metric)
            locator = CanonicalJSONObject(
                root={
                    "native_locator": source_node.locator.model_dump(
                        mode="json", exclude_none=True
                    ),
                    "source_node_ids": list(item.source_node_ids),
                    "definition_node_ids": list(item.definition_node_ids),
                    "source_definition_sha256": normalized_definition_sha,
                    "raw_source_definition_sha256": item.source_definition_sha256,
                    "normalization": normalization,
                    "recipe": _RECIPE,
                    "fulltext_run_id": request.fulltext_run_id,
                }
            )
            observation = ReportedFactObservationV2(
                observation_id=observation_id,
                idempotency_key=observation_id,
                fact_cell_id=cell_id,
                observation_kind="reported",
                value_kind="numeric",
                numeric_value=item.normalized_numeric_value,
                raw_lexical_value=item.raw_lexical_value,
                method_name=_NAME,
                method_version=_RECIPE,
                method_config_sha256=config_sha,
                revision_kind="initial",
                effective_at=item.period_end,
                knowledge_at=stamp,
                recorded_at=stamp,
                document_version_id=request.document_version_id,
                evidence_node_id=node_id,
                source_locator=locator,
                source_entry_sha256=_sha(item.model_dump_json()),
                subject_binding_revision_id=subject.binding_revision_id,
                source_taxonomy_version=taxonomy_version,
            )
            facts.append(ReportedSourceFact(cell=cell, observation=observation))
        seal_id = "meli-table-seal:" + config_sha
        publication_id = "meli-table-publication:" + config_sha
        receipt = SourceFactRepository(conn).publish(
            SourceFactPublication(
                publication_id=publication_id,
                idempotency_key=publication_id,
                created_at=stamp,
                recorded_at=stamp,
                reported_facts=tuple(facts),
                extraction_seals=(
                    ExtractionRunCompletenessSealV2(
                        extraction_seal_id=seal_id,
                        idempotency_key=seal_id,
                        extraction_run_id=run_id,
                        expected_node_count=len(_METRICS),
                        completeness_policy_name="closed_meli_three_metric_population_not_document_coverage",
                        completeness_policy_version=_RECIPE,
                        completeness_policy_sha256=_sha(output),
                        knowledge_at=stamp,
                        recorded_at=stamp,
                    ),
                ),
            )
        )
    except Exception:
        conn.rollback()
        raise
    return result.model_copy(
        update={"publication_id": receipt.publication_id, "exact_replay": receipt.exact_replay}
    )
