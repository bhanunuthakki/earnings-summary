"""decisions.process_quality — process score distinct from outcome (Track B 8).

The decisions ledger graded only the OUTCOME (correct / wrong / mixed). But a
right call can be right for the wrong reasons (``lucky``) and a wrong call can be
soundly reasoned (``sound``) — the process and the outcome are different axes,
and a calibration loop that conflates them rewards luck and punishes discipline.

This adds a first-class ``process_quality`` column (NULL | 'sound' | 'flawed' |
'lucky') so the two can be crossed into a process-by-outcome matrix on the
scorecard. Nullable, no CHECK (the write path
``decision_extractor.record_process_quality`` is the validation gate, matching
the column's siblings ``outcome_label`` / ``conviction``). Additive — existing
rows read NULL ("not yet scored").

Revision ID: 0114_decision_process_quality
Revises: 0113_kpi_definition_origin
Create Date: 2026-06-26
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0114_decision_process_quality"
down_revision: str | Sequence[str] | None = "0113_kpi_definition_origin"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _columns(insp: sa.Inspector) -> set[str]:
    if "decisions" not in insp.get_table_names():
        return set()
    return {c["name"] for c in insp.get_columns("decisions")}


def upgrade() -> None:
    bind = op.get_bind()
    cols = _columns(sa.inspect(bind))
    if not cols:
        return  # no decisions table yet (pre-0046) — nothing to extend
    if "process_quality" not in cols:
        with op.batch_alter_table("decisions") as batch:
            batch.add_column(sa.Column("process_quality", sa.String(length=16), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    cols = _columns(sa.inspect(bind))
    if "process_quality" in cols:
        with op.batch_alter_table("decisions") as batch:
            batch.drop_column("process_quality")
