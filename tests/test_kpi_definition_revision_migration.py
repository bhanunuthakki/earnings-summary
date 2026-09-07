from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic.config import Config

from alembic import command
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
    persist_kpi_semantic_context,
)
from sqlite_runtime import (
    SQLiteConnectionRole,
    connect_sqlite,
    register_sqlite_integrity_functions,
)

ROOT = Path(__file__).resolve().parents[1]
REVISION = "0038_add_kpi_definition_revisions"
PARENT = "0037_commitment_scan_segment_coverage"
AT = datetime(2026, 9, 6, 18, tzinfo=UTC)
LOCATOR_JSON = '{"page":7}'
LOCATOR_SHA = hashlib.sha256(LOCATOR_JSON.encode()).hexdigest()
KPI_ROOT_ID = 900_001


def _config(path: Path) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path.as_posix()}")
    return config


def _writer_connection(path: Path) -> sqlite3.Connection:
    return connect_sqlite(path, role=SQLiteConnectionRole.WRITER)


def _raw_guarded_connection(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    register_sqlite_integrity_functions(conn)
    return conn


def _seed_authority(conn: sqlite3.Connection) -> None:
    timestamp = AT.isoformat().replace("+00:00", "Z")
    blob_sha = "a" * 64
    conn.execute(
        "INSERT INTO issuer_entities VALUES (?,?,?,?)",
        ("issuer-nu", "issuer-nu", "operating_company", timestamp),
    )
    conn.execute(
        "INSERT INTO reporting_entities VALUES (?,?,?,?,?,?)",
        (
            "entity-nu",
            "entity-nu",
            "issuer-nu",
            "legal_registrant",
            "Nu Holdings",
            timestamp,
        ),
    )
    conn.execute(
        "INSERT INTO evidence_content_blobs VALUES (?,?,?,?,?)",
        (blob_sha, 1, "application/pdf", "evidence/test.pdf", timestamp),
    )
    conn.execute(
        "INSERT INTO evidence_source_observations "
        "(observation_id,idempotency_key,source_kind,source_url,blob_sha256,observed_at,"
        "retrieved_at,retrieval_config_sha256,collector_code_version) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (
            "observation-nu",
            "observation-nu",
            "ir_document",
            "https://example.invalid/nu.pdf",
            blob_sha,
            timestamp,
            timestamp,
            "b" * 64,
            "test-collector/v1",
        ),
    )
    conn.execute(
        "INSERT INTO documents "
        "(id,ticker,source_type,doc_type,file_path,sha256,fetched_at,fetch_status,"
        "raw_bytes_size,source_url,source_quality_tier) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            100,
            "NU",
            "ir",
            "earnings_release",
            "evidence/test.pdf",
            "c" * 64,
            timestamp,
            "success",
            1,
            "https://example.invalid/nu.pdf",
            "company_reported",
        ),
    )
    conn.execute(
        "INSERT INTO evidence_document_versions "
        "(document_version_id,document_key,version_sequence,observation_id,blob_sha256,"
        "issuer_id,ticker,document_type,form_type,language,legacy_document_id,recorded_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "document-nu-v1",
            "document-nu",
            1,
            "observation-nu",
            blob_sha,
            "issuer-nu",
            "NU",
            "earnings_release",
            "EX-99.1",
            "en",
            100,
            timestamp,
        ),
    )
    conn.execute(
        "INSERT INTO evidence_extraction_runs "
        "(extraction_run_id,idempotency_key,document_version_id,input_sha256,"
        "extractor_name,extractor_config_sha256,extractor_code_version,output_sha256,"
        "started_at,completed_at,outcome) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            "run-nu-v1",
            "run-nu-v1",
            "document-nu-v1",
            blob_sha,
            "test-extractor",
            "d" * 64,
            "test-extractor/v1",
            "e" * 64,
            timestamp,
            timestamp,
            "succeeded",
        ),
    )
    conn.execute(
        "INSERT INTO evidence_nodes "
        "(node_id,evidence_key,revision,extraction_run_id,node_kind,text,recorded_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (
            "document-node-nu-v1",
            "document-node-nu",
            1,
            "run-nu-v1",
            "document",
            "Nu earnings release",
            timestamp,
        ),
    )
    conn.execute(
        "INSERT INTO evidence_nodes "
        "(node_id,evidence_key,revision,extraction_run_id,parent_node_id,node_kind,text,locator_json,"
        "locator_sha256,recorded_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            "node-nu-v1",
            "node-nu",
            1,
            "run-nu-v1",
            "document-node-nu-v1",
            "table_cell",
            "Monthly ARPAC",
            LOCATOR_JSON,
            LOCATOR_SHA,
            timestamp,
        ),
    )
    conn.execute(
        "INSERT INTO kpi_definitions "
        "(id,ticker,name,unit,primary_source,reporting_cadence,definition_origin) "
        "VALUES (?,'NU','Monthly ARPAC','actual','ir','quarterly','capture')",
        (KPI_ROOT_ID,),
    )


