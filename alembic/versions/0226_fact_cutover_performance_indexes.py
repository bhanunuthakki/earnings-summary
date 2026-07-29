"""Index the evidence-anchor joins used by the bounded fact cutover.

Revision ID: 0226_fact_cutover_performance_indexes
Revises: 0225_financial_fact_resolution_cutover
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0226_fact_cutover_performance_indexes"
down_revision: str | None = "0225_financial_fact_resolution_cutover"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_RUNS = "evidence_extraction_runs"
_NODES = "evidence_nodes"
_RUN_INDEX = "ix_evidence_runs_document_outcome"
_NODE_INDEX = "ix_evidence_nodes_extraction_kind"


def _index_names(table_name: str) -> set[str]:
    return {
        str(index["name"])
        for index in sa.inspect(op.get_bind()).get_indexes(table_name)
        if index.get("name") is not None
    }


def upgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if _RUNS in tables and _RUN_INDEX not in _index_names(_RUNS):
        op.create_index(
            _RUN_INDEX,
            _RUNS,
            ["document_version_id", "outcome", "completed_at", "extraction_run_id"],
        )
    if _NODES in tables and _NODE_INDEX not in _index_names(_NODES):
        op.create_index(
            _NODE_INDEX,
            _NODES,
            ["extraction_run_id", "node_kind", "revision", "node_id"],
        )


def downgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if _NODES in tables and _NODE_INDEX in _index_names(_NODES):
        op.drop_index(_NODE_INDEX, table_name=_NODES)
    if _RUNS in tables and _RUN_INDEX in _index_names(_RUNS):
        op.drop_index(_RUN_INDEX, table_name=_RUNS)
