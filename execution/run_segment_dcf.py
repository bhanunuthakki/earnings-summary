"""Run a per-segment DCF valuation and persist one dcf_runs row per segment.

Reads each segment's latest FY revenue from segment_facts, applies the user's
per-segment growth assumptions and FCF margins, and discounts at a single
WACC + terminal growth (consistent across segments). The total NPV is the
sum of per-segment NPVs.

Usage:
    python execution/run_segment_dcf.py --ticker GOOG --assumptions GOOG_segments.json

Where GOOG_segments.json looks like:
    {
        "wacc": 0.09,
        "terminal_growth": 0.025,
        "metric": "revenue_by_product",
        "notes": "10y projection, baseline",
        "segments": {
            "Google Search & Other": {
                "growths": [0.08, 0.07, 0.06, 0.05, 0.04, 0.04, 0.03, 0.03, 0.03, 0.03],
                "fcf_margin": 0.32
            },
            "Google Cloud": {
                "growths": [0.30, 0.28, 0.25, 0.22, 0.20, 0.18, 0.15, 0.12, 0.10, 0.08],
                "fcf_margin": 0.18
            }
        }
    }
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from compute.dcf import run_segment_dcf_for_ticker  # noqa: E402
from pipeline.queries import open_db  # noqa: E402


def _load_assumptions(path: Path) -> dict[str, object]:
    with open(path, encoding="utf-8") as f:
        payload = json.load(f)
    if not isinstance(payload, dict):
        raise ValueError(f"Assumptions file must be a JSON object, got {type(payload).__name__}")
    for key in ("wacc", "terminal_growth", "segments"):
        if key not in payload:
            raise ValueError(f"Assumptions file missing required key: {key!r}")
    if not isinstance(payload["segments"], dict):
        raise ValueError(
            "'segments' must be an object mapping segment_name -> {growths, fcf_margin}"
        )
    return payload


def _split_segment_specs(
    segments: dict[str, dict[str, object]],
) -> tuple[dict[str, list[float]], dict[str, float]]:
    growths: dict[str, list[float]] = {}
    margins: dict[str, float] = {}
    for name, spec in segments.items():
        if not isinstance(spec, dict):
            raise ValueError(f"Segment {name!r} must be an object with 'growths' and 'fcf_margin'")
        g = spec.get("growths")
        m = spec.get("fcf_margin")
        if not isinstance(g, list) or not all(isinstance(x, (int, float)) for x in g):
            raise ValueError(f"Segment {name!r} 'growths' must be a list of numbers")
        if not isinstance(m, (int, float)):
            raise ValueError(f"Segment {name!r} 'fcf_margin' must be a number")
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
        agg = run_segment_dcf_for_ticker(
            conn,
            ticker=args.ticker,
            segment_growths=growths,
            segment_fcf_margins=margins,
            wacc=wacc,
            terminal_growth=terminal_growth,
            notes=str(notes) if notes is not None else None,
            metric=metric,
        )
    except ValueError as e:
        print(json.dumps({"ticker": args.ticker.upper(), "error": str(e)}, indent=2))
        return 1
    finally:
        conn.close()

    output = {
        "ticker": agg.ticker,
        "dcf_run_id": agg.row_id,
        "wacc": wacc,
        "terminal_growth": terminal_growth,
        "metric": metric,
        "horizon_years": len(next(iter(growths.values()))),
        "components": [
            {
                "component_name": c.component_name,
                "component_type": c.component_type,
                "base_revenue_billions": c.inputs.base_revenue / 1e9,
                "fcf_margin": c.inputs.fcf_margin,
                "growths": c.inputs.revenue_growths,
                "npv_billions": c.result.npv / 1e9,
            }
            for c in agg.components
        ],
        "aggregate_npv_billions": agg.aggregate_npv / 1e9,
        "aggregate_npv_per_share": agg.aggregate_npv_per_share,
    }
    print(json.dumps(output, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
