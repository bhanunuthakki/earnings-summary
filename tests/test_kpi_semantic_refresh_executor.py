from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable, Generator
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Protocol

import pytest
from pydantic import ValidationError

from execution import apply_kpi_semantic_refresh as refresh
from execution import record_kpi_repair_judgment as record_judgment
from execution.backup_restore_readiness_receipt import BackupRestoreReadinessReceipt
from execution.fetch_windows_review_bundle import WindowsReviewPins
from models.facts import Currency, FactLocator, LocatorKind, Unit
from operations.kpi_repair_receipts import (
    KpiRepairAttemptReceipt,
    KpiRepairJudgeReceipt,
    repair_executor_code_sha256,
    seal_attempt,
    seal_judgment,
)
from operations.review_bundle import (
    OperationsReviewBundle,
    ReviewIdentity,
    ReviewObservation,
    ReviewScheduler,
    ReviewSchema,
)
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
from pipeline.kpi_semantic_dispositions import (
    LegacyKpiQuarantineRequest,
    apply_kpi_semantic_disposition_manifest,
    prepare_kpi_semantic_disposition_manifest,
)
from pipeline.kpi_semantic_scope import ScopedKpiDefinition
from pipeline.kpi_semantics import (
    KpiAccountingBasis,
    KpiConsolidationScope,
    KpiPeriodRole,
    KpiPublicationLane,
    KpiSemanticContext,
    KpiSemanticContextRevision,
    KpiSemanticStatus,
    KpiUnitScale,
    current_kpi_semantic_context,
    normalize_source_numeric,
    persist_kpi_semantic_context,
)
from provenance.evidence_ledger import EvidenceLocator
from provenance.fulltext_extractor_identity import (
    PDF_FULLTEXT_EXTRACTOR,
)
from schema_compat import expected_head
from sqlite_freshness import sqlite_file_token


class EntryEffectView(Protocol):
    inserted_fact_rows: int
    inserted_context_rows: int
    inserted_definition_rows: int
    inserted_comparability_rows: int
    fact_head_id: int
    definition_revision_id: str | None
    definition_commitment_sha256: str | None


# Explicit internal test seams; production callers retain the public authority APIs.
apply_entry = getattr(refresh, "_apply_entry")
context_for_entry = getattr(refresh, "_context_for_entry")
detect_applied_postcondition = getattr(refresh, "_detect_applied_postcondition")
require_canonical_result_heads = getattr(refresh, "_require_canonical_result_heads")
validate_applied_entry_postcondition = getattr(refresh, "_validate_applied_entry_postcondition")
validate_apply_authority = getattr(refresh, "_validate_apply_authority")
validate_source_binding = getattr(refresh, "_validate_source_binding")
validate_v2_idempotency_marker = getattr(refresh, "_validate_v2_idempotency_marker")
verify_replay = getattr(refresh, "_verify_replay")
write_content_addressed = getattr(refresh, "_write_content_addressed")
EntryEffect = getattr(refresh, "_EntryEffect")

NOW = datetime(2026, 8, 27, 20, tzinfo=UTC)
SOURCE_EVIDENCE_LOCATOR = EvidenceLocator(
    source_ref="ir_documents/NU/q4.pdf",
    page_number=7,
)


def _accept_pinned_identity(**_kwargs: object) -> None:
    return None


def _no_receipt_reasons(*_args: object, **_kwargs: object) -> tuple[str, ...]:
    return ()


def _source_nu(
    _conn: sqlite3.Connection, _entry: refresh.RefreshEntry
) -> tuple[refresh.SourceType, str]:
    return refresh.SourceType.IR_DOC, "NU"


def _source_wix(
    _conn: sqlite3.Connection, _entry: refresh.RefreshEntry
) -> tuple[refresh.SourceType, str]:
    return refresh.SourceType.IR_DOC, "WIX"


def _pins() -> WindowsReviewPins:
    return WindowsReviewPins.model_construct()


def _backup(manifest: refresh.RefreshManifest) -> BackupRestoreReadinessReceipt:
    return BackupRestoreReadinessReceipt.model_construct(
        evidence_id=manifest.backup_restore_evidence_id
    )


def _review_bundle(
    manifest: refresh.RefreshManifest,
    db_path: Path,
    *,
    observed_at: datetime = NOW,
    scheduler_recorded_at: datetime = NOW,
) -> OperationsReviewBundle:
    observation = ReviewObservation.model_construct(
        state="current", observed_at=observed_at, evidence_recorded_at=scheduler_recorded_at
    )
    return OperationsReviewBundle.model_construct(
        observed_at=observed_at,
        identity=ReviewIdentity.model_construct(
            database_instance_sha256=hashlib.sha256(str(db_path.resolve()).encode()).hexdigest()
        ),
        database=observation,
        schema_revision=ReviewSchema.model_construct(
            observation=observation,
            actual_heads=(manifest.expected_schema_revision,),
            matches=True,
        ),
        scheduler=ReviewScheduler.model_construct(observation=observation, tasks=()),
        content_sha256=manifest.review_bundle_sha256,
    )


def _context() -> KpiSemanticContext:
    return KpiSemanticContext(
        metric_name_as_reported="Total customers",
        reported_period_end=date(2024, 12, 31),
        period_role=KpiPeriodRole.CURRENT,
        publication_lane=KpiPublicationLane.CURRENT_ACTUAL,
        accounting_basis=KpiAccountingBasis.MANAGEMENT,
        consolidation_scope=KpiConsolidationScope.CONSOLIDATED,
        unit_scale=KpiUnitScale.MILLIONS,
        status=KpiSemanticStatus.ADMITTED,
    )


def _entry(**changes: object) -> refresh.RefreshEntry:
    excerpt = "Total customers reached 114 million."
    locator = FactLocator(
        kind=LocatorKind.PDF_SLIDE,
        pdf_page=7,
        verbatim_snippet=excerpt,
    )
    locator_json = locator.to_json()
    assert locator_json is not None
    values: dict[str, object] = {
        "action": "supersede",
        "old_fact_id": 10,
        "expected_fact_head_id": 10,
        "expected_context_head_id": None,
        "expected_context_revision": 0,
        "expected_old_source_doc_id": 1,
        "expected_old_source_sha256": "a" * 64,
        "source_doc_id": 2,
        "source_content_sha256": "b" * 64,
        "source_observation_version": "2025-01-30T12:00:00+00:00",
        "source_period_end": "2024-12-31",
        "evidence_node_id": "node-2",
        "evidence_locator_sha256": SOURCE_EVIDENCE_LOCATOR.canonical_sha256,
        "fact_locator_sha256": hashlib.sha256(locator_json.encode()).hexdigest(),
        "source_excerpt": excerpt,
        "source_value_text": "114",
        "value": "114",
        "unit": Unit.MILLIONS,
        "locator": locator,
        "context": _context(),
        "semantic_evidence": {
            "metric_name_value": "Total customers",
            "metric_name_quote": "Total customers",
            "reported_period_end_value": "2024-12-31",
            "reported_period_quote": "Q4 2024",
            "accounting_basis_value": "management",
            "accounting_basis_quote": "Management KPI",
            "consolidation_scope_value": "consolidated",
            "consolidation_scope_quote": "Consolidated",
            "unit_scale_value": "millions",
            "unit_scale_quote": "figures in millions",
            "dimension_values": {},
            "dimension_quotes": {},
        },
        "expected_inserted_fact_rows": 1,
        "expected_inserted_context_rows": 1,
    }
    values.update(changes)
    return refresh.RefreshEntry.model_validate(values)


def _manifest() -> refresh.RefreshManifest:
    return refresh.RefreshManifest(
        schema_version="kpi_semantic_refresh.v5",
        user_id="bhanu",
        logical_idempotency_key="nu:2024q4:total-customers:source-review:v1",
        reviewer="owner",
        knowledge_at=NOW,
        review_bundle_sha256="d" * 64,
        expected_schema_revision="0032_allow_source_reviewed_kpi_supersessions",
        backup_restore_evidence_id="e" * 64,
        entries=(_entry(),),
    )


def _v7_definition(**changes: object) -> IssuerKpiDefinitionRevision:
    values: dict[str, object] = {
        "kpi_definition_revision_id": "definition-total-customers-r1",
        "idempotency_key": "definition:nu:total-customers:r1",
        "kpi_definition_id": 1,
        "reporting_entity_id": "entity-nu",
        "revision": 1,
        "status": KpiDefinitionStatus.ADMITTED,
        "lifecycle": KpiDefinitionLifecycle.ACTIVE,
        "reported_label": "Total customers",
        "reported_definition_text": "Total customers",
        "definition_text_status": KpiDefinitionTextStatus.VERBATIM,
        "period_kind": KpiDefinitionPeriodKind.INSTANT,
        "stock_flow_behavior": KpiStockFlowBehavior.STOCK,
        "unit_family": KpiUnitFamily.CURRENCY,
        "unit_key": Unit.MILLIONS,
        "unit_scale": KpiUnitScale.MILLIONS,
        "currency_disposition": KpiCurrencyDisposition.EXPLICIT,
        "currency": Currency.USD,
        "accounting_basis": KpiAccountingBasis.MANAGEMENT,
        "consolidation_scope": KpiConsolidationScope.CONSOLIDATED,
        "dimensions": {},
        "source_document_version_id": "document-v2",
        "source_evidence_node_id": "node-2",
        "source_locator": SOURCE_EVIDENCE_LOCATOR.model_dump(mode="json"),
        "reviewed_by": "owner",
        "effective_at": datetime(2024, 12, 31, tzinfo=UTC),
        "knowledge_at": NOW,
        "recorded_at": NOW,
    }
    values.update(changes)
    return IssuerKpiDefinitionRevision.model_validate(values)


def _v7_manifest(**entry_changes: object) -> refresh.RefreshManifest:
    definition = _v7_definition()
    entry = _entry(
        currency=Currency.USD,
        definition_revision=definition,
        expected_definition_head_id=None,
        expected_definition_revision=0,
        **entry_changes,
    )
    return refresh.RefreshManifest(
        schema_version="kpi_semantic_refresh.v7",
        user_id="bhanu",
        logical_idempotency_key="nu:2024q4:total-customers:definition-review:v1",
        reviewer="owner",
        knowledge_at=NOW,
        review_bundle_sha256="d" * 64,
        expected_schema_revision="0039_add_dcf_forecast_series",
        backup_restore_evidence_id="e" * 64,
        entries=(entry,),
    )


def _v7_relation() -> KpiDefinitionComparabilityRevision:
    return KpiDefinitionComparabilityRevision(
        comparability_revision_id="relation-prior-new-r1",
        idempotency_key="relation:prior:new:r1",
        predecessor_definition_revision_id="prior-definition-r1",
        successor_definition_revision_id="definition-total-customers-r1",
        revision=1,
        relation_kind=KpiDefinitionRelationKind.RENAMED,
        disposition=KpiDefinitionComparabilityDisposition.CONTINUOUS,
        reason_code="issuer_disclosed_rename",
        reviewed_by="owner",
        source_document_version_id="document-v2",
        source_evidence_node_id="node-2",
        source_locator=SOURCE_EVIDENCE_LOCATOR.model_dump(mode="json"),
        effective_at=datetime(2024, 12, 31, tzinfo=UTC),
        knowledge_at=NOW,
        recorded_at=NOW,
    )


def test_manifest_binds_locator_excerpt_and_expected_row_effects() -> None:
    entry = _entry()
    stale_schema = _manifest().model_dump(mode="json")
    stale_schema["schema_version"] = "kpi_semantic_refresh.v4"
    with pytest.raises(ValidationError, match=r"kpi_semantic_refresh\.v5"):
        refresh.RefreshManifest.model_validate(stale_schema)
    with pytest.raises(ValidationError, match="fact locator hash mismatch"):
        _entry(fact_locator_sha256="f" * 64)
    with pytest.raises(ValidationError, match="supersede must expect one fact row"):
        _entry(expected_inserted_fact_rows=0)
    wrong_basis = dict(_entry().semantic_evidence.model_dump(mode="json"))
    wrong_basis["accounting_basis_value"] = "gaap"
    with pytest.raises(
        ValidationError, match="accounting-basis evidence value must match semantic context"
    ):
        _entry(semantic_evidence=wrong_basis)
    assert entry.locator.verbatim_snippet == entry.source_excerpt
    manifest = _manifest()
    serialized_v5 = json.loads(manifest.model_dump_json())
    assert "predecessor_resolution_state" not in serialized_v5["entries"][0]
    assert manifest.content_sha256() == _manifest().content_sha256()
    legacy_v5 = _manifest().model_dump(mode="json")
    assert (
        refresh.RefreshManifest.model_validate(legacy_v5).entries[0].predecessor_resolution_state
        == "canonical_current"
    )
    with pytest.raises(
        ValidationError, match="quarantined legacy predecessors may only be superseded"
    ):
        _entry(
            action="bind_existing",
            predecessor_resolution_state="quarantined_legacy",
            source_doc_id=1,
            source_content_sha256="a" * 64,
            expected_inserted_fact_rows=0,
        )
    with pytest.raises(
        ValidationError,
        match="v5 supports canonical-current predecessors only",
    ):
        refresh.RefreshManifest.model_validate(
            {
                **_manifest().model_dump(mode="json"),
                "entries": [
                    _entry(predecessor_resolution_state="quarantined_legacy").model_dump(
                        mode="json"
                    )
                ],
            }
        )
    quarantined = refresh.RefreshManifest.model_validate(
        {
            **_manifest().model_dump(mode="json"),
            "schema_version": "kpi_semantic_refresh.v6",
            "entries": [
                _entry(predecessor_resolution_state="quarantined_legacy").model_dump(mode="json")
            ],
        }
    )
    assert quarantined.schema_version == "kpi_semantic_refresh.v6"
    assert (
        json.loads(quarantined.model_dump_json())["entries"][0]["predecessor_resolution_state"]
        == "quarantined_legacy"
    )
    missing_v6_state = json.loads(quarantined.model_dump_json())
    missing_v6_state["entries"][0].pop("predecessor_resolution_state")
    with pytest.raises(ValidationError, match="v6 requires predecessor_resolution_state"):
        refresh.RefreshManifest.model_validate(missing_v6_state)