def _definition(**changes: object) -> IssuerKpiDefinitionRevision:
    values: dict[str, object] = {
        "kpi_definition_revision_id": "definition-nu-r1",
        "idempotency_key": "definition-nu-r1",
        "kpi_definition_id": KPI_ROOT_ID,
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
        "source_document_version_id": "document-nu-v1",
        "source_evidence_node_id": "node-nu-v1",
        "source_locator": {"page": 7},
        "reviewed_by": "owner",
        "effective_at": AT,
        "knowledge_at": AT,
        "recorded_at": AT,
    }
    values.update(changes)
    return IssuerKpiDefinitionRevision.model_validate(values)


def _context() -> KpiSemanticContext:
    return KpiSemanticContext(
        metric_name_as_reported="Monthly ARPAC",
        reported_period_end=AT.date(),
        period_role=KpiPeriodRole.CURRENT,
        publication_lane=KpiPublicationLane.CURRENT_ACTUAL,
        accounting_basis=KpiAccountingBasis.MANAGEMENT,
        consolidation_scope=KpiConsolidationScope.CONSOLIDATED,
        dimensions={},
        unit_scale=KpiUnitScale.NONE,
        status=KpiSemanticStatus.ADMITTED,
    )


def test_migration_keeps_unbound_context_explicit_and_enforces_exact_binding(
    migrated_db: Callable[..., Path], tmp_path: Path
) -> None:
    path = migrated_db(tmp_path / "kpi-definition-binding.db", target=REVISION)
    with _writer_connection(path) as conn:
        conn.row_factory = sqlite3.Row
        _seed_authority(conn)
        conn.execute(
            "INSERT INTO kpi_facts "
            "(id,ticker,period_end,fiscal_period_type,kpi_definition_id,value,currency,unit,"
            "source_doc_id,source_excerpt,confidence,extracted_by,locator) "
            "VALUES (1,'NU',?,'Q4',?,12.5,'USD','actual',100,?,1.0,'test',?)",
            (AT.isoformat(), KPI_ROOT_ID, "Monthly ARPAC was $12.5", LOCATOR_JSON),
        )
        unbound_id = persist_kpi_semantic_context(
            conn,
            kpi_fact_id=1,
            context=_context(),
            reviewed_by="owner",
            knowledge_at=AT,
            kpi_definition_revision_id=None,
        )
        assert (
            conn.execute(
                "SELECT kpi_definition_revision_id FROM kpi_fact_semantic_contexts WHERE id=?",
                (unbound_id,),
            ).fetchone()[0]
            is None
        )

        definition = persist_kpi_definition_revision(conn, _definition())
        bound_id = persist_kpi_semantic_context(
            conn,
            kpi_fact_id=1,
            context=_context(),
            reviewed_by="owner",
            knowledge_at=AT,
            kpi_definition_revision_id=definition.kpi_definition_revision_id,
        )
        assert (
            conn.execute(
                "SELECT kpi_definition_revision_id FROM kpi_fact_semantic_contexts WHERE id=?",
                (bound_id,),
            ).fetchone()[0]
            == definition.kpi_definition_revision_id
        )

        with pytest.raises(sqlite3.IntegrityError, match="binding mismatch"):
            conn.execute(
                "INSERT INTO kpi_fact_semantic_contexts "
                "(kpi_fact_id,revision,supersedes_context_id,metric_name_as_reported,"
                "reported_period_end,period_role,publication_lane,accounting_basis,"
                "consolidation_scope,dimensions_json,unit_scale,status,reviewed_by,knowledge_at,"
                "kpi_definition_revision_id) VALUES (1,3,?,'Different economics',?,'current',"
                "'current_actual','management','consolidated','{}','none','admitted','owner',?,?)",
                (
                    bound_id,
                    AT.date().isoformat(),
                    AT.isoformat(),
                    definition.kpi_definition_revision_id,
                ),
            )

        later = AT.replace(hour=19)
        _ = persist_kpi_definition_revision(
            conn,
            _definition(
                kpi_definition_revision_id="definition-nu-r2",
                idempotency_key="definition-nu-r2",
                revision=2,
                supersedes_definition_revision_id=definition.kpi_definition_revision_id,
                knowledge_at=later,
                recorded_at=later,
            ),
        )
        with pytest.raises(sqlite3.IntegrityError, match="binding mismatch"):
            conn.execute(
                "INSERT INTO kpi_fact_semantic_contexts "
                "(kpi_fact_id,revision,supersedes_context_id,metric_name_as_reported,"
                "reported_period_end,period_role,publication_lane,accounting_basis,"
                "consolidation_scope,dimensions_json,unit_scale,status,reviewed_by,knowledge_at,"
                "kpi_definition_revision_id) VALUES (1,3,?,'Monthly ARPAC',?,'current',"
                "'current_actual','management','consolidated','{}','none','admitted','owner',?,?)",
                (
                    bound_id,
                    AT.date().isoformat(),
                    later.isoformat(),
                    definition.kpi_definition_revision_id,
                ),
            )


