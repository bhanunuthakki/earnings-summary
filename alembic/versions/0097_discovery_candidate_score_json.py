"""discovery_candidates.score_json — the per-class "why this rank" breakdown.

The weighted scorer (``src/discovery/scoring.py``, replacing 0081's
equal-weight count) emits a structured ``score_why``: the total, the
per-signal-class subtotals, the fundamental-vs-investor split, whether the
investor term was clamped, the corroboration multiplier, and the per-signal
contributions. The panel renders a one-line evidence summary inline and the
full breakdown behind a peek. We persist it next to the candidate so the UI
reads it without recomputing.

Nullable (a pre-scoring or legacy row simply has none) with a tolerant
``json_valid`` CHECK that explicitly allows NULL. SQLite ADD COLUMN supports a
column-level CHECK in one statement (verified on 3.50.4), so no table rebuild
is needed; downgrade drops the column via batch (the SQLite-portable path).

Revision ID: 0097_discovery_candidate_score_json
Revises: 0096_discovery_signals
Create Date: 2026-06-13
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0097_discovery_candidate_score_json"
down_revision: str | Sequence[str] | None = "0096_discovery_signals"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _columns(insp: sa.Inspector, table: str) -> set[str]:
    return {c["name"] for c in insp.get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "discovery_candidates" not in insp.get_table_names():
        return  # nothing to extend
    if "score_json" in _columns(insp, "discovery_candidates"):
        return  # idempotent
    # One statement: SQLite ADD COLUMN accepts a column-level CHECK, and the
    # NULL default passes it (json_valid(NULL) is not true), so existing rows
    # are untouched without a table rebuild.
    op.execute(
        "ALTER TABLE discovery_candidates ADD COLUMN score_json TEXT "
        "CHECK (score_json IS NULL OR json_valid(score_json))"
    )


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "discovery_candidates" not in insp.get_table_names():
        return
    if "score_json" not in _columns(insp, "discovery_candidates"):
        return
    with op.batch_alter_table("discovery_candidates") as batch:
        batch.drop_column("score_json")
