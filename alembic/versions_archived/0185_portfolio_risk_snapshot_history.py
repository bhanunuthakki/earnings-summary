"""portfolio_risk_snapshot_history — append-only history for the risk snapshot.

Program review 2026-07-19: `portfolio_risk_snapshots` is a single-row-per-user
upsert (0105, `uq_portfolio_risk_snapshots_user`), so "did my exposure drift
vs 30 days ago" was structurally unanswerable — and the one prod row was
all-NULL because the persist gate accepted any partially-loaded tracker
response (fixed in `_persist_risk_snapshot` alongside this migration). This
table is the drift substrate: every good capture APPENDS here while the
single-row table stays the cheap latest-view the offline surfaces read.

Same columns as 0105 minus the UNIQUE(user_id). Workstream C8 builds the
drift trigger on top; this migration only lays the substrate.

Revision ID: 0185_portfolio_risk_snapshot_history
Revises: 0184_macro_sensitivities_n_obs
Create Date: 2026-07-19
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0185_portfolio_risk_snapshot_history"
down_revision: str | Sequence[str] | None = "0184_macro_sensitivities_n_obs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "portfolio_risk_snapshot_history" in insp.get_table_names():
        return  # idempotent
    op.create_table(
        "portfolio_risk_snapshot_history",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Text(), nullable=False, server_default=sa.text("'bhanu'")),
        sa.Column("captured_at", sa.Text(), nullable=False),
        sa.Column("window_start", sa.Text(), nullable=True),
        sa.Column("window_end", sa.Text(), nullable=True),
        sa.Column("benchmark", sa.Text(), nullable=True),
        sa.Column("beta", sa.Float(), nullable=True),
        sa.Column("alpha_annualized_pct", sa.Float(), nullable=True),
        sa.Column("sharpe", sa.Float(), nullable=True),
        sa.Column("sortino", sa.Float(), nullable=True),
        sa.Column("information_ratio", sa.Float(), nullable=True),
        sa.Column("tracking_error_annualized", sa.Float(), nullable=True),
        sa.Column("portfolio_volatility_annualized", sa.Float(), nullable=True),
        sa.Column("r_squared", sa.Float(), nullable=True),
        sa.Column("weighted_avg_correlation_spy", sa.Float(), nullable=True),
        sa.Column("num_positions", sa.Integer(), nullable=True),
        sa.Column("top1_weight_pct", sa.Float(), nullable=True),
        sa.Column("top5_weight_pct", sa.Float(), nullable=True),
        sa.Column("top10_weight_pct", sa.Float(), nullable=True),
        sa.Column("hhi", sa.Float(), nullable=True),
        sa.Column("effective_holdings", sa.Float(), nullable=True),
        sa.Column("max_drawdown_pct", sa.Float(), nullable=True),
        sa.Column("current_drawdown_pct", sa.Float(), nullable=True),
        sa.Column("drawdown_recovered", sa.Integer(), nullable=True),
        sa.Column("days_to_recovery", sa.Integer(), nullable=True),
        sa.Column("spy_beta", sa.Float(), nullable=True),
        sa.Column("qqq_beta", sa.Float(), nullable=True),
        sa.Column("growth_tilt", sa.Float(), nullable=True),
        sa.Column("avg_correlation_spy", sa.Float(), nullable=True),
        sa.Column("rate_beta_10y", sa.Float(), nullable=True),
        sa.Column("names_priced", sa.Integer(), nullable=True),
        sa.Column("names_total", sa.Integer(), nullable=True),
    )
    op.create_index(
        "ix_risk_snapshot_history_user_time",
        "portfolio_risk_snapshot_history",
        ["user_id", "captured_at"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "portfolio_risk_snapshot_history" not in insp.get_table_names():
        return
    op.drop_table("portfolio_risk_snapshot_history")
