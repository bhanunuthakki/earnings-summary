from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from models.facts import Currency, Unit
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
)
from pipeline.kpi_semantics import (
    KpiAccountingBasis,
    KpiConsolidationScope,
    KpiPeriodRole,
    KpiPublicationLane,
    KpiSemanticContext,
    KpiSemanticStatus,
    KpiUnitScale,
)

NOW = datetime(2026, 9, 6, 18, tzinfo=UTC)
EFFECTIVE = datetime(2024, 1, 1, tzinfo=UTC)


def revision_database(path: Path | None = None) -> sqlite3.Connection:
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
    conn.execute(
        "CREATE TABLE tracked_companies("
        "ticker TEXT PRIMARY KEY,list_type TEXT NOT NULL,archived_at TEXT)"
    )
    conn.execute("INSERT INTO tracked_companies VALUES ('NU','portfolio',NULL)")
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


def definition_fixture(**changes: object) -> IssuerKpiDefinitionRevision:
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


def comparability_fixture(
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


def semantic_fixture(label: str = "Monthly ARPAC") -> KpiSemanticContext:
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


def fact_fixture(
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
