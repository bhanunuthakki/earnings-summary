"""Run a per-segment DCF at quarterly granularity (40-period default horizon).

Reads each segment's TTM revenue (sum of last 4 quarters) from segment_facts,
applies per-quarter growth assumptions and a per-segment FCF margin, and
discounts at the annualized WACC (converted internally to per-quarter).

Quarterly granularity captures the actual cadence at which segments compound,
useful when growth is changing fast quarter-over-quarter (e.g. GOOG Cloud).

Usage:
    python execution/run_quarterly_segment_dcf.py --ticker GOOG \\
        --assumptions examples/dcf/GOOG_segments_quarterly.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from compute.dcf import run_quarterly_segment_dcf_for_ticker  # noqa: E402
from pipeline.queries import open_db  # noqa: E402


def _load_assumptions(path: Path) -> dict[str, object]:
    with open(path, encoding="utf-8") as f:
        payload = json.load(f)
    if not isinstance(payload, dict):
        raise ValueError(f"Assumptions must be a JSON object, got {type(payload).__name__}")
    for key in ("wacc", "terminal_growth", "segments"):
        if key not in payload:
            raise ValueError(f"Missing required key: {key!r}")
    if not isinstance(payload["segments"], dict):
        raise ValueError("'segments' must map segment_name -> {growths, fcf_margin}")
    return payload


def _split_segment_specs(
    segments: dict[str, dict[str, object]], expected_periods: int | None = None
) -> tuple[dict[str, list[float]], dict[str, float]]:
    growths: dict[str, list[float]] = {}
    margins: dict[str, float] = {}
    for name, spec in segments.items():
        if not isinstance(spec, dict):
            raise ValueError(f"Segment {name!r} must be an object")
        g = spec.get("growths")
        m = spec.get("fcf_margin")
        if not isinstance(g, list) or not all(isinstance(x, (int, float)) for x in g):
            raise ValueError(f"Segment {name!r} 'growths' must be a list of numbers")
        if not isinstance(m, (int, float)):
            raise ValueError(f"Segment {name!r} 'fcf_margin' must be a number")
        if expected_periods is not None and len(g) != expected_periods:
            raise ValueError(
                f"Segment {name!r} has {len(g)} growth periods; expected {expected_periods}"
            )
        growths[name] = [float(x) for x in g]
        margins[name] = float(m)
    return growths, margins


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ticker", required=True)
    parser.add_argument("--assumptions", required=True, type=Path, help="Path to JSON assumptions")
    parser.add_argument(
        "--db", default=str(PROJECT_ROOT / "data" / "portfolio.db"), help="Path to portfolio.db"
    )
    args = parser.parse_args()

    payload = _load_assumptions(args.assumptions)
    growths, margins = _split_segment_specs(payload["segments"])
    wacc = float(payload["wacc"])
    terminal_growth = float(payload["terminal_growth"])
    metric = str(payload.get("metric", "revenue_by_product"))
    notes = payload.get("notes")

    conn = open_db(args.db)
    try:
        rows = run_quarterly_segment_dcf_for_ticker(
            conn,
            ticker=args.ticker,
            segment_quarterly_growths=growths,
            segment_fcf_margins=margins,
            wacc=wacc,
            terminal_growth=terminal_growth,
            notes=str(notes) if notes is not None else None,
            metric=metric,
        )
    finally:
        conn.close()

    if not rows:
        print(
            json.dumps(
                {"ticker": args.ticker.upper(), "warning": "no segments matched assumptions"},
                indent=2,
            )
        )
        return 1

    total_npv = sum(r.result.npv for r in rows)
    horizon_quarters = len(next(iter(growths.values())))
    output = {
        "ticker": args.ticker.upper(),
        "wacc_annualized": wacc,
        "terminal_growth_annualized": terminal_growth,
        "metric": metric,
        "horizon_quarters": horizon_quarters,
        "horizon_years": horizon_quarters / 4,
        "segments": [
            {
                "segment_name": r.segment_name,
                "dcf_run_id": r.row_id,
                "ttm_base_revenue_billions": r.inputs.base_revenue / 1e9,
                "fcf_margin": r.inputs.fcf_margin,
                "first_year_growth_avg": (
                    sum(r.inputs.revenue_growths[:4]) / 4
                    if len(r.inputs.revenue_growths) >= 4
                    else None
                ),
                "npv_billions": r.result.npv / 1e9,
            }
            for r in rows
        ],
        "total_npv_billions": total_npv / 1e9,
    }
    print(json.dumps(output, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
