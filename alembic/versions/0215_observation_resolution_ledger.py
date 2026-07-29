"""Add immutable reported observations and revisioned resolution decisions.

This is an additive foundation.  It does not rewrite or read-cut over any
legacy fact table.  Reported observations retain their evidence anchor and
both clocks; resolution revisions retain the complete candidate set that was
considered at a given knowledge cutoff.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0215_observation_resolution_ledger"
down_revision: str | Sequence[str] | None = "0214_evidence_selection_lifecycle"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OBSERVATIONS = "reported_observations"
_RESOLUTIONS = "observation_resolution_revisions"
_CANDIDATES = "observation_resolution_candidates"
_CURRENT_VIEW = "v_observation_resolution_current"
_APPEND_ONLY_TABLES = (_OBSERVATIONS, _RESOLUTIONS, _CANDIDATES)


def _append_only_triggers(table: str) -> None:
    op.execute(
        f"CREATE TRIGGER trg_{table}_append_only BEFORE UPDATE ON {table} "
        "BEGIN SELECT RAISE(ABORT, 'observation resolution ledger is append-only'); END"
    )
    op.execute(
        f"CREATE TRIGGER trg_{table}_append_only_delete BEFORE DELETE ON {table} "
        "BEGIN SELECT RAISE(ABORT, 'observation resolution ledger is append-only'); END"
    )


def upgrade() -> None:
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())

    if _OBSERVATIONS not in existing:
        op.create_table(
            _OBSERVATIONS,
            sa.Column("observation_id", sa.String(length=128), primary_key=True),
            sa.Column("idempotency_key", sa.String(length=256), nullable=False, unique=True),
            sa.Column("issuer_id", sa.String(length=128), nullable=False),
            sa.Column("ticker", sa.String(length=16), nullable=True),
            sa.Column("concept_key", sa.String(length=256), nullable=False),
            sa.Column("period_start", sa.DateTime(), nullable=False),
            sa.Column("period_end", sa.DateTime(), nullable=False),
            sa.Column("fiscal_period_type", sa.String(length=32), nullable=False),
            sa.Column("dimensions_json", sa.Text(), nullable=False),
            sa.Column("numeric_value", sa.Text(), nullable=True),
            sa.Column("text_value", sa.Text(), nullable=True),
            sa.Column("currency", sa.String(length=16), nullable=True),
            sa.Column("unit", sa.String(length=64), nullable=True),
            sa.Column("scale", sa.Integer(), nullable=True),
            sa.Column("observation_status", sa.String(length=16), nullable=False),
            sa.Column(
                "evidence_node_id",
                sa.String(length=128),
                sa.ForeignKey("evidence_nodes.node_id"),
                nullable=False,
            ),
            sa.Column("available_at", sa.DateTime(), nullable=False),
            sa.Column("recorded_at", sa.DateTime(), nullable=False),
            sa.Column("method", sa.String(length=128), nullable=False),
            sa.Column("method_version", sa.String(length=128), nullable=False),
            sa.Column("confidence", sa.Float(), nullable=False),
            sa.Column("legacy_table", sa.String(length=128), nullable=True),
            sa.Column("legacy_row_id", sa.Integer(), nullable=True),
            sa.UniqueConstraint(
                "legacy_table", "legacy_row_id", name="uq_reported_observation_legacy"
            ),
            sa.CheckConstraint(
                "fiscal_period_type IN ('annual', 'quarter', 'year_to_date', 'instant', 'other')",
                name="ck_reported_observation_period_type",
            ),
            sa.CheckConstraint(
                "observation_status IN ('reported', 'derived')",
                name="ck_reported_observation_status",
            ),
            sa.CheckConstraint(
                "(numeric_value IS NOT NULL AND text_value IS NULL) OR "
                "(numeric_value IS NULL AND text_value IS NOT NULL)",
                name="ck_reported_observation_one_value",
            ),
            sa.CheckConstraint(
                "(legacy_table IS NULL AND legacy_row_id IS NULL) OR "
                "(legacy_table IS NOT NULL AND legacy_row_id IS NOT NULL)",
                name="ck_reported_observation_legacy_pair",
            ),
            sa.CheckConstraint(
                "confidence >= 0 AND confidence <= 1", name="ck_reported_observation_confidence"
            ),
            sa.CheckConstraint(
                "period_end >= period_start", name="ck_reported_observation_period_range"
            ),
        )
        op.create_index(
            "ix_reported_observation_lookup",
            _OBSERVATIONS,
            ["issuer_id", "ticker", "concept_key", "period_end", "fiscal_period_type"],
        )

    if _RESOLUTIONS not in existing:
        op.create_table(
            _RESOLUTIONS,
            sa.Column("resolution_id", sa.String(length=128), primary_key=True),
            sa.Column("idempotency_key", sa.String(length=256), nullable=False, unique=True),
            sa.Column("logical_key", sa.String(length=256), nullable=False),
            sa.Column("revision", sa.Integer(), nullable=False),
            sa.Column(
                "selected_observation_id",
                sa.String(length=128),
                sa.ForeignKey(f"{_OBSERVATIONS}.observation_id"),
                nullable=False,
            ),
            sa.Column("resolver_kind", sa.String(length=64), nullable=False),
            sa.Column("policy_version", sa.String(length=128), nullable=False),
            sa.Column("reason", sa.Text(), nullable=False),
            sa.Column("knowledge_cutoff", sa.DateTime(), nullable=False),
            sa.Column("effective_at", sa.DateTime(), nullable=False),
            sa.Column("material_dissent", sa.Boolean(), nullable=False),
            sa.Column(
                "supersedes_resolution_id",
                sa.String(length=128),
                sa.ForeignKey(f"{_RESOLUTIONS}.resolution_id"),
                nullable=True,
            ),
            sa.Column("recorded_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint(
                "logical_key", "revision", name="uq_observation_resolution_revision"
            ),
            sa.CheckConstraint("revision > 0", name="ck_observation_resolution_revision_positive"),
        )
        op.create_index(
            "ix_observation_resolution_logical_revision",
            _RESOLUTIONS,
            ["logical_key", "revision"],
        )

    if _CANDIDATES not in existing:
        op.create_table(
            _CANDIDATES,
            sa.Column(
                "resolution_id",
                sa.String(length=128),
                sa.ForeignKey(
                    f"{_RESOLUTIONS}.resolution_id", deferrable=True, initially="DEFERRED"
                ),
                primary_key=True,
            ),
            sa.Column(
                "observation_id",
                sa.String(length=128),
                sa.ForeignKey(f"{_OBSERVATIONS}.observation_id"),
                primary_key=True,
            ),
        )

    op.execute(
        "CREATE TRIGGER trg_observation_resolution_selected_candidate BEFORE INSERT ON "
        f"{_RESOLUTIONS} WHEN NOT EXISTS (SELECT 1 FROM {_CANDIDATES} "
        "WHERE resolution_id = NEW.resolution_id AND observation_id = NEW.selected_observation_id) "
        "BEGIN SELECT RAISE(ABORT, 'selected observation must be a resolution candidate'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_observation_resolution_candidates_finalized BEFORE INSERT ON "
        f"{_CANDIDATES} WHEN EXISTS (SELECT 1 FROM {_RESOLUTIONS} "
        "WHERE resolution_id = NEW.resolution_id) "
        "BEGIN SELECT RAISE(ABORT, 'resolution candidate set is finalized'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_observation_resolution_revision_chain BEFORE INSERT ON "
        f"{_RESOLUTIONS} WHEN NEW.revision = 1 AND NEW.supersedes_resolution_id IS NOT NULL "
        "BEGIN SELECT RAISE(ABORT, 'first resolution revision cannot supersede'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_observation_resolution_revision_parent BEFORE INSERT ON "
        f"{_RESOLUTIONS} WHEN NEW.revision > 1 AND (NEW.supersedes_resolution_id IS NULL OR NOT EXISTS "
        f"(SELECT 1 FROM {_RESOLUTIONS} WHERE resolution_id = NEW.supersedes_resolution_id "
        "AND logical_key = NEW.logical_key AND revision = NEW.revision - 1)) "
        "BEGIN SELECT RAISE(ABORT, 'resolution revision must supersede the previous revision'); END"
    )
    for table in _APPEND_ONLY_TABLES:
        _append_only_triggers(table)
    op.execute(
        f"CREATE VIEW {_CURRENT_VIEW} AS "
        f"SELECT resolution_id, idempotency_key, logical_key, revision, selected_observation_id, "
        "resolver_kind, policy_version, reason, knowledge_cutoff, effective_at, material_dissent, "
        "supersedes_resolution_id, recorded_at FROM observation_resolution_revisions AS revision "
        "WHERE NOT EXISTS (SELECT 1 FROM observation_resolution_revisions AS newer "
        "WHERE newer.logical_key = revision.logical_key AND newer.revision > revision.revision)"
    )


def downgrade() -> None:
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())
    op.execute(f"DROP VIEW IF EXISTS {_CURRENT_VIEW}")
    for trigger in (
        "trg_observation_resolution_selected_candidate",
        "trg_observation_resolution_candidates_finalized",
        "trg_observation_resolution_revision_chain",
        "trg_observation_resolution_revision_parent",
    ):
        op.execute(f"DROP TRIGGER IF EXISTS {trigger}")
    for table in _APPEND_ONLY_TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_append_only")
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_append_only_delete")
    if _CANDIDATES in existing:
        op.drop_table(_CANDIDATES)
    if _RESOLUTIONS in existing:
        op.drop_index("ix_observation_resolution_logical_revision", table_name=_RESOLUTIONS)
        op.drop_table(_RESOLUTIONS)
    if _OBSERVATIONS in existing:
        op.drop_index("ix_reported_observation_lookup", table_name=_OBSERVATIONS)
        op.drop_table(_OBSERVATIONS)
