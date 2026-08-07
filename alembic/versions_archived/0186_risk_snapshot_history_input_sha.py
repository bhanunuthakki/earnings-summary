"""portfolio_risk_snapshot_history.input_sha — content-hash idempotency.

Personal Investment Partner PRD §7.1 (P0-A, ratified 2026-07-23): re-running
the risk refresh on the same analytics input must not duplicate a history
row. `write_snapshot` computes a sha256 over the metric content and the
history append becomes INSERT OR IGNORE against the unique (user_id,
input_sha) index. Legacy rows keep NULL (SQLite unique indexes treat NULLs
as distinct, so pre-0186 history is untouched).

Revision ID: 0186_risk_snapshot_history_input_sha
Revises: 0185_portfolio_risk_snapshot_history
Create Date: 2026-07-23
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0186_risk_snapshot_history_input_sha"
down_revision: str | Sequence[str] | None = "0185_portfolio_risk_snapshot_history"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "portfolio_risk_snapshot_history"
_INDEX = "uq_risk_snapshot_history_user_sha"


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if _TABLE not in insp.get_table_names():
        return  # pre-0185 DB; 0185 creates the table on its own pass
    cols = {c["name"] for c in insp.get_columns(_TABLE)}
    if "input_sha" not in cols:
        op.add_column(_TABLE, sa.Column("input_sha", sa.Text(), nullable=True))
    index_names = {ix["name"] for ix in insp.get_indexes(_TABLE)}
    if _INDEX not in index_names:
        op.create_index(_INDEX, _TABLE, ["user_id", "input_sha"], unique=True)


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if _TABLE not in insp.get_table_names():
        return
    index_names = {ix["name"] for ix in insp.get_indexes(_TABLE)}
    if _INDEX in index_names:
        op.drop_index(_INDEX, table_name=_TABLE)
    cols = {c["name"] for c in insp.get_columns(_TABLE)}
    if "input_sha" in cols:
        op.drop_column(_TABLE, "input_sha")
