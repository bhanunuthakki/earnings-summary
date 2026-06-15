"""
execution/run_8k_extraction_eval.py
-----------------------------------
Golden-set eval for the 8-K segment extractor (``src/provenance/edgar_8k.py``).
For each case in ``evals/golden/extract_8k_overrides.json`` it runs the REAL LLM
extraction over the recorded exhibit text and scores the result against the
expected segment map. Spends real tokens — run it manually / on the weekly eval
cadence, NOT in CI. Exit code is non-zero if any case scores below ``--threshold``.

This gates promoting the extractor to a cheaper backend (e.g. Gemini Flash): only
swap the ``extract_8k_overrides`` model in ``llm.cli.LLM_MODELS`` once this passes.

Usage:
  python execution/run_8k_extraction_eval.py [--threshold 0.95]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import cast

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)
SRC_DIR = os.path.join(PROJECT_ROOT, "src")
sys.path.append(SRC_DIR)

from provenance import edgar_8k  # noqa: E402

GOLDEN = os.path.join(PROJECT_ROOT, "evals", "golden", "extract_8k_overrides.json")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--threshold", type=float, default=0.95)
    parser.add_argument("--golden", default=GOLDEN)
    args = parser.parse_args()

    with open(args.golden, encoding="utf-8") as f:
        cases = cast("list[dict[str, object]]", json.load(f))

    failures = 0
    for case in cases:
        case_id = str(case.get("id"))
        extracted = edgar_8k.extract_segment_map(
            text=str(case.get("exhibit_text") or ""),
            ticker=str(case.get("ticker") or ""),
            period_end=str(case.get("period_end") or ""),
            fiscal_period_type=str(case.get("fiscal_period_type") or ""),
            dim_type=str(case.get("dim_type") or "product"),
        )
        expected_raw = case.get("expected")
        expected = (
            {
                str(k): float(cast("float", v))
                for k, v in cast("dict[str, object]", expected_raw).items()
            }
            if isinstance(expected_raw, dict)
            else {}
        )
        score = edgar_8k.score_segment_extraction(extracted, expected)
        status = "PASS" if score >= args.threshold else "FAIL"
        if status == "FAIL":
            failures += 1
        print(f"  [{status}] {case_id}: score={score:.3f} ({len(extracted)} segments extracted)")

    print(f"\n  {len(cases) - failures}/{len(cases)} cases >= {args.threshold:.2f}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