def test_v7_manifest_rejects_duplicate_relation_identity_and_pair() -> None:
    relation = _v7_relation()
    with pytest.raises(ValidationError, match="comparability revision identities"):
        _v7_manifest(comparability_revisions=(relation, relation))

    reversed_relation = relation.model_copy(
        update={
            "comparability_revision_id": "relation-new-prior-r1",
            "idempotency_key": "relation:new:prior:r1",
            "predecessor_definition_revision_id": relation.successor_definition_revision_id,
            "successor_definition_revision_id": relation.predecessor_definition_revision_id,
        }
    )
    with pytest.raises(ValidationError, match="comparability pairs"):
        _v7_manifest(comparability_revisions=(relation, reversed_relation))


def test_v5_manifest_serialization_and_hash_match_predecessor_contract(tmp_path: Path) -> None:
    manifest = _manifest()
    legacy_entry = _entry().model_dump(mode="json")
    legacy_entry.pop("predecessor_resolution_state")
    for field in (
        "expected_definition_head_id",
        "expected_definition_revision",
        "definition_revision",
        "comparability_revisions",
    ):
        legacy_entry.pop(field)
    expected_payload = {
        "schema_version": "kpi_semantic_refresh.v5",
        "user_id": "bhanu",
        "logical_idempotency_key": "nu:2024q4:total-customers:source-review:v1",
        "reviewer": "owner",
        "knowledge_at": NOW.isoformat().replace("+00:00", "Z"),
        "review_bundle_sha256": "d" * 64,
        "expected_schema_revision": "0032_allow_source_reviewed_kpi_supersessions",
        "backup_restore_evidence_id": "e" * 64,
        "entries": [legacy_entry],
    }
    assert manifest.model_dump(mode="json") == expected_payload
    assert json.loads(manifest.model_dump_json()) == expected_payload
    assert manifest.content_sha256() == refresh.canonical_sha256(expected_payload)
    assert (
        manifest.content_sha256()
        == "5c093b45192615fd8236e161a4cd9436e349d6bcd638d2489372da83cfa8a061"  # pragma: allowlist secret -- fixed legacy contract digest
    )

    output = tmp_path / "legacy-v5.json"
    write_content_addressed(output, manifest.model_dump_json(indent=2))
    assert json.loads(output.read_text(encoding="utf-8")) == expected_payload


def test_manifest_knowledge_time_rejects_future_decision_authority() -> None:
    boundary = _manifest().model_copy(update={"knowledge_at": NOW + timedelta(minutes=5)})
    refresh.validate_manifest_knowledge_time(boundary, now=NOW)

    future = _manifest().model_copy(
        update={"knowledge_at": NOW + timedelta(minutes=5, microseconds=1)}
    )
    with pytest.raises(refresh.RepairBlockedError, match="manifest_knowledge_at_from_future"):
        refresh.validate_manifest_knowledge_time(future, now=NOW)


def test_dry_run_blocks_future_manifest_before_external_evidence_or_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    future_manifest = _manifest().model_copy(
        update={"knowledge_at": datetime.now(UTC) + timedelta(hours=1)}
    )
    manifest_path = tmp_path / "future-manifest.json"
    manifest_path.write_text(future_manifest.model_dump_json(), encoding="utf-8")
    placeholder = tmp_path / "placeholder.json"
    placeholder.write_text("{}", encoding="utf-8")

    def _parse_bundle(_payload: str | bytes | bytearray) -> OperationsReviewBundle:
        return OperationsReviewBundle.model_construct()

    def _parse_backup(_payload: str | bytes | bytearray) -> BackupRestoreReadinessReceipt:
        return BackupRestoreReadinessReceipt.model_construct()

    def _parse_pins(_payload: str | bytes | bytearray) -> WindowsReviewPins:
        return WindowsReviewPins.model_construct()

    monkeypatch.setattr(
        refresh.OperationsReviewBundle,
        "model_validate_json",
        staticmethod(_parse_bundle),
    )
    monkeypatch.setattr(
        refresh.BackupRestoreReadinessReceipt,
        "model_validate_json",
        staticmethod(_parse_backup),
    )
    monkeypatch.setattr(
        refresh.WindowsReviewPins,
        "model_validate_json",
        staticmethod(_parse_pins),
    )

    def _unexpected_external_evidence(**_kwargs: object) -> None:
        raise AssertionError("future manifest reached external evidence validation")

    monkeypatch.setattr(refresh, "_validate_external_evidence", _unexpected_external_evidence)
    receipt_root = tmp_path / "receipts"
    result = refresh.main(
        [
            "--manifest",
            str(manifest_path),
            "--user-id",
            future_manifest.user_id,
            "--db",
            str(tmp_path / "must-not-open.db"),
            "--review-bundle",
            str(placeholder),
            "--trusted-review-pins",
            str(placeholder),
            "--backup-restore-receipt",
            str(placeholder),
            "--receipt-root",
            str(receipt_root),
        ]
    )

    assert result == 2
    assert not (tmp_path / "must-not-open.db").exists()
    receipt_files = tuple((receipt_root / "attempts").glob("*.json"))
    assert len(receipt_files) == 1
    receipt = KpiRepairAttemptReceipt.model_validate_json(receipt_files[0].read_text())
    assert receipt.state == "blocked"
    assert receipt.blocker_codes == ("manifest_knowledge_at_from_future",)


@pytest.mark.parametrize(
    ("unit", "scale"),
    [
        (Unit.THOUSANDS, KpiUnitScale.THOUSANDS),
        (Unit.MILLIONS, KpiUnitScale.MILLIONS),
        (Unit.BILLIONS, KpiUnitScale.BILLIONS),
        (Unit.ACTUAL, KpiUnitScale.NONE),
        (Unit.PERCENT, KpiUnitScale.NONE),
        (Unit.RATIO, KpiUnitScale.NONE),
        (Unit.BPS, KpiUnitScale.NONE),
        (Unit.COUNT, KpiUnitScale.NONE),
        (Unit.COUNT, KpiUnitScale.THOUSANDS),
        (Unit.COUNT, KpiUnitScale.MILLIONS),
        (Unit.COUNT, KpiUnitScale.BILLIONS),
    ],
)
def test_manifest_requires_persisted_unit_to_match_semantic_scale(
    unit: Unit, scale: KpiUnitScale
) -> None:
    context = _context().model_copy(update={"unit_scale": scale})
    evidence = _entry().semantic_evidence.model_copy(update={"unit_scale_value": scale})
    assert _entry(unit=unit, context=context, semantic_evidence=evidence).unit is unit


@pytest.mark.parametrize(
    ("scale", "expected"),
    [
        (KpiUnitScale.NONE, Decimal("114")),
        (KpiUnitScale.THOUSANDS, Decimal("114000")),
        (KpiUnitScale.MILLIONS, Decimal("114000000")),
        (KpiUnitScale.BILLIONS, Decimal("114000000000")),
    ],
)
def test_repair_source_binding_uses_normalized_count_value(
    scale: KpiUnitScale, expected: Decimal
) -> None:
    assert normalize_source_numeric(Decimal("114"), unit=Unit.COUNT, unit_scale=scale) == expected


@pytest.mark.parametrize(
    ("unit", "scale"),
    [
        (Unit.ACTUAL, KpiUnitScale.MILLIONS),
        (Unit.MILLIONS, KpiUnitScale.NONE),
        (Unit.THOUSANDS, KpiUnitScale.MILLIONS),
        (Unit.BILLIONS, KpiUnitScale.MILLIONS),
        (Unit.PERCENT, KpiUnitScale.MILLIONS),
        (Unit.RATIO, KpiUnitScale.THOUSANDS),
        (Unit.BPS, KpiUnitScale.BILLIONS),
    ],
)
def test_manifest_rejects_persisted_unit_semantic_scale_mismatch(
    unit: Unit, scale: KpiUnitScale
) -> None:
    context = _context().model_copy(update={"unit_scale": scale})
    evidence = _entry().semantic_evidence.model_copy(update={"unit_scale_value": scale})
    with pytest.raises(ValidationError, match="persisted fact unit must match semantic unit scale"):
        _entry(unit=unit, context=context, semantic_evidence=evidence)


def test_attempt_and_sol_receipts_are_content_addressed_and_tamper_evident() -> None:
    manifest = _manifest()
    attempt = seal_attempt(
        attempt_id="1" * 32,
        logical_idempotency_key_sha256="2" * 64,
        manifest_sha256=manifest.content_sha256(),
        review_bundle_sha256=manifest.review_bundle_sha256,
        backup_restore_evidence_id=manifest.backup_restore_evidence_id,
        executor_code_sha256="5" * 64,
        mode="dry_run",
        state="passed",
        started_at=NOW,
        completed_at=NOW,
        validated_entries=1,
        inserted_fact_rows=1,
        inserted_context_rows=1,
        blocker_codes=(),
        result_fact_head_ids=(11,),
    )
    judgment = seal_judgment(
        manifest_sha256=manifest.content_sha256(),
        dry_run_receipt_sha256=attempt.content_sha256,
        review_bundle_sha256=manifest.review_bundle_sha256,
        executor_code_sha256="5" * 64,
        purpose="kpi_source_repair",
        rubric_version="kpi-repair-v1",
        evidence_tier="J2",
        judge_model="gpt-5.6-sol",
        judge_run_id="sol-review-1",
        prompt_sha256="3" * 64,
        response_sha256="4" * 64,
        verdict="PASS",
        findings=(),
        observed_at=NOW,
        issuance_identity_sha256="6" * 64,
    )
    assert KpiRepairAttemptReceipt.model_validate_json(attempt.model_dump_json()) == attempt
    assert KpiRepairJudgeReceipt.model_validate_json(judgment.model_dump_json()) == judgment
    tampered = json.loads(judgment.model_dump_json())
    tampered["verdict"] = "BLOCK"
    with pytest.raises(ValidationError, match="hash mismatch"):
        KpiRepairJudgeReceipt.model_validate(tampered)


def test_v3_attempt_receipt_records_exact_definition_effects_without_changing_v2() -> None:
    legacy = seal_attempt(
        attempt_id="1" * 32,
        logical_idempotency_key_sha256="2" * 64,
        manifest_sha256="3" * 64,
        review_bundle_sha256="4" * 64,
        backup_restore_evidence_id="5" * 64,
        executor_code_sha256="6" * 64,
        mode="apply",
        state="applied",
        started_at=NOW,
        completed_at=NOW,
        validated_entries=1,
        inserted_fact_rows=0,
        inserted_context_rows=1,
        blocker_codes=(),
        result_fact_head_ids=(10,),
    )
    definition = seal_attempt(
        **legacy.model_dump(mode="python", exclude={"schema_version", "content_sha256"}),
        inserted_definition_rows=1,
        inserted_comparability_rows=0,
        result_definition_revision_ids=("definition-r1",),
        result_definition_commitment_sha256s=("7" * 64,),
    )

    assert legacy.schema_version == "kpi_repair_attempt.v2"
    legacy_payload = json.loads(legacy.model_dump_json())
    assert "inserted_definition_rows" not in legacy_payload
    assert "result_definition_revision_ids" not in legacy_payload
    assert definition.schema_version == "kpi_repair_attempt.v3"
    assert definition.inserted_definition_rows == 1
    assert definition.result_definition_revision_ids == ("definition-r1",)
    assert definition.result_definition_commitment_sha256s == ("7" * 64,)

    tampered = definition.model_dump(mode="json")
    tampered["result_definition_commitment_sha256s"] = ["8" * 64]
    with pytest.raises(ValidationError, match="hash mismatch"):
        KpiRepairAttemptReceipt.model_validate(tampered)


