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

import compute.balance_sheet as balance_sheet
import compute.cashflow as cashflow
import compute.income_statement as income_statement
import execution.refresh_cache as refresh_cache
from execution.save_fmp_data import TODAY, per_ticker_jobs
from models.companies import ListType
from pipeline.fmp_doc_index import classify_fmp_filename
from pipeline.fmp_recovery import (
    CircuitConfig,
    CircuitState,
    ContainmentReason,
    CorpusFailureReason,
    CredentialAvailability,
    EnqueueWorkRequest,
    ExecutionMode,
    FmpSnapshotProof,
    OutcomeCode,
    PlanRunRequest,
    ReceiptStatus,
    RecoverableWorkRequest,
    RecoveryAvailability,
    RefreshReceipt,
    WorkOutcome,
    WorkSpec,
    enqueue_work,
    make_work_id,
    plan_run,
    recoverable_work,
)
from provenance import data_backbone_rehearsal, financial_fact_resolution
from provenance.evidence_backfill import ensure_legacy_document_evidence
from provenance.financial_fact_resolution import (
    governed_document_fact_admission,
    rehydrate_document_fact_observations,
)

REVISION = "0008_add_fmp_recovery"
ACTIVE_REVISION = "0015_add_thesis_episode_attention"
NOW = datetime(2026, 8, 12, 9, 0, 0)
CONTENT = "c" * 64


def test_repository_clock_normalizes_pacific_time_to_naive_utc() -> None:
    pacific = datetime(2026, 8, 12, 0, 30, tzinfo=ZoneInfo("America/Los_Angeles"))
    assert refresh_cache._naive_utc(pacific) == datetime(2026, 8, 12, 7, 30)


def _item(
    ticker: str,
    *,
    suffix: str = "income_statement_quarterly",
    endpoint: str = "income-statement",
    period: str = "quarter",
    endpoint_class: str = "statement",
) -> refresh_cache.QueueItem:
    return refresh_cache.QueueItem(
        ticker=ticker,
        list_type=ListType.PORTFOLIO.value,
        endpoint=endpoint,
        period=period,
        suffix=suffix,
        endpoint_class=endpoint_class,
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


def _seed_legacy_fmp_facts_without_observations(
    connection: sqlite3.Connection,
    *,
    project_root: Path,
    raw_path: Path,
    ticker: str = "RBRK",
) -> tuple[int, int]:
    relative_path = str(raw_path.resolve().relative_to(project_root.resolve())).replace("\\", "/")
    content_sha = hashlib.sha256(raw_path.read_bytes()).hexdigest()
    connection.execute(
        "INSERT INTO documents "
        "(ticker,source_type,doc_type,period_end,file_path,sha256,fetched_at,"
        "fetch_status,raw_bytes_size,source_url) "
        "VALUES (?,'fmp','fmp_income_statement','2026-07-31',?,?,?,'ok',?,NULL)",
        (ticker, relative_path, content_sha, NOW.isoformat(), raw_path.stat().st_size),
    )
    document_id = int(connection.execute("SELECT last_insert_rowid()").fetchone()[0])
    ensure_legacy_document_evidence(
        connection,
        repo_root=project_root,
        document_id=document_id,
    )
    connection.commit()
    trigger = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='trigger' "
        "AND name='trg_financial_facts_observation_insert'"
    ).fetchone()
    assert trigger is not None and isinstance(trigger[0], str)
    connection.execute("DROP TRIGGER trg_financial_facts_observation_insert")
    connection.execute(
        "ALTER TABLE fact_observation_revisions RENAME TO legacy_fact_observation_revisions"
    )
    from compute.income_statement import extract_income_statement_facts

    fact_count = extract_income_statement_facts(connection, document_id, project_root)
    connection.execute(
        "ALTER TABLE legacy_fact_observation_revisions RENAME TO fact_observation_revisions"
    )
    connection.execute(str(trigger[0]))
    connection.commit()
    assert fact_count > 0
    assert (
        connection.execute(
            "SELECT COUNT(*) FROM fact_observation_revisions WHERE source_document_id=?",
            (document_id,),
        ).fetchone()[0]
        == 0
    )
    return document_id, fact_count


def _upgrade_legacy_fmp_fixture(
    migrated_db: Callable[..., Path],
    destination: Path,
    *,
    project_root: Path,
    raw_path: Path,
) -> tuple[Path, int, int]:
    seeded: list[tuple[int, int]] = []

    def before_upgrade(path: Path) -> None:
        connection = _connection(path)
        try:
            seeded.append(
                _seed_legacy_fmp_facts_without_observations(
                    connection,
                    project_root=project_root,
                    raw_path=raw_path,
                )
            )
        finally:
            connection.close()

    db_path = migrated_db(
        destination,
        upgrade_from="0007_add_earnings_surprise_observations",
        before_upgrade=before_upgrade,
        target=ACTIVE_REVISION,
    )
    assert len(seeded) == 1
    return db_path, *seeded[0]


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


