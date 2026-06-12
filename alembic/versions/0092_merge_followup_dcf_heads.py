"""Merge the two 0091 heads into a single linear head.

Two parallel Wave-2 sessions each added a migration off
``0090_ask_claim_grounding_budget`` and merged in the same window, leaving
the revision tree with two heads:

  * ``0091_ask_evidence_followup_budget`` (S7 — ask evidence loop budget)
  * ``0091_dcf_runs_assumptions_sync`` (S11 PR2 — dcf_runs assumptions sync)

Both are independent and additive (a budget-row INSERT and a dcf_runs
column/status change), so they compose without ordering constraints. This
is a no-op merge revision whose only job is to rejoin them — without it,
``alembic upgrade head`` fails with "Multiple head revisions are present".

Revision ID: 0092_merge_followup_dcf_heads
Revises: 0091_ask_evidence_followup_budget, 0091_dcf_runs_assumptions_sync
Create Date: 2026-06-12
"""

from __future__ import annotations

from collections.abc import Sequence

revision: str = "0092_merge_followup_dcf_heads"
down_revision: str | Sequence[str] | None = (
    "0091_ask_evidence_followup_budget",
    "0091_dcf_runs_assumptions_sync",
)
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """No-op: the two parents are independent; this only rejoins the tree."""


def downgrade() -> None:
    """No-op: splitting back into two heads needs no schema change."""
