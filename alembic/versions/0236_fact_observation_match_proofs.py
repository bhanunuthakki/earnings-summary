"""Bridge exact fact observations to accepted evidence-match revisions.

Revision ID: 0236_fact_observation_match_proofs
Revises: 0235_legacy_fact_evidence_matches

This additive schema assumes the hardened columns introduced by 0235.  It
deliberately depends only on that migration's stable match identity, fact
scope, outcome, evidence node, binding-current view, and clocks; no optional
candidate-detail columns are required.  Capture triggers and canonical reader
views remain unchanged.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0236_fact_observation_match_proofs"
down_revision: str | Sequence[str] | None = "0235_legacy_fact_evidence_matches"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "fact_observation_match_proofs"
_VIEW = "v_fact_observation_match_proofs_current_valid"


def _exact_replay_predicate(alias: str = "prior") -> str:
    return (
        f"{alias}.proof_id IS NEW.proof_id "
        f"AND {alias}.idempotency_key IS NEW.idempotency_key "
        f"AND {alias}.observation_id IS NEW.observation_id "
        f"AND {alias}.match_revision_id IS NEW.match_revision_id "
        f"AND {alias}.fact_table IS NEW.fact_table "
        f"AND {alias}.fact_row_id IS NEW.fact_row_id "
        f"AND {alias}.fact_revision IS NEW.fact_revision "
        f"AND {alias}.effective_at IS NEW.effective_at "
        f"AND {alias}.knowledge_at IS NEW.knowledge_at "
        f"AND {alias}.recorded_at IS NEW.recorded_at"
    )


def upgrade() -> None:
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())
    required = {
        "fact_observation_revisions",
        "legacy_fact_evidence_match_revisions",
        "reported_observations",
    }
    missing = sorted(required - existing)
    if missing:
        raise RuntimeError(
            "fact observation match proofs require hardened predecessor tables: "
            + ", ".join(missing)
        )

    op.create_table(
        _TABLE,
        sa.Column("proof_id", sa.String(128), primary_key=True),
        sa.Column("idempotency_key", sa.String(256), nullable=False, unique=True),
        sa.Column(
            "observation_id",
            sa.String(128),
            sa.ForeignKey("reported_observations.observation_id"),
            nullable=False,
        ),
        sa.Column(
            "match_revision_id",
            sa.String(128),
            sa.ForeignKey("legacy_fact_evidence_match_revisions.match_revision_id"),
            nullable=False,
        ),
        sa.Column("fact_table", sa.String(32), nullable=False),
        sa.Column("fact_row_id", sa.Integer(), nullable=False),
        sa.Column("fact_revision", sa.Integer(), nullable=False),
        sa.Column("effective_at", sa.DateTime(), nullable=False),
        sa.Column("knowledge_at", sa.DateTime(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint(
            "observation_id",
            "match_revision_id",
            name="uq_fact_observation_match_proof_pair",
        ),
        sa.CheckConstraint(
            "fact_table IN ('financial_facts', 'kpi_facts')",
            name="ck_fact_observation_match_proof_fact_table",
        ),
        sa.CheckConstraint(
            "fact_row_id > 0 AND fact_revision > 0",
            name="ck_fact_observation_match_proof_positive_ids",
        ),
        sa.CheckConstraint(
            "knowledge_at >= effective_at AND recorded_at >= knowledge_at",
            name="ck_fact_observation_match_proof_clocks",
        ),
    )
    op.create_index(
        "ix_fact_observation_match_proof_fact",
        _TABLE,
        ["fact_table", "fact_row_id", "fact_revision"],
    )
    op.create_index(
        "ix_fact_observation_match_proof_match",
        _TABLE,
        ["match_revision_id"],
    )

    # One ordered trigger makes exact replay a no-op before any current-state
    # validation.  This keeps a historical proof replayable even after its
    # binding has advanced, while rejecting any divergent idempotency reuse.
    op.execute(
        f"CREATE TRIGGER trg_{_TABLE}_validate BEFORE INSERT ON {_TABLE} BEGIN "
        "SELECT CASE WHEN EXISTS ("
        f"SELECT 1 FROM {_TABLE} AS prior "
        "WHERE prior.idempotency_key = NEW.idempotency_key "
        f"AND {_exact_replay_predicate()}"
        ") THEN RAISE(IGNORE) END; "
        "SELECT CASE WHEN EXISTS ("
        f"SELECT 1 FROM {_TABLE} AS prior "
        "WHERE prior.idempotency_key = NEW.idempotency_key"
        ") THEN RAISE(ABORT, "
        "'fact observation match proof idempotency conflict') END; "
        "SELECT CASE WHEN NOT EXISTS ("
        "SELECT 1 FROM fact_observation_revisions AS link "
        "WHERE link.fact_table = NEW.fact_table "
        "AND link.fact_row_id = NEW.fact_row_id "
        "AND link.fact_revision = NEW.fact_revision "
        "AND link.observation_id = NEW.observation_id"
        ") THEN RAISE(ABORT, "
        "'proof requires the exact fact observation revision') END; "
        "SELECT CASE WHEN NOT EXISTS ("
        "SELECT 1 FROM legacy_fact_evidence_match_revisions AS match "
        "WHERE match.match_revision_id = NEW.match_revision_id "
        "AND match.fact_table = NEW.fact_table "
        "AND match.fact_row_id = NEW.fact_row_id"
        ") THEN RAISE(ABORT, "
        "'proof match must agree with the fact row') END; "
        "SELECT CASE WHEN NOT EXISTS ("
        "SELECT 1 FROM v_legacy_fact_evidence_matches_accepted_current AS match "
        "WHERE match.match_revision_id = NEW.match_revision_id "
        "AND match.fact_table = NEW.fact_table "
        "AND match.fact_row_id = NEW.fact_row_id"
        ") THEN RAISE(ABORT, "
        "'proof requires an accepted match on its current exact binding') END; "
        "SELECT CASE WHEN NOT EXISTS ("
        "SELECT 1 FROM reported_observations AS observation "
        "JOIN legacy_fact_evidence_match_revisions AS match "
        "ON match.match_revision_id = NEW.match_revision_id "
        "WHERE observation.observation_id = NEW.observation_id "
        "AND observation.evidence_node_id = match.evidence_node_id"
        ") THEN RAISE(ABORT, "
        "'proof observation and match evidence nodes must agree') END; "
        "SELECT CASE WHEN NOT EXISTS ("
        "SELECT 1 FROM reported_observations AS observation "
        "JOIN legacy_fact_evidence_match_revisions AS match "
        "ON match.match_revision_id = NEW.match_revision_id "
        "WHERE observation.observation_id = NEW.observation_id "
        "AND NEW.knowledge_at >= observation.available_at "
        "AND NEW.knowledge_at >= match.knowledge_at"
        ") THEN RAISE(ABORT, "
        "'proof knowledge clock precedes its observation or match') END; "
        "END"
    )
    op.execute(
        f"CREATE TRIGGER trg_{_TABLE}_append_only BEFORE UPDATE ON {_TABLE} "
        "BEGIN SELECT RAISE(ABORT, "
        "'fact observation match proof is append-only'); END"
    )
    op.execute(
        f"CREATE TRIGGER trg_{_TABLE}_append_only_delete BEFORE DELETE ON {_TABLE} "
        "BEGIN SELECT RAISE(ABORT, "
        "'fact observation match proof is append-only'); END"
    )

    op.execute(
        f"CREATE VIEW {_VIEW} AS "
        "SELECT proof.* "
        f"FROM {_TABLE} AS proof "
        "JOIN fact_observation_revisions AS link "
        "ON link.fact_table = proof.fact_table "
        "AND link.fact_row_id = proof.fact_row_id "
        "AND link.fact_revision = proof.fact_revision "
        "AND link.observation_id = proof.observation_id "
        "JOIN reported_observations AS observation "
        "ON observation.observation_id = proof.observation_id "
        "JOIN v_legacy_fact_evidence_matches_accepted_current AS match "
        "ON match.match_revision_id = proof.match_revision_id "
        "AND match.fact_table = proof.fact_table "
        "AND match.fact_row_id = proof.fact_row_id "
        "AND match.evidence_node_id = observation.evidence_node_id "
        "WHERE proof.knowledge_at >= observation.available_at "
        "AND proof.knowledge_at >= match.knowledge_at"
    )


def downgrade() -> None:
    op.execute(f"DROP VIEW IF EXISTS {_VIEW}")
    for trigger in (
        f"trg_{_TABLE}_validate",
        f"trg_{_TABLE}_append_only",
        f"trg_{_TABLE}_append_only_delete",
    ):
        op.execute(f"DROP TRIGGER IF EXISTS {trigger}")
    op.drop_index("ix_fact_observation_match_proof_match", table_name=_TABLE)
    op.drop_index("ix_fact_observation_match_proof_fact", table_name=_TABLE)
    op.drop_table(_TABLE)
