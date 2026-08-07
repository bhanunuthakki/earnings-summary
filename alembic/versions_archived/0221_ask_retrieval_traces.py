"""Record immutable grounded-Ask retrieval and answer lineage."""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0221_ask_retrieval_traces"
down_revision: str | Sequence[str] | None = "0220_source_inventory_seals"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES = (
    "ask_retrieval_traces",
    "ask_retrieval_trace_items",
    "ask_answer_groundings",
)


def _append_only(table: str) -> None:
    op.execute(
        f"CREATE TRIGGER trg_{table}_append_only BEFORE UPDATE ON {table} "
        "BEGIN SELECT RAISE(ABORT, 'Ask grounding lineage is append-only'); END"
    )
    op.execute(
        f"CREATE TRIGGER trg_{table}_append_only_delete BEFORE DELETE ON {table} "
        "BEGIN SELECT RAISE(ABORT, 'Ask grounding lineage is append-only'); END"
    )


def upgrade() -> None:
    op.create_table(
        "ask_retrieval_traces",
        sa.Column("trace_id", sa.String(128), primary_key=True),
        sa.Column("idempotency_key", sa.String(256), nullable=False, unique=True),
        sa.Column("question_sha256", sa.String(64), nullable=False),
        sa.Column("scope_sha256", sa.String(64), nullable=False),
        sa.Column("retrieval_config_sha256", sa.String(64), nullable=False),
        sa.Column("outcome", sa.String(32), nullable=False),
        sa.Column("reason_code", sa.String(128), nullable=False),
        sa.Column("manifest_ids_json", sa.Text(), nullable=False),
        sa.Column("filters_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "outcome IN ('ready', 'coverage_incomplete', 'unavailable')",
            name="ck_ask_retrieval_trace_outcome",
        ),
        sa.CheckConstraint(
            "length(question_sha256) = 64 AND length(scope_sha256) = 64 "
            "AND length(retrieval_config_sha256) = 64",
            name="ck_ask_retrieval_trace_hashes",
        ),
    )
    op.create_table(
        "ask_retrieval_trace_items",
        sa.Column(
            "trace_id",
            sa.String(128),
            sa.ForeignKey("ask_retrieval_traces.trace_id"),
            primary_key=True,
        ),
        sa.Column("rank", sa.Integer(), primary_key=True),
        sa.Column(
            "manifest_id",
            sa.String(128),
            sa.ForeignKey("search_corpus_manifests.manifest_id"),
            nullable=False,
        ),
        sa.Column(
            "chunk_id",
            sa.String(128),
            sa.ForeignKey("search_chunks.chunk_id"),
            nullable=False,
        ),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column("bundle_sha256", sa.String(64), nullable=False),
        sa.Column("recorded_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint(
            "trace_id", "manifest_id", "chunk_id", name="uq_ask_trace_chunk"
        ),
        sa.CheckConstraint("rank > 0", name="ck_ask_retrieval_item_rank"),
        sa.CheckConstraint(
            "length(bundle_sha256) = 64", name="ck_ask_retrieval_item_hash"
        ),
    )
    op.create_table(
        "ask_answer_groundings",
        sa.Column("grounding_id", sa.String(128), primary_key=True),
        sa.Column("idempotency_key", sa.String(256), nullable=False, unique=True),
        sa.Column(
            "trace_id",
            sa.String(128),
            sa.ForeignKey("ask_retrieval_traces.trace_id"),
            nullable=False,
        ),
        sa.Column("prompt_sha256", sa.String(64), nullable=False),
        sa.Column("answer_sha256", sa.String(64), nullable=False),
        sa.Column("llm_call_id", sa.String(128), nullable=True),
        sa.Column("recorded_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "length(prompt_sha256) = 64 AND length(answer_sha256) = 64",
            name="ck_ask_answer_grounding_hashes",
        ),
    )
    op.create_index(
        "ix_ask_answer_grounding_trace",
        "ask_answer_groundings",
        ["trace_id", "recorded_at"],
    )
    for table in _TABLES:
        _append_only(table)


def downgrade() -> None:
    for table in reversed(_TABLES):
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_append_only")
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_append_only_delete")
    op.drop_index("ix_ask_answer_grounding_trace", table_name="ask_answer_groundings")
    for table in reversed(_TABLES):
        op.drop_table(table)
