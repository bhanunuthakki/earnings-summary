"""Drop retired coaching, Telegram reply, nudge, and weekly packet stores.

The retired product had no remaining runtime readers or writers after revision
0062 removed the decision-journal joins. This migration removes only its six
exclusive tables. Restore a reviewed predecessor database snapshot to recover
their history; downgrade never creates empty replacements.
"""

from __future__ import annotations

import re

from alembic import op
from sqlalchemy.engine import Connection

revision = "0064_retired_capture_coaching_stores"
down_revision = "0063_document_product_completion"
branch_labels = None
depends_on = None

_DROP_ORDER = (
    "pending_telegram_replies",
    "coach_pings",
    "coach_mutes",
    "decision_nudges",
    "weekly_packet_items",
    "weekly_packet_runs",
)
_EXPECTED_INTERNAL_FKS = {("weekly_packet_items", "weekly_packet_runs")}


def _quote_identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _assert_no_unexpected_dependencies(connection: Connection) -> None:
    tables = {
        str(row[0])
        for row in connection.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    missing = sorted(set(_DROP_ORDER) - tables)
    if missing:
        raise RuntimeError(f"retired-store migration is missing expected tables: {missing}")

    for row in connection.exec_driver_sql(
        "SELECT name, sql FROM sqlite_master WHERE type='view' AND sql IS NOT NULL"
    ).fetchall():
        view_name, sql = str(row[0]), str(row[1])
        for table_name in _DROP_ORDER:
            pattern = rf"(?<![A-Za-z0-9_]){re.escape(table_name)}(?![A-Za-z0-9_])"
            if re.search(pattern, sql, re.I):
                raise RuntimeError(
                    f"view {view_name!r} still references retired table {table_name!r}"
                )

    for source_table in sorted(tables):
        rows = connection.exec_driver_sql(
            f"PRAGMA foreign_key_list({_quote_identifier(source_table)})"
        ).fetchall()
        for row in rows:
            target_table = str(row[2])
            if target_table not in _DROP_ORDER:
                continue
            dependency = (source_table, target_table)
            if dependency not in _EXPECTED_INTERNAL_FKS:
                raise RuntimeError(
                    "unexpected foreign-key dependency on retired table: "
                    f"{source_table!r} references {target_table!r}"
                )


def upgrade() -> None:
    bind = op.get_bind()
    _assert_no_unexpected_dependencies(bind)
    for table_name in _DROP_ORDER:
        op.execute(f"DROP TABLE {_quote_identifier(table_name)}")


def downgrade() -> None:
    raise RuntimeError(
        "Restore a reviewed predecessor database snapshot to recover retired "
        "Telegram, coaching, nudge, and weekly-packet history"
    )
