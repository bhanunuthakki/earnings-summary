# pyright: reportPrivateUsage=false
from __future__ import annotations

import sqlite3
from datetime import timedelta

import pytest

from ask.sealed_retrieval import derive_retrieval_scope_id
from provenance import latest_governed_population as population_module
from provenance.latest_governed_population import (
    LatestGovernedPopulationAdmission,
    LatestGovernedPopulationPersistence,
    LatestGovernedPopulationRequest,
    LatestGovernedPopulationScopeAdmission,
    build_latest_governed_population_receipt,
    latest_governed_population_operation_id,
    load_latest_governed_population_receipt,
    persist_latest_governed_population_receipt,
    populate_latest_governed_cohort,
    verify_latest_governed_population_receipt,
)
from provenance.latest_governed_state import (
    LatestGovernedRefreshRequest,
    LatestGovernedStateError,
    refresh_latest_governed_state,
)
from tests.test_latest_governed_state import (
    SHA_A,
    SHA_B,
    T0,
    latest_governed_test_database,
)

SCOPE_ID = "ask-scope:v1:3476a10310c9cbbf527a8277bff9db171809c30a0111949fd0b2619e3398fad3"
SECOND_SOURCE_SCOPE_KEY = "investor-research-secondary"
SECOND_SCOPE_ID = derive_retrieval_scope_id(
    source_scope_key=SECOND_SOURCE_SCOPE_KEY,
    issuer_id="issuer-1",
)


def _database():
    conn = latest_governed_test_database()
    conn.execute(
        "CREATE TABLE latest_governed_population_operation_ledger_v2 ("
        "operation_id TEXT PRIMARY KEY,idempotency_key TEXT UNIQUE,"
        "database_instance_id TEXT,eligibility_artifact_sha256 TEXT,"
        "registry_artifact_sha256 TEXT,admission_sha256 TEXT,request_sha256 TEXT,"
        "result_sha256 TEXT,receipt_sha256 TEXT,receipt_json TEXT)"
    )
    return conn


def _persistence() -> LatestGovernedPopulationPersistence:
    return LatestGovernedPopulationPersistence(
        database_path="candidate.db",
        database_instance_id="database-instance:" + "1" * 32,
        alembic_revision="0269_latest_governed_population_receipt_v2",
        eligibility_artifact_sha256=SHA_A,
        registry_artifact_sha256=SHA_B,
    )


def _admission(terminal: str) -> LatestGovernedPopulationAdmission:
    return LatestGovernedPopulationAdmission(
        eligibility_report_sha256=SHA_A,
        production_scope_registry_sha256=SHA_B,
        population_run_id="population-1",
        population_receipt_set_sha256=SHA_A,
        scopes=(
            LatestGovernedPopulationScopeAdmission(
                scope_id=SCOPE_ID,
                source_scope_key="investor-research",
                source_scope_revision_id="scope-revision-1",
                issuer_id="issuer-1",
                reporting_entity_id="reporting-1",
                ticker="TEST",
                promotion_id="promotion-1",
                terminal_commitment=terminal,
            ),
        ),
    )


def _request(
    admission: LatestGovernedPopulationAdmission,
    *,
    apply: bool,
    after_scope_id: str | None = None,
) -> LatestGovernedPopulationRequest:
    return LatestGovernedPopulationRequest(
        operation_recorded_at=T0 + timedelta(minutes=2),
        admission_sha256=admission.commitment_sha256,
        apply=apply,
        after_scope_id=after_scope_id,
        max_scopes=1,
    )


def test_population_dry_run_is_nonmutating_and_exactly_admission_bound() -> None:
    conn = _database()
    planned = refresh_latest_governed_state(
        conn,
        LatestGovernedRefreshRequest(
            scope_id=SCOPE_ID,
            operation_recorded_at=T0 + timedelta(minutes=1),
            apply=False,
        ),
    )
    admission = _admission(planned.terminal_commitment)

    result = populate_latest_governed_cohort(conn, admission, _request(admission, apply=False))

    assert result.outcome == "planned"
    assert result.processed_scope_ids == (SCOPE_ID,)
    assert result.cohort_audit is None
    assert conn.execute("SELECT COUNT(*) FROM latest_governed_refresh_runs").fetchone() == (0,)
    assert conn.execute("SELECT COUNT(*) FROM latest_governed_scope_heads").fetchone() == (0,)


