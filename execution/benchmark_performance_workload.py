"""Run one real BHA-115 integrity, migration, or route workload."""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import logging
import platform
import resource
import subprocess
import sys
import tempfile
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(PROJECT_ROOT / "execution"))

from quality import performance_routes as _performance_routes  # noqa: E402
from quality.performance import CausalRunEnvelope, RouteCausalCompanion  # noqa: E402
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite  # noqa: E402

# Compatibility aliases retain the private seams used by focused tests and
# downstream benchmark helpers while the implementation lives in one module.
ROUTE_FIXTURE_IDENTITY = _performance_routes.ROUTE_FIXTURE_IDENTITY
ROUTE_NAMES = _performance_routes.ROUTE_NAMES
_route_request = _performance_routes.route_request
_routes = _performance_routes.routes
_seed_route_fixture = _performance_routes.seed_route_fixture


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
    with connect_sqlite(database, role=SQLiteConnectionRole.READ_ONLY) as connection:
        return int(
            connection.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type IN ('table', 'index', 'trigger', 'view')"
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
    with connect_sqlite(database, role=SQLiteConnectionRole.READ_ONLY) as connection:
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
    with tempfile.TemporaryDirectory(prefix="bha115-migrations-") as temp_name:
        database = Path(temp_name) / "portfolio.db"
        statement_count, revision, migration_elapsed, alembic_invocations = _migrate(root, database)
        with connect_sqlite(database, role=SQLiteConnectionRole.READ_ONLY) as connection:
            observed_revision = str(
                connection.execute("SELECT version_num FROM alembic_version").fetchone()[0]
            )
        if observed_revision != revision:
            raise RuntimeError(
                f"migration revision changed while measuring schema: {revision!r} -> {observed_revision!r}"
            )
        return (
            0,
            revision,
            statement_count,
            migration_elapsed,
            alembic_invocations,
            _schema_object_count(database),
        )


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
