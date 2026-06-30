"""Widen analyst_notes kind CHECK to admit 'influence'.

An influence is a document, deck, or link the analyst shared via Telegram
(or any capture channel) that shapes their investing mental model.  Unlike
a musing (stream-of-consciousness thought) an influence is something the
analyst *found* and flagged as worth remembering.

SQLite cannot ALTER a CHECK, so batch_alter_table recreates the table with
the widened constraint.  Mirrors the pattern established by 0115.

Revision ID: 0125_investor_influences_kind
Revises: 0124_research_loop_budgets
Create Date: 2026-06-30
"""

from __future__ import annotations

import contextlib
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0125_investor_influences_kind"
down_revision: str | Sequence[str] | None = "0124_research_loop_budgets"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_KINDS_WIDENED = (
    "('question', 'decision', 'watch', 'assumption', 'observation', 'musing', 'influence')"
)
_KINDS_PRIOR = "('question', 'decision', 'watch', 'assumption', 'observation', 'musing')"


def _has_table(insp: sa.Inspector, name: str) -> bool:
    return name in insp.get_table_names()


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if not _has_table(insp, "analyst_notes"):
        return
    with op.batch_alter_table("analyst_notes") as batch:
        with contextlib.suppress(ValueError, sa.exc.OperationalError):
            batch.drop_constraint("ck_analyst_notes_kind", type_="check")
        batch.create_check_constraint("ck_analyst_notes_kind", f"kind IN {_KINDS_WIDENED}")


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if not _has_table(insp, "analyst_notes"):
        return
    has_influence = bind.execute(
        sa.text("SELECT COUNT(*) FROM analyst_notes WHERE kind = 'influence'")
    ).scalar()
    with op.batch_alter_table("analyst_notes") as batch:
        if not has_influence:
            with contextlib.suppress(ValueError, sa.exc.OperationalError):
                batch.drop_constraint("ck_analyst_notes_kind", type_="check")
            batch.create_check_constraint("ck_analyst_notes_kind", f"kind IN {_KINDS_PRIOR}")
