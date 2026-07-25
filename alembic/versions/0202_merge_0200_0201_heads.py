"""Merge the two parallel 0199-children heads (no-op DDL).

Two sessions merged migrations off the same ``0199_risk_snapshot_provenance``
parent within the same hour — ``0200_prompt_ab_arms`` (#1005, the randomized
prompt-quality loop) and ``0201_senior_partner_brief_budget`` (#1002, PRD
§9.1 P2.2) — leaving the repo with two alembic heads. Every test fixture that
builds a DB via ``upgrade head`` then fails at setup, which is why a routine
full-suite run reported 940 errors across otherwise-untouched suites: the
failure was the migration graph, not the tests.

Renumbering is NOT safe here. Prod is already stamped at
``0201_senior_partner_brief_budget`` (its DDL applied 2026-07-25), so
re-pointing 0201's ``down_revision`` at 0200 would leave prod's stamp
claiming a lineage whose 0200 DDL never ran — the tables that revision
creates would be silently absent while alembic reported the DB up to date.
The standard merge revision is the only join that keeps a live stamp honest:
upgrading from either side applies the other side's pending operations and
lands here. On prod specifically, ``upgrade head`` from 0201 will now apply
``0200_prompt_ab_arms``'s DDL and then this no-op merge point.

This is the second head collision of this shape (see
``0189_merge_0188_heads``). The durable lesson both times: an ``alembic
heads`` check immediately before push is necessary but NOT sufficient, because
another session can merge in the window between that check and yours. Only a
re-check at MERGE time closes it.

Numbering note: the next linear migration should chain off THIS revision
(``down_revision = "0202_merge_0200_0201_heads"``) — and per the
parallel-sessions rule, pick the number at merge time, not at authoring time.

Revision ID: 0202_merge_0200_0201_heads
Revises: 0200_prompt_ab_arms, 0201_senior_partner_brief_budget
Create Date: 2026-07-25
"""

from __future__ import annotations

from collections.abc import Sequence

revision: str = "0202_merge_0200_0201_heads"
down_revision: str | Sequence[str] | None = (
    "0200_prompt_ab_arms",
    "0201_senior_partner_brief_budget",
)
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Merge point only — no DDL."""


def downgrade() -> None:
    """Merge point only — no DDL."""
