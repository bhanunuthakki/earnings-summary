"""macro_overlay — daily macro time series + per-ticker betas.

The fresh-review memo (Week 3) introduces a macro overlay: rates, FX,
commodities, sector indices that every holding is implicitly exposed to.
Two tables underpin the scenario engine:

  macro_series         — long-form daily history per series_id. One row per
                          (series_id, rate_date). Source labels (`FMP`,
                          `FRED`, `manual`) let us mix providers without
                          losing provenance. Volume target: ~3k rows per
                          series × 12 series ≈ 36k rows steady-state.

  macro_sensitivities  — per-ticker beta to each series, computed by OLS
                          on weekly returns over a configurable lookback
                          window (default 252 trading days ≈ 1y). Stored
                          keyed by (ticker, series_id, lookback_window) so
                          we can carry e.g. 1-year and 3-year betas
                          side by side without overwriting.

The scenario lenses (`macro_scenario`, `portfolio_macro_stress`) join the
two tables: given a shock to fed_funds / USD-BRL / Brent / etc., the
sensitivities table maps the shock to per-ticker P&L deltas and the lens
narrates the read-through against the active thesis.

Revision ID: 0045_macro_overlay
Revises: 0043_self_update_triggers
Create Date: 2026-05-25
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0045_macro_overlay"
down_revision: str | Sequence[str] | None = "0043_self_update_triggers"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())

    # ------------------------------------------------------------------
    # macro_series — daily history per series_id
    # ------------------------------------------------------------------
    if "macro_series" not in existing:
        op.create_table(
            "macro_series",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("series_id", sa.String(length=48), nullable=False),
            sa.Column("rate_date", sa.Date(), nullable=False),
            sa.Column("value", sa.Numeric(20, 8), nullable=False),
            sa.Column("source", sa.String(length=32), nullable=False, server_default="FMP"),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint("series_id", "rate_date", name="uq_macro_series"),
        )
        op.create_index(
            "idx_macro_series_lookup", "macro_series", ["series_id", "rate_date"]
        )

    # ------------------------------------------------------------------
    # macro_sensitivities — per-ticker beta + r-squared
    # ------------------------------------------------------------------
    if "macro_sensitivities" not in existing:
        op.create_table(
            "macro_sensitivities",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("ticker", sa.String(length=16), nullable=False),
            sa.Column("series_id", sa.String(length=48), nullable=False),
            sa.Column("beta", sa.Float(), nullable=False),
            sa.Column("r_squared", sa.Float(), nullable=True),
            sa.Column("lookback_window_days", sa.Integer(), nullable=False),
            sa.Column("computed_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint(
                "ticker", "series_id", "lookback_window_days", name="uq_macro_sens"
            ),
        )
        op.create_index(
            "idx_macro_sens_ticker", "macro_sensitivities", ["ticker", "series_id"]
        )


def downgrade() -> None:
    for t in ("macro_sensitivities", "macro_series"):
        op.execute(f"DROP TABLE IF EXISTS {t}")
