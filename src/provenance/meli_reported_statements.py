"""Closed MELI H1 displayed-statement population; no XBRL or semantic admission.

Each concept names a displayed row and its complete displayed scope. Empty XBRL
coordinates are intentional: table scope is retained in the definition/locator,
not fabricated as issuer taxonomy dimensions. No forecast or derived TTM enters.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict

from provenance.evidence_ledger import EvidenceLedger, EvidenceNode, ExtractionRun
from provenance.fact_plane_v2 import (
    CanonicalJSONObject,
    ExtractionRunCompletenessSealV2,
    FactCellV2,
    ReportedFactObservationV2,
)
from provenance.meli_reported_tables import (
    ReportedTableEvidence,
    ReportedTableNode,
    ReportedTableRejectionError,
    ReportedTableRequest,
    clean_reported_text,
    load_reported_table_evidence,
    reported_table_cells,
    reported_table_passage,
)
from provenance.reporting_entity_registry import ReportingEntityRegistry
from provenance.source_fact_repository import (
    ReportedSourceFact,
    SourceFactPublication,
    SourceFactRepository,
)

RECIPE = "meli-h1-displayed-statements.v1"
_NAME = "meli-reported-statements"
_NAMESPACE = "issuer-reported-statement:sec:1099590"
_NORMALIZATION = "meli-statement-displayed-unit-normalization.v1"
_BASIS = "The accompanying unaudited interim condensed consolidated financial statements are prepared in conformity with accounting principles generally accepted in the U.S."
_CURRENCY = "These unaudited interim condensed consolidated financial statements are stated in U.S. dollars, except for where otherwise indicated."
_FLOW_LABELS = {
    "commerce_revenues": "Total commerce revenues",
    "total_revenues": "Net revenues and financial income",
    "fintech_revenues": "Total fintech revenues",
    "credit_revenues": "Credit revenues (4)",
    "operating_income": "Income from operations",
    "depreciation_and_amortization": "Depreciation and amortization",
    "productive_asset_expenditures": "Investments in property and equipment, intangible assets and intangible assets at fair value",
}
_REVENUE_DEFINITIONS = (
    "Includes final value fees and flat fees paid by sellers derived from intermediation services",
    "Includes revenues from inventory sales and related shipping fees.",
    "Includes revenues from commissions the Company charges for transactions off-platform",
    "Includes interest earned on loans and advances granted to users, and interest and commissions earned on Mercado Pago credit card transactions.",
    "Includes sales of mobile point of sales devices.",
)


class _Closed(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class StatementDisposition(_Closed):
    population_key: str
    concept: str
    status: Literal["captured", "rejected"]
    reason_code: str
    period_start: datetime | None
    period_end: datetime
    raw_lexical_value: str | None = None
    displayed_scope: dict[str, str] = {}
    source_node_ids: tuple[str, ...] = ()
    definition_node_ids: tuple[str, ...] = ()
    source_definition_sha256: str | None = None
    unit_key: Literal["USD_millions", "shares"] | None = None
    normalized_unit_key: Literal["USD", "shares"] | None = None
    normalized_numeric_value: str | None = None
    normalization_multiplier: str | None = None


class StatementResult(_Closed):
    mode: Literal["dry_run", "apply"]
    recipe: Literal["meli-h1-displayed-statements.v1"] = RECIPE
    document_version_id: str
    extraction_run_id: str
    population: tuple[StatementDisposition, ...]
    expected_count: Literal[18] = 18
    captured_count: int
    rejected_count: int
    source_population_complete: bool
    canonical_admission: Literal["not_performed"] = "not_performed"
    document_completeness: Literal["not_claimed"] = "not_claimed"
    publication_id: str | None = None
    exact_replay: bool = False


class _Row(_Closed):
    table: str
    index: int
    cells: tuple[tuple[str, tuple[ReportedTableNode, ...]], ...]

    @property
    def texts(self) -> list[str]:
        return [text for text, _ in self.cells]

    @property
    def nodes(self) -> tuple[ReportedTableNode, ...]:
        return tuple(n for _, nodes in self.cells for n in nodes)


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _rows(evidence: ReportedTableEvidence) -> dict[str, tuple[_Row, ...]]:
    coordinates = sorted(
        {
            (n.locator.table_name, n.locator.table_row_index)
            for n in evidence.nodes
            if n.kind == "table_cell"
            and n.locator.table_name is not None
            and n.locator.table_row_index is not None
        }
    )
    tables: dict[str, list[_Row]] = {}
    for table, index in coordinates:
        cells = reported_table_cells(evidence, table, index)
        if cells:
            tables.setdefault(table, []).append(_Row(table=table, index=index, cells=tuple(cells)))
    return {key: tuple(value) for key, value in tables.items()}


def _header(rows: tuple[_Row, ...], index: int) -> list[list[str]]:
    return [row.texts for row in rows if row.index < index]


def _require_section_headers(header: list[list[str]], after: int, allowed: set[str]) -> None:
    # The qualified layout has a closed set of singleton section headings.
    # An inserted local currency, scale or accounting-basis override is not
    # silently ignored in favor of the global declaration.
    if any(len(line) == 1 and line[0] not in allowed for line in header[after:]):
        raise ReportedTableRejectionError("unexpected_statement_header_or_scope_override")


def _numeric(row: _Row) -> list[tuple[str, tuple[ReportedTableNode, ...]]]:
    cells = [(text, nodes) for text, nodes in row.cells[1:] if text != "$"]
    if any(
        not re.fullmatch(
            r"(?:(?:\d+|\d{1,3}(?:,\d{3})+)(?:\.\d+)?|\((?:\d+|\d{1,3}(?:,\d{3})+)(?:\.\d+)?\))",
            text,
        )
        for text, _ in cells
    ):
        raise ReportedTableRejectionError("unsupported_displayed_numeric_cells")
    return cells


def _statement_context(
    evidence: ReportedTableEvidence, row: _Row, title: str, unit: str
) -> tuple[ReportedTableNode, ...]:
    before = min(n.locator.char_start or 0 for n in row.nodes)
    title_nodes = reported_table_passage(evidence, title, before=before)
    after = max(n.locator.char_end or 0 for n in title_nodes)
    unit_nodes = reported_table_passage(evidence, unit, after=after, before=before)
    return (*title_nodes, *unit_nodes)


def _revenue_rows(
    evidence: ReportedTableEvidence, tables: dict[str, tuple[_Row, ...]], label: str
) -> list[tuple[_Row, int, tuple[ReportedTableNode, ...], dict[str, str]]]:
    results: list[tuple[_Row, int, tuple[ReportedTableNode, ...], dict[str, str]]] = []
    year = evidence.period_end.year
    for rows in tables.values():
        for row in rows:
            if row.texts[0] != label:
                continue
            header = _header(rows, row.index)
            if ["Six Months Ended June 30,"] not in header:
                continue  # An explicitly separate quarterly table is not an H1 candidate.
            expected = [
                ["Six Months Ended June 30,"],
                ["Brazil", "Mexico", "Argentina", "Other Countries (6)", "Total"],
                [str(year), str(year - 1)] * 5,
                ["(In millions)"],
            ]
            if header[:4] != expected or len(_numeric(row)) != 10:
                raise ReportedTableRejectionError(
                    "revenue_period_geography_or_unit_columns_invalid"
                )
            labels = [r.texts[0] for r in rows]
            order = [
                "Commerce services (1)",
                "Commerce product sales (2)",
                "Total commerce revenues",
                "Financial services and income (3)",
                "Credit revenues (4)",
                "Fintech product sales (5)",
                "Total fintech revenues",
                "Total net revenues and financial income",
            ]
            if labels[4:] != order:
                raise ReportedTableRejectionError("revenue_business_product_population_changed")
            definition = tuple(
                n
                for phrase in _REVENUE_DEFINITIONS
                for n in reported_table_passage(evidence, phrase)
            )
            scope = {
                "geography": "Total",
                "presentation": "disaggregated by similar products and services",
                "business": "Commerce"
                if label == "Total commerce revenues"
                else "Fintech"
                if label != "Total net revenues and financial income"
                else "Total",
                "product": "Credit revenues" if label == "Credit revenues (4)" else "Total",
            }
            results.append((row, 8, definition, scope))
    return results


def _flow_rows(
    evidence: ReportedTableEvidence, tables: dict[str, tuple[_Row, ...]], concept: str
) -> list[tuple[_Row, int, tuple[ReportedTableNode, ...], dict[str, str]]]:
    label = _FLOW_LABELS[concept]
    if concept in {"commerce_revenues", "fintech_revenues", "credit_revenues"}:
        return _revenue_rows(evidence, tables, label)
    income = concept in {"total_revenues", "operating_income"}
    year = evidence.period_end.year
    expected = (
        [
            ["Six Months Ended June 30,", "Three Months Ended June 30,"],
            [str(year), str(year - 1)] * 2,
        ]
        if income
        else [["Six Months Ended June 30,"], [str(year), str(year - 1)]]
    )
    result: list[tuple[_Row, int, tuple[ReportedTableNode, ...], dict[str, str]]] = []
    for rows in tables.values():
        for row in rows:
            if row.texts[0] != label or _header(rows, row.index)[:2] != expected:
                continue
            if not income and ["Cash flows from operations:"] not in _header(rows, row.index):
                continue
            _require_section_headers(
                _header(rows, row.index),
                2,
                {"Operating expenses:", "Other income (expenses):"}
                if income
                else {
                    "Cash flows from operations:",
                    "Adjustments to reconcile net income to net cash provided by operating activities:",
                    "Changes in assets and liabilities:",
                    "Cash flows from investing activities:",
                    "Cash flows from financing activities:",
                },
            )
            if len(_numeric(row)) != (4 if income else 2):
                raise ReportedTableRejectionError("statement_column_population_changed")
            title = "Interim Condensed Consolidated Statements of " + (
                "Income" if income else "Cash Flows"
            )
            unit = (
                "(In millions of U.S. dollars, except for share data)"
                if income
                else "(In millions of U.S. dollars)"
            )
            definitions = _statement_context(evidence, row, title, unit)
            result.append(
                (
                    row,
                    0,
                    definitions,
                    {
                        "geography": "consolidated",
                        "statement": "income" if income else "cash flows",
                        "business": "Total",
                        "product": "Total",
                    },
                )
            )
    if concept == "total_revenues":
        # An independently displayed total must agree; neither is silently preferred.
        result.extend(_revenue_rows(evidence, tables, "Total net revenues and financial income"))
    return result


def _point_rows(
    evidence: ReportedTableEvidence, tables: dict[str, tuple[_Row, ...]], concept: str
) -> list[tuple[_Row, int, tuple[ReportedTableNode, ...], dict[str, str]]]:
    result: list[tuple[_Row, int, tuple[ReportedTableNode, ...], dict[str, str]]] = []
    year = evidence.period_end.year
    for rows in tables.values():
        for row in rows:
            header = _header(rows, row.index)
            if concept == "diluted_weighted_average_shares":
                if row.texts[0] != "Weighted average of outstanding common shares":
                    continue
                section = next(
                    (
                        r.texts[0]
                        for r in reversed(rows[: rows.index(row)])
                        if r.texts[0] in {"Basic earnings per share", "Diluted earnings per share"}
                    ),
                    None,
                )
                if section != "Diluted earnings per share":
                    continue
                if any(text in {"$", "%"} for text in row.texts):
                    raise ReportedTableRejectionError("share_count_unit_override")
                if (
                    header[:2]
                    != [
                        ["Six Months Ended June 30,", "Three Months Ended June 30,"],
                        [str(year), str(year - 1)] * 2,
                    ]
                    or len(_numeric(row)) != 4
                ):
                    raise ReportedTableRejectionError("diluted_share_period_columns_invalid")
                _require_section_headers(
                    header, 2, {"Basic earnings per share", "Diluted earnings per share"}
                )
                definitions = _statement_context(
                    evidence,
                    row,
                    "Interim Condensed Consolidated Statements of Income",
                    "(In millions of U.S. dollars, except for share data)",
                )
                scope = {
                    "geography": "consolidated",
                    "share_basis": "Diluted earnings per share",
                    "measurement": "Weighted average of outstanding common shares",
                }
            elif concept == "gross_loans_receivable":
                if row.texts[0] != "Total" or [f"June 30, {year}"] not in header:
                    continue
                if (
                    header[:3]
                    != [
                        [f"June 30, {year}"],
                        [
                            "Loans receivable",
                            "Allowance for doubtful accounts",
                            "Loans receivable, net",
                        ],
                        ["(In millions)"],
                    ]
                    or len(_numeric(row)) != 3
                ):
                    continue
                if [r.texts[0] for r in rows][3:] != [
                    "Merchant",
                    "Consumer",
                    "Credit cards",
                    "Asset-backed",
                    "Total",
                ]:
                    raise ReportedTableRejectionError("loan_category_population_changed")
                definitions = reported_table_passage(
                    evidence, "The Company classifies loans receivable as"
                )
                scope = {
                    "geography": "consolidated",
                    "loan_categories": "Merchant; Consumer; Credit cards; Asset-backed",
                    "measurement": "Loans receivable",
                    "excluded_columns": "Allowance for doubtful accounts; Loans receivable, net",
                }
            else:
                if row.texts[0] != "Operating lease liabilities":
                    continue
                section = next(
                    (
                        r.texts[0]
                        for r in reversed(rows[: rows.index(row)])
                        if r.texts[0] in {"Current liabilities:", "Non-current liabilities:"}
                    ),
                    None,
                )
                wanted = (
                    "Current liabilities:"
                    if concept == "current_operating_lease_liabilities"
                    else "Non-current liabilities:"
                )
                if section != wanted:
                    continue
                compact = [[text.replace(" ", "") for text in line] for line in header[:1]]
                if (
                    compact != [[f"June30,{year}", f"December31,{year - 1}"]]
                    or len(_numeric(row)) != 2
                ):
                    raise ReportedTableRejectionError("lease_period_columns_invalid")
                _require_section_headers(
                    header,
                    1,
                    {
                        "Assets",
                        "Current assets:",
                        "Non-current assets:",
                        "Liabilities",
                        "Current liabilities:",
                        "Non-current liabilities:",
                    },
                )
                definitions = _statement_context(
                    evidence,
                    row,
                    "Interim Condensed Consolidated Balance Sheets",
                    "(In millions of U.S. dollars, except par value)",
                )
                scope = {
                    "geography": "consolidated",
                    "liability_section": wanted,
                    "lease_type": "Operating lease liabilities",
                }
            result.append((row, 0, definitions, scope))
    return result


def _extract(
    evidence: ReportedTableEvidence,
    tables: dict[str, tuple[_Row, ...]],
    concept: str,
    prior: bool = False,
) -> StatementDisposition:
    flow = concept in _FLOW_LABELS
    shares = concept == "diluted_weighted_average_shares"
    end = evidence.period_end.replace(year=evidence.period_end.year - int(prior))
    start = end.replace(month=1, day=1) if flow or shares else None
    key = concept + ("_prior_h1" if prior else "_current_h1" if flow or shares else "_current")
    empty = StatementDisposition(
        population_key=key,
        concept=concept,
        status="rejected",
        reason_code="source_row_missing",
        period_start=start,
        period_end=end,
    )
    try:
        matches = (
            _flow_rows(evidence, tables, concept)
            if flow
            else _point_rows(evidence, tables, concept)
        )
        if not matches:
            return empty
        numeric_cells = [_numeric(row)[column + int(prior)] for row, column, _, _ in matches]
        lexicals = [text for text, _ in numeric_cells]
        values = [
            Decimal(text.strip("()").replace(",", "")) * (-1 if text.startswith("(") else 1)
            for text in lexicals
        ]
        if len(set(values)) != 1:
            raise ReportedTableRejectionError("conflicting_displayed_duplicates")
        # Expenditure is a positive magnitude by the reported payments convention;
        # preserve the source cash-flow parentheses and the signed transformation.
        multiplier = Decimal(-1 if concept == "productive_asset_expenditures" else 1) * (
            1 if shares else 1_000_000
        )
        if concept == "productive_asset_expenditures" and values[0] > 0:
            raise ReportedTableRejectionError("expenditure_cashflow_sign_changed")
        row, _, definitions, scope = matches[0]
        basis_nodes = reported_table_passage(evidence, _BASIS)
        currency_nodes = () if shares else reported_table_passage(evidence, _CURRENCY)
        definitions = (*definitions, *basis_nodes, *currency_nodes)
        all_nodes = tuple(
            n
            for r, _, _, _ in matches
            for rr in tables[r.table]
            if rr.index <= r.index
            for n in rr.nodes
        )
        definition_nodes = tuple(
            dict.fromkeys(
                n.node_id for n in (*definitions, *(n for _, _, defs, _ in matches for n in defs))
            )
        )
        source_nodes = tuple(
            dict.fromkeys(
                [n.node_id for _, ns in numeric_cells for n in ns] + [n.node_id for n in all_nodes]
            )
        )
        unit = "shares" if shares else "USD_millions"
        definition = {
            "recipe": RECIPE,
            "concept": concept,
            "label": row.texts[0],
            "displayed_scope": scope,
            "basis": "us_gaap",
            "unit": unit,
            "period_kind": "duration" if start else "instant",
            "wording": [clean_reported_text(n.text) for n in definitions],
        }
        return empty.model_copy(
            update={
                "status": "captured",
                "reason_code": "exact_displayed_statement_row",
                "raw_lexical_value": lexicals[0],
                "displayed_scope": scope,
                "source_node_ids": source_nodes,
                "definition_node_ids": definition_nodes,
                "source_definition_sha256": _sha(_json(definition)),
                "unit_key": unit,
                "normalized_unit_key": "shares" if shares else "USD",
                "normalized_numeric_value": format(values[0] * multiplier, "f"),
                "normalization_multiplier": str(multiplier),
            }
        )
    except ReportedTableRejectionError as error:
        return empty.model_copy(update={"reason_code": str(error)})


def publish_meli_reported_statements(
    conn: sqlite3.Connection, request: ReportedTableRequest
) -> StatementResult:
    """Publish only the 18-member displayed population inside one read/write snapshot."""
    if conn.in_transaction:
        raise ValueError("statement publication requires an idle connection")
    conn.execute("BEGIN")
    try:
        evidence = load_reported_table_evidence(conn, request)
        tables = _rows(evidence)
        population = tuple(
            _extract(evidence, tables, concept, prior)
            for concept in _FLOW_LABELS
            for prior in (False, True)
        ) + tuple(
            _extract(evidence, tables, concept)
            for concept in (
                "gross_loans_receivable",
                "diluted_weighted_average_shares",
                "current_operating_lease_liabilities",
                "noncurrent_operating_lease_liabilities",
            )
        )
        config = _sha(
            _json(
                {
                    "recipe": RECIPE,
                    "document": request.document_version_id,
                    "sha": evidence.blob_sha256,
                    "fulltext_run": request.fulltext_run_id,
                }
            )
        )
        run_id = "meli-statement-run:" + config
        captured = sum(p.status == "captured" for p in population)
        result = StatementResult(
            mode="apply" if request.apply else "dry_run",
            document_version_id=request.document_version_id,
            extraction_run_id=run_id,
            population=population,
            captured_count=captured,
            rejected_count=18 - captured,
            source_population_complete=captured == 18,
        )
        if request.apply:
            result = _publish(conn, request, evidence, result, config)
            conn.commit()
        else:
            conn.rollback()
        return result
    except Exception:
        conn.rollback()
        raise


def _publish(
    conn: sqlite3.Connection,
    request: ReportedTableRequest,
    evidence: ReportedTableEvidence,
    result: StatementResult,
    config: str,
) -> StatementResult:
    existing = conn.execute(
        "SELECT completed_at FROM evidence_extraction_runs WHERE extraction_run_id=?",
        (result.extraction_run_id,),
    ).fetchone()
    stamp = request.recorded_at if existing is None else datetime.fromisoformat(str(existing[0]))
    if stamp > request.recorded_at:
        raise ValueError("existing statement extraction is newer than cutoff")
    subject = ReportingEntityRegistry(conn).canonicalize_recorded_subject(
        evidence.issuer_id, knowledge_at=stamp
    )
    if subject.reporting_entity_id is None or subject.material_dissent:
        raise ValueError("statement publication requires one undisputed reporting entity")
    output = _json([item.model_dump(mode="json") for item in result.population])
    ledger = EvidenceLedger(conn)
    ledger.persist(
        ExtractionRun(
            extraction_run_id=result.extraction_run_id,
            idempotency_key=result.extraction_run_id,
            document_version_id=request.document_version_id,
            input_sha256=evidence.blob_sha256,
            extractor_name=_NAME,
            extractor_code_version=RECIPE,
            extractor_config_sha256=config,
            output_sha256=_sha(output),
            started_at=stamp,
            completed_at=stamp,
            outcome="succeeded",
        )
    )
    nodes = {node.node_id: node for node in evidence.nodes}
    facts: list[ReportedSourceFact] = []
    for item in result.population:
        node_id = "meli-statement-node:" + _sha(result.extraction_run_id + item.population_key)
        source = nodes[item.source_node_ids[0]] if item.source_node_ids else None
        ledger.persist(
            EvidenceNode(
                node_id=node_id,
                evidence_key=node_id,
                revision=1,
                extraction_run_id=result.extraction_run_id,
                node_kind="claim",
                text=item.model_dump_json(),
                locator=None if source is None else source.locator,
                recorded_at=stamp,
            )
        )
        if item.status == "rejected":
            continue
        if (
            source is None
            or item.normalized_unit_key is None
            or item.normalized_numeric_value is None
            or item.source_definition_sha256 is None
            or item.raw_lexical_value is None
        ):
            raise ValueError("captured statement lacks exact source identity")
        definition_sha = _sha(
            _json(
                {
                    "raw_definition": item.source_definition_sha256,
                    "normalization": _NORMALIZATION,
                    "factor": item.normalization_multiplier,
                    "target_unit": item.normalized_unit_key,
                }
            )
        )
        cell_id = "meli-statement-cell:" + _sha(
            _json(
                {
                    "entity": subject.reporting_entity_id,
                    "concept": item.concept,
                    "definition": definition_sha,
                    "start": None if item.period_start is None else item.period_start.isoformat(),
                    "end": item.period_end.isoformat(),
                }
            )
        )
        taxonomy = "source-definition:" + definition_sha
        cell = FactCellV2(
            fact_cell_id=cell_id,
            idempotency_key=cell_id,
            reporting_entity_id=subject.reporting_entity_id,
            concept_namespace=_NAMESPACE,
            concept_name=item.concept,
            taxonomy_name="issuer-reported-statement",
            taxonomy_version=taxonomy,
            accounting_basis="us_gaap",
            consolidation_scope="consolidated",
            dimensions=(),
            period_kind="duration" if item.period_start else "instant",
            period_start=item.period_start,
            period_end=item.period_end,
            fiscal_year=item.period_end.year,
            fiscal_period="H1" if item.period_start else "Q2",
            unit_key=item.normalized_unit_key,
            currency=None if item.normalized_unit_key == "shares" else "USD",
            effective_at=item.period_end,
            knowledge_at=stamp,
            recorded_at=stamp,
        )
        observation_id = "meli-statement-observation:" + _sha(
            result.extraction_run_id + item.population_key
        )
        locator = CanonicalJSONObject.model_validate(
            {
                "native_locator": source.locator.model_dump(mode="json", exclude_none=True),
                "source_node_ids": list(item.source_node_ids),
                "definition_node_ids": list(item.definition_node_ids),
                "displayed_scope": item.displayed_scope,
                "source_definition_sha256": definition_sha,
                "raw_source_definition_sha256": item.source_definition_sha256,
                "normalization": {
                    "recipe": _NORMALIZATION,
                    "raw_unit_key": item.unit_key,
                    "raw_lexical_value": item.raw_lexical_value,
                    "target_unit_key": item.normalized_unit_key,
                    "multiplier": item.normalization_multiplier,
                    "sign_convention": "displayed_cash_outflow_to_positive_expenditure"
                    if item.concept == "productive_asset_expenditures"
                    else "displayed_signed_amount",
                },
                "recipe": RECIPE,
                "fulltext_run_id": request.fulltext_run_id,
                "scope_representation": "displayed_table_labels_not_xbrl_dimensions",
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
            method_version=RECIPE,
            method_config_sha256=config,
            revision_kind="initial",
            effective_at=item.period_end,
            knowledge_at=stamp,
            recorded_at=stamp,
            document_version_id=request.document_version_id,
            evidence_node_id=node_id,
            source_locator=locator,
            source_entry_sha256=_sha(item.model_dump_json()),
            subject_binding_revision_id=subject.binding_revision_id,
            source_taxonomy_version=taxonomy,
        )
        facts.append(ReportedSourceFact(cell=cell, observation=observation))
    seal_id = "meli-statement-seal:" + config
    publication_id = "meli-statement-publication:" + config
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
                    extraction_run_id=result.extraction_run_id,
                    expected_node_count=18,
                    completeness_policy_name="closed_meli_h1_18_statement_population_not_document_coverage",
                    completeness_policy_version=RECIPE,
                    completeness_policy_sha256=_sha(output),
                    knowledge_at=stamp,
                    recorded_at=stamp,
                ),
            ),
        )
    )
    return result.model_copy(
        update={"publication_id": receipt.publication_id, "exact_replay": receipt.exact_replay}
    )
