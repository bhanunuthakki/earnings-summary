"""Preview new chart primitives with real META + AMZN data.

Writes a self-contained HTML to .tmp/charts_preview.html. Open it in
a browser to evaluate the new building blocks side-by-side with the
existing charts.py output.

Run:  python scratch/preview_charts_v2.py
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

# Make src/ importable when running from worktree root.
HERE = Path(__file__).resolve().parent
WORKTREE = HERE.parent
sys.path.insert(0, str(WORKTREE / "src"))

# Repo's data dir lives outside the worktree.
DB_PATH = WORKTREE.parent.parent.parent / "data" / "portfolio.db"
OUT_PATH = WORKTREE / ".tmp" / "charts_preview.html"

from report.renderers.charts_v2 import (  # noqa: E402
    CSS,
    BarSpec,
    LineSeries,
    MatrixRow,
    bar_chart,
    multi_line_chart,
    paired_chart,
    qoq_delta_bars,
    stacked_area,
    stacked_area_100pct,
    yoy_heatmap_table,
)


def quarter_label(period_end: str) -> str:
    """MBI-style compact quarter label: Q3'25 not '2025 Q3'."""
    y = int(period_end[:4])
    m = int(period_end[5:7])
    q = (m - 1) // 3 + 1
    return f"Q{q}'{str(y)[2:]}"


def load_quarterly(ticker: str, n: int = 16):
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT period_end, revenue, gross_profit, operating_income, net_income,
               operating_cash_flow, free_cash_flow, capex, eps_diluted
        FROM metrics
        WHERE ticker = ? AND fiscal_period_type IN ('Q1','Q2','Q3','Q4')
        ORDER BY period_end DESC LIMIT ?
        """,
        (ticker, n + 4),  # +4 for YoY lookback
    ).fetchall()
    conn.close()
    rows = list(reversed(rows))
    # Dedupe to one row per period_end (metrics view sometimes has both quarterly + Q1 buckets).
    seen = {}
    for r in rows:
        seen.setdefault(r["period_end"], dict(r))
        for k in r.keys():
            if seen[r["period_end"]].get(k) is None and r[k] is not None:
                seen[r["period_end"]][k] = r[k]
    rows = list(seen.values())[-n:]
    return rows


def load_segments(ticker: str, metric: str, n: int = 12):
    if metric == "revenue_by_product":
        dim_type, junction_metric = ("product", "revenue")
    elif metric == "revenue_by_geography":
        dim_type, junction_metric = ("geography", "revenue")
    elif metric == "operating_income":
        dim_type, junction_metric = ("business_unit", "operating_income")
    else:
        dim_type, junction_metric = ("business_unit", metric)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT sp.period_end AS period_end,
               sd.dim_name AS segment_name,
               sd.value AS value
        FROM segment_periods sp
        JOIN segment_dimensions sd ON sd.period_id = sp.id
        WHERE sp.ticker = ?
          AND sd.dim_type = ?
          AND sd.metric = ?
          AND sp.fiscal_period_type IN ('Q1','Q2','Q3','Q4')
        ORDER BY sp.period_end ASC
        """,
        (ticker, dim_type, junction_metric),
    ).fetchall()
    conn.close()
    periods, by_seg = [], {}
    for r in rows:
        p = str(r["period_end"])[:10]
        if p not in periods:
            periods.append(p)
        by_seg.setdefault(r["segment_name"], {})[p] = float(r["value"])
    periods = periods[-n:]
    series = []
    for name, vals in by_seg.items():
        ser = [vals.get(p) for p in periods]
        if any(v is not None for v in ser):
            series.append((name, ser))
    return periods, series


def yoy_series(levels: list[float | None]) -> list[float | None]:
    out: list[float | None] = []
    for i, v in enumerate(levels):
        if i < 4 or v is None or levels[i - 4] in (None, 0):
            out.append(None)
        else:
            prior = levels[i - 4]
            assert prior is not None
            out.append((v / prior - 1) * 100)
    return out