def test_population_apply_populates_and_audits_all_0261_planes() -> None:
    conn = _database()
    planned = refresh_latest_governed_state(
        conn,
        LatestGovernedRefreshRequest(
            scope_id=SCOPE_ID,
            operation_recorded_at=T0 + timedelta(minutes=1),
            apply=False,
        ),
    )
    admission = _admission(planned.terminal_commitment)

    result = populate_latest_governed_cohort(
        conn,
        admission,
        _request(admission, apply=True),
        persistence=_persistence(),
        input_stability_check=lambda: None,
    )

    assert result.outcome == "complete"
    assert result.remaining_scope_ids == ()
    assert result.cohort_audit is not None
    assert result.cohort_audit.table_counts == {
        "latest_governed_refresh_runs": 1,
        "latest_governed_refresh_stage": 0,
        "latest_governed_refresh_receipts": 1,
        "latest_governed_refresh_changes": 0,
        "latest_governed_scope_heads": 1,
        "latest_governed_fact_entries": 1,
        "latest_governed_document_entries": 1,
        "latest_governed_narrative_entries": 1,
        "latest_governed_narrative_fts": 1,
    }
    assert result.scope_results[0].head_state_sha256 == planned.terminal_commitment
    assert result.scope_results[0].stage_count == 0


def test_population_refuses_stale_admission_and_invalid_resume_cursor() -> None:
    conn = _database()
    planned = refresh_latest_governed_state(
        conn,
        LatestGovernedRefreshRequest(
            scope_id=SCOPE_ID,
            operation_recorded_at=T0 + timedelta(minutes=1),
            apply=False,
        ),
    )
    admission = _admission(planned.terminal_commitment)
    stale = admission.model_copy(
        update={
            "scopes": (admission.scopes[0].model_copy(update={"terminal_commitment": "c" * 64}),)
        }
    )
    with pytest.raises(LatestGovernedStateError, match="dry-run differs"):
        populate_latest_governed_cohort(
            conn,
            stale,
            _request(stale, apply=True),
            persistence=_persistence(),
            input_stability_check=lambda: None,
        )
    with pytest.raises(LatestGovernedStateError, match="outside the cohort"):
        populate_latest_governed_cohort(
            conn,
            admission,
            _request(admission, apply=True, after_scope_id="missing"),
        )
    assert conn.execute("SELECT COUNT(*) FROM latest_governed_scope_heads").fetchone() == (0,)


def test_population_receipt_binds_database_inputs_and_nested_result() -> None:
    conn = _database()
    planned = refresh_latest_governed_state(
        conn,
        LatestGovernedRefreshRequest(
            scope_id=SCOPE_ID,
            operation_recorded_at=T0 + timedelta(minutes=1),
            apply=False,
        ),
    )
    admission = _admission(planned.terminal_commitment)
    request = _request(admission, apply=False)
    result = populate_latest_governed_cohort(conn, admission, request)
    receipt = build_latest_governed_population_receipt(
        database_path="candidate.db",
        database_instance_id="database-instance:" + "1" * 32,
        alembic_revision="0269_latest_governed_population_receipt_v2",
        eligibility_artifact_sha256=SHA_A,
        registry_artifact_sha256=SHA_B,
        admission=admission,
        request=request,
        result=result,
        prior_checkpoint_receipt_sha256=None,
    )
    assert verify_latest_governed_population_receipt(receipt)
    with pytest.raises(ValueError, match="receipt commitment mismatch"):
        receipt.model_copy(update={"database_path": "other.db"}).__class__.model_validate(
            receipt.model_copy(update={"database_path": "other.db"}).model_dump(mode="json")
        )


