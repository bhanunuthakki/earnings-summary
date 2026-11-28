"""Retire 27 verified-empty dead-subsystem tables (110 triggers, 5 views).

Revision ID: 0080_retire_unused_ask_audit_search_fact_archive_population_tables
Revises: 0079_simplify_transcript_intake_and_scan_log
Create Date: 2026-11-28

Drops four unused, zero-row schema families that have no live production writers
or readers:
  1. Sealed/shadow Ask audit & heterogeneous retrieval traces (13 tables, 53 triggers, 1 view)
  2. Fact-search v2 projection tables (4 tables, 24 triggers, 2 views)
  3. Archive generation catalog tables (3 tables, 8 triggers, 1 view)
  4. Population run/cutover/parity ledger tables (7 tables, 25 triggers, 1 view)

Following the precedent in ``0064_retired_capture_coaching_stores``, every
present target table is asserted empty (``COUNT(*) == 0``) before drop, and
unexpected cross-table triggers, views, or foreign keys fail closed. Restore a
reviewed predecessor database snapshot to recover retired empty schema objects.
"""

from __future__ import annotations

import re

from sqlalchemy.engine import Connection

from alembic import op

revision = "0080_retire_unused_ask_audit_search_fact_archive_population_tables"
down_revision = "0079_simplify_transcript_intake_and_scan_log"
branch_labels = None
depends_on = None

_DROP_VIEWS: tuple[str, ...] = (
    "v_archive_generations_verified",
    "v_ask_retrieval_scope_current",
    "v_population_cutover_current",
    "v_search_fact_hits_current",
    "v_search_fact_projection_current_sealed",
)

# Topological order: child tables dropped strictly before parents they reference.
_DROP_TABLES: tuple[str, ...] = (
    "archive_generation_registration_receipts",
    "archive_generation_table_commitments",
    "ask_answer_audit_claim_citations",
    "ask_answer_audit_retrievals",
    "ask_answer_audit_seals",
    "ask_answer_groundings",
    "ask_retrieval_trace_hits",
    "ask_retrieval_trace_items",
    "heterogeneous_retrieval_trace_seals",
    "population_cutover_audit_receipts",
    "population_cutover_receipts",
    "population_parity_receipts",
    "population_plane_receipts",
    "search_fact_projection_memberships",
    "search_fact_projection_seals",
    "archive_generations",
    "ask_answer_audit_citations",
    "ask_answer_audit_claims",
    "ask_retrieval_scope_promotions",
    "search_fact_projection_rows",
    "ask_retrieval_traces",
    "population_run_headers",
    "heterogeneous_retrieval_trace_results",
    "ask_answer_audit_records",
    "search_fact_projection_runs",
    "heterogeneous_retrieval_trace_candidates",
    "heterogeneous_retrieval_trace_headers",
)


def _quote_identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _preflight(connection: Connection) -> list[str]:
    existing_tables = {
        str(row[0])
        for row in connection.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    to_drop = [table for table in _DROP_TABLES if table in existing_tables]
    drop_set = set(to_drop)
    drop_views_set = set(_DROP_VIEWS)

    # Assert every retired table is strictly empty before dropping.
    non_empty: list[tuple[str, int]] = []
    for table_name in to_drop:
        count = int(
            connection.exec_driver_sql(
                f"SELECT COUNT(*) FROM {_quote_identifier(table_name)}"  # nosec B608
            ).scalar_one()
        )
        if count != 0:
            non_empty.append((table_name, count))
    if non_empty:
        raise RuntimeError(f"refusing to drop non-empty retired tables: {non_empty}")

    # Ensure no surviving trigger references a retired table.
    for row in connection.exec_driver_sql(
        "SELECT name, tbl_name, sql FROM sqlite_master WHERE type='trigger' AND sql IS NOT NULL"
    ).fetchall():
        trigger_name, source_table, sql = str(row[0]), str(row[1]), str(row[2])
        if source_table in drop_set:
            continue
        for table_name in to_drop:
            pattern = rf"(?<![A-Za-z0-9_]){re.escape(table_name)}(?![A-Za-z0-9_])"
            if re.search(pattern, sql, re.I):
                raise RuntimeError(
                    f"trigger {trigger_name!r} on {source_table!r} still references {table_name!r}"
                )

    # Ensure no surviving view references a retired table.
    for row in connection.exec_driver_sql(
        "SELECT name, sql FROM sqlite_master WHERE type='view' AND sql IS NOT NULL"
    ).fetchall():
        view_name, sql = str(row[0]), str(row[1])
        if view_name in drop_views_set:
            continue
        for table_name in to_drop:
            pattern = rf"(?<![A-Za-z0-9_]){re.escape(table_name)}(?![A-Za-z0-9_])"
            if re.search(pattern, sql, re.I):
                raise RuntimeError(
                    f"view {view_name!r} still references retired table {table_name!r}"
                )

    # Ensure no surviving table has an inbound FK pointing to a retired table.
    for source_table in sorted(existing_tables - drop_set):
        rows = connection.exec_driver_sql(
            f"PRAGMA foreign_key_list({_quote_identifier(source_table)})"
        ).fetchall()
        for row in rows:
            target_table = str(row[2])
            if target_table in drop_set:
                raise RuntimeError(
                    f"surviving table {source_table!r} has FK referencing {target_table!r}"
                )

    return to_drop


def upgrade() -> None:
    bind = op.get_bind()
    to_drop = _preflight(bind)
    for view_name in _DROP_VIEWS:
        op.execute(f"DROP VIEW IF EXISTS {_quote_identifier(view_name)}")
    for table_name in to_drop:
        op.execute(f"DROP TABLE {_quote_identifier(table_name)}")


def downgrade() -> None:
    raise RuntimeError(
        "Restore a reviewed predecessor database snapshot to recover retired "
        "ask-audit, search-fact-projection, archive-generation, and population-ledger tables"
    )
