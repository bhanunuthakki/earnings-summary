"""Alembic migration environment.

Wires SQLAlchemy metadata to Alembic's context. `target_metadata` is None until
Phase 2 of the backend redesign declares the SQLAlchemy models — until then,
migrations are written by hand against `op.create_table` / `op.alter_column`.
"""

from __future__ import annotations

import sys
from logging.config import fileConfig
from pathlib import Path

from sqlalchemy import engine_from_config, pool

from alembic import context

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from sqlite_runtime import require_safe_sqlite_writer_runtime  # noqa: E402

config = context.config

if config.config_file_name is not None:
    # disable_existing_loggers=False: running migrations in-process (tests,
    # the morning pipeline, the dashboard) must NOT mute the application's
    # already-configured loggers. The alembic default (True) disables every
    # logger not named in alembic.ini — which silently drops app log records
    # for the rest of the process and caused order-dependent test failures
    # (a migration test running first muted llm.cli warnings caplog expected).
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = None


def run_migrations_offline() -> None:
    """Run migrations without a live DB connection (emit SQL only)."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against a live DB connection."""
    configured_url = config.get_main_option("sqlalchemy.url") or ""
    if configured_url.lower().startswith("sqlite"):
        require_safe_sqlite_writer_runtime()
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        # Match the platform-wide 30s busy timeout (src/sqlite_runtime.py /
        # reference_platform_invariants). Without it, alembic fails INSTANTLY
        # with "database is locked" whenever any writer holds the lock — the
        # 2026-07-31 db_gc incident blocked the #1108 deploy this way. Now
        # that db_gc commits in bounded batches, 30s outlasts any one batch.
        # PRAGMA at the raw DBAPI level: exec_driver_sql would trip
        # SQLAlchemy 2.x autobegin and leave a transaction open under
        # alembic's own transaction management, silently rolling back every
        # migration (NoSuchTableError across the suite).
        if connection.engine.dialect.name == "sqlite":
            raw = connection.connection.driver_connection
            if raw is not None:
                raw.execute("PRAGMA busy_timeout = 30000")
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