@pytest.mark.parametrize(
    "dependency",
    (
        "execution/fetch_windows_review_bundle.py",
        "execution/backup_restore_readiness_receipt.py",
        "src/operations/review_bundle.py",
    ),
)
def test_repair_code_seal_changes_with_authority_dependency(
    tmp_path: Path, dependency: str
) -> None:
    path = tmp_path / dependency
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("VERSION = 1\n", encoding="utf-8")
    before = repair_executor_code_sha256(tmp_path)
    path.write_text("VERSION = 2\n", encoding="utf-8")
    assert repair_executor_code_sha256(tmp_path) != before


def test_external_evidence_rejects_stale_review_and_scheduler_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = _manifest()
    db_path = tmp_path / "restored.db"
    bundle = _review_bundle(
        manifest,
        db_path,
        observed_at=NOW - timedelta(hours=1),
    )
    backup = _backup(manifest)
    monkeypatch.setattr(refresh, "validate_receipt_for_source", _no_receipt_reasons)
    monkeypatch.setattr(refresh, "validate_pinned_identity", _accept_pinned_identity)
    with pytest.raises(refresh.RepairBlockedError, match="review_bundle_stale"):
        refresh.validate_external_repair_evidence(
            manifest=manifest,
            db_path=db_path,
            review_bundle=bundle,
            trusted_pins=_pins(),
            backup=backup,
            now=NOW,
            max_review_age=timedelta(minutes=20),
        )
    bundle = _review_bundle(
        manifest,
        db_path,
        scheduler_recorded_at=NOW - timedelta(hours=1),
    )
    with pytest.raises(refresh.RepairBlockedError, match="scheduler_runtime_evidence_stale"):
        refresh.validate_external_repair_evidence(
            manifest=manifest,
            db_path=db_path,
            review_bundle=bundle,
            trusted_pins=_pins(),
            backup=backup,
            now=NOW,
            max_review_age=timedelta(minutes=20),
        )


def test_external_evidence_rejects_untrusted_host_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = _manifest()
    bundle = OperationsReviewBundle.model_construct(content_sha256=manifest.review_bundle_sha256)

    def reject(**_kwargs: object) -> None:
        raise ValueError("trusted_host_identity_mismatch")

    monkeypatch.setattr(refresh, "validate_pinned_identity", reject)
    with pytest.raises(refresh.RepairBlockedError, match="trusted_review_pin_mismatch"):
        refresh.validate_external_repair_evidence(
            manifest=manifest,
            db_path=tmp_path / "unused.db",
            review_bundle=bundle,
            trusted_pins=_pins(),
            backup=_backup(manifest),
            now=NOW,
            max_review_age=timedelta(minutes=20),
        )


def test_source_binding_requires_exact_document_node_locator_excerpt_and_value() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE documents (
          id INTEGER PRIMARY KEY,ticker TEXT,source_type TEXT,doc_type TEXT,period_end TEXT,
          sha256 TEXT,fetched_at TEXT,file_path TEXT
        );
        CREATE TABLE evidence_document_versions (
          document_version_id TEXT PRIMARY KEY,legacy_document_id INTEGER,blob_sha256 TEXT,
          ticker TEXT
        );
        CREATE TABLE evidence_extraction_runs (
          extraction_run_id TEXT PRIMARY KEY,document_version_id TEXT,extractor_name TEXT,
          extractor_config_sha256 TEXT,extractor_code_version TEXT,outcome TEXT
        );
        CREATE TABLE evidence_nodes (
          node_id TEXT PRIMARY KEY,extraction_run_id TEXT,text TEXT,locator_json TEXT,
          locator_sha256 TEXT,node_kind TEXT
        );
        CREATE TABLE v_legacy_document_evidence_bindings_current (
          legacy_document_id INTEGER,document_version_id TEXT,evidence_node_id TEXT,
          scope_content_sha256 TEXT
        );
        """
    )
    conn.execute(
        "INSERT INTO documents VALUES (?,?,?,?,?,?,?,?)",
        (
            2,
            "NU",
            "ir_doc",
            "ir_presentation",
            "2024-12-31",
            "b" * 64,
            "2025-01-30T12:00:00+00:00",
            "ir_documents/NU/q4.pdf",
        ),
    )
    conn.execute(
        "INSERT INTO evidence_document_versions VALUES ('version-2',2,?,'NU')", ("b" * 64,)
    )
    conn.execute(
        "INSERT INTO evidence_extraction_runs VALUES (?,?,?,?,?,?)",
        (
            "run-2",
            "version-2",
            PDF_FULLTEXT_EXTRACTOR.name,
            PDF_FULLTEXT_EXTRACTOR.config_sha256,
            PDF_FULLTEXT_EXTRACTOR.code_version,
            "succeeded",
        ),
    )
    conn.execute(
        "INSERT INTO evidence_nodes VALUES (?,?,?,?,?,?),(?,?,?,?,?,?)",
        (
            "root-2",
            "run-2",
            "NU Q4 2024 investor presentation.",
            SOURCE_EVIDENCE_LOCATOR.canonical_json,
            SOURCE_EVIDENCE_LOCATOR.canonical_sha256,
            "document",
            "node-2",
            "run-2",
            "Q4 2024 | Total customers | Management KPI | Consolidated | "
            "figures in millions | Total customers reached 114 million.",
            SOURCE_EVIDENCE_LOCATOR.canonical_json,
            SOURCE_EVIDENCE_LOCATOR.canonical_sha256,
            "pdf_page",
        ),
    )
    conn.execute(
        "INSERT INTO v_legacy_document_evidence_bindings_current VALUES (2,'version-2','root-2',?)",
        ("b" * 64,),
    )
    source_type, source_ticker = validate_source_binding(conn, _entry())
    assert source_type.value == "ir_doc"
    assert source_ticker == "NU"
    conn.execute("UPDATE documents SET doc_type='ir_historical_spreadsheet',period_end=NULL")
    validate_source_binding(conn, _entry(source_period_end=None))
    conn.execute("UPDATE documents SET doc_type='ir_presentation'")
    with pytest.raises(refresh.RepairBlockedError, match="source_period_mismatch"):
        validate_source_binding(conn, _entry(source_period_end=None))
    conn.execute("UPDATE documents SET doc_type='ir_supplement'")
    with pytest.raises(refresh.RepairBlockedError, match="source_period_mismatch"):
        validate_source_binding(conn, _entry(source_period_end=None))
    conn.execute(
        "UPDATE documents SET doc_type='ir_historical_spreadsheet',period_end='2024-12-31'"
    )
    with pytest.raises(refresh.RepairBlockedError, match="source_period_mismatch"):
        validate_source_binding(conn, _entry(source_period_end=None))
    conn.execute("UPDATE documents SET doc_type='ir_presentation'")
    count_entry = _entry(unit=Unit.COUNT, value="114000000")
    validate_source_binding(conn, count_entry)
    with pytest.raises(refresh.RepairBlockedError, match="source_value_mismatch"):
        validate_source_binding(conn, count_entry.model_copy(update={"value": Decimal("114")}))
    conn.execute(
        "UPDATE v_legacy_document_evidence_bindings_current SET document_version_id='other-version'"
    )
    with pytest.raises(
        refresh.RepairBlockedError, match="source_evidence_binding_version_mismatch"
    ):
        validate_source_binding(conn, _entry())
    conn.execute(
        "UPDATE v_legacy_document_evidence_bindings_current SET document_version_id='version-2'"
    )
    conn.execute(
        "UPDATE v_legacy_document_evidence_bindings_current SET scope_content_sha256=?",
        ("d" * 64,),
    )
    with pytest.raises(
        refresh.RepairBlockedError, match="source_evidence_binding_content_mismatch"
    ):
        validate_source_binding(conn, _entry())
    conn.execute(
        "UPDATE v_legacy_document_evidence_bindings_current SET scope_content_sha256=?",
        ("b" * 64,),
    )
    conn.execute("UPDATE evidence_nodes SET node_kind='section' WHERE node_id='root-2'")
    with pytest.raises(refresh.RepairBlockedError, match="source_evidence_binding_not_document"):
        validate_source_binding(conn, _entry())
    conn.execute("UPDATE evidence_nodes SET node_kind='document' WHERE node_id='root-2'")
    conn.execute("UPDATE evidence_extraction_runs SET outcome='failed'")
    with pytest.raises(refresh.RepairBlockedError, match="evidence_extraction_not_succeeded"):
        validate_source_binding(conn, _entry())
    conn.execute("UPDATE evidence_extraction_runs SET outcome='succeeded'")
    conn.execute("UPDATE evidence_extraction_runs SET extractor_name='unreviewed-extractor'")
    with pytest.raises(refresh.RepairBlockedError, match="evidence_extractor_not_promoted"):
        validate_source_binding(conn, _entry())
    conn.execute(
        "UPDATE evidence_extraction_runs SET extractor_name=?",
        (PDF_FULLTEXT_EXTRACTOR.name,),
    )
    conn.execute("UPDATE evidence_nodes SET node_kind='document' WHERE node_id='node-2'")
    with pytest.raises(refresh.RepairBlockedError, match="evidence_node_not_substantive"):
        validate_source_binding(conn, _entry())
    conn.execute("UPDATE evidence_nodes SET node_kind='pdf_page' WHERE node_id='node-2'")
    conn.execute("UPDATE evidence_document_versions SET ticker='WIX'")
    with pytest.raises(refresh.RepairBlockedError, match="evidence_document_issuer_mismatch"):
        validate_source_binding(conn, _entry())
    conn.execute("UPDATE evidence_document_versions SET ticker='NU'")
    conn.execute(
        "UPDATE evidence_document_versions SET blob_sha256=?",
        ("d" * 64,),
    )
    with pytest.raises(refresh.RepairBlockedError, match="evidence_document_content_mismatch"):
        validate_source_binding(conn, _entry())
    conn.execute(
        "UPDATE evidence_document_versions SET blob_sha256=?",
        ("b" * 64,),
    )
    changed_locator = FactLocator(
        kind=LocatorKind.PDF_SLIDE,
        pdf_page=7,
        verbatim_snippet="Total customers reached 115 million.",
    )
    changed_locator_json = changed_locator.to_json()
    assert changed_locator_json is not None
    with pytest.raises(refresh.RepairBlockedError, match="source_excerpt_mismatch"):
        validate_source_binding(
            conn,
            _entry(
                source_excerpt="Total customers reached 115 million.",
                source_value_text="115",
                value="115",
                locator=changed_locator,
                fact_locator_sha256=hashlib.sha256(changed_locator_json.encode()).hexdigest(),
            ),
        )
    conn.close()


def test_changed_fact_chain_head_blocks_before_any_repair_write() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE documents (id INTEGER PRIMARY KEY,ticker TEXT,sha256 TEXT);
        CREATE TABLE kpi_definitions (
          id INTEGER PRIMARY KEY,ticker TEXT,name TEXT,unit TEXT
        );
        CREATE TABLE kpi_facts (
          id INTEGER PRIMARY KEY,ticker TEXT,period_end TEXT,fiscal_period_type TEXT,
          kpi_definition_id INTEGER,value TEXT,unit TEXT,source_doc_id INTEGER,
          supersedes_id INTEGER
        );
        INSERT INTO documents VALUES (1,'NU','aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa');
        INSERT INTO kpi_definitions VALUES (7,'NU','Total customers','millions');
        INSERT INTO kpi_facts VALUES (10,'NU','2024-12-31','Q4',7,'95','millions',1,NULL);
        INSERT INTO kpi_facts VALUES (11,'NU','2024-12-31','Q4',7,'114','millions',1,10);
        """
    )
    with pytest.raises(refresh.RepairBlockedError, match="fact_chain_head_changed"):
        refresh.validate_refresh_entry(conn, _entry(), {7})
    assert conn.execute("SELECT COUNT(*) FROM kpi_facts").fetchone()[0] == 2
    conn.close()


