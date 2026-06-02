"""Alembic migration environment.

Wires SQLAlchemy metadata to Alembic's context. `target_metadata` is None until
Phase 2 of the backend redesign declares the SQLAlchemy models — until then,
migrations are written by hand against `op.create_table` / `op.alter_column`.
"""

from __future__ import annotations

from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

from alembic import context

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
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
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
