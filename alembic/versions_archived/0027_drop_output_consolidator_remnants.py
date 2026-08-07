"""drop_output_consolidator_remnants — DROP output_artifacts + step_pdf_generated.

The output_consolidator subsystem (HTML dashboards, per-ticker memo/tracker/master
copies into output/research/) was deleted alongside its upstreams in Phase 1 (PR
#40). The schema it wrote into — `output_artifacts` table and the
`quarterly_artifacts.step_pdf_generated` column — was left in place. Both are now
write-nowhere/read-nowhere: this migration drops them so the schema matches the
code.

Both objects were created by src/db.py:init_db() at module import-time, never by
an earlier alembic migration — so this revision uses `IF EXISTS` and a PRAGMA
check to stay safe on DBs that were initialized through alembic alone and never
imported `db`.

Revision ID: 0027_drop_output_consolidator_remnants
Revises: 0026_brief_dirty_triggers
Create Date: 2026-05-10
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0027_drop_output_consolidator_remnants"
down_revision: str | Sequence[str] | None = "0026_brief_dirty_triggers"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("DROP TABLE IF EXISTS output_artifacts")

    bind = op.get_bind()
    cols = {row[1] for row in bind.execute(sa.text("PRAGMA table_info(quarterly_artifacts)"))}
    if "step_pdf_generated" in cols:
        with op.batch_alter_table("quarterly_artifacts") as batch:
            batch.drop_column("step_pdf_generated")


def downgrade() -> None:
    with op.batch_alter_table("quarterly_artifacts") as batch:
        batch.add_column(sa.Column("step_pdf_generated", sa.Boolean(), server_default="0"))

    op.create_table(
        "output_artifacts",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("ticker", sa.Text(), nullable=False),
        sa.Column(
            "kind",
            sa.Text(),
            sa.CheckConstraint(
                "kind IN ('memo','thesis_tracker','master_pdf','ticker_index','portfolio_index')"
            ),
            nullable=False,
        ),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("generated_at", sa.TIMESTAMP(), server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("is_latest", sa.Integer(), server_default="1"),
        sa.UniqueConstraint("ticker", "kind", "path"),
    )
    op.create_index(
        "idx_output_artifacts_latest",
        "output_artifacts",
        ["ticker", "kind", "is_latest"],
    )