def _entry_validation_db(*, definition_ticker: str, definition_unit: str) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE documents (id INTEGER PRIMARY KEY,ticker TEXT,sha256 TEXT);
        CREATE TABLE kpi_definitions (
          id INTEGER PRIMARY KEY,ticker TEXT,name TEXT,unit TEXT
        );
        CREATE TABLE kpi_facts (
          id INTEGER PRIMARY KEY,ticker TEXT,period_end TEXT,fiscal_period_type TEXT,
          kpi_definition_id INTEGER,value TEXT,unit TEXT,source_doc_id INTEGER,
          supersedes_id INTEGER
        );
        INSERT INTO documents VALUES (
          1,'NU','aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'
        );
        INSERT INTO kpi_facts VALUES (
          10,'NU','2024-12-31','Q4',7,'114','millions',1,NULL
        );
        """
    )
    conn.execute(
        "INSERT INTO kpi_definitions VALUES (7,?,'Total customers',?)",
        (definition_ticker, definition_unit),
    )
    return conn


def test_quarantined_predecessor_requires_exact_owner_scope_and_noncanonical_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _entry_validation_db(definition_ticker="NU", definition_unit="millions")
    conn.execute("CREATE VIEW v_kpi_facts_resolved_current AS SELECT * FROM kpi_facts WHERE id<>10")
    monkeypatch.setattr(refresh, "_validate_source_binding", _source_nu)
    entry = _entry(predecessor_resolution_state="quarantined_legacy")

    row, source_type = refresh.validate_refresh_entry(
        conn,
        entry,
        set(),
        owner_tickers=frozenset({"NU"}),
    )

    assert int(row["id"]) == 10
    assert source_type is refresh.SourceType.IR_DOC
    with pytest.raises(
        refresh.RepairBlockedError,
        match="quarantined_predecessor_outside_owner_portfolio",
    ):
        refresh.validate_refresh_entry(conn, entry, set(), owner_tickers=frozenset())
    conn.execute("DROP VIEW v_kpi_facts_resolved_current")
    conn.execute("CREATE VIEW v_kpi_facts_resolved_current AS SELECT * FROM kpi_facts")
    with pytest.raises(refresh.RepairBlockedError, match="quarantined_predecessor_is_canonical"):
        refresh.validate_refresh_entry(
            conn,
            entry,
            set(),
            owner_tickers=frozenset({"NU"}),
        )
    conn.close()


@pytest.mark.parametrize("action", ["bind_existing", "supersede"])
def test_entry_rejects_cross_issuer_source_for_every_action(
    action: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = _entry_validation_db(definition_ticker="NU", definition_unit="millions")
    monkeypatch.setattr(
        refresh,
        "_validate_source_binding",
        _source_wix,
    )
    changes: dict[str, object] = {"action": action}
    if action == "bind_existing":
        changes.update(
            source_doc_id=1,
            source_content_sha256="a" * 64,
            expected_inserted_fact_rows=0,
        )
    with pytest.raises(refresh.RepairBlockedError, match="source_issuer_mismatch"):
        refresh.validate_refresh_entry(conn, _entry(**changes), {7})
    conn.close()


@pytest.mark.parametrize("action", ["bind_existing", "supersede"])
def test_entry_rejects_cross_issuer_definition_for_every_action(
    action: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = _entry_validation_db(definition_ticker="WIX", definition_unit="millions")
    monkeypatch.setattr(
        refresh,
        "_validate_source_binding",
        _source_nu,
    )
    changes: dict[str, object] = {"action": action}
    if action == "bind_existing":
        changes.update(
            source_doc_id=1,
            source_content_sha256="a" * 64,
            expected_inserted_fact_rows=0,
        )
    with pytest.raises(refresh.RepairBlockedError, match="source_issuer_mismatch"):
        refresh.validate_refresh_entry(conn, _entry(**changes), {7})
    conn.close()


@pytest.mark.parametrize("action", ["bind_existing", "supersede"])
def test_entry_rejects_definition_unit_mismatch_for_every_action(
    action: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = _entry_validation_db(definition_ticker="NU", definition_unit="actual")
    monkeypatch.setattr(
        refresh,
        "_validate_source_binding",
        _source_nu,
    )
    changes: dict[str, object] = {"action": action}
    if action == "bind_existing":
        changes.update(
            source_doc_id=1,
            source_content_sha256="a" * 64,
            expected_inserted_fact_rows=0,
        )
    with pytest.raises(refresh.RepairBlockedError, match="definition_unit_mismatch"):
        refresh.validate_refresh_entry(conn, _entry(**changes), {7})
    conn.close()


def test_cli_requires_explicit_database_path() -> None:
    parser = refresh.build_parser()
    db_action = next(action for action in parser._actions if action.dest == "db")
    receipt_action = next(action for action in parser._actions if action.dest == "receipt_root")
    assert db_action.required is True
    assert receipt_action.required is True


def test_missing_marker_recovers_exact_committed_postcondition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        "CREATE TABLE kpi_facts (id INTEGER PRIMARY KEY);"
        "INSERT INTO kpi_facts VALUES (10);"
        "CREATE VIEW v_kpi_facts_resolved_current AS SELECT * FROM kpi_facts;"
    )
    entry = _entry(
        action="bind_existing",
        source_doc_id=1,
        source_content_sha256="a" * 64,
        expected_inserted_fact_rows=0,
    )
    manifest = _manifest().model_copy(update={"entries": (entry,)})

    checked: list[int] = []

    def _exact_postcondition(
        _conn: sqlite3.Connection,
        *,
        manifest: refresh.RefreshManifest,
        entry: refresh.RefreshEntry,
        head_id: int,
    ) -> None:
        del manifest, entry
        checked.append(head_id)

    monkeypatch.setattr(refresh, "_validate_applied_entry_postcondition", _exact_postcondition)
    assert detect_applied_postcondition(conn, manifest=manifest) == (10,)
    assert checked == [10]
    conn.close()


def test_marker_replay_rejects_unrelated_canonical_head(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    locator_json = _entry().locator.to_json()
    assert locator_json is not None
    conn.executescript(
        "CREATE TABLE documents (id INTEGER PRIMARY KEY,sha256 TEXT);"
        "CREATE TABLE kpi_facts ("
        "id INTEGER PRIMARY KEY,source_doc_id INTEGER,value TEXT,unit TEXT,currency TEXT,"
        "supersedes_id INTEGER,source_excerpt TEXT,locator TEXT,extracted_by TEXT);"
        "INSERT INTO documents VALUES (2,'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb');"
        "CREATE VIEW v_kpi_facts_resolved_current AS SELECT * FROM kpi_facts;"
    )
    conn.execute(
        "INSERT INTO kpi_facts VALUES (?,?,?,?,?,?,?,?,?)",
        (10, 2, "95", "millions", None, None, None, None, "legacy"),
    )
    conn.execute(
        "INSERT INTO kpi_facts VALUES (?,?,?,?,?,?,?,?,?)",
        (
            11,
            2,
            "114",
            "millions",
            None,
            10,
            _entry().source_excerpt,
            locator_json,
            "source_review:owner",
        ),
    )
    conn.execute(
        "INSERT INTO kpi_facts VALUES (?,?,?,?,?,?,?,?,?)",
        (
            12,
            2,
            "114",
            "millions",
            None,
            None,
            _entry().source_excerpt,
            locator_json,
            "source_review:owner",
        ),
    )

    def _current_context(
        _conn: sqlite3.Connection, *, kpi_fact_id: int
    ) -> KpiSemanticContextRevision:
        return KpiSemanticContextRevision(
            id=1,
            kpi_fact_id=kpi_fact_id,
            revision=1,
            context=context_for_entry(_entry()),
            reviewed_by="owner",
            knowledge_at=NOW,
        )

    monkeypatch.setattr(refresh, "current_kpi_semantic_context", _current_context)
    monkeypatch.setattr(refresh, "_validate_source_binding", _source_nu)
    with pytest.raises(refresh.RepairBlockedError, match="replay_fact_postcondition_mismatch"):
        verify_replay(
            conn,
            manifest=_manifest(),
            result_heads=(12,),
            result_definition_revision_ids=(None,),
            result_definition_commitment_sha256s=(None,),
        )
    verify_replay(
        conn,
        manifest=_manifest(),
        result_heads=(11,),
        result_definition_revision_ids=(None,),
        result_definition_commitment_sha256s=(None,),
    )
    conn.close()


def test_v7_marker_replay_requires_exact_definition_identity_and_commitment() -> None:
    manifest = _v7_manifest()
    definition = manifest.entries[0].definition_revision
    assert definition is not None
    conn = sqlite3.connect(":memory:")

    with pytest.raises(
        refresh.RepairBlockedError,
        match="idempotency_marker_definition_binding_mismatch",
    ):
        verify_replay(
            conn,
            manifest=manifest,
            result_heads=(10,),
            result_definition_revision_ids=(definition.kpi_definition_revision_id,),
            result_definition_commitment_sha256s=("f" * 64,),
        )
    with pytest.raises(
        refresh.RepairBlockedError,
        match="idempotency_marker_definition_binding_mismatch",
    ):
        verify_replay(
            conn,
            manifest=manifest,
            result_heads=(10,),
            result_definition_revision_ids=("unrelated-definition",),
            result_definition_commitment_sha256s=(definition.commitment_sha256,),
        )
    conn.close()


def test_v2_marker_is_bound_to_exact_sealed_apply_receipt(tmp_path: Path) -> None:
    manifest = _v7_manifest()
    definition = manifest.entries[0].definition_revision
    assert definition is not None
    logical_sha = hashlib.sha256(manifest.logical_idempotency_key.encode()).hexdigest()
    receipt = seal_attempt(
        attempt_id="1" * 32,
        logical_idempotency_key_sha256=logical_sha,
        manifest_sha256=manifest.content_sha256(),
        review_bundle_sha256=manifest.review_bundle_sha256,
        backup_restore_evidence_id=manifest.backup_restore_evidence_id,
        executor_code_sha256="2" * 64,
        mode="apply",
        state="applied",
        started_at=NOW,
        completed_at=NOW,
        validated_entries=1,
        inserted_fact_rows=1,
        inserted_context_rows=1,
        inserted_definition_rows=1,
        inserted_comparability_rows=0,
        blocker_codes=(),
        result_fact_head_ids=(11,),
        result_definition_revision_ids=(definition.kpi_definition_revision_id,),
        result_definition_commitment_sha256s=(definition.commitment_sha256,),
    )
    attempts = tmp_path / "attempts"
    attempts.mkdir()
    (attempts / f"{receipt.attempt_id}.json").write_text(receipt.model_dump_json())
    marker: dict[str, object] = {
        "schema_version": "kpi_repair_idempotency.v2",
        "apply_attempt_id": receipt.attempt_id,
        "logical_idempotency_key_sha256": logical_sha,
        "manifest_sha256": manifest.content_sha256(),
        "apply_receipt_sha256": receipt.content_sha256,
        "inserted_definition_rows": 1,
        "inserted_comparability_rows": 0,
        "result_fact_head_ids": [11],
        "result_definition_revision_ids": [definition.kpi_definition_revision_id],
        "result_definition_commitment_sha256s": [definition.commitment_sha256],
    }

    assert validate_v2_idempotency_marker(
        marker,
        receipt_root=tmp_path,
        logical_key_sha256=logical_sha,
        manifest_sha256=manifest.content_sha256(),
        review_bundle_sha256=manifest.review_bundle_sha256,
        backup_restore_evidence_id=manifest.backup_restore_evidence_id,
        executor_code_sha256="2" * 64,
    ) == (
        (11,),
        (definition.kpi_definition_revision_id,),
        (definition.commitment_sha256,),
    )

    for field in ("review_bundle_sha256", "backup_restore_evidence_id", "executor_code_sha256"):
        bindings = {
            "review_bundle_sha256": manifest.review_bundle_sha256,
            "backup_restore_evidence_id": manifest.backup_restore_evidence_id,
            "executor_code_sha256": "2" * 64,
        }
        bindings[field] = "f" * 64
        with pytest.raises(refresh.RepairBlockedError, match="apply_receipt_mismatch"):
            validate_v2_idempotency_marker(
                marker,
                receipt_root=tmp_path,
                logical_key_sha256=logical_sha,
                manifest_sha256=manifest.content_sha256(),
                **bindings,
            )

    marker["inserted_definition_rows"] = 9
    with pytest.raises(refresh.RepairBlockedError, match="apply_receipt_mismatch"):
        validate_v2_idempotency_marker(
            marker,
            receipt_root=tmp_path,
            logical_key_sha256=logical_sha,
            manifest_sha256=manifest.content_sha256(),
            review_bundle_sha256=manifest.review_bundle_sha256,
            backup_restore_evidence_id=manifest.backup_restore_evidence_id,
            executor_code_sha256="2" * 64,
        )
    marker["apply_attempt_id"] = "../outside"
    with pytest.raises(refresh.RepairBlockedError, match="apply_attempt_invalid"):
        validate_v2_idempotency_marker(
            marker,
            receipt_root=tmp_path,
            logical_key_sha256=logical_sha,
            manifest_sha256=manifest.content_sha256(),
            review_bundle_sha256=manifest.review_bundle_sha256,
            backup_restore_evidence_id=manifest.backup_restore_evidence_id,
            executor_code_sha256="2" * 64,
        )


def test_bind_existing_fails_when_exact_fact_cannot_resolve(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = sqlite3.connect(":memory:")
    row = conn.execute("SELECT 1").fetchone()
    assert row is not None
    entry = _entry(
        action="bind_existing",
        source_doc_id=1,
        source_content_sha256="a" * 64,
        expected_inserted_fact_rows=0,
    )
    manifest = _manifest().model_copy(update={"entries": (entry,)})

    def persist_context(
        _conn: sqlite3.Connection,
        *,
        kpi_fact_id: int,
        context: KpiSemanticContext,
        reviewed_by: str = "pipeline",
        knowledge_at: datetime | None = None,
        kpi_definition_revision_id: str | None = None,
    ) -> int:
        del kpi_fact_id, context, reviewed_by, knowledge_at, kpi_definition_revision_id
        return 1

    monkeypatch.setattr(refresh, "persist_kpi_semantic_context", persist_context)

    def reject_resolution(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("wrong selected observation")

    monkeypatch.setattr(refresh, "require_canonical_kpi_resolution", reject_resolution)
    with pytest.raises(refresh.RepairBlockedError, match="canonical_fact_resolution_failed"):
        apply_entry(
            conn,
            manifest=manifest,
            entry=entry,
            row=row,
            source_type=refresh.SourceType.IR_DOC,
        )
    conn.close()


def test_result_heads_must_all_exist_in_canonical_relation() -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        "CREATE TABLE kpi_facts (id INTEGER PRIMARY KEY);"
        "INSERT INTO kpi_facts VALUES (10);"
        "CREATE VIEW v_kpi_facts_resolved_current AS "
        "SELECT * FROM kpi_facts WHERE 0;"
    )
    with pytest.raises(refresh.RepairBlockedError, match="result_fact_not_canonically_resolved"):
        require_canonical_result_heads(conn, result_heads=(10,))
    conn.close()


@pytest.mark.parametrize(
    ("cli_user_id", "expected_result", "expiry_stage", "apply"),
    [
        ("bhanu", 0, None, False),
        ("default", 2, None, False),
        ("bhanu", 2, "proof", False),
        ("bhanu", 2, "clone", False),
        ("bhanu", 2, "mutation", False),
        ("bhanu", 0, None, True),
        ("bhanu", 2, "proof", True),
        ("bhanu", 2, "mutation", True),
        ("bhanu", 2, "judge", True),
        ("bhanu", 2, "failed_postwrite", True),
        ("bhanu", 2, "proof_backwards", False),
        ("bhanu", 2, "clone_backwards", False),
        ("bhanu", 2, "mutation_backwards", True),
        ("bhanu", 2, "diagnostics", False),
        ("bhanu", 2, "diagnostics", True),
        ("bhanu", 0, "replay", True),
        ("bhanu", 0, "replay_missing_marker", True),
        ("bhanu", 0, "replay_artifact", True),
        ("bhanu", 0, "replay_scope", True),
        ("bhanu", 0, "replay_postcondition", True),
        ("bhanu", 0, "replay_code", True),
        ("bhanu", 0, "replay_issuer", True),
        ("bhanu", 0, "replay_definition", True),
    ],
)
def test_repair_command_binds_owner_scope_and_enforces_current_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cli_user_id: str,
    expected_result: int,
    expiry_stage: str | None,
    apply: bool,
) -> None:
    replay_test = expiry_stage is not None and expiry_stage.startswith("replay")
    clock = [
        NOW + timedelta(seconds=800) if expiry_stage and expiry_stage.endswith("backwards") else NOW
    ]

    def now(_tz: object) -> datetime:
        return clock[0]

    monkeypatch.setattr(refresh, "datetime", SimpleNamespace(now=now))
    monkeypatch.setattr(refresh, "validate_pinned_identity", _accept_pinned_identity)
    original_diagnostics = refresh.emit_repair_phase_diagnostics

    def diagnostics(
        *,
        phase: str,
        started_monotonic: float,
        review_bundle: OperationsReviewBundle,
        max_review_age: timedelta,
        now: datetime,
    ) -> None:
        if expiry_stage == "diagnostics" and phase == "post_write_checks":
            clock[0] = NOW + timedelta(seconds=901)
        original_diagnostics(
            phase=phase,
            started_monotonic=started_monotonic,
            review_bundle=review_bundle,
            max_review_age=max_review_age,
            now=now,
        )

    if expiry_stage == "diagnostics":
        monkeypatch.setattr(refresh, "emit_repair_phase_diagnostics", diagnostics)
    manifest = _v7_manifest() if replay_test else _manifest()
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(manifest.model_dump_json(), encoding="utf-8")
    review_path = tmp_path / "review.json"
    review_path.write_text("{}", encoding="utf-8")
    backup_path = tmp_path / "backup.json"
    backup_path.write_text("{}", encoding="utf-8")
    pins_path = tmp_path / "pins.json"
    pins_path.write_text("{}", encoding="utf-8")
    db_path = tmp_path / "disposable.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE alembic_version(version_num TEXT)")
    conn.execute("INSERT INTO alembic_version VALUES (?)", (manifest.expected_schema_revision,))
    conn.execute("CREATE TABLE dry_run_probe(value TEXT)")
    if replay_test:
        conn.execute(
            "CREATE TABLE kpi_facts(id INTEGER, ticker TEXT, kpi_definition_id INTEGER, value TEXT)"
        )
    conn.commit()
    conn.close()
    snapshot_path = tmp_path / "verified-snapshot.db"
    snapshot_path.write_bytes(db_path.read_bytes())
    live_token_before = sqlite_file_token(db_path)

    fake_bundle = _review_bundle(manifest, db_path).model_copy(
        update={
            "identity": ReviewIdentity.model_construct(
                database_instance_sha256=hashlib.sha256(b"test-lineage").hexdigest()
            )
        }
    )
    fake_backup = _backup(manifest).model_copy(
        update={
            "snapshot_resolved_path": str(snapshot_path),
            "snapshot_byte_size": snapshot_path.stat().st_size,
            "snapshot_sha256": hashlib.sha256(snapshot_path.read_bytes()).hexdigest(),
        }
    )

    def _parse_bundle(_payload: str | bytes | bytearray) -> OperationsReviewBundle:
        return fake_bundle

    def _parse_backup(_payload: str | bytes | bytearray) -> BackupRestoreReadinessReceipt:
        return fake_backup

    def _parse_pins(_payload: str | bytes | bytearray) -> WindowsReviewPins:
        return _pins()

    monkeypatch.setattr(
        refresh.OperationsReviewBundle,
        "model_validate_json",
        staticmethod(_parse_bundle),
    )
    monkeypatch.setattr(
        refresh.BackupRestoreReadinessReceipt,
        "model_validate_json",
        staticmethod(_parse_backup),
    )
    monkeypatch.setattr(
        refresh.WindowsReviewPins,
        "model_validate_json",
        staticmethod(_parse_pins),
    )

    replay_started = [False]
    artifact_checks: list[bool] = []
    evidence_db_paths: list[Path] = []
    lock_owned = [False]
    events: list[str] = []

    def _accept_external_evidence(**kwargs: object) -> datetime:
        assert lock_owned[0]
        if replay_started[0]:
            raise refresh.RepairBlockedError("backup_restore_source_content_changed")
        events.append("proof")
        evidence_db_paths.append(Path(str(kwargs["db_path"])).resolve())
        if expiry_stage == "proof":
            clock[0] = NOW + timedelta(seconds=901)
        elif expiry_stage and expiry_stage.endswith("backwards"):
            clock[0] = NOW + timedelta(seconds=850)
            if expiry_stage == "proof_backwards":
                clock[0] = NOW + timedelta(seconds=830)
                return NOW + timedelta(seconds=850)
        return clock[0]

    original_repair_database = getattr(refresh, "_repair_database")

    @contextmanager
    def clone_database(**kwargs: object) -> Generator[Path, None, None]:
        assert lock_owned[0]
        events.append("clone")
        with original_repair_database(**kwargs) as clone:
            if expiry_stage == "clone":
                clock[0] = NOW + timedelta(seconds=901)
            elif expiry_stage == "clone_backwards":
                clock[0] = NOW + timedelta(seconds=830)
            yield clone

    monkeypatch.setattr(refresh, "_repair_database", clone_database)

    def _test_lineage(_conn: sqlite3.Connection) -> str:
        return "test-lineage"

    monkeypatch.setattr(refresh, "_validate_external_evidence", _accept_external_evidence)
    monkeypatch.setattr(refresh, "database_lineage_identity", _test_lineage)

    opened_paths: list[Path] = []

    def open_test_db(path: Path) -> sqlite3.Connection:
        opened_paths.append(path.resolve())
        connection = sqlite3.connect(path)
        connection.row_factory = sqlite3.Row
        return connection

    monkeypatch.setattr(refresh, "open_db", open_test_db)

    @contextmanager
    def _job_lock(*_args: object, **_kwargs: object) -> Generator[None, None, None]:
        lock_owned[0] = True
        try:
            yield
        finally:
            lock_owned[0] = False

    monkeypatch.setattr(
        refresh,
        "JobLock",
        _job_lock,
    )

    def _scoped_definitions(
        _conn: sqlite3.Connection, *, repo_root: Path, user_id: str
    ) -> tuple[ScopedKpiDefinition, ...]:
        del repo_root
        assert user_id == "bhanu"
        return (ScopedKpiDefinition.model_construct(kpi_definition_id=1 if replay_test else 641),)

    monkeypatch.setattr(
        refresh,
        "scoped_kpi_definitions",
        _scoped_definitions,
    )

    def _validated_entry(
        connection: sqlite3.Connection,
        _entry_value: refresh.RefreshEntry,
        _allowed: set[int],
        *,
        owner_tickers: frozenset[str],
        owner_user_id: str | None = None,
    ) -> tuple[sqlite3.Row, refresh.SourceType]:
        assert _allowed == ({1} if replay_test else {641})
        assert owner_tickers == frozenset()
        assert owner_user_id == "bhanu"
        row = connection.execute(
            "SELECT 'NU' AS ticker, '2024-12-31' AS period_end, "
            "'Q4' AS fiscal_period_type, 'Total customers' AS name"
        ).fetchone()
        assert isinstance(row, sqlite3.Row)
        return row, refresh.SourceType.IR_DOC

    monkeypatch.setattr(
        refresh,
        "_validate_entry",
        _validated_entry,
    )

    def simulated_apply(connection: sqlite3.Connection, **_kwargs: object) -> EntryEffectView:
        assert not replay_started[0], "committed replay must not mutate facts"
        connection.execute("INSERT INTO dry_run_probe VALUES ('would-write')")
        if replay_test:
            connection.execute("INSERT INTO kpi_facts VALUES (11, 'NU', 1, '114')")
        if expiry_stage == "mutation":
            clock[0] = NOW + timedelta(seconds=901)
        elif expiry_stage == "judge":
            clock[0] = NOW + timedelta(seconds=2)
        elif expiry_stage == "mutation_backwards":
            clock[0] = NOW + timedelta(seconds=830)
        return EntryEffect(
            inserted_fact_rows=1,
            inserted_context_rows=1,
            inserted_definition_rows=int(replay_test),
            inserted_comparability_rows=0,
            fact_head_id=11,
            definition_revision_id=manifest.entries[
                0
            ].definition_revision.kpi_definition_revision_id
            if manifest.entries[0].definition_revision
            else None,
            definition_commitment_sha256=manifest.entries[0].definition_revision.commitment_sha256
            if manifest.entries[0].definition_revision
            else None,
        )

    monkeypatch.setattr(refresh, "_apply_entry", simulated_apply)

    def accept_canonical_heads(_conn: sqlite3.Connection, *, result_heads: tuple[int, ...]) -> None:
        del result_heads
        if expiry_stage == "failed_postwrite":
            raise RuntimeError("synthetic failure after transactional inserts")

    monkeypatch.setattr(
        refresh,
        "_require_canonical_result_heads",
        accept_canonical_heads,
    )
    apply_arguments: list[str] = []
    if apply:
        from tests.fixtures.kpi_judge_setup import qualification_fixture

        def code_sha(_root: Path) -> str:
            return "f" * 64

        def accept_apply_authority(**_kwargs: object) -> None:
            return None

        def no_committed_postcondition(
            _conn: sqlite3.Connection, *, manifest: refresh.RefreshManifest
        ) -> tuple[int, ...] | None:
            del manifest
            if replay_test and _conn.execute("SELECT COUNT(*) FROM dry_run_probe").fetchone()[0]:
                return (11,)
            return None

        monkeypatch.setattr(refresh, "repair_executor_code_sha256", code_sha)
        monkeypatch.setattr(refresh, "_validate_apply_authority", accept_apply_authority)
        monkeypatch.setattr(refresh, "_detect_applied_postcondition", no_committed_postcondition)
        dry = seal_attempt(
            attempt_id="1" * 32,
            logical_idempotency_key_sha256="2" * 64,
            manifest_sha256=manifest.content_sha256(),
            review_bundle_sha256=manifest.review_bundle_sha256,
            backup_restore_evidence_id=manifest.backup_restore_evidence_id,
            executor_code_sha256=code_sha(tmp_path),
            mode="dry_run",
            state="passed",
            started_at=NOW,
            completed_at=NOW,
            validated_entries=1,
            inserted_fact_rows=1,
            inserted_context_rows=1,
            blocker_codes=(),
            result_fact_head_ids=(11,),
        )
        qualification = qualification_fixture("kpi_source_repair", NOW)
        if expiry_stage == "judge":
            qualification = qualification.model_copy(
                update={"expires_at": NOW + timedelta(seconds=1)}
            )
            qualification = qualification.model_copy(
                update={
                    "content_sha256": refresh.canonical_sha256(
                        qualification.model_dump(mode="json", exclude={"content_sha256"})
                    )
                }
            )
        judge = seal_judgment(
            schema_version="kpi_repair_judge.v3",
            manifest_sha256=manifest.content_sha256(),
            dry_run_receipt_sha256=dry.content_sha256,
            review_bundle_sha256=manifest.review_bundle_sha256,
            executor_code_sha256=code_sha(tmp_path),
            rubric_version="kpi-semantic-refresh-v7",
            evidence_tier="J3",
            judge_model="synthetic-judge",
            qualification=qualification,
            judge_run_id="synthetic-local-only",
            prompt_sha256="3" * 64,
            response_sha256="4" * 64,
            verdict="PASS",
            purpose="kpi_source_repair",
            findings=(),
            observed_at=NOW,
            issuance_identity_sha256="5" * 64,
        )
        dry_path, judge_path = tmp_path / "dry.json", tmp_path / "judge.json"
        dry_path.write_text(dry.model_dump_json())
        judge_path.write_text(judge.model_dump_json())
        apply_arguments = [
            "--apply",
            "--approved-manifest-sha256",
            manifest.content_sha256(),
            "--dry-run-receipt",
            str(dry_path),
            "--judge-receipt",
            str(judge_path),
        ]
    if replay_test:

        def committed_postcondition(connection: sqlite3.Connection, **kwargs: object) -> None:
            assert lock_owned[0]
            row = connection.execute(
                "SELECT value FROM kpi_facts WHERE id=?", (kwargs["head_id"],)
            ).fetchone()
            if row is None or row[0] != "114":
                raise refresh.RepairBlockedError("replay_fact_postcondition_mismatch")

        monkeypatch.setattr(
            refresh, "_validate_applied_entry_postcondition", committed_postcondition
        )
        monkeypatch.setattr(refresh, "_validate_source_binding", _source_nu)

        def immutable_backup_guard(
            _backup: BackupRestoreReadinessReceipt, **kwargs: object
        ) -> tuple[str, ...]:
            assert lock_owned[0]
            assert kwargs["source_db"] == db_path
            assert kwargs["source_revision"] == manifest.expected_schema_revision
            artifact_checks.append(bool(kwargs["require_current_identity"]))
            assert kwargs["require_current_identity"] is False
            if (
                hashlib.sha256(snapshot_path.read_bytes()).hexdigest()
                != fake_backup.snapshot_sha256
            ):
                return ("backup_restore_snapshot_identity_mismatch",)
            return ()

        monkeypatch.setattr(refresh, "validate_receipt_for_source", immutable_backup_guard)

    receipt_root = tmp_path / "receipts"
    arguments = [
        "--manifest",
        str(manifest_path),
        "--user-id",
        cli_user_id,
        "--db",
        str(db_path),
        "--review-bundle",
        str(review_path),
        "--trusted-review-pins",
        str(pins_path),
        "--backup-restore-receipt",
        str(backup_path),
        "--receipt-root",
        str(receipt_root),
        *apply_arguments,
    ]
    result = refresh.main(arguments)
    assert result == expected_result
    if not apply or (expiry_stage is not None and not replay_test):
        assert sqlite_file_token(db_path) == live_token_before
    with sqlite3.connect(db_path) as check:
        assert check.execute("SELECT COUNT(*) FROM dry_run_probe").fetchone()[0] == int(
            apply and (expiry_stage is None or replay_test)
        )
    receipt_files = tuple((receipt_root / "attempts").glob("*.json"))
    assert len(receipt_files) == 1
    receipt = KpiRepairAttemptReceipt.model_validate_json(receipt_files[0].read_text())
    if cli_user_id == "bhanu":
        assert evidence_db_paths == [db_path.resolve()]
        assert events[0] == "proof"
        if expiry_stage in {"proof", "proof_backwards"}:
            assert events == ["proof"]
            assert opened_paths == []
        else:
            assert events == ["proof", "clone"]
            assert len(opened_paths) == 1
            if apply:
                assert opened_paths[0] == db_path.resolve()
            else:
                assert opened_paths[0] != db_path.resolve()
                assert not opened_paths[0].exists()
        if expiry_stage is None or replay_test:
            assert receipt.state == ("applied" if apply else "passed")
            assert receipt.inserted_fact_rows == 1
            assert receipt.inserted_context_rows == 1
        else:
            assert receipt.state == ("failed" if expiry_stage == "failed_postwrite" else "blocked")
            assert receipt.inserted_fact_rows == receipt.inserted_context_rows == 0
            assert receipt.inserted_definition_rows == receipt.inserted_comparability_rows == 0
            assert receipt.result_fact_head_ids == ()
            assert receipt.result_definition_revision_ids == ()
            assert receipt.result_definition_commitment_sha256s == ()
            assert receipt.blocker_codes == (
                "unexpected_RuntimeError"
                if expiry_stage == "failed_postwrite"
                else "judge_receipt_not_authorizing"
                if expiry_stage == "judge"
                else "repair_clock_moved_backwards"
                if expiry_stage.endswith("backwards")
                else "scheduler_runtime_evidence_stale",
            )
    else:
        assert evidence_db_paths == []
        assert receipt.state == "blocked"
        assert receipt.blocker_codes == ("manifest_user_identity_mismatch",)
        assert receipt.inserted_fact_rows == 0
        assert receipt.inserted_context_rows == 0

    if replay_test:
        # The successful apply changes the source; the original source proof
        # can no longer authorize writes. Exact committed replay must be read-only.
        assert db_path.read_bytes() != snapshot_path.read_bytes()
        replay_started[0] = True
        marker_path = next((receipt_root / "by_logical_key").glob("*.json"))
        original_marker = marker_path.read_bytes()
        if expiry_stage == "replay_missing_marker":
            marker_path.unlink()
        elif expiry_stage == "replay_artifact":
            snapshot_path.write_bytes(b"changed immutable rollback artifact")
        elif expiry_stage == "replay_scope":

            def no_scope(
                _conn: sqlite3.Connection, **_kwargs: object
            ) -> tuple[ScopedKpiDefinition, ...]:
                return ()

            monkeypatch.setattr(refresh, "scoped_kpi_definitions", no_scope)
        elif expiry_stage == "replay_postcondition":
            with sqlite3.connect(db_path) as changed:
                changed.execute("UPDATE kpi_facts SET value='115' WHERE id=11")
        elif expiry_stage == "replay_issuer":
            with sqlite3.connect(db_path) as changed:
                changed.execute("UPDATE kpi_facts SET ticker='UNRELATED' WHERE id=11")
        elif expiry_stage == "replay_definition":
            with sqlite3.connect(db_path) as changed:
                changed.execute("UPDATE kpi_facts SET kpi_definition_id=999 WHERE id=11")
        elif expiry_stage == "replay_code":

            def changed_code(_root: Path) -> str:
                return "c" * 64

            monkeypatch.setattr(refresh, "repair_executor_code_sha256", changed_code)
        source_before_replay = db_path.read_bytes()
        source_token = sqlite_file_token(db_path)
        opened_before = tuple(opened_paths)
        replay_result = refresh.main(arguments)
        expected_blocker = {
            "replay_artifact": "backup_restore_snapshot_identity_mismatch",
            "replay_scope": "fact_outside_owner_visible_scope",
            "replay_postcondition": "replay_fact_postcondition_mismatch",
            "replay_code": "judge_receipt_not_authorizing",
            "replay_issuer": "replay_source_issuer_mismatch",
            "replay_definition": "replay_definition_root_changed",
        }.get(str(expiry_stage))
        assert replay_result == (2 if expected_blocker else 0)
        replay_receipt = KpiRepairAttemptReceipt.model_validate_json(
            (receipt_root / "latest.json").read_text()
        )
        assert replay_receipt.state == ("blocked" if expected_blocker else "replayed")
        assert replay_receipt.blocker_codes == ((expected_blocker,) if expected_blocker else ())
        assert replay_receipt.inserted_fact_rows == replay_receipt.inserted_context_rows == 0
        assert db_path.read_bytes() == source_before_replay
        assert sqlite_file_token(db_path) == source_token
        assert tuple(opened_paths) == opened_before
        assert evidence_db_paths == [db_path.resolve()]
        if expected_blocker is None:
            assert replay_receipt.result_fact_head_ids == (11,)
            assert artifact_checks == [False]
            if expiry_stage != "replay_missing_marker":
                assert marker_path.read_bytes() == original_marker


def test_dry_run_rejects_corrupted_snapshot_clone_before_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    live_db = tmp_path / "live.db"
    snapshot = tmp_path / "snapshot.db"
    live_db.write_bytes(b"live")
    snapshot.write_bytes(b"verified snapshot")
    backup = _backup(_manifest()).model_copy(
        update={
            "snapshot_resolved_path": str(snapshot),
            "snapshot_byte_size": snapshot.stat().st_size,
            "snapshot_sha256": hashlib.sha256(snapshot.read_bytes()).hexdigest(),
        }
    )
    original_copy = refresh.shutil.copy2

    def _corrupting_copy(source: Path, destination: Path) -> Path:
        copied = original_copy(source, destination)
        destination.write_bytes(destination.read_bytes() + b"corrupt")
        return Path(copied)

    monkeypatch.setattr(refresh.shutil, "copy2", _corrupting_copy)
    with (
        pytest.raises(
            refresh.RepairBlockedError,
            match="backup_restore_snapshot_clone_identity_mismatch",
        ),
        refresh.repair_database_authority(live_db=live_db, backup=backup, apply=False),
    ):
        pytest.fail("corrupted clone must not be yielded for opening")


@pytest.mark.parametrize("capture_definition", [False, True])
@pytest.mark.parametrize("capture_quarantine", [False, True])
def test_migrated_db_applies_quarantined_count_correction_and_rolls_back_dry_run(
    migrated_db: Callable[..., Path],
    tmp_path: Path,
    capture_definition: bool,
    capture_quarantine: bool,
) -> None:
    db_path = migrated_db(tmp_path / "same-source-kpi-repair.db")
    conn = refresh.open_db(db_path)
    try:
        conn.execute(
            "INSERT INTO documents "
            "(id,ticker,source_type,doc_type,period_end,file_path,sha256,fetched_at,"
            "fetch_status,raw_bytes_size,source_quality_tier) "
            "VALUES (1,'NU','ir_doc','ir_transcript','2024-12-31',"
            "'ir_documents/NU/q4.pdf',?,?,'ok',1,'fmp_normalized')",
            ("a" * 64, NOW.isoformat()),
        )
        conn.execute(
            "INSERT INTO documents "
            "(id,ticker,source_type,doc_type,period_end,file_path,sha256,fetched_at,"
            "fetch_status,raw_bytes_size,parent_document_id,source_quality_tier) "
            "VALUES (2,'NU','llm_extracted','llm_summary','2024-12-31',"
            "'.tmp/NU_Q4_2024_summary.txt',?,?,'ok',1,1,'fmp_normalized')",
            ("c" * 64, NOW.isoformat()),
        )
        evidence_text = (
            "Q4 2024 | Total customers | Management KPI | Consolidated | "
            "figures in millions | Total customers reached 114.2 million."
        )
        locator_json = SOURCE_EVIDENCE_LOCATOR.canonical_json
        locator_sha = SOURCE_EVIDENCE_LOCATOR.canonical_sha256
        conn.execute(
            "INSERT INTO issuer_entities VALUES (?,?,?,?)",
            ("issuer-nu", "issuer:nu", "operating_company", NOW.isoformat()),
        )
        conn.execute(
            "INSERT INTO reporting_entities "
            "(reporting_entity_id,idempotency_key,issuer_id,reporting_entity_kind,"
            "display_name,created_at) VALUES (?,?,?,?,?,?)",
            (
                "reporting-nu",
                "reporting:nu",
                "issuer-nu",
                "legal_registrant",
                "Nu Holdings Ltd.",
                NOW.isoformat(),
            ),
        )
        conn.execute(
            "INSERT INTO evidence_content_blobs VALUES (?,?,?,?,?)",
            ("a" * 64, 1, "application/pdf", "https://example.invalid/nu-q4", NOW.isoformat()),
        )
        conn.execute(
            "INSERT INTO evidence_source_observations "
            "(observation_id,idempotency_key,source_kind,source_url,blob_sha256,"
            "observed_at,retrieved_at,retrieval_config_sha256,collector_code_version) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (
                "source-nu-q4",
                "source:nu:q4",
                "ir_document",
                "https://example.invalid/nu-q4",
                "a" * 64,
                NOW.isoformat(),
                NOW.isoformat(),
                "b" * 64,
                "test-v1",
            ),
        )
        conn.execute(
            "INSERT INTO evidence_document_versions "
            "(document_version_id,document_key,version_sequence,observation_id,blob_sha256,"
            "issuer_id,ticker,document_type,form_type,period_end,language,legacy_document_id,"
            "recorded_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "document-nu-q4",
                "document:nu:q4",
                1,
                "source-nu-q4",
                "a" * 64,
                "issuer-nu",
                "NU",
                "earnings_release",
                "earnings_release",
                "2024-12-31",
                "en",
                1,
                NOW.isoformat(),
            ),
        )
        conn.execute(
            "INSERT INTO evidence_extraction_runs VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                "run-nu-q4",
                "run:nu:q4",
                "document-nu-q4",
                "a" * 64,
                PDF_FULLTEXT_EXTRACTOR.name,
                PDF_FULLTEXT_EXTRACTOR.config_sha256,
                PDF_FULLTEXT_EXTRACTOR.code_version,
                "d" * 64,
                NOW.isoformat(),
                NOW.isoformat(),
                "succeeded",
            ),
        )
        conn.execute(
            "INSERT INTO evidence_nodes VALUES (?,?,?,?,?,?,?,?,?,?,?),(?,?,?,?,?,?,?,?,?,?,?)",
            (
                "root-nu-q4",
                "root:nu:q4",
                1,
                "run-nu-q4",
                None,
                None,
                "document",
                "NU Q4 2024 earnings transcript.",
                locator_json,
                locator_sha,
                NOW.isoformat(),
                "node-nu-q4",
                "node:nu:q4",
                1,
                "run-nu-q4",
                None,
                None,
                "pdf_page",
                evidence_text,
                locator_json,
                locator_sha,
                NOW.isoformat(),
            ),
        )
        conn.execute(
            "INSERT INTO legacy_document_evidence_binding_revisions VALUES "
            "(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "binding-nu-q4",
                "binding:nu:q4",
                1,
                1,
                "document-nu-q4",
                "root-nu-q4",
                locator_json,
                locator_sha,
                "a" * 64,
                NOW.isoformat(),
                NOW.isoformat(),
                NOW.isoformat(),
                None,
            ),
        )
        conn.execute(
            "INSERT INTO kpi_definitions (id,ticker,name,unit,primary_source) "
            "VALUES (641,'NU','Total customers (millions)','count','ir_doc')"
        )
        trigger_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' "
            "AND name='trg_kpi_facts_observation_insert'"
        ).fetchone()[0]
        assert isinstance(trigger_sql, str)
        conn.execute("DROP TRIGGER trg_kpi_facts_observation_insert")
        conn.execute(
            "INSERT INTO kpi_facts "
            "(id,ticker,period_end,fiscal_period_type,kpi_definition_id,value,unit,currency,"
            "source_doc_id,confidence,extracted_by) "
            "VALUES (42175,'NU','2024-12-31','Q4',641,'95','count',NULL,2,0.9,'legacy')"
        )
        conn.execute(trigger_sql)
        if capture_quarantine:
            conn.execute(
                "INSERT INTO tracked_companies(ticker,name,list_type,user_id) "
                "VALUES ('NU','Nu Holdings','portfolio','bhanu')"
            )
            quarantine_manifest = prepare_kpi_semantic_disposition_manifest(
                conn,
                repo_root=tmp_path,
                user_id="bhanu",
                reviewer="owner",
                logical_idempotency_key="nu:legacy-quarantine:v1",
                expected_schema_revision=expected_head(),
                review_bundle_sha256="d" * 64,
                backup_restore_evidence_id="e" * 64,
                knowledge_at=NOW,
                legacy_fact_requests=(
                    LegacyKpiQuarantineRequest(
                        fact_id=42175, reason_code="wrong_population_legacy_summary"
                    ),
                ),
            )
            apply_kpi_semantic_disposition_manifest(
                conn, repo_root=tmp_path, manifest=quarantine_manifest
            )
        quarantined_context = current_kpi_semantic_context(conn, kpi_fact_id=42175)
        source_excerpt = "Total customers reached 114.2 million."
        locator = FactLocator(
            kind=LocatorKind.PDF_SLIDE,
            pdf_page=7,
            verbatim_snippet=source_excerpt,
        )
        locator_payload = locator.to_json()
        assert locator_payload is not None
        entry_changes: dict[str, object] = {}
        if capture_definition:
            entry_changes.update(
                definition_revision=_v7_definition(
                    kpi_definition_id=641,
                    reporting_entity_id="reporting-nu",
                    unit_family=KpiUnitFamily.COUNT,
                    unit_key=Unit.COUNT,
                    currency_disposition=KpiCurrencyDisposition.NOT_APPLICABLE,
                    currency=None,
                    source_document_version_id="document-nu-q4",
                    source_evidence_node_id="node-nu-q4",
                    source_locator=json.loads(locator_json),
                ),
                expected_definition_head_id=None,
                expected_definition_revision=0,
            )
        entry = _entry(
            predecessor_resolution_state="quarantined_legacy",
            expected_context_head_id=(
                None if quarantined_context is None else quarantined_context.id
            ),
            expected_context_revision=(
                0 if quarantined_context is None else quarantined_context.revision
            ),
            old_fact_id=42175,
            expected_fact_head_id=42175,
            expected_old_source_doc_id=2,
            expected_old_source_sha256="c" * 64,
            source_doc_id=1,
            source_content_sha256="a" * 64,
            source_observation_version=NOW.isoformat(),
            evidence_node_id="node-nu-q4",
            evidence_locator_sha256=locator_sha,
            fact_locator_sha256=hashlib.sha256(locator_payload.encode()).hexdigest(),
            source_excerpt=source_excerpt,
            source_value_text="114.2",
            value="114200000",
            unit=Unit.COUNT,
            locator=locator,
            **entry_changes,
        )
        manifest = refresh.RefreshManifest.model_validate(
            _manifest()
            .model_copy(
                update={
                    "schema_version": (
                        "kpi_semantic_refresh.v7"
                        if capture_definition
                        else "kpi_semantic_refresh.v6"
                    ),
                    "entries": (entry,),
                }
            )
            .model_dump(mode="json")
        )
        conn.commit()
        old_before = tuple(
            conn.execute(
                "SELECT value,unit,source_doc_id,locator,source_excerpt "
                "FROM kpi_facts WHERE id=42175"
            ).fetchone()
        )
        assert old_before == (95.0, "count", 2, None, None)
        assert current_kpi_semantic_context(conn, kpi_fact_id=42175) == quarantined_context
        assert (
            conn.execute("SELECT 1 FROM v_kpi_facts_resolved_current WHERE id=42175").fetchone()
            is None
        )

        conn.execute("BEGIN")
        row, source_type = refresh.validate_refresh_entry(
            conn,
            entry,
            set(),
            owner_tickers=frozenset({"NU"}),
            owner_user_id="bhanu",
        )
        dry_run_effect = apply_entry(
            conn,
            manifest=manifest,
            entry=entry,
            row=row,
            source_type=source_type,
        )
        validate_applied_entry_postcondition(
            conn,
            manifest=manifest,
            entry=entry,
            head_id=dry_run_effect.fact_head_id,
        )
        conn.rollback()
        assert conn.execute("SELECT COUNT(*) FROM kpi_facts").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM kpi_definition_revisions").fetchone()[0] == 0
        assert (
            tuple(
                conn.execute(
                    "SELECT value,unit,source_doc_id,locator,source_excerpt "
                    "FROM kpi_facts WHERE id=42175"
                ).fetchone()
            )
            == old_before
        )

        conn.execute("BEGIN")
        conn.execute(
            "INSERT INTO kpi_facts "
            "(ticker,period_end,fiscal_period_type,kpi_definition_id,value,unit,source_doc_id,"
            "confidence,extracted_by,supersedes_id) VALUES "
            "('NU','2024-12-31','Q4',641,'113000000','count',1,1.0,'test',42175)"
        )
        with pytest.raises(refresh.RepairBlockedError, match="fact_chain_head_changed"):
            refresh.validate_refresh_entry(
                conn,
                entry,
                set(),
                owner_tickers=frozenset({"NU"}),
                owner_user_id="bhanu",
            )
        conn.rollback()

        conn.execute("BEGIN")
        persist_kpi_semantic_context(
            conn,
            kpi_fact_id=42175,
            context=_context().model_copy(
                update={
                    "status": KpiSemanticStatus.QUARANTINED,
                    "reason_code": "stale_test_context",
                }
            ),
            reviewed_by="stale-review",
            knowledge_at=NOW,
        )
        with pytest.raises(refresh.RepairBlockedError, match="semantic_context_head_changed"):
            refresh.validate_refresh_entry(
                conn,
                entry,
                set(),
                owner_tickers=frozenset({"NU"}),
                owner_user_id="bhanu",
            )
        conn.rollback()

        conn.execute("BEGIN")
        row, source_type = refresh.validate_refresh_entry(
            conn,
            entry,
            set(),
            owner_tickers=frozenset({"NU"}),
            owner_user_id="bhanu",
        )
        applied_effect = apply_entry(
            conn,
            manifest=manifest,
            entry=entry,
            row=row,
            source_type=source_type,
        )
        new_id = applied_effect.fact_head_id
        validate_applied_entry_postcondition(
            conn,
            manifest=manifest,
            entry=entry,
            head_id=applied_effect.fact_head_id,
        )
        conn.commit()
        successor = conn.execute(
            "SELECT value,currency,supersedes_id,extracted_by FROM kpi_facts WHERE id=?",
            (new_id,),
        ).fetchone()
        assert tuple(successor) == (114200000.0, None, 42175, "source_review:owner")
        semantic = current_kpi_semantic_context(conn, kpi_fact_id=new_id)
        assert semantic is not None
        assert semantic.reviewed_by == "owner"
        assert semantic.knowledge_at == NOW
        assert semantic.kpi_definition_revision_id == (
            "definition-total-customers-r1" if capture_definition else None
        )
        assert conn.execute("SELECT COUNT(*) FROM kpi_definition_revisions").fetchone()[0] == int(
            capture_definition
        )
        observation = conn.execute(
            "SELECT observation.numeric_value,observation.evidence_node_id,"
            "revision.fact_table,revision.fact_row_id "
            "FROM fact_observation_revisions revision JOIN reported_observations observation "
            "ON observation.observation_id=revision.observation_id "
            "WHERE revision.fact_table='kpi_facts' AND revision.fact_row_id=?",
            (new_id,),
        ).fetchone()
        assert tuple(observation) == ("114200000", "root-nu-q4", "kpi_facts", new_id)
        revision = conn.execute(
            "SELECT observation_id,source_document_id FROM fact_observation_revisions "
            "WHERE fact_table='kpi_facts' AND fact_row_id=?",
            (new_id,),
        ).fetchone()
        assert tuple(revision) == (f"kpi_facts:{new_id}:r1", 1)
        resolved = conn.execute(
            "SELECT id,reported_observation_id FROM v_kpi_facts_resolved_current "
            "WHERE kpi_definition_id=641 AND period_end='2024-12-31'"
        ).fetchone()
        assert tuple(resolved) == (new_id, f"kpi_facts:{new_id}:r1")
        assert (
            tuple(
                conn.execute(
                    "SELECT value,unit,source_doc_id,locator,source_excerpt "
                    "FROM kpi_facts WHERE id=42175"
                ).fetchone()
            )
            == old_before
        )
        assert current_kpi_semantic_context(conn, kpi_fact_id=42175) == quarantined_context
        assert (
            conn.execute("SELECT 1 FROM v_kpi_facts_resolved_current WHERE id=42175").fetchone()
            is None
        )
        assert conn.execute("SELECT COUNT(*) FROM kpi_legacy_disposition_captures").fetchone()[
            0
        ] == int(capture_quarantine)
        # Reconstruct the committed correction after the successor exists.
        validate_applied_entry_postcondition(conn, manifest=manifest, entry=entry, head_id=new_id)
        prior_context = _context() if quarantined_context is None else quarantined_context.context
        persist_kpi_semantic_context(
            conn,
            kpi_fact_id=42175,
            context=prior_context.model_copy(
                update={
                    "status": KpiSemanticStatus.QUARANTINED,
                    "reason_code": "later_context_review",
                }
            ),
            reviewed_by="owner",
            knowledge_at=NOW,
        )
        with pytest.raises(
            refresh.RepairBlockedError, match="replay_quarantined_predecessor_context_changed"
        ):
            validate_applied_entry_postcondition(
                conn, manifest=manifest, entry=entry, head_id=new_id
            )
    finally:
        conn.close()


def test_invalid_input_still_publishes_durable_failure_receipt(tmp_path: Path) -> None:
    invalid = tmp_path / "invalid.json"
    invalid.write_text("not-json", encoding="utf-8")
    receipt_root = tmp_path / "receipts"
    result = refresh.main(
        [
            "--manifest",
            str(invalid),
            "--user-id",
            "bhanu",
            "--db",
            str(tmp_path / "unused.db"),
            "--review-bundle",
            str(invalid),
            "--trusted-review-pins",
            str(invalid),
            "--backup-restore-receipt",
            str(invalid),
            "--receipt-root",
            str(receipt_root),
        ]
    )
    assert result == 2
    receipts = tuple((receipt_root / "attempts").glob("*.json"))
    assert len(receipts) == 1
    receipt = KpiRepairAttemptReceipt.model_validate_json(receipts[0].read_text())
    assert receipt.state == "failed"
    assert receipt.blocker_codes[0].startswith("invalid_input_")


def test_apply_authority_accepts_pinned_runtime_code_with_separate_state_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_root = tmp_path / "canonical-state"
    code_root = tmp_path / "runtime-code"
    runtime_code_identity = "a" * 64
    bundle = OperationsReviewBundle.model_construct(
        identity=ReviewIdentity.model_construct(
            code_instance_sha256=refresh.identity_sha256(runtime_code_identity)
        )
    )
    monkeypatch.setattr(refresh.sys, "platform", "win32")
    monkeypatch.setattr(refresh, "PROJECT_ROOT", code_root)
    monkeypatch.setattr(refresh, "CANONICAL_WINDOWS_STATE_ROOT", state_root)

    def code_identity(_root: Path) -> str:
        return runtime_code_identity

    monkeypatch.setattr(refresh, "review_code_identity", code_identity)

    validate_apply_authority(
        db_path=state_root / "data" / "portfolio.db",
        receipt_root=state_root / "data" / "operations" / "kpi_repairs",
        review_bundle=bundle,
    )


def test_apply_authority_rejects_unpinned_runtime_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_root = tmp_path / "canonical-state"
    bundle = OperationsReviewBundle.model_construct(
        identity=ReviewIdentity.model_construct(
            code_instance_sha256=refresh.identity_sha256("a" * 64)
        )
    )
    monkeypatch.setattr(refresh.sys, "platform", "win32")
    monkeypatch.setattr(refresh, "PROJECT_ROOT", tmp_path / "runtime-code")
    monkeypatch.setattr(refresh, "CANONICAL_WINDOWS_STATE_ROOT", state_root)

    def code_identity(_root: Path) -> str:
        return "b" * 64

    monkeypatch.setattr(refresh, "review_code_identity", code_identity)

    with pytest.raises(refresh.RepairBlockedError, match="apply_code_identity_mismatch"):
        validate_apply_authority(
            db_path=state_root / "data" / "portfolio.db",
            receipt_root=state_root / "data" / "operations" / "kpi_repairs",
            review_bundle=bundle,
        )


def test_canonical_windows_db_lock_is_owned_by_state_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_root = tmp_path / "canonical-state"
    code_root = tmp_path / "runtime-code"
    monkeypatch.setattr(refresh.sys, "platform", "win32")
    monkeypatch.setattr(refresh, "PROJECT_ROOT", code_root)
    monkeypatch.setattr(refresh, "CANONICAL_WINDOWS_STATE_ROOT", state_root)

    assert refresh.repair_lock_root(state_root / "data" / "portfolio.db") == state_root
    assert refresh.repair_lock_root(tmp_path / "disposable.db") == code_root


def test_judge_receipt_verdict_comes_only_from_structured_sol_response(tmp_path: Path) -> None:
    from tests.fixtures.kpi_judge_setup import qualification_fixture

    manifest = _manifest()
    dry_run = seal_attempt(
        attempt_id="7" * 32,
        logical_idempotency_key_sha256="8" * 64,
        manifest_sha256=manifest.content_sha256(),
        review_bundle_sha256=manifest.review_bundle_sha256,
        backup_restore_evidence_id=manifest.backup_restore_evidence_id,
        executor_code_sha256=repair_executor_code_sha256(refresh.PROJECT_ROOT),
        mode="dry_run",
        state="passed",
        started_at=NOW,
        completed_at=NOW,
        validated_entries=1,
        inserted_fact_rows=1,
        inserted_context_rows=1,
        blocker_codes=(),
        result_fact_head_ids=(11,),
    )
    dry_path = tmp_path / "dry.json"
    dry_path.write_text(dry_run.model_dump_json(), encoding="utf-8")
    qualification_path = tmp_path / "qualification.json"
    qualification_path.write_text(qualification_fixture("kpi_source_repair", NOW).model_dump_json())
    prompt_path = tmp_path / "prompt.txt"
    prompt_path.write_text("judge this source repair", encoding="utf-8")
    response_path = tmp_path / "response.json"
    response_path.write_text(
        json.dumps(
            {
                "purpose": "kpi_source_repair",
                "rubric_version": "kpi-repair-v1",
                "evidence_tier": "J2",
                "verdict": "BLOCK",
                "findings": ["source label unsupported"],
                "issued_at": NOW.isoformat(),
            }
        ),
        encoding="utf-8",
    )
    output = tmp_path / "judge.json"
    assert (
        record_judgment.main(
            [
                "--dry-run-receipt",
                str(dry_path),
                "--judge-run-id",
                "sol-test-1",
                "--judge-model",
                "synthetic-judge",
                "--qualification",
                str(qualification_path),
                "--prompt-file",
                str(prompt_path),
                "--response-file",
                str(response_path),
                "--output",
                str(output),
            ]
        )
        == 0
    )
    receipt = KpiRepairJudgeReceipt.model_validate_json(output.read_text())
    assert receipt.verdict == "BLOCK"


@pytest.mark.parametrize(
    ("elapsed_seconds", "blocker"),
    [(901, "scheduler_runtime_evidence_stale"), (1201, "review_bundle_stale")],
)
def test_external_proof_rechecks_clock_after_full_source_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    elapsed_seconds: int,
    blocker: str,
) -> None:
    manifest = _manifest()
    db_path = tmp_path / "never-opened.db"
    clock = [NOW]
    calls: list[Path] = []

    def validate_source(*_args: object, **_kwargs: object) -> tuple[str, ...]:
        calls.append(db_path)
        clock[0] = NOW + timedelta(seconds=elapsed_seconds)
        return ()

    def now(_tz: object) -> datetime:
        return clock[0]

    monkeypatch.setattr(refresh, "datetime", SimpleNamespace(now=now))
    monkeypatch.setattr(refresh, "validate_pinned_identity", _accept_pinned_identity)
    monkeypatch.setattr(refresh, "validate_receipt_for_source", validate_source)
    with pytest.raises(refresh.RepairBlockedError, match=blocker):
        refresh.validate_external_repair_evidence(
            manifest=manifest,
            db_path=db_path,
            review_bundle=_review_bundle(manifest, db_path),
            trusted_pins=_pins(),
            backup=_backup(manifest),
            now=NOW,
            max_review_age=timedelta(seconds=1200),
        )
    assert calls == [db_path]
    assert not db_path.exists()


@pytest.mark.parametrize(
    ("review_age", "scheduler_age", "requested_limit", "blocker"),
    [
        (1200, 900, 1200, None),
        (1201, 900, 1200, "review_bundle_stale"),
        (1201, 900, 3600, "review_bundle_stale"),
        (900, 901, 1200, "scheduler_runtime_evidence_stale"),
        (600, 601, 600, "scheduler_runtime_evidence_stale"),
        (601, 600, 600, "review_bundle_stale"),
    ],
)
def test_review_guard_preserves_both_deadlines_and_shorter_requested_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    review_age: int,
    scheduler_age: int,
    requested_limit: int,
    blocker: str | None,
) -> None:
    manifest = _manifest()
    bundle = _review_bundle(
        manifest,
        tmp_path / "not-opened.db",
        observed_at=NOW - timedelta(seconds=review_age),
        scheduler_recorded_at=NOW - timedelta(seconds=scheduler_age),
    )
    monkeypatch.setattr(refresh, "validate_pinned_identity", _accept_pinned_identity)

    def validate() -> None:
        refresh.validate_repair_review_preconditions(
            manifest=manifest,
            review_bundle=bundle,
            trusted_pins=_pins(),
            backup=_backup(manifest),
            now=NOW,
            max_review_age=timedelta(seconds=requested_limit),
        )

    if blocker is None:
        validate()
    else:
        with pytest.raises(refresh.RepairBlockedError, match=blocker):
            validate()


def test_review_preflight_rejects_clock_rollback_before_artifact_access(
    tmp_path: Path,
) -> None:
    manifest = _manifest()
    with pytest.raises(refresh.RepairBlockedError, match="repair_clock_moved_backwards"):
        refresh.validate_repair_review_preconditions(
            manifest=manifest,
            review_bundle=OperationsReviewBundle.model_construct(),
            trusted_pins=_pins(),
            backup=_backup(manifest),
            now=NOW - timedelta(seconds=1),
            max_review_age=timedelta(seconds=1200),
            not_before=NOW,
        )
    assert tuple(tmp_path.iterdir()) == ()


def test_review_preflight_does_not_read_receipt_controlled_artifact_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = _manifest()
    bundle = _review_bundle(manifest, tmp_path / "never-opened.db")
    backup = _backup(manifest).model_copy(
        update={"snapshot_resolved_path": str(tmp_path / "never-read.db")}
    )

    def reject_filesystem(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("cheap review preflight accessed a filesystem path")

    with monkeypatch.context() as patch:
        patch.setattr(refresh, "validate_pinned_identity", _accept_pinned_identity)
        for method in ("resolve", "stat", "open"):
            patch.setattr(Path, method, reject_filesystem)
        refresh.validate_repair_review_preconditions(
            manifest=manifest,
            review_bundle=bundle,
            trusted_pins=_pins(),
            backup=backup,
            now=NOW,
            max_review_age=timedelta(seconds=1200),
        )
    assert tuple(tmp_path.iterdir()) == ()


@pytest.mark.parametrize(
    ("elapsed", "age_limit", "review_remaining", "scheduler_remaining"),
    [(850, 5000, 350.0, 50.0), (901, 1200, 299.0, -1.0), (850, 900, 50.0, 50.0)],
)
def test_phase_diagnostics_measure_monotonic_duration_and_fixed_authority_budgets(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    elapsed: int,
    age_limit: int,
    review_remaining: float,
    scheduler_remaining: float,
) -> None:
    def monotonic() -> float:
        return 112.5

    monkeypatch.setattr(refresh, "perf_counter", monotonic)
    refresh.emit_repair_phase_diagnostics(
        phase="full_source_proof",
        started_monotonic=100.0,
        review_bundle=_review_bundle(_manifest(), Path("unused")),
        max_review_age=timedelta(seconds=age_limit),
        now=NOW + timedelta(seconds=elapsed),
    )
    assert json.loads(capsys.readouterr().err) == {
        "event": "kpi_repair_phase_completed",
        "phase": "full_source_proof",
        "duration_seconds": 12.5,
        "review_remaining_seconds": review_remaining,
        "scheduler_remaining_seconds": scheduler_remaining,
    }


@pytest.mark.parametrize("stream_error", [OSError, BrokenPipeError])
def test_phase_diagnostics_preserve_pending_repair_blocker_when_stream_fails(
    monkeypatch: pytest.MonkeyPatch,
    stream_error: type[OSError],
) -> None:
    def fail_write(_payload: str) -> int:
        raise stream_error("synthetic closed diagnostic stream")

    monkeypatch.setattr(refresh.sys, "stderr", SimpleNamespace(write=fail_write))
    with pytest.raises(refresh.RepairBlockedError, match="scheduler_runtime_evidence_stale"):
        try:
            raise refresh.RepairBlockedError("scheduler_runtime_evidence_stale")
        finally:
            refresh.emit_repair_phase_diagnostics(
                phase="post_write_checks",
                started_monotonic=0.0,
                review_bundle=OperationsReviewBundle.model_construct(),
                max_review_age=timedelta(seconds=1200),
                now=NOW,
            )
    with pytest.raises(stream_error):
        refresh.emit_repair_phase_diagnostics(
            phase="post_write_checks",
            started_monotonic=0.0,
            review_bundle=OperationsReviewBundle.model_construct(),
            max_review_age=timedelta(seconds=1200),
            now=NOW,
        )


def test_phase_diagnostics_do_not_fabricate_unavailable_budget_or_mask_rejection(
    capsys: pytest.CaptureFixture[str],
) -> None:
    refresh.emit_repair_phase_diagnostics(
        phase="receipt_publication",
        started_monotonic=0.0,
        review_bundle=OperationsReviewBundle.model_construct(),
        max_review_age=timedelta(seconds=1200),
        now=NOW,
    )
    diagnostic = json.loads(capsys.readouterr().err)
    assert diagnostic["duration_seconds"] >= 0.0
    assert diagnostic["review_remaining_seconds"] is None
    assert diagnostic["scheduler_remaining_seconds"] is None
