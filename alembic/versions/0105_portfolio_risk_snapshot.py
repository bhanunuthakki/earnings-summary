"""portfolio_risk_snapshot — last-known whole-book risk cache (L5 PR2).

The Risk + Performance surfaces read benchmark-risk stats, concentration, book
drawdown, and factor exposure from the live :8000 tracker. When the tracker is
offline they previously went blank. This single-row-per-user cache holds the
last successful read so those surfaces degrade to cached values stamped
"as of <captured_at>" instead.

Single row per ``user_id`` (UNIQUE) — upserted on each successful fetch, so it's
always the most recent snapshot, never a growing ledger. Every metric column is
nullable: a partial tracker response (e.g. positioning up but beta down) still
records what it has.

Revision ID: 0105_portfolio_risk_snapshot
Revises: 0104_ask_answer_budget
Create Date: 2026-06-14
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0105_portfolio_risk_snapshot"
down_revision: str | Sequence[str] | None = "0104_ask_answer_budget"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "portfolio_risk_snapshots" in insp.get_table_names():
        return  # idempotent
    op.create_table(
        "portfolio_risk_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Text(), nullable=False, server_default=sa.text("'bhanu'")),
        # When this snapshot was captured (naive-UTC ISO) — what the offline
        # surfaces stamp as "as of".
        sa.Column("captured_at", sa.Text(), nullable=False),
        # The tracker's analytics window for the captured stats (display context).
        sa.Column("window_start", sa.Text(), nullable=True),
        sa.Column("window_end", sa.Text(), nullable=True),
        # Benchmark-risk stats (from /beta).
        sa.Column("benchmark", sa.Text(), nullable=True),
        sa.Column("beta", sa.Float(), nullable=True),
        sa.Column("alpha_annualized_pct", sa.Float(), nullable=True),
        sa.Column("sharpe", sa.Float(), nullable=True),
        sa.Column("sortino", sa.Float(), nullable=True),
        sa.Column("information_ratio", sa.Float(), nullable=True),
        sa.Column("tracking_error_annualized", sa.Float(), nullable=True),
        sa.Column("portfolio_volatility_annualized", sa.Float(), nullable=True),
        sa.Column("r_squared", sa.Float(), nullable=True),
        # Concentration (from /positioning).
        sa.Column("weighted_avg_correlation_spy", sa.Float(), nullable=True),
        sa.Column("num_positions", sa.Integer(), nullable=True),
        sa.Column("top1_weight_pct", sa.Float(), nullable=True),
        sa.Column("top5_weight_pct", sa.Float(), nullable=True),
        sa.Column("top10_weight_pct", sa.Float(), nullable=True),
        sa.Column("hhi", sa.Float(), nullable=True),
        sa.Column("effective_holdings", sa.Float(), nullable=True),
        # Book drawdown (derived from /performance).
        sa.Column("max_drawdown_pct", sa.Float(), nullable=True),
        sa.Column("current_drawdown_pct", sa.Float(), nullable=True),
        sa.Column("drawdown_recovered", sa.Integer(), nullable=True),  # 0/1 flag
        sa.Column("days_to_recovery", sa.Integer(), nullable=True),
        # Factor / style roll-up (derived from the positioning correlation rows).
        sa.Column("spy_beta", sa.Float(), nullable=True),
        sa.Column("qqq_beta", sa.Float(), nullable=True),
        sa.Column("growth_tilt", sa.Float(), nullable=True),
        sa.Column("avg_correlation_spy", sa.Float(), nullable=True),
        sa.Column("rate_beta_10y", sa.Float(), nullable=True),
        sa.Column("names_priced", sa.Integer(), nullable=True),
        sa.Column("names_total", sa.Integer(), nullable=True),
        sa.UniqueConstraint("user_id", name="uq_portfolio_risk_snapshots_user"),
    )


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "portfolio_risk_snapshots" not in insp.get_table_names():
        return
    op.drop_table("portfolio_risk_snapshots")
