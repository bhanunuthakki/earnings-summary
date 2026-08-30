from __future__ import annotations

import hashlib
import inspect
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

import execution.apply_kpi_semantic_dispositions as disposition_executor
from execution.apply_kpi_semantic_dispositions import (
    OPERATIONS_GOVERNANCE_DISPOSITION as APPLY_SURFACE_DISPOSITION,
)
from execution.apply_kpi_semantic_dispositions import (
    OPERATIONS_GOVERNANCE_PRESERVED_CONTRACT as APPLY_PRESERVED_CONTRACT,
)
from execution.apply_kpi_semantic_dispositions import build_parser as disposition_parser
from execution.apply_kpi_semantic_dispositions import (
    execute_disposition_transaction,
    judge_authorizes,
    recover_committed_disposition,
    validate_disposition_external_evidence,
)
from execution.apply_kpi_semantic_dispositions import main as apply_dispositions_main
from execution.audit_kpi_semantics import summarize_kpi_semantic_audit
from execution.prepare_kpi_semantic_dispositions import (
    OPERATIONS_GOVERNANCE_DISPOSITION as PREPARE_SURFACE_DISPOSITION,
)
from execution.prepare_kpi_semantic_dispositions import (
    OPERATIONS_GOVERNANCE_PRESERVED_CONTRACT as PREPARE_PRESERVED_CONTRACT,
)
from execution.prepare_kpi_semantic_dispositions import build_parser as prepare_parser
from execution.record_kpi_disposition_judgment import main as record_disposition_judgment
from operations.kpi_repair_receipts import (
    KpiDispositionAttemptReceipt,
    KpiDispositionJudgeReceipt,
    repair_executor_code_sha256,
    seal_disposition_attempt,
)
from operations.review_bundle import OperationsReviewBundle, ReviewIdentity
from pipeline.kpi_report_reference_dispositions import (
    ReportKpiReferenceDisposition,
    ReportKpiReferenceStatus,
    current_report_kpi_reference_disposition,
    load_report_kpi_reference_inventory,
    persist_report_kpi_reference_disposition,
    report_kpi_references,
)
from pipeline.kpi_semantic_dispositions import (
    apply_kpi_semantic_disposition_manifest,
    prepare_kpi_semantic_disposition_manifest,
)
from pipeline.kpi_semantic_scope import scoped_kpi_definitions
from pipeline.kpi_semantics import semantic_admission_sql

NOW = datetime(2026, 8, 30, 12, tzinfo=UTC)


def test_disposition_clis_have_an_explicit_no_surface_change_review() -> None:
    assert APPLY_SURFACE_DISPOSITION.startswith("no_surface_change_")
    assert PREPARE_SURFACE_DISPOSITION.startswith("no_surface_change_")
    assert "src/operations/review_bundle.py:ReviewKpiCensus" in APPLY_PRESERVED_CONTRACT
    assert APPLY_PRESERVED_CONTRACT == PREPARE_PRESERVED_CONTRACT


def test_disposition_executor_reuses_the_reviewed_repair_authority_seam() -> None:
    assert validate_disposition_external_evidence.__module__ == "apply_kpi_semantic_refresh"
    actions = {action.dest: action for action in disposition_parser()._actions}
    for required in (
        "manifest",
        "user_id",
        "db",
        "repo_root",
        "review_bundle",
        "trusted_review_pins",
        "backup_restore_receipt",
        "receipt_root",
    ):
        assert actions[required].required is True
    assert "backup" not in actions
    assert "expected_database_instance_id" not in actions
    assert (
        inspect.getsource(apply_dispositions_main).count("validate_disposition_external_evidence(")
        == 2
    )
    prepare_actions = {action.dest: action for action in prepare_parser()._actions}
    for required in (
        "db",
        "repo_root",
        "user_id",
        "reviewer",
        "logical_idempotency_key",
        "review_bundle",
        "backup_restore_receipt",
        "output",
    ):
        assert prepare_actions[required].required is True


