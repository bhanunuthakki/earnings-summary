from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Literal

import pytest
from pydantic import ValidationError

import compute.kpi_resolver as kpi_resolver_module
import pipeline.kpi_source_review as source_review_module
from compute.kpi_resolver import (
    KpiRevisionSeriesExclusionReason,
    KpiRevisionSeriesStatus,
    resolve_revision_aware_kpi_series,
)
from models.facts import Currency, FactLocator, Unit
from pipeline.kpi_definition_revisions import (
    IssuerKpiDefinitionRevision,
    KpiCurrencyDisposition,
    KpiDefinitionComparabilityDisposition,
    KpiDefinitionComparabilityRevision,
    KpiDefinitionLifecycle,
    KpiDefinitionPeriodKind,
    KpiDefinitionRelationKind,
    KpiDefinitionStatus,
    KpiDefinitionTextStatus,
    KpiStockFlowBehavior,
    KpiUnitFamily,
    current_kpi_definition_revision,
    kpi_definition_revision_as_known,
    persist_kpi_definition_comparability_revision,
    persist_kpi_definition_revision,
)
from pipeline.kpi_semantics import (
    KpiAccountingBasis,
    KpiConsolidationScope,
    KpiPeriodRole,
    KpiPublicationLane,
    KpiSemanticContext,
    KpiSemanticStatus,
    KpiUnitScale,
    current_kpi_semantic_context,
    persist_kpi_semantic_context,
)
from pipeline.kpi_source_review import insert_source_reviewed_kpi_supersession

NOW = datetime(2026, 9, 6, 18, tzinfo=UTC)
EFFECTIVE = datetime(2024, 1, 1, tzinfo=UTC)


def _database(path: Path | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:" if path is None else path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    if path is not None:
        assert str(conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]).lower() == "wal"
    conn.executescript(
        """
        CREATE TABLE kpi_definitions(
            id INTEGER PRIMARY KEY,ticker TEXT NOT NULL,name TEXT NOT NULL,unit TEXT NOT NULL
        );
        CREATE TABLE reporting_entities(
            reporting_entity_id TEXT PRIMARY KEY,issuer_id TEXT NOT NULL
        );
        CREATE TABLE securities(security_id TEXT PRIMARY KEY,issuer_id TEXT NOT NULL);
        CREATE TABLE evidence_source_observations(
            observation_id TEXT PRIMARY KEY,retrieved_at TEXT NOT NULL
        );
        CREATE TABLE evidence_document_versions(
            document_version_id TEXT PRIMARY KEY,observation_id TEXT NOT NULL,
            issuer_id TEXT NOT NULL,legacy_document_id INTEGER,version_sequence INTEGER NOT NULL,
            recorded_at TEXT NOT NULL
        );
        CREATE TABLE evidence_extraction_runs(
            extraction_run_id TEXT PRIMARY KEY,document_version_id TEXT NOT NULL,outcome TEXT NOT NULL
        );
        CREATE TABLE evidence_nodes(
            node_id TEXT PRIMARY KEY,extraction_run_id TEXT NOT NULL,recorded_at TEXT NOT NULL,
            locator_json TEXT NOT NULL,locator_sha256 TEXT NOT NULL
        );
        CREATE VIEW v_legacy_document_evidence_bindings_current AS
        SELECT document.legacy_document_id,document.document_version_id,node.node_id AS evidence_node_id
        FROM evidence_document_versions document
        JOIN evidence_extraction_runs run
          ON run.document_version_id=document.document_version_id
        JOIN evidence_nodes node ON node.extraction_run_id=run.extraction_run_id;
        CREATE TABLE kpi_definition_revisions(
            kpi_definition_revision_id TEXT PRIMARY KEY,
            idempotency_key TEXT NOT NULL UNIQUE,
            kpi_definition_id INTEGER NOT NULL,
            reporting_entity_id TEXT NOT NULL,
            scope_security_id TEXT,
            revision INTEGER NOT NULL,
            supersedes_definition_revision_id TEXT UNIQUE,
            status TEXT NOT NULL,lifecycle TEXT NOT NULL,
            reported_label TEXT NOT NULL,reported_definition_text TEXT,
            definition_text_status TEXT NOT NULL,period_kind TEXT NOT NULL,
            stock_flow_behavior TEXT NOT NULL,unit_family TEXT NOT NULL,
            unit_key TEXT NOT NULL,unit_scale TEXT NOT NULL,
            currency_disposition TEXT NOT NULL,currency TEXT,
            accounting_basis TEXT NOT NULL,consolidation_scope TEXT NOT NULL,
            dimensions_json TEXT NOT NULL,source_document_version_id TEXT NOT NULL,
            source_evidence_node_id TEXT NOT NULL,source_locator_json TEXT NOT NULL,
            source_locator_sha256 TEXT NOT NULL,reason_code TEXT,reviewed_by TEXT NOT NULL,
            commitment_json TEXT NOT NULL,commitment_sha256 TEXT NOT NULL,
            effective_at TEXT NOT NULL,knowledge_at TEXT NOT NULL,recorded_at TEXT NOT NULL,
            UNIQUE(kpi_definition_id,revision)
        );
        CREATE TABLE kpi_definition_comparability_revisions(
            comparability_revision_id TEXT PRIMARY KEY,
            idempotency_key TEXT NOT NULL UNIQUE,
            predecessor_definition_revision_id TEXT NOT NULL,
            successor_definition_revision_id TEXT NOT NULL,
            revision INTEGER NOT NULL,
            supersedes_comparability_revision_id TEXT UNIQUE,
            relation_kind TEXT NOT NULL,disposition TEXT NOT NULL,
            reason_code TEXT NOT NULL,reviewed_by TEXT NOT NULL,
            source_document_version_id TEXT NOT NULL,source_evidence_node_id TEXT NOT NULL,
            source_locator_json TEXT NOT NULL,source_locator_sha256 TEXT NOT NULL,
            commitment_json TEXT NOT NULL,commitment_sha256 TEXT NOT NULL,
            effective_at TEXT NOT NULL,knowledge_at TEXT NOT NULL,recorded_at TEXT NOT NULL,
            UNIQUE(predecessor_definition_revision_id,successor_definition_revision_id,revision)
        );
        CREATE TABLE kpi_facts(
            id INTEGER PRIMARY KEY AUTOINCREMENT,ticker TEXT NOT NULL,period_end TEXT NOT NULL,
            fiscal_period_type TEXT NOT NULL,kpi_definition_id INTEGER NOT NULL,value TEXT NOT NULL,
            unit TEXT NOT NULL,currency TEXT,source_doc_id INTEGER NOT NULL,
            confidence REAL,extracted_by TEXT,supersedes_id INTEGER,locator TEXT,
            source_excerpt TEXT,computed_from TEXT
        );
        CREATE VIEW v_kpi_facts_resolved_current AS
        SELECT fact.* FROM kpi_facts fact
        WHERE NOT EXISTS (SELECT 1 FROM kpi_facts successor WHERE successor.supersedes_id=fact.id);
        CREATE TABLE reported_observations(
            observation_id TEXT PRIMARY KEY,ticker TEXT NOT NULL,concept_key TEXT NOT NULL,
            period_end TEXT NOT NULL,fiscal_period_type TEXT NOT NULL,numeric_value TEXT NOT NULL,
            currency TEXT,unit TEXT NOT NULL,available_at TEXT NOT NULL,recorded_at TEXT NOT NULL
        );
        CREATE TABLE fact_observation_revisions(
            fact_table TEXT NOT NULL,fact_row_id INTEGER NOT NULL,fact_revision INTEGER NOT NULL,
            observation_id TEXT NOT NULL,logical_key TEXT NOT NULL,source_document_id INTEGER NOT NULL,
            locator_json TEXT,captured_at TEXT NOT NULL
        );
        CREATE TABLE observation_resolution_revisions(
            resolution_id TEXT PRIMARY KEY,logical_key TEXT NOT NULL,revision INTEGER NOT NULL,
            selected_observation_id TEXT NOT NULL,knowledge_cutoff TEXT NOT NULL,
            effective_at TEXT NOT NULL,recorded_at TEXT NOT NULL
        );
        CREATE TABLE fact_resolution_outcomes(
            resolution_id TEXT PRIMARY KEY,resolution_status TEXT NOT NULL,recorded_at TEXT NOT NULL
        );
        CREATE TABLE kpi_fact_semantic_contexts(
            id INTEGER PRIMARY KEY AUTOINCREMENT,kpi_fact_id INTEGER NOT NULL,
            revision INTEGER NOT NULL,supersedes_context_id INTEGER UNIQUE,
            metric_name_as_reported TEXT NOT NULL,reported_period_end TEXT,
            period_role TEXT NOT NULL,publication_lane TEXT NOT NULL,
            accounting_basis TEXT NOT NULL,consolidation_scope TEXT NOT NULL,
            dimensions_json TEXT NOT NULL,unit_scale TEXT NOT NULL,
            source_row_label TEXT,source_column_header TEXT,source_value_text TEXT,
            status TEXT NOT NULL,reason_code TEXT,reviewed_by TEXT NOT NULL,
            knowledge_at TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT '2026-09-06T18:00:00Z',
            kpi_definition_revision_id TEXT,
            UNIQUE(kpi_fact_id,revision)
        );
        """
    )
    conn.execute("INSERT INTO kpi_definitions VALUES (1,'NU','Monthly ARPAC','actual')")
    conn.execute("INSERT INTO reporting_entities VALUES ('entity-nu','issuer-nu')")
    conn.execute(
        "INSERT INTO evidence_source_observations VALUES ('observation-1',?)",
        (datetime(2023, 12, 31, tzinfo=UTC).isoformat(),),
    )
    conn.execute(
        "INSERT INTO evidence_document_versions VALUES "
        "('document-v1','observation-1','issuer-nu',10,1,?)",
        (datetime(2024, 1, 2, tzinfo=UTC).isoformat(),),
    )
    conn.execute("INSERT INTO evidence_extraction_runs VALUES ('run-1','document-v1','succeeded')")
    conn.execute(
        "INSERT INTO evidence_nodes VALUES ('node-1','run-1',?,'{\"page\":7}',?)",
        (
            datetime(2024, 1, 2, tzinfo=UTC).isoformat(),
            "7690fcf16c9c3db8a58cec6e5c9ac059b698818d3850be1d712f6fd12eb7f922",  # pragma: allowlist secret -- synthetic public locator hash fixture
        ),
    )
    return conn


