"""confidence_observations — the ground-truth ledger that grades stored confidence.

Close-the-Loops L10 (self-calibrating credibility engine). The per-fact
``confidence`` number (src/pipeline/confidence.py) is a hand-set arithmetic
prior — ``TIER_BASE`` + ``METHOD_DELTA`` − ``ISSUE_PENALTY``, all constants —
that has never been checked against reality. The reality IS collected: the
restatement detector links a corrected fact to the row it superseded
(``supersedes_id``), and the validation engine logs cross-source disagreements.
Both were discarded — every ``insert_*_with_restatement_detection`` call site
threw the ``superseded_id`` away, and no surface ever joined a disagreement back
to the confidence it should have lowered.

This append-only ledger captures that ground truth as graded *observations*:
each row is one fact whose stored confidence we can now score against what
later happened to it.

  * ``kind='restatement'`` — a later filing re-reported this fact's period.
    ``outcome=1`` when the number HELD (a 10-K confirming a 10-Q, delta within
    the immaterial tolerance); ``outcome=0`` when it was materially restated
    (the source corrected itself → the earlier figure was wrong).
  * ``kind='disagreement'`` — this fact disagreed with a higher-tier source for
    the same (ticker, period, line_item). ``outcome=0`` (the higher tier is the
    truth proxy) — a calibration signal on whether the disagreement *penalty*
    is harsh enough.

Every contextual column (``source_tier``, ``line_item``, ``ticker``,
``period_end``, ``old_value``/``new_value``/``delta_pct``) is a SNAPSHOT taken
at capture time, so the reliability read is a pure aggregation over this one
table — no join back to ``financial_facts`` — and survives the graded row being
purged. ``predicted_confidence`` is the point-in-time stored confidence of the
graded fact: capturing it at supersede time, rather than re-reading it later,
keeps the prediction-vs-outcome pair honest even after the confidence backfill
rescores the old row.

Append-only + idempotent: ``UNIQUE(fact_table, fact_id, kind)`` means the
forward call-site capture and the historical backfill
(``execution/build_confidence_observations.py``) coexist — whichever records a
given (fact, kind) first wins, re-runs are no-ops.

Revision ID: 0106_confidence_observations
Revises: 0105_portfolio_risk_snapshot
Create Date: 2026-06-14
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0106_confidence_observations"
down_revision: str | Sequence[str] | None = "0105_portfolio_risk_snapshot"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_KINDS = "('restatement', 'disagreement')"
_TABLES = "('financial_facts', 'kpi_facts')"


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "confidence_observations" in insp.get_table_names():
        return  # idempotent
    op.create_table(
        "confidence_observations",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Text(), nullable=False, server_default=sa.text("'bhanu'")),
        # Which fact table + row the observation grades. The graded row is the
        # OLDER (superseded) fact for a restatement, the LOWER-tier fact for a
        # disagreement.
        sa.Column("fact_table", sa.Text(), nullable=False),
        sa.Column("fact_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        # The point-in-time stored confidence of the graded fact (the prediction).
        sa.Column("predicted_confidence", sa.Float(), nullable=False),
        # Snapshots of the graded fact's context (denormalized so the reliability
        # read never joins back, and survives a later purge of the fact row).
        sa.Column("source_tier", sa.Text(), nullable=True),
        sa.Column("line_item", sa.Text(), nullable=True),
        sa.Column("ticker", sa.Text(), nullable=True),
        sa.Column("period_end", sa.Text(), nullable=True),
        sa.Column("old_value", sa.Float(), nullable=True),
        sa.Column("new_value", sa.Float(), nullable=True),
        sa.Column("delta_pct", sa.Float(), nullable=True),
        # 1 = the graded value held / was correct; 0 = it was wrong.
        sa.Column("outcome", sa.Integer(), nullable=False),
        sa.Column("observed_at", sa.Text(), nullable=False),
        sa.CheckConstraint(f"kind IN {_KINDS}", name="ck_confidence_obs_kind"),
        sa.CheckConstraint(f"fact_table IN {_TABLES}", name="ck_confidence_obs_table"),
        sa.CheckConstraint("outcome IN (0, 1)", name="ck_confidence_obs_outcome"),
        sa.UniqueConstraint("fact_table", "fact_id", "kind", name="uq_confidence_obs"),
    )
    op.create_index(
        "ix_confidence_obs_tier_kind",
        "confidence_observations",
        ["user_id", "source_tier", "kind"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "confidence_observations" not in insp.get_table_names():
        return
    op.drop_index("ix_confidence_obs_tier_kind", table_name="confidence_observations")
    op.drop_table("confidence_observations")