def test_invalid_authority_artifacts_emit_a_typed_failure_receipt(tmp_path: Path) -> None:
    receipt_root = tmp_path / "receipts"
    code = apply_dispositions_main(
        [
            "--manifest",
            str(tmp_path / "missing-manifest.json"),
            "--user-id",
            "owner",
            "--db",
            str(tmp_path / "missing.db"),
            "--repo-root",
            str(Path(__file__).resolve().parents[1]),
            "--review-bundle",
            str(tmp_path / "missing-review.json"),
            "--trusted-review-pins",
            str(tmp_path / "missing-pins.json"),
            "--backup-restore-receipt",
            str(tmp_path / "missing-backup.json"),
            "--receipt-root",
            str(receipt_root),
        ]
    )

    assert code == 2
    receipt = KpiDispositionAttemptReceipt.model_validate_json(
        (receipt_root / "latest.json").read_text(encoding="utf-8")
    )
    assert receipt.state == "failed"
    assert receipt.blocker_codes == ("invalid_input_FileNotFoundError",)


def test_sol_disposition_judgment_is_bound_to_exact_dry_run_and_code(
    tmp_path: Path,
) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    conn = _db()
    holdings_root = _repo(tmp_path)
    manifest = prepare_kpi_semantic_disposition_manifest(
        conn,
        repo_root=holdings_root,
        user_id="owner",
        reviewer="source-review:owner",
        logical_idempotency_key="judge-binding",
        expected_schema_revision="0033_add_report_kpi_reference_resolutions",
        review_bundle_sha256="d" * 64,
        backup_restore_evidence_id="e" * 64,
        knowledge_at=NOW,
    )
    dry_run = seal_disposition_attempt(
        attempt_id="a" * 32,
        logical_idempotency_key_sha256="b" * 64,
        manifest_sha256=manifest.content_sha256(),
        review_bundle_sha256=manifest.review_bundle_sha256,
        backup_restore_evidence_id=manifest.backup_restore_evidence_id,
        executor_code_sha256=repair_executor_code_sha256(repo_root),
        mode="dry_run",
        state="passed",
        started_at=NOW,
        completed_at=NOW,
        validated_fact_dispositions=1,
        validated_reference_dispositions=1,
        inserted_context_rows=1,
        replayed_context_rows=0,
        inserted_reference_rows=1,
        replayed_reference_rows=0,
        blocker_codes=(),
    )
    dry_path = tmp_path / "dry.json"
    prompt_path = tmp_path / "prompt.txt"
    response_path = tmp_path / "response.json"
    output_path = tmp_path / "judge.json"
    dry_path.write_text(dry_run.model_dump_json(), encoding="utf-8")
    prompt_path.write_text("Judge exact KPI disposition evidence.", encoding="utf-8")
    response_path.write_text(
        '{"purpose":"kpi_semantic_disposition","rubric_version":"j3-v1",'
        '"evidence_tier":"J3","verdict":"PASS","findings":[],'
        '"issued_at":"2026-08-30T12:00:00Z"}',
        encoding="utf-8",
    )

    assert (
        record_disposition_judgment(
            [
                "--dry-run-receipt",
                str(dry_path),
                "--judge-run-id",
                "sol:test-run",
                "--prompt-file",
                str(prompt_path),
                "--response-file",
                str(response_path),
                "--output",
                str(output_path),
            ]
        )
        == 0
    )
    judge = KpiDispositionJudgeReceipt.model_validate_json(output_path.read_text(encoding="utf-8"))
    assert judge.verdict == "PASS"
    assert judge.dry_run_receipt_sha256 == dry_run.content_sha256
    assert judge_authorizes(
        dry_run=dry_run,
        judge=judge,
        manifest_sha=manifest.content_sha256(),
        manifest=manifest,
        executor_code_sha=repair_executor_code_sha256(repo_root),
    )
    assert not judge_authorizes(
        dry_run=dry_run,
        judge=judge.model_copy(update={"evidence_tier": "J2"}),
        manifest_sha=manifest.content_sha256(),
        manifest=manifest,
        executor_code_sha=repair_executor_code_sha256(repo_root),
    )