def _definition(**changes: object) -> IssuerKpiDefinitionRevision:
    values: dict[str, object] = {
        "kpi_definition_revision_id": "definition-r1",
        "idempotency_key": "definition-key-r1",
        "kpi_definition_id": 1,
        "reporting_entity_id": "entity-nu",
        "revision": 1,
        "status": KpiDefinitionStatus.ADMITTED,
        "lifecycle": KpiDefinitionLifecycle.ACTIVE,
        "reported_label": "Monthly ARPAC",
        "reported_definition_text": "Average revenue per active customer per month.",
        "definition_text_status": KpiDefinitionTextStatus.VERBATIM,
        "period_kind": KpiDefinitionPeriodKind.DURATION,
        "stock_flow_behavior": KpiStockFlowBehavior.FLOW,
        "unit_family": KpiUnitFamily.CURRENCY,
        "unit_key": Unit.ACTUAL,
        "unit_scale": KpiUnitScale.NONE,
        "currency_disposition": KpiCurrencyDisposition.EXPLICIT,
        "currency": Currency.USD,
        "accounting_basis": KpiAccountingBasis.MANAGEMENT,
        "consolidation_scope": KpiConsolidationScope.CONSOLIDATED,
        "dimensions": {},
        "source_document_version_id": "document-v1",
        "source_evidence_node_id": "node-1",
        "source_locator": {"page": 7},
        "reviewed_by": "owner",
        "effective_at": EFFECTIVE,
        "knowledge_at": NOW,
        "recorded_at": NOW,
    }
    values.update(changes)
    return IssuerKpiDefinitionRevision.model_validate(values)


def _relation(
    predecessor: str,
    successor: str,
    **changes: object,
) -> KpiDefinitionComparabilityRevision:
    values: dict[str, object] = {
        "comparability_revision_id": f"relation-{predecessor}-{successor}",
        "idempotency_key": f"relation-key-{predecessor}-{successor}",
        "predecessor_definition_revision_id": predecessor,
        "successor_definition_revision_id": successor,
        "revision": 1,
        "relation_kind": KpiDefinitionRelationKind.RENAMED,
        "disposition": KpiDefinitionComparabilityDisposition.CONTINUOUS,
        "reason_code": "issuer_disclosed_rename",
        "reviewed_by": "owner",
        "source_document_version_id": "document-v1",
        "source_evidence_node_id": "node-1",
        "source_locator": {"page": 7},
        "effective_at": EFFECTIVE,
        "knowledge_at": NOW,
        "recorded_at": NOW,
    }
    values.update(changes)
    return KpiDefinitionComparabilityRevision.model_validate(values)