def test_database_rejects_locator_tampering_sibling_branches_and_history_loss(
    migrated_db: Callable[..., Path], tmp_path: Path
) -> None:
    path = migrated_db(tmp_path / "kpi-definition-guards.db", target=REVISION)
    with _writer_connection(path) as conn:
        _seed_authority(conn)
        first = persist_kpi_definition_revision(conn, _definition())
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute(
                "UPDATE kpi_definition_revisions SET reported_label='Changed' "
                "WHERE kpi_definition_revision_id=?",
                (first.kpi_definition_revision_id,),
            )

        wrong_locator = _definition(
            kpi_definition_revision_id="definition-nu-r2-wrong-locator",
            idempotency_key="definition-nu-r2-wrong-locator",
            revision=2,
            supersedes_definition_revision_id=first.kpi_definition_revision_id,
            source_locator={"page": 8},
        )
        with pytest.raises(sqlite3.IntegrityError, match="source evidence mismatch"):
            _insert_raw_definition(conn, wrong_locator)

        second = persist_kpi_definition_revision(
            conn,
            _definition(
                kpi_definition_revision_id="definition-nu-r2",
                idempotency_key="definition-nu-r2",
                revision=2,
                supersedes_definition_revision_id=first.kpi_definition_revision_id,
                effective_at=datetime(2027, 1, 1, tzinfo=UTC),
            ),
        )
        sibling = _definition(
            kpi_definition_revision_id="definition-nu-r2-sibling",
            idempotency_key="definition-nu-r2-sibling",
            revision=2,
            supersedes_definition_revision_id=first.kpi_definition_revision_id,
        )
        with pytest.raises(sqlite3.IntegrityError):
            _insert_raw_definition(conn, sibling)
        assert second.revision == 2

        conn.execute(
            "INSERT INTO kpi_definitions "
            "(id,ticker,name,unit,primary_source,reporting_cadence,definition_origin) "
            "VALUES (?,'NU','Monthly ARPAC BRL','actual','ir','quarterly','capture')",
            (KPI_ROOT_ID + 1,),
        )
        brl = persist_kpi_definition_revision(
            conn,
            _definition(
                kpi_definition_revision_id="definition-nu-brl-r1",
                idempotency_key="definition-nu-brl-r1",
                kpi_definition_id=KPI_ROOT_ID + 1,
                currency=Currency.BRL,
            ),
        )
        currency_continuity = KpiDefinitionComparabilityRevision(
            comparability_revision_id="relation-usd-brl-r1",
            idempotency_key="relation-usd-brl-r1",
            predecessor_definition_revision_id=first.kpi_definition_revision_id,
            successor_definition_revision_id=brl.kpi_definition_revision_id,
            revision=1,
            relation_kind=KpiDefinitionRelationKind.RENAMED,
            disposition=KpiDefinitionComparabilityDisposition.CONTINUOUS,
            reason_code="issuer_disclosed_rename",
            reviewed_by="owner",
            source_document_version_id="document-nu-v1",
            source_evidence_node_id="node-nu-v1",
            source_locator={"page": 7},
            effective_at=AT,
            knowledge_at=AT,
            recorded_at=AT,
        )
        with pytest.raises(sqlite3.IntegrityError, match="currency change"):
            _insert_raw_comparability(conn, currency_continuity)

        conn.executemany(
            "INSERT INTO kpi_definitions "
            "(id,ticker,name,unit,primary_source,reporting_cadence,definition_origin) "
            "VALUES (?,?,?,'count','ir','quarterly','capture')",
            [
                (KPI_ROOT_ID + 2, "NU", "Customer count"),
                (KPI_ROOT_ID + 3, "NU", "Customer count in thousands"),
            ],
        )
        count = persist_kpi_definition_revision(
            conn,
            _definition(
                kpi_definition_revision_id="definition-nu-count-r1",
                idempotency_key="definition-nu-count-r1",
                kpi_definition_id=KPI_ROOT_ID + 2,
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
                kpi_definition_revision_id="definition-nu-count-thousands-r1",
                idempotency_key="definition-nu-count-thousands-r1",
                kpi_definition_id=KPI_ROOT_ID + 3,
                reported_label="Customer count in thousands",
                unit_family=KpiUnitFamily.COUNT,
                unit_key=Unit.COUNT,
                unit_scale=KpiUnitScale.THOUSANDS,
                currency_disposition=KpiCurrencyDisposition.NOT_APPLICABLE,
                currency=None,
            ),
        )
        conn.execute(
            "INSERT INTO kpi_definitions "
            "(id,ticker,name,unit,primary_source,reporting_cadence,definition_origin) "
            "VALUES (?,'NU','Customer ratio','ratio','ir','quarterly','capture')",
            (KPI_ROOT_ID + 4,),
        )
        ratio = persist_kpi_definition_revision(
            conn,
            _definition(
                kpi_definition_revision_id="definition-nu-ratio-r1",
                idempotency_key="definition-nu-ratio-r1",
                kpi_definition_id=KPI_ROOT_ID + 4,
                reported_label="Customer ratio",
                stock_flow_behavior=KpiStockFlowBehavior.RATIO,
                unit_family=KpiUnitFamily.RATIO,
                unit_key=Unit.RATIO,
                currency_disposition=KpiCurrencyDisposition.NOT_APPLICABLE,
                currency=None,
            ),
        )
        for relation in (
            KpiDefinitionComparabilityRevision(
                comparability_revision_id="relation-ratio-count-r1",
                idempotency_key="relation-ratio-count-r1",
                predecessor_definition_revision_id=ratio.kpi_definition_revision_id,
                successor_definition_revision_id=count.kpi_definition_revision_id,
                revision=1,
                relation_kind=KpiDefinitionRelationKind.RENAMED,
                disposition=KpiDefinitionComparabilityDisposition.CONTINUOUS,
                reason_code="invalid_cross_family",
                reviewed_by="owner",
                source_document_version_id="document-nu-v1",
                source_evidence_node_id="node-nu-v1",
                source_locator={"page": 7},
                effective_at=AT,
                knowledge_at=AT,
                recorded_at=AT,
            ),
            KpiDefinitionComparabilityRevision(
                comparability_revision_id="relation-count-scale-r1",
                idempotency_key="relation-count-scale-r1",
                predecessor_definition_revision_id=count.kpi_definition_revision_id,
                successor_definition_revision_id=scaled_count.kpi_definition_revision_id,
                revision=1,
                relation_kind=KpiDefinitionRelationKind.RENAMED,
                disposition=KpiDefinitionComparabilityDisposition.CONTINUOUS,
                reason_code="invalid_unhandled_scale",
                reviewed_by="owner",
                source_document_version_id="document-nu-v1",
                source_evidence_node_id="node-nu-v1",
                source_locator={"page": 7},
                effective_at=AT,
                knowledge_at=AT,
                recorded_at=AT,
            ),
        ):
            with pytest.raises(sqlite3.IntegrityError, match="unit or scale change"):
                _insert_raw_comparability(conn, relation)
        conn.commit()

    with pytest.raises(RuntimeError, match="without losing history"):
        command.downgrade(_config(path), PARENT)


