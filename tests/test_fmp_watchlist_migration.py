"""Watchlist migration preserves recovery history and unrelated schema guards."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy.exc import IntegrityError

from models.companies import ListType
from pipeline.fmp_recovery import (
    CredentialAvailability,
    EnqueueWorkRequest,
    OutcomeCode,
    PlanRunRequest,
    RecordOutcomesRequest,
    WorkOutcome,
    WorkSpec,
    enqueue_work,
    plan_run,
    record_outcomes,
)


def test_watchlist_upgrade_preserves_leased_work_events_indexes_and_guards(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    database = migrated_db(tmp_path / "runtime.db", target="0039_add_dcf_forecast_series")
    now = datetime(2026, 9, 19)
    portfolio = WorkSpec(
        ticker="PORT",
        coverage_role=ListType.PORTFOLIO,
        endpoint_key="income_statement_quarterly",
        period_key="quarterly:last-5",
        cache_generation_id="generation-1",
        policy_sha256="a" * 64,
    )
    tables = ("fmp_work_backlog", "fmp_work_attempts", "fmp_recovery_events")
    with sqlite3.connect(database) as conn:
        conn.row_factory = sqlite3.Row
        plan = plan_run(
            conn,
            PlanRunRequest(
                run_id="retain-active-lease",
                worker_id="worker-1",
                now=now,
                credentials=CredentialAvailability.AVAILABLE,
                work=(portfolio,),
            ),
        )
        lease_token = plan.items[0].lease_token
        assert lease_token is not None
        record_outcomes(
            conn,
            RecordOutcomesRequest(
                run_id="retain-active-lease",
                now=now,
                expected_work_ids=(plan.items[0].work_id,),
                outcomes=(
                    WorkOutcome(
                        work_id=plan.items[0].work_id,
                        lease_token=lease_token,
                        outcome_code=OutcomeCode.SERVER_ERROR,
                        observed_at=now,
                        http_status=503,
                    ),
                ),
            ),
        )
        plan_run(
            conn,
            PlanRunRequest(
                run_id="retained-open-lease",
                worker_id="worker-1",
                now=now,
                credentials=CredentialAvailability.AVAILABLE,
                work=(portfolio.model_copy(update={"ticker": "LEASE"}),),
            ),
        )
        before = {
            table: [tuple(row) for row in conn.execute(f"SELECT * FROM {table}")]
            for table in tables
        }
        assert all(before.values())
        schema = conn.execute(
            "SELECT sql FROM sqlite_schema WHERE name='fmp_work_backlog'"
        ).fetchone()[0]
        indexes = conn.execute(
            "SELECT name,sql FROM sqlite_schema WHERE tbl_name='fmp_work_backlog' "
            "AND type='index' ORDER BY name"
        ).fetchall()
        indexes_before = [tuple(row) for row in indexes]
    migrated_db(database, target="0040_fmp_watchlist_recovery", upgrade_existing=True)
    with sqlite3.connect(database) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        for table, rows in before.items():
            assert [tuple(row) for row in conn.execute(f"SELECT * FROM {table}")] == rows
        assert [
            tuple(row)
            for row in conn.execute(
                "SELECT name,sql FROM sqlite_schema WHERE tbl_name='fmp_work_backlog' "
                "AND type='index' ORDER BY name"
            )
        ] == indexes_before
        upgraded_schema = conn.execute(
            "SELECT sql FROM sqlite_schema WHERE name='fmp_work_backlog'"
        ).fetchone()[0]
        assert upgraded_schema.replace('"fmp_work_backlog"', "fmp_work_backlog") == (
            schema.replace(
                "('portfolio','evaluation','index_member')",
                "('portfolio','evaluation','watchlist','index_member')",
            )
            .replace("(100,200,300)", "(100,150,200,300)")
            .replace(
                """CHECK(
                (coverage_role = 'evaluation' AND requested = 1
                    AND owner_request_id IS NOT NULL)
                OR coverage_role != 'evaluation'
            )""",
                "CHECK(requested = 0 OR (owner_request_id IS NOT NULL AND length(trim(owner_request_id)) > 0))",
            )
        )
        watch = portfolio.model_copy(
            update={"ticker": "WATCH", "coverage_role": ListType.WATCHLIST}
        )
        evaluation = portfolio.model_copy(
            update={"ticker": "EVAL", "coverage_role": ListType.EVALUATION}
        )
        receipt = enqueue_work(conn, EnqueueWorkRequest(now=now, work=(watch, evaluation)))
        assert receipt.enqueued_count == 2
        assert tuple(
            conn.execute(
                "SELECT coverage_role,priority FROM fmp_work_backlog WHERE ticker='WATCH'"
            ).fetchone()
        ) == ("watchlist", 150)
        for assignment in (
            "coverage_role='none'",
            "requested=1,owner_request_id=NULL",
            "priority=151",
        ):
            with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
                conn.execute(f"UPDATE fmp_work_backlog SET {assignment} WHERE ticker='WATCH'")
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_upgrade_refuses_unattributed_request_and_restores_predecessor(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    database = migrated_db(tmp_path / "runtime.db", target="0039_add_dcf_forecast_series")
    with sqlite3.connect(database) as conn:
        conn.row_factory = sqlite3.Row
        enqueue_work(
            conn,
            EnqueueWorkRequest(
                now=datetime(2026, 9, 19),
                work=(
                    WorkSpec(
                        ticker="PORT",
                        coverage_role=ListType.PORTFOLIO,
                        endpoint_key="profile",
                        period_key="current",
                        cache_generation_id="generation-1",
                        policy_sha256="a" * 64,
                    ),
                ),
            ),
        )
        conn.execute("UPDATE fmp_work_backlog SET requested=1,owner_request_id=NULL")
        before = tuple(conn.execute("SELECT * FROM fmp_work_backlog").fetchone())
        schema = conn.execute(
            "SELECT sql FROM sqlite_schema WHERE name='fmp_work_backlog'"
        ).fetchone()[0]
    with pytest.raises(IntegrityError, match="CHECK constraint failed"):
        migrated_db(database, target="0040_fmp_watchlist_recovery", upgrade_existing=True)
    with sqlite3.connect(database) as conn:
        assert tuple(conn.execute("SELECT * FROM fmp_work_backlog").fetchone()) == before
        assert (
            conn.execute("SELECT sql FROM sqlite_schema WHERE name='fmp_work_backlog'").fetchone()[
                0
            ]
            == schema
        )
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone()[0] == (
            "0039_add_dcf_forecast_series"
        )
        assert (
            conn.execute(
                "SELECT name FROM sqlite_schema WHERE name='fmp_work_backlog_rebuild'"
            ).fetchone()
            is None
        )