def _context(label: str = "Monthly ARPAC") -> KpiSemanticContext:
    return KpiSemanticContext(
        metric_name_as_reported=label,
        reported_period_end=datetime(2024, 12, 31, tzinfo=UTC).date(),
        period_role=KpiPeriodRole.CURRENT,
        publication_lane=KpiPublicationLane.CURRENT_ACTUAL,
        accounting_basis=KpiAccountingBasis.MANAGEMENT,
        consolidation_scope=KpiConsolidationScope.CONSOLIDATED,
        dimensions={},
        unit_scale=KpiUnitScale.NONE,
        status=KpiSemanticStatus.ADMITTED,
    )


def _fact(
    conn: sqlite3.Connection,
    definition_id: int = 1,
    *,
    logical_key: str | None = None,
    known_at: datetime = NOW,
    value: str = "12.5",
    supersedes_id: int | None = None,
) -> int:
    cursor = conn.execute(
        "INSERT INTO kpi_facts "
        "(ticker,period_end,fiscal_period_type,kpi_definition_id,value,unit,currency,"
        "source_doc_id,confidence,locator,source_excerpt) "
        "VALUES ('NU','2024-12-31','Q4',?,'12.5','actual','USD',10,1.0,'{\"pdf_page\":7}',"
        "'Monthly ARPAC was $12.5')",
        (definition_id,),
    )
    assert cursor.lastrowid is not None
    fact_id = int(cursor.lastrowid)
    if value != "12.5" or supersedes_id is not None:
        conn.execute(
            "UPDATE kpi_facts SET value=?,supersedes_id=? WHERE id=?",
            (value, supersedes_id, fact_id),
        )
    key = logical_key or f"kpi-logical-{fact_id}"
    current = conn.execute(
        "SELECT resolution_id,revision FROM observation_resolution_revisions "
        "WHERE logical_key=? ORDER BY revision DESC LIMIT 1",
        (key,),
    ).fetchone()
    revision = 1 if current is None else int(current[1]) + 1
    observation_id = f"kpi-observation-{fact_id}"
    resolution_id = f"kpi-resolution-{key}-{revision}"
    clock = known_at.isoformat()
    conn.execute(
        "INSERT INTO reported_observations VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            observation_id,
            "NU",
            f"kpi_definition:{definition_id}",
            "2024-12-31",
            "quarter",
            value,
            None,
            "actual",
            clock,
            clock,
        ),
    )
    conn.execute(
        "INSERT INTO fact_observation_revisions VALUES "
        "('kpi_facts',?,1,?,?,10,'{\"pdf_page\":7}',?)",
        (fact_id, observation_id, key, clock),
    )
    conn.execute(
        "INSERT INTO observation_resolution_revisions VALUES (?,?,?,?,?,?,?)",
        (resolution_id, key, revision, observation_id, clock, clock, clock),
    )
    conn.execute(
        "INSERT INTO fact_resolution_outcomes VALUES (?,'resolved',?)",
        (resolution_id, clock),
    )
    return fact_id


def _no_effect(*_: object, **__: object) -> None:
    return None


def test_definition_replay_is_exact_and_sibling_branch_is_rejected() -> None:
    conn = _database()
    first = persist_kpi_definition_revision(conn, _definition())
    replay = persist_kpi_definition_revision(conn, _definition())
    assert replay == first
    assert conn.execute("SELECT COUNT(*) FROM kpi_definition_revisions").fetchone()[0] == 1

    with pytest.raises(ValueError, match="idempotency key conflicts"):
        persist_kpi_definition_revision(
            conn,
            _definition(reported_definition_text="Conflicting replay."),
        )

    successor = _definition(
        kpi_definition_revision_id="definition-r2",
        idempotency_key="definition-key-r2",
        revision=2,
        supersedes_definition_revision_id="definition-r1",
        knowledge_at=NOW.replace(hour=19),
        recorded_at=NOW.replace(hour=19),
    )
    persist_kpi_definition_revision(conn, successor)
    with pytest.raises(ValueError, match="exact current head"):
        persist_kpi_definition_revision(
            conn,
            _definition(
                kpi_definition_revision_id="definition-r2-sibling",
                idempotency_key="definition-key-r2-sibling",
                revision=2,
                supersedes_definition_revision_id="definition-r1",
                knowledge_at=NOW.replace(hour=20),
                recorded_at=NOW.replace(hour=20),
            ),
        )


def test_effective_selection_handles_future_revision_and_later_historical_correction() -> None:
    conn = _database()
    january = datetime(2025, 1, 1, tzinfo=UTC)
    february = datetime(2025, 2, 1, tzinfo=UTC)
    march = datetime(2025, 3, 1, tzinfo=UTC)
    july = datetime(2025, 7, 1, tzinfo=UTC)
    persist_kpi_definition_revision(
        conn,
        _definition(effective_at=january, knowledge_at=january, recorded_at=january),
    )
    persist_kpi_definition_revision(
        conn,
        _definition(
            kpi_definition_revision_id="definition-r2",
            idempotency_key="definition-key-r2",
            revision=2,
            supersedes_definition_revision_id="definition-r1",
            reported_label="Monthly ARPAC after July",
            effective_at=july,
            knowledge_at=february,
            recorded_at=february,
        ),
    )
    correction = persist_kpi_definition_revision(
        conn,
        _definition(
            kpi_definition_revision_id="definition-r3",
            idempotency_key="definition-key-r3",
            revision=3,
            supersedes_definition_revision_id="definition-r2",
            reported_definition_text="Corrected historical issuer definition.",
            effective_at=january,
            knowledge_at=march,
            recorded_at=march,
        ),
    )

    may_view = kpi_definition_revision_as_known(
        conn,
        kpi_definition_id=1,
        effective_at=datetime(2025, 5, 1, tzinfo=UTC),
        known_at=datetime(2025, 8, 1, tzinfo=UTC),
    )
    august_view = kpi_definition_revision_as_known(
        conn,
        kpi_definition_id=1,
        effective_at=datetime(2025, 8, 1, tzinfo=UTC),
        known_at=datetime(2025, 8, 1, tzinfo=UTC),
    )
    current_head = current_kpi_definition_revision(conn, kpi_definition_id=1)
    assert may_view == correction
    assert august_view is not None and august_view.kpi_definition_revision_id == "definition-r2"
    assert current_head == correction