_CATALOG_SUFFIX_DOC_TYPES = (
    ("income_statement_annual", "fmp_income_statement"),
    ("income_statement_quarterly", "fmp_income_statement"),
    ("balance_sheet_annual", "fmp_balance_sheet"),
    ("balance_sheet_quarterly", "fmp_balance_sheet"),
    ("cash_flow_annual", "fmp_cashflow"),
    ("cash_flow_quarterly", "fmp_cashflow"),
    ("income_growth_annual", "fmp_financial_growth"),
    ("income_growth_quarterly", "fmp_financial_growth"),
    ("balance_growth_annual", "fmp_financial_growth"),
    ("balance_growth_quarterly", "fmp_financial_growth"),
    ("cashflow_growth_annual", "fmp_financial_growth"),
    ("cashflow_growth_quarterly", "fmp_financial_growth"),
    ("financial_growth_annual", "fmp_financial_growth"),
    ("financial_growth_quarterly", "fmp_financial_growth"),
    ("as_reported_income_annual", "fmp_as_reported_income"),
    ("as_reported_income_quarterly", "fmp_as_reported_income"),
    ("as_reported_balance_annual", "fmp_as_reported_balance"),
    ("as_reported_balance_quarterly", "fmp_as_reported_balance"),
    ("as_reported_cashflow_annual", "fmp_as_reported_cashflow"),
    ("as_reported_cashflow_quarterly", "fmp_as_reported_cashflow"),
    ("as_reported_financial_annual", "fmp_as_reported_financial"),
    ("as_reported_financial_quarterly", "fmp_as_reported_financial"),
    ("income_statement_ttm", "fmp_income_statement"),
    ("balance_sheet_ttm", "fmp_balance_sheet"),
    ("cash_flow_ttm", "fmp_cashflow"),
    ("product_segments_annual", "fmp_segment_product"),
    ("product_segments_quarterly", "fmp_segment_product"),
    ("geo_segments_annual", "fmp_segment_geographic"),
    ("geo_segments_quarterly", "fmp_segment_geographic"),
    ("key_metrics_annual", "fmp_key_metrics"),
    ("key_metrics_quarterly", "fmp_key_metrics"),
    ("key_metrics_ttm", "fmp_key_metrics"),
    ("financial_ratios_annual", "fmp_financial_ratios"),
    ("financial_ratios_quarterly", "fmp_financial_ratios"),
    ("financial_ratios_ttm", "fmp_financial_ratios"),
    ("enterprise_values_annual", "fmp_enterprise_values"),
    ("enterprise_values_quarterly", "fmp_enterprise_values"),
    ("financial_scores", "fmp_other"),
    ("owner_earnings_annual", "fmp_owner_earnings"),
    ("financial_reports_dates", "fmp_financial_reports_dates"),
    *((f"form_10k_{year}", "fmp_10k_json") for year in range(TODAY.year - 10, TODAY.year)),
    ("analyst_estimates_annual", "fmp_analyst_estimates"),
    ("analyst_estimates_quarterly", "fmp_analyst_estimates"),
    ("historical_ratings", "fmp_grades"),
    ("price_target_consensus", "fmp_price_target_consensus"),
    ("price_target_summary", "fmp_price_target_consensus"),
    ("grades_summary", "fmp_grades"),
    ("historical_grades", "fmp_grades"),
    ("ratings_snapshot", "fmp_grades"),
    ("profile", "fmp_profile"),
    ("historical_market_cap", "fmp_historical_market_cap"),
    ("shares_float", "fmp_other"),
    ("peers", "fmp_peers"),
    ("company_executives", "fmp_executives"),
    ("historical_employee_count", "fmp_historical_employees"),
    ("dcf_basic", "fmp_dcf"),
    ("dcf_levered", "fmp_dcf_levered"),
    ("price_chart_10y_div_adj", "fmp_historical_price"),
)


def test_full_production_catalog_has_durable_doc_type_classification() -> None:
    catalog_suffixes = tuple(str(job["suffix"]) for job in per_ticker_jobs("RBRK"))
    expected_suffixes = tuple(suffix for suffix, _doc_type in _CATALOG_SUFFIX_DOC_TYPES)

    assert len(catalog_suffixes) == 67
    assert catalog_suffixes == expected_suffixes