def test_post_commit_receipt_failure_recovers_as_exact_replay_without_second_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _db()
    repo = _repo(tmp_path)
    manifest = prepare_kpi_semantic_disposition_manifest(
        source,
        repo_root=repo,
        user_id="owner",
        reviewer="source-review:owner",
        logical_idempotency_key="crash-safe-replay",
        expected_schema_revision="0033_add_report_kpi_reference_resolutions",
        review_bundle_sha256="d" * 64,
        backup_restore_evidence_id="e" * 64,
        knowledge_at=NOW,
    )
    source.commit()
    db_path = tmp_path / "portfolio.db"
    persisted = sqlite3.connect(db_path)
    source.backup(persisted)
    persisted.close()
    repo_root = Path(__file__).resolve().parents[1]
    code_sha = repair_executor_code_sha256(repo_root)
    review_bundle = OperationsReviewBundle.model_construct(
        identity=ReviewIdentity.model_construct(
            database_instance_sha256=manifest.expected_database_instance_sha256
        )
    )
    manifest_sha = manifest.content_sha256()
    logical_sha = hashlib.sha256(manifest.logical_idempotency_key.encode()).hexdigest()

    applied = execute_disposition_transaction(
        db_path=db_path,
        repo_root=repo,
        manifest=manifest,
        manifest_sha=manifest_sha,
        logical_key_sha=logical_sha,
        executor_code_sha=code_sha,
        review_bundle=review_bundle,
        apply=True,
    )
    assert applied.inserted_context_rows == 1
    assert applied.inserted_reference_rows == 1
    before = sqlite3.connect(db_path)
    before_counts = (
        before.execute("SELECT COUNT(*) FROM kpi_fact_semantic_contexts").fetchone()[0],
        before.execute("SELECT COUNT(*) FROM report_kpi_reference_resolution_revisions").fetchone()[
            0
        ],
        before.execute("SELECT COUNT(*) FROM kpi_semantic_disposition_commits").fetchone()[0],
    )
    before.close()

    def fail_publication(**_values: object) -> object:
        raise OSError("injected receipt publication failure")

    monkeypatch.setattr(disposition_executor, "publish_disposition_receipt", fail_publication)
    with pytest.raises(OSError, match="injected"):
        disposition_executor.publish_disposition_receipt(
            receipt_root=tmp_path / "receipts",
            receipt=seal_disposition_attempt(
                attempt_id="f" * 32,
                logical_idempotency_key_sha256=logical_sha,
                manifest_sha256=manifest_sha,
                review_bundle_sha256=manifest.review_bundle_sha256,
                backup_restore_evidence_id=manifest.backup_restore_evidence_id,
                executor_code_sha256=code_sha,
                mode="apply",
                state="applied",
                started_at=NOW,
                completed_at=NOW,
                validated_fact_dispositions=1,
                validated_reference_dispositions=1,
                inserted_context_rows=1,
                replayed_context_rows=0,
                inserted_reference_rows=1,
                replayed_reference_rows=0,
                blocker_codes=(),
            ),
        )

    replay = recover_committed_disposition(
        db_path=db_path,
        manifest=manifest,
        manifest_sha=manifest_sha,
        logical_key_sha=logical_sha,
        executor_code_sha=code_sha,
        review_bundle=review_bundle,
    )
    assert replay is not None
    assert replay.replayed_context_rows == 1
    assert replay.replayed_reference_rows == 1
    after = sqlite3.connect(db_path)
    after_counts = (
        after.execute("SELECT COUNT(*) FROM kpi_fact_semantic_contexts").fetchone()[0],
        after.execute("SELECT COUNT(*) FROM report_kpi_reference_resolution_revisions").fetchone()[
            0
        ],
        after.execute("SELECT COUNT(*) FROM kpi_semantic_disposition_commits").fetchone()[0],
    )
    after.close()
    assert after_counts == before_counts == (1, 1, 1)


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE alembic_version(version_num TEXT PRIMARY KEY);
        INSERT INTO alembic_version VALUES ('0033_add_report_kpi_reference_resolutions');
        CREATE TABLE database_runtime_identity(singleton INTEGER PRIMARY KEY,database_instance_id TEXT);
        INSERT INTO database_runtime_identity VALUES (1,'database-instance:0123456789abcdef0123456789abcdef');
        CREATE TABLE tracked_companies(
            ticker TEXT, list_type TEXT, user_id TEXT, archived_at TEXT
        );
        CREATE TABLE kpi_definitions(id INTEGER PRIMARY KEY,ticker TEXT,name TEXT);
        CREATE TABLE kpi_facts(
            id INTEGER PRIMARY KEY,ticker TEXT,period_end TEXT,fiscal_period_type TEXT,
            kpi_definition_id INTEGER,value TEXT,unit TEXT,source_doc_id INTEGER,
            supersedes_id INTEGER
        );
        CREATE TABLE documents(
            id INTEGER PRIMARY KEY,ticker TEXT,source_type TEXT,doc_type TEXT,
            period_end TEXT,sha256 TEXT,fetched_at TEXT,parent_document_id INTEGER,
            file_path TEXT
        );
        CREATE VIEW v_kpi_facts_resolved_current AS SELECT * FROM kpi_facts;
        CREATE TABLE kpi_fact_semantic_contexts(
            id INTEGER PRIMARY KEY AUTOINCREMENT,kpi_fact_id INTEGER NOT NULL,
            revision INTEGER NOT NULL,supersedes_context_id INTEGER UNIQUE,
            metric_name_as_reported TEXT NOT NULL,reported_period_end TEXT,
            period_role TEXT NOT NULL,publication_lane TEXT NOT NULL,
            accounting_basis TEXT NOT NULL,consolidation_scope TEXT NOT NULL,
            dimensions_json TEXT NOT NULL,unit_scale TEXT NOT NULL,
            source_row_label TEXT,source_column_header TEXT,source_value_text TEXT,
            status TEXT NOT NULL,reason_code TEXT,reviewed_by TEXT NOT NULL,
            knowledge_at TEXT NOT NULL,UNIQUE(kpi_fact_id,revision)
        );
        CREATE TABLE report_kpi_reference_resolution_revisions(
            id INTEGER PRIMARY KEY AUTOINCREMENT,user_id TEXT NOT NULL,ticker TEXT NOT NULL,
            source_path TEXT NOT NULL,json_pointer TEXT NOT NULL,reference_kind TEXT NOT NULL,
            requested_label TEXT NOT NULL,reference_content_sha256 TEXT NOT NULL,
            status TEXT NOT NULL,kpi_definition_id INTEGER,reason_code TEXT NOT NULL,
            revision INTEGER NOT NULL,supersedes_resolution_id INTEGER UNIQUE,
            reviewed_by TEXT NOT NULL,knowledge_at TEXT NOT NULL,
            UNIQUE(user_id,ticker,source_path,json_pointer,revision)
        );
        CREATE TABLE kpi_semantic_disposition_commits(
            manifest_sha256 TEXT PRIMARY KEY,logical_idempotency_key_sha256 TEXT UNIQUE,
            review_bundle_sha256 TEXT,backup_restore_evidence_id TEXT,
            executor_code_sha256 TEXT,fact_disposition_count INTEGER,
            reference_disposition_count INTEGER,inserted_context_rows INTEGER,
            inserted_reference_rows INTEGER,committed_at TEXT
        );
        """
    )
    conn.execute("INSERT INTO tracked_companies VALUES ('NU','portfolio','owner',NULL)")
    conn.execute("INSERT INTO tracked_companies VALUES ('NU','portfolio','other',NULL)")
    conn.execute("INSERT INTO kpi_definitions VALUES (1,'NU','Total customers')")
    conn.execute(
        "INSERT INTO documents VALUES "
        "(10,'NU','fmp','vendor_kpi','2024-12-31',?,? ,NULL,'vendor://kpi')",
        ("a" * 64, "2026-08-01T00:00:00Z"),
    )
    conn.execute(
        "INSERT INTO kpi_facts VALUES (1,'NU','2024-12-31','Q4',1,'114200000','count',10,NULL)"
    )
    return conn


def _repo(tmp_path: Path) -> Path:
    path = tmp_path / "micro_thesis" / "holdings"
    path.mkdir(parents=True)
    (path / "NU.json").write_text(
        '{"ticker":"NU","tier_1_kpis":[{"name":"Total customers"},{"name":"Unmapped engagement"}]}',
        encoding="utf-8",
    )
    return tmp_path


def test_disposition_rollout_is_append_only_idempotent_and_fail_closed(tmp_path: Path) -> None:
    conn = _db()
    repo = _repo(tmp_path)
    manifest = prepare_kpi_semantic_disposition_manifest(
        conn,
        repo_root=repo,
        user_id="owner",
        reviewer="source-review:owner",
        logical_idempotency_key="portfolio-kpi-dispositions-2026-08-30",
        expected_schema_revision="0033_add_report_kpi_reference_resolutions",
        review_bundle_sha256="b" * 64,
        backup_restore_evidence_id="c" * 64,
        knowledge_at=NOW,
    )

    assert [entry.reason_code for entry in manifest.fact_dispositions] == [
        "source_type_not_reviewable"
    ]
    assert len(manifest.report_reference_dispositions) == 1
    assert (
        manifest.report_reference_dispositions[0].disposition.reason_code
        == "no_matching_reported_definition"
    )

    result = apply_kpi_semantic_disposition_manifest(conn, repo_root=repo, manifest=manifest)
    replay = apply_kpi_semantic_disposition_manifest(conn, repo_root=repo, manifest=manifest)

    assert result.inserted_context_rows == 1
    assert result.inserted_reference_rows == 1
    assert replay.replayed_context_rows == 1
    assert replay.replayed_reference_rows == 1
    assert conn.execute("SELECT COUNT(*) FROM kpi_fact_semantic_contexts").fetchone()[0] == 1
    assert (
        conn.execute("SELECT COUNT(*) FROM report_kpi_reference_resolution_revisions").fetchone()[0]
        == 1
    )
    join, where = semantic_admission_sql(conn, fail_closed=True)
    assert conn.execute(f"SELECT kf.id FROM kpi_facts kf {join} WHERE {where}").fetchall() == []
    rows = scoped_kpi_definitions(conn, repo_root=repo, user_id="owner")
    fact_row = next(row for row in rows if row.kpi_definition_id == 1)
    unresolved = next(row for row in rows if row.kpi_definition_id is None)
    assert fact_row.missing_context_count == 0
    assert fact_row.quarantined_context_count == 1
    assert unresolved.report_reference_status is ReportKpiReferenceStatus.UNRESOLVED
    audit = summarize_kpi_semantic_audit(rows, user_id="owner")
    assert audit.disposition_gate_blocked is False
    assert audit.decision_grade_admission_blocked is True
    assert audit.gate_blocked is True
    assert audit.undisposed_report_references == 0
    assert audit.disposed_unresolved_report_references == 1
    changed_identity = manifest.model_copy(update={"expected_database_instance_sha256": "f" * 64})
    with pytest.raises(ValueError, match="database lineage identity changed"):
        apply_kpi_semantic_disposition_manifest(
            conn,
            repo_root=repo,
            manifest=changed_identity,
        )


def test_reference_revision_is_owner_scoped_and_v1_cannot_clear_unresolved(
    tmp_path: Path,
) -> None:
    conn = _db()
    repo = _repo(tmp_path)
    reference = report_kpi_references(repo, ("NU",))[1]
    unresolved = ReportKpiReferenceDisposition(
        status=ReportKpiReferenceStatus.UNRESOLVED,
        reason_code="no_matching_reported_definition",
    )
    first = persist_report_kpi_reference_disposition(
        conn,
        user_id="owner",
        reference=reference,
        disposition=unresolved,
        reviewed_by="source-review:owner",
        knowledge_at=NOW,
    )
    replayed = persist_report_kpi_reference_disposition(
        conn,
        user_id="owner",
        reference=reference,
        disposition=unresolved,
        reviewed_by="source-review:owner",
        knowledge_at=NOW,
    )

    assert replayed == first
    current = current_report_kpi_reference_disposition(conn, user_id="owner", reference=reference)
    assert current is not None
    assert current.revision == 1
    assert current.supersedes_resolution_id is None
    assert (
        current_report_kpi_reference_disposition(conn, user_id="other", reference=reference) is None
    )
    with pytest.raises(ValueError, match="cannot bind"):
        ReportKpiReferenceDisposition(
            status=ReportKpiReferenceStatus.UNRESOLVED,
            kpi_definition_id=1,
            reason_code="guess_fix",
        )
    with pytest.raises(ValueError):
        ReportKpiReferenceDisposition.model_validate(
            {"status": "retired", "reason_code": "clear_the_gate"}
        )


def test_punctuation_or_alias_like_label_drift_does_not_auto_bind(tmp_path: Path) -> None:
    conn = _db()
    repo = _repo(tmp_path)
    holdings = repo / "micro_thesis" / "holdings" / "NU.json"
    holdings.write_text(
        '{"ticker":"NU","tier_1_kpis":[{"name":"Total-customers"}]}',
        encoding="utf-8",
    )

    rows = scoped_kpi_definitions(conn, repo_root=repo, user_id="owner")

    assert len(rows) == 2
    definition = next(row for row in rows if row.kpi_definition_id == 1)
    reference = next(row for row in rows if row.kpi_definition_id is None)
    assert definition.reasons == ("facts_metrics",)
    assert reference.name == "Total-customers"
    assert reference.report_reference_status is None


def test_duplicate_exact_labels_are_ambiguous_and_never_auto_bind(tmp_path: Path) -> None:
    conn = _db()
    conn.execute("INSERT INTO kpi_definitions VALUES (2,'NU','Total customers')")
    repo = _repo(tmp_path)
    holdings = repo / "micro_thesis" / "holdings" / "NU.json"
    holdings.write_text(
        '{"ticker":"NU","tier_1_kpis":[{"name":"Total customers"}]}',
        encoding="utf-8",
    )

    rows = scoped_kpi_definitions(conn, repo_root=repo, user_id="owner")

    assert all("report" not in row.reasons for row in rows if row.kpi_definition_id is not None)
    ambiguous = next(row for row in rows if row.kpi_definition_id is None)
    assert ambiguous.name == "Total customers"
    assert ambiguous.report_reference_status is None
    assert ambiguous.report_reference_reason_code == "ambiguous_exact_reported_definition"
    manifest = prepare_kpi_semantic_disposition_manifest(
        conn,
        repo_root=repo,
        user_id="owner",
        reviewer="source-review:owner",
        logical_idempotency_key="ambiguous-label",
        expected_schema_revision="0033_add_report_kpi_reference_resolutions",
        review_bundle_sha256="b" * 64,
        backup_restore_evidence_id="c" * 64,
        knowledge_at=NOW,
    )
    assert (
        manifest.report_reference_dispositions[0].disposition.reason_code
        == "ambiguous_exact_reported_definition"
    )


@pytest.mark.parametrize(
    ("contents", "reason_code"),
    [
        ("not-json", "report_configuration_json_invalid"),
        ('{"ticker":"NU","tier_1_kpis":{}}', "report_kpi_tier_invalid"),
        ('{"ticker":"NU","tier_1_kpis":[{}]}', "report_kpi_tier_name_invalid"),
        ('{"ticker":"NU","chart_priorities":[42]}', "report_chart_priority_entry_invalid"),
        ('{"ticker":"NU","break_rules":[{}]}', "report_break_rule_name_invalid"),
    ],
)
def test_report_configuration_inventory_fails_closed_without_partial_references(
    tmp_path: Path, contents: str, reason_code: str
) -> None:
    holdings = tmp_path / "micro_thesis" / "holdings"
    holdings.mkdir(parents=True)
    (holdings / "NU.json").write_text(contents, encoding="utf-8")

    inventory = load_report_kpi_reference_inventory(tmp_path, ("NU",))

    assert inventory.references == ()
    assert inventory.source_states[0].reason_code == reason_code
    rows = scoped_kpi_definitions(_db(), repo_root=tmp_path, user_id="owner")
    audit = summarize_kpi_semantic_audit(rows, user_id="owner")
    assert audit.invalid_or_missing_report_configurations == 1
    assert audit.disposition_gate_blocked is True


def test_missing_report_configuration_blocks_manifest_preparation(tmp_path: Path) -> None:
    conn = _db()
    rows = scoped_kpi_definitions(conn, repo_root=tmp_path, user_id="owner")
    audit = summarize_kpi_semantic_audit(rows, user_id="owner")
    assert audit.invalid_or_missing_report_configurations == 1
    assert audit.disposition_gate_blocked is True

    with pytest.raises(ValueError, match="inventory is incomplete"):
        prepare_kpi_semantic_disposition_manifest(
            conn,
            repo_root=tmp_path,
            user_id="owner",
            reviewer="source-review:owner",
            logical_idempotency_key="missing-report-config",
            expected_schema_revision="0033_add_report_kpi_reference_resolutions",
            review_bundle_sha256="b" * 64,
            backup_restore_evidence_id="c" * 64,
            knowledge_at=NOW,
        )


def test_foreign_ticker_report_configuration_is_rejected_without_references(
    tmp_path: Path,
) -> None:
    holdings = tmp_path / "micro_thesis" / "holdings"
    holdings.mkdir(parents=True)
    (holdings / "NU.json").write_text(
        '{"ticker":"MELI","tier_1_kpis":[{"name":"Revenue"}]}',
        encoding="utf-8",
    )

    inventory = load_report_kpi_reference_inventory(tmp_path, ("NU",))

    assert inventory.references == ()
    assert inventory.source_states[0].reason_code == "report_configuration_ticker_mismatch"
