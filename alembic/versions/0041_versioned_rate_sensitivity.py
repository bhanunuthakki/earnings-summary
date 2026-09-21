"""Preserve legacy sensitivities and admit separately reconstructed rate estimates."""

from __future__ import annotations

from alembic import op

revision = "0041_versioned_rate_sensitivity"
down_revision = "0040_fmp_watchlist_recovery"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE macro_sensitivity_estimates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL CHECK(ticker=UPPER(ticker)),
            series_id TEXT NOT NULL CHECK(series_id IN ('us_10y','fed_funds')),
            metric_version TEXT NOT NULL CHECK(metric_version='v2_rate_diff'),
            shock_unit TEXT NOT NULL CHECK(shock_unit='percentage_point'),
            return_unit TEXT NOT NULL CHECK(return_unit='log_return'),
            beta REAL NOT NULL,
            r_squared REAL NOT NULL CHECK(r_squared BETWEEN 0 AND 1),
            n_obs INTEGER NOT NULL CHECK(n_obs >= 12),
            lookback_window_days INTEGER NOT NULL CHECK(lookback_window_days > 0),
            input_sha TEXT NOT NULL CHECK(length(input_sha)=64),
            inputs_json TEXT NOT NULL CHECK(json_valid(inputs_json)),
            source_as_of TEXT NOT NULL,
            computed_at TEXT NOT NULL,
            UNIQUE(ticker,series_id,lookback_window_days,metric_version,input_sha)
        )
    """)
    op.execute("""CREATE TRIGGER macro_sensitivity_estimates_no_update
        BEFORE UPDATE ON macro_sensitivity_estimates BEGIN
        SELECT RAISE(ABORT,'rate estimates are immutable'); END""")
    op.execute("""CREATE TRIGGER macro_sensitivity_estimates_no_delete
        BEFORE DELETE ON macro_sensitivity_estimates BEGIN
        SELECT RAISE(ABORT,'rate estimates are immutable'); END""")


def downgrade() -> None:
    if (
        op.get_bind()
        .exec_driver_sql("SELECT 1 FROM macro_sensitivity_estimates LIMIT 1")
        .fetchone()
    ):
        raise RuntimeError(
            "Rate estimate history must be preserved; restore a verified backup to roll back"
        )
    op.execute("DROP TABLE macro_sensitivity_estimates")
