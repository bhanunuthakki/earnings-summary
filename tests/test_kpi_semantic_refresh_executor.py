from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable
from contextlib import nullcontext
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from execution import apply_kpi_semantic_refresh as refresh
from execution import record_kpi_repair_judgment as record_judgment
from models.facts import FactLocator, Unit
from operations.kpi_repair_receipts import (
    KpiRepairAttemptReceipt,
    KpiRepairJudgeReceipt,
    repair_executor_code_sha256,
    seal_attempt,
    seal_judgment,
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
    normalize_source_numeric,
)
from pipeline.kpi_source_review import insert_source_reviewed_kpi_supersession

NOW = datetime(2026, 8, 27, 20, tzinfo=UTC)


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
    locator = FactLocator(pdf_page=7, verbatim_snippet=excerpt)
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
        "evidence_locator_sha256": "c" * 64,
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
        schema_version="kpi_semantic_refresh.v3",
        logical_idempotency_key="nu:2024q4:total-customers:source-review:v1",
        reviewer="owner",
        knowledge_at=NOW,
        review_bundle_sha256="d" * 64,
        expected_schema_revision="0032_allow_source_reviewed_kpi_supersessions",
        backup_restore_evidence_id="e" * 64,
        entries=(_entry(),),
    )


def test_manifest_binds_locator_excerpt_and_expected_row_effects() -> None:
    entry = _entry()
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
    assert _manifest().content_sha256() == _manifest().content_sha256()


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
    identity = hashlib.sha256(str(db_path.resolve()).encode()).hexdigest()
    schema = SimpleNamespace(matches=True, actual_heads=(manifest.expected_schema_revision,))
    bundle = SimpleNamespace(
        content_sha256=manifest.review_bundle_sha256,
        observed_at=NOW - timedelta(hours=1),
        identity=SimpleNamespace(database_instance_sha256=identity),
        schema_revision=schema,
        scheduler=SimpleNamespace(
            observation=SimpleNamespace(evidence_recorded_at=NOW, state="current")
        ),
    )
    backup = SimpleNamespace(evidence_id=manifest.backup_restore_evidence_id)
    monkeypatch.setattr(refresh, "validate_receipt_for_source", lambda *args, **kwargs: ())
    monkeypatch.setattr(refresh, "validate_pinned_identity", lambda **_kwargs: None)
    with pytest.raises(refresh.RepairBlockedError, match="review_bundle_stale"):
        refresh._validate_external_evidence(
            manifest=manifest,
            db_path=db_path,
            review_bundle=bundle,
            trusted_pins=SimpleNamespace(),
            backup=backup,
            now=NOW,
            max_review_age=timedelta(minutes=20),
        )
    bundle.observed_at = NOW
    bundle.scheduler.observation.evidence_recorded_at = NOW - timedelta(hours=1)
    with pytest.raises(refresh.RepairBlockedError, match="scheduler_runtime_evidence_stale"):
        refresh._validate_external_evidence(
            manifest=manifest,
            db_path=db_path,
            review_bundle=bundle,
            trusted_pins=SimpleNamespace(),
            backup=backup,
            now=NOW,
            max_review_age=timedelta(minutes=20),
        )


def test_external_evidence_rejects_untrusted_host_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = _manifest()
    bundle = SimpleNamespace(content_sha256=manifest.review_bundle_sha256)

    def reject(**_kwargs: object) -> None:
        raise ValueError("trusted_host_identity_mismatch")

    monkeypatch.setattr(refresh, "validate_pinned_identity", reject)
    with pytest.raises(refresh.RepairBlockedError, match="trusted_review_pin_mismatch"):
        refresh._validate_external_evidence(
            manifest=manifest,
            db_path=tmp_path / "unused.db",
            review_bundle=bundle,
            trusted_pins=SimpleNamespace(),
            backup=SimpleNamespace(),
            now=NOW,
            max_review_age=timedelta(minutes=20),
        )


