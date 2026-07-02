"""Provenance & self-updating freshness — valuations version instead of overwriting,
decisions record the model-version they rest on, and staleness derives itself.

2026-07-02: advisor recommendations (``decisions`` rows) are grounded in a DCF
valuation, but ``dcf_runs`` overwrote in place (``INSERT OR REPLACE`` on the
``uq_dcf_runs_ticker`` unique index), so a corrected model destroyed its
predecessor and a decision recorded its basis only in free-text prose. Nothing
could tell that a rec rested on a superseded model — e.g. RBRK decision id 76
stood on NPV $91, later corrected to $66.45, with no structural trace.

This lands the generic provenance+freshness convention; first instance is the
DCF → decision chain (KPI / thesis / allocation kinds plug into the same shape
in a follow-up).

dcf_runs — becomes a *versioned* model output:
- ``is_latest`` INTEGER NOT NULL DEFAULT 1, ``superseded_at`` (naive-UTC TEXT),
  ``superseded_by_id`` INTEGER (plain INTEGER, NO REFERENCES — the repo-wide
  FK-poisoning invariant).
- the standalone unique index ``uq_dcf_runs_ticker`` is dropped and replaced by a
  PARTIAL unique index ``uq_dcf_runs_latest`` on ``(ticker, COALESCE(segment_name,''))
  WHERE is_latest = 1`` — exactly one current run per ticker+segment while history
  accumulates. The index drops cleanly; ``ck_dcf_runs_over_under_ratio`` (0076) is a
  table CHECK and is untouched — NO table rebuild.

decisions — gains *basis provenance* (which model-version a row rests on):
- ``basis_kind`` ('dcf'|'kpi'|'thesis'|'allocation'), ``basis_ref_id`` INTEGER
  (NO REFERENCES), ``basis_value`` REAL (the key-number snapshot, e.g. fair value),
  ``basis_as_of`` TEXT, ``basis_meta_json`` TEXT (kind-specific extras). All
  nullable — existing rows read basis_kind NULL = 'unknown' until backfilled.

v_decision_freshness — a self-updating VIEW: joins each decision to the CURRENT
latest model-version of its basis_kind+entity and derives ``valuation_superseded``,
``basis_drift_pct`` and ``basis_status`` ∈ {unknown, fresh, superseded_minor,
superseded_material (|drift| >= 10%)}. Being a view, the next model that lands
auto-flips every decision resting on the prior one — no cron. The current-latest
subquery is UNION-shaped; only the 'dcf' branch exists now.

Revision ID: 0137_provenance_freshness
Revises: 0136_prompt_ab
"""

from __future__ import annotations

import contextlib

import sqlalchemy as sa

from alembic import op

revision: str = "0137_provenance_freshness"
down_revision: str | None = "0136_prompt_ab"
branch_labels = None
depends_on = None

# Kept in sync with model_provenance.freshness.MATERIAL_DRIFT_PCT — a view can't
# read a Python constant, so the 0.10 threshold is duplicated here by necessity.
# Change both together.
_MATERIAL_DRIFT = "0.10"


