"""comparable_sets / comparable_set_members / comp_set_metrics_daily -- Phase 1 of the
bottoms-up comparable-set program (docs/design/comparable_sets_bottoms_up.md section 6).

Three new, purely additive tables:
  * ``comparable_sets`` -- one row per resolved+frozen (ticker, method_version) set.
  * ``comparable_set_members`` -- versioned membership, valid_from/valid_to.
  * ``comp_set_metrics_daily`` -- one row per (scope, date, metric, stat_type); the
    single home for both bottoms-up aggregates AND re-keyed FMP snapshot rows
    (``scope_type='fmp_snapshot'``), per the design doc's "no cross-format
    drift-check join" decision.

**Deviation from the doc's literal §6 DDL, recorded here per the Directive
Maintenance convention (refine in the same PR, don't re-litigate silently):**
the doc's snippet writes ``comparable_set_members.comparable_set_id`` with a real
``sa.ForeignKey("comparable_sets.comparable_set_id")``. This repo's FK-poisoning
invariant ([[reference-platform-invariants]], and the identical call already made
in 0160/0161 for formula_definitions/metric_computation_attempts) means a real FK
fails every child insert under a test fixture stamped at an earlier alembic
revision, because ``db.open_conn`` runs ``PRAGMA foreign_keys=ON``. Shipped here as
a plain ``sa.String(64)`` column with no ``ForeignKey`` -- validated at the code
layer instead (``compute.comparable_sets`` only ever writes a ``comparable_set_id``
it just created/looked up itself in the same transaction).

Revision ID: 0165_comparable_sets
Revises: 0164_tracked_companies_accounting_standard
Create Date: 2026-07-17
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0165_comparable_sets"
down_revision: str | Sequence[str] | None = "0164_tracked_companies_accounting_standard"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SETS = "comparable_sets"
_MEMBERS = "comparable_set_members"
_METRICS = "comp_set_metrics_daily"


def upgrade() -> None:
    bind = op.get_bind()
    names = set(sa.inspect(bind).get_table_names())

    if _SETS not in names:
        op.create_table(
            _SETS,
            sa.Column("comparable_set_id", sa.String(64), primary_key=True),
            sa.Column("ticker", sa.String(16), nullable=False),
            sa.Column("method_version", sa.Integer(), nullable=False),
            sa.Column("resolved_at", sa.DateTime(), nullable=False),
            sa.Column("metric_class", sa.String(16), nullable=False),
            sa.CheckConstraint(
                "metric_class IN ('operating', 'financial', 'reit')",
                name="ck_comparable_sets_metric_class",
            ),
            sa.Column("method_flags", sa.Text(), nullable=True),
            sa.Column("source_summary", sa.Text(), nullable=True),
        )
        op.create_index("idx_comparable_sets_ticker", _SETS, ["ticker"])

    if _MEMBERS not in names:
        op.create_table(
            _MEMBERS,
            # Plain String, no ForeignKey -- see module docstring (FK-poisoning
            # invariant, same precedent as 0160/0161).
            sa.Column("comparable_set_id", sa.String(64), nullable=False),
            sa.Column("member_ticker", sa.String(16), nullable=False),
            sa.Column("membership_reason", sa.String(24), nullable=False),
            sa.CheckConstraint(
                "membership_reason IN ('industry_seed', 'sector_widened', "
                "'llm_ratified', 'pinned_override')",
                name="ck_comparable_set_members_reason",
            ),
            sa.Column("context_only", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("valid_from", sa.Date(), nullable=False),
            sa.Column("valid_to", sa.Date(), nullable=True),
            sa.PrimaryKeyConstraint(
                "comparable_set_id",
                "member_ticker",
                "valid_from",
                name="pk_comparable_set_members",
            ),
        )
        op.create_index("idx_csm_member", _MEMBERS, ["member_ticker", "valid_from"])

    if _METRICS not in names:
        op.create_table(
            _METRICS,
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("scope_type", sa.String(16), nullable=False),
            sa.CheckConstraint(
                "scope_type IN ('comparable_set', 'industry', 'sector', 'fmp_snapshot')",
                name="ck_comp_set_metrics_daily_scope_type",
            ),
            sa.Column("scope_key", sa.String(64), nullable=False),
            sa.Column("as_of_date", sa.Date(), nullable=False),
            sa.Column("metric", sa.String(24), nullable=False),
            sa.CheckConstraint(
                "metric IN ('pe_ttm', 'ev_ebitda_ttm', 'p_b', 'p_tbv', 'rev_yoy', 'fcf_yield_ttm')",
                name="ck_comp_set_metrics_daily_metric",
            ),
            sa.Column("stat_type", sa.String(16), nullable=False),
            sa.CheckConstraint(
                "stat_type IN ('median', 'aggregate')",
                name="ck_comp_set_metrics_daily_stat_type",
            ),
            sa.Column("value", sa.Float(), nullable=True),
            sa.Column("n_members", sa.Integer(), nullable=False),
            sa.Column("n_valid", sa.Integer(), nullable=False),
            sa.Column("coverage_pct", sa.Float(), nullable=False),
            sa.Column("method_version", sa.Integer(), nullable=False),
            sa.Column("method_flags", sa.Text(), nullable=True),
            sa.Column("computed_at", sa.DateTime(), nullable=False),
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
        op.create_index("idx_csmd_scope_date", _METRICS, ["scope_type", "scope_key", "as_of_date"])


def downgrade() -> None:
    bind = op.get_bind()
    names = set(sa.inspect(bind).get_table_names())
    if _METRICS in names:
        op.drop_table(_METRICS)
    if _MEMBERS in names:
        op.drop_table(_MEMBERS)
    if _SETS in names:
        op.drop_table(_SETS)