def test_source_binding_requires_exact_document_node_locator_excerpt_and_value() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE documents (
          id INTEGER PRIMARY KEY,ticker TEXT,source_type TEXT,period_end TEXT,
          sha256 TEXT,fetched_at TEXT
        );
        CREATE TABLE evidence_document_versions (
          document_version_id TEXT PRIMARY KEY,legacy_document_id INTEGER
        );
        CREATE TABLE evidence_extraction_runs (
          extraction_run_id TEXT PRIMARY KEY,document_version_id TEXT
        );
        CREATE TABLE evidence_nodes (
          node_id TEXT PRIMARY KEY,extraction_run_id TEXT,text TEXT,locator_sha256 TEXT
        );
        CREATE TABLE v_legacy_document_evidence_bindings_current (
          legacy_document_id INTEGER,evidence_node_id TEXT
        );
        """
    )
    conn.execute(
        "INSERT INTO documents VALUES (?,?,?,?,?,?)",
        (
            2,
            "NU",
            "ir_doc",
            "2024-12-31",
            "b" * 64,
            "2025-01-30T12:00:00+00:00",
        ),
    )
    conn.execute("INSERT INTO evidence_document_versions VALUES ('version-2',2)")
    conn.execute("INSERT INTO evidence_extraction_runs VALUES ('run-2','version-2')")
    conn.execute(
        "INSERT INTO evidence_nodes VALUES (?,?,?,?)",
        (
            "node-2",
            "run-2",
            "Q4 2024 | Total customers | Management KPI | Consolidated | "
            "figures in millions | Total customers reached 114 million.",
            "c" * 64,
        ),
    )
    conn.execute("INSERT INTO v_legacy_document_evidence_bindings_current VALUES (2,'node-2')")
    source_type, source_ticker = refresh._validate_source_binding(conn, _entry())
    assert source_type.value == "ir_doc"
    assert source_ticker == "NU"
    count_entry = _entry(unit=Unit.COUNT, value="114000000")
    refresh._validate_source_binding(conn, count_entry)
    with pytest.raises(refresh.RepairBlockedError, match="source_value_mismatch"):
        refresh._validate_source_binding(
            conn, count_entry.model_copy(update={"value": Decimal("114")})
        )
    conn.execute(
        "UPDATE v_legacy_document_evidence_bindings_current SET evidence_node_id='other-node'"
    )
    with pytest.raises(refresh.RepairBlockedError, match="source_evidence_binding_not_exact"):
        refresh._validate_source_binding(conn, _entry())
    conn.execute("UPDATE v_legacy_document_evidence_bindings_current SET evidence_node_id='node-2'")
    changed_locator = FactLocator(
        pdf_page=7, verbatim_snippet="Total customers reached 115 million."
    )
    changed_locator_json = changed_locator.to_json()
    assert changed_locator_json is not None
    with pytest.raises(refresh.RepairBlockedError, match="source_excerpt_mismatch"):
        refresh._validate_source_binding(
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
        refresh._validate_entry(conn, _entry(), {7})
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


@pytest.mark.parametrize("action", ["bind_existing", "supersede"])
def test_entry_rejects_cross_issuer_source_for_every_action(
    action: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = _entry_validation_db(definition_ticker="NU", definition_unit="millions")
    monkeypatch.setattr(
        refresh,
        "_validate_source_binding",
        lambda _conn, _entry: (refresh.SourceType.IR_DOC, "WIX"),
    )
    changes: dict[str, object] = {"action": action}
    if action == "bind_existing":
        changes.update(
            source_doc_id=1,
            source_content_sha256="a" * 64,
            expected_inserted_fact_rows=0,
        )
    with pytest.raises(refresh.RepairBlockedError, match="source_issuer_mismatch"):
        refresh._validate_entry(conn, _entry(**changes), {7})
    conn.close()


@pytest.mark.parametrize("action", ["bind_existing", "supersede"])
def test_entry_rejects_cross_issuer_definition_for_every_action(
    action: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = _entry_validation_db(definition_ticker="WIX", definition_unit="millions")
    monkeypatch.setattr(
        refresh,
        "_validate_source_binding",
        lambda _conn, _entry: (refresh.SourceType.IR_DOC, "NU"),
    )
    changes: dict[str, object] = {"action": action}
    if action == "bind_existing":
        changes.update(
            source_doc_id=1,
            source_content_sha256="a" * 64,
            expected_inserted_fact_rows=0,
        )
    with pytest.raises(refresh.RepairBlockedError, match="source_issuer_mismatch"):
        refresh._validate_entry(conn, _entry(**changes), {7})
    conn.close()


@pytest.mark.parametrize("action", ["bind_existing", "supersede"])
def test_entry_rejects_definition_unit_mismatch_for_every_action(
    action: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = _entry_validation_db(definition_ticker="NU", definition_unit="actual")
    monkeypatch.setattr(
        refresh,
        "_validate_source_binding",
        lambda _conn, _entry: (refresh.SourceType.IR_DOC, "NU"),
    )
    changes: dict[str, object] = {"action": action}
    if action == "bind_existing":
        changes.update(
            source_doc_id=1,
            source_content_sha256="a" * 64,
            expected_inserted_fact_rows=0,
        )
    with pytest.raises(refresh.RepairBlockedError, match="definition_unit_mismatch"):
        refresh._validate_entry(conn, _entry(**changes), {7})
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
    entry = _entry(
        action="bind_existing",
        source_doc_id=1,
        source_content_sha256="a" * 64,
        expected_inserted_fact_rows=0,
    )
    manifest = _manifest().model_copy(update={"entries": (entry,)})
    monkeypatch.setattr(
        refresh,
        "current_kpi_semantic_context",
        lambda *_args, **_kwargs: SimpleNamespace(context=refresh._context_for_entry(entry)),
    )
    assert refresh._detect_applied_postcondition(conn, manifest=manifest) == (10,)
    conn.close()


def test_dry_run_executes_exact_write_shape_then_rolls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = _manifest()
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
    conn.commit()
    conn.close()

    fake_bundle = SimpleNamespace(
        content_sha256=manifest.review_bundle_sha256,
        identity=SimpleNamespace(database_instance_sha256="9" * 64),
    )
    fake_backup = SimpleNamespace(evidence_id=manifest.backup_restore_evidence_id)
    monkeypatch.setattr(
        refresh.OperationsReviewBundle,
        "model_validate_json",
        staticmethod(lambda _payload: fake_bundle),
    )
    monkeypatch.setattr(
        refresh.BackupRestoreReadinessReceipt,
        "model_validate_json",
        staticmethod(lambda _payload: fake_backup),
    )
    monkeypatch.setattr(
        refresh.WindowsReviewPins,
        "model_validate_json",
        staticmethod(lambda _payload: SimpleNamespace()),
    )
    monkeypatch.setattr(refresh, "_validate_external_evidence", lambda **_kwargs: None)
    monkeypatch.setattr(refresh, "database_lineage_identity", lambda _conn: "test-lineage")
    fake_bundle.identity.database_instance_sha256 = hashlib.sha256(b"test-lineage").hexdigest()

    def open_test_db(path: Path) -> sqlite3.Connection:
        connection = sqlite3.connect(path)
        connection.row_factory = sqlite3.Row
        return connection

    monkeypatch.setattr(refresh, "open_db", open_test_db)
    monkeypatch.setattr(
        refresh,
        "JobLock",
        lambda *_args, **_kwargs: nullcontext(),
    )
    monkeypatch.setattr(
        refresh,
        "scoped_kpi_definitions",
        lambda *_args, **_kwargs: (SimpleNamespace(kpi_definition_id=7),),
    )
    monkeypatch.setattr(
        refresh,
        "_validate_entry",
        lambda *_args, **_kwargs: (
            {
                "ticker": "NU",
                "period_end": "2024-12-31",
                "fiscal_period_type": "Q4",
                "name": "Total customers",
            },
            SimpleNamespace(value="ir_doc"),
        ),
    )

    def simulated_apply(connection: sqlite3.Connection, **_kwargs: object) -> tuple[int, int, int]:
        connection.execute("INSERT INTO dry_run_probe VALUES ('would-write')")
        return 1, 1, 11

    monkeypatch.setattr(refresh, "_apply_entry", simulated_apply)
    receipt_root = tmp_path / "receipts"
    result = refresh.main(
        [
            "--manifest",
            str(manifest_path),
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
        ]
    )
    assert result == 0
    with sqlite3.connect(db_path) as check:
        assert check.execute("SELECT COUNT(*) FROM dry_run_probe").fetchone()[0] == 0
    receipt_files = tuple((receipt_root / "attempts").glob("*.json"))
    assert len(receipt_files) == 1
    receipt = KpiRepairAttemptReceipt.model_validate_json(receipt_files[0].read_text())
    assert receipt.state == "passed"
    assert receipt.inserted_fact_rows == 1
    assert receipt.inserted_context_rows == 1


def test_migrated_db_allows_same_source_count_supersession_with_review_attribution(
    migrated_db: Callable[..., Path], tmp_path: Path
) -> None:
    db_path = migrated_db(tmp_path / "same-source-kpi-repair.db")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute(
            "INSERT INTO documents "
            "(id,ticker,source_type,doc_type,period_end,file_path,sha256,fetched_at,"
            "fetch_status,raw_bytes_size,source_quality_tier) "
            "VALUES (1,'NU','ir_doc','earnings_release','2024-12-31','source.html',?,?,'ok',1,'issuer_reported')",
            ("a" * 64, NOW.isoformat()),
        )
        evidence_text = (
            "Q4 2024 | Total customers | Management KPI | Consolidated | "
            "figures in millions | Total customers reached 114.2 million."
        )
        locator_json = json.dumps({"kind": "document", "page": 7}, sort_keys=True)
        locator_sha = hashlib.sha256(locator_json.encode()).hexdigest()
        content_sha = hashlib.sha256(evidence_text.encode()).hexdigest()
        conn.execute(
            "INSERT INTO issuer_entities VALUES (?,?,?,?)",
            ("issuer-nu", "issuer:nu", "operating_company", NOW.isoformat()),
        )
        conn.execute(
            "INSERT INTO evidence_content_blobs VALUES (?,?,?,?,?)",
            ("a" * 64, 1, "text/html", "https://example.invalid/nu-q4", NOW.isoformat()),
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
                "test-extractor",
                "c" * 64,
                "test-v1",
                "d" * 64,
                NOW.isoformat(),
                NOW.isoformat(),
                "succeeded",
            ),
        )
        conn.execute(
            "INSERT INTO evidence_nodes VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                "node-nu-q4",
                "node:nu:q4",
                1,
                "run-nu-q4",
                None,
                None,
                "document",
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
                "node-nu-q4",
                locator_json,
                locator_sha,
                content_sha,
                NOW.isoformat(),
                NOW.isoformat(),
                NOW.isoformat(),
                None,
            ),
        )
        conn.execute(
            "INSERT INTO kpi_definitions (id,ticker,name,unit,primary_source) "
            "VALUES (641,'NU','Total customers (millions)','millions','ir_doc')"
        )
        conn.execute(
            "INSERT INTO kpi_facts "
            "(id,ticker,period_end,fiscal_period_type,kpi_definition_id,value,unit,currency,"
            "source_doc_id,confidence,extracted_by) "
            "VALUES (42175,'NU','2024-12-31','Q4',641,'95','millions',NULL,1,0.9,'legacy')"
        )
        new_id = insert_source_reviewed_kpi_supersession(
            conn,
            predecessor_id=42175,
            expected_head_id=42175,
            value=Decimal("114.2"),
            unit=Unit.MILLIONS,
            currency=None,
            source_doc_id=1,
            locator=_entry().locator,
            source_excerpt="Total customers reached 114.2 million.",
            reviewer="owner",
            knowledge_at=NOW,
            context=_context(),
        )
        successor = conn.execute(
            "SELECT value,currency,supersedes_id,extracted_by FROM kpi_facts WHERE id=?",
            (new_id,),
        ).fetchone()
        assert tuple(successor) == (114.2, None, 42175, "source_review:owner")
        semantic = current_kpi_semantic_context(conn, kpi_fact_id=new_id)
        assert semantic is not None
        assert semantic.reviewed_by == "owner"
        assert semantic.knowledge_at == NOW
        observation = conn.execute(
            "SELECT observation.numeric_value,observation.evidence_node_id,"
            "revision.fact_table,revision.fact_row_id "
            "FROM fact_observation_revisions revision JOIN reported_observations observation "
            "ON observation.observation_id=revision.observation_id "
            "WHERE revision.fact_table='kpi_facts' AND revision.fact_row_id=?",
            (new_id,),
        ).fetchone()
        assert tuple(observation) == ("114.2", "node-nu-q4", "kpi_facts", new_id)
        revision = conn.execute(
            "SELECT observation_id,source_document_id FROM fact_observation_revisions "
            "WHERE fact_table='kpi_facts' AND fact_row_id=?",
            (new_id,),
        ).fetchone()
        assert tuple(revision) == (f"kpi_facts:{new_id}:r1", 1)
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


def test_judge_receipt_verdict_comes_only_from_structured_sol_response(tmp_path: Path) -> None:
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
