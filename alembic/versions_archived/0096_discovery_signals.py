"""discovery_signals — the typed, weighted child of discovery_candidates.

The single-Float ``discovery_candidates.score`` (0081) is an equal-weight
integer count: one point per screen passed, one per adjacency source. It
structurally cannot express a per-source WEIGHT (a Whale Rock new buy ≠ an
ARK rebalance) nor hold the dated, typed signal a weighted-recency-decay
ranking needs. This table is that substrate: one row per
``(user, ticker, signal_class, source_key)`` — the typed observation that
surfaced (or re-surfaced) a name — carrying the resolved source weight, the
signal-native ``raw_strength``, and the ``observed_at`` event time the
recency decay reads. The candidate's ``score`` is now a weighted sum over
these rows (``src/discovery/scoring.py``), and ``score_json`` (0095) holds
the per-class "why this rank" breakdown.

Routing (the signal-taxonomy arbiter, interaction_paradigm §6): a 13F move
on an UNTRACKED ticker is a ``discovery_signals`` row (``signal_class
= 'investor_13f'``); a move on a TRACKED ticker is a ``news`` row instead —
this table never duplicates the information-diet ``signals`` substrate.

UNIQUE(user, ticker, signal_class, source_key) makes a re-run an UPSERT that
refreshes weight/strength/observed_at/detail — the same idempotence
discovery_candidates already has. New-table CHECKs per 0071's policy.

Revision ID: 0096_discovery_signals
Revises: 0095_signals
Create Date: 2026-06-13
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0096_discovery_signals"
down_revision: str | Sequence[str] | None = "0095_signals"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The signal taxonomy this table is closed under. 'screen' / 'adjacency' are
# the deterministic fundamental sources (run_discovery.py); 'investor_13f' is
# the EDGAR 13F-HR miner's top-of-funnel investor-research signal.
_CLASSES = "('screen', 'adjacency', 'investor_13f')"


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "discovery_signals" in insp.get_table_names():
        return  # idempotent
    op.create_table(
        "discovery_signals",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Text(), nullable=False, server_default=sa.text("'bhanu'")),
        sa.Column("ticker", sa.Text(), nullable=False),
        sa.Column("signal_class", sa.Text(), nullable=False),
        sa.Column("source_key", sa.Text(), nullable=False),
        # The roster weight resolved at write time (denormalized snapshot of
        # discovery_sources.base_weight) so a candidate's score is reproducible
        # from its own rows even if a weight is edited afterward.
        sa.Column("weight", sa.Float(), nullable=False, server_default=sa.text("1.0")),
        # Signal-native magnitude, normalized toward ~[0, 1+] so weights are the
        # comparable lever: a screen pass = 1.0; an adjacency = capped mention
        # count; a 13F move = position size as a fraction of the fund's book.
        sa.Column("raw_strength", sa.Float(), nullable=False, server_default=sa.text("1.0")),
        # When the underlying EVENT happened (screen run, transcript date, 13F
        # filing) — the recency-decay clock, distinct from updated_at.
        sa.Column("observed_at", sa.Text(), nullable=False),
        sa.Column("detail", sa.Text(), nullable=True),
        # Structured extras: 13F action ('new'/'add'/'trim'/'exit'), the holding
        # an adjacency was found near, the fund's style tags — read by scoring.
        sa.Column("meta_json", sa.Text(), nullable=True),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.CheckConstraint(f"signal_class IN {_CLASSES}", name="ck_discovery_signals_class"),
        sa.CheckConstraint(
            "meta_json IS NULL OR json_valid(meta_json)",
            name="ck_discovery_signals_meta_json",
        ),
        sa.UniqueConstraint(
            "user_id",
            "ticker",
            "signal_class",
            "source_key",
            name="uq_discovery_signals_identity",
        ),
    )
    op.create_index("ix_discovery_signals_user_ticker", "discovery_signals", ["user_id", "ticker"])
    op.create_index(
        "ix_discovery_signals_user_class", "discovery_signals", ["user_id", "signal_class"]
    )


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "discovery_signals" not in insp.get_table_names():
        return
    op.drop_index("ix_discovery_signals_user_class", table_name="discovery_signals")
    op.drop_index("ix_discovery_signals_user_ticker", table_name="discovery_signals")
    op.drop_table("discovery_signals")
