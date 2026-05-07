"""dcf_runs_breakdown — single-row-per-ticker DCFs with component breakdown.

Replaces the previous one-row-per-segment shape with a single aggregate row
per ticker whose `breakdown_json` column holds the per-segment + overhead
component list. The aggregate `npv` equals the sum of component NPVs.

Migration steps:
    1. Add `breakdown_json TEXT NULL` column to dcf_runs.
    2. Backfill: for each ticker that has segment_name-having rows, collapse
       them into the latest `segment_name IS NULL` row by writing a JSON list
       of `{component_name, component_type, base_revenue, revenue_growths,
       fcf_margin, wacc, terminal_growth, periods_per_year, npv,
       npv_per_share}` entries. Append a synthetic 'overhead' component
       computed as (consolidated_npv - sum(segment_npvs)) so the aggregate
       row's npv equals the sum of components by construction.
    3. Delete the per-segment rows.
    4. Add UNIQUE(ticker) constraint enforcing one row per company.

`segment_name` column is kept (nullable) for backward-compat with snapshots
written before this migration; new writes leave it NULL.

Revision ID: 0018_dcf_runs_breakdown
Revises: 0017_management_commitments
Create Date: 2026-05-07
"""

from __future__ import annotations

import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0018_dcf_runs_breakdown"
down_revision: str | Sequence[str] | None = "0017_management_commitments"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_cols = {c["name"] for c in inspector.get_columns("dcf_runs")}
    if "breakdown_json" not in existing_cols:
        op.add_column("dcf_runs", sa.Column("breakdown_json", sa.Text(), nullable=True))

    rows = bind.execute(
        sa.text(
            "SELECT id, ticker, segment_name, base_revenue, revenue_growths_json, "
            "fcf_margin, wacc, terminal_growth, npv, npv_per_share, horizon_years, "
            "valuation_date "
            "FROM dcf_runs ORDER BY ticker, valuation_date DESC, id DESC"
        )
    ).fetchall()

    by_ticker: dict[str, dict[str, list]] = {}
    for r in rows:
        b = by_ticker.setdefault(r.ticker, {"agg": [], "seg": []})
        if r.segment_name is None:
            b["agg"].append(r)
        else:
            b["seg"].append(r)

    for ticker, parts in by_ticker.items():
        agg_rows = parts["agg"]
        seg_rows = parts["seg"]
        if not seg_rows:
            continue
        if not agg_rows:
            continue
        keep = agg_rows[0]
        consolidated_npv = float(keep.npv)

        segment_components: list[dict[str, object]] = []
        sum_segment_npv = 0.0
        for s in seg_rows:
            try:
                growths = json.loads(s.revenue_growths_json or "[]")
            except (TypeError, ValueError):
                growths = []
            seg_npv = float(s.npv)
            sum_segment_npv += seg_npv
            segment_components.append(
                {
                    "component_name": s.segment_name,
                    "component_type": "segment",
                    "base_revenue": float(s.base_revenue),
                    "revenue_growths": growths,
                    "fcf_margin": float(s.fcf_margin),
                    "wacc": float(s.wacc),
                    "terminal_growth": float(s.terminal_growth),
                    "periods_per_year": _infer_periods_per_year(len(growths), s.horizon_years),
                    "npv": seg_npv,
                    "npv_per_share": float(s.npv_per_share) if s.npv_per_share is not None else None,
                }
            )

        overhead_npv = consolidated_npv - sum_segment_npv
        overhead_component = {
            "component_name": "overhead",
            "component_type": "overhead",
            "base_revenue": None,
            "revenue_growths": [],
            "fcf_margin": None,
            "wacc": float(keep.wacc),
            "terminal_growth": float(keep.terminal_growth),
            "periods_per_year": _infer_periods_per_year(
                len(json.loads(keep.revenue_growths_json or "[]")), keep.horizon_years
            ),
            "npv": overhead_npv,
            "npv_per_share": None,
            "note": "consolidated_minus_segments",
        }

        breakdown = segment_components + [overhead_component]
        bind.execute(
            sa.text("UPDATE dcf_runs SET breakdown_json = :bj WHERE id = :id"),
            {"bj": json.dumps(breakdown), "id": keep.id},
        )
        ids_to_delete = [s.id for s in seg_rows]
        bind.execute(
            sa.text("DELETE FROM dcf_runs WHERE id IN :ids").bindparams(
                sa.bindparam("ids", expanding=True)
            ),
            {"ids": ids_to_delete},
        )

    bind.execute(
        sa.text(
            "DELETE FROM dcf_runs WHERE id NOT IN ("
            "SELECT id FROM ("
            "SELECT id, ticker, "
            "ROW_NUMBER() OVER (PARTITION BY ticker ORDER BY valuation_date DESC, id DESC) AS rn "
            "FROM dcf_runs WHERE segment_name IS NULL"
            ") WHERE rn = 1) "
            "AND segment_name IS NULL"
        )
    )

    inspector = sa.inspect(bind)
    existing_indexes = {ix["name"] for ix in inspector.get_indexes("dcf_runs")}
    if "uq_dcf_runs_ticker" not in existing_indexes:
        op.create_index("uq_dcf_runs_ticker", "dcf_runs", ["ticker"], unique=True)


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    existing_indexes = {ix["name"] for ix in inspector.get_indexes("dcf_runs")}
    if "uq_dcf_runs_ticker" in existing_indexes:
        op.drop_index("uq_dcf_runs_ticker", table_name="dcf_runs")
    existing_cols = {c["name"] for c in inspector.get_columns("dcf_runs")}
    if "breakdown_json" in existing_cols:
        with op.batch_alter_table("dcf_runs") as batch:
            batch.drop_column("breakdown_json")


def _infer_periods_per_year(growths_len: int, horizon_years: int | None) -> int:
    """Heuristic: dcf_runs.horizon_years is actually the growth-period count, not years.

    `run_quarterly_segment_dcf.py` builds 40-step growths (10y × 4q); annual
    runs use ~5–10. So a count >= 16 strongly implies a quarterly cadence.
    """
    candidate = max(growths_len, horizon_years or 0)
    return 4 if candidate >= 16 else 1