# Column *specs* (not shared sa.Column objects) so each upgrade() builds fresh
# Column instances — a module-level Column gets consumed/attached by op.add_column
# and cannot be reused across successive upgrades in one process (the test suite).
def _dcf_version_columns() -> tuple[sa.Column, ...]:
    return (
        sa.Column("is_latest", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("superseded_at", sa.String(32), nullable=True),
        sa.Column("superseded_by_id", sa.Integer(), nullable=True),
    )


def _decision_basis_columns() -> tuple[sa.Column, ...]:
    return (
        sa.Column("basis_kind", sa.String(16), nullable=True),
        sa.Column("basis_ref_id", sa.Integer(), nullable=True),
        sa.Column("basis_value", sa.Float(), nullable=True),
        sa.Column("basis_as_of", sa.String(32), nullable=True),
        sa.Column("basis_meta_json", sa.Text(), nullable=True),
    )


# One current model-version per (kind, entity). Only 'dcf' today; Phase 2 adds
#   UNION ALL SELECT 'kpi'..., 'thesis'..., 'allocation'...
_CURRENT_LATEST_SUBQUERY = """
    SELECT 'dcf' AS kind, ticker AS entity, id AS cur_ref_id,
           npv_per_share AS cur_value, valuation_date AS cur_as_of
    FROM dcf_runs
    WHERE is_latest = 1 AND COALESCE(segment_name, '') = ''
"""

_VIEW_SQL = f"""
CREATE VIEW v_decision_freshness AS
SELECT
    d.id                  AS decision_id,
    d.ticker              AS ticker,
    d.recommendation_kind AS recommendation_kind,
    d.decided_by          AS decided_by,
    d.outcome_label       AS outcome_label,
    d.made_at             AS made_at,
    d.basis_kind          AS basis_kind,
    d.basis_ref_id        AS basis_ref_id,
    d.basis_value         AS basis_value,
    d.basis_as_of         AS basis_as_of,
    cur.cur_ref_id        AS current_ref_id,
    cur.cur_value         AS current_value,
    cur.cur_as_of         AS current_as_of,
    CASE
        WHEN d.basis_kind IS NULL OR d.basis_value IS NULL THEN NULL
        WHEN cur.cur_ref_id IS NULL THEN 0
        WHEN d.basis_ref_id IS NOT NULL AND d.basis_ref_id = cur.cur_ref_id THEN 0
        WHEN d.basis_as_of IS NOT NULL AND cur.cur_as_of IS NOT NULL
             AND d.basis_as_of >= cur.cur_as_of THEN 0
        ELSE 1
    END AS valuation_superseded,
    CASE
        WHEN d.basis_value IS NOT NULL AND d.basis_value <> 0 AND cur.cur_value IS NOT NULL
            THEN (cur.cur_value - d.basis_value) / d.basis_value
        ELSE NULL
    END AS basis_drift_pct,
    CASE
        WHEN d.basis_kind IS NULL OR d.basis_value IS NULL THEN 'unknown'
        WHEN cur.cur_ref_id IS NULL THEN 'fresh'
        WHEN (d.basis_ref_id IS NOT NULL AND d.basis_ref_id = cur.cur_ref_id)
             OR (d.basis_as_of IS NOT NULL AND cur.cur_as_of IS NOT NULL
                 AND d.basis_as_of >= cur.cur_as_of)
            THEN 'fresh'
        WHEN cur.cur_value IS NOT NULL AND d.basis_value <> 0
             AND ABS((cur.cur_value - d.basis_value) / d.basis_value) >= {_MATERIAL_DRIFT}
            THEN 'superseded_material'
        ELSE 'superseded_minor'
    END AS basis_status
FROM decisions d
LEFT JOIN (
{_CURRENT_LATEST_SUBQUERY}
) cur ON cur.kind = d.basis_kind AND cur.entity = d.ticker
"""


def _has_table(insp: sa.Inspector, name: str) -> bool:
    return name in insp.get_table_names()


def _columns(insp: sa.Inspector, table: str) -> set[str]:
    return {c["name"] for c in insp.get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)

    if _has_table(insp, "dcf_runs"):
        have = _columns(insp, "dcf_runs")
        for col in _dcf_version_columns():
            if col.name not in have:
                op.add_column("dcf_runs", col)
        # Overwrite → versioned: drop the whole-ticker unique index, add the
        # partial one that only constrains the current row. COALESCE folds the
        # NULL segment so SQLite's "NULLs are distinct" rule can't admit two
        # current unsegmented runs for one ticker.
        op.execute("DROP INDEX IF EXISTS uq_dcf_runs_ticker")
        op.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_dcf_runs_latest "
            "ON dcf_runs (ticker, COALESCE(segment_name, '')) WHERE is_latest = 1"
        )

    if _has_table(insp, "decisions"):
        have = _columns(insp, "decisions")
        for col in _decision_basis_columns():
            if col.name not in have:
                op.add_column("decisions", col)

    if _has_table(insp, "decisions") and _has_table(insp, "dcf_runs"):
        op.execute("DROP VIEW IF EXISTS v_decision_freshness")
        op.execute(_VIEW_SQL)


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)

    op.execute("DROP VIEW IF EXISTS v_decision_freshness")

    if _has_table(insp, "dcf_runs"):
        dupes = bind.execute(
            sa.text(
                "SELECT COUNT(*) FROM (SELECT ticker FROM dcf_runs "
                "GROUP BY ticker, COALESCE(segment_name,'') HAVING COUNT(*) > 1)"
            )
        ).scalar()
        if dupes:
            raise RuntimeError(
                f"cannot downgrade 0137: {dupes} ticker(s) carry superseded dcf_runs history — "
                "the prior whole-ticker unique index would be violated; prune history first"
            )
        op.execute("DROP INDEX IF EXISTS uq_dcf_runs_latest")
        op.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_dcf_runs_ticker ON dcf_runs (ticker)")
        with op.batch_alter_table("dcf_runs") as batch:
            for name in ("superseded_by_id", "superseded_at", "is_latest"):
                with contextlib.suppress(KeyError, sa.exc.OperationalError):
                    batch.drop_column(name)

    if _has_table(insp, "decisions"):
        with op.batch_alter_table("decisions") as batch:
            for name in (
                "basis_meta_json",
                "basis_as_of",
                "basis_value",
                "basis_ref_id",
                "basis_kind",
            ):
                with contextlib.suppress(KeyError, sa.exc.OperationalError):
                    batch.drop_column(name)