def test_population_operation_identity_binds_exact_persistence_artifacts() -> None:
    conn = _database()
    planned = refresh_latest_governed_state(
        conn,
        LatestGovernedRefreshRequest(
            scope_id=SCOPE_ID,
            operation_recorded_at=T0 + timedelta(minutes=1),
            apply=False,
        ),
    )
    admission = _admission(planned.terminal_commitment)
    request = _request(admission, apply=True)
    persistence = _persistence()
    first = latest_governed_population_operation_id(
        persistence=persistence,
        admission_sha256=admission.commitment_sha256,
        request=request,
    )
    changed = latest_governed_population_operation_id(
        persistence=persistence.model_copy(update={"eligibility_artifact_sha256": "c" * 64}),
        admission_sha256=admission.commitment_sha256,
        request=request,
    )
    assert first != changed


def test_population_receipt_and_head_commit_atomically_then_replay_exactly() -> None:
    conn = _database()
    conn.execute(
        "CREATE TRIGGER population_identity_immutable BEFORE INSERT ON "
        "latest_governed_population_operation_ledger_v2 WHEN EXISTS ("
        "SELECT 1 FROM latest_governed_population_operation_ledger_v2 existing WHERE "
        "existing.operation_id=NEW.operation_id OR "
        "existing.idempotency_key=NEW.idempotency_key OR "
        "existing.receipt_sha256=NEW.receipt_sha256) BEGIN "
        "SELECT RAISE(ABORT,'population ledger is immutable'); END"
    )
    planned = refresh_latest_governed_state(
        conn,
        LatestGovernedRefreshRequest(
            scope_id=SCOPE_ID,
            operation_recorded_at=T0 + timedelta(minutes=1),
            apply=False,
        ),
    )
    admission = _admission(planned.terminal_commitment)
    request = _request(admission, apply=True)
    first = populate_latest_governed_cohort(
        conn,
        admission,
        request,
        persistence=_persistence(),
        input_stability_check=lambda: None,
    )
    counts_before = (
        conn.execute("SELECT COUNT(*) FROM latest_governed_refresh_runs").fetchone(),
        conn.execute("SELECT COUNT(*) FROM latest_governed_refresh_receipts").fetchone(),
        conn.execute(
            "SELECT COUNT(*) FROM latest_governed_population_operation_ledger_v2"
        ).fetchone(),
    )
    replay = populate_latest_governed_cohort(
        conn,
        admission,
        request,
        persistence=_persistence(),
        input_stability_check=lambda: None,
    )
    assert replay == first
    operation_id = latest_governed_population_operation_id(
        persistence=_persistence(),
        admission_sha256=admission.commitment_sha256,
        request=request,
    )
    receipt = load_latest_governed_population_receipt(conn, operation_id)
    assert receipt is not None
    assert not persist_latest_governed_population_receipt(conn, receipt)
    assert counts_before == (
        conn.execute("SELECT COUNT(*) FROM latest_governed_refresh_runs").fetchone(),
        conn.execute("SELECT COUNT(*) FROM latest_governed_refresh_receipts").fetchone(),
        conn.execute(
            "SELECT COUNT(*) FROM latest_governed_population_operation_ledger_v2"
        ).fetchone(),
    )


def test_population_input_change_rolls_back_head_and_ledger_then_resumes_stage() -> None:
    conn = _database()
    planned = refresh_latest_governed_state(
        conn,
        LatestGovernedRefreshRequest(
            scope_id=SCOPE_ID,
            operation_recorded_at=T0 + timedelta(minutes=1),
            apply=False,
        ),
    )
    admission = _admission(planned.terminal_commitment)
    request = _request(admission, apply=True)

    def changed() -> None:
        raise ValueError("admission artifact changed")

    with pytest.raises(ValueError, match="artifact changed"):
        populate_latest_governed_cohort(
            conn,
            admission,
            request,
            persistence=_persistence(),
            input_stability_check=changed,
        )
    assert conn.execute("SELECT COUNT(*) FROM latest_governed_scope_heads").fetchone() == (0,)
    assert conn.execute(
        "SELECT COUNT(*) FROM latest_governed_population_operation_ledger_v2"
    ).fetchone() == (0,)
    assert conn.execute("SELECT COUNT(*) FROM latest_governed_refresh_stage").fetchone() == (3,)

    completed = populate_latest_governed_cohort(
        conn,
        admission,
        request,
        persistence=_persistence(),
        input_stability_check=lambda: None,
    )
    assert completed.outcome == "complete"
    assert conn.execute("SELECT COUNT(*) FROM latest_governed_refresh_stage").fetchone() == (0,)


