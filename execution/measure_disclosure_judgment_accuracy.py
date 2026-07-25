"""D1e (docs/design/disclosure_intelligence_v1_prd.md): measure the accuracy
of the ALREADY-SHIPPED ``disclosure_events.verdict`` values against the hand
labels in ``evals/golden/metric_lifecycle_triage.json`` /
``evals/golden/disclosure_item_specificity_triage.json``.

This is deliberately NOT a re-run of the LLM (no spend, no quota use, fully
reproducible from the checked-in golden files): each golden case carries both
the hand label (``expected``) and the verdict actually persisted to prod at
labeling time (``prod_verdict``, a frozen receipt). For metric-lifecycle
cases the hand label is a (relevance, prior) tuple, which this script maps to
the persisted verdict string using the SAME encoding
``execution/detect_discontinued_metrics.py`` uses when writing
``disclosure_events`` rows, so the comparison is apples-to-apples:

    relevance == accounting_plumbing         -> "noise"
    relevance == business_metric, prior == concealment -> "concealment"
    relevance == business_metric, prior == maturity    -> "maturity"
    relevance == business_metric, prior == unclear     -> "unclassified"

For specificity cases the hand label's ``verdict`` compares directly to
``prod_verdict`` (both share the ``boilerplate_update``/``substantive``
vocabulary).

Prints per-purpose accuracy + a confusion breakdown to stdout. Small,
plain-text output (well under the 2,000-line/100KB threshold) — nothing
written to ``.tmp/``.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import cast

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _map_metric_verdict(relevance: str, prior: str) -> str:
    if relevance == "accounting_plumbing":
        return "noise"
    if prior == "concealment":
        return "concealment"
    if prior == "maturity":
        return "maturity"
    return "unclassified"


def _load_cases(path: Path, purpose: str) -> list[dict[str, object]]:
    doc = cast("dict[str, object]", json.loads(path.read_text(encoding="utf-8")))
    if doc.get("purpose") != purpose:
        raise ValueError(f"{path}: purpose must be {purpose!r}, got {doc.get('purpose')!r}")
    cases = doc.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ValueError(f"{path}: needs a non-empty `cases` list")
    return cast("list[dict[str, object]]", cases)


def measure_metric_lifecycle(path: Path) -> dict[str, object]:
    cases = _load_cases(path, "metric_lifecycle_triage")
    n = len(cases)
    matched = 0
    confusion: Counter[tuple[str, str]] = Counter()
    concealment_hand_labeled = 0
    concealment_confirmed = 0
    mismatches: list[str] = []
    for c in cases:
        expected = cast("dict[str, object]", c["expected"])
        relevance = str(expected["relevance"])
        prior = str(expected["prior"])
        hand_mapped = _map_metric_verdict(relevance, prior)
        prod_verdict = str(c["prod_verdict"])
        confusion[(prod_verdict, hand_mapped)] += 1
        if prod_verdict == "concealment":
            concealment_hand_labeled += 1
            if hand_mapped == "concealment":
                concealment_confirmed += 1
        if hand_mapped == prod_verdict:
            matched += 1
        else:
            mismatches.append(
                f"  {c['id']}: prod={prod_verdict!r} vs hand={hand_mapped!r} "
                f"(relevance={relevance}, prior={prior})"
            )
    return {
        "purpose": "metric_lifecycle_triage",
        "n_cases": n,
        "n_matched": matched,
        "accuracy": matched / n if n else None,
        "confusion_prod_vs_hand": {f"{p}->{h}": c for (p, h), c in confusion.items()},
        "concealment_precision": (
            concealment_confirmed / concealment_hand_labeled if concealment_hand_labeled else None
        ),
        "concealment_n": concealment_hand_labeled,
        "mismatches": mismatches,
    }


def measure_specificity(path: Path) -> dict[str, object]:
    cases = _load_cases(path, "disclosure_item_specificity_triage")
    n = len(cases)
    matched = 0
    confusion: Counter[tuple[str, str]] = Counter()
    mismatches: list[str] = []
    for c in cases:
        expected = cast("dict[str, object]", c["expected"])
        hand_verdict = str(expected["verdict"])
        prod_verdict = str(c["prod_verdict"])
        confusion[(prod_verdict, hand_verdict)] += 1
        if hand_verdict == prod_verdict:
            matched += 1
        else:
            mismatches.append(f"  {c['id']}: prod={prod_verdict!r} vs hand={hand_verdict!r}")
    substantive_as_boilerplate = confusion[("boilerplate_update", "substantive")]
    boilerplate_as_substantive = confusion[("substantive", "boilerplate_update")]
    return {
        "purpose": "disclosure_item_specificity_triage",
        "n_cases": n,
        "n_matched": matched,
        "accuracy": matched / n if n else None,
        "confusion_prod_vs_hand": {f"{p}->{h}": c for (p, h), c in confusion.items()},
        # The costly direction for a research tool: a real disclosure change
        # buried under a "boilerplate_update" verdict, never surfaced.
        "n_substantive_buried_as_boilerplate": substantive_as_boilerplate,
        "n_boilerplate_flagged_as_substantive": boilerplate_as_substantive,
        "mismatches": mismatches,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--golden-dir", type=Path, default=PROJECT_ROOT / "evals" / "golden")
    args = parser.parse_args(argv)

    metric_report = measure_metric_lifecycle(args.golden_dir / "metric_lifecycle_triage.json")
    spec_report = measure_specificity(args.golden_dir / "disclosure_item_specificity_triage.json")

    print("=" * 78)
    print("metric_lifecycle_triage — accuracy of shipped verdicts vs hand labels")
    print("=" * 78)
    print(json.dumps(metric_report, indent=2))
    print()
    print("=" * 78)
    print("disclosure_item_specificity_triage — accuracy of shipped verdicts vs hand labels")
    print("=" * 78)
    print(json.dumps(spec_report, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