def test_discontinued_effective_revision_does_not_fall_back_to_active_history() -> None:
    conn = _database()
    persist_kpi_definition_revision(conn, _definition())
    persist_kpi_definition_revision(
        conn,
        _definition(
            kpi_definition_revision_id="definition-r2",
            idempotency_key="definition-key-r2",
            revision=2,
            supersedes_definition_revision_id="definition-r1",
            lifecycle=KpiDefinitionLifecycle.DISCONTINUED,
            effective_at=datetime(2025, 1, 1, tzinfo=UTC),
            knowledge_at=NOW.replace(hour=19),
            recorded_at=NOW.replace(hour=19),
        ),
    )
    result = resolve_revision_aware_kpi_series(
        conn,
        kpi_definition_id=1,
        effective_at=datetime(2025, 2, 1, tzinfo=UTC),
        known_at=NOW.replace(hour=20),
    )
    assert result.status is KpiRevisionSeriesStatus.DISCONTINUED
    assert result.anchor_definition_revision_id == "definition-r2"


def test_currency_contract_quarantines_unknown_and_requires_a_break() -> None:
    with pytest.raises(ValidationError, match="explicit currency"):
        _definition(currency_disposition=KpiCurrencyDisposition.UNKNOWN, currency=None)
    noncurrency = _definition(
        unit_key=Unit.PERCENT,
        unit_family=KpiUnitFamily.PERCENTAGE,
        currency_disposition=KpiCurrencyDisposition.NOT_APPLICABLE,
        currency=None,
    )
    assert noncurrency.currency_disposition is KpiCurrencyDisposition.NOT_APPLICABLE

    conn = _database()
    conn.execute("INSERT INTO kpi_definitions VALUES (2,'NU','Monthly ARPAC BRL','actual')")
    usd = persist_kpi_definition_revision(conn, _definition())
    brl = persist_kpi_definition_revision(
        conn,
        _definition(
            kpi_definition_revision_id="definition-brl-r1",
            idempotency_key="definition-brl-key-r1",
            kpi_definition_id=2,
            currency=Currency.BRL,
        ),
    )
    with pytest.raises(ValueError, match="currency change"):
        persist_kpi_definition_comparability_revision(
            conn,
            _relation(usd.kpi_definition_revision_id, brl.kpi_definition_revision_id),
        )


def test_continuous_comparability_rejects_cross_family_and_unhandled_scale_changes() -> None:
    conn = _database()
    conn.executemany(
        "INSERT INTO kpi_definitions VALUES (?,?,?,?)",
        [
            (2, "NU", "Customer count", "count"),
            (3, "NU", "Customer count in thousands", "count"),
        ],
    )
    monetary = persist_kpi_definition_revision(conn, _definition())
    count = persist_kpi_definition_revision(
        conn,
        _definition(
            kpi_definition_revision_id="definition-count-r1",
            idempotency_key="definition-count-key-r1",
            kpi_definition_id=2,
            reported_label="Customer count",
            unit_family=KpiUnitFamily.COUNT,
            unit_key=Unit.COUNT,
            currency_disposition=KpiCurrencyDisposition.NOT_APPLICABLE,
            currency=None,
        ),
    )
    scaled_count = persist_kpi_definition_revision(
        conn,
        _definition(
            kpi_definition_revision_id="definition-count-thousands-r1",
            idempotency_key="definition-count-thousands-key-r1",
            kpi_definition_id=3,
            reported_label="Customer count in thousands",
            unit_family=KpiUnitFamily.COUNT,
            unit_key=Unit.COUNT,
            unit_scale=KpiUnitScale.THOUSANDS,
            currency_disposition=KpiCurrencyDisposition.NOT_APPLICABLE,
            currency=None,
        ),
    )

    with pytest.raises(ValueError, match="unit or presentation-scale change"):
        persist_kpi_definition_comparability_revision(
            conn,
            _relation(monetary.kpi_definition_revision_id, count.kpi_definition_revision_id),
        )
    with pytest.raises(ValueError, match="unit or presentation-scale change"):
        persist_kpi_definition_comparability_revision(
            conn,
            _relation(
                count.kpi_definition_revision_id,
                scaled_count.kpi_definition_revision_id,
            ),
        )

    explicit_break = persist_kpi_definition_comparability_revision(
        conn,
        _relation(
            count.kpi_definition_revision_id,
            scaled_count.kpi_definition_revision_id,
            comparability_revision_id="relation-count-scale-break",
            idempotency_key="relation-count-scale-break",
            disposition=KpiDefinitionComparabilityDisposition.COMPARABLE_WITH_BREAK,
        ),
    )
    assert explicit_break.disposition is KpiDefinitionComparabilityDisposition.COMPARABLE_WITH_BREAK


def test_continuous_comparability_requires_equal_semantic_axes() -> None:
    conn = _database()
    axis_changes: tuple[dict[str, object], ...] = (
        {
            "period_kind": KpiDefinitionPeriodKind.INSTANT,
            "stock_flow_behavior": KpiStockFlowBehavior.STOCK,
        },
        {"stock_flow_behavior": KpiStockFlowBehavior.OTHER},
        {"accounting_basis": KpiAccountingBasis.GAAP},
        {"consolidation_scope": KpiConsolidationScope.OTHER},
        {"dimensions": {"customer_plan": "premium"}},
    )
    conn.executemany(
        "INSERT INTO kpi_definitions VALUES (?,?,?,?)",
        [
            (index, "NU", f"ARPAC semantic variant {index}", "actual")
            for index in range(2, 2 + len(axis_changes))
        ],
    )
    baseline = persist_kpi_definition_revision(conn, _definition())
    for index, changes in enumerate(axis_changes, start=2):
        changed = persist_kpi_definition_revision(
            conn,
            _definition(
                kpi_definition_revision_id=f"definition-axis-{index}",
                idempotency_key=f"definition-axis-key-{index}",
                kpi_definition_id=index,
                reported_label=f"ARPAC semantic variant {index}",
                **changes,
            ),
        )
        with pytest.raises(ValueError, match="semantic-axis change"):
            persist_kpi_definition_comparability_revision(
                conn,
                _relation(
                    baseline.kpi_definition_revision_id,
                    changed.kpi_definition_revision_id,
                ),
            )