@pytest.mark.parametrize(("suffix", "expected_doc_type"), _CATALOG_SUFFIX_DOC_TYPES)
def test_production_backlog_suffixes_map_to_exact_corpus_coordinate_and_doc_type(
    tmp_path: Path,
    suffix: str,
    expected_doc_type: str,
) -> None:
    project_root = tmp_path / "runtime"
    raw_dir = project_root / "data" / "historical" / "fmp"
    raw_dir.mkdir(parents=True)
    item = _item("RBRK", suffix=suffix)
    path = raw_dir / f"RBRK_{suffix}.json"
    path.write_text('[{"date":"2026-07-31"}]', encoding="utf-8")

    assert (
        refresh_cache._corpus_path(
            item,
            raw_corpus_dir=raw_dir,
            project_root=project_root,
        )
        == path
    )
    spec = refresh_cache._work_spec(
        item,
        raw_corpus_dir=raw_dir,
        now=NOW,
        owner_request_id="fixture",
    )
    assert spec.endpoint_key == suffix
    assert spec.corpus_snapshot is not None
    assert spec.corpus_snapshot.content_sha256 == hashlib.sha256(path.read_bytes()).hexdigest()
    assert classify_fmp_filename(path.name) == expected_doc_type


def test_corpus_coordinate_rejects_noncanonical_root_and_path_components(tmp_path: Path) -> None:
    project_root = tmp_path / "runtime"
    raw_dir = project_root / "data" / "historical" / "fmp"
    raw_dir.mkdir(parents=True)

    assert (
        refresh_cache._corpus_path(
            _item("RBRK"),
            raw_corpus_dir=tmp_path / "other-fmp",
            project_root=project_root,
        )
        is None
    )
    assert (
        refresh_cache._corpus_path(
            _item("RBRK", suffix="../income_statement_quarterly"),
            raw_corpus_dir=raw_dir,
            project_root=project_root,
        )
        is None
    )


def test_missing_auth_uses_read_only_corpus_without_dispatch_or_freshness_advance(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    db_path = migrated_db(tmp_path / "runtime.db", target=REVISION)
    raw_dir = tmp_path / "data" / "historical" / "fmp"
    raw_dir.mkdir(parents=True)
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


@pytest.mark.parametrize(
    ("module", "endpoint", "suffix", "field", "held_value", "path_value", "line_item"),
    (
        (
            income_statement,
            "income-statement",
            "income_statement_quarterly",
            "revenue",
            111,
            911,
            "revenue",
        ),
        (
            balance_sheet,
            "balance-sheet-statement",
            "balance_sheet_quarterly",
            "totalAssets",
            222,
            922,
            "total_assets",
        ),
        (
            cashflow,
            "cashflow-statement",
            "cash_flow_quarterly",
            "freeCashFlow",
            333,
            933,
            "free_cash_flow",
        ),
    ),
    ids=("income", "balance", "cashflow"),
)
def test_corpus_statement_facts_use_validated_held_records_not_reopened_path(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    module: object,
    endpoint: str,
    suffix: str,
    field: str,
    held_value: int,
    path_value: int,
    line_item: str,
) -> None:
    """A swap-and-restore pathname reader cannot change held-byte extraction."""
    project_root = tmp_path / "repo"
    raw_dir = project_root / "data" / "historical" / "fmp"
    raw_dir.mkdir(parents=True)
    raw_path = raw_dir / f"RBRK_{suffix}.json"
    held_record: dict[str, object] = {
        "date": "2026-07-31",
        "symbol": "RBRK",
        "reportedCurrency": "USD",
        "period": "Q2",
        field: held_value,
    }
    raw_path.write_text(json.dumps([held_record]), encoding="utf-8")
    original = raw_path.read_bytes()
    path_reader_calls = 0

    def swapped_path_reader(path: Path) -> list[dict[str, object]]:
        nonlocal path_reader_calls
        path_reader_calls += 1
        swapped = {**held_record, field: path_value}
        # Simulate the vulnerable second pathname read observing replaced bytes,
        # followed by an attacker restoring the exact governed corpus artifact.
        assert path == raw_path
        return [swapped]

    monkeypatch.setattr(module, "read_records_json", swapped_path_reader)
    db_path = migrated_db(project_root / "data" / "runtime.db", target=ACTIVE_REVISION)
    connection = _connection(db_path)
    try:
        result = refresh_cache.run_recovery_batch(
            connection,
            items=(
                _item(
                    "RBRK",
                    endpoint=endpoint,
                    suffix=suffix,
                    endpoint_class="statement",
                ),
            ),
            credentials=CredentialAvailability.MISSING,
            raw_corpus_dir=raw_dir,
            project_root=project_root,
            now=NOW,
            run_id=f"held-records-{suffix}",
            dispatch=_unexpected_dispatch,
            provider_call_budget=0,
        )

        assert result.corpus_count == 1
        assert result.failed_count == 0
        assert path_reader_calls == 0
        fact = connection.execute(
            "SELECT value FROM financial_facts WHERE ticker='RBRK' AND line_item=?",
            (line_item,),
        ).fetchone()
        assert fact is not None
        assert int(fact["value"]) == held_value
        assert raw_path.read_bytes() == original
    finally:
        connection.close()


def test_real_corpus_rehydrates_legacy_facts_missing_canonical_observations(
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
    db_path, document_id, fact_count = _upgrade_legacy_fmp_fixture(
        migrated_db,
        project_root / "data" / "runtime.db",
        project_root=project_root,
        raw_path=raw_path,
    )
    connection = _connection(db_path)
    try:
        first = refresh_cache.run_recovery_batch(
            connection,
            items=(_item("RBRK"),),
            credentials=CredentialAvailability.MISSING,
            raw_corpus_dir=raw_dir,
            project_root=project_root,
            now=NOW,
            run_id="legacy-facts-first",
            dispatch=_unexpected_dispatch,
            provider_call_budget=0,
        )
        assert first.corpus_count == 1
        assert first.pending_count == 1
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM fact_observation_revisions WHERE source_document_id=?",
                (document_id,),
            ).fetchone()[0]
            == fact_count
        )
        assert (
            governed_document_fact_admission(
                connection,
                document_id=document_id,
                ticker="RBRK",
                content_sha256=hashlib.sha256(raw_path.read_bytes()).hexdigest(),
                inserted_count=0,
            ).status
            == "idempotent_replay"
        )

        second = refresh_cache.run_recovery_batch(
            connection,
            items=(_item("RBRK"),),
            credentials=CredentialAvailability.MISSING,
            raw_corpus_dir=raw_dir,
            project_root=project_root,
            now=NOW + timedelta(minutes=6),
            run_id="legacy-facts-second",
            dispatch=_unexpected_dispatch,
            provider_call_budget=0,
        )
        assert second.corpus_count == 1
        assert second.pending_count == 1
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM fact_observation_revisions WHERE source_document_id=?",
                (document_id,),
            ).fetchone()[0]
            == fact_count
        )
    finally:
        connection.close()


