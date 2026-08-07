"""qualitative_conditions — non-numeric falsifiable conditions on decisions (L9 PR2).

The numeric ``decisions.decision_conditions`` (0086) only ever held conditions a
future REPORTED NUMBER could satisfy — the extraction prompt deliberately skips
"purely qualitative triggers that carry no number" ("the CEO departs", "a
credible competitor enters the market"), and the evaluator hard-skips any
condition whose metric_source isn't kpi/financial. So a qualitative "what would
change my mind" never fired: it waited on next quarter's XBRL, which for a
qualitative event never comes.

This adds a SEPARATE column for those conditions, populated by its own
``qualitative_conditions_extract`` pass (kept apart from the numeric path so the
two never collide). They are routed to the existing material_news /
earnings_tone triggers — surfaced as an annotation when news or a tone shift
lands for the name — so they fire on NEWS, not on a number.

Two nullable columns on ``decisions``:
  * ``qualitative_conditions`` — JSON array of {phrase, watch_for, note}; '[]'
    is "extracted, none found" (distinct from NULL = not yet extracted).
  * ``qualitative_conditions_extracted_at`` — naive-UTC stamp; the attach rung
    walks NULLs, so it is independent of the numeric ``conditions_extracted_at``.

Revision ID: 0109_qualitative_conditions
Revises: 0108_restatement_trigger_kind
Create Date: 2026-06-14
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0109_qualitative_conditions"
down_revision: str | Sequence[str] | None = "0108_restatement_trigger_kind"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _columns(bind: object) -> set[str]:
    insp = sa.inspect(bind)
    if "decisions" not in insp.get_table_names():
        return set()
    return {c["name"] for c in insp.get_columns("decisions")}


def upgrade() -> None:
    bind = op.get_bind()
    cols = _columns(bind)
    if not cols:
        return  # no decisions table yet (pre-0083) — nothing to extend
    if "qualitative_conditions" not in cols:
        op.add_column("decisions", sa.Column("qualitative_conditions", sa.Text(), nullable=True))
    if "qualitative_conditions_extracted_at" not in cols:
        op.add_column(
            "decisions",
            sa.Column("qualitative_conditions_extracted_at", sa.DateTime(), nullable=True),
        )


def downgrade() -> None:
    bind = op.get_bind()
    cols = _columns(bind)
    if "qualitative_conditions_extracted_at" in cols:
        op.drop_column("decisions", "qualitative_conditions_extracted_at")
    if "qualitative_conditions" in cols:
        op.drop_column("decisions", "qualitative_conditions")
