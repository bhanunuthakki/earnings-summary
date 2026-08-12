# pyright: reportPrivateUsage=false
"""Runtime integration for the durable FMP recovery foundation."""

from __future__ import annotations

import argparse
import builtins
import hashlib
import json
import os
import sqlite3
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
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
    EnqueueWorkRequest,
    ExecutionMode,
    FmpSnapshotProof,
    OutcomeCode,
    PlanRunRequest,
    ReceiptStatus,
    RecoverableWorkRequest,
    RecoveryAvailability,
    WorkOutcome,
    WorkSpec,
    enqueue_work,
    make_work_id,
    plan_run,
    recoverable_work,
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


def test_offline_corpus_only_bypasses_external_seams_is_idempotent_and_preserves_corpus(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    capsys: pytest.CaptureFixture[str],
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
    (raw_dir / "LOW_income_statement_quarterly.json").write_text("[]", encoding="utf-8")
    db_path = migrated_db(project_root / "data" / "runtime.db", target=REVISION)
    seed = _connection(db_path)
    seed.execute(
        "INSERT INTO tracked_companies (ticker,name,list_type) VALUES ('RBRK','Rubrik','portfolio')"
    )
    seed.execute(
        "INSERT INTO tracked_companies (ticker,name,list_type) VALUES ('LOW','Lower tier','watchlist')"
    )
    seed.commit()
    plan_run(
        seed,
        PlanRunRequest(
            run_id="unrelated-existing-backlog",
            worker_id="seed",
            now=NOW,
            credentials=CredentialAvailability.MISSING,
            work=(
                WorkSpec(
                    ticker="META",
                    coverage_role=ListType.PORTFOLIO,
                    endpoint_key="balance_sheet_quarterly",
                    period_key="quarter",
                    cache_generation_id="unrelated-existing-backlog",
                    policy_sha256="a" * 64,
                ),
            ),
        ),
    )
    seed.close()
    before = refresh_cache._raw_corpus_manifest(raw_dir)

    monkeypatch.setattr(refresh_cache, "PROJECT_ROOT", project_root)
    monkeypatch.setattr(refresh_cache, "FMP_DIR", raw_dir)

    def forbidden(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("offline corpus replay touched an external seam")

    monkeypatch.setattr(refresh_cache, "resolve_tier", forbidden)
    monkeypatch.setattr(refresh_cache, "decide_recovery_credentials", forbidden)
    monkeypatch.setattr(refresh_cache, "load_fmp_auth", forbidden)
    monkeypatch.setattr(refresh_cache, "FmpAuthConfig", forbidden)
    monkeypatch.setattr(refresh_cache, "dotenv_values", forbidden)
    monkeypatch.setattr(refresh_cache, "managed_python_prefix", forbidden)
    monkeypatch.setattr(refresh_cache, "_maybe_refresh_earnings_hints", forbidden)
    monkeypatch.setattr(refresh_cache, "audit", forbidden)
    monkeypatch.setattr(refresh_cache, "_dispatch_one", forbidden)
    monkeypatch.setattr(refresh_cache.subprocess, "run", forbidden)
    monkeypatch.setattr(refresh_cache.subprocess, "Popen", forbidden)
    monkeypatch.delitem(sys.modules, "save_fmp_data", raising=False)
    original_import = builtins.__import__

    def guarded_import(
        name: str,
        globals_map: Mapping[str, object] | None = None,
        locals_map: Mapping[str, object] | None = None,
        fromlist: Sequence[str] = (),
        level: int = 0,
    ) -> object:
        if name == "save_fmp_data" or name.endswith(".save_fmp_data"):
            raise AssertionError("offline replay imported the provider dispatcher")
        return original_import(name, globals_map, locals_map, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    args = argparse.Namespace(
        db=str(db_path),
        only=None,
        tickers=None,
        force=False,
        max_calls=None,
        dry_run=False,
        offline_corpus_only=True,
    )

    first_exit = refresh_cache._run_offline_corpus_only(args)
    first = json.loads(capsys.readouterr().out)
    second_exit = refresh_cache._run_offline_corpus_only(args)
    second = json.loads(capsys.readouterr().out)

    assert first_exit == 2
    assert second_exit == 2
    assert first["mode"] == "offline_corpus_only"
    assert first["network_calls"] == 0
    assert first["status"] == ReceiptStatus.DEGRADED_CORPUS.value
    assert first["eligible_count"] == 1
    assert first["selected_count"] == 1
    assert first["admitted_count"] == 1
    assert first["admitted_new_count"] == 1
    assert first["already_applied_count"] == 0
    assert first["corpus_count"] == 1
    assert first["failed_count"] == 0
    assert first["deferred_count"] == 0
    assert first["excluded_by_tier_count"] == 1
    assert second["network_calls"] == 0
    assert second["corpus_count"] == 1
    assert second["admitted_new_count"] == 0
    assert second["already_applied_count"] == 1
    assert first["run_id"] != second["run_id"]
    assert first["pending_count"] == 1
    assert second["pending_count"] == 1
    assert refresh_cache._raw_corpus_manifest(raw_dir) == before
    facts = _connection(db_path)
    try:
        assert (
            facts.execute("SELECT COUNT(*) FROM financial_facts WHERE ticker='RBRK'").fetchone()[0]
            > 0
        )
        document_count = facts.execute(
            "SELECT COUNT(*) FROM documents WHERE ticker='RBRK' AND source_type='fmp'"
        ).fetchone()[0]
        assert document_count == 1
        assert (
            facts.execute(
                "SELECT COUNT(*) FROM fmp_work_attempts attempt "
                "JOIN fmp_work_backlog work ON work.work_id=attempt.work_id "
                "WHERE work.ticker='META'"
            ).fetchone()[0]
            == 0
        )
        attempts_by_run = facts.execute(
            "SELECT run_id,COUNT(*) FROM fmp_work_attempts WHERE work_id IN "
            "(SELECT work_id FROM fmp_work_backlog WHERE ticker='RBRK') GROUP BY run_id"
        ).fetchall()
        assert len(attempts_by_run) == 1
        assert attempts_by_run[0]["run_id"] == first["run_id"]
    finally:
        facts.close()


def test_offline_corpus_only_reports_partial_malformed_corpus_truthfully(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    project_root = tmp_path / "repo"
    raw_dir = project_root / "data" / "historical" / "fmp"
    raw_dir.mkdir(parents=True)
    (raw_dir / "RBRK_income_statement_quarterly.json").write_text(
        json.dumps(
            [
                {
                    "date": "2026-07-31",
                    "symbol": "RBRK",
                    "reportedCurrency": "USD",
                    "period": "Q2",
                    "fiscalYear": "2026",
                    "revenue": 310000000,
                }
            ]
        ),
        encoding="utf-8",
    )
    (raw_dir / "WIX_balance_sheet_quarterly.json").write_bytes(b"{not-json")
    db_path = migrated_db(project_root / "data" / "runtime.db", target=REVISION)
    seed = _connection(db_path)
    seed.executemany(
        "INSERT INTO tracked_companies (ticker,name,list_type) VALUES (?,?,?)",
        (("RBRK", "Rubrik", "portfolio"), ("WIX", "Wix", "portfolio")),
    )
    seed.commit()
    seed.close()
    before = refresh_cache._raw_corpus_manifest(raw_dir)
    monkeypatch.setattr(refresh_cache, "PROJECT_ROOT", project_root)
    monkeypatch.setattr(refresh_cache, "FMP_DIR", raw_dir)
    args = argparse.Namespace(
        db=str(db_path),
        only=None,
        tickers=None,
        force=False,
        max_calls=None,
        dry_run=False,
        offline_corpus_only=True,
    )

    exit_code = refresh_cache._run_offline_corpus_only(args)
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 3
    assert payload["status"] == ReceiptStatus.PARTIAL.value
    assert payload["network_calls"] == 0
    assert payload["eligible_count"] == 2
    assert payload["selected_count"] == 2
    assert payload["admitted_count"] == 1
    assert payload["admitted_new_count"] == 1
    assert payload["already_applied_count"] == 0
    assert payload["corpus_count"] == 1
    assert payload["failed_count"] == 1
    assert payload["deferred_count"] == 0
    assert payload["excluded_by_tier_count"] == 0
    assert refresh_cache._raw_corpus_manifest(raw_dir) == before


def test_offline_corpus_only_detects_same_size_restored_mtime_tampering(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    raw_dir = tmp_path / "fmp"
    raw_dir.mkdir()
    raw_path = raw_dir / "RBRK_income_statement_quarterly.json"
    raw_path.write_bytes(b"original")
    original_stat = raw_path.stat()

    def tamper_during_selection(
        _connection: sqlite3.Connection,
        *,
        raw_corpus_dir: Path,
        only_list_type: str | None,
        explicit_tickers: list[str] | None,
    ) -> tuple[list[refresh_cache.QueueItem], int]:
        assert raw_corpus_dir == raw_dir
        assert only_list_type is None
        assert explicit_tickers is None
        raw_path.write_bytes(b"tampered")
        os.utime(
            raw_path,
            ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns),
        )
        return [], 0

    monkeypatch.setattr(refresh_cache, "FMP_DIR", raw_dir)
    monkeypatch.setattr(refresh_cache, "_offline_corpus_items", tamper_during_selection)

    def in_memory_connection(
        _path: str,
        *,
        role: object,
    ) -> sqlite3.Connection:
        del role
        return sqlite3.connect(":memory:")

    monkeypatch.setattr(
        refresh_cache,
        "connect_sqlite",
        in_memory_connection,
    )
    args = argparse.Namespace(db=str(tmp_path / "runtime.db"), only=None, tickers=None)

    exit_code = refresh_cache._run_offline_corpus_only(args)
    stdout = capsys.readouterr().out.strip().splitlines()

    assert exit_code == 4
    assert len(stdout) == 1
    payload = json.loads(stdout[0])
    assert payload["status"] == ReceiptStatus.FAILED.value
    assert payload["manifest_unchanged"] is False
    assert payload["manifest_before_sha256"] != payload["manifest_after_sha256"]


def test_offline_corpus_only_all_deferred_emits_one_failed_receipt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    raw_dir = tmp_path / "fmp"
    raw_dir.mkdir()
    (raw_dir / "RBRK_income_statement_quarterly.json").write_bytes(b"[]")
    item = _item("RBRK")

    def deferred_recovery(*_args: object, **kwargs: object) -> refresh_cache.RecoveryRunResult:
        return refresh_cache.RecoveryRunResult(
            run_id=str(kwargs["run_id"]),
            status=ReceiptStatus.FAILED,
            planned_count=1,
            dispatch_count=0,
            fresh_count=0,
            corpus_count=0,
            admitted_new_count=0,
            already_applied_count=0,
            failed_count=0,
            circuit_state=CircuitState.OPEN,
            circuit_revision=1,
            pending_count=1,
        )

    def selected_item(
        _connection: sqlite3.Connection,
        *,
        raw_corpus_dir: Path,
        only_list_type: str | None,
        explicit_tickers: list[str] | None,
    ) -> tuple[list[refresh_cache.QueueItem], int]:
        del raw_corpus_dir, only_list_type, explicit_tickers
        return [item], 0

    def in_memory_connection(
        _path: str,
        *,
        role: object,
    ) -> sqlite3.Connection:
        del role
        return sqlite3.connect(":memory:")

    def no_failures(
        _connection: sqlite3.Connection,
        *,
        run_id: str,
        intended_work_ids: frozenset[str],
    ) -> int:
        del run_id, intended_work_ids
        return 0

    def one_pending(
        _connection: sqlite3.Connection,
        *,
        intended_work_ids: frozenset[str],
    ) -> int:
        del intended_work_ids
        return 1

    monkeypatch.setattr(refresh_cache, "FMP_DIR", raw_dir)
    monkeypatch.setattr(refresh_cache, "_offline_corpus_items", selected_item)
    monkeypatch.setattr(
        refresh_cache,
        "connect_sqlite",
        in_memory_connection,
    )
    monkeypatch.setattr(refresh_cache, "run_recovery_batch", deferred_recovery)
    monkeypatch.setattr(refresh_cache, "_offline_failed_count", no_failures)
    monkeypatch.setattr(refresh_cache, "_offline_pending_count", one_pending)
    args = argparse.Namespace(db=str(tmp_path / "runtime.db"), only=None, tickers=None)

    exit_code = refresh_cache._run_offline_corpus_only(args)
    stdout = capsys.readouterr().out.strip().splitlines()

    assert exit_code == 4
    assert len(stdout) == 1
    payload = json.loads(stdout[0])
    assert payload["status"] == ReceiptStatus.FAILED.value
    assert payload["admitted_count"] == 0
    assert payload["deferred_count"] == 1


@pytest.mark.skipif(os.name != "nt", reason="Windows sharing semantics")
def test_offline_admission_handle_denies_refresh_overwrite_and_delete(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    raw_dir = tmp_path / "fmp"
    raw_dir.mkdir()
    raw_path = raw_dir / "RBRK_income_statement_quarterly.json"
    original = b"[]"
    raw_path.write_bytes(original)
    attempted_errors: list[OSError] = []

    def held_admission(
        _connection: sqlite3.Connection,
        _item_value: refresh_cache.QueueItem,
        planned_value: refresh_cache.PlannedWork,
        _raw_corpus_dir: Path,
        _project_root: Path,
        observed_at: datetime,
        _held: object,
    ) -> WorkOutcome:
        def refresh_writer() -> None:
            for mutation in (
                lambda: raw_path.write_bytes(b"{}"),
                raw_path.unlink,
            ):
                try:
                    mutation()
                except OSError as exc:
                    attempted_errors.append(exc)

        writer = threading.Thread(target=refresh_writer)
        writer.start()
        writer.join(timeout=5)
        assert not writer.is_alive()
        return WorkOutcome(
            work_id=planned_value.work_id,
            lease_token="lease",
            outcome_code=OutcomeCode.CORPUS_SUCCESS,
            observed_at=observed_at,
            corpus_snapshot=planned_value.corpus_snapshot,
        )

    monkeypatch.setattr(refresh_cache, "_admit_held_corpus", held_admission)
    snapshot = refresh_cache._corpus_snapshot(raw_path, root=raw_dir)
    assert snapshot is not None
    planned = refresh_cache.PlannedWork(
        work_id="a" * 64,
        ticker="RBRK",
        priority=0,
        endpoint_key="income_statement_quarterly",
        period_key="quarter",
        execution_mode=ExecutionMode.CORPUS,
        lease_token="lease",
        corpus_snapshot=snapshot,
    )

    outcome = refresh_cache._admit_corpus(
        sqlite3.connect(":memory:"),
        _item("RBRK"),
        planned,
        raw_dir,
        tmp_path,
        NOW,
    )

    assert outcome.outcome_code is OutcomeCode.CORPUS_SUCCESS
    assert len(attempted_errors) == 2
    assert raw_path.read_bytes() == original


def test_recoverable_work_filters_allowed_ids_before_global_limit(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    db_path = migrated_db(tmp_path / "runtime.db", target=REVISION)
    connection = _connection(db_path)
    unrelated = tuple(
        WorkSpec(
            ticker=f"P{i:04d}",
            coverage_role=ListType.PORTFOLIO,
            endpoint_key="income_statement_quarterly",
            period_key="quarter",
            cache_generation_id="starvation",
            policy_sha256="a" * 64,
        )
        for i in range(500)
    )
    selected = WorkSpec(
        ticker="IDX",
        coverage_role=ListType.INDEX_MEMBER,
        endpoint_key="income_statement_quarterly",
        period_key="quarter",
        cache_generation_id="starvation",
        policy_sha256="b" * 64,
    )
    try:
        enqueue_work(
            connection,
            EnqueueWorkRequest(now=NOW, work=unrelated),
        )
        enqueue_work(
            connection,
            EnqueueWorkRequest(now=NOW, work=(selected,)),
        )
        selected_id = make_work_id(selected)

        plan = recoverable_work(
            connection,
            RecoverableWorkRequest(
                run_id="selected-after-500",
                worker_id="test",
                now=NOW,
                credentials=CredentialAvailability.MISSING,
                provider_calls_permitted=False,
                availability=(RecoveryAvailability(work_id=selected_id),),
                allowed_work_ids=(selected_id,),
                limit=500,
            ),
        )

        assert [item.work_id for item in plan.items] == [selected_id]
    finally:
        connection.close()


def test_corpus_manifest_and_offline_enumeration_reject_symlink(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    project_root = tmp_path / "repo"
    raw_dir = project_root / "data" / "historical" / "fmp"
    raw_dir.mkdir(parents=True)
    external = tmp_path / "external.json"
    external.write_text("[]", encoding="utf-8")
    link = raw_dir / "RBRK_income_statement_quarterly.json"
    try:
        link.symlink_to(external)
    except OSError as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")
    db_path = migrated_db(project_root / "data" / "runtime.db", target=REVISION)
    connection = _connection(db_path)
    connection.execute(
        "INSERT INTO tracked_companies (ticker,name,list_type) VALUES ('RBRK','Rubrik','portfolio')"
    )
    connection.commit()
    try:
        with pytest.raises(ValueError, match="unsafe corpus entry"):
            refresh_cache._raw_corpus_manifest(raw_dir)
        with pytest.raises(ValueError, match="unsafe corpus entry"):
            refresh_cache._offline_corpus_items(
                connection,
                raw_corpus_dir=raw_dir,
                only_list_type=None,
                explicit_tickers=None,
            )
    finally:
        connection.close()


def test_corpus_enumeration_fails_closed_on_reparse_entry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    raw_dir = tmp_path / "fmp"
    raw_dir.mkdir()
    (raw_dir / "RBRK_income_statement_quarterly.json").write_text("[]", encoding="utf-8")
    checks = iter((False, True))

    def fake_reparse_point(_stat: os.stat_result) -> bool:
        return next(checks)

    monkeypatch.setattr(refresh_cache, "_is_reparse_point", fake_reparse_point)

    with pytest.raises(ValueError, match="unsafe corpus entry"):
        refresh_cache._raw_corpus_manifest(raw_dir)


def test_public_offline_cli_branches_before_legacy_lock_and_subprocess(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = False

    def offline(_args: argparse.Namespace) -> int:
        nonlocal called
        called = True
        return 2

    def forbidden(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("offline public CLI touched legacy lock or subprocess")

    monkeypatch.setattr(refresh_cache, "_run_offline_with_lock", offline)
    monkeypatch.setattr(refresh_cache, "_acquire_lock", forbidden)
    monkeypatch.setattr(refresh_cache.subprocess, "run", forbidden)
    monkeypatch.setattr(refresh_cache.subprocess, "Popen", forbidden)
    monkeypatch.setattr(
        sys,
        "argv",
        ["refresh_cache.py", "run", "--offline-corpus-only", "--db", "unused.db"],
    )

    assert refresh_cache.main() == 2
    assert called


def test_offline_atomic_lock_contention_is_retryable_and_nonzero(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    lock_path = tmp_path / "offline.lock"
    lock_path.write_text("held", encoding="ascii")
    monkeypatch.setattr(refresh_cache, "OFFLINE_LOCK_PATH", lock_path)

    def forbidden_work(_args: argparse.Namespace) -> int:
        raise AssertionError("contended lock ran work")

    monkeypatch.setattr(refresh_cache, "_run_offline_corpus_only", forbidden_work)

    exit_code = refresh_cache._run_offline_with_lock(argparse.Namespace())
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 75
    assert payload["retryable"] is True
    assert payload["network_calls"] == 0


def test_offline_receipt_rejects_negative_counts() -> None:
    with pytest.raises(ValueError):
        refresh_cache.OfflineCorpusRunResult(
            run_id="offline-corpus:123e4567-e89b-42d3-a456-426614174000",
            status=ReceiptStatus.FAILED,
            discovered_file_count=-1,
            selected_count=0,
            admitted_count=0,
            admitted_new_count=0,
            already_applied_count=0,
            eligible_count=0,
            corpus_count=0,
            failed_count=0,
            deferred_count=0,
            excluded_by_tier_count=0,
            skipped_count=0,
            pending_count=0,
            manifest_sha256="a" * 64,
            manifest_before_sha256="a" * 64,
            manifest_after_sha256="a" * 64,
            manifest_unchanged=True,
        )


def _valid_offline_receipt_payload() -> dict[str, object]:
    return {
        "run_id": "offline-corpus:123e4567-e89b-42d3-a456-426614174000",
        "status": ReceiptStatus.DEGRADED_CORPUS,
        "discovered_file_count": 2,
        "selected_count": 1,
        "admitted_count": 1,
        "admitted_new_count": 1,
        "already_applied_count": 0,
        "eligible_count": 1,
        "corpus_count": 1,
        "failed_count": 0,
        "deferred_count": 0,
        "excluded_by_tier_count": 1,
        "skipped_count": 0,
        "pending_count": 1,
        "manifest_sha256": "a" * 64,
        "manifest_before_sha256": "a" * 64,
        "manifest_after_sha256": "a" * 64,
        "manifest_unchanged": True,
        "network_calls": 0,
        "mode": "offline_corpus_only",
    }


@pytest.mark.parametrize(
    ("field_name", "invalid_value"),
    (
        ("run_id", "offline-corpus:not-a-uuid"),
        ("manifest_after_sha256", "A" * 64),
        ("admitted_count", 2),
        ("status", ReceiptStatus.FAILED),
        ("manifest_after_sha256", "b" * 64),
        ("selected_count", 2),
    ),
)
def test_offline_receipt_rejects_invalid_uuid_hash_arithmetic_status_and_manifest(
    field_name: str,
    invalid_value: object,
) -> None:
    payload = _valid_offline_receipt_payload()
    payload[field_name] = invalid_value

    with pytest.raises(ValueError):
        refresh_cache.OfflineCorpusRunResult.model_validate(payload)


def test_raw_corpus_manifest_22k_tiny_files_has_bounded_two_pass_runtime(
    tmp_path: Path,
) -> None:
    raw_dir = tmp_path / "fmp"
    raw_dir.mkdir()
    for index in range(22_000):
        (raw_dir / f"T{index:05d}_profile.json").write_bytes(b"[]")

    started = time.perf_counter()
    before = refresh_cache._raw_corpus_manifest(raw_dir)
    after = refresh_cache._raw_corpus_manifest(raw_dir)
    elapsed = time.perf_counter() - started

    assert before == after
    assert len(before.entries) == 22_000
    assert before.total_bytes == 44_000
    assert elapsed < 300.0


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