def test_redefined_recast_and_restated_relations_cannot_be_continuous() -> None:
    for relation_kind in (
        KpiDefinitionRelationKind.REDEFINED,
        KpiDefinitionRelationKind.RECAST,
        KpiDefinitionRelationKind.RESTATED,
    ):
        with pytest.raises(
            ValidationError,
            match="only same-definition or renamed relations can be continuous",
        ):
            _relation("definition-r1", "definition-r2", relation_kind=relation_kind)


def test_semantic_binding_requires_exact_effective_definition_as_known() -> None:
    conn = _database()
    first = persist_kpi_definition_revision(conn, _definition())
    later = NOW + timedelta(hours=1)
    second = persist_kpi_definition_revision(
        conn,
        _definition(
            kpi_definition_revision_id="definition-r2",
            idempotency_key="definition-key-r2",
            revision=2,
            supersedes_definition_revision_id=first.kpi_definition_revision_id,
            effective_at=datetime(2024, 6, 1, tzinfo=UTC),
            knowledge_at=later,
            recorded_at=later,
        ),
    )
    fact_id = _fact(conn, known_at=later)
    context_at = later + timedelta(hours=1)

    with pytest.raises(ValueError, match="exact effective definition revision as known"):
        persist_kpi_semantic_context(
            conn,
            kpi_fact_id=fact_id,
            context=_context(),
            reviewed_by="owner",
            knowledge_at=context_at,
            kpi_definition_revision_id=first.kpi_definition_revision_id,
        )
    bound_id = persist_kpi_semantic_context(
        conn,
        kpi_fact_id=fact_id,
        context=_context(),
        reviewed_by="owner",
        knowledge_at=context_at,
        kpi_definition_revision_id=second.kpi_definition_revision_id,
    )
    assert bound_id is not None


def test_semantic_binding_is_exact_and_unbound_rows_stay_visible_in_shadow_counts() -> None:
    conn = _database()
    definition = persist_kpi_definition_revision(conn, _definition())
    bound_fact = _fact(conn)
    unbound_fact = _fact(conn)
    persist_kpi_semantic_context(
        conn,
        kpi_fact_id=bound_fact,
        context=_context(),
        reviewed_by="owner",
        knowledge_at=NOW,
        kpi_definition_revision_id=definition.kpi_definition_revision_id,
    )
    persist_kpi_semantic_context(
        conn,
        kpi_fact_id=unbound_fact,
        context=_context(),
        reviewed_by="owner",
        knowledge_at=NOW,
        kpi_definition_revision_id=None,
    )
    current = current_kpi_semantic_context(conn, kpi_fact_id=bound_fact)
    assert current is not None
    assert current.kpi_definition_revision_id == definition.kpi_definition_revision_id

    result = resolve_revision_aware_kpi_series(
        conn,
        kpi_definition_id=1,
        effective_at=NOW,
        known_at=NOW,
    )
    assert result.status is KpiRevisionSeriesStatus.ELIGIBLE
    assert result.eligible_fact_ids == (bound_fact,)
    assert {exclusion.reason: exclusion.count for exclusion in result.exclusions} == {
        KpiRevisionSeriesExclusionReason.LEGACY_UNBOUND: 1
    }

    with pytest.raises(ValueError, match="does not match the semantic context"):
        persist_kpi_semantic_context(
            conn,
            kpi_fact_id=_fact(conn),
            context=_context("Similar but not exact"),
            reviewed_by="owner",
            knowledge_at=NOW,
            kpi_definition_revision_id=definition.kpi_definition_revision_id,
        )


def test_semantic_binding_and_shadow_heads_respect_recorded_and_knowledge_cutoffs() -> None:
    conn = _database()
    later = NOW.replace(day=7)
    delayed = persist_kpi_definition_revision(
        conn,
        _definition(recorded_at=later),
    )
    with pytest.raises(ValueError, match="predates the recorded definition"):
        persist_kpi_semantic_context(
            conn,
            kpi_fact_id=_fact(conn),
            context=_context(),
            reviewed_by="owner",
            knowledge_at=NOW,
            kpi_definition_revision_id=delayed.kpi_definition_revision_id,
        )

    conn = _database()
    definition = persist_kpi_definition_revision(conn, _definition())
    fact_id = _fact(conn)
    persist_kpi_semantic_context(
        conn,
        kpi_fact_id=fact_id,
        context=_context(),
        reviewed_by="owner",
        knowledge_at=NOW,
        kpi_definition_revision_id=definition.kpi_definition_revision_id,
    )
    persist_kpi_semantic_context(
        conn,
        kpi_fact_id=fact_id,
        context=_context(),
        reviewed_by="owner",
        knowledge_at=later,
        kpi_definition_revision_id=None,
    )
    historical = resolve_revision_aware_kpi_series(
        conn,
        kpi_definition_id=1,
        effective_at=NOW,
        known_at=NOW,
    )
    current = resolve_revision_aware_kpi_series(
        conn,
        kpi_definition_id=1,
        effective_at=later,
        known_at=later,
    )
    assert historical.eligible_fact_ids == (fact_id,)
    assert current.eligible_fact_ids == ()
    assert current.exclusions[0].reason is KpiRevisionSeriesExclusionReason.LEGACY_UNBOUND