def test_empty_migration_downgrades_without_leaving_revision_tables(
    migrated_db: Callable[..., Path], tmp_path: Path
) -> None:
    path = migrated_db(tmp_path / "kpi-definition-empty-downgrade.db", target=REVISION)
    command.downgrade(_config(path), PARENT)
    with sqlite3.connect(path) as conn:
        tables = {
            str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        columns = {
            str(row[1]) for row in conn.execute("PRAGMA table_info(kpi_fact_semantic_contexts)")
        }
    assert "kpi_definition_revisions" not in tables
    assert "kpi_definition_comparability_revisions" not in tables
    assert "kpi_definition_revision_id" not in columns


def test_migration_round_trips_every_approved_count_scale_and_rejects_invalid_pairs(
    migrated_db: Callable[..., Path], tmp_path: Path
) -> None:
    path = migrated_db(tmp_path / "kpi-definition-count-scales.db", target=REVISION)
    scales = (
        KpiUnitScale.NONE,
        KpiUnitScale.THOUSANDS,
        KpiUnitScale.MILLIONS,
        KpiUnitScale.BILLIONS,
    )
    with _writer_connection(path) as conn:
        _seed_authority(conn)
        conn.executemany(
            "INSERT INTO kpi_definitions "
            "(id,ticker,name,unit,primary_source,reporting_cadence,definition_origin) "
            "VALUES (?,?,?,'count','ir','quarterly','capture')",
            [
                (index, "NU", f"Count scale {scale.value}")
                for index, scale in enumerate(scales, start=KPI_ROOT_ID + 1)
            ],
        )
        for index, scale in enumerate(scales, start=KPI_ROOT_ID + 1):
            persisted = persist_kpi_definition_revision(
                conn,
                _definition(
                    kpi_definition_revision_id=f"definition-count-{scale.value}",
                    idempotency_key=f"definition-count-{scale.value}",
                    kpi_definition_id=index,
                    reported_label=f"Count scale {scale.value}",
                    unit_family=KpiUnitFamily.COUNT,
                    unit_key=Unit.COUNT,
                    unit_scale=scale,
                    currency_disposition=KpiCurrencyDisposition.NOT_APPLICABLE,
                    currency=None,
                ),
            )
            assert persisted.unit_scale is scale

        with pytest.raises(sqlite3.IntegrityError):
            _insert_raw_definition_payload(
                conn,
                _definition(
                    kpi_definition_revision_id="definition-invalid-money-scale",
                    idempotency_key="definition-invalid-money-scale",
                    kpi_definition_id=KPI_ROOT_ID + 1,
                ),
                unit_scale=KpiUnitScale.MILLIONS.value,
            )


def test_migration_rejects_noncanonical_empty_objects_and_nonstring_dimensions(
    migrated_db: Callable[..., Path], tmp_path: Path
) -> None:
    path = migrated_db(tmp_path / "kpi-definition-json-shape.db", target=REVISION)
    with _raw_guarded_connection(path) as conn:
        _seed_authority(conn)
        with pytest.raises(sqlite3.IntegrityError):
            _insert_raw_definition_payload(
                conn,
                _definition(),
                source_locator_json="{ }",
            )
        with pytest.raises(sqlite3.IntegrityError):
            _insert_raw_definition_payload(
                conn,
                _definition(
                    consolidation_scope=KpiConsolidationScope.SEGMENT,
                    dimensions={"segment": "consumer"},
                ),
                dimensions_json="{ }",
            )
        with pytest.raises(sqlite3.IntegrityError, match="string values"):
            _insert_raw_definition_payload(
                conn,
                _definition(),
                dimensions_json='{"region":1}',
            )


@pytest.mark.parametrize(
    ("missing_key", "changes"),
    [
        ("scope_security_id", {}),
        ("supersedes_definition_revision_id", {}),
        (
            "reported_definition_text",
            {
                "reported_definition_text": None,
                "definition_text_status": KpiDefinitionTextStatus.NOT_STATED,
            },
        ),
        (
            "currency",
            {
                "unit_family": KpiUnitFamily.COUNT,
                "unit_key": Unit.COUNT,
                "currency_disposition": KpiCurrencyDisposition.NOT_APPLICABLE,
                "currency": None,
            },
        ),
        ("reason_code", {}),
    ],
)
def test_migration_requires_nullable_definition_commitment_keys(
    migrated_db: Callable[..., Path],
    tmp_path: Path,
    missing_key: str,
    changes: dict[str, object],
) -> None:
    path = migrated_db(
        tmp_path / f"kpi-definition-missing-{missing_key}.db",
        target=REVISION,
    )
    with _raw_guarded_connection(path) as conn:
        _seed_authority(conn)
        revision = _definition(**changes)
        with pytest.raises(sqlite3.IntegrityError, match="keys are incomplete"):
            _insert_raw_definition_payload(
                conn,
                revision,
                omit_commitment_key=missing_key,
            )
        _insert_raw_definition(conn, revision)
        assert conn.execute("SELECT COUNT(*) FROM kpi_definition_revisions").fetchone()[0] == 1


def test_migration_requires_nullable_comparability_commitment_key(
    migrated_db: Callable[..., Path], tmp_path: Path
) -> None:
    path = migrated_db(tmp_path / "kpi-comparability-missing-null-key.db", target=REVISION)
    with _raw_guarded_connection(path) as conn:
        _seed_authority(conn)
        conn.execute(
            "INSERT INTO kpi_definitions "
            "(id,ticker,name,unit,primary_source,reporting_cadence,definition_origin) "
            "VALUES (?,'NU','Renamed ARPAC','actual','ir','quarterly','capture')",
            (KPI_ROOT_ID + 1,),
        )
        first = persist_kpi_definition_revision(conn, _definition())
        second = persist_kpi_definition_revision(
            conn,
            _definition(
                kpi_definition_revision_id="definition-nu-renamed-r1",
                idempotency_key="definition-nu-renamed-r1",
                kpi_definition_id=KPI_ROOT_ID + 1,
                reported_label="Renamed ARPAC",
            ),
        )
        relation = KpiDefinitionComparabilityRevision(
            comparability_revision_id="relation-null-key-r1",
            idempotency_key="relation-null-key-r1",
            predecessor_definition_revision_id=first.kpi_definition_revision_id,
            successor_definition_revision_id=second.kpi_definition_revision_id,
            revision=1,
            relation_kind=KpiDefinitionRelationKind.RENAMED,
            disposition=KpiDefinitionComparabilityDisposition.CONTINUOUS,
            reason_code="issuer_disclosed_rename",
            reviewed_by="owner",
            source_document_version_id="document-nu-v1",
            source_evidence_node_id="node-nu-v1",
            source_locator={"page": 7},
            effective_at=AT,
            knowledge_at=AT,
            recorded_at=AT,
        )
        with pytest.raises(sqlite3.IntegrityError, match="keys are incomplete"):
            _insert_raw_comparability_payload(
                conn,
                relation,
                omit_commitment_key="supersedes_comparability_revision_id",
            )
        _insert_raw_comparability(conn, relation)
        assert (
            conn.execute("SELECT COUNT(*) FROM kpi_definition_comparability_revisions").fetchone()[
                0
            ]
            == 1
        )


def test_migration_rejects_continuity_across_semantic_axes_and_relation_kinds(
    migrated_db: Callable[..., Path], tmp_path: Path
) -> None:
    path = migrated_db(tmp_path / "kpi-definition-semantic-axes.db", target=REVISION)
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
    with _writer_connection(path) as conn:
        _seed_authority(conn)
        conn.executemany(
            "INSERT INTO kpi_definitions "
            "(id,ticker,name,unit,primary_source,reporting_cadence,definition_origin) "
            "VALUES (?,?,?,'actual','ir','quarterly','capture')",
            [
                (index, "NU", f"Semantic axis {index}")
                for index in range(KPI_ROOT_ID + 1, KPI_ROOT_ID + 1 + len(axis_changes))
            ],
        )
        baseline = persist_kpi_definition_revision(conn, _definition())
        for index, changes in enumerate(axis_changes, start=KPI_ROOT_ID + 1):
            changed = persist_kpi_definition_revision(
                conn,
                _definition(
                    kpi_definition_revision_id=f"definition-axis-{index}",
                    idempotency_key=f"definition-axis-{index}",
                    kpi_definition_id=index,
                    reported_label=f"Semantic axis {index}",
                    **changes,
                ),
            )
            relation = KpiDefinitionComparabilityRevision(
                comparability_revision_id=f"relation-axis-{index}",
                idempotency_key=f"relation-axis-{index}",
                predecessor_definition_revision_id=baseline.kpi_definition_revision_id,
                successor_definition_revision_id=changed.kpi_definition_revision_id,
                revision=1,
                relation_kind=KpiDefinitionRelationKind.RENAMED,
                disposition=KpiDefinitionComparabilityDisposition.CONTINUOUS,
                reason_code="invalid_semantic_continuity",
                reviewed_by="owner",
                source_document_version_id="document-nu-v1",
                source_evidence_node_id="node-nu-v1",
                source_locator={"page": 7},
                effective_at=AT,
                knowledge_at=AT,
                recorded_at=AT,
            )
            with pytest.raises(sqlite3.IntegrityError, match="semantic-axis change"):
                _insert_raw_comparability(conn, relation)

        valid_relation = KpiDefinitionComparabilityRevision(
            comparability_revision_id="relation-kind-raw",
            idempotency_key="relation-kind-raw",
            predecessor_definition_revision_id=baseline.kpi_definition_revision_id,
            successor_definition_revision_id="definition-axis-2",
            revision=1,
            relation_kind=KpiDefinitionRelationKind.RENAMED,
            disposition=KpiDefinitionComparabilityDisposition.CONTINUOUS,
            reason_code="invalid_relation_kind",
            reviewed_by="owner",
            source_document_version_id="document-nu-v1",
            source_evidence_node_id="node-nu-v1",
            source_locator={"page": 7},
            effective_at=AT,
            knowledge_at=AT,
            recorded_at=AT,
        )
        with pytest.raises(sqlite3.IntegrityError):
            _insert_raw_comparability_payload(
                conn,
                valid_relation,
                relation_kind=KpiDefinitionRelationKind.REDEFINED.value,
            )


_DEFINITION_COLUMNS = (
    "kpi_definition_revision_id",
    "idempotency_key",
    "kpi_definition_id",
    "reporting_entity_id",
    "scope_security_id",
    "revision",
    "supersedes_definition_revision_id",
    "status",
    "lifecycle",
    "reported_label",
    "reported_definition_text",
    "definition_text_status",
    "period_kind",
    "stock_flow_behavior",
    "unit_family",
    "unit_key",
    "unit_scale",
    "currency_disposition",
    "currency",
    "accounting_basis",
    "consolidation_scope",
    "dimensions_json",
    "source_document_version_id",
    "source_evidence_node_id",
    "source_locator_json",
    "source_locator_sha256",
    "reason_code",
    "reviewed_by",
    "commitment_json",
    "commitment_sha256",
    "effective_at",
    "knowledge_at",
    "recorded_at",
)


def _insert_raw_definition(conn: sqlite3.Connection, revision: IssuerKpiDefinitionRevision) -> None:
    _insert_raw_definition_payload(conn, revision)


def _insert_raw_definition_payload(
    conn: sqlite3.Connection,
    revision: IssuerKpiDefinitionRevision,
    omit_commitment_key: str | None = None,
    **overrides: object,
) -> None:
    payload = {
        **revision.model_dump(mode="json"),
        "dimensions_json": json.dumps(revision.dimensions, sort_keys=True, separators=(",", ":")),
        "source_locator_json": revision.source_locator_json,
        "source_locator_sha256": revision.source_locator_sha256,
        "commitment_json": revision.commitment_json,
        "commitment_sha256": revision.commitment_sha256,
    }
    payload.update(overrides)
    commitment = json.loads(revision.commitment_json)
    if "dimensions_json" in overrides:
        commitment["dimensions"] = json.loads(str(overrides["dimensions_json"]))
    if "source_locator_json" in overrides:
        commitment["source_locator"] = json.loads(str(overrides["source_locator_json"]))
        payload["source_locator_sha256"] = hashlib.sha256(
            str(overrides["source_locator_json"]).encode()
        ).hexdigest()
    for key in ("unit_family", "unit_key", "unit_scale"):
        if key in overrides:
            commitment[key] = overrides[key]
    if omit_commitment_key is not None:
        del commitment[omit_commitment_key]
    commitment_json = json.dumps(commitment, sort_keys=True, separators=(",", ":"))
    payload["commitment_json"] = commitment_json
    payload["commitment_sha256"] = hashlib.sha256(commitment_json.encode()).hexdigest()
    values = [payload.get(column) for column in _DEFINITION_COLUMNS]
    conn.execute(
        f"INSERT INTO kpi_definition_revisions ({','.join(_DEFINITION_COLUMNS)}) "
        f"VALUES ({','.join('?' for _ in values)})",  # nosec B608 -- fixed test columns
        values,
    )


_COMPARABILITY_COLUMNS = (
    "comparability_revision_id",
    "idempotency_key",
    "predecessor_definition_revision_id",
    "successor_definition_revision_id",
    "revision",
    "supersedes_comparability_revision_id",
    "relation_kind",
    "disposition",
    "reason_code",
    "reviewed_by",
    "source_document_version_id",
    "source_evidence_node_id",
    "source_locator_json",
    "source_locator_sha256",
    "commitment_json",
    "commitment_sha256",
    "effective_at",
    "knowledge_at",
    "recorded_at",
)


def _insert_raw_comparability(
    conn: sqlite3.Connection, revision: KpiDefinitionComparabilityRevision
) -> None:
    _insert_raw_comparability_payload(conn, revision)


def _insert_raw_comparability_payload(
    conn: sqlite3.Connection,
    revision: KpiDefinitionComparabilityRevision,
    omit_commitment_key: str | None = None,
    **overrides: object,
) -> None:
    payload = {
        **revision.model_dump(mode="json"),
        "source_locator_json": revision.source_locator_json,
        "source_locator_sha256": revision.source_locator_sha256,
        "commitment_json": revision.commitment_json,
        "commitment_sha256": revision.commitment_sha256,
    }
    payload.update(overrides)
    commitment = json.loads(revision.commitment_json)
    for key in ("relation_kind", "disposition"):
        if key in overrides:
            commitment[key] = overrides[key]
    if "source_locator_json" in overrides:
        commitment["source_locator"] = json.loads(str(overrides["source_locator_json"]))
        payload["source_locator_sha256"] = hashlib.sha256(
            str(overrides["source_locator_json"]).encode()
        ).hexdigest()
    if omit_commitment_key is not None:
        del commitment[omit_commitment_key]
    commitment_json = json.dumps(commitment, sort_keys=True, separators=(",", ":"))
    payload["commitment_json"] = commitment_json
    payload["commitment_sha256"] = hashlib.sha256(commitment_json.encode()).hexdigest()
    values = [payload.get(column) for column in _COMPARABILITY_COLUMNS]
    conn.execute(
        "INSERT INTO kpi_definition_comparability_revisions "
        f"({','.join(_COMPARABILITY_COLUMNS)}) "
        f"VALUES ({','.join('?' for _ in values)})",  # nosec B608 -- fixed test columns
        values,
    )
