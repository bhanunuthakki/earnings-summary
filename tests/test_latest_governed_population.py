from __future__ import annotations

from datetime import timedelta

import pytest

from provenance.latest_governed_population import (
    LatestGovernedPopulationAdmission,
    LatestGovernedPopulationPersistence,
    LatestGovernedPopulationRequest,
    LatestGovernedPopulationScopeAdmission,
    build_latest_governed_population_receipt,
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


def _database():
    conn = latest_governed_test_database()
    conn.execute(
        "CREATE TABLE latest_governed_population_operation_ledger ("
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
        alembic_revision="0268_latest_governed_population_operation_ledger",
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
        alembic_revision="0268_latest_governed_population_operation_ledger",
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


def test_population_receipt_and_head_commit_atomically_then_replay_exactly() -> None:
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
        conn.execute("SELECT COUNT(*) FROM latest_governed_population_operation_ledger").fetchone(),
    )
    replay = populate_latest_governed_cohort(
        conn,
        admission,
        request,
        persistence=_persistence(),
        input_stability_check=lambda: None,
    )
    assert replay == first
    assert counts_before == (
        conn.execute("SELECT COUNT(*) FROM latest_governed_refresh_runs").fetchone(),
        conn.execute("SELECT COUNT(*) FROM latest_governed_refresh_receipts").fetchone(),
        conn.execute("SELECT COUNT(*) FROM latest_governed_population_operation_ledger").fetchone(),
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
        "SELECT COUNT(*) FROM latest_governed_population_operation_ledger"
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
