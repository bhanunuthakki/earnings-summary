"""Run one real BHA-115 integrity or migration workload.

The command is intentionally self-contained: metrics are measured here, never
accepted from a caller-provided sidecar.  One JSON causal envelope is written
to stdout; diagnostic logs go to stderr.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import logging
import os
import platform
import resource
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Literal, cast

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(PROJECT_ROOT / "execution"))

from quality.performance import (  # noqa: E402
    EXTERNAL_TRAP_BOUNDARIES,
    EXTERNAL_TRAP_PROOF_VERSION,
    CausalRunEnvelope,
    RouteCausalCompanion,
    external_trap_proof_sha256,
)

ROUTE_FIXTURE_IDENTITY = "localhost-unauthenticated-fixture-v1"
ROUTE_NAMES: tuple[str, ...] = (
    "/healthz",
    "/api/capture/text",
    "/api/onmymind/<int:note_id>/reply",
    "/api/onmymind/<int:note_id>/answer",
    "/api/research/task/<int:task_id>/run",
    "/api/research/task/<int:task_id>/status",
    "/api/research/task/<int:task_id>/reject",
    "/api/research/proposal/<int:proposal_id>/<verb>",
    "/api/research/proposals/<int:proposal_id>",
    "/api/reconcile/<kind>/<int:item_id>/<verdict>",
    "/api/reconcile/falsifier/<int:decision_id>",
    "/api/onmymind/<int:note_id>/<verb>",
    "/api/tenets",
    "/api/tenets/<int:tenet_id>/<action>",
    "/api/profile/fact/<int:fact_id>/affirm",
    "/api/profile/fact/<int:fact_id>/reject",
    "/api/profile/fact/<int:fact_id>/reaffirm",
    "/api/profile/fact/<int:fact_id>/retire",
    "/api/profile/fact/<int:fact_id>/update",
    "/api/tenets/distill",
)

ROUTE_REQUESTS: tuple[tuple[str, str], ...] = (
    ("GET", "/healthz"),
    ("POST", "/api/capture/text"),
    ("POST", "/api/onmymind/1/reply"),
    ("GET", "/api/onmymind/1/answer"),
    ("POST", "/api/research/task/1/run"),
    ("GET", "/api/research/task/1/status"),
    ("POST", "/api/research/task/1/reject"),
    ("POST", "/api/research/proposal/1/approve"),
    ("GET", "/api/research/proposals/1"),
    ("POST", "/api/reconcile/position/1/approve"),
    ("POST", "/api/reconcile/falsifier/1"),
    ("POST", "/api/onmymind/1/save"),
    ("POST", "/api/tenets"),
    ("POST", "/api/tenets/1/archive"),
    ("POST", "/api/profile/fact/1/affirm"),
    ("POST", "/api/profile/fact/1/reject"),
    ("POST", "/api/profile/fact/1/reaffirm"),
    ("POST", "/api/profile/fact/1/retire"),
    ("POST", "/api/profile/fact/1/update"),
    ("POST", "/api/tenets/distill"),
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workload", choices=("integrity", "migrations", "routes"), required=True)
    parser.add_argument("--repo-root", type=Path, default=PROJECT_ROOT)
    return parser


def _rss_bytes() -> int:
    value = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    return value if platform.system() == "Darwin" else value * 1024


def _event(name: str, **fields: object) -> None:
    print(json.dumps({"event": name, **fields}, sort_keys=True), file=sys.stderr, flush=True)


def _schema_object_count(database: Path) -> int:
    with sqlite3.connect(database) as connection:
        return int(
            connection.execute(
                "SELECT COUNT(*) FROM sqlite_master "
                "WHERE type IN ('table', 'index', 'trigger', 'view')"
            ).fetchone()[0]
        )


def _migrate(root: Path, database: Path) -> tuple[int, str, float, int]:
    from alembic.config import Config
    from sqlalchemy import event
    from sqlalchemy.engine import Engine

    from alembic import command

    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database}")
    statement_count = 0

    def count_statement(*_: object) -> None:
        nonlocal statement_count
        statement_count += 1

    event.listen(Engine, "before_cursor_execute", count_statement)
    logging.getLogger("alembic").setLevel(logging.CRITICAL)
    started = time.perf_counter()
    try:
        with contextlib.redirect_stderr(io.StringIO()):
            command.upgrade(config, "head")
    finally:
        event.remove(Engine, "before_cursor_execute", count_statement)
    with sqlite3.connect(database) as connection:
        revision = str(connection.execute("SELECT version_num FROM alembic_version").fetchone()[0])
    return statement_count, revision, max(0.000001, time.perf_counter() - started), 1


def _integrity(root: Path) -> tuple[int, str | None, int, float, int, int]:
    from provenance.integrity_audit import AuditOptions, audit_connection
    from sqlite_runtime import SQLiteConnectionRole, connect_sqlite

    with tempfile.TemporaryDirectory(prefix="bha115-integrity-") as temp_name:
        database = Path(temp_name) / "portfolio.db"
        _, migrated_revision, migration_elapsed, alembic_invocations = _migrate(root, database)
        statement_counter = [0]
        connection = connect_sqlite(database, role=SQLiteConnectionRole.READ_ONLY)
        connection.set_trace_callback(
            lambda _: statement_counter.__setitem__(0, statement_counter[0] + 1)
        )
        try:
            summary = audit_connection(
                connection,
                AuditOptions(sample_limit=20, deep_sqlite_checks=True, verify_bytes=False),
            )
            rows = 0
            for table in summary.tables_present:
                quoted = '"' + table.replace('"', '""') + '"'
                rows += int(connection.execute(f"SELECT COUNT(*) FROM {quoted}").fetchone()[0])
            schema_object_count = _schema_object_count(database)
        finally:
            connection.close()
    return (
        rows,
        migrated_revision,
        statement_counter[0],
        migration_elapsed,
        alembic_invocations,
        schema_object_count,
    )


def _migrations(root: Path) -> tuple[int, str, int, float, int, int]:
    # The database is always created beneath a disposable directory.  Never
    # route this benchmark through the checkout's data/portfolio.db default.
    with tempfile.TemporaryDirectory(prefix="bha115-migrations-") as temp_name:
        database = Path(temp_name) / "portfolio.db"
        statement_count, revision, migration_elapsed, alembic_invocations = _migrate(root, database)
        with sqlite3.connect(database) as connection:
            observed_revision = str(
                connection.execute("SELECT version_num FROM alembic_version").fetchone()[0]
            )
        if observed_revision != revision:
            raise RuntimeError(
                f"migration revision changed while measuring schema: {revision!r} -> "
                f"{observed_revision!r}"
            )
        return (
            0,
            revision,
            statement_count,
            migration_elapsed,
            alembic_invocations,
            _schema_object_count(database),
        )


def _database_state_sha256(database: Path) -> str:
    """Hash logical SQLite state, normalizing request-time timestamps.

    A byte hash proves the copied fixture is exact.  This second digest proves
    that cold and warm requests reached the same logical post-request state
    without pretending that ``updated_at`` values are deterministic clocks.
    """

    def normalize(value: object, key: str = "") -> object:
        if isinstance(value, dict):
            mapping = cast("dict[object, object]", value)
            return {str(k): normalize(v, str(k)) for k, v in mapping.items()}
        if isinstance(value, list):
            items = cast("list[object]", value)
            return [normalize(item, key) for item in items]
        if key.endswith("_at") or key in {"as_of", "created", "updated"}:
            return "<timestamp>"
        return value

    with sqlite3.connect(database) as connection:
        objects = connection.execute(
            "SELECT name, type, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' "
            "ORDER BY type, name"
        ).fetchall()
        state: list[object] = []
        for name, kind, sql in objects:
            if kind != "table":
                state.append((kind, str(name), str(sql or "")))
                continue
            columns = [str(row[1]) for row in connection.execute(f'PRAGMA table_info("{name}")')]
            order_by = ", ".join(f'"{column}"' for column in columns)
            rows = connection.execute(f'SELECT * FROM "{name}" ORDER BY {order_by}').fetchall()
            normalized_rows: list[list[object]] = []
            for row in rows:
                values: list[object] = []
                for column, value in zip(columns, row, strict=True):
                    if column.endswith("_json") and isinstance(value, str):
                        with contextlib.suppress(TypeError, ValueError):
                            value = normalize(json.loads(value))
                    values.append(normalize(value, column))
                normalized_rows.append(values)
            state.append((kind, str(name), tuple(columns), normalized_rows))
    return hashlib.sha256(json.dumps(state, sort_keys=True, default=str).encode()).hexdigest()


def _seed_route_fixture(root: Path, database: Path, fixture_repo: Path) -> dict[str, int]:
    """Create the canonical route corpus once using the real store APIs."""
    _migrate(root, database)
    from ask.exchange_store import (
        SessionContextV1,
        begin_exchange,
        hash_request_payload,
        put_session_context,
    )
    from ask.store import ensure_session
    from owner_profile.store import append_fact
    from research.proposal_approval import create_ask_proposal
    from research.proposals import create_proposal, create_task
    from synthesis.tenets import record_tenet
    from user_state.notes import create_note

    note = create_note(
        body="BHA-115 deterministic note",
        kind="musing",
        ticker="NU",
        source="capture",
        source_ref="seed:bha115-route",
        context={"channel": "benchmark"},
        db_path=database,
    )
    task_id = create_task(
        note_id=note.id, claim="BHA-115 deterministic claim", ticker="NU", db_path=database
    )
    proposal_id = create_proposal(
        task_id=task_id,
        kind="memo",
        ticker="NU",
        title="BHA-115 deterministic proposal",
        body_md="Deterministic benchmark proposal.",
        budget_tier="cheap",
        db_path=database,
    )
    ask_session = ensure_session("bha115-fixture-session", db_path=database)
    put_session_context(
        ask_session.id,
        SessionContextV1(company_ticker="NU"),
        db_path=database,
    )
    begin_exchange(
        session_id=ask_session.id,
        request_id="bha115-fixture-exchange",
        payload_sha256=hash_request_payload({"query": "fixture"}),
        user_text="fixture",
        expected_revision=0,
        db_path=database,
    )
    fixture_holdings = fixture_repo / "micro_thesis" / "holdings" / "NU.json"
    fixture_holdings.parent.mkdir(parents=True, exist_ok=True)
    fixture_holdings.write_text(
        json.dumps(
            {
                "ticker": "NU",
                "name": "Nu Holdings",
                "thesis": "Old thesis",
                "tier_1_kpis": [{"name": "NIM", "source": "earnings release"}],
                "tier_2_kpis": [],
                "tier_3_kpis": [],
                "break_rules": [],
                "business_model_rules": [],
                "break_rules_soft": [],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    governed = create_ask_proposal(
        {
            "target_file": "micro_thesis/holdings/NU.json",
            "target_path": "/thesis",
            "old_value": "Old thesis",
            "new_value": "New thesis",
            "summary": "Refresh the NU thesis",
        },
        repo_root=fixture_repo,
        db_path=database,
        exchange_request_id="bha115-fixture-exchange",
    )
    tenet = record_tenet(body_md="BHA-115 deterministic tenet", status="proposed", db_path=database)
    with sqlite3.connect(database) as connection:
        proposed_fact_id = append_fact(
            connection,
            category="capacity",
            key="home_city",
            value={"city": "San Francisco"},
            narrative="Home city: San Francisco.",
            provenance="wealthplan_import",
        )
        connection.commit()
    with sqlite3.connect(database) as connection:
        expired_fact_id = append_fact(
            connection,
            category="appetite",
            key="dry_powder_policy",
            value={"months": 3.0},
            narrative="Dry-powder policy: keep 3 months uninvested.",
            provenance="owner",
            status="affirmed",
            review_horizon_days=90,
        )
        connection.execute(
            "UPDATE owner_profile_facts SET affirmed_at = '2020-01-01T00:00:00' WHERE id = ?",
            (expired_fact_id,),
        )
        connection.commit()
    with sqlite3.connect(database) as connection:
        cursor = connection.execute(
            "INSERT INTO decisions (ticker, recommendation_kind, decided_by, falsifier, made_at, created_at) "
            "VALUES ('NU', 'add', 'owner', 'BHA-115 falsifier (inferred)', '2026-01-01T00:00:00', '2026-01-01T00:00:00')"
        )
        if cursor.lastrowid is None:
            raise RuntimeError("fixture decision insert did not return an id")
        decision_id = int(cursor.lastrowid)
        connection.commit()
    return {
        "note": note.id,
        "task": task_id,
        "proposal": proposal_id,
        "governed_proposal": governed.proposal_id,
        "tenet": tenet.id,
        "decision": decision_id,
        "proposed_fact": int(proposed_fact_id),
        "expired_fact": int(expired_fact_id),
    }


def _route_request(
    route_name: str, ids: dict[str, int]
) -> tuple[str, str, dict[str, object], tuple[int, ...]]:
    """Resolve one route to a valid path, method, payload, and status contract."""
    routes: dict[str, tuple[str, str, dict[str, object], tuple[int, ...]]] = {
        "/healthz": ("GET", "/healthz", {}, (200,)),
        "/api/capture/text": (
            "POST",
            "/api/capture/text",
            {"json": {"text": "BHA-115 tray fixture"}},
            (200,),
        ),
        "/api/onmymind/<int:note_id>/reply": (
            "POST",
            f"/api/onmymind/{ids['note']}/reply",
            {"json": {"text": "Keep this note"}},
            (200,),
        ),
        "/api/onmymind/<int:note_id>/answer": (
            "GET",
            f"/api/onmymind/{ids['note']}/answer",
            {},
            (200,),
        ),
        "/api/research/task/<int:task_id>/run": (
            "POST",
            f"/api/research/task/{ids['task']}/run",
            {"json": {}},
            (200,),
        ),
        "/api/research/task/<int:task_id>/status": (
            "GET",
            f"/api/research/task/{ids['task']}/status",
            {},
            (200,),
        ),
        "/api/research/task/<int:task_id>/reject": (
            "POST",
            f"/api/research/task/{ids['task']}/reject",
            {"json": {}},
            (200,),
        ),
        "/api/research/proposal/<int:proposal_id>/<verb>": (
            "POST",
            f"/api/research/proposal/{ids['proposal']}/approve",
            {"json": {}},
            (200,),
        ),
        "/api/research/proposals/<int:proposal_id>": (
            "GET",
            f"/api/research/proposals/{ids['governed_proposal']}",
            {},
            (200,),
        ),
        "/api/reconcile/<kind>/<int:item_id>/<verdict>": (
            "POST",
            f"/api/reconcile/note/{ids['note']}/live",
            {"json": {}},
            (200,),
        ),
        "/api/reconcile/falsifier/<int:decision_id>": (
            "POST",
            f"/api/reconcile/falsifier/{ids['decision']}",
            {"json": {"action": "ratify"}},
            (200,),
        ),
        "/api/onmymind/<int:note_id>/<verb>": (
            "POST",
            f"/api/onmymind/{ids['note']}/save",
            {"json": {}},
            (200,),
        ),
        "/api/tenets": (
            "POST",
            "/api/tenets",
            {"json": {"body_md": "BHA-115 route-created tenet"}},
            (200,),
        ),
        "/api/tenets/<int:tenet_id>/<action>": (
            "POST",
            f"/api/tenets/{ids['tenet']}/approve",
            {"json": {}},
            (200,),
        ),
        "/api/profile/fact/<int:fact_id>/affirm": (
            "POST",
            f"/api/profile/fact/{ids['proposed_fact']}/affirm",
            {"json": {}},
            (200,),
        ),
        "/api/profile/fact/<int:fact_id>/reject": (
            "POST",
            f"/api/profile/fact/{ids['proposed_fact']}/reject",
            {"json": {}},
            (200,),
        ),
        "/api/profile/fact/<int:fact_id>/reaffirm": (
            "POST",
            f"/api/profile/fact/{ids['expired_fact']}/reaffirm",
            {"json": {}},
            (200,),
        ),
        "/api/profile/fact/<int:fact_id>/retire": (
            "POST",
            f"/api/profile/fact/{ids['expired_fact']}/retire",
            {"json": {}},
            (200,),
        ),
        "/api/profile/fact/<int:fact_id>/update": (
            "POST",
            f"/api/profile/fact/{ids['expired_fact']}/update",
            {"json": {"narrative": "Dry-powder policy: 4 months now."}},
            (200,),
        ),
        "/api/tenets/distill": ("POST", "/api/tenets/distill", {"json": {}}, (200,)),
    }
    return routes[route_name]


def _routes(root: Path) -> tuple[int, str, int, tuple[RouteCausalCompanion, ...]]:
    """Exercise twenty routes with scored cold and deliberately warmed requests."""
    import http.client
    import socket
    import urllib.request
    from unittest.mock import patch

    import comments_server

    from onmymind import reply as reply_module
    from research import run as research_run_module

    with tempfile.TemporaryDirectory(prefix="bha115-routes-") as temp_name:
        temp_root = Path(temp_name)
        canonical = temp_root / "canonical.db"
        fixture_repo = temp_root / "repo"
        ids = _seed_route_fixture(root, canonical, fixture_repo)
        # The managed runtime uses WAL for writers; checkpoint before copying
        # so committed fixture rows live in the main file, not an un-copied
        # ``-wal`` sidecar.
        with sqlite3.connect(canonical) as connection:
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        # Keep a byte-for-byte immutable template separate from the seed DB.
        # Importing the large Flask surface can initialize legacy DB helpers;
        # route copies must never observe that incidental activity.
        fixture_template = temp_root / "fixture-template.db"
        shutil.copyfile(canonical, fixture_template)
        fixture_repo_template = temp_root / "fixture-repo-template"
        shutil.copytree(fixture_repo, fixture_repo_template)
        fixture_sha256 = hashlib.sha256(fixture_template.read_bytes()).hexdigest()
        fixture_state_sha256 = _database_state_sha256(fixture_template)
        counts = {"sql": 0, "connections": 0}
        external_attempts = 0
        external_hold_seconds = 0.0
        trap_events: list[str] = []
        original_sqlite_connect = sqlite3.connect

        def counted_connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
            connection = original_sqlite_connect(*args, **kwargs)
            counts["connections"] += 1
            connection.set_trace_callback(lambda _sql: counts.__setitem__("sql", counts["sql"] + 1))
            return connection

        def blocked_external(label: str) -> object:
            def deny(*_args: object, **_kwargs: object) -> object:
                nonlocal external_attempts, external_hold_seconds
                started = time.perf_counter()
                external_attempts += 1
                trap_events.append(label)
                external_hold_seconds += time.perf_counter() - started
                raise RuntimeError(f"network disabled for deterministic benchmark ({label})")

            return deny

        def fixture_reply_call(_card: str, _reply: str) -> dict[str, object]:
            trap_events.append("onmymind.reply._default_call")
            return {"intent": "note", "reason": "deterministic fixture"}

        def fixture_web_call(*_args: object, **_kwargs: object) -> str:
            trap_events.append("research.run._call_web")
            return json.dumps(
                {
                    "findings": [],
                    "search_evidence": {
                        "queries": [],
                        "window_covered": "fixture",
                        "sources_opened": [],
                    },
                }
            )

        def fixture_struct_call(*_args: object, **kwargs: object) -> dict[str, object]:
            trap_events.append("research.run._call_struct")
            purpose = str(kwargs.get("purpose", ""))
            if purpose == "research_adversarial_assess":
                return {"refuted": False, "confidence": "low", "rationale": "fixture"}
            return {"title": "BHA-115 fixture draft", "body_md": "Deterministic fixture draft."}

        env_names = (
            "COMMENTS_SERVER_REPORT_CAPABILITY",
            "LEDGER_RESEARCH_RUN",
            "LEDGER_RESEARCH_TAP",
            "LEDGER_ANSWER",
        )
        old_env = {name: os.environ.get(name) for name in env_names}
        os.environ.update(
            {
                "COMMENTS_SERVER_REPORT_CAPABILITY": "bha115-route-fixture-capability",
                "LEDGER_RESEARCH_RUN": "1",
                "LEDGER_RESEARCH_TAP": "0",
                "LEDGER_ANSWER": "0",
            }
        )
        companions: list[RouteCausalCompanion] = []

        def copy_fixture(destination: Path) -> None:
            for suffix in ("-wal", "-shm"):
                with contextlib.suppress(FileNotFoundError):
                    (destination.parent / f"{destination.name}{suffix}").unlink()
            shutil.copyfile(fixture_template, destination)

        def checkpoint_database(database: Path) -> None:
            """Quiesce WAL state before a copy or logical-state read."""
            with sqlite3.connect(database) as connection:
                checkpoint = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                if checkpoint is not None and int(checkpoint[0]) != 0:
                    raise RuntimeError(f"database WAL checkpoint was busy: {checkpoint!r}")

        def wait_for_route_workers() -> None:
            deadline = time.perf_counter() + 2.0
            while time.perf_counter() < deadline:
                if not any(
                    thread.name.startswith("research-run-") for thread in threading.enumerate()
                ):
                    return
                time.sleep(0.005)
            raise RuntimeError("research route worker did not finish during benchmark")

        try:
            with (
                patch.object(sqlite3, "connect", side_effect=counted_connect),
                patch.object(socket, "socket", side_effect=blocked_external("socket.socket")),
                patch.object(
                    socket,
                    "create_connection",
                    side_effect=blocked_external("socket.create_connection"),
                ),
                patch.object(
                    http.client.HTTPConnection,
                    "connect",
                    side_effect=blocked_external("http.client.HTTPConnection.connect"),
                ),
                patch.object(
                    http.client.HTTPSConnection,
                    "connect",
                    side_effect=blocked_external("http.client.HTTPSConnection.connect"),
                ),
                patch.object(
                    urllib.request,
                    "urlopen",
                    side_effect=blocked_external("urllib.request.urlopen"),
                ),
                patch.object(reply_module, "_default_call", side_effect=fixture_reply_call),
                patch.object(research_run_module, "_call_web", side_effect=fixture_web_call),
                patch.object(research_run_module, "_call_struct", side_effect=fixture_struct_call),
            ):
                for phase in ("cold", "warm"):
                    for route_name in ROUTE_NAMES:
                        database = temp_root / f"{phase}-{ROUTE_NAMES.index(route_name)}.db"
                        copy_fixture(database)
                        if hashlib.sha256(database.read_bytes()).hexdigest() != fixture_sha256:
                            raise RuntimeError(
                                f"route fixture copy hash mismatch: {phase} {route_name}"
                            )
                        if _database_state_sha256(database) != fixture_state_sha256:
                            raise RuntimeError(
                                f"route fixture state mismatch: {phase} {route_name}"
                            )
                        route_repo = temp_root / f"{phase}-{ROUTE_NAMES.index(route_name)}-repo"
                        shutil.copytree(fixture_repo_template, route_repo)
                        method, path, kwargs, allowed = _route_request(route_name, ids)
                        typed_method = cast("Literal['GET', 'POST']", method)
                        app = comments_server.create_app(route_repo, db_path=database)
                        app.config.update(TESTING=True)
                        with app.test_client() as client:
                            if phase == "warm":
                                # Warm the same Flask route/app/cache boundary
                                # on this app, then restore pristine state so a
                                # mutating route remains a valid scored tap.
                                warm_response = client.open(
                                    path,
                                    method=typed_method,
                                    headers={
                                        "X-Report-Capability": "bha115-route-fixture-capability"
                                    },
                                    environ_base={"REMOTE_ADDR": "127.0.0.1"},
                                    **cast("dict[str, Any]", kwargs),
                                )
                                if warm_response.status_code not in allowed:
                                    raise RuntimeError(
                                        f"warmup {route_name} returned {warm_response.status_code}"
                                    )
                                if route_name == "/api/research/task/<int:task_id>/run":
                                    # Give the fire-and-forget thread a scheduling
                                    # tick before waiting, avoiding a copy race
                                    # when the worker has not started yet.
                                    time.sleep(0.02)
                                wait_for_route_workers()
                                checkpoint_database(database)
                                copy_fixture(database)
                            before_sql = counts["sql"]
                            before_connections = counts["connections"]
                            route_external_attempts = external_attempts
                            route_external_hold_seconds = external_hold_seconds
                            route_trap_event_count = len(trap_events)
                            started = time.perf_counter()
                            response = client.open(
                                path,
                                method=typed_method,
                                headers={"X-Report-Capability": "bha115-route-fixture-capability"},
                                environ_base={"REMOTE_ADDR": "127.0.0.1"},
                                **cast("dict[str, Any]", kwargs),
                            )
                        if route_name == "/api/research/task/<int:task_id>/run":
                            wait_for_route_workers()
                        elapsed = max(0.000001, time.perf_counter() - started)
                        route_sql_statements = counts["sql"] - before_sql
                        route_connection_count = counts["connections"] - before_connections
                        checkpoint_database(database)
                        state_sha256 = _database_state_sha256(database)
                        body = response.get_data()
                        route_trap_events = trap_events[route_trap_event_count:]
                        route_external_attempt_count = external_attempts - route_external_attempts
                        companions.append(
                            RouteCausalCompanion(
                                route_name=route_name,
                                phase=phase,
                                method=typed_method,
                                status_code=response.status_code,
                                allowed_success_statuses=allowed,
                                elapsed_seconds=elapsed,
                                sql_statements=route_sql_statements,
                                connection_count=route_connection_count,
                                response_sha256=hashlib.sha256(body).hexdigest(),
                                auth_fixture_identity=ROUTE_FIXTURE_IDENTITY,
                                fixture_sha256=fixture_sha256,
                                external_call_hold_seconds=(
                                    external_hold_seconds - route_external_hold_seconds
                                ),
                                network_disabled=True,
                                state_sha256=state_sha256,
                                external_attempt_count=route_external_attempt_count,
                                external_trap_sha256=external_trap_proof_sha256(
                                    events=tuple(route_trap_events)
                                ),
                                external_trap_proof_version=EXTERNAL_TRAP_PROOF_VERSION,
                                external_trap_coverage=EXTERNAL_TRAP_BOUNDARIES,
                                external_trap_events=tuple(route_trap_events),
                            )
                        )
                        if response.status_code not in allowed:
                            raise RuntimeError(
                                f"{phase} {route_name} returned {response.status_code}"
                            )
                        if route_external_attempt_count != 0 or external_attempts != 0:
                            raise RuntimeError(f"external attempt observed on {phase} {route_name}")
                        if state_sha256 == "":
                            raise RuntimeError("empty route state fingerprint")
        finally:
            for name, value in old_env.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value
        cold = {item.route_name: item for item in companions if item.phase == "cold"}
        warm = {item.route_name: item for item in companions if item.phase == "warm"}
        for name in ROUTE_NAMES:
            left, right = cold[name], warm[name]
            if (
                left.status_code,
                left.allowed_success_statuses,
                left.response_sha256,
                left.fixture_sha256,
                left.state_sha256,
            ) != (
                right.status_code,
                right.allowed_success_statuses,
                right.response_sha256,
                right.fixture_sha256,
                right.state_sha256,
            ):
                raise RuntimeError(
                    f"cold/warm route identity mismatch: {name} "
                    f"cold={left.model_dump()} warm={right.model_dump()}"
                )
        with sqlite3.connect(fixture_template) as connection:
            revision = str(
                connection.execute("SELECT version_num FROM alembic_version").fetchone()[0]
            )
        if fixture_state_sha256 == "":
            raise RuntimeError("empty canonical fixture state fingerprint")
        return len(companions), revision, counts["sql"], tuple(companions)


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    root = args.repo_root.resolve()
    started = time.perf_counter()
    _event("bha115_workload_started", workload=args.workload)
    try:
        route_companions: tuple[RouteCausalCompanion, ...] = ()
        alembic_invocations = 0
        migration_elapsed_seconds: float | None = None
        schema_object_count: int | None = None
        if args.workload == "integrity":
            (
                rows,
                alembic_revision,
                sql_statements,
                migration_elapsed_seconds,
                alembic_invocations,
                schema_object_count,
            ) = _integrity(root)
            stage = "integrity"
        elif args.workload == "migrations":
            (
                rows,
                alembic_revision,
                sql_statements,
                migration_elapsed_seconds,
                alembic_invocations,
                schema_object_count,
            ) = _migrations(root)
            stage = "migrations"
        else:
            rows, alembic_revision, sql_statements, route_companions = _routes(root)
            stage = "route-render"
        revision = subprocess.check_output(
            ["git", "-C", str(root), "rev-parse", "HEAD"], text=True
        ).strip()
        envelope = CausalRunEnvelope(
            sql_statements=sql_statements,
            rows=rows,
            elapsed_seconds=max(0.000001, time.perf_counter() - started),
            peak_rss_bytes=_rss_bytes(),
            alembic_revision=alembic_revision,
            alembic_invocations=alembic_invocations,
            migration_elapsed_seconds=migration_elapsed_seconds,
            schema_object_count=schema_object_count,
            query_plan_sha256=None,
            connection_role=(
                "read"
                if stage == "integrity"
                else ("request_scoped_read" if stage == "route-render" else "none")
            ),
            stage=stage,
            revision=revision,
            route_companions=route_companions,
            rss_semantics="process_high_water",
        )
    except Exception as exc:
        _event("bha115_workload_failed", error=type(exc).__name__, workload=args.workload)
        return 1
    print(json.dumps(envelope.model_dump(mode="json"), sort_keys=True))
    _event("bha115_workload_finished", stage=envelope.stage, rows=envelope.rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
