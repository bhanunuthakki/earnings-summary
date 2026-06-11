"""saved_views — persisted ViewSpec pivots (master build P5.1).

One row per saved slice-and-dice view: a name plus the ViewSpec JSON
(metrics x tickers x period x transform) that re-runs it deterministically
against financial_facts / kpi_facts / the segment junction. The spec is
stored verbatim as JSON — the engine (src/viewspec) owns the schema and
re-validates on load, so a spec written by a newer engine version degrades
to a validation error, never a crash.

Saving under an existing (user_id, name) replaces that view's spec (the
write path uses ON CONFLICT DO UPDATE) — names are how the owner refers to
views from the cockpit/report embed hooks, so they stay stable while the
spec evolves.

New-table CHECKs per 0071's policy.

Revision ID: 0079_saved_views
Revises: 0078_stance_scores
Create Date: 2026-06-11
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0079_saved_views"
down_revision: str | Sequence[str] | None = "0078_stance_scores"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "saved_views" in insp.get_table_names():
        return  # idempotent
    op.create_table(
        "saved_views",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "user_id",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'bhanu'"),
        ),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("spec_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.CheckConstraint("length(trim(name)) > 0", name="ck_saved_views_name_nonempty"),
        sa.CheckConstraint("json_valid(spec_json)", name="ck_saved_views_spec_json"),
        sa.UniqueConstraint("user_id", "name", name="uq_saved_views_user_name"),
    )
    op.create_index("ix_saved_views_user", "saved_views", ["user_id"])


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "saved_views" not in insp.get_table_names():
        return
    op.drop_index("ix_saved_views_user", table_name="saved_views")
    op.drop_table("saved_views")