def test_legacy_fact_rehydration_rejects_content_mismatch_without_capture(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    project_root = tmp_path / "repo"
    raw_dir = project_root / "data" / "historical" / "fmp"
    raw_dir.mkdir(parents=True)
    raw_path = raw_dir / "RBRK_income_statement_quarterly.json"
    raw_path.write_text(
        json.dumps([{"date": "2026-07-31", "symbol": "RBRK", "period": "Q2", "revenue": 1}]),
        encoding="utf-8",
    )
    db_path, document_id, _ = _upgrade_legacy_fmp_fixture(
        migrated_db,
        project_root / "data" / "runtime.db",
        project_root=project_root,
        raw_path=raw_path,
    )
    connection = _connection(db_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        with pytest.raises(ValueError, match="document/hash"):
            rehydrate_document_fact_observations(
                connection,
                document_id=document_id,
                ticker="RBRK",
                content_sha256="0" * 64,
                inserted_count=0,
                recorded_at=NOW,
            )
        connection.rollback()
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM fact_observation_revisions WHERE source_document_id=?",
                (document_id,),
            ).fetchone()[0]
            == 0
        )
    finally:
        connection.close()


def test_legacy_fact_rehydration_rejects_cross_ticker_fact_without_capture(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    project_root = tmp_path / "repo"
    raw_dir = project_root / "data" / "historical" / "fmp"
    raw_dir.mkdir(parents=True)
    raw_path = raw_dir / "RBRK_income_statement_quarterly.json"
    raw_path.write_text(
        json.dumps([{"date": "2026-07-31", "symbol": "RBRK", "period": "Q2", "revenue": 1}]),
        encoding="utf-8",
    )
    db_path, document_id, _ = _upgrade_legacy_fmp_fixture(
        migrated_db,
        project_root / "data" / "runtime.db",
        project_root=project_root,
        raw_path=raw_path,
    )
    connection = _connection(db_path)
    try:
        connection.execute(
            "UPDATE financial_facts SET ticker='WIX' WHERE source_doc_id=?",
            (document_id,),
        )
        connection.commit()
        link_count_before = int(
            connection.execute(
                "SELECT COUNT(*) FROM fact_observation_revisions WHERE source_document_id=?",
                (document_id,),
            ).fetchone()[0]
        )
        connection.execute("BEGIN IMMEDIATE")
        with pytest.raises(ValueError, match="different ticker"):
            rehydrate_document_fact_observations(
                connection,
                document_id=document_id,
                ticker="RBRK",
                content_sha256=hashlib.sha256(raw_path.read_bytes()).hexdigest(),
                inserted_count=0,
                recorded_at=NOW,
            )
        connection.rollback()
        assert (
            int(
                connection.execute(
                    "SELECT COUNT(*) FROM fact_observation_revisions WHERE source_document_id=?",
                    (document_id,),
                ).fetchone()[0]
            )
            == link_count_before
        )
    finally:
        connection.close()


def test_legacy_fact_rehydration_rolls_back_partial_capture(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
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
                    "period": "Q2",
                    "revenue": 310000000,
                    "netIncome": -42000000,
                }
            ]
        ),
        encoding="utf-8",
    )
    raw_before = (hashlib.sha256(raw_path.read_bytes()).hexdigest(), raw_path.stat().st_mtime_ns)
    db_path, document_id, fact_count = _upgrade_legacy_fmp_fixture(
        migrated_db,
        project_root / "data" / "runtime.db",
        project_root=project_root,
        raw_path=raw_path,
    )
    connection = _connection(db_path)
    try:
        assert fact_count == 2
        real_capture = financial_fact_resolution.capture_fact_row_observation
        calls = 0

        def fail_second_capture(
            conn: sqlite3.Connection,
            *,
            fact_table: financial_fact_resolution.FactTable,
            fact_row_id: int,
            recorded_at: datetime,
        ) -> bool:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("forced partial capture failure")
            return real_capture(
                conn,
                fact_table=fact_table,
                fact_row_id=fact_row_id,
                recorded_at=recorded_at,
            )

        monkeypatch.setattr(
            financial_fact_resolution,
            "capture_fact_row_observation",
            fail_second_capture,
        )
        result = refresh_cache.run_recovery_batch(
            connection,
            items=(_item("RBRK"),),
            credentials=CredentialAvailability.MISSING,
            raw_corpus_dir=raw_dir,
            project_root=project_root,
            now=NOW,
            run_id="legacy-facts-partial-failure",
            dispatch=_unexpected_dispatch,
            provider_call_budget=0,
        )
        assert result.failed_count == 1
        assert len(result.corpus_failure_diagnostics) == 1
        assert (
            result.corpus_failure_diagnostics[0].reason
            is refresh_cache.CorpusFailureReason.FACT_ADMISSION_FAILED
        )
        assert result.corpus_failure_diagnostics[0].disposition == "pending_retry"
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM fact_observation_revisions WHERE source_document_id=?",
                (document_id,),
            ).fetchone()[0]
            == 0
        )
        assert (
            connection.execute("SELECT COUNT(*) FROM fact_resolution_outcomes").fetchone()[0] == 0
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM financial_facts WHERE source_doc_id=?", (document_id,)
            ).fetchone()[0]
            == fact_count
        )
        assert (
            hashlib.sha256(raw_path.read_bytes()).hexdigest(),
            raw_path.stat().st_mtime_ns,
        ) == raw_before
    finally:
        connection.close()


