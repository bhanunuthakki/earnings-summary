"""timeseries_signals — persisted output of the time-series intelligence layer.

The Phase 1 time-series module (src/timeseries/) added six statistical primitives
+ DB-aware loaders for materializing a Series. The CLI at execution/analyze_timeseries.py
prints a JSON report to stdout — useful for one-off inspection but invisible
to every downstream consumer (briefs, lenses, dashboards). This migration
persists each detected signal as one row so the renderer (§3.5, separate PR)
and the LLM prompts (§2/4/5/9, separate PR) can read them with a normal SQL
query instead of re-running primitives every render.

Schema:
  one row per (ticker, metric_name, metric_kind, signal_type) — refresh
  overwrites in place via INSERT ... ON CONFLICT REPLACE so the table stays
  thin (one current row per signal, not an append-only history).

Severity is computed by the writer at insert time (see signal_writer._severity)
so the renderer can filter ``WHERE severity='red'`` without re-deriving
thresholds; a future tweak to severity logic re-runs the writer and the new
severity overwrites the old row.

Narrative is a pre-rendered English sentence ("Revenue YoY decelerated to 12%
vs 4Q avg 19%; 3rd consecutive quarter of deceleration"). Template-rendered
in the writer — no LLM call — so it round-trips deterministically.

Revision ID: 0053_timeseries_signals
Revises: 0047_document_table_extractions, 0051_tracked_processing_tier
Create Date: 2026-05-25

Mergepoint note: the chain has two open heads (0047 and 0051) — both already
inherit 0052_llm_budgets transitively via 0046_decisions, which itself merged
0052. This migration only needs to merge 0047 + 0051; listing 0052 again
would cause an `update_version` KeyError because 0052 has already been
removed from `alembic_version` heads by 0046's apply.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0053_timeseries_signals"
down_revision: str | Sequence[str] | None = (
    "0047_document_table_extractions",
    "0051_tracked_processing_tier",
)
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())
    if "timeseries_signals" in existing:
        return

    op.create_table(
        "timeseries_signals",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        # Ticker the signal pertains to. Always upper-case at write time —
        # the writer normalizes so downstream filters can rely on that.
        sa.Column("ticker", sa.String(length=16), nullable=False),
        # Metric the signal was computed over. For metric_kind='financial'
        # this is a line_item from financial_facts (e.g. "revenue",
        # "operating_income", "free_cash_flow"). For 'kpi' it's a
        # kpi_definitions.name. For 'segment' it's "<segment>:<metric>"
        # (e.g. "Google Cloud:revenue") so the row is self-describing.
        sa.Column("metric_name", sa.String(length=128), nullable=False),
        # Source table the metric was loaded from. CHECK constraint enforces
        # the closed enum so a typo doesn't silently land bad data.
        sa.Column(
            "metric_kind",
            sa.String(length=16),
            nullable=False,
        ),
        # Which primitive produced this row. CHECK enforces the closed enum.
        sa.Column(
            "signal_type",
            sa.String(length=32),
            nullable=False,
        ),
        # Raw primitive output as JSON. Keeps the row width fixed regardless
        # of how the primitive's payload shape evolves; renderer parses
        # whatever fields it expects per signal_type.
        sa.Column("value_json", sa.Text(), nullable=False),
        # Three-bucket severity computed at write time by signal_writer.
        # CHECK enforces the enum so the renderer can switch on str equality.
        sa.Column(
            "severity",
            sa.String(length=8),
            nullable=False,
        ),
        # Pre-rendered English sentence. Optional — some signals (e.g. the
        # correlation matrix as a whole) don't have a single sentence
        # representation and render in the UI as a heatmap instead.
        sa.Column("narrative", sa.Text(), nullable=True),
        sa.Column("computed_at", sa.DateTime(), nullable=False),
        # Groups rows written in the same logical refresh. Matches the
        # llm_calls.run_id convention so a single refresh's outputs across
        # tables can be joined on run_id when triaging anomalies.
        sa.Column("run_id", sa.String(length=64), nullable=True),
        sa.CheckConstraint(
            "metric_kind IN ('financial', 'kpi', 'segment')",
            name="ck_timeseries_signals_metric_kind",
        ),
        sa.CheckConstraint(
            "signal_type IN ('trend', 'inflection', 'anomaly', "
            "'yoy_acceleration', 'seasonal', 'correlation')",
            name="ck_timeseries_signals_signal_type",
        ),
        sa.CheckConstraint(
            "severity IN ('green', 'yellow', 'red')",
            name="ck_timeseries_signals_severity",
        ),
        sa.UniqueConstraint(
            "ticker",
            "metric_name",
            "metric_kind",
            "signal_type",
            name="uq_timeseries_signals_logical",
        ),
    )
    op.create_index(
        "idx_timeseries_signals_ticker_signal",
        "timeseries_signals",
        ["ticker", "signal_type"],
    )
    op.create_index(
        "idx_timeseries_signals_ticker_severity",
        "timeseries_signals",
        ["ticker", "severity"],
    )
    op.create_index(
        "idx_timeseries_signals_run_id",
        "timeseries_signals",
        ["run_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "idx_timeseries_signals_run_id", table_name="timeseries_signals"
    )
    op.drop_index(
        "idx_timeseries_signals_ticker_severity", table_name="timeseries_signals"
    )
    op.drop_index(
        "idx_timeseries_signals_ticker_signal", table_name="timeseries_signals"
    )
    op.drop_table("timeseries_signals")