def test_semantic_heads_require_knowledge_and_created_clocks() -> None:
    conn = _database()
    definition = persist_kpi_definition_revision(conn, _definition())
    late_candidate_fact = _fact(conn)
    stable_fact = _fact(conn)
    later = NOW + timedelta(days=1)
    candidate_context = _context()
    assert candidate_context.reported_period_end is not None
    conn.execute(
        "INSERT INTO kpi_fact_semantic_contexts "
        "(kpi_fact_id,revision,metric_name_as_reported,reported_period_end,period_role,"
        "publication_lane,accounting_basis,consolidation_scope,dimensions_json,unit_scale,"
        "status,reviewed_by,knowledge_at,created_at,kpi_definition_revision_id) "
        "VALUES (?,1,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            late_candidate_fact,
            candidate_context.metric_name_as_reported,
            candidate_context.reported_period_end.isoformat(),
            candidate_context.period_role.value,
            candidate_context.publication_lane.value,
            candidate_context.accounting_basis.value,
            candidate_context.consolidation_scope.value,
            "{}",
            candidate_context.unit_scale.value,
            candidate_context.status.value,
            "owner",
            NOW.isoformat(),
            later.isoformat(),
            definition.kpi_definition_revision_id,
        ),
    )
    stable_context_id = persist_kpi_semantic_context(
        conn,
        kpi_fact_id=stable_fact,
        context=_context(),
        reviewed_by="owner",
        knowledge_at=NOW,
        kpi_definition_revision_id=definition.kpi_definition_revision_id,
    )
    conn.execute(
        "INSERT INTO kpi_fact_semantic_contexts "
        "(kpi_fact_id,revision,supersedes_context_id,metric_name_as_reported,"
        "reported_period_end,period_role,publication_lane,accounting_basis,"
        "consolidation_scope,dimensions_json,unit_scale,status,reviewed_by,knowledge_at,"
        "created_at,kpi_definition_revision_id) "
        "VALUES (?,2,?,'Monthly ARPAC','2024-12-31','current','current_actual',"
        "'management','consolidated','{}','none','admitted','owner',?,?,NULL)",
        (stable_fact, stable_context_id, NOW.isoformat(), later.isoformat()),
    )

    historical = resolve_revision_aware_kpi_series(
        conn,
        kpi_definition_id=1,
        effective_at=NOW,
        known_at=NOW,
    )
    after_recording = resolve_revision_aware_kpi_series(
        conn,
        kpi_definition_id=1,
        effective_at=later,
        known_at=later,
    )
    assert historical.eligible_fact_ids == (stable_fact,)
    assert after_recording.eligible_fact_ids == (late_candidate_fact,)
    assert {exclusion.reason: exclusion.count for exclusion in after_recording.exclusions} == {
        KpiRevisionSeriesExclusionReason.LEGACY_UNBOUND: 1
    }


def test_shadow_resolver_reconstructs_fact_resolution_as_known() -> None:
    conn = _database()
    definition = persist_kpi_definition_revision(conn, _definition())
    logical_key = "monthly-arpac-2024-q4"
    earlier_fact = _fact(conn, logical_key=logical_key, known_at=NOW, value="12.5")
    persist_kpi_semantic_context(
        conn,
        kpi_fact_id=earlier_fact,
        context=_context(),
        reviewed_by="owner",
        knowledge_at=NOW,
        kpi_definition_revision_id=definition.kpi_definition_revision_id,
    )

    later = NOW + timedelta(days=1)
    corrected_fact = _fact(
        conn,
        logical_key=logical_key,
        known_at=later,
        value="13.0",
        supersedes_id=earlier_fact,
    )
    persist_kpi_semantic_context(
        conn,
        kpi_fact_id=corrected_fact,
        context=_context(),
        reviewed_by="owner",
        knowledge_at=later,
        kpi_definition_revision_id=definition.kpi_definition_revision_id,
    )

    historical = resolve_revision_aware_kpi_series(
        conn,
        kpi_definition_id=1,
        effective_at=NOW,
        known_at=NOW,
    )
    current = resolve_revision_aware_kpi_series(
        conn,
        kpi_definition_id=1,
        effective_at=later,
        known_at=later,
    )
    assert historical.eligible_fact_ids == (earlier_fact,)
    assert current.eligible_fact_ids == (corrected_fact,)


def test_shadow_resolver_rejects_late_fact_link_and_mutable_row_drift() -> None:
    conn = _database()
    definition = persist_kpi_definition_revision(conn, _definition())
    fact_id = _fact(conn)
    persist_kpi_semantic_context(
        conn,
        kpi_fact_id=fact_id,
        context=_context(),
        reviewed_by="owner",
        knowledge_at=NOW,
        kpi_definition_revision_id=definition.kpi_definition_revision_id,
    )
    later = NOW + timedelta(days=1)
    conn.execute(
        "UPDATE fact_observation_revisions SET captured_at=? WHERE fact_row_id=?",
        (later.isoformat(), fact_id),
    )
    before_capture = resolve_revision_aware_kpi_series(
        conn,
        kpi_definition_id=1,
        effective_at=NOW,
        known_at=NOW,
    )
    assert before_capture.eligible_fact_ids == ()

    conn.execute(
        "UPDATE fact_observation_revisions SET captured_at=? WHERE fact_row_id=?",
        (NOW.isoformat(), fact_id),
    )
    conn.execute("UPDATE kpi_facts SET value='99.0' WHERE id=?", (fact_id,))
    drifted = resolve_revision_aware_kpi_series(
        conn,
        kpi_definition_id=1,
        effective_at=NOW,
        known_at=NOW,
    )
    assert drifted.status is KpiRevisionSeriesStatus.HISTORICAL_FACT_AUTHORITY_UNAVAILABLE
    assert drifted.eligible_fact_ids == ()


