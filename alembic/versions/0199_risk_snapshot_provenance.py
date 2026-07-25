"""portfolio_risk_snapshots / portfolio_risk_snapshot_history —
metric_version + rebase_basis provenance (Personal Investment Partner PRD
``docs/design/personal_investment_partner_prd.md`` §7.1 requirement 9,
acceptance gap closed 2026-07-24).

PRD §7.1.9: "Historical comparisons use snapshots produced by the same
metric/version definition. A metric-version change must be explicit and must
not render a false delta against an incomparable prior." Before this
migration neither the single-row latest-view table nor the append-only
history table carried ANY signal of which metric definition or analytics-
window basis produced a row — a future change to the beta/sharpe/drawdown
math (a new factor leg, a different annualization, a redefined drawdown
window) would silently start comparing apples to oranges, and the Risk
Budget's "vs prior" delta (``src/pipeline/allocation_recommendation_panel.py
::render_risk_budget_section``) had no way to detect it.

Two plain nullable TEXT columns, added to BOTH tables (the single-row
upsert and the history append go through the same
``portfolio_risk_snapshot_store.write_snapshot`` writer and must carry the
same shape):

  * ``metric_version`` — the snapshot's metric-definition identity
    (``portfolio_risk_snapshot_store.METRIC_VERSION``, currently ``"v1"``).
  * ``rebase_basis`` — how the analytics window was established:
    ``"observed"`` (the tracker's actually-observed history) or
    ``"modeled_backfill"`` (the window includes a modeled walk-back before
    the first observation — derived from
    ``PerformanceSeries.backfill_start_unreliable``).

Existing rows are backfilled to ``metric_version='v1'``,
``rebase_basis='observed'`` — the CURRENT definition — rather than left
NULL. A NULL here would mean "unknown, not comparable to anything" (see
``portfolio_risk_snapshot_store.comparable``), but every row already in the
table was in fact produced by the one metric definition and observed-window
basis that has ever existed in this codebase; leaving them NULL would make
today's very first "vs prior" delta spuriously fire the incomparable-reason
guard instead of rendering. Any row captured going forward that used a
genuinely different definition gets its OWN explicit value at write time.

Follows 0186's idempotent ``sa.inspect`` guard (add-column-if-absent, no FK,
no CHECK) via plain ``op.add_column`` — a nullable column with no default
requiring a rewrite does not need SQLite's batch-table-rebuild dance.
Checked ``sqlite_master`` for views referencing either table: none exist (as
of this revision), so the batch/view-guard dance from 0188 is not needed
here.

Revision ID: 0199_risk_snapshot_provenance
Revises: 0198_filing_sections
Create Date: 2026-07-24

NOTE (for the repo owner, re-chain at push time): 0198 is claimed by a
parallel in-flight branch (senior partner brief budget) not yet on main as
of this revision's authoring. This migration chains off 0197_decision_drafts
— the highest revision confirmed on ``main`` at authoring time — and should
be re-chained onto 0198 if/when that lands first.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0199_risk_snapshot_provenance"
down_revision: str | Sequence[str] | None = "0198_filing_sections"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_LATEST_TABLE = "portfolio_risk_snapshots"
_HISTORY_TABLE = "portfolio_risk_snapshot_history"
_TABLES = (_LATEST_TABLE, _HISTORY_TABLE)

# The current definition every pre-existing row was, in fact, produced
# under — see the module docstring for why this is a backfill, not a NULL.
_CURRENT_METRIC_VERSION = "v1"
_CURRENT_REBASE_BASIS = "observed"


def _columns(insp: sa.Inspector, table: str) -> set[str]:
    return {c["name"] for c in insp.get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    existing_tables = set(insp.get_table_names())

    for table in _TABLES:
        if table not in existing_tables:
            continue  # pre-0105/0185 DB; those migrations create it fresh
        cols = _columns(insp, table)
        if "metric_version" not in cols:
            op.add_column(table, sa.Column("metric_version", sa.Text(), nullable=True))
        if "rebase_basis" not in cols:
            op.add_column(table, sa.Column("rebase_basis", sa.Text(), nullable=True))
        # The two raw dates the basis is derived FROM, so a future reader can
        # recompute the classification instead of trusting the expression that
        # produced it. These are NOT redundant with window_start/window_end:
        # those come from the BETA endpoint, while the basis and all four
        # drawdown columns come from the PERFORMANCE endpoint, and the two
        # endpoints default to different windows (measured live 2026-07-24:
        # beta 2025-07-24, performance 2026-05-09). Cross-checking the basis
        # against window_start would therefore contradict it.
        if "perf_window_start" not in cols:
            op.add_column(table, sa.Column("perf_window_start", sa.Text(), nullable=True))
        if "perf_observed_from" not in cols:
            op.add_column(table, sa.Column("perf_observed_from", sa.Text(), nullable=True))
        # Backfill pre-existing rows to the current definition — see module
        # docstring for why NULL would be wrong here.
        bind.execute(
            sa.text(
                f"UPDATE {table} SET metric_version = :mv, rebase_basis = :rb "
                "WHERE metric_version IS NULL"
            ),
            {"mv": _CURRENT_METRIC_VERSION, "rb": _CURRENT_REBASE_BASIS},
        )


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    existing_tables = set(insp.get_table_names())

    for table in _TABLES:
        if table not in existing_tables:
            continue
        cols = _columns(insp, table)
        # Direct op.drop_column (mirrors 0186's identical treatment of
        # input_sha) — no view references either table (verified against
        # sqlite_master at authoring time), so the batch/view-guard dance
        # from 0188 is unnecessary here.
        if "perf_observed_from" in cols:
            op.drop_column(table, "perf_observed_from")
        if "perf_window_start" in cols:
            op.drop_column(table, "perf_window_start")
        if "rebase_basis" in cols:
            op.drop_column(table, "rebase_basis")
        if "metric_version" in cols:
            op.drop_column(table, "metric_version")
