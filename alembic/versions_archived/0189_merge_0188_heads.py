"""Merge the two parallel 0188 heads (no-op DDL).

Two sessions merged migrations numbered 0188 off the same 0187 parent within
the same hour — ``0188_coach_reply_capture`` (#954, B3) and
``0188_decisions_advice_artifact`` (#958, P0.4a) — leaving the repo with two
alembic heads and ``upgrade head`` refusing to run. Prod was already stamped
at ``0188_decisions_advice_artifact`` before the collision was noticed, so
renumbering either file would orphan a live stamp; the standard alembic merge
revision is the only safe join. Upgrading from either side applies the other
side's pending operations and lands here.

Numbering note: the next linear migration should chain off THIS revision
(down_revision = "0189_merge_0188_heads") — and per the parallel-sessions
rule, pick your number at rebase/push time, not at authoring time.

Revision ID: 0189_merge_0188_heads
Revises: 0188_coach_reply_capture, 0188_decisions_advice_artifact
Create Date: 2026-07-23
"""

from __future__ import annotations

from collections.abc import Sequence

revision: str = "0189_merge_0188_heads"
down_revision: str | Sequence[str] | None = (
    "0188_coach_reply_capture",
    "0188_decisions_advice_artifact",
)
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Merge point only — no DDL."""


def downgrade() -> None:
    """Merge point only — no DDL."""