def test_legacy_fact_rehydration_reports_evidence_drift_truthfully(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
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
                    "period": "Q2",
                    "revenue": 310000000,
                }
            ]
        ),
        encoding="utf-8",
    )
    db_path, document_id, _ = _upgrade_legacy_fmp_fixture(
        migrated_db,
        project_root / "data" / "runtime.db",
        project_root=project_root,
        raw_path=raw_path,
    )
    connection = _connection(db_path)

    real_reread = refresh_cache._HeldCorpusFile.reread
    rereads = 0

    def fail_reread(
        self: refresh_cache._HeldCorpusFile,
    ) -> tuple[bytes, os.stat_result]:
        nonlocal rereads
        rereads += 1
        if rereads == 3:
            raise RuntimeError("forced held evidence drift")
        return real_reread(self)

    monkeypatch.setattr(refresh_cache._HeldCorpusFile, "reread", fail_reread)
    try:
        result = refresh_cache.run_recovery_batch(
            connection,
            items=(_item("RBRK"),),
            credentials=CredentialAvailability.MISSING,
            raw_corpus_dir=raw_dir,
            project_root=project_root,
            now=NOW,
            run_id="legacy-facts-evidence-drift",
            dispatch=_unexpected_dispatch,
            provider_call_budget=0,
        )
        assert result.failed_count == 1
        assert result.corpus_failure_diagnostics[0].reason is CorpusFailureReason.EVIDENCE_CHANGED
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM fact_observation_revisions WHERE source_document_id=?",
                (document_id,),
            ).fetchone()[0]
            == 0
        )
    finally:
        connection.close()


