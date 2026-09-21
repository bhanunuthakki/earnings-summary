"""Canonical reported statement inputs for the generic DCF; no raw-cache fallback."""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict

from dcf.fiscal_periods import detect_fy_periods
from dcf.primary_fact_overlay import Statement, primary_source_eligible, statement_field_mappings
from provenance.canonical_fact_resolution import CanonicalFactResolutionEngine
from provenance.fact_read_model import FactReadModel, ProvenanceBundle
from provenance.metric_ontology import MetricOntology

_STATEMENTS: tuple[Statement, ...] = ("income", "balance", "cash_flow")
_MAPPING = {
    item.line_item: (statement, item)
    for statement in _STATEMENTS
    for item in statement_field_mappings(statement)
}
_REQUIRED_DURATION = frozenset(
    {
        "revenue",
        "cost_of_revenue",
        "research_and_development",
        "sga",
        "operating_income",
        "net_income",
        "depreciation_and_amortization",
        "stock_based_compensation",
        "capital_expenditure",
    }
)
_REQUIRED_BALANCE = frozenset({"total_stockholders_equity", "total_debt"})
_ReadPlan = tuple[str, str, str, str, str, str]


class DcfStatementCell(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    concept: str
    canonical_metric_cell_id: str
    metric_id: str
    definition_revision_id: str | None = None
    resolution_revision_id: str | None = None
    provenance: ProvenanceBundle | None = None
    reason_codes: tuple[str, ...] = ()
    source_document_id: int | None = None
    source_type: str | None = None
    source_tier: str | None = None
    source_url: str | None = None

    @property
    def available(self) -> bool:
        return self.provenance is not None and not self.reason_codes


class DcfStatementInputs(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    schema_version: Literal["canonical-dcf-statements/v1"] = "canonical-dcf-statements/v1"
    ticker: str
    as_of: datetime
    cells: tuple[DcfStatementCell, ...] = ()
    reason_codes: tuple[str, ...] = ()

    def require_complete_actuals(self) -> None:
        """Validate model-driving actuals; missing inputs never become zero."""
        if self.reason_codes:
            raise ValueError("canonical DCF statements unavailable: " + ";".join(self.reason_codes))
        periods: dict[tuple[int, str], dict[str, DcfStatementCell]] = defaultdict(dict)
        identities: dict[str, set[tuple[str, str | None]]] = defaultdict(set)
        for item in self.cells:
            if not item.available or item.provenance is None:
                if item.concept in _REQUIRED_DURATION | _REQUIRED_BALANCE | {
                    "weighted_avg_shares_diluted"
                }:
                    raise ValueError(
                        f"canonical DCF input unavailable: {item.concept}: {item.reason_codes}"
                    )
                continue
            identities[item.concept].add((item.metric_id, item.definition_revision_id))
            source = item.provenance.cell
            if source.fiscal_year is not None and source.fiscal_period is not None:
                key = (source.fiscal_year, source.fiscal_period)
                if item.concept in periods[key]:
                    raise ValueError(f"ambiguous canonical DCF period: {key} {item.concept}")
                periods[key][item.concept] = item
        if any(len(values) != 1 for values in identities.values()):
            raise ValueError("canonical DCF metric/definition continuity unavailable")
        if not periods:
            raise ValueError("canonical DCF statements unavailable: no admitted history")
        currencies: set[str] = set()
        coordinate_scope: set[tuple[str, str, str]] = set()
        for key, fields in periods.items():
            missing = (_REQUIRED_DURATION | _REQUIRED_BALANCE) - fields.keys()
            if missing:
                raise ValueError(f"canonical DCF required actuals missing: {key} {sorted(missing)}")
            revenue = fields["revenue"].provenance
            if revenue is None:
                raise ValueError("canonical DCF revenue evidence unavailable")
            for item in fields.values():
                bundle = item.provenance
                if bundle is None:
                    continue
                source = bundle.cell
                coordinate_scope.add(
                    (
                        source.reporting_entity_id,
                        source.accounting_basis,
                        source.consolidation_scope,
                    )
                )
                if bundle.observation.currency is not None:
                    currencies.add(bundle.observation.currency)
                if source.period_end != revenue.cell.period_end:
                    raise ValueError(f"canonical DCF fiscal end mismatch: {key}")
                if (
                    source.period_kind == "duration"
                    and source.period_start != revenue.cell.period_start
                ):
                    raise ValueError(f"canonical DCF duration mismatch: {key} {item.concept}")
            if not {"cash_and_short_term_investments", "cash_and_equivalents"} & fields.keys():
                raise ValueError(f"canonical DCF cash unavailable: {key}")
        if len(currencies) != 1 or len(coordinate_scope) != 1:
            raise ValueError("canonical DCF currency or issuer/basis/scope mismatch")
        cadence = detect_fy_periods(periods)
        if cadence not in {("Q1", "Q2", "Q3", "Q4"), ("Q2", "Q4")}:
            raise ValueError("canonical DCF unsupported fiscal cadence")
        previous: tuple[tuple[int, str], ProvenanceBundle] | None = None
        for key in sorted(periods):
            revenue = periods[key]["revenue"].provenance
            if revenue is None or revenue.cell.period_start is None:
                raise ValueError("canonical DCF exact duration start unavailable")
            duration_days = (
                revenue.cell.period_end.date() - revenue.cell.period_start.date()
            ).days + 1
            # Quarterly admission is the existing financial-reader 70-105-day
            # policy. For established Q2/Q4 cadence, use cockpit_fundamentals'
            # 175-200-day half-year spacing, with exact discrete boundaries below.
            minimum, maximum = (175, 200) if cadence == ("Q2", "Q4") else (70, 105)
            if not minimum <= duration_days <= maximum:
                raise ValueError(
                    f"canonical DCF unsupported fiscal duration: {key} {duration_days} days"
                )
            if key[1] not in cadence:
                raise ValueError("canonical DCF inconsistent fiscal cadence")
            if previous is not None:
                old_key, old = previous
                position = cadence.index(old_key[1]) + 1
                expected = (
                    (old_key[0] + 1, cadence[0])
                    if position == len(cadence)
                    else (old_key[0], cadence[position])
                )
                if key != expected or revenue.cell.period_start.date() != (
                    old.cell.period_end.date() + timedelta(days=1)
                ):
                    raise ValueError("canonical DCF noncontiguous or overlapping fiscal actuals")
            previous = key, revenue
        if not any(all((year, period) in periods for period in cadence) for year, _ in periods):
            raise ValueError("canonical DCF complete fiscal year unavailable")
        for year in {year for year, _ in periods}:
            if not all((year, period) in periods for period in cadence):
                continue
            first = periods[year, cadence[0]]["revenue"].provenance
            last = periods[year, cadence[-1]]["revenue"].provenance
            if first is None or last is None or first.cell.period_start is None:
                raise ValueError("canonical DCF annual coordinates unavailable")
            span = (last.cell.period_end.date() - first.cell.period_start.date()).days + 1
            # Existing annual admission tolerance includes 52/53-week fiscal years.
            if abs(span - 365) > 20:
                raise ValueError(f"canonical DCF unsupported annual span: {year} {span} days")
        latest = periods[max(periods)]
        shares = latest.get("weighted_avg_shares_diluted")
        if (
            shares is None
            or shares.provenance is None
            or shares.provenance.observation.decimal_value is None
            or shares.provenance.observation.decimal_value <= 0
        ):
            raise ValueError("canonical DCF positive diluted shares unavailable")

    def builder_records(self, statement: Statement) -> list[dict[str, object]]:
        """Temporary compatibility shape at the source adapter, never provider data."""
        rows: dict[tuple[int, str], dict[str, object]] = {}
        for item in self.cells:
            if (
                not item.available
                or item.provenance is None
                or _MAPPING[item.concept][0] != statement
            ):
                continue
            source, value = item.provenance.cell, item.provenance.observation
            if (
                source.fiscal_year is None
                or source.fiscal_period is None
                or value.decimal_value is None
            ):
                continue
            key = (source.fiscal_year, source.fiscal_period)
            record = rows.setdefault(
                key,
                {
                    "fiscalYear": source.fiscal_year,
                    "period": source.fiscal_period,
                    "date": source.period_end.date().isoformat(),
                },
            )
            if value.currency is not None:
                record["reportedCurrency"] = value.currency
            record[_MAPPING[item.concept][1].fmp_field] = float(value.decimal_value)
        return [rows[key] for key in sorted(rows, reverse=True)]

    def bridge_lineage(self, statement: Statement) -> dict[str, object]:
        """Adapt exact canonical identities to the existing approved bridge contract."""
        applied: list[dict[str, object]] = []
        for item in self.cells:
            if (
                not item.available
                or item.provenance is None
                or _MAPPING[item.concept][0] != statement
            ):
                continue
            bundle = item.provenance
            applied.append(
                {
                    "line_item": item.concept,
                    "fmp_field": _MAPPING[item.concept][1].fmp_field,
                    "period_end": bundle.cell.period_end.date().isoformat(),
                    "fiscal_period_type": bundle.cell.fiscal_period,
                    "primary_value": float(bundle.observation.decimal_value or Decimal(0)),
                    "unit": "count" if item.concept == "weighted_avg_shares_diluted" else "actual",
                    "currency": bundle.observation.currency,
                    "source_doc_id": item.source_document_id,
                    "source_type": item.source_type,
                    "source_tier": item.source_tier,
                    "source_url": item.source_url,
                    "as_of": self.as_of.isoformat(),
                    "reported_observation_id": bundle.observation.observation_id,
                    "resolution_id": item.resolution_revision_id,
                    "canonical_metric_cell_id": item.canonical_metric_cell_id,
                    "metric_definition_revision_id": item.definition_revision_id,
                    "locator": bundle.evidence.source_locator.model_dump_json()
                    if bundle.evidence
                    else None,
                    "derivation": None,
                }
            )
        return {"status": "ok", "applied": applied, "conflicts": [], "rejected": []}


def _read_cell(
    conn: sqlite3.Connection,
    ticker: str,
    concept: str,
    cell_id: str,
    metric_id: str,
    resolution_revision_id: str,
    bundle: ProvenanceBundle,
    cutoff: datetime,
) -> DcfStatementCell:
    initial = DcfStatementCell(
        concept=concept, canonical_metric_cell_id=cell_id, metric_id=metric_id
    )
    ontology = MetricOntology(conn)
    definition = ontology.metric_definition_as_known(metric_id, cutoff)
    binding = ontology.binding_as_known(bundle.observation.observation_id, cutoff)
    admitted = (
        ontology.metric_definition_as_known(metric_id, binding.recorded_at) if binding else None
    )
    semantics = {
        "lifecycle",
        "definition_text",
        "value_kind",
        "period_kind",
        "unit_family",
        "accounting_basis",
        "scope_constraints",
    }
    source, value = bundle.cell, bundle.observation
    reasons: list[str] = []
    expected_kind = "instant" if _MAPPING[concept][0] == "balance" else "duration"
    expected_family = "shares" if concept == "weighted_avg_shares_diluted" else "currency"
    if (
        definition is None
        or admitted is None
        or binding is None
        or binding.binding_status != "bound"
        or binding.canonical_metric_cell_id != cell_id
        or definition.lifecycle != "active"
        or definition.model_dump(include=semantics) != admitted.model_dump(include=semantics)
        or definition.value_kind != "numeric"
        or definition.period_kind != expected_kind
        or definition.unit_family != expected_family
        or definition.accounting_basis != source.accounting_basis
        or definition.scope_constraints.get("reporting_entity_id") != source.reporting_entity_id
        or definition.scope_constraints.get("consolidation_scope") != source.consolidation_scope
    ):
        reasons.append("active_metric_definition_or_binding_unavailable")
    if (
        value.decimal_value is None
        or bundle.evidence is None
        or value.observation_kind != "reported"
        or source.dimensions
        or source.scope_security_id is not None
        or source.fiscal_year is None
        or source.fiscal_period not in {"Q1", "Q2", "Q3", "Q4"}
        or source.period_end > cutoff
        or source.period_kind != expected_kind
    ):
        reasons.append("exact_reported_fiscal_coordinate_unavailable")
    if expected_family == "shares":
        if value.unit_key != "shares" or value.currency is not None:
            reasons.append("exact_share_unit_unavailable")
    elif value.currency is None or value.unit_key != value.currency:
        reasons.append("exact_currency_unit_unavailable")
    document = (
        None
        if bundle.evidence is None
        else conn.execute(
            "SELECT legacy.id,legacy.source_type,legacy.source_quality_tier,observation.source_url "
            "FROM evidence_document_versions document JOIN evidence_source_observations observation "
            "ON observation.observation_id=document.observation_id JOIN documents legacy "
            "ON legacy.id=document.legacy_document_id WHERE document.document_version_id=? "
            "AND UPPER(legacy.ticker)=? AND UPPER(document.ticker)=?",
            (bundle.evidence.document_version_id, ticker, ticker),
        ).fetchone()
    )
    if document is None or not primary_source_eligible(document[1], document[2]):
        reasons.append("primary_document_authority_unavailable")
    return initial.model_copy(
        update={
            "definition_revision_id": definition.metric_definition_revision_id
            if definition
            else None,
            "resolution_revision_id": resolution_revision_id,
            "provenance": bundle,
            "reason_codes": tuple(reasons),
            "source_document_id": int(document[0]) if document else None,
            "source_type": str(document[1]) if document else None,
            "source_tier": str(document[2]) if document else None,
            "source_url": str(document[3]) if document else None,
        }
    )


def read_dcf_statements(
    conn: sqlite3.Connection, ticker: str, *, as_of: datetime
) -> DcfStatementInputs:
    """Read approved normalized concepts via the canonical resolver and sealed fact boundary."""
    if as_of.tzinfo is None:
        raise ValueError("DCF statement cutoff must be timezone-aware")
    cutoff, ticker = as_of.astimezone(UTC), ticker.upper()
    original_factory = conn.row_factory
    owns_snapshot = not conn.in_transaction
    try:
        conn.row_factory = sqlite3.Row
        if owns_snapshot:
            conn.execute("BEGIN")
        rows = conn.execute(
            "SELECT DISTINCT binding.canonical_metric_cell_id,source.concept_name,target.metric_id "
            "FROM fact_cell_canonical_binding_revisions binding "
            "JOIN fact_cells_v2 source ON source.fact_cell_id=binding.fact_cell_id "
            "JOIN canonical_metric_cells target ON target.canonical_metric_cell_id=binding.canonical_metric_cell_id "
            "JOIN fact_observations_v2 observation ON observation.observation_id=binding.source_observation_id "
            "JOIN evidence_document_versions document ON document.document_version_id=observation.document_version_id "
            "WHERE document.ticker=? AND source.concept_namespace='urn:earnings-summary:legacy:financial' "
            "AND (source.fiscal_period IS NULL OR source.fiscal_period NOT IN ('FY','TTM')) "
            "AND binding.binding_status='bound' AND julianday(binding.recorded_at)<=julianday(?) "
            "AND julianday(binding.knowledge_at)<=julianday(?) ORDER BY source.concept_name,binding.canonical_metric_cell_id",
            (ticker, cutoff.isoformat(), cutoff.isoformat()),
        ).fetchall()
        plans: list[DcfStatementCell | _ReadPlan] = []
        observation_ids: list[str] = []
        resolver = CanonicalFactResolutionEngine(conn)
        for cell_id, concept, metric_id in rows:
            if str(concept) not in _MAPPING:
                continue
            try:
                resolution = resolver.as_known(str(cell_id), cutoff)
                if (
                    resolution is None
                    or resolution.status != "resolved"
                    or resolution.selected_observation_id is None
                ):
                    plans.append(
                        DcfStatementCell(
                            concept=str(concept),
                            canonical_metric_cell_id=str(cell_id),
                            metric_id=str(metric_id),
                            reason_codes=("canonical_resolution_unavailable",),
                        )
                    )
                    continue
                observation_id = resolution.selected_observation_id
                observation_ids.append(observation_id)
                plans.append(
                    (
                        str(concept),
                        str(cell_id),
                        str(metric_id),
                        resolution.canonical_resolution_revision_id,
                        observation_id,
                        ticker,
                    )
                )
            except (ValueError, RuntimeError, sqlite3.Error):
                plans.append(
                    DcfStatementCell(
                        concept=str(concept),
                        canonical_metric_cell_id=str(cell_id),
                        metric_id=str(metric_id),
                        reason_codes=("canonical_evidence_invalid",),
                    )
                )
        try:
            batch = FactReadModel(conn).provenance_bundles(tuple(observation_ids), cutoff=cutoff)
        except (ValueError, RuntimeError, sqlite3.Error):
            batch = ()
        reads = iter(batch)
        cells: list[DcfStatementCell] = []
        for plan in plans:
            if isinstance(plan, DcfStatementCell):
                cells.append(plan)
                continue
            concept, cell_id, metric_id, resolution_revision_id, observation_id, plan_ticker = plan
            result = next(reads, None)
            if result is None or result.observation_id != observation_id or result.bundle is None:
                cells.append(
                    DcfStatementCell(
                        concept=concept,
                        canonical_metric_cell_id=cell_id,
                        metric_id=metric_id,
                        reason_codes=("canonical_evidence_invalid",),
                    )
                )
                continue
            try:
                cells.append(
                    _read_cell(
                        conn,
                        plan_ticker,
                        concept,
                        cell_id,
                        metric_id,
                        resolution_revision_id,
                        result.bundle,
                        cutoff,
                    )
                )
            except (ValueError, RuntimeError, sqlite3.Error):
                cells.append(
                    DcfStatementCell(
                        concept=concept,
                        canonical_metric_cell_id=cell_id,
                        metric_id=metric_id,
                        reason_codes=("canonical_evidence_invalid",),
                    )
                )
        return DcfStatementInputs(ticker=ticker, as_of=cutoff, cells=tuple(cells))
    except sqlite3.Error:
        return DcfStatementInputs(
            ticker=ticker, as_of=cutoff, reason_codes=("canonical_statement_schema_unavailable",)
        )
    finally:
        if owns_snapshot:
            conn.rollback()
        conn.row_factory = original_factory
