"""Durable FMP circuit, backlog, lease, and receipt behavior."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Generator
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from models.companies import ListType
from pipeline.fmp_recovery import (
    SCREENING_ENDPOINT_KEYS,
    AlternativeCoverageState,
    AlternativeResolution,
    AlternativeSource,
    CircuitConfig,
    CircuitState,
    CorpusSnapshot,
    CredentialAvailability,
    ExecutionMode,
    FmpSnapshotProof,
    OutcomeCode,
    PlanRunRequest,
    ReceiptStatus,
    RecordOutcomesRequest,
    RecoverableWorkRequest,
    RecoveryAvailability,
    RunPlan,
    WorkOutcome,
    WorkSpec,
    WorkState,
    make_work_id,
    plan_run,
    record_outcomes,
    recoverable_work,
)

REVISION = "0008_add_fmp_recovery"
NOW = datetime(2026, 8, 11, 12, 0, 0)
POLICY_A = "a" * 64
POLICY_B = "b" * 64
CONTENT_A = "c" * 64
PROOF_A = "d" * 64


@pytest.fixture
def db_path(tmp_path: Path, migrated_db: Callable[..., Path]) -> Path:
    return migrated_db(tmp_path / "fmp-recovery.db", target=REVISION)


@contextmanager
def _connection(path: Path) -> Generator[sqlite3.Connection, None, None]:
    connection = sqlite3.connect(path, timeout=2)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        yield connection
    finally:
        connection.close()


def _corpus(*, captured_at: datetime = NOW - timedelta(days=2)) -> CorpusSnapshot:
    return CorpusSnapshot(
        cache_generation_id="corpus-2026q2",
        content_sha256=CONTENT_A,
        captured_at=captured_at,
    )


def _resolution(
    *,
    endpoint_key: str = "income_statement_quarterly",
    period_key: str = "quarterly:last-5",
) -> AlternativeResolution:
    return AlternativeResolution(
        source=AlternativeSource.SEC,
        policy_sha256=POLICY_A,
        endpoint_key=endpoint_key,
        period_key=period_key,
        concept_keys=("revenue", "net_income"),
        evidence_fresh_at=NOW - timedelta(days=1),
        source_authorized=True,
        has_unresolved_disagreement=False,
        coverage_state=AlternativeCoverageState.COMPLETE,
        evidence_ids=(11, 12),
        fact_ids=(22,),
    )


def _spec(
    ticker: str,
    *,
    role: ListType = ListType.PORTFOLIO,
    endpoint: str = "income_statement_quarterly",
    generation: str = "refresh-2026-08-11",
    policy_sha256: str = POLICY_A,
    requested: bool = False,
    owner_request_id: str | None = None,
    corpus_snapshot: CorpusSnapshot | None = None,
    fmp_snapshot: FmpSnapshotProof | None = None,
    alternative_resolution: AlternativeResolution | None = None,
) -> WorkSpec:
    return WorkSpec(
        ticker=ticker,
        coverage_role=role,
        endpoint_key=endpoint,
        period_key="quarterly:last-5",
        cache_generation_id=generation,
        policy_sha256=policy_sha256,
        requested=requested,
        owner_request_id=owner_request_id,
        corpus_snapshot=corpus_snapshot,
        fmp_snapshot=fmp_snapshot,
        alternative_resolution=alternative_resolution,
    )


def _plan(
    connection: sqlite3.Connection,
    *specs: WorkSpec,
    run_id: str = "run-1",
    now: datetime = NOW,
    credentials: CredentialAvailability = CredentialAvailability.AVAILABLE,
    config: CircuitConfig | None = None,
    lease_seconds: int = 30,
):
    return plan_run(
        connection,
        PlanRunRequest(
            run_id=run_id,
            worker_id="worker-a",
            now=now,
            lease_seconds=lease_seconds,
            credentials=credentials,
            circuit_config=config or CircuitConfig(),
            work=specs,
        ),
    )


def _record(
    connection: sqlite3.Connection,
    plan: RunPlan,
    codes: tuple[OutcomeCode, ...],
    *,
    run_id: str = "run-1",
    now: datetime = NOW,
    statuses: tuple[int | None, ...] | None = None,
    retry_after_at: datetime | None = None,
    expected_work_ids: tuple[str, ...] | None = None,
):
    statuses = statuses or tuple(None for _ in codes)
    outcomes: list[WorkOutcome] = []
    for item, code, status in zip(plan.items, codes, statuses, strict=True):
        if item.lease_token is None:
            raise AssertionError("record helper requires leased work")
        fmp_snapshot = item.fmp_snapshot
        if code is OutcomeCode.LIVE_SUCCESS and fmp_snapshot is None:
            if item.cache_generation_id is None or item.policy_sha256 is None:
                raise AssertionError("live fixture requires persisted work identity")
            fmp_snapshot = FmpSnapshotProof(
                work_id=item.work_id,
                cache_generation_id=item.cache_generation_id,
                policy_sha256=item.policy_sha256,
                content_sha256=CONTENT_A,
                captured_at=now,
            )
        outcomes.append(
            WorkOutcome(
                work_id=item.work_id,
                lease_token=item.lease_token,
                outcome_code=code,
                observed_at=now,
                http_status=status,
                retry_after_at=retry_after_at,
                corpus_snapshot=item.corpus_snapshot,
                fmp_snapshot=fmp_snapshot,
                alternative_resolution=item.alternative_resolution,
            )
        )
    return record_outcomes(
        connection,
        RecordOutcomesRequest(
            run_id=run_id,
            now=now,
            expected_work_ids=(
                expected_work_ids
                if expected_work_ids is not None
                else tuple(item.work_id for item in plan.items)
            ),
            outcomes=tuple(outcomes),
        ),
    )


def test_work_id_is_deterministic_and_commits_generation_and_policy() -> None:
    base = _spec("RBRK")
    assert make_work_id(base) == make_work_id(base)
    assert make_work_id(base) != make_work_id(_spec("RBRK", generation="refresh-2"))
    assert make_work_id(base) != make_work_id(_spec("RBRK", policy_sha256=POLICY_B))


def test_plan_prioritizes_portfolio_then_requested_evaluation_then_index(
    db_path: Path,
) -> None:
    specs = (
        _spec("IDX", role=ListType.INDEX_MEMBER, endpoint="profile"),
        _spec(
            "EVAL",
            role=ListType.EVALUATION,
            requested=True,
            owner_request_id="owner-request-7",
        ),
        _spec("PORT", role=ListType.PORTFOLIO),
    )
    with _connection(db_path) as connection:
        plan = _plan(connection, *specs)
        assert [item.ticker for item in plan.items] == ["PORT", "EVAL", "IDX"]
        assert [item.priority for item in plan.items] == [300, 200, 100]
        assert all(item.execution_mode is ExecutionMode.LIVE for item in plan.items)


@pytest.mark.parametrize(
    "spec",
    [
        _spec("WATCH", role=ListType.WATCHLIST),
        _spec("CAT", role=ListType.NONE),
        _spec("IDX", role=ListType.INDEX_MEMBER, endpoint="analyst_estimates"),
    ],
)
def test_policy_denials_fail_before_any_backlog_write(db_path: Path, spec: WorkSpec) -> None:
    with _connection(db_path) as connection:
        with pytest.raises(ValueError, match="not authorized"):
            _plan(connection, spec)
        assert connection.execute("SELECT COUNT(*) FROM fmp_work_backlog").fetchone()[0] == 0


def test_evaluation_requires_an_explicit_owner_request_before_planning() -> None:
    with pytest.raises(ValidationError, match="owner request_id"):
        _spec("EVAL", role=ListType.EVALUATION)


def test_screening_allowlist_matches_peer_depth_contract() -> None:
    assert (
        frozenset(
            {
                "profile",
                "peers",
                "key_metrics_ttm",
                "financial_ratios_ttm",
                "income_statement_quarterly",
                "key_metrics_quarterly",
                "balance_sheet_quarterly",
                "historical_market_cap",
            }
        )
        == SCREENING_ENDPOINT_KEYS
    )


@pytest.mark.parametrize(
    "credentials, reason",
    [
        (CredentialAvailability.MISSING, "auth_missing"),
        (CredentialAvailability.INVALID, "auth_invalid"),
    ],
)
def test_missing_or_invalid_credentials_open_before_external_work_and_use_corpus(
    db_path: Path,
    credentials: CredentialAvailability,
    reason: str,
) -> None:
    spec = _spec("RBRK", corpus_snapshot=_corpus())
    with _connection(db_path) as connection:
        plan = _plan(connection, spec, credentials=credentials)
        assert plan.items[0].execution_mode is ExecutionMode.CORPUS
        state = connection.execute(
            "SELECT state,last_reason_code,revision FROM provider_circuit_state WHERE provider='fmp'"
        ).fetchone()
        assert tuple(state) == (CircuitState.OPEN.value, reason, 1)

        receipt = _record(connection, plan, (OutcomeCode.CORPUS_SUCCESS,))
        assert receipt.status is ReceiptStatus.DEGRADED_CORPUS
        assert receipt.corpus_age_seconds == pytest.approx(2 * 24 * 60 * 60)
        assert receipt.backlog.pending_count == 1
        assert receipt.backlog.oldest_pending_age_seconds == pytest.approx(0)
        assert receipt.backlog.next_probe_at is not None


def test_three_server_failures_open_circuit_but_success_resets_counter(db_path: Path) -> None:
    config = CircuitConfig(
        transient_failure_threshold=3,
        rate_limit_threshold=4,
        retry_delay_seconds=0,
        probe_delay_seconds=60,
    )
    spec = _spec("RBRK")
    with _connection(db_path) as connection:
        for attempt in range(1, 4):
            now = NOW + timedelta(seconds=attempt)
            plan = _plan(
                connection,
                spec,
                run_id=f"run-{attempt}",
                now=now,
                config=config,
            )
            _record(
                connection,
                plan,
                (OutcomeCode.SERVER_ERROR,),
                run_id=f"run-{attempt}",
                now=now,
                statuses=(503,),
            )
            row = connection.execute(
                "SELECT state,consecutive_failures FROM provider_circuit_state WHERE provider='fmp'"
            ).fetchone()
            assert row[1] == attempt
            assert row[0] == (
                CircuitState.OPEN.value if attempt == 3 else CircuitState.CLOSED.value
            )


def test_rate_limits_use_their_configured_threshold_and_retry_after(db_path: Path) -> None:
    config = CircuitConfig(
        transient_failure_threshold=5,
        rate_limit_threshold=2,
        retry_delay_seconds=0,
        probe_delay_seconds=60,
    )
    retry_at = NOW + timedelta(minutes=15)
    spec = _spec("WIX")
    with _connection(db_path) as connection:
        for attempt in (1, 2):
            now = NOW + timedelta(seconds=attempt)
            plan = _plan(
                connection,
                spec,
                run_id=f"limit-{attempt}",
                now=now,
                config=config,
            )
            _record(
                connection,
                plan,
                (OutcomeCode.RATE_LIMITED,),
                run_id=f"limit-{attempt}",
                now=now,
                statuses=(429,),
                retry_after_at=retry_at if attempt == 2 else None,
            )
        row = connection.execute(
            "SELECT state,consecutive_rate_limits,next_probe_at FROM provider_circuit_state "
            "WHERE provider='fmp'"
        ).fetchone()
        safe_probe_at = NOW + timedelta(seconds=2 + config.rate_limit_probe_delay_seconds)
        assert tuple(row) == (CircuitState.OPEN.value, 2, safe_probe_at.isoformat())


def test_later_retry_after_extends_an_already_open_rate_limit_circuit(db_path: Path) -> None:
    config = CircuitConfig(rate_limit_threshold=2, rate_limit_probe_delay_seconds=10)
    with _connection(db_path) as connection:
        stale = _plan(connection, _spec("WIX"), run_id="stale", config=config)
        first = _plan(connection, _spec("RBRK"), run_id="first", config=config)
        _record(
            connection,
            first,
            (OutcomeCode.RATE_LIMITED,),
            run_id="first",
            statuses=(429,),
            retry_after_at=NOW + timedelta(minutes=5),
        )
        _record(
            connection,
            stale,
            (OutcomeCode.RATE_LIMITED,),
            run_id="stale",
            statuses=(429,),
            retry_after_at=NOW + timedelta(minutes=30),
        )
        next_probe_at = connection.execute(
            "SELECT next_probe_at FROM provider_circuit_state WHERE provider='fmp'"
        ).fetchone()[0]
        assert next_probe_at == (NOW + timedelta(minutes=30)).isoformat()


@pytest.mark.parametrize(
    "code, status",
    [
        (OutcomeCode.HTTP_UNAUTHORIZED, 401),
        (OutcomeCode.ACCOUNT_PAYMENT_REQUIRED, 402),
        (OutcomeCode.ACCOUNT_AUTH_FORBIDDEN, 403),
    ],
)
def test_account_auth_outcomes_open_immediately(
    db_path: Path, code: OutcomeCode, status: int
) -> None:
    with _connection(db_path) as connection:
        plan = _plan(connection, _spec("RBRK"))
        receipt = _record(connection, plan, (code,), statuses=(status,))
        row = connection.execute(
            "SELECT state,last_reason_code FROM provider_circuit_state WHERE provider='fmp'"
        ).fetchone()
        assert row[0] == CircuitState.OPEN.value
        assert row[1] == code.value
        assert receipt.status is ReceiptStatus.FAILED


def test_endpoint_forbidden_is_terminal_without_poisoning_provider(db_path: Path) -> None:
    with _connection(db_path) as connection:
        plan = _plan(connection, _spec("RBRK"))
        _record(
            connection,
            plan,
            (OutcomeCode.ENDPOINT_FORBIDDEN,),
            statuses=(403,),
        )
        circuit = connection.execute(
            "SELECT state,consecutive_failures FROM provider_circuit_state WHERE provider='fmp'"
        ).fetchone()
        work = connection.execute(
            "SELECT state FROM fmp_work_backlog WHERE work_id=?", (plan.items[0].work_id,)
        ).fetchone()
        assert tuple(circuit) == (CircuitState.CLOSED.value, 0)
        assert work[0] == WorkState.TERMINAL.value


def test_stale_live_success_cannot_close_a_newer_auth_circuit(db_path: Path) -> None:
    with _connection(db_path) as connection:
        stale_live = _plan(connection, _spec("RBRK"), run_id="stale-live")
        _plan(
            connection,
            _spec("WIX", corpus_snapshot=_corpus()),
            run_id="auth-failure",
            credentials=CredentialAvailability.MISSING,
        )
        receipt = _record(
            connection,
            stale_live,
            (OutcomeCode.LIVE_SUCCESS,),
            run_id="stale-live",
            statuses=(200,),
        )
        state = connection.execute(
            "SELECT state,last_reason_code FROM provider_circuit_state WHERE provider='fmp'"
        ).fetchone()
        assert tuple(state) == (CircuitState.OPEN.value, "auth_missing")
        assert receipt.status is ReceiptStatus.FRESH


@pytest.mark.parametrize(
    "outcome_code,http_status",
    [
        (OutcomeCode.ENDPOINT_FORBIDDEN, 403),
        (OutcomeCode.CLIENT_CONTRACT_ERROR, 422),
    ],
)
def test_non_global_probe_failure_reopens_instead_of_stranding_half_open(
    db_path: Path,
    outcome_code: OutcomeCode,
    http_status: int,
) -> None:
    config = CircuitConfig(auth_probe_delay_seconds=1, probe_delay_seconds=10)
    spec = _spec("RBRK", corpus_snapshot=_corpus())
    with _connection(db_path) as connection:
        corpus_plan = _plan(
            connection,
            spec,
            credentials=CredentialAvailability.MISSING,
            config=config,
        )
        _record(connection, corpus_plan, (OutcomeCode.CORPUS_SUCCESS,))
        probe_at = NOW + timedelta(seconds=1)
        probe = _plan(connection, spec, run_id="probe", now=probe_at, config=config)
        _record(
            connection,
            probe,
            (outcome_code,),
            run_id="probe",
            now=probe_at,
            statuses=(http_status,),
        )
        state = connection.execute(
            "SELECT state,last_reason_code,next_probe_at FROM provider_circuit_state "
            "WHERE provider='fmp'"
        ).fetchone()
        assert tuple(state) == (
            CircuitState.OPEN.value,
            outcome_code.value,
            (probe_at + timedelta(seconds=10)).isoformat(),
        )


def test_subset_success_receipt_is_partial_while_same_run_work_is_unresolved(
    db_path: Path,
) -> None:
    with _connection(db_path) as connection:
        plan = _plan(connection, _spec("RBRK"), _spec("WIX"), run_id="subset")
        first = RunPlan(
            run_id=plan.run_id,
            circuit_state=plan.circuit_state,
            circuit_revision=plan.circuit_revision,
            items=(plan.items[0],),
            backlog=plan.backlog,
        )
        receipt = _record(
            connection,
            first,
            (OutcomeCode.LIVE_SUCCESS,),
            run_id="subset",
            statuses=(200,),
            expected_work_ids=tuple(item.work_id for item in plan.items),
        )
        assert receipt.status is ReceiptStatus.PARTIAL
        assert receipt.backlog.leased_count == 1


def test_only_one_half_open_probe_is_leased_across_connections(db_path: Path) -> None:
    config = CircuitConfig(auth_probe_delay_seconds=10)
    first = _spec("RBRK", corpus_snapshot=_corpus())
    second = _spec("WIX", corpus_snapshot=_corpus())
    with _connection(db_path) as connection:
        corpus_plan = _plan(
            connection,
            first,
            second,
            credentials=CredentialAvailability.MISSING,
            config=config,
        )
        _record(
            connection,
            corpus_plan,
            (OutcomeCode.CORPUS_SUCCESS, OutcomeCode.CORPUS_SUCCESS),
        )
    probe_at = NOW + timedelta(seconds=10)
    with _connection(db_path) as connection_a:
        plan_a = _plan(
            connection_a,
            first,
            run_id="probe-a",
            now=probe_at,
            config=config,
        )
        assert plan_a.items[0].execution_mode is ExecutionMode.PROBE
    with _connection(db_path) as connection_b:
        plan_b = _plan(
            connection_b,
            second,
            run_id="probe-b",
            now=probe_at,
            config=config,
        )
        assert plan_b.items[0].execution_mode is ExecutionMode.ALREADY_APPLIED_CORPUS
        row = connection_b.execute(
            "SELECT state,probe_work_id,last_probe_at FROM provider_circuit_state "
            "WHERE provider='fmp'"
        ).fetchone()
        assert tuple(row) == (
            CircuitState.HALF_OPEN.value,
            plan_a.items[0].work_id,
            probe_at.isoformat(),
        )


def test_expired_lease_is_reclaimed_and_stale_owner_cannot_record(db_path: Path) -> None:
    spec = _spec("RBRK")
    with _connection(db_path) as connection:
        old = _plan(connection, spec, lease_seconds=5)
        new = _plan(
            connection,
            spec,
            run_id="run-2",
            now=NOW + timedelta(seconds=6),
        )
        assert old.items[0].work_id == new.items[0].work_id
        assert old.items[0].lease_token != new.items[0].lease_token
        with pytest.raises(ValueError, match="lease"):
            _record(
                connection,
                old,
                (OutcomeCode.LIVE_SUCCESS,),
                now=NOW + timedelta(seconds=6),
                statuses=(200,),
            )


def test_expired_lease_cannot_record_without_an_intervening_planner(db_path: Path) -> None:
    with _connection(db_path) as connection:
        plan = _plan(connection, _spec("RBRK"), lease_seconds=1)
        with pytest.raises(ValueError, match="expired"):
            _record(
                connection,
                plan,
                (OutcomeCode.LIVE_SUCCESS,),
                now=NOW + timedelta(seconds=2),
                statuses=(200,),
            )


def test_invalid_outcome_batch_rolls_back_all_attempts(db_path: Path) -> None:
    with _connection(db_path) as connection:
        plan = _plan(connection, _spec("RBRK"), _spec("WIX"))
        valid, invalid = plan.items
        if valid.lease_token is None:
            raise AssertionError("valid fixture work must be leased")
        if (
            valid.cache_generation_id is None
            or valid.policy_sha256 is None
            or invalid.cache_generation_id is None
            or invalid.policy_sha256 is None
        ):
            raise AssertionError("fixture work must expose its persisted identity")
        valid_proof = FmpSnapshotProof(
            work_id=valid.work_id,
            cache_generation_id=valid.cache_generation_id,
            policy_sha256=valid.policy_sha256,
            content_sha256=CONTENT_A,
            captured_at=NOW,
        )
        invalid_proof = FmpSnapshotProof(
            work_id=invalid.work_id,
            cache_generation_id=invalid.cache_generation_id,
            policy_sha256=invalid.policy_sha256,
            content_sha256=CONTENT_A,
            captured_at=NOW,
        )
        with pytest.raises(ValueError, match="lease"):
            record_outcomes(
                connection,
                RecordOutcomesRequest(
                    run_id="run-1",
                    now=NOW,
                    expected_work_ids=tuple(item.work_id for item in plan.items),
                    outcomes=(
                        WorkOutcome(
                            work_id=valid.work_id,
                            lease_token=valid.lease_token,
                            outcome_code=OutcomeCode.LIVE_SUCCESS,
                            observed_at=NOW,
                            http_status=200,
                            fmp_snapshot=valid_proof,
                        ),
                        WorkOutcome(
                            work_id=invalid.work_id,
                            lease_token="wrong-token",
                            outcome_code=OutcomeCode.LIVE_SUCCESS,
                            observed_at=NOW,
                            http_status=200,
                            fmp_snapshot=invalid_proof,
                        ),
                    ),
                ),
            )
        assert connection.execute("SELECT COUNT(*) FROM fmp_work_attempts").fetchone()[0] == 0
        states = connection.execute("SELECT DISTINCT state FROM fmp_work_backlog").fetchall()
        assert [row[0] for row in states] == [WorkState.LEASED.value]


def test_complete_alternative_resolution_is_persisted_and_satisfies_work(
    db_path: Path,
) -> None:
    with pytest.raises(ValidationError, match="complete"):
        AlternativeResolution(
            source=AlternativeSource.SEC,
            policy_sha256=POLICY_A,
            endpoint_key="income_statement_quarterly",
            period_key="quarterly:last-5",
            concept_keys=("revenue",),
            evidence_fresh_at=NOW - timedelta(days=1),
            source_authorized=True,
            has_unresolved_disagreement=False,
            coverage_state=AlternativeCoverageState.PARTIAL,
            evidence_ids=(11,),
            fact_ids=(22,),
        )
    resolution = _resolution()
    spec = _spec("RBRK", alternative_resolution=resolution)
    with _connection(db_path) as connection:
        plan = _plan(
            connection,
            spec,
            credentials=CredentialAvailability.MISSING,
        )
        assert plan.items[0].execution_mode is ExecutionMode.ALTERNATIVE
        receipt = _record(connection, plan, (OutcomeCode.ALTERNATIVE_SUCCESS,))
        assert receipt.status is ReceiptStatus.FRESH
        attempt = connection.execute(
            "SELECT resolution_source,resolution_policy_sha256,coverage_proof_sha256,"
            "evidence_ids_json,fact_ids_json FROM fmp_work_attempts"
        ).fetchone()
        assert tuple(attempt) == (
            "sec",
            POLICY_A,
            resolution.canonical_proof_sha256,
            "[11,12]",
            "[22]",
        )


def test_same_run_replan_returns_complete_work_identity(db_path: Path) -> None:
    spec = _spec("RBRK")
    with _connection(db_path) as connection:
        first = _plan(connection, spec, run_id="resume")
        replay = _plan(connection, spec, run_id="resume")
        assert replay.items == first.items


def test_cache_write_before_outcome_reconciles_same_work_id_after_crash(
    db_path: Path,
) -> None:
    original = _spec("RBRK")
    proof = FmpSnapshotProof(
        work_id=make_work_id(original),
        cache_generation_id=original.cache_generation_id,
        policy_sha256=original.policy_sha256,
        content_sha256=CONTENT_A,
        captured_at=NOW + timedelta(seconds=2),
    )
    recovered = _spec("RBRK", fmp_snapshot=proof)
    with _connection(db_path) as connection:
        first = _plan(connection, original, lease_seconds=1)
        second = _plan(
            connection,
            recovered,
            run_id="reconcile-run",
            now=NOW + timedelta(seconds=2),
        )
        assert first.items[0].work_id == second.items[0].work_id
        assert second.items[0].execution_mode is ExecutionMode.RECONCILE
        receipt = _record(
            connection,
            second,
            (OutcomeCode.RECONCILED_SUCCESS,),
            run_id="reconcile-run",
            now=NOW + timedelta(seconds=2),
        )
        assert receipt.status is ReceiptStatus.FRESH
        assert (
            connection.execute(
                "SELECT state FROM fmp_work_backlog WHERE work_id=?", (second.items[0].work_id,)
            ).fetchone()[0]
            == WorkState.SATISFIED.value
        )


def test_snapshot_proof_is_bound_to_exact_work_and_live_success_requires_it(
    db_path: Path,
) -> None:
    first = _spec("RBRK")
    second = _spec("WIX")
    wrong_proof = FmpSnapshotProof(
        work_id=make_work_id(first),
        cache_generation_id=second.cache_generation_id,
        policy_sha256=second.policy_sha256,
        content_sha256=CONTENT_A,
        captured_at=NOW,
    )
    with pytest.raises(ValidationError, match="work generation and policy"):
        _spec("WIX", fmp_snapshot=wrong_proof)
    with _connection(db_path) as connection:
        plan = _plan(connection, first)
        item = plan.items[0]
        if item.lease_token is None:
            raise AssertionError("fixture work must be leased")
        with pytest.raises(ValidationError, match="snapshot proof"):
            WorkOutcome(
                work_id=item.work_id,
                lease_token=item.lease_token,
                outcome_code=OutcomeCode.LIVE_SUCCESS,
                observed_at=NOW,
                http_status=200,
            )


def test_identical_corpus_proof_is_not_rehydrated_repeatedly(db_path: Path) -> None:
    spec = _spec("RBRK", corpus_snapshot=_corpus())
    with _connection(db_path) as connection:
        first = _plan(connection, spec, credentials=CredentialAvailability.MISSING)
        _record(connection, first, (OutcomeCode.CORPUS_SUCCESS,))
        repeat = _plan(
            connection,
            spec,
            run_id="repeat",
            credentials=CredentialAvailability.MISSING,
        )
        assert repeat.items[0].execution_mode is ExecutionMode.ALREADY_APPLIED_CORPUS
        assert repeat.backlog.pending_count == 1


def test_portfolio_promotion_raises_existing_index_work_priority(db_path: Path) -> None:
    index = _spec("RBRK", role=ListType.INDEX_MEMBER, endpoint="profile")
    portfolio = _spec("RBRK", endpoint="profile")
    with _connection(db_path) as connection:
        initial = _plan(connection, index)
        connection.execute(
            "UPDATE fmp_work_backlog SET state='TERMINAL',lease_owner=NULL,lease_token=NULL,"
            "lease_run_id=NULL,lease_mode=NULL,lease_expires_at=NULL,terminal_reason_code='old' "
            "WHERE work_id=?",
            (initial.items[0].work_id,),
        )
        connection.commit()
        promoted = _plan(connection, portfolio, run_id="promoted")
        assert promoted.items[0].priority == 300
        row = connection.execute(
            "SELECT coverage_role,priority,state,terminal_reason_code FROM fmp_work_backlog "
            "WHERE work_id=?",
            (promoted.items[0].work_id,),
        ).fetchone()
        assert tuple(row) == ("portfolio", 300, WorkState.LEASED.value, None)


def test_alternative_resolution_must_match_period_and_be_undisputed() -> None:
    with pytest.raises(ValidationError, match="authorized and undisputed"):
        AlternativeResolution(
            source=AlternativeSource.SEC,
            policy_sha256=POLICY_A,
            endpoint_key="income_statement_quarterly",
            period_key="quarterly:last-5",
            concept_keys=("revenue",),
            evidence_fresh_at=NOW,
            source_authorized=True,
            has_unresolved_disagreement=True,
            coverage_state=AlternativeCoverageState.COMPLETE,
            evidence_ids=(1,),
            fact_ids=(2,),
        )
    with pytest.raises(ValidationError, match="policy, endpoint, and period"):
        _spec("RBRK", alternative_resolution=_resolution(period_key="annual:last-1"))


def test_recoverable_work_claims_existing_pending_item_with_typed_corpus_proof(
    db_path: Path,
) -> None:
    spec = _spec("RBRK")
    with _connection(db_path) as connection:
        initial = _plan(
            connection,
            spec,
            credentials=CredentialAvailability.MISSING,
        )
        assert initial.items[0].execution_mode is ExecutionMode.UNAVAILABLE
        leased = recoverable_work(
            connection,
            RecoverableWorkRequest(
                run_id="corpus-drain",
                worker_id="worker-b",
                now=NOW + timedelta(seconds=1),
                lease_seconds=30,
                credentials=CredentialAvailability.MISSING,
                availability=(
                    RecoveryAvailability(
                        work_id=initial.items[0].work_id,
                        corpus_snapshot=_corpus(),
                    ),
                ),
            ),
        )
        assert len(leased.items) == 1
        assert leased.items[0].execution_mode is ExecutionMode.CORPUS


def test_mixed_fresh_alternative_and_corpus_receipt_is_partial_and_replay_is_idempotent(
    db_path: Path,
) -> None:
    fresh = _spec("RBRK", alternative_resolution=_resolution())
    corpus = _spec("WIX", corpus_snapshot=_corpus())
    with _connection(db_path) as connection:
        plan = _plan(
            connection,
            fresh,
            corpus,
            run_id="mixed-run",
            credentials=CredentialAvailability.MISSING,
        )
        fresh_item, corpus_item = plan.items
        assert fresh_item.execution_mode is ExecutionMode.ALTERNATIVE
        assert corpus_item.execution_mode is ExecutionMode.CORPUS
        if fresh_item.lease_token is None or corpus_item.lease_token is None:
            raise AssertionError("mixed fixture work must be leased")
        request = RecordOutcomesRequest(
            run_id="mixed-run",
            now=NOW,
            expected_work_ids=tuple(item.work_id for item in plan.items),
            outcomes=(
                WorkOutcome(
                    work_id=fresh_item.work_id,
                    lease_token=fresh_item.lease_token,
                    outcome_code=OutcomeCode.ALTERNATIVE_SUCCESS,
                    observed_at=NOW,
                    alternative_resolution=fresh_item.alternative_resolution,
                ),
                WorkOutcome(
                    work_id=corpus_item.work_id,
                    lease_token=corpus_item.lease_token,
                    outcome_code=OutcomeCode.CORPUS_SUCCESS,
                    observed_at=NOW,
                    corpus_snapshot=corpus_item.corpus_snapshot,
                ),
            ),
        )
        receipt = record_outcomes(connection, request)
        replay = record_outcomes(connection, request)
        assert receipt.status is ReceiptStatus.PARTIAL
        assert replay == receipt
        assert connection.execute("SELECT COUNT(*) FROM fmp_work_attempts").fetchone()[0] == 2


def test_fresh_receipt_accounts_for_unavailable_items_in_the_same_plan(
    db_path: Path,
) -> None:
    resolvable = _spec("RBRK", alternative_resolution=_resolution())
    unavailable = _spec("WIX")
    with _connection(db_path) as connection:
        plan = _plan(
            connection,
            resolvable,
            unavailable,
            run_id="mixed-availability",
            credentials=CredentialAvailability.MISSING,
        )
        alternative = next(
            item for item in plan.items if item.execution_mode is ExecutionMode.ALTERNATIVE
        )
        assert any(item.execution_mode is ExecutionMode.UNAVAILABLE for item in plan.items)
        if alternative.lease_token is None:
            raise AssertionError("alternative fixture must be leased")
        receipt = record_outcomes(
            connection,
            RecordOutcomesRequest(
                run_id="mixed-availability",
                now=NOW,
                expected_work_ids=tuple(item.work_id for item in plan.items),
                outcomes=(
                    WorkOutcome(
                        work_id=alternative.work_id,
                        lease_token=alternative.lease_token,
                        outcome_code=OutcomeCode.ALTERNATIVE_SUCCESS,
                        observed_at=NOW,
                        alternative_resolution=alternative.alternative_resolution,
                    ),
                ),
            ),
        )
        assert receipt.status is ReceiptStatus.PARTIAL
        assert receipt.fresh_count == 1
        assert receipt.backlog.pending_count == 1