def test_fresh_fact_extraction_rolls_back_on_post_extract_evidence_drift(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
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
                    "revenue": 310000000,
                }
            ]
        ),
        encoding="utf-8",
    )
    db_path = migrated_db(project_root / "data" / "runtime.db", target=ACTIVE_REVISION)
    connection = _connection(db_path)
    real_reread = refresh_cache._HeldCorpusFile.reread
    rereads = 0

    def fail_post_extract_reread(
        self: refresh_cache._HeldCorpusFile,
    ) -> tuple[bytes, os.stat_result]:
        nonlocal rereads
        rereads += 1
        if rereads == 3:
            raise RuntimeError("forced post-extract evidence drift")
        return real_reread(self)

    monkeypatch.setattr(refresh_cache._HeldCorpusFile, "reread", fail_post_extract_reread)
    try:
        result = refresh_cache.run_recovery_batch(
            connection,
            items=(_item("RBRK"),),
            credentials=CredentialAvailability.MISSING,
            raw_corpus_dir=raw_dir,
            project_root=project_root,
            now=NOW,
            run_id="fresh-facts-evidence-drift",
            dispatch=_unexpected_dispatch,
            provider_call_budget=0,
        )

        assert result.failed_count == 1
        assert result.corpus_failure_diagnostics[0].reason is CorpusFailureReason.EVIDENCE_CHANGED
        document = connection.execute(
            "SELECT id FROM documents WHERE ticker='RBRK' AND source_type='fmp'"
        ).fetchone()
        assert document is not None
        document_id = int(document["id"])
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM financial_facts WHERE source_doc_id=?", (document_id,)
            ).fetchone()[0]
            == 0
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM fact_observation_revisions WHERE source_document_id=?",
                (document_id,),
            ).fetchone()[0]
            == 0
        )
    finally:
        connection.close()


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


def test_operator_budget_contains_rate_limited_provider_before_configured_threshold(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    db_path = migrated_db(tmp_path / "runtime.db", target=REVISION)
    connection = _connection(db_path)
    calls: list[str] = []

    def rate_limited(
        _conn: sqlite3.Connection,
        item: refresh_cache.QueueItem,
        planned: refresh_cache.PlannedWork,
    ) -> WorkOutcome:
        calls.append(item.ticker)
        assert planned.lease_token is not None
        return WorkOutcome(
            work_id=planned.work_id,
            lease_token=planned.lease_token,
            outcome_code=OutcomeCode.RATE_LIMITED,
            observed_at=NOW,
            http_status=429,
        )

    try:
        result = refresh_cache.run_recovery_batch(
            connection,
            items=tuple(_item(ticker) for ticker in ("RBRK", "WIX", "META")),
            credentials=CredentialAvailability.AVAILABLE,
            raw_corpus_dir=tmp_path / "missing-corpus",
            now=NOW,
            run_id="bounded-rate-limit",
            dispatch=rate_limited,
            provider_call_budget=2,
            circuit_config=CircuitConfig(rate_limit_threshold=3),
            contain_on_budget_exhaustion=True,
        )

        assert len(calls) == 2
        assert result.dispatch_count == 2
        circuit = connection.execute(
            "SELECT state,consecutive_rate_limits,last_reason_code "
            "FROM provider_circuit_state WHERE provider='fmp'"
        ).fetchone()
        assert circuit is not None
        assert tuple(circuit) == (
            CircuitState.OPEN.value,
            2,
            ContainmentReason.OPERATOR_CALL_BUDGET_EXHAUSTED_AFTER_RATE_LIMIT.value,
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM fmp_recovery_events WHERE event_type='circuit_contained'"
            ).fetchone()[0]
            == 1
        )
    finally:
        connection.close()


