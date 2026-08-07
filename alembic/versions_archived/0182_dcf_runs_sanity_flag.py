"""dcf_runs gains a sanity_flag column — the DCF trust gate (program review 2026-07-19).

The 2026-07-19 adversarial review found 24 is_latest rows with |over_under_pct| > 0.6
(NBIS +11.7, NTRA +7.55, …) feeding 20+ consumer surfaces and LLM lenses with no gate,
plus a TWD-as-USD unit defect (TSM) the missing-FX class of which is now fail-loud at
build time. This column is the persistence half of the gate: ``src/dcf/persist.py``
stamps ``sanity_flag='outlier'`` at the single write chokepoint whenever the derived
|over_under_pct| exceeds ``SANITY_OVER_UNDER_LIMIT`` (0.6), and consumer surfaces render
an explicit "unreviewed model" badge (never a silent drop) while LLM lenses withhold the
fair value.

Additive, nullable TEXT. NULL = model within sanity bounds (or predates the gate).
Backfill stamps every existing row past the limit — history rows included, so a
superseded outlier reads as what it was.

Revision ID: 0182_dcf_runs_sanity_flag
Revises: 0181_fact_overrides_locator
Create Date: 2026-07-19
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0182_dcf_runs_sanity_flag"
down_revision: str | Sequence[str] | None = "0181_fact_overrides_locator"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Mirror of dcf.persist.SANITY_OVER_UNDER_LIMIT at migration time (a migration must
# not import application code; the live stamping in persist.py is the source of truth).
_LIMIT = 0.6


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "dcf_runs" not in insp.get_table_names():
        return  # pre-0013 DB; nothing to gate
    existing_cols = {c["name"] for c in insp.get_columns("dcf_runs")}
    if "sanity_flag" in existing_cols:
        return  # idempotent
    # Plain ADD COLUMN (nullable, no default) — no batch rebuild, no triggers on
    # dcf_runs to disturb.
    op.add_column("dcf_runs", sa.Column("sanity_flag", sa.Text(), nullable=True))
    bind.execute(
        sa.text(
            "UPDATE dcf_runs SET sanity_flag = 'outlier' "
            "WHERE over_under_pct IS NOT NULL AND ABS(over_under_pct) > :lim"
        ),
        {"lim": _LIMIT},
    )


def downgrade() -> None:
    # Deliberate no-op. Dropping the column needs a batch_alter_table rebuild,
    # and SQLite refuses to rebuild a table that dependent VIEWS reference
    # (v_decision_freshness, 0137 lineage, selects from dcf_runs) — the exact
    # failure test_decision_journal_view's downgrade walk exposed. A nullable
    # additive column is harmless to keep on downgrade (the 0171 non-destructive
    # precedent); pre-0182 code never mentions it.
    return