def test_shadow_resolver_holds_one_wal_snapshot_across_fact_proof_and_use(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "kpi-shadow-snapshot.db"
    reader = _database(path)
    definition = persist_kpi_definition_revision(reader, _definition())
    fact_id = _fact(reader)
    persist_kpi_semantic_context(
        reader,
        kpi_fact_id=fact_id,
        context=_context(),
        reviewed_by="owner",
        knowledge_at=NOW,
        kpi_definition_revision_id=definition.kpi_definition_revision_id,
    )
    reader.commit()
    writer = sqlite3.connect(path)
    writer.row_factory = sqlite3.Row
    original = kpi_resolver_module.canonical_fact_row_ids_as_known

    def mutate_after_fact_proof(
        conn: sqlite3.Connection,
        *,
        fact_table: Literal["financial_facts", "kpi_facts"],
        effective_at: datetime,
        known_at: datetime,
        concept_keys: Sequence[str] = (),
    ) -> tuple[int, ...]:
        ids = original(
            conn,
            fact_table=fact_table,
            effective_at=effective_at,
            known_at=known_at,
            concept_keys=concept_keys,
        )
        writer.execute("UPDATE kpi_facts SET value='99.0' WHERE id=?", (fact_id,))
        writer.commit()
        return ids

    monkeypatch.setattr(
        kpi_resolver_module,
        "canonical_fact_row_ids_as_known",
        mutate_after_fact_proof,
    )
    result = resolve_revision_aware_kpi_series(
        reader,
        kpi_definition_id=1,
        effective_at=NOW,
        known_at=NOW,
    )

    assert result.eligible_fact_ids == (fact_id,)
    assert not reader.in_transaction
    assert (
        str(reader.execute("SELECT value FROM kpi_facts WHERE id=?", (fact_id,)).fetchone()[0])
        == "99.0"
    )
    writer.close()
    reader.close()


def test_shadow_resolver_does_not_close_a_caller_owned_transaction() -> None:
    conn = _database()
    _ = persist_kpi_definition_revision(conn, _definition())
    conn.commit()
    conn.execute("BEGIN")

    _ = resolve_revision_aware_kpi_series(
        conn,
        kpi_definition_id=1,
        effective_at=NOW,
        known_at=NOW,
    )
    assert conn.in_transaction
    conn.rollback()


def test_shadow_resolver_applies_effective_cutoff_to_fact_periods() -> None:
    conn = _database()
    definition = persist_kpi_definition_revision(conn, _definition())
    fact_id = _fact(conn)
    persist_kpi_semantic_context(
        conn,
        kpi_fact_id=fact_id,
        context=_context(),
        reviewed_by="owner",
        knowledge_at=NOW,
        kpi_definition_revision_id=definition.kpi_definition_revision_id,
    )

    result = resolve_revision_aware_kpi_series(
        conn,
        kpi_definition_id=1,
        effective_at=datetime(2024, 6, 30, tzinfo=UTC),
        known_at=NOW,
    )
    assert result.eligible_fact_ids == ()


def test_shadow_resolver_excludes_ineligible_related_definitions() -> None:
    conn = _database()
    variants = (
        {
            "kpi_definition_revision_id": "definition-future",
            "idempotency_key": "definition-future",
            "effective_at": datetime(2027, 1, 1, tzinfo=UTC),
        },
        {
            "kpi_definition_revision_id": "definition-quarantined",
            "idempotency_key": "definition-quarantined",
            "status": KpiDefinitionStatus.QUARANTINED,
            "reason_code": "definition_unresolved",
        },
        {
            "kpi_definition_revision_id": "definition-discontinued",
            "idempotency_key": "definition-discontinued",
            "lifecycle": KpiDefinitionLifecycle.DISCONTINUED,
        },
    )
    conn.executemany(
        "INSERT INTO kpi_definitions VALUES (?,?,?,?)",
        [(index, "NU", f"Related variant {index}", "actual") for index in range(2, 5)],
    )
    anchor = persist_kpi_definition_revision(conn, _definition())
    for index, changes in enumerate(variants, start=2):
        related = persist_kpi_definition_revision(
            conn,
            _definition(
                kpi_definition_id=index,
                reported_label=f"Related variant {index}",
                **changes,
            ),
        )
        _ = persist_kpi_definition_comparability_revision(
            conn,
            _relation(
                anchor.kpi_definition_revision_id,
                related.kpi_definition_revision_id,
            ),
        )

    result = resolve_revision_aware_kpi_series(
        conn,
        kpi_definition_id=1,
        effective_at=NOW,
        known_at=NOW,
    )
    assert result.included_definition_revision_ids == (anchor.kpi_definition_revision_id,)
    assert result.breaks == ()


def test_shadow_resolver_fails_explicitly_without_historical_fact_authority() -> None:
    conn = _database()
    definition = persist_kpi_definition_revision(conn, _definition())
    fact_id = _fact(conn)
    persist_kpi_semantic_context(
        conn,
        kpi_fact_id=fact_id,
        context=_context(),
        reviewed_by="owner",
        knowledge_at=NOW,
        kpi_definition_revision_id=definition.kpi_definition_revision_id,
    )
    conn.execute("DROP TABLE fact_resolution_outcomes")

    result = resolve_revision_aware_kpi_series(
        conn,
        kpi_definition_id=1,
        effective_at=NOW,
        known_at=NOW,
    )
    assert result.status is KpiRevisionSeriesStatus.HISTORICAL_FACT_AUTHORITY_UNAVAILABLE
    assert result.eligible_fact_ids == ()
    assert (
        result.exclusions[0].reason
        is KpiRevisionSeriesExclusionReason.HISTORICAL_FACT_AUTHORITY_UNAVAILABLE
    )
    assert result.exclusions[0].count == 1


def test_shadow_resolver_fails_explicitly_on_partial_definition_schema() -> None:
    conn = _database()
    _ = persist_kpi_definition_revision(conn, _definition())
    conn.execute("DROP TABLE kpi_definition_comparability_revisions")

    result = resolve_revision_aware_kpi_series(
        conn,
        kpi_definition_id=1,
        effective_at=NOW,
        known_at=NOW,
    )
    assert result.status is KpiRevisionSeriesStatus.HISTORICAL_FACT_AUTHORITY_UNAVAILABLE
    assert result.eligible_fact_ids == ()


@pytest.mark.parametrize(
    ("table", "missing_column"),
    [
        ("kpi_definition_revisions", "reported_definition_text"),
        ("kpi_fact_semantic_contexts", "metric_name_as_reported"),
        ("kpi_fact_semantic_contexts", "id"),
        ("kpi_definition_comparability_revisions", "disposition"),
        ("kpi_facts", "computed_from"),
    ],
)
def test_shadow_resolver_preflights_every_read_table_column(
    table: str,
    missing_column: str,
) -> None:
    conn = _database()
    if table == "kpi_fact_semantic_contexts" and missing_column == "id":
        retained_columns = [
            str(row[1])
            for row in conn.execute("PRAGMA table_info(kpi_fact_semantic_contexts)")
            if str(row[1]) != "id"
        ]
        projection = ",".join(retained_columns)
        conn.execute(
            "ALTER TABLE kpi_fact_semantic_contexts RENAME TO incomplete_semantic_contexts"
        )
        conn.execute(
            f"CREATE TABLE kpi_fact_semantic_contexts AS "  # nosec B608
            f"SELECT {projection} FROM incomplete_semantic_contexts"  # nosec B608
        )
    else:
        conn.execute(f"ALTER TABLE {table} DROP COLUMN {missing_column}")  # nosec B608

    result = resolve_revision_aware_kpi_series(
        conn,
        kpi_definition_id=1,
        effective_at=NOW,
        known_at=NOW,
    )
    assert result.status is KpiRevisionSeriesStatus.HISTORICAL_FACT_AUTHORITY_UNAVAILABLE
    assert result.eligible_fact_ids == ()


def test_direct_comparability_does_not_expand_transitively() -> None:
    conn = _database()
    conn.executemany(
        "INSERT INTO kpi_definitions VALUES (?,?,?,?)",
        [
            (2, "NU", "Renamed ARPAC", "actual"),
            (3, "NU", "Monthly ARPAC", "actual"),
        ],
    )
    first = persist_kpi_definition_revision(conn, _definition())
    second = persist_kpi_definition_revision(
        conn,
        _definition(
            kpi_definition_revision_id="definition-two-r1",
            idempotency_key="definition-two-key-r1",
            kpi_definition_id=2,
            reported_label="Renamed ARPAC",
        ),
    )
    third = persist_kpi_definition_revision(
        conn,
        _definition(
            kpi_definition_revision_id="definition-three-r1",
            idempotency_key="definition-three-key-r1",
            kpi_definition_id=3,
            reported_label="Monthly ARPAC",
        ),
    )
    persist_kpi_definition_comparability_revision(
        conn, _relation(first.kpi_definition_revision_id, second.kpi_definition_revision_id)
    )
    persist_kpi_definition_comparability_revision(
        conn, _relation(second.kpi_definition_revision_id, third.kpi_definition_revision_id)
    )

    result = resolve_revision_aware_kpi_series(
        conn,
        kpi_definition_id=1,
        effective_at=NOW,
        known_at=NOW,
    )
    assert result.included_definition_revision_ids == (
        first.kpi_definition_revision_id,
        second.kpi_definition_revision_id,
    )
    assert third.kpi_definition_revision_id not in result.included_definition_revision_ids


def test_source_reviewed_capture_rolls_back_definition_fact_and_context_together(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _database()
    predecessor_id = _fact(conn)
    conn.commit()

    def no_restatement(*_: object, **__: object) -> None:
        return None

    monkeypatch.setattr(source_review_module, "record_restatement_observation", no_restatement)

    def fail_resolution(*_: object, **__: object) -> None:
        raise RuntimeError("forced canonical-resolution failure")

    monkeypatch.setattr(source_review_module, "require_canonical_kpi_resolution", fail_resolution)
    with pytest.raises(RuntimeError, match="forced canonical-resolution failure"):
        insert_source_reviewed_kpi_supersession(
            conn,
            predecessor_id=predecessor_id,
            expected_head_id=predecessor_id,
            value=Decimal("13.0"),
            unit=Unit.ACTUAL,
            currency=Currency.USD,
            source_doc_id=10,
            locator=FactLocator(pdf_page=7),
            source_excerpt="Monthly ARPAC was $13.0",
            reviewer="owner",
            knowledge_at=NOW,
            context=_context(),
            definition_revision=_definition(),
        )

    assert conn.execute("SELECT COUNT(*) FROM kpi_facts").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM kpi_fact_semantic_contexts").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM kpi_definition_revisions").fetchone()[0] == 0
    assert (
        conn.execute("SELECT COUNT(*) FROM kpi_definition_comparability_revisions").fetchone()[0]
        == 0
    )


def test_source_reviewed_idle_connection_stays_rollbackable_for_dry_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _database()
    first = persist_kpi_definition_revision(conn, _definition())
    predecessor_id = _fact(conn)
    conn.commit()
    later = NOW + timedelta(hours=1)
    second = _definition(
        kpi_definition_revision_id="definition-r2",
        idempotency_key="definition-key-r2",
        revision=2,
        supersedes_definition_revision_id=first.kpi_definition_revision_id,
        effective_at=datetime(2024, 6, 1, tzinfo=UTC),
        knowledge_at=later,
        recorded_at=later,
    )
    relation = _relation(
        first.kpi_definition_revision_id,
        second.kpi_definition_revision_id,
        knowledge_at=later,
        recorded_at=later,
    )
    monkeypatch.setattr(
        source_review_module,
        "record_restatement_observation",
        _no_effect,
    )
    monkeypatch.setattr(
        source_review_module,
        "require_canonical_kpi_resolution",
        _no_effect,
    )

    _ = insert_source_reviewed_kpi_supersession(
        conn,
        predecessor_id=predecessor_id,
        expected_head_id=predecessor_id,
        value=Decimal("13.0"),
        unit=Unit.ACTUAL,
        currency=Currency.USD,
        source_doc_id=10,
        locator=FactLocator(pdf_page=7),
        source_excerpt="Monthly ARPAC was $13.0",
        reviewer="owner",
        knowledge_at=later,
        context=_context(),
        definition_revision=second,
        comparability_revisions=(relation,),
    )
    assert conn.in_transaction
    assert conn.execute("SELECT COUNT(*) FROM kpi_facts").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM kpi_definition_revisions").fetchone()[0] == 2
    assert (
        conn.execute("SELECT COUNT(*) FROM kpi_definition_comparability_revisions").fetchone()[0]
        == 1
    )

    conn.rollback()
    assert conn.execute("SELECT COUNT(*) FROM kpi_facts").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM kpi_fact_semantic_contexts").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM kpi_definition_revisions").fetchone()[0] == 1
    assert (
        conn.execute("SELECT COUNT(*) FROM kpi_definition_comparability_revisions").fetchone()[0]
        == 0
    )


