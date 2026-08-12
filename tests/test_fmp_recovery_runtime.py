# pyright: reportPrivateUsage=false
"""Runtime integration for the durable FMP recovery foundation."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

import execution.refresh_cache as refresh_cache
from models.companies import ListType
from pipeline.fmp_recovery import (
    CircuitConfig,
    CircuitState,
    CredentialAvailability,
    ExecutionMode,
    FmpSnapshotProof,
    OutcomeCode,
    PlanRunRequest,
    ReceiptStatus,
    WorkOutcome,
    WorkSpec,
    plan_run,
)
from provenance.financial_fact_resolution import governed_document_fact_admission

REVISION = "0008_add_fmp_recovery"
NOW = datetime(2026, 8, 12, 9, 0, 0)
CONTENT = "c" * 64


def test_repository_clock_normalizes_pacific_time_to_naive_utc() -> None:
    pacific = datetime(2026, 8, 12, 0, 30, tzinfo=ZoneInfo("America/Los_Angeles"))
    assert refresh_cache._naive_utc(pacific) == datetime(2026, 8, 12, 7, 30)


def _item(ticker: str, *, suffix: str = "income_statement_quarterly") -> refresh_cache.QueueItem:
    return refresh_cache.QueueItem(
        ticker=ticker,
        list_type=ListType.PORTFOLIO.value,
        endpoint="income-statement",
        period="quarter",
        suffix=suffix,
        endpoint_class="statement",
        bucket="missing",
        last_pulled=None,
        last_status=None,
        days_overdue=99,
        priority=0,
    )


def _connection(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def _live_outcome(
    connection: sqlite3.Connection,
    _item: refresh_cache.QueueItem,
    planned: refresh_cache.PlannedWork,
    *,
    observed_at: datetime = NOW,
) -> WorkOutcome:
    assert not connection.in_transaction
    assert (
        connection.execute(
            "SELECT state FROM fmp_work_backlog WHERE work_id=?", (planned.work_id,)
        ).fetchone()[0]
        == "LEASED"
    )
    assert planned.lease_token is not None
    assert planned.cache_generation_id is not None
    assert planned.policy_sha256 is not None
    return WorkOutcome(
        work_id=planned.work_id,
        lease_token=planned.lease_token,
        outcome_code=OutcomeCode.LIVE_SUCCESS,
        observed_at=observed_at,
        http_status=200,
        fmp_snapshot=FmpSnapshotProof(
            work_id=planned.work_id,
            cache_generation_id=planned.cache_generation_id,
            policy_sha256=planned.policy_sha256,
            content_sha256=CONTENT,
            captured_at=observed_at,
        ),
    )


def _unexpected_dispatch(
    _connection: sqlite3.Connection,
    _item: refresh_cache.QueueItem,
    _planned: refresh_cache.PlannedWork,
) -> WorkOutcome:
    raise AssertionError("no provider dispatch expected")


def _admit_fixture_corpus(
    _connection: sqlite3.Connection,
    _item: refresh_cache.QueueItem,
    planned: refresh_cache.PlannedWork,
    _raw_dir: Path,
    _project_root: Path,
    observed_at: datetime,
) -> WorkOutcome:
    assert planned.lease_token is not None
    assert planned.corpus_snapshot is not None
    return WorkOutcome(
        work_id=planned.work_id,
        lease_token=planned.lease_token,
        outcome_code=OutcomeCode.CORPUS_SUCCESS,
        observed_at=observed_at,
        corpus_snapshot=planned.corpus_snapshot,
    )


def test_missing_auth_uses_read_only_corpus_without_dispatch_or_freshness_advance(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    db_path = migrated_db(tmp_path / "runtime.db", target=REVISION)
    raw_dir = tmp_path / "fmp"
    raw_dir.mkdir()
    items = (_item("RBRK"), _item("WIX"))
    before: dict[Path, tuple[str, int]] = {}
    for item in items:
        path = raw_dir / f"{item.ticker}_{item.suffix}.json"
        path.write_text(f'{{"ticker":"{item.ticker}"}}', encoding="utf-8")
        captured = NOW - timedelta(days=2)
        timestamp = captured.timestamp()
        os.utime(path, (timestamp, timestamp))
        before[path] = (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns)

    connection = _connection(db_path)
    try:
        old_pulled = (NOW - timedelta(days=30)).isoformat()
        connection.execute(
            "INSERT INTO fmp_endpoint_status "
            "(ticker,endpoint,period,status,last_pulled) VALUES (?,?,?,?,?)",
            ("RBRK", "income-statement", "quarter", "ok", old_pulled),
        )
        connection.commit()

        def forbidden_dispatch(*_args: object) -> WorkOutcome:
            raise AssertionError("corpus mode must not dispatch FMP")

        result = refresh_cache.run_recovery_batch(
            connection,
            items=items,
            credentials=CredentialAvailability.MISSING,
            raw_corpus_dir=raw_dir,
            now=NOW,
            run_id="missing-auth",
            dispatch=forbidden_dispatch,
            corpus_admitter=_admit_fixture_corpus,
        )

        assert result.status is ReceiptStatus.DEGRADED_CORPUS
        assert result.exit_code == 2
        assert result.dispatch_count == 0
        assert result.corpus_count == 2
        assert (
            connection.execute(
                "SELECT last_pulled FROM fmp_endpoint_status WHERE ticker='RBRK'"
            ).fetchone()[0]
            == old_pulled
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM fmp_work_backlog WHERE state='PENDING'"
            ).fetchone()[0]
            == 2
        )
    finally:
        connection.close()

    after = {
        path: (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns)
        for path in before
    }
    assert after == before


def test_real_corpus_admission_reclaims_crashed_lease_without_duplicate_facts(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    project_root = tmp_path / "repo"
    raw_dir = project_root / "data" / "historical" / "fmp"
    raw_dir.mkdir(parents=True)
    raw_path = raw_dir / "RBRK_income_statement_quarterly.json"
    raw_path.write_text(
        json.dumps(
            [
                {
                    "date": "2026-07-31",
                    "symbol": "RBRK",
                    "reportedCurrency": "USD",
                    "period": "Q2",
                    "fiscalYear": "2026",
                    "revenue": 310000000,
                    "netIncome": -42000000,
                }
            ]
        ),
        encoding="utf-8",
    )
    captured = NOW - timedelta(days=2)
    os.utime(raw_path, (captured.timestamp(), captured.timestamp()))
    raw_before = (hashlib.sha256(raw_path.read_bytes()).hexdigest(), raw_path.stat().st_mtime_ns)
    db_path = migrated_db(project_root / "data" / "runtime.db", target=REVISION)
    connection = _connection(db_path)
    old_pulled = (NOW - timedelta(days=30)).isoformat()
    try:
        connection.execute(
            "INSERT INTO fmp_endpoint_status "
            "(ticker,endpoint,period,status,last_pulled) VALUES (?,?,?,?,?)",
            ("RBRK", "income-statement", "quarter", "ok", old_pulled),
        )
        connection.commit()

        def crash_after_extractor_commit(
            conn: sqlite3.Connection,
            item: refresh_cache.QueueItem,
            planned: refresh_cache.PlannedWork,
            corpus_dir: Path,
            root: Path,
            observed_at: datetime,
        ) -> WorkOutcome:
            outcome = refresh_cache._admit_corpus(
                conn,
                item,
                planned,
                corpus_dir,
                root,
                observed_at,
            )
            assert outcome.outcome_code is OutcomeCode.CORPUS_SUCCESS
            raise RuntimeError("simulated crash before recovery outcome recording")

        with pytest.raises(RuntimeError, match="simulated crash"):
            refresh_cache.run_recovery_batch(
                connection,
                items=(_item("RBRK"),),
                credentials=CredentialAvailability.MISSING,
                raw_corpus_dir=raw_dir,
                project_root=project_root,
                now=NOW,
                run_id="real-corpus-crash",
                dispatch=_unexpected_dispatch,
                corpus_admitter=crash_after_extractor_commit,
                provider_call_budget=0,
            )
        leased = connection.execute(
            "SELECT state,lease_token,lease_expires_at FROM fmp_work_backlog"
        ).fetchone()
        assert leased is not None
        assert leased["state"] == "LEASED"
        assert leased["lease_token"] is not None
        facts_after_first = int(
            connection.execute(
                "SELECT COUNT(*) FROM financial_facts WHERE ticker='RBRK'"
            ).fetchone()[0]
        )
        assert facts_after_first > 0
        document = connection.execute(
            "SELECT id,sha256 FROM documents WHERE ticker='RBRK'"
        ).fetchone()
        assert document is not None
        replay_proof = governed_document_fact_admission(
            connection,
            document_id=int(document["id"]),
            ticker="RBRK",
            content_sha256=str(document["sha256"]),
            inserted_count=0,
        )
        assert replay_proof.status == "idempotent_replay"
        assert replay_proof.total_admitted_count == facts_after_first
        second = refresh_cache.run_recovery_batch(
            connection,
            items=(_item("RBRK"),),
            credentials=CredentialAvailability.MISSING,
            raw_corpus_dir=raw_dir,
            project_root=project_root,
            now=NOW + timedelta(minutes=6),
            run_id="real-corpus-reclaim",
            dispatch=_unexpected_dispatch,
            provider_call_budget=0,
        )
        assert second.status is ReceiptStatus.DEGRADED_CORPUS
        assert second.corpus_count == 1
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM financial_facts WHERE ticker='RBRK'"
            ).fetchone()[0]
            == facts_after_first
        )
        reclaimed = connection.execute(
            "SELECT state,lease_token,lease_expires_at FROM fmp_work_backlog"
        ).fetchone()
        assert reclaimed is not None
        assert reclaimed["state"] == "PENDING"
        assert reclaimed["lease_token"] is None
        assert reclaimed["lease_expires_at"] is None
        assert (
            connection.execute(
                "SELECT last_pulled FROM fmp_endpoint_status WHERE ticker='RBRK'"
            ).fetchone()[0]
            == old_pulled
        )
    finally:
        connection.close()
    assert (
        hashlib.sha256(raw_path.read_bytes()).hexdigest(),
        raw_path.stat().st_mtime_ns,
    ) == raw_before


def test_real_corpus_admission_keeps_exact_governed_empty_document_unavailable(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    project_root = tmp_path / "repo"
    raw_dir = project_root / "data" / "historical" / "fmp"
    raw_dir.mkdir(parents=True)
    raw_path = raw_dir / "RBRK_income_statement_quarterly.json"
    raw_path.write_text(
        json.dumps([{"date": "2026-07-31", "symbol": "RBRK", "period": "Q2"}]),
        encoding="utf-8",
    )
    captured = NOW - timedelta(days=2)
    os.utime(raw_path, (captured.timestamp(), captured.timestamp()))
    db_path = migrated_db(project_root / "data" / "runtime.db", target=REVISION)
    connection = _connection(db_path)
    try:
        result = refresh_cache.run_recovery_batch(
            connection,
            items=(_item("RBRK"),),
            credentials=CredentialAvailability.MISSING,
            raw_corpus_dir=raw_dir,
            project_root=project_root,
            now=NOW,
            run_id="real-corpus-empty",
            dispatch=_unexpected_dispatch,
            provider_call_budget=0,
        )
        assert result.status is ReceiptStatus.FAILED
        assert result.corpus_count == 0
        document = connection.execute(
            "SELECT id,sha256 FROM documents WHERE ticker='RBRK'"
        ).fetchone()
        assert document is not None
        proof = governed_document_fact_admission(
            connection,
            document_id=int(document["id"]),
            ticker="RBRK",
            content_sha256=str(document["sha256"]),
            inserted_count=0,
        )
        assert proof.status == "empty"
        assert proof.total_admitted_count == 0
    finally:
        connection.close()


def test_zero_budget_still_persists_all_intended_work_before_processing_cap(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    db_path = migrated_db(tmp_path / "runtime.db", target=REVISION)
    connection = _connection(db_path)
    items = tuple(_item(f"C{i:03d}") for i in range(501))
    try:
        result = refresh_cache.run_recovery_batch(
            connection,
            items=items,
            credentials=CredentialAvailability.AVAILABLE,
            raw_corpus_dir=tmp_path / "missing",
            now=NOW,
            run_id="persist-overflow",
            dispatch=_unexpected_dispatch,
            max_items=1,
            provider_call_budget=0,
        )
        assert result.planned_count == 501
        assert result.dispatch_count == 0
        assert connection.execute("SELECT COUNT(*) FROM fmp_work_backlog").fetchone()[0] == 501
    finally:
        connection.close()


def test_zero_budget_missing_auth_still_hydrates_available_corpus(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    db_path = migrated_db(tmp_path / "runtime.db", target=REVISION)
    raw_dir = tmp_path / "fmp"
    raw_dir.mkdir()
    (raw_dir / "RBRK_income_statement_quarterly.json").write_text("[]", encoding="utf-8")
    connection = _connection(db_path)
    try:
        result = refresh_cache.run_recovery_batch(
            connection,
            items=(_item("RBRK"),),
            credentials=CredentialAvailability.MISSING,
            raw_corpus_dir=raw_dir,
            now=NOW,
            run_id="zero-budget-corpus",
            dispatch=_unexpected_dispatch,
            corpus_admitter=_admit_fixture_corpus,
            provider_call_budget=0,
        )
        assert result.status is ReceiptStatus.DEGRADED_CORPUS
        assert result.dispatch_count == 0
        assert result.corpus_count == 1
    finally:
        connection.close()


def test_account_failure_stops_dispatch_and_persists_unattempted_work_pending(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    db_path = migrated_db(tmp_path / "runtime.db", target=REVISION)
    connection = _connection(db_path)
    calls: list[str] = []

    def unauthorized(
        conn: sqlite3.Connection,
        item: refresh_cache.QueueItem,
        planned: refresh_cache.PlannedWork,
    ) -> WorkOutcome:
        assert not conn.in_transaction
        calls.append(item.ticker)
        assert planned.lease_token is not None
        return WorkOutcome(
            work_id=planned.work_id,
            lease_token=planned.lease_token,
            outcome_code=OutcomeCode.HTTP_UNAUTHORIZED,
            observed_at=NOW,
            http_status=401,
        )

    try:
        result = refresh_cache.run_recovery_batch(
            connection,
            items=tuple(_item(ticker) for ticker in ("RBRK", "WIX", "META")),
            credentials=CredentialAvailability.AVAILABLE,
            raw_corpus_dir=tmp_path / "missing-corpus",
            now=NOW,
            run_id="auth-failure",
            dispatch=unauthorized,
        )

        assert calls == ["META"]
        assert result.status is ReceiptStatus.FAILED
        assert result.exit_code == 4
        assert result.dispatch_count == 1
        assert result.failed_count == 3
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM fmp_work_backlog WHERE state='PENDING'"
            ).fetchone()[0]
            == 3
        )
        assert (
            connection.execute(
                "SELECT state FROM provider_circuit_state WHERE provider='fmp'"
            ).fetchone()[0]
            == "OPEN"
        )
    finally:
        connection.close()


def test_transient_threshold_stops_later_provider_calls(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    db_path = migrated_db(tmp_path / "runtime.db", target=REVISION)
    connection = _connection(db_path)
    calls: list[str] = []

    def transport_failure(
        _conn: sqlite3.Connection,
        item: refresh_cache.QueueItem,
        planned: refresh_cache.PlannedWork,
    ) -> WorkOutcome:
        calls.append(item.ticker)
        assert planned.lease_token is not None
        return WorkOutcome(
            work_id=planned.work_id,
            lease_token=planned.lease_token,
            outcome_code=OutcomeCode.TRANSPORT_ERROR,
            observed_at=NOW,
        )

    try:
        result = refresh_cache.run_recovery_batch(
            connection,
            items=tuple(_item(ticker) for ticker in ("RBRK", "WIX", "META")),
            credentials=CredentialAvailability.AVAILABLE,
            raw_corpus_dir=tmp_path / "missing-corpus",
            now=NOW,
            run_id="transient-threshold",
            dispatch=transport_failure,
            circuit_config=CircuitConfig(transient_failure_threshold=2),
        )

        assert calls == ["META", "RBRK"]
        assert result.dispatch_count == 2
        assert result.failed_count == 3
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM fmp_work_backlog WHERE state='PENDING'"
            ).fetchone()[0]
            == 3
        )
    finally:
        connection.close()


def test_due_probe_success_closes_circuit_then_drains_bounded_priority_work(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    db_path = migrated_db(tmp_path / "runtime.db", target=REVISION)
    connection = _connection(db_path)
    items = tuple(_item(ticker) for ticker in ("RBRK", "WIX", "META"))
    modes: list[ExecutionMode] = []

    def dispatch(
        conn: sqlite3.Connection,
        item: refresh_cache.QueueItem,
        planned: refresh_cache.PlannedWork,
    ) -> WorkOutcome:
        modes.append(planned.execution_mode)
        return _live_outcome(conn, item, planned, observed_at=NOW + timedelta(hours=7))

    try:
        refresh_cache.run_recovery_batch(
            connection,
            items=items,
            credentials=CredentialAvailability.MISSING,
            raw_corpus_dir=tmp_path / "missing-corpus",
            now=NOW,
            run_id="seed-open",
            dispatch=_unexpected_dispatch,
        )

        result = refresh_cache.run_recovery_batch(
            connection,
            items=items,
            credentials=CredentialAvailability.AVAILABLE,
            raw_corpus_dir=tmp_path / "missing-corpus",
            now=NOW + timedelta(hours=7),
            run_id="due-probe",
            dispatch=dispatch,
            max_items=3,
        )

        assert modes == [ExecutionMode.PROBE, ExecutionMode.LIVE, ExecutionMode.LIVE]
        assert result.status is ReceiptStatus.FRESH
        assert result.exit_code == 0
        assert result.dispatch_count == 3
        assert (
            connection.execute(
                "SELECT state FROM provider_circuit_state WHERE provider='fmp'"
            ).fetchone()[0]
            == "CLOSED"
        )
    finally:
        connection.close()


def test_due_empty_probe_proves_reachability_then_drains_priority_work(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    db_path = migrated_db(tmp_path / "runtime.db", target=REVISION)
    connection = _connection(db_path)
    items = tuple(_item(ticker) for ticker in ("RBRK", "WIX", "META"))
    modes: list[ExecutionMode] = []

    def dispatch(
        conn: sqlite3.Connection,
        item: refresh_cache.QueueItem,
        planned: refresh_cache.PlannedWork,
    ) -> WorkOutcome:
        modes.append(planned.execution_mode)
        if planned.execution_mode is ExecutionMode.PROBE:
            assert planned.lease_token is not None
            return WorkOutcome(
                work_id=planned.work_id,
                lease_token=planned.lease_token,
                outcome_code=OutcomeCode.ENDPOINT_EMPTY,
                observed_at=NOW + timedelta(hours=7),
                http_status=200,
            )
        return _live_outcome(conn, item, planned, observed_at=NOW + timedelta(hours=7))

    try:
        refresh_cache.run_recovery_batch(
            connection,
            items=items,
            credentials=CredentialAvailability.MISSING,
            raw_corpus_dir=tmp_path / "missing-corpus",
            now=NOW,
            run_id="seed-open-empty",
            dispatch=_unexpected_dispatch,
        )
        result = refresh_cache.run_recovery_batch(
            connection,
            items=items,
            credentials=CredentialAvailability.AVAILABLE,
            raw_corpus_dir=tmp_path / "missing-corpus",
            now=NOW + timedelta(hours=7),
            run_id="due-empty-probe",
            dispatch=dispatch,
            max_items=3,
        )
        assert modes == [ExecutionMode.PROBE, ExecutionMode.LIVE, ExecutionMode.LIVE]
        assert result.status is ReceiptStatus.PARTIAL
        assert result.dispatch_count == 3
        assert result.fresh_count == 2
        assert result.failed_count == 1
    finally:
        connection.close()


def test_structured_result_distinguishes_partial_from_failed(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    db_path = migrated_db(tmp_path / "runtime.db", target=REVISION)
    raw_dir = tmp_path / "fmp"
    raw_dir.mkdir()
    (raw_dir / "WIX_income_statement_quarterly.json").write_text("[]", encoding="utf-8")
    connection = _connection(db_path)
    try:
        result = refresh_cache.run_recovery_batch(
            connection,
            items=(_item("RBRK"), _item("WIX")),
            credentials=CredentialAvailability.MISSING,
            raw_corpus_dir=raw_dir,
            now=NOW,
            run_id="partial-corpus",
            dispatch=_unexpected_dispatch,
            corpus_admitter=_admit_fixture_corpus,
        )
        assert result.status is ReceiptStatus.PARTIAL
        assert result.exit_code == 3
        assert result.corpus_count == 1
        assert result.failed_count == 1
    finally:
        connection.close()


def test_open_circuit_selects_corpus_before_auth_resolution(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    db_path = migrated_db(tmp_path / "runtime.db", target=REVISION)
    connection = _connection(db_path)
    auth_reads = 0
    try:
        plan_run(
            connection,
            PlanRunRequest(
                run_id="open-circuit",
                worker_id="seed",
                now=NOW,
                credentials=CredentialAvailability.MISSING,
                work=(
                    WorkSpec(
                        ticker="RBRK",
                        coverage_role=ListType.PORTFOLIO,
                        endpoint_key="income_statement_quarterly",
                        period_key="quarter",
                        cache_generation_id="seed",
                        policy_sha256="a" * 64,
                    ),
                ),
            ),
        )

        def unexpected_auth() -> refresh_cache.FmpAuthConfig:
            nonlocal auth_reads
            auth_reads += 1
            raise AssertionError("open circuit must be checked before auth")

        decision = refresh_cache.decide_recovery_credentials(
            connection,
            now=NOW + timedelta(minutes=1),
            auth_loader=unexpected_auth,
        )

        assert auth_reads == 0
        assert not decision.network_permitted
        assert not decision.hints_permitted
        assert decision.auth is None
        assert (
            connection.execute(
                "SELECT state FROM provider_circuit_state WHERE provider='fmp'"
            ).fetchone()[0]
            == CircuitState.OPEN.value
        )
    finally:
        connection.close()


def test_missing_auth_decision_disables_network_and_hints(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    db_path = migrated_db(tmp_path / "runtime.db", target=REVISION)
    connection = _connection(db_path)
    try:
        decision = refresh_cache.decide_recovery_credentials(
            connection,
            now=NOW,
            auth_loader=lambda: (_ for _ in ()).throw(refresh_cache.FmpAuthError("missing")),
        )
        assert decision.credentials is CredentialAvailability.MISSING
        assert not decision.network_permitted
        assert not decision.hints_permitted
        assert decision.auth is None
    finally:
        connection.close()


def test_run_command_emits_degraded_receipt_without_hint_or_fmp_call(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = migrated_db(tmp_path / "runtime.db", target=REVISION)
    connection = _connection(db_path)
    raw_dir = tmp_path / "fmp"
    raw_dir.mkdir()
    (raw_dir / "RBRK_income_statement_quarterly.json").write_text("[]", encoding="utf-8")
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    hints_called = False

    monkeypatch.setattr(refresh_cache, "CACHE_DIR", cache_dir)
    monkeypatch.setattr(refresh_cache, "QUEUE_PATH", cache_dir / "queue.json")
    monkeypatch.setattr(refresh_cache, "FMP_DIR", raw_dir)

    def fake_connect_sqlite(
        _path: object,
        *,
        role: object,
        schema_preflight: bool | None = None,
    ) -> sqlite3.Connection:
        del role, schema_preflight
        return connection

    def fake_credential_decision(
        _connection: sqlite3.Connection,
        *,
        now: datetime,
    ) -> refresh_cache.RecoveryCredentialDecision:
        del now
        return refresh_cache.RecoveryCredentialDecision(
            credentials=CredentialAvailability.MISSING,
            auth=None,
            network_permitted=False,
            hints_permitted=False,
        )

    monkeypatch.setattr(refresh_cache, "connect_sqlite", fake_connect_sqlite)
    monkeypatch.setattr(
        refresh_cache,
        "decide_recovery_credentials",
        fake_credential_decision,
    )

    def unexpected_hints(**_kwargs: object) -> None:
        nonlocal hints_called
        hints_called = True

    monkeypatch.setattr(refresh_cache, "_maybe_refresh_earnings_hints", unexpected_hints)

    def fake_audit(
        _connection: sqlite3.Connection,
        *,
        only_list_types: frozenset[str] | None = None,
        explicit_tickers: list[str] | None = None,
        force: bool = False,
        now: datetime | None = None,
    ) -> refresh_cache.AuditReport:
        del only_list_types, explicit_tickers, force, now
        return refresh_cache.AuditReport(
            generated_at=NOW,
            items=[_item("RBRK")],
            counts={"missing": 1},
        )

    monkeypatch.setattr(refresh_cache, "audit", fake_audit)
    monkeypatch.setattr(refresh_cache, "_admit_corpus", _admit_fixture_corpus)
    args = argparse.Namespace(
        tier="basic",
        db=str(db_path),
        only=None,
        tickers=None,
        force=False,
        max_calls=1,
        dry_run=False,
    )

    exit_code = refresh_cache._run_under_lock(args)
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 2
    assert payload["status"] == ReceiptStatus.DEGRADED_CORPUS.value
    assert payload["dispatch_count"] == 0
    assert not hints_called


def test_empty_audit_still_runs_due_open_circuit_backlog_probe(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = migrated_db(tmp_path / "runtime.db", target=REVISION)
    connection = _connection(db_path)
    due_now = NOW + timedelta(hours=7)
    plan_run(
        connection,
        PlanRunRequest(
            run_id="seed-due-backlog",
            worker_id="seed",
            now=NOW,
            credentials=CredentialAvailability.MISSING,
            work=(
                WorkSpec(
                    ticker="RBRK",
                    coverage_role=ListType.PORTFOLIO,
                    endpoint_key="income_statement_quarterly",
                    period_key="quarter",
                    cache_generation_id="seed",
                    policy_sha256="a" * 64,
                ),
            ),
        ),
    )
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    monkeypatch.setattr(refresh_cache, "CACHE_DIR", cache_dir)
    monkeypatch.setattr(refresh_cache, "QUEUE_PATH", cache_dir / "queue.json")
    monkeypatch.setattr(refresh_cache, "FMP_DIR", tmp_path / "missing-corpus")
    monkeypatch.setattr(refresh_cache, "_utc_now", lambda: due_now)

    def fake_connect_sqlite(
        _path: object,
        *,
        role: object,
        schema_preflight: bool | None = None,
    ) -> sqlite3.Connection:
        del role, schema_preflight
        return connection

    monkeypatch.setattr(refresh_cache, "connect_sqlite", fake_connect_sqlite)

    def available_credentials(
        *_args: object,
        **_kwargs: object,
    ) -> refresh_cache.RecoveryCredentialDecision:
        return refresh_cache.RecoveryCredentialDecision(
            credentials=CredentialAvailability.AVAILABLE,
            auth=refresh_cache.FmpAuthConfig(api_key="test-key", source="environment"),
            network_permitted=True,
            hints_permitted=False,
        )

    monkeypatch.setattr(refresh_cache, "decide_recovery_credentials", available_credentials)

    def empty_audit(*_args: object, **_kwargs: object) -> refresh_cache.AuditReport:
        return refresh_cache.AuditReport(
            generated_at=due_now,
            items=[],
            counts={"fresh": 1},
        )

    monkeypatch.setattr(refresh_cache, "audit", empty_audit)
    modes: list[ExecutionMode] = []

    def successful_probe(
        conn: sqlite3.Connection,
        item: refresh_cache.QueueItem,
        planned: refresh_cache.PlannedWork,
        **_kwargs: object,
    ) -> WorkOutcome:
        modes.append(planned.execution_mode)
        return _live_outcome(conn, item, planned, observed_at=due_now)

    monkeypatch.setattr(refresh_cache, "_dispatch_one", successful_probe)
    args = argparse.Namespace(
        tier="basic",
        db=str(db_path),
        only=None,
        tickers=None,
        force=False,
        max_calls=1,
        dry_run=False,
    )

    assert refresh_cache._run_under_lock(args) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == ReceiptStatus.FRESH.value
    assert payload["planned_count"] == 0
    assert modes == [ExecutionMode.PROBE]
