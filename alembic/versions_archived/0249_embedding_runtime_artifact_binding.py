"""Bind embedding publications to exact local runtime artifacts.

Revision ID: 0249_embedding_runtime_artifact_binding
Revises: 0248_native_processing_closure_adapters
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0249_embedding_runtime_artifact_binding"
down_revision: str | Sequence[str] | None = "0248_native_processing_closure_adapters"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "search_embedding_model_promotions",
        sa.Column("runtime_artifact_json", sa.Text(), nullable=True),
    )
    op.add_column(
        "search_embedding_model_promotions",
        sa.Column("runtime_artifact_sha256", sa.String(64), nullable=True),
    )
    op.add_column(
        "search_embedding_artifacts",
        sa.Column("runtime_artifact_sha256", sa.String(64), nullable=True),
    )
    op.add_column(
        "search_projection_seals",
        sa.Column("runtime_artifact_sha256", sa.String(64), nullable=True),
    )

    op.execute(
        "CREATE TRIGGER trg_embedding_promotions_runtime_binding "
        "BEFORE INSERT ON search_embedding_model_promotions "
        "WHEN NEW.runtime_artifact_json IS NULL "
        "OR json_valid(NEW.runtime_artifact_json) <> 1 "
        "OR length(NEW.runtime_artifact_sha256) <> 64 "
        "OR NEW.runtime_artifact_sha256 GLOB '*[^0-9a-f]*' "
        "BEGIN SELECT RAISE(ABORT, 'new embedding promotion requires runtime artifact'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_embedding_artifacts_runtime_binding "
        "BEFORE INSERT ON search_embedding_artifacts "
        "WHEN NEW.outcome = 'succeeded' AND ("
        "length(NEW.runtime_artifact_sha256) <> 64 "
        "OR NEW.runtime_artifact_sha256 GLOB '*[^0-9a-f]*' "
        "OR NOT EXISTS (SELECT 1 FROM search_embedding_model_promotions AS promotion "
        "WHERE promotion.provider = NEW.provider AND promotion.model = NEW.model "
        "AND promotion.dimensions = NEW.dimensions "
        "AND promotion.runtime_artifact_sha256 = NEW.runtime_artifact_sha256)) "
        "BEGIN SELECT RAISE(ABORT, 'successful embedding requires promoted runtime artifact'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_vector_seals_runtime_binding "
        "BEFORE INSERT ON search_projection_seals "
        "WHEN NEW.index_kind = 'vector' AND ("
        "length(NEW.runtime_artifact_sha256) <> 64 "
        "OR NEW.runtime_artifact_sha256 GLOB '*[^0-9a-f]*' "
        "OR NOT EXISTS (SELECT 1 FROM search_embedding_model_promotions AS promotion "
        "WHERE promotion.provider = NEW.provider AND promotion.model = NEW.model "
        "AND promotion.dimensions = NEW.dimensions "
        "AND promotion.runtime_artifact_sha256 = NEW.runtime_artifact_sha256) "
        "OR NOT EXISTS (SELECT 1 FROM search_embedding_artifacts AS artifact "
        "WHERE artifact.index_run_id = NEW.index_run_id "
        "AND artifact.outcome = 'succeeded') "
        "OR EXISTS (SELECT 1 FROM search_embedding_artifacts AS artifact "
        "WHERE artifact.index_run_id = NEW.index_run_id AND artifact.outcome = 'succeeded' "
        "AND (artifact.runtime_artifact_sha256 IS NULL "
        "OR artifact.runtime_artifact_sha256 <> NEW.runtime_artifact_sha256))) "
        "BEGIN SELECT RAISE(ABORT, 'vector seal requires one promoted runtime artifact'); END"
    )
    for table, columns in (
        (
            "search_embedding_model_promotions",
            "runtime_artifact_json, runtime_artifact_sha256",
        ),
        ("search_embedding_artifacts", "runtime_artifact_sha256"),
        ("search_projection_seals", "runtime_artifact_sha256"),
    ):
        op.execute(
            f"CREATE TRIGGER trg_{table}_runtime_immutable "
            f"BEFORE UPDATE OF {columns} ON {table} "
            "BEGIN SELECT RAISE(ABORT, 'embedding runtime artifact binding is immutable'); END"
        )


def downgrade() -> None:
    for table in (
        "search_projection_seals",
        "search_embedding_artifacts",
        "search_embedding_model_promotions",
    ):
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_runtime_immutable")
    op.execute("DROP TRIGGER IF EXISTS trg_vector_seals_runtime_binding")
    op.execute("DROP TRIGGER IF EXISTS trg_embedding_artifacts_runtime_binding")
    op.execute("DROP TRIGGER IF EXISTS trg_embedding_promotions_runtime_binding")
    # DROP COLUMN reparses every trigger in a SQLite schema. Supported legacy
    # fixture schemas can intentionally omit columns referenced by later guard
    # triggers, so preserve/drop/recreate triggers around this exact reversal.
    bind = op.get_bind()
    trigger_rows = bind.execute(
        sa.text(
            "SELECT name, sql FROM sqlite_master "
            "WHERE type='trigger' AND sql IS NOT NULL ORDER BY name"
        )
    ).fetchall()
    triggers = [(str(row[0]), str(row[1])) for row in trigger_rows]
    for name, _sql in triggers:
        escaped = name.replace('"', '""')
        op.execute(f'DROP TRIGGER "{escaped}"')
    try:
        op.drop_column("search_projection_seals", "runtime_artifact_sha256")
        op.drop_column("search_embedding_artifacts", "runtime_artifact_sha256")
        op.drop_column("search_embedding_model_promotions", "runtime_artifact_sha256")
        op.drop_column("search_embedding_model_promotions", "runtime_artifact_json")
    finally:
        for _name, sql in triggers:
            op.execute(sql)
