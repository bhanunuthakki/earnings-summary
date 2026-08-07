"""investor_calibration — the append-only ledger for recalibrating 13F weights.

The Discovery rule calls for recalibrating each rostered fund's ``base_weight``
quarterly against the realized forward returns of its new buys. The miner
full-replaces ``investor_13f`` signals each quarter (a sold position drops), so
the signal table can't carry that history — this ledger does: one row per
``(source_key, ticker, observed_at)`` new buy, recorded at SURFACING time with
the price then, and later filled with the price one ``horizon_days`` out and the
realized return + a win/loss verdict. ``recalibrate_investor_weights.py`` reads
it to nudge ``discovery_sources.base_weight`` by each fund's measured hit-rate.

Append-only + idempotent (UNIQUE identity): re-running the snapshot never
double-records a buy, and a measured row is never re-measured. A name with no
price on file resolves to ``outcome='no_price'`` and is excluded from the
hit-rate rather than scored as a loss.

Revision ID: 0100_investor_calibration
Revises: 0099_discovery_sources_ciks
Create Date: 2026-06-13
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0100_investor_calibration"
down_revision: str | Sequence[str] | None = "0099_discovery_sources_ciks"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OUTCOMES = "('win', 'loss', 'flat', 'no_price')"


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "investor_calibration" in insp.get_table_names():
        return  # idempotent
    op.create_table(
        "investor_calibration",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Text(), nullable=False, server_default=sa.text("'bhanu'")),
        sa.Column("source_key", sa.Text(), nullable=False),
        sa.Column("ticker", sa.Text(), nullable=False),
        # The 13F filing date — when the buy SURFACED (the entry the forward
        # return is measured from, not the manager's actual quarter-end fill).
        sa.Column("observed_at", sa.Text(), nullable=False),
        sa.Column("action", sa.Text(), nullable=True),  # 'new' | 'add'
        sa.Column("entry_price", sa.Float(), nullable=True),
        sa.Column("entry_recorded_at", sa.Text(), nullable=False),
        sa.Column("horizon_days", sa.Integer(), nullable=False, server_default=sa.text("180")),
        # Filled by the measure step once the horizon has elapsed.
        sa.Column("measured_at", sa.Text(), nullable=True),
        sa.Column("exit_price", sa.Float(), nullable=True),
        sa.Column("forward_return", sa.Float(), nullable=True),
        sa.Column("outcome", sa.Text(), nullable=True),
        sa.CheckConstraint(
            f"outcome IS NULL OR outcome IN {_OUTCOMES}", name="ck_investor_calibration_outcome"
        ),
        sa.UniqueConstraint(
            "user_id", "source_key", "ticker", "observed_at", name="uq_investor_calibration"
        ),
    )
    op.create_index(
        "ix_investor_calibration_source", "investor_calibration", ["user_id", "source_key"]
    )


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "investor_calibration" not in insp.get_table_names():
        return
    op.drop_index("ix_investor_calibration_source", table_name="investor_calibration")
    op.drop_table("investor_calibration")