def main() -> None:
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)

    # ---- META data ----
    meta_q = load_quarterly("META", n=16)
    meta_labels_full = [quarter_label(r["period_end"]) for r in meta_q]
    meta_rev = [r["revenue"] for r in meta_q]
    meta_oi = [r["operating_income"] for r in meta_q]
    meta_fcf = [r["free_cash_flow"] for r in meta_q]
    meta_capex = [r["capex"] for r in meta_q]
    meta_gp = [r["gross_profit"] for r in meta_q]

    # Compute margins.
    meta_gross_margin = [
        (gp / rev * 100) if (gp is not None and rev) else None
        for gp, rev in zip(meta_gp, meta_rev)
    ]
    meta_op_margin = [
        (oi / rev * 100) if (oi is not None and rev) else None
        for oi, rev in zip(meta_oi, meta_rev)
    ]
    meta_fcf_margin = [
        (fcf / rev * 100) if (fcf is not None and rev) else None
        for fcf, rev in zip(meta_fcf, meta_rev)
    ]
    meta_capex_pct = [
        (abs(cx) / rev * 100) if (cx is not None and rev) else None
        for cx, rev in zip(meta_capex, meta_rev)
    ]

    # Truncate display to last 12 quarters (YoY lookback uses the full 16).
    DISP = 12
    labels = meta_labels_full[-DISP:]
    rev_disp = meta_rev[-DISP:]
    oi_disp = meta_oi[-DISP:]
    fcf_disp = meta_fcf[-DISP:]
    rev_yoy = yoy_series(meta_rev)[-DISP:]
    oi_yoy = yoy_series(meta_oi)[-DISP:]
    fcf_yoy = yoy_series(meta_fcf)[-DISP:]

    # Segments — geography & product.
    geo_periods, geo_series = load_segments("META", "revenue_by_geography", n=12)
    prod_periods, prod_series = load_segments("META", "revenue_by_product", n=12)
    geo_labels = [quarter_label(p) for p in geo_periods]
    prod_labels = [quarter_label(p) for p in prod_periods]
    # Sort segments by latest value desc.
    geo_series.sort(key=lambda kv: -(kv[1][-1] or 0))
    prod_series.sort(key=lambda kv: -(kv[1][-1] or 0))

    # ---- Render ----
    blocks: list[str] = []

    def section(title: str, why: str, body: str) -> str:
        return (
            f'<section class="preview-section">'
            f'<h2>{title}</h2>'
            f'<p class="why">{why}</p>'
            f'{body}'
            f'</section>'
        )

    # 1. Bar chart — YoY %
    blocks.append(section(
        "Primitive 1 — Bar chart (YoY %)",
        "Every bar labeled inline. Green = positive growth, red = negative. "
        "Zero baseline always visible. This replaces the existing line charts for any rate/growth metric.",
        bar_chart(BarSpec(
            values=rev_yoy,
            labels=labels,
            title="META Revenue YoY %",
            width=720,
            height=240,
            value_fmt="pct",
            signed_color=True,
        )),
    ))

    # 2. Bar chart — level (zero-based)
    blocks.append(section(
        "Primitive 2 — Bar chart (level, zero-based)",
        "Same primitive used for absolute $ levels. Note the chart starts at $0 — the relative "
        "size of each bar now correctly represents the actual magnitude of revenue. Labels in $B inline.",
        bar_chart(BarSpec(
            values=rev_disp,
            labels=labels,
            title="META Revenue ($)",
            width=720,
            height=240,
            value_fmt="dollar",
            signed_color=False,
        )),
    ))

    # 3. Paired chart — level + YoY% side by side
    blocks.append(section(
        "Primitive 3 — Paired chart (level + YoY%)",
        "Both views at once. Left tells you magnitude (where the business is), right tells you "
        "direction (how fast it's growing). This is the proposed default for §3 priority metrics.",
        paired_chart(
            level_values=rev_disp,
            yoy_values=rev_yoy,
            labels=labels,
            title="META Revenue",
            level_fmt="dollar",
        )
        + paired_chart(
            level_values=oi_disp,
            yoy_values=oi_yoy,
            labels=labels,
            title="META Operating Income",
            level_fmt="dollar",
        )
        + paired_chart(
            level_values=fcf_disp,
            yoy_values=fcf_yoy,
            labels=labels,
            title="META Free Cash Flow",
            level_fmt="dollar",
        ),
    ))

    # 4. Multi-line chart — OI + FCF margins only (gross margin is too high-range to share axis)
    blocks.append(section(
        "Primitive 4 — Multi-line chart (OI + FCF margin overlaid)",
        "Operating and FCF margin on one panel. Gross margin (~82% for META) lives at a "
        "different magnitude than these two (~25-50%), so sharing an axis compresses the "
        "analytically interesting movement. Gross margin gets its own single-line view if needed.",
        multi_line_chart(
            series=[
                LineSeries(name="Operating margin", values=meta_op_margin[-DISP:]),
                LineSeries(name="FCF margin", values=meta_fcf_margin[-DISP:]),
            ],
            labels=labels,
            title="META operating + FCF margin (%)",
            width=720,
            height=260,
            value_fmt="pct",
        ),
    ))

    # 5. Capex / Revenue % chart (replaces capex level chart)
    blocks.append(section(
        "Primitive 5 — Capex/Revenue %",
        "Replaces the current Capex level chart (which has the inverted-axis problem). "
        "Capex as a % of revenue is the analytically interesting view.",
        bar_chart(BarSpec(
            values=meta_capex_pct[-DISP:],
            labels=labels,
            title="META Capex / Revenue (%)",
            width=720,
            height=240,
            value_fmt="pct",
            signed_color=False,
            bar_color="#0173b2",
        )),
    ))

    # 6. Stacked area — segments (absolute $ levels)
    blocks.append(section(
        "Primitive 6 — Stacked area (segment mix-shift, absolute $)",
        "Geographic revenue mix over 12 quarters. The width of each band shows mix at "
        "any quarter; the changing thickness shows shift. End-of-band labels show the latest mix %.",
        stacked_area(
            series=[LineSeries(name=name, values=vals) for name, vals in geo_series],
            labels=geo_labels,
            title="META revenue by geography ($)",
            width=900,
            height=300,
        ),
    ))

    # 6b. 100%-stacked area — pure mix view
    blocks.append(section(
        "Primitive 6b — 100%-stacked area (pure mix-shift)",
        "Same data, normalized to 100% at every quarter. Hides the absolute growth and "
        "shows only CONCENTRATION change — useful for seeing whether a segment is gaining "
        "or losing share independent of overall growth.",
        stacked_area_100pct(
            series=[LineSeries(name=name, values=vals) for name, vals in geo_series],
            labels=geo_labels,
            title="META revenue by geography — share of total (%)",
            width=900,
            height=280,
        ),
    ))

    # 7. YoY heatmap matrix
    rows_full = [
        MatrixRow(name="Revenue", levels=meta_rev),
        MatrixRow(name="Gross profit", levels=meta_gp),
        MatrixRow(name="Operating income", levels=meta_oi),
        MatrixRow(name="Net income", levels=[r["net_income"] for r in meta_q]),
        MatrixRow(name="Operating CF", levels=[r["operating_cash_flow"] for r in meta_q]),
        MatrixRow(name="Free cash flow", levels=meta_fcf),
        MatrixRow(name="EPS (diluted)", levels=[r["eps_diluted"] for r in meta_q]),
    ]
    blocks.append(section(
        "Primitive 7 — YoY% matrix with heat shading + CAGR columns",
        "Dense scannable grid: every cell shows YoY% growth. Heat shading (green/red) shows "
        "accel/decel at a glance without reading numbers. Right columns show 1y/2y/3y trailing CAGR "
        "computed from the absolute levels.",
        yoy_heatmap_table(
            rows=rows_full,
            periods=meta_labels_full,
            title="META — YoY % growth matrix",
            display_quarters=12,
            cagr_periods=(4, 8, 12),
        ),
    ))

    # 8. QoQ $-delta bars (MBI page 6 style)
    blocks.append(section(
        "Primitive 8 — QoQ $-delta bars (incremental investment view)",
        "How many dollars of revenue were added each quarter compared to the prior quarter. "
        "The MBI 'AWS QoQ revenue growth ($M)' chart on page 6 of your example.",
        qoq_delta_bars(
            levels=meta_rev,
            labels=meta_labels_full,
            title="META QoQ revenue delta ($)",
            width=900,
            height=260,
        ),
    ))

    # Render full HTML.
    html_out = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>Chart primitives preview</title>
