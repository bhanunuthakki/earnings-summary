"""comparable_sets / comparable_set_members / comp_set_metrics_daily —
Phase 1 foundation for the bottoms-up comparable-set program
(docs/design/comparable_sets_bottoms_up.md §6).

Additive-only; existing-table guard follows the ``sa.inspect(bind).
get_table_names()`` convention from ``alembic/versions/
0044_etf_profile_and_holdings.py``. No existing table/column touched.

Three tables:
  comparable_sets        -- one row per resolved (ticker, method_version) set
  comparable_set_members -- membership with valid_from/valid_to (versioned)
  comp_set_metrics_daily -- median + cap-weighted aggregate per (scope,
                            metric, stat_type, date, method_version)

Deviation from the design doc's literal §6 DDL snippet: the doc shows
``comparable_set_members.comparable_set_id`` with a real
``sa.ForeignKey("comparable_sets.comparable_set_id")``. Dropped here —
repo-wide FK-poisoning invariant (reference_platform_invariants.md, and see
0160/0161/0162's identical deviation): ``open_conn`` runs ``PRAGMA
foreign_keys=ON``, so a real FK fails every child insert under a test
fixture stamped at an earlier alembic revision than the parent table. Code-
level referential integrity only (``compute.comparable_sets`` always writes
the parent ``comparable_sets`` row before any member row); the
``idx_csm_member`` index still covers the lookup path a FK would have
indexed anyway.

No SQL views (repo precedent: reference_metrics_ratios_views_slow.md — views
here are a known slow/buggy landmine); comp_set_metrics_daily is a
materialized table written by execution/track_comp_metrics.py.

Revision ID: 0170_comparable_sets
Revises: 0168_segment_quarterly_coverage
Create Date: 2026-07-17
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0170_comparable_sets"
down_revision: str | Sequence[str] | None = "0168_segment_quarterly_coverage"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())

    # ------------------------------------------------------------------
    # comparable_sets -- one row per resolved set
    # ------------------------------------------------------------------
    if "comparable_sets" not in existing:
        op.create_table(
            "comparable_sets",
            sa.Column("comparable_set_id", sa.String(64), primary_key=True),
            sa.Column("ticker", sa.String(16), nullable=False),
            sa.Column("method_version", sa.Integer, nullable=False),
            sa.Column("resolved_at", sa.DateTime, nullable=False),
            sa.Column(
                "metric_class", sa.String(16), nullable=False
            ),  # 'operating'|'financial'|'reit'
            sa.Column("method_flags", sa.Text, nullable=True),  # JSON blob, see §3.1/5.4
            sa.Column(
                "source_summary", sa.Text, nullable=True
            ),  # JSON: {step_a_n, step_b_n, step_c_n, override_applied}
        )
        op.create_index("idx_comparable_sets_ticker", "comparable_sets", ["ticker"])

    # ------------------------------------------------------------------
    # comparable_set_members -- membership, versioned by valid_from/valid_to
    # ------------------------------------------------------------------
    if "comparable_set_members" not in existing:
        op.create_table(
            "comparable_set_members",
            # Plain column -- no REFERENCES (repo-wide FK-poisoning invariant,
            # see module docstring). Code-level RI only.
            sa.Column("comparable_set_id", sa.String(64), nullable=False),
            sa.Column("member_ticker", sa.String(16), nullable=False),
            sa.Column(
                "membership_reason", sa.String(24), nullable=False
            ),  # 'industry_seed'|'sector_widened'|'llm_ratified'|'pinned_override'
            sa.Column(
                "context_only", sa.Boolean, nullable=False, server_default=sa.false()
            ),  # market-cap-only peer, §3.1 Step C
            sa.Column("valid_from", sa.Date, nullable=False),
            sa.Column("valid_to", sa.Date, nullable=True),  # NULL = still current
            sa.PrimaryKeyConstraint(
                "comparable_set_id", "member_ticker", "valid_from", name="pk_comparable_set_members"
            ),
        )
        op.create_index("idx_csm_member", "comparable_set_members", ["member_ticker", "valid_from"])

    # ------------------------------------------------------------------
    # comp_set_metrics_daily -- materialized median + aggregate per scope/date
    # ------------------------------------------------------------------
    if "comp_set_metrics_daily" not in existing:
        op.create_table(
            "comp_set_metrics_daily",
            sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
            sa.Column(
                "scope_type", sa.String(16), nullable=False
            ),  # 'comparable_set'|'industry'|'sector'|'fmp_snapshot'
            sa.Column(
                "scope_key", sa.String(64), nullable=False
            ),  # comparable_set_id, or industry/sector string
            sa.Column("as_of_date", sa.Date, nullable=False),
            sa.Column(
                "metric", sa.String(24), nullable=False
            ),  # 'pe_ttm'|'ev_ebitda_ttm'|'p_b'|'p_tbv'|'rev_yoy'|'fcf_yield_ttm'
            sa.Column("stat_type", sa.String(16), nullable=False),  # 'median'|'aggregate'
            sa.Column(
                "value", sa.Float, nullable=True
            ),  # NULL when undefined (§5.2), never a sentinel like -1/0
            sa.Column("n_members", sa.Integer, nullable=False),
            sa.Column("n_valid", sa.Integer, nullable=False),
            sa.Column("coverage_pct", sa.Float, nullable=False),
            sa.Column("method_version", sa.Integer, nullable=False),
            sa.Column("method_flags", sa.Text, nullable=True),  # JSON blob
            sa.Column("computed_at", sa.DateTime, nullable=False),
            sa.UniqueConstraint(
                "scope_type",
                "scope_key",
                "as_of_date",
                "metric",
                "stat_type",
                "method_version",
                name="uq_comp_set_metrics_daily",
            ),
        )
        op.create_index(
            "idx_csmd_scope_date",
            "comp_set_metrics_daily",
            ["scope_type", "scope_key", "as_of_date"],
        )


def downgrade() -> None:
    for t in ("comp_set_metrics_daily", "comparable_set_members", "comparable_sets"):
        op.execute(f"DROP TABLE IF EXISTS {t}")