def test_population_concurrent_database_change_before_finalization_rolls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _database()
    planned = refresh_latest_governed_state(
        conn,
        LatestGovernedRefreshRequest(
            scope_id=SCOPE_ID,
            operation_recorded_at=T0 + timedelta(minutes=1),
            apply=False,
        ),
    )
    admission = _admission(planned.terminal_commitment)
    versions = iter((7, 8))

    def changed_data_version(_conn: sqlite3.Connection) -> int:
        return next(versions)

    monkeypatch.setattr(population_module, "_data_version", changed_data_version)

    with pytest.raises(LatestGovernedStateError, match="changed before checkpoint"):
        populate_latest_governed_cohort(
            conn,
            admission,
            _request(admission, apply=True),
            persistence=_persistence(),
            input_stability_check=lambda: None,
        )

    assert conn.execute("SELECT COUNT(*) FROM latest_governed_scope_heads").fetchone() == (0,)
    assert conn.execute(
        "SELECT COUNT(*) FROM latest_governed_population_operation_ledger_v2"
    ).fetchone() == (0,)


def test_population_replay_refuses_database_rollback_after_receipt() -> None:
    conn = _database()
    planned = refresh_latest_governed_state(
        conn,
        LatestGovernedRefreshRequest(
            scope_id=SCOPE_ID,
            operation_recorded_at=T0 + timedelta(minutes=1),
            apply=False,
        ),
    )
    admission = _admission(planned.terminal_commitment)
    request = _request(admission, apply=True)
    populate_latest_governed_cohort(
        conn,
        admission,
        request,
        persistence=_persistence(),
        input_stability_check=lambda: None,
    )
    conn.execute(
        "UPDATE latest_governed_scope_heads SET state_sha256=? WHERE scope_key=?",
        ("e" * 64, SCOPE_ID),
    )
    with pytest.raises(LatestGovernedStateError, match="current database heads"):
        populate_latest_governed_cohort(
            conn,
            admission,
            request,
            persistence=_persistence(),
            input_stability_check=lambda: None,
        )


def test_population_wraps_a_previously_finalized_noop_refresh_atomically() -> None:
    conn = _database()
    refresh_latest_governed_state(
        conn,
        LatestGovernedRefreshRequest(
            scope_id=SCOPE_ID,
            operation_recorded_at=T0 + timedelta(minutes=1),
            apply=True,
        ),
    )
    dry = refresh_latest_governed_state(
        conn,
        LatestGovernedRefreshRequest(
            scope_id=SCOPE_ID,
            operation_recorded_at=T0 + timedelta(minutes=2),
            apply=False,
        ),
    )
    admission = _admission(dry.terminal_commitment)
    request = LatestGovernedPopulationRequest(
        operation_recorded_at=T0 + timedelta(minutes=2),
        admission_sha256=admission.commitment_sha256,
        apply=True,
        max_scopes=1,
    )
    refresh_latest_governed_state(
        conn,
        LatestGovernedRefreshRequest(
            scope_id=SCOPE_ID,
            operation_recorded_at=request.operation_recorded_at,
            apply=True,
        ),
    )

    result = populate_latest_governed_cohort(
        conn,
        admission,
        request,
        persistence=_persistence(),
        input_stability_check=lambda: None,
    )

    assert result.outcome == "complete"
    assert conn.execute(
        "SELECT COUNT(*) FROM latest_governed_population_operation_ledger_v2"
    ).fetchone() == (1,)