def test_operator_budget_containment_is_atomic_with_final_429_across_restart(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = migrated_db(tmp_path / "runtime.db", target=REVISION)
    connection = _connection(db_path)
    calls = 0

    def rate_limited(
        _connection: sqlite3.Connection,
        _item_value: refresh_cache.QueueItem,
        planned: refresh_cache.PlannedWork,
    ) -> WorkOutcome:
        nonlocal calls
        calls += 1
        assert planned.lease_token is not None
        return WorkOutcome(
            work_id=planned.work_id,
            lease_token=planned.lease_token,
            outcome_code=OutcomeCode.RATE_LIMITED,
            observed_at=NOW,
            http_status=429,
        )

    original_record_outcomes = refresh_cache.record_outcomes

    def crash_after_atomic_receipt(
        conn: sqlite3.Connection,
        request: refresh_cache.RecordOutcomesRequest,
    ) -> RefreshReceipt:
        receipt = original_record_outcomes(conn, request)
        if request.containment_reason is not None:
            raise RuntimeError("simulated crash after atomic final receipt")
        return receipt

    monkeypatch.setattr(refresh_cache, "record_outcomes", crash_after_atomic_receipt)
    try:
        with pytest.raises(RuntimeError, match="simulated crash after atomic final receipt"):
            refresh_cache.run_recovery_batch(
                connection,
                items=tuple(_item(ticker) for ticker in ("RBRK", "WIX", "META")),
                credentials=CredentialAvailability.AVAILABLE,
                raw_corpus_dir=tmp_path / "missing-corpus",
                now=NOW,
                run_id="bounded-rate-limit-crash",
                dispatch=rate_limited,
                provider_call_budget=2,
                circuit_config=CircuitConfig(rate_limit_threshold=3),
                contain_on_budget_exhaustion=True,
            )
        assert calls == 2
        circuit = connection.execute(
            "SELECT state,last_reason_code FROM provider_circuit_state WHERE provider='fmp'"
        ).fetchone()
        assert circuit is not None
        assert tuple(circuit) == (
            CircuitState.OPEN.value,
            ContainmentReason.OPERATOR_CALL_BUDGET_EXHAUSTED_AFTER_RATE_LIMIT.value,
        )
    finally:
        connection.close()

    monkeypatch.setattr(refresh_cache, "record_outcomes", original_record_outcomes)
    restarted = _connection(db_path)
    try:
        result = refresh_cache.run_recovery_batch(
            restarted,
            items=tuple(_item(ticker) for ticker in ("RBRK", "WIX", "META")),
            credentials=CredentialAvailability.AVAILABLE,
            raw_corpus_dir=tmp_path / "missing-corpus",
            now=NOW + timedelta(minutes=1),
            run_id="bounded-rate-limit-restart",
            dispatch=rate_limited,
            provider_call_budget=2,
            circuit_config=CircuitConfig(rate_limit_threshold=3),
            contain_on_budget_exhaustion=True,
        )
        assert calls == 2
        assert result.dispatch_count == 0
    finally:
        restarted.close()


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


def test_generic_corpus_endpoint_is_durably_admitted_without_facts(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    project_root = tmp_path / "repo"
    raw_dir = project_root / "data" / "historical" / "fmp"
    raw_dir.mkdir(parents=True)
    raw_path = raw_dir / "RBRK_analyst_estimates_annual.json"
    raw_path.write_text(
        json.dumps([{"date": "2027-01-31", "symbol": "RBRK", "revenueAvg": 1_300_000_000}]),
        encoding="utf-8",
    )
    db_path = migrated_db(project_root / "data" / "runtime.db", target=REVISION)
    connection = _connection(db_path)
    item = _item(
        "RBRK",
        endpoint="analyst-estimates",
        period="annual",
        suffix="analyst_estimates_annual",
        endpoint_class="time_sensitive",
    )
    try:
        first = refresh_cache.run_recovery_batch(
            connection,
            items=(item,),
            credentials=CredentialAvailability.MISSING,
            raw_corpus_dir=raw_dir,
            project_root=project_root,
            now=NOW,
            run_id="generic-corpus-first",
            dispatch=_unexpected_dispatch,
            provider_call_budget=0,
        )
        counts = {
            table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in ("documents", "evidence_document_versions", "financial_facts")
        }
        second = refresh_cache.run_recovery_batch(
            connection,
            items=(item,),
            credentials=CredentialAvailability.MISSING,
            raw_corpus_dir=raw_dir,
            project_root=project_root,
            now=NOW + timedelta(minutes=1),
            run_id="generic-corpus-replay",
            dispatch=_unexpected_dispatch,
            provider_call_budget=0,
        )

        assert first.status is ReceiptStatus.DEGRADED_CORPUS
        assert first.corpus_count == 1
        assert second.status is ReceiptStatus.DEGRADED_CORPUS
        assert second.corpus_count == 1
        assert counts == {"documents": 1, "evidence_document_versions": 1, "financial_facts": 0}
        assert {
            table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in counts
        } == counts
    finally:
        connection.close()


@pytest.mark.parametrize(
    ("endpoint", "period", "suffix", "raw_payload"),
    (
        ("profile", "", "profile", "[1]"),
        ("profile", "", "profile", "[{}]"),
        ("profile", "", "profile", '[{"symbol":"RBRK"},1]'),
        ("profile", "", "profile", "1"),
        (
            "income-statement",
            "quarter",
            "income_statement_quarterly",
            '[{"date":"2026-07-31","symbol":"WIX","period":"Q2"}]',
        ),
        ("profile", "", "profile", "not-json"),
    ),
    ids=("non-object", "empty-object", "mixed", "scalar", "wrong-wix-ticker", "not-json"),
)
def test_corpus_contract_error_precedes_every_canonical_write_and_dumps_diagnostic(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    endpoint: str,
    period: str,
    suffix: str,
    raw_payload: str,
) -> None:
    project_root = tmp_path / "repo"
    raw_dir = project_root / "data" / "historical" / "fmp"
    raw_dir.mkdir(parents=True)
    (raw_dir / f"RBRK_{suffix}.json").write_text(raw_payload, encoding="utf-8")
    diagnostic_dir = project_root / ".tmp" / "fmp_validation_failures"
    monkeypatch.setattr(refresh_cache, "FMP_VALIDATION_DUMP_DIR", diagnostic_dir)
    db_path = migrated_db(project_root / "data" / "runtime.db", target=REVISION)
    connection = _connection(db_path)
    item = _item(
        "RBRK",
        endpoint=endpoint,
        period=period,
        suffix=suffix,
        endpoint_class="statement" if "statement" in endpoint else "time_sensitive",
    )
    try:
        result = refresh_cache.run_recovery_batch(
            connection,
            items=(item,),
            credentials=CredentialAvailability.MISSING,
            raw_corpus_dir=raw_dir,
            project_root=project_root,
            now=NOW,
            run_id=f"invalid-corpus-{suffix}",
            dispatch=_unexpected_dispatch,
            provider_call_budget=0,
        )

        assert result.status is ReceiptStatus.FAILED
        assert result.corpus_count == 0
        assert result.failed_count == 1
        for table in ("documents", "evidence_document_versions", "financial_facts"):
            assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
        assert (
            connection.execute("SELECT outcome_code FROM fmp_work_attempts").fetchone()[0]
            == OutcomeCode.CLIENT_CONTRACT_ERROR.value
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM fmp_endpoint_status WHERE ticker='RBRK' AND status='ok'"
            ).fetchone()[0]
            == 0
        )
        diagnostics = list(diagnostic_dir.glob("*.json"))
        assert len(diagnostics) == 1
        diagnostic = json.loads(diagnostics[0].read_text(encoding="utf-8"))
        assert diagnostic["transport"] == "corpus"
        assert diagnostic["ticker"] == "RBRK"
        assert diagnostic["validation_errors"]
        assert diagnostic["raw_response_text"] == raw_payload
    finally:
        connection.close()


def test_corpus_admission_allows_cross_issuer_rows_for_stock_peers(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    project_root = tmp_path / "repo"
    raw_dir = project_root / "data" / "historical" / "fmp"
    raw_dir.mkdir(parents=True)
    (raw_dir / "RBRK_peers.json").write_text(
        json.dumps([{"symbol": "WIX"}, {"ticker": "META"}]),
        encoding="utf-8",
    )
    db_path = migrated_db(project_root / "data" / "runtime.db", target=REVISION)
    connection = _connection(db_path)
    item = _item(
        "RBRK",
        endpoint="stock-peers",
        period="",
        suffix="peers",
        endpoint_class="reference",
    )
    try:
        result = refresh_cache.run_recovery_batch(
            connection,
            items=(item,),
            credentials=CredentialAvailability.MISSING,
            raw_corpus_dir=raw_dir,
            project_root=project_root,
            now=NOW,
            run_id="cross-issuer-peers",
            dispatch=_unexpected_dispatch,
            provider_call_budget=0,
        )

        assert result.status is ReceiptStatus.DEGRADED_CORPUS
        assert result.corpus_count == 1
        assert connection.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM financial_facts").fetchone()[0] == 0
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
    db_path = migrated_db(project_root / "data" / "runtime.db", target=ACTIVE_REVISION)
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
    db_path = migrated_db(project_root / "data" / "runtime.db", target=ACTIVE_REVISION)
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
    raw_dir = tmp_path / "data" / "historical" / "fmp"
    raw_dir.mkdir(parents=True)
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


def test_raw_corpus_manifest_matches_rehearsal_path_order_and_hash(tmp_path: Path) -> None:
    raw_dir = tmp_path / "fmp"
    raw_dir.mkdir()
    (raw_dir / "A_balance_sheet_annual.json").write_bytes(b"[]")
    (raw_dir / "AAC_balance_sheet_annual.json").write_bytes(b"{}")

    raw_manifest = refresh_cache._raw_corpus_manifest(raw_dir)
    rehearsal_manifest = data_backbone_rehearsal.build_corpus_manifest(raw_dir)

    expected_paths = [
        "AAC_balance_sheet_annual.json",
        "A_balance_sheet_annual.json",
    ]
    assert [entry.relative_path for entry in raw_manifest.entries] == expected_paths
    assert [entry.relative_path for entry in rehearsal_manifest.entries] == expected_paths
    assert raw_manifest.manifest_sha256 == rehearsal_manifest.manifest_sha256


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
