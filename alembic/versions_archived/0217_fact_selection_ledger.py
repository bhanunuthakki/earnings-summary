"""Add immutable selection decisions for legacy fact rows.

Legacy fact rows remain physically present.  This ledger records which row a
reader should include or exclude, why, under which deterministic policy, and
what was known at the time.  Reader cutover is deliberately deferred.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0217_fact_selection_ledger"
down_revision: str | Sequence[str] | None = "0216_search_corpus_foundation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "fact_selection_decisions"
_CURRENT_VIEW = "v_fact_selection_current"


def _append_only_triggers() -> None:
    op.execute(
        f"CREATE TRIGGER trg_{_TABLE}_append_only BEFORE UPDATE ON {_TABLE} "
        "BEGIN SELECT RAISE(ABORT, 'fact selection ledger is append-only'); END"
    )
    op.execute(
        f"CREATE TRIGGER trg_{_TABLE}_append_only_delete BEFORE DELETE ON {_TABLE} "
        "BEGIN SELECT RAISE(ABORT, 'fact selection ledger is append-only'); END"
    )


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("decision_id", sa.String(length=128), primary_key=True),
        sa.Column("idempotency_key", sa.String(length=256), nullable=False, unique=True),
        sa.Column("target_table", sa.String(length=128), nullable=False),
        sa.Column("target_row_id", sa.Integer(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("selection_state", sa.String(length=16), nullable=False),
        sa.Column("reason_code", sa.String(length=128), nullable=False),
        sa.Column("reason_details_json", sa.Text(), nullable=False),
        sa.Column("decision_kind", sa.String(length=16), nullable=False),
        sa.Column("policy_name", sa.String(length=128), nullable=False),
        sa.Column("policy_version", sa.String(length=128), nullable=False),
        sa.Column("policy_config_sha256", sa.String(length=64), nullable=False),
        sa.Column(
            "evidence_node_id",
            sa.String(length=128),
            sa.ForeignKey("evidence_nodes.node_id"),
            nullable=True,
        ),
        sa.Column(
            "validation_issue_id",
            sa.Integer(),
            sa.ForeignKey("validation_issues.id"),
            nullable=True,
        ),
        sa.Column("effective_at", sa.DateTime(), nullable=False),
        sa.Column("knowledge_at", sa.DateTime(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(), nullable=False),
        sa.Column(
            "supersedes_decision_id",
            sa.String(length=128),
            sa.ForeignKey(f"{_TABLE}.decision_id"),
            nullable=True,
        ),
        sa.Column("material_dissent", sa.Boolean(), nullable=False),
        sa.UniqueConstraint(
            "target_table", "target_row_id", "revision", name="uq_fact_selection_revision"
        ),
        sa.CheckConstraint("target_table IN ('kpi_facts')", name="ck_fact_selection_target_table"),
        sa.CheckConstraint("target_row_id > 0", name="ck_fact_selection_target_row"),
        sa.CheckConstraint("revision > 0", name="ck_fact_selection_revision_positive"),
        sa.CheckConstraint(
            "selection_state IN ('included', 'excluded')", name="ck_fact_selection_state"
        ),
        sa.CheckConstraint(
            "decision_kind IN ('deterministic', 'manual', 'imported')",
            name="ck_fact_selection_kind",
        ),
        sa.CheckConstraint(
            "length(policy_config_sha256) = 64", name="ck_fact_selection_config_sha"
        ),
        sa.CheckConstraint(
            "knowledge_at >= effective_at AND recorded_at >= knowledge_at",
            name="ck_fact_selection_clock_order",
        ),
    )
    op.create_index(
        "ix_fact_selection_target_revision", _TABLE, ["target_table", "target_row_id", "revision"]
    )
    op.create_index("ix_fact_selection_evidence", _TABLE, ["evidence_node_id"])
    op.create_index("ix_fact_selection_validation_issue", _TABLE, ["validation_issue_id"])

    op.execute(
        "CREATE TRIGGER trg_fact_selection_revision_chain BEFORE INSERT ON "
        f"{_TABLE} WHEN NEW.revision = 1 AND NEW.supersedes_decision_id IS NOT NULL "
        "BEGIN SELECT RAISE(ABORT, 'first fact selection revision cannot supersede'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_fact_selection_revision_parent BEFORE INSERT ON "
        f"{_TABLE} WHEN NEW.revision > 1 AND (NEW.supersedes_decision_id IS NULL OR NOT EXISTS "
        f"(SELECT 1 FROM {_TABLE} WHERE decision_id = NEW.supersedes_decision_id "
        "AND target_table = NEW.target_table AND target_row_id = NEW.target_row_id "
        "AND revision = NEW.revision - 1)) "
        "BEGIN SELECT RAISE(ABORT, 'fact selection revision must supersede prior same-target revision'); END"
    )
    _append_only_triggers()
    op.execute(
        f"CREATE VIEW {_CURRENT_VIEW} AS "
        "SELECT decision_id, idempotency_key, target_table, target_row_id, revision, selection_state, "
        "reason_code, reason_details_json, decision_kind, policy_name, policy_version, "
        "policy_config_sha256, evidence_node_id, validation_issue_id, effective_at, knowledge_at, "
        "recorded_at, supersedes_decision_id, material_dissent "
        f"FROM {_TABLE} AS decision WHERE NOT EXISTS "
        f"(SELECT 1 FROM {_TABLE} AS newer WHERE newer.target_table = decision.target_table "
        "AND newer.target_row_id = decision.target_row_id AND newer.revision > decision.revision)"
    )


def downgrade() -> None:
    op.execute(f"DROP VIEW IF EXISTS {_CURRENT_VIEW}")
    for trigger in (
        "trg_fact_selection_revision_chain",
        "trg_fact_selection_revision_parent",
        f"trg_{_TABLE}_append_only",
        f"trg_{_TABLE}_append_only_delete",
    ):
        op.execute(f"DROP TRIGGER IF EXISTS {trigger}")
    op.drop_index("ix_fact_selection_validation_issue", table_name=_TABLE)
    op.drop_index("ix_fact_selection_evidence", table_name=_TABLE)
    op.drop_index("ix_fact_selection_target_revision", table_name=_TABLE)
    op.drop_table(_TABLE)