def test_population_resume_audits_every_committed_prefix_plane_before_publication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _database()
    columns = tuple(
        str(row[1]) for row in conn.execute("PRAGMA table_info(v_ask_retrieval_scope_current)")
    )
    values = list(
        conn.execute(
            "SELECT * FROM v_ask_retrieval_scope_current WHERE scope_key=?",
            (SCOPE_ID,),
        ).fetchone()
    )
    values[columns.index("promotion_id")] = "promotion-2"
    values[columns.index("scope_key")] = SECOND_SCOPE_ID
    values[columns.index("source_scope_key")] = SECOND_SOURCE_SCOPE_KEY
    values[columns.index("source_scope_revision_id")] = "scope-revision-2"
    conn.execute(
        "INSERT INTO v_ask_retrieval_scope_current VALUES (" + ",".join("?" for _ in values) + ")",
        values,
    )
    conn.execute(
        "INSERT INTO v_issuer_reporting_scope_current VALUES (?,?,?,?)",
        ("scope-revision-2", SECOND_SOURCE_SCOPE_KEY, "issuer-1", "core"),
    )
    first_plan = refresh_latest_governed_state(
        conn,
        LatestGovernedRefreshRequest(
            scope_id=SCOPE_ID,
            operation_recorded_at=T0 + timedelta(minutes=1),
            apply=False,
        ),
    )
    second_plan = refresh_latest_governed_state(
        conn,
        LatestGovernedRefreshRequest(
            scope_id=SECOND_SCOPE_ID,
            operation_recorded_at=T0 + timedelta(minutes=1),
            apply=False,
        ),
    )
    scope_admissions = (
        _admission(first_plan.terminal_commitment).scopes[0],
        LatestGovernedPopulationScopeAdmission(
            scope_id=SECOND_SCOPE_ID,
            source_scope_key=SECOND_SOURCE_SCOPE_KEY,
            source_scope_revision_id="scope-revision-2",
            issuer_id="issuer-1",
            reporting_entity_id="reporting-1",
            ticker="TEST2",
            promotion_id="promotion-2",
            terminal_commitment=second_plan.terminal_commitment,
        ),
    )
    admission = LatestGovernedPopulationAdmission(
        eligibility_report_sha256=SHA_A,
        production_scope_registry_sha256=SHA_B,
        population_run_id="population-1",
        population_receipt_set_sha256=SHA_A,
        scopes=tuple(sorted(scope_admissions, key=lambda item: item.scope_id)),
    )
    first_scope_id, second_scope_id = tuple(item.scope_id for item in admission.scopes)
    first_request = _request(admission, apply=True)
    first_persistence = _persistence()
    first = populate_latest_governed_cohort(
        conn,
        admission,
        first_request,
        persistence=first_persistence,
        input_stability_check=lambda: None,
    )
    assert first.outcome == "checkpoint"
    first_operation_id = latest_governed_population_operation_id(
        persistence=first_persistence,
        admission_sha256=admission.commitment_sha256,
        request=first_request,
    )
    prior_receipt = load_latest_governed_population_receipt(conn, first_operation_id)
    assert prior_receipt is not None
    original_bucket = int(
        conn.execute(
            "SELECT digest_bucket FROM latest_governed_fact_entries WHERE scope_key=?",
            (first_scope_id,),
        ).fetchone()[0]
    )
    conn.execute(
        "UPDATE latest_governed_fact_entries SET digest_bucket=4095 WHERE scope_key=?",
        (first_scope_id,),
    )
    conn.commit()
    resume = _request(admission, apply=True, after_scope_id=first_scope_id)
    persistence = _persistence().model_copy(update={"prior_checkpoint_receipt_sha256": SHA_A})
    original_projection = population_module.current_latest_governed_projection_commitment
    prefix_checks: list[bool] = []

    def observe_prefix_projection(
        connection: sqlite3.Connection,
        scope_id: str,
    ) -> str:
        if scope_id == first_scope_id:
            prefix_checks.append(connection.in_transaction)
        return original_projection(connection, scope_id)

    monkeypatch.setattr(
        population_module,
        "current_latest_governed_projection_commitment",
        observe_prefix_projection,
    )

    with pytest.raises(LatestGovernedStateError, match="prefix projection differs"):
        populate_latest_governed_cohort(
            conn,
            admission,
            resume,
            persistence=persistence,
            prior_checkpoint=prior_receipt,
            input_stability_check=lambda: None,
        )

    assert conn.execute(
        "SELECT COUNT(*) FROM latest_governed_scope_heads WHERE scope_key=?",
        (second_scope_id,),
    ).fetchone() == (0,)
    assert conn.execute(
        "SELECT COUNT(*) FROM latest_governed_population_operation_ledger_v2"
    ).fetchone() == (1,)
    conn.execute(
        "UPDATE latest_governed_fact_entries SET digest_bucket=? WHERE scope_key=?",
        (original_bucket, first_scope_id),
    )
    conn.commit()

    first_refresh_id = str(
        conn.execute(
            "SELECT refresh_run_id FROM latest_governed_refresh_runs WHERE scope_key=?",
            (first_scope_id,),
        ).fetchone()[0]
    )
    conn.execute(
        "INSERT INTO latest_governed_refresh_stage VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            first_refresh_id,
            0,
            "fact",
            "upsert",
            "stale-prefix-stage",
            0,
            None,
            "a" * 64,
            '{"test":true}',
            "b" * 64,
            "staged",
            (T0 + timedelta(minutes=1)).isoformat(),
            None,
        ),
    )
    conn.commit()
    with pytest.raises(LatestGovernedStateError, match="prefix projection differs"):
        populate_latest_governed_cohort(
            conn,
            admission,
            resume,
            persistence=persistence,
            prior_checkpoint=prior_receipt,
            input_stability_check=lambda: None,
        )
    conn.execute(
        "DELETE FROM latest_governed_refresh_stage WHERE refresh_run_id=?",
        (first_refresh_id,),
    )
    conn.commit()

    orphan_rowid = 999_999
    conn.execute(
        "INSERT INTO latest_governed_narrative_fts("
        "rowid,scope_key,expected_document_key,chunk_key,text) VALUES (?,?,?,?,?)",
        (orphan_rowid, first_scope_id, "orphan-document", "orphan-chunk", "orphan"),
    )
    conn.commit()
    with pytest.raises(LatestGovernedStateError, match="FTS rowids differ"):
        populate_latest_governed_cohort(
            conn,
            admission,
            resume,
            persistence=persistence,
            prior_checkpoint=prior_receipt,
            input_stability_check=lambda: None,
        )
    conn.execute(
        "INSERT INTO latest_governed_narrative_fts("
        "latest_governed_narrative_fts,rowid,scope_key,expected_document_key,chunk_key,text) "
        "VALUES ('delete',?,?,?,?,?)",
        (orphan_rowid, first_scope_id, "orphan-document", "orphan-chunk", "orphan"),
    )
    conn.commit()

    conn.execute(
        "CREATE TRIGGER tamper_prefix_after_target_head "
        "AFTER INSERT ON latest_governed_scope_heads "
        f"WHEN NEW.scope_key='{second_scope_id}' BEGIN "  # nosec B608 -- derived fixed test scope
        "UPDATE latest_governed_fact_entries SET digest_bucket=4095 "
        f"WHERE scope_key='{first_scope_id}'; END"  # nosec B608 -- derived fixed test scope
    )
    with pytest.raises(LatestGovernedStateError, match="prefix projection differs"):
        populate_latest_governed_cohort(
            conn,
            admission,
            resume,
            persistence=persistence,
            prior_checkpoint=prior_receipt,
            input_stability_check=lambda: None,
        )
    conn.execute("DROP TRIGGER tamper_prefix_after_target_head")
    assert conn.execute(
        "SELECT digest_bucket FROM latest_governed_fact_entries WHERE scope_key=?",
        (first_scope_id,),
    ).fetchone() == (original_bucket,)

    completed = populate_latest_governed_cohort(
        conn,
        admission,
        resume,
        persistence=persistence,
        prior_checkpoint=prior_receipt,
        input_stability_check=lambda: None,
    )
    assert completed.outcome == "complete"
    assert completed.cohort_audit is not None
    assert {
        audit.scope_id: audit.terminal_commitment for audit in completed.cohort_audit.scopes
    } == {item.scope_id: item.terminal_commitment for item in admission.scopes}
    assert any(not in_transaction for in_transaction in prefix_checks)
    assert any(prefix_checks)
