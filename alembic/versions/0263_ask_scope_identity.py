"""Bind Ask promotions to the composite 0227 source-scope identity.

Revision ID: 0263_ask_scope_identity
Revises: 0262_news_events

The legacy ``scope_key`` column remains in place for compatibility with the
0261 latest-state tables, but from this revision onward it stores only the
canonical issuer-specific retrieval scope ID. The raw 0227 key and exact source
revision are preserved in separate immutable columns.

SQLite evaluates the canonical digest through a deterministic function
registered by the central connection factory. Connections that bypass that
factory cannot insert promotions because the trigger function is unavailable.
Once the first v2 promotion exists, rollback is forward-only (withdraw/disable
or restore a pre-activation snapshot); schema downgrade intentionally refuses.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0263_ask_scope_identity"
down_revision: str | None = "0262_news_events"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "ask_retrieval_scope_promotions"
_TRIGGER = "trg_ask_retrieval_scope_promotion_source_scope_exact"


def _require_empty(bind: sa.Connection, *, operation: str) -> None:
    if (
        bind.execute(sa.text("SELECT 1 FROM ask_retrieval_scope_promotions LIMIT 1")).first()
        is not None
    ):
        raise RuntimeError(
            f"0263 {operation} requires an empty Ask promotion table; "
            "existing immutable promotions need an explicit evidence-preserving migration"
        )


def _acquire_writer_lock(bind: sa.Connection) -> None:
    """Acquire SQLite's writer reservation inside Alembic's transaction.

    Alembic opens a deferred transaction for SQLite. A no-op write promotes it
    to the same reserved-writer state as ``BEGIN IMMEDIATE`` without attempting
    to nest another transaction. The lock is therefore held from before the
    emptiness check through the DDL and Alembic's commit.
    """

    bind.exec_driver_sql(
        "UPDATE ask_retrieval_scope_promotions SET promotion_id=promotion_id WHERE 0"
    )


def upgrade() -> None:
    bind = op.get_bind()
    _acquire_writer_lock(bind)
    _require_empty(bind, operation="upgrade")
    op.add_column(
        _TABLE,
        sa.Column("source_scope_key", sa.String(128), nullable=True),
    )
    op.add_column(
        _TABLE,
        sa.Column("source_scope_revision_id", sa.String(128), nullable=True),
    )
    op.execute(
        f"CREATE TRIGGER {_TRIGGER} BEFORE INSERT ON {_TABLE} WHEN "  # nosec B608 -- migration-internal fixed identifiers
        "NEW.scope_key<>derive_retrieval_scope_id(NEW.source_scope_key,NEW.issuer_id) "
        "OR length(NEW.scope_key)<>77 OR substr(NEW.scope_key,1,13)<>'ask-scope:v1:' "
        "OR substr(NEW.scope_key,14) GLOB '*[^0-9a-f]*' "
        "OR NEW.source_scope_key IS NULL OR length(NEW.source_scope_key)=0 "
        "OR NEW.source_scope_revision_id IS NULL "
        "OR length(NEW.source_scope_revision_id)=0 "
        "OR NOT EXISTS (SELECT 1 FROM v_issuer_reporting_scope_current source "
        "WHERE source.scope_revision_id=NEW.source_scope_revision_id "
        "AND source.scope_key=NEW.source_scope_key "
        "AND source.issuer_id=NEW.issuer_id "
        "AND source.inclusion_state='core') "
        "BEGIN SELECT RAISE(ABORT, "
        "'Ask promotion requires its exact current composite source scope'); END"
    )


def downgrade() -> None:
    bind = op.get_bind()
    _acquire_writer_lock(bind)
    _require_empty(bind, operation="downgrade")
    op.execute(f"DROP TRIGGER IF EXISTS {_TRIGGER}")
    trigger_rows = bind.execute(
        sa.text(
            "SELECT name,sql FROM sqlite_master "
            "WHERE type='trigger' AND tbl_name=:table_name "
            "AND sql IS NOT NULL ORDER BY name"
        ),
        {"table_name": _TABLE},
    ).fetchall()
    triggers = tuple((str(row[0]), str(row[1])) for row in trigger_rows)
    for name, _sql in triggers:
        escaped = name.replace('"', '""')
        op.execute(f'DROP TRIGGER "{escaped}"')  # nosec B608 -- sqlite_master identity
    bind.exec_driver_sql("PRAGMA writable_schema=ON")
    try:
        try:
            op.drop_column(_TABLE, "source_scope_revision_id")
            op.drop_column(_TABLE, "source_scope_key")
        finally:
            bind.exec_driver_sql("PRAGMA writable_schema=OFF")
    finally:
        for _name, sql in triggers:
            op.execute(sql)
    restored = tuple(
        (str(row[0]), str(row[1]))
        for row in bind.execute(
            sa.text(
                "SELECT name,sql FROM sqlite_master "
                "WHERE type='trigger' AND tbl_name=:table_name "
                "AND sql IS NOT NULL ORDER BY name"
            ),
            {"table_name": _TABLE},
        ).fetchall()
    )
    if restored != triggers:
        raise RuntimeError("0263 downgrade did not restore the exact promotion trigger inventory")
