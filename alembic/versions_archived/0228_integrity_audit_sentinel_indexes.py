"""Add sparse sentinel indexes for operational integrity audits.

Revision ID: 0228_integrity_audit_sentinel_indexes
Revises: 0227_issuer_reporting_registry

The invalid-observation audit previously scanned every wide observation row
twice (exact count plus bounded sample).  This partial index contains only
rows that violate the invariant, preserving an exact audit while making the
healthy path proportional to the number of violations rather than corpus
size.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0228_integrity_audit_sentinel_indexes"
down_revision: str | None = "0227_issuer_reporting_registry"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "reported_observations"
_INDEX = "ix_reported_observations_invalid_value_clock"
_RESOLUTIONS = "observation_resolution_revisions"
_MEMBERSHIP_INDEX = "ix_observation_resolution_selected_membership_audit"
_CHAIN_INDEX = "ix_observation_resolution_chain_audit"
_OUTCOMES = "fact_resolution_outcomes"
_OUTCOME_STATUS_INDEX = "ix_fact_resolution_outcomes_status_resolution"
_OBSERVATION_EVIDENCE_INDEX = "ix_reported_observations_evidence_node_observation"
_INVALID_VALUE_OR_CLOCK = (
    "(numeric_value IS NULL AND text_value IS NULL) "
    "OR (numeric_value IS NOT NULL AND text_value IS NOT NULL) "
    "OR (numeric_value IS NOT NULL AND unit IS NULL) "
    "OR (numeric_value IS NOT NULL AND unit = 'currency' AND currency IS NULL) "
    "OR recorded_at < available_at"
)


def _index_names(table: str) -> set[str]:
    return {
        str(index["name"])
        for index in sa.inspect(op.get_bind()).get_indexes(table)
        if index.get("name") is not None
    }


def upgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if _TABLE in tables and _INDEX not in _index_names(_TABLE):
        op.create_index(
            _INDEX,
            _TABLE,
            ["observation_id"],
            unique=False,
            sqlite_where=sa.text(_INVALID_VALUE_OR_CLOCK),
        )
    if _TABLE in tables and _OBSERVATION_EVIDENCE_INDEX not in _index_names(_TABLE):
        op.create_index(
            _OBSERVATION_EVIDENCE_INDEX,
            _TABLE,
            ["evidence_node_id", "observation_id"],
        )
    if _RESOLUTIONS in tables:
        resolution_indexes = _index_names(_RESOLUTIONS)
        if _MEMBERSHIP_INDEX not in resolution_indexes:
            op.create_index(
                _MEMBERSHIP_INDEX,
                _RESOLUTIONS,
                ["resolution_id", "selected_observation_id"],
            )
        if _CHAIN_INDEX not in resolution_indexes:
            op.create_index(
                _CHAIN_INDEX,
                _RESOLUTIONS,
                ["resolution_id", "supersedes_resolution_id", "logical_key", "revision"],
            )
    if _OUTCOMES in tables and _OUTCOME_STATUS_INDEX not in _index_names(_OUTCOMES):
        op.create_index(
            _OUTCOME_STATUS_INDEX,
            _OUTCOMES,
            ["resolution_status", "resolution_id"],
        )


def downgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if _OUTCOMES in tables and _OUTCOME_STATUS_INDEX in _index_names(_OUTCOMES):
        op.drop_index(_OUTCOME_STATUS_INDEX, table_name=_OUTCOMES)
    if _RESOLUTIONS in tables:
        resolution_indexes = _index_names(_RESOLUTIONS)
        if _CHAIN_INDEX in resolution_indexes:
            op.drop_index(_CHAIN_INDEX, table_name=_RESOLUTIONS)
        if _MEMBERSHIP_INDEX in resolution_indexes:
            op.drop_index(_MEMBERSHIP_INDEX, table_name=_RESOLUTIONS)
    if _TABLE in tables:
        observation_indexes = _index_names(_TABLE)
        if _OBSERVATION_EVIDENCE_INDEX in observation_indexes:
            op.drop_index(_OBSERVATION_EVIDENCE_INDEX, table_name=_TABLE)
        if _INDEX in observation_indexes:
            op.drop_index(_INDEX, table_name=_TABLE)