<style>
  body {{ font-family: 'Inter', -apple-system, sans-serif; max-width: 1280px; margin: 24px auto; padding: 0 20px; color: #1a1f2e; }}
  h1 {{ font-size: 20px; margin-bottom: 8px; }}
  .lede {{ color: #67737d; font-size: 13px; margin-bottom: 24px; }}
  .preview-section {{ margin: 28px 0; padding: 18px; border: 1px solid #e3e7eb; border-radius: 6px; background: #fff; }}
  .preview-section h2 {{ font-size: 15px; margin: 0 0 6px 0; }}
  .why {{ color: #67737d; font-size: 12.5px; margin: 0 0 14px 0; line-height: 1.5; }}
  {CSS}
</style></head><body>
<h1>Chart primitives preview — META data through {meta_labels_full[-1]}</h1>
<p class="lede">8 building blocks rendered with real META quarterly data from data/portfolio.db.
Goal: validate the visual encoding before integrating into §3 / §4 of the unified brief.
All charts are hand-rolled SVG — no JS, no dependencies, prints clean, screenshots clean.</p>
{"".join(blocks)}
</body></html>"""

    OUT_PATH.write_text(html_out, encoding="utf-8")
    print(f"Wrote {OUT_PATH}")
    print(f"Open in browser: file:///{OUT_PATH.as_posix()}")


if __name__ == "__main__":
    main()