def test_second_source_review_entry_failure_allows_manifest_rollback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _database()
    predecessor_id = _fact(conn)
    conn.commit()
    monkeypatch.setattr(
        source_review_module,
        "record_restatement_observation",
        _no_effect,
    )
    monkeypatch.setattr(
        source_review_module,
        "require_canonical_kpi_resolution",
        _no_effect,
    )

    def apply_entry() -> int:
        return insert_source_reviewed_kpi_supersession(
            conn,
            predecessor_id=predecessor_id,
            expected_head_id=predecessor_id,
            value=Decimal("13.0"),
            unit=Unit.ACTUAL,
            currency=Currency.USD,
            source_doc_id=10,
            locator=FactLocator(pdf_page=7),
            source_excerpt="Monthly ARPAC was $13.0",
            reviewer="owner",
            knowledge_at=NOW,
            context=_context(),
            definition_revision=_definition(),
        )

    try:
        _ = apply_entry()
        with pytest.raises(ValueError, match="not the exact current head"):
            _ = apply_entry()
        raise RuntimeError("manifest executor must roll back after the second entry")
    except RuntimeError as exc:
        assert str(exc) == "manifest executor must roll back after the second entry"
        conn.rollback()

    assert conn.execute("SELECT COUNT(*) FROM kpi_facts").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM kpi_fact_semantic_contexts").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM kpi_definition_revisions").fetchone()[0] == 0
