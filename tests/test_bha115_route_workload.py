from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest

from execution import benchmark_performance_workload as workload
from quality.performance import COHORT_REGISTRY, CausalRunEnvelope, RouteCausalCompanion
from quality.performance_state import database_state_sha256


def test_route_workload_is_exactly_twenty_routes_and_not_help_placeholder() -> None:
    cohort = COHORT_REGISTRY["route_cold_warm"]

    assert cohort.route_count == 20
    assert len(cohort.route_names) == 20
    assert len(set(cohort.route_names)) == 20
    assert "--help" not in cohort.declared_command
    assert cohort.route_names == workload.ROUTE_NAMES


def test_route_companion_requires_network_and_fixture_evidence() -> None:
    companion = RouteCausalCompanion(
        route_name="/healthz",
        phase="cold",
        method="GET",
        status_code=200,
        allowed_success_statuses=(200,),
        elapsed_seconds=0.01,
        sql_statements=0,
        connection_count=0,
        response_sha256="a" * 64,
        auth_fixture_identity=workload.ROUTE_FIXTURE_IDENTITY,
        fixture_sha256="b" * 64,
        external_call_hold_seconds=0.0,
        network_disabled=True,
    )
    envelope = CausalRunEnvelope(
        sql_statements=0,
        rows=0,
        elapsed_seconds=0.01,
        peak_rss_bytes=0,
        alembic_revision=None,
        query_plan_sha256=None,
        connection_role="none",
        stage="route-render",
        revision="worktree",
        route_companions=(companion,),
    )

    assert json.loads(envelope.model_dump_json())["route_companions"][0]["network_disabled"] is True
    with pytest.raises(ValueError):
        RouteCausalCompanion.model_validate({**companion.model_dump(), "network_disabled": "yes"})
    with pytest.raises(ValueError):
        RouteCausalCompanion.model_validate(
            {
                **companion.model_dump(),
                "external_trap_sha256": hashlib.sha256(b"").hexdigest(),
            }
        )


def test_route_cli_rejects_caller_supplied_metrics() -> None:
    with pytest.raises(SystemExit):
        workload.main(["--workload", "routes", "--sql-statements", "999"])


def test_route_cli_accepts_only_explicit_repo_root(tmp_path: Path) -> None:
    # The parser's default is the checkout, but the benchmark always resolves
    # its own disposable DB; this guards against a production-path argument.
    parser_factory = cast(Callable[[], argparse.ArgumentParser], getattr(workload, "_parser"))
    args = parser_factory().parse_args(["--workload", "routes", "--repo-root", str(tmp_path)])
    assert args.repo_root == tmp_path


def test_route_state_hash_only_normalizes_explicit_event_timestamps(tmp_path: Path) -> None:
    database = tmp_path / "state.db"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE analyst_notes (id INTEGER PRIMARY KEY, created_at TEXT, body TEXT)"
        )
        connection.execute(
            "CREATE TABLE canonical_axes (id INTEGER PRIMARY KEY, knowledge_at TEXT, body TEXT)"
        )
        connection.execute("INSERT INTO analyst_notes VALUES (1, '2026-01-01', 'same')")
        connection.execute("INSERT INTO canonical_axes VALUES (1, '2026-01-01', 'same')")
        connection.commit()
    first = database_state_sha256(database)
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE analyst_notes SET created_at = '2027-01-01'")
        connection.commit()
    assert database_state_sha256(database) == first
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE canonical_axes SET knowledge_at = '2027-01-01'")
        connection.commit()
    assert database_state_sha256(database) != first


def test_route_cli_integration_emits_forty_real_route_companions() -> None:
    root = Path(__file__).resolve().parents[1]
    checkout_db = root / "data" / "portfolio.db"
    existed = checkout_db.exists()
    completed = subprocess.run(
        [
            sys.executable,
            str(root / "execution" / "sqlite_bootstrap.py"),
            str(root / "execution" / "benchmark_performance_workload.py"),
            "--workload",
            "routes",
            "--repo-root",
            str(root),
        ],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    envelope = CausalRunEnvelope.model_validate_json(completed.stdout.strip())
    assert envelope.stage == "route-render"
    assert envelope.rss_semantics == "process_high_water"
    assert len(envelope.route_companions) == 40
    assert {item.phase for item in envelope.route_companions} == {"cold", "warm"}
    assert {item.route_name for item in envelope.route_companions} == set(workload.ROUTE_NAMES)
    assert all(item.method in {"GET", "POST"} for item in envelope.route_companions)
    assert all(len(item.response_sha256) == 64 for item in envelope.route_companions)
    assert all(item.network_disabled for item in envelope.route_companions)
    assert all(item.external_call_hold_seconds >= 0 for item in envelope.route_companions)
    assert any(item.sql_statements > 0 for item in envelope.route_companions)
    assert any(item.connection_count > 0 for item in envelope.route_companions)
    assert checkout_db.exists() is existed
