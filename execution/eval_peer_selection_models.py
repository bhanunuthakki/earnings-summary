"""Multi-model comparison for the peer_selection LLM call.

Runs all 10 golden cases through 4 model/backend combinations and reports
recall, pass-rate, precision, latency, and parse-failures so the right model
can be pinned in LLM_MODELS["peer_selection"].

Usage:
    python execution/eval_peer_selection_models.py
    python execution/eval_peer_selection_models.py --force-budget-bypass

Output:
    stdout  — progress + summary table
    .tmp/peer_selection_model_eval.md  — Markdown report with per-case matrix
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path
from typing import NamedTuple

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from compute.peer_selection import suggest_peers  # noqa: E402
from evals.peer_selection import (  # noqa: E402
    PeerCase,
    load_peer_selection_golden,
)
from llm.structured import StructuredParseError  # noqa: E402

GOLDEN_PATH = PROJECT_ROOT / "evals" / "golden" / "peer_selection.json"


class ModelSpec(NamedTuple):
    label: str
    model: str
    backend: str | None  # None → claude


MODELS: list[ModelSpec] = [
    ModelSpec("claude-sonnet-4-6 (baseline)", "claude-sonnet-4-6", None),
    ModelSpec("claude-opus-4-8", "claude-opus-4-8", None),
    ModelSpec("gemini-3.1-pro-preview", "gemini-3.1-pro-preview", "gemini"),
    ModelSpec("gemini-2.5-flash", "gemini-2.5-flash", "gemini"),
]


class CaseDetail(NamedTuple):
    case_id: str
    ticker: str
    recall: float
    precision: float
    passed: bool
    latency_ms: int
    parse_fail: bool
    returned: list[str]
    expected: list[str]


class ModelResult(NamedTuple):
    spec: ModelSpec
    avg_recall: float
    pass_rate: float
    avg_precision: float
    avg_latency_ms: float
    parse_failures: int
    cases: list[CaseDetail]


def _make_suggest_fn(spec: ModelSpec, *, force_budget_bypass: bool) -> type[object]:
    """Return a suggest_fn (PeerCase → list[str]) for the given model spec."""

    def suggest_fn(case: PeerCase) -> list[str]:
        from llm.cli import is_hard_stop

        try:
            results = suggest_peers(
                ticker=case.ticker,
                name=case.name or None,
                business_description=case.business_description,
                segments=list(case.segments),
                sector=case.sector,
                industry=case.industry,
                model=spec.model,
                backend=spec.backend,
                max_peers=10,
            )
            return [s.ticker for s in results]
        except Exception as exc:
            if is_hard_stop(exc):
                raise  # budget/setup: propagate out of the run
            print(f"    [warn] {type(exc).__name__}: {str(exc)[:120]}")
            return []

    return suggest_fn  # type: ignore[return-value]


def run_model(
    spec: ModelSpec,
    cases: list[PeerCase],
    *,
    force_budget_bypass: bool,
) -> ModelResult:
    print(f"\n{'=' * 60}")
    print(f"Running: {spec.label}  (backend={spec.backend or 'claude'})")
    print(f"{'=' * 60}")
    suggest_fn = _make_suggest_fn(spec, force_budget_bypass=force_budget_bypass)

    total_recall = 0.0
    total_precision = 0.0
    total_latency = 0
    passed_count = 0
    parse_failures = 0
    case_details: list[CaseDetail] = []

    for case in cases:
        t0 = time.monotonic()
        try:
            returned_raw = suggest_fn(case)  # type: ignore[call-arg]
            latency_ms = int((time.monotonic() - t0) * 1000)
            returned = [t.strip().upper() for t in returned_raw if t.strip()]
            returned_set = frozenset(returned)
            expected_set = case.expected_peers
            hits = returned_set & expected_set
            recall = len(hits) / len(expected_set) if expected_set else 0.0
            acceptable = expected_set | case.also_valid
            precision = len(returned_set & acceptable) / len(returned_set) if returned_set else 0.0
            passed = recall >= 0.5
            parse_fail = not returned_raw and True if True else False
        except StructuredParseError:
            latency_ms = int((time.monotonic() - t0) * 1000)
            returned = []
            recall = 0.0
            precision = 0.0
            passed = False
            parse_fail = True
            parse_failures += 1

        total_recall += recall
        total_precision += precision
        total_latency += latency_ms
        if passed:
            passed_count += 1

        detail = CaseDetail(
            case_id=case.case_id,
            ticker=case.ticker,
            recall=recall,
            precision=precision,
            passed=passed,
            latency_ms=latency_ms,
            parse_fail=parse_fail,
            returned=returned,
            expected=sorted(case.expected_peers),
        )
        case_details.append(detail)
        status = "PASS" if passed else "FAIL"
        print(
            f"  [{status}] {case.ticker:<6}  recall={recall:.2f}  prec={precision:.2f}"
            f"  {latency_ms:>5}ms"
            + (f"  returned={returned[:3]}" if returned else "  (no suggestions)")
        )

    n = len(cases)
    avg_recall = total_recall / n if n else 0.0
    pass_rate = passed_count / n if n else 0.0
    avg_precision = total_precision / n if n else 0.0
    avg_latency = total_latency / n if n else 0.0

    print(
        f"\n  SUMMARY: avg_recall={avg_recall:.3f}  pass_rate={pass_rate:.1%}"
        f"  avg_prec={avg_precision:.3f}  avg_latency={avg_latency:.0f}ms"
        f"  parse_failures={parse_failures}"
    )

    return ModelResult(
        spec=spec,
        avg_recall=avg_recall,
        pass_rate=pass_rate,
        avg_precision=avg_precision,
        avg_latency_ms=avg_latency,
        parse_failures=parse_failures,
        cases=case_details,
    )


def pick_winner(results: list[ModelResult]) -> tuple[ModelResult, str]:
    """Choose the recommended model per the model-downgrade-loop philosophy:
    if Sonnet is at parity with anything better, keep Sonnet (cheaper).
    If Gemini wins, flag for owner sign-off rather than silent routing."""
    baseline = next(r for r in results if "sonnet-4-6" in r.spec.model and r.spec.backend is None)
    winner = max(results, key=lambda r: (r.avg_recall, r.pass_rate, -r.avg_latency_ms))

    parity_delta = 0.03  # recall diff ≤ 3pp → treat as parity

    if abs(winner.avg_recall - baseline.avg_recall) <= parity_delta:
        winner = baseline
        rationale = (
            f"Sonnet (baseline) is AT PARITY with the field "
            f"(best recall={max(r.avg_recall for r in results):.3f}, "
            f"baseline={baseline.avg_recall:.3f}, delta<=3pp). "
            "KEEP Sonnet — cheaper at equal quality (model-downgrade-loop philosophy)."
        )
    elif winner.spec.backend == "gemini":
        rationale = (
            f"Gemini model '{winner.spec.model}' wins on recall "
            f"(recall={winner.avg_recall:.3f} vs Sonnet={baseline.avg_recall:.3f}). "
            "DO NOT silently route production — recommend adding 'peer_selection' to "
            "GEMINI_BACKEND_ALLOWED_PURPOSES + GEMINI_MODELS pin for owner sign-off "
            "(directives/gemini_backend.md). Sub-cent metered API cost at "
            "superior recall is a strong cost win."
        )
    else:
        rationale = (
            f"Claude model '{winner.spec.model}' wins on recall "
            f"(recall={winner.avg_recall:.3f} vs Sonnet={baseline.avg_recall:.3f}). "
            f"Update LLM_MODELS['peer_selection'] = '{winner.spec.model}'."
        )

    return winner, rationale


def build_report(results: list[ModelResult], winner: ModelResult, rationale: str) -> str:
    lines: list[str] = []
    lines.append("# peer_selection model eval — Claude vs Gemini")
    lines.append("")
    lines.append("## Summary table")
    lines.append("")
    lines.append(
        "| Model | Backend | Avg Recall | Pass-rate | Avg Precision | Avg Latency ms | Parse Failures |"
    )
    lines.append("|---|---|---|---|---|---|---|")
    for r in results:
        marker = " ← WINNER" if r.spec.label == winner.spec.label else ""
        lines.append(
            f"| {r.spec.label}{marker} | {r.spec.backend or 'claude'} "
            f"| {r.avg_recall:.3f} | {r.pass_rate:.1%} "
            f"| {r.avg_precision:.3f} | {r.avg_latency_ms:.0f} "
            f"| {r.parse_failures} |"
        )
    lines.append("")
    lines.append("## Per-case recall matrix")
    lines.append("")

    # header
    model_labels = [r.spec.label[:24] for r in results]
    header = "| Case / Ticker |" + "".join(f" {lbl} |" for lbl in model_labels)
    sep = "|---|" + "---|" * len(results)
    lines.append(header)
    lines.append(sep)

    # rows — index by (ticker, case_id)
    case_ids = [c.case_id for c in results[0].cases]
    for i, case_id in enumerate(case_ids):
        ticker = results[0].cases[i].ticker
        row = f"| {case_id} ({ticker}) |"
        for r in results:
            detail = r.cases[i]
            cell = f"{detail.recall:.2f}" + ("✓" if detail.passed else "✗")
            row += f" {cell} |"
        lines.append(row)

    lines.append("")
    lines.append("## Recommendation")
    lines.append("")
    lines.append(rationale)
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument(
        "--force-budget-bypass",
        action="store_true",
        help="skip per-purpose budget caps (eval escape hatch)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="run only the first N cases (smoke mode)",
    )
    args = parser.parse_args()

    if args.force_budget_bypass:
        os.environ["SWEEP_FORCE_BUDGET_BYPASS"] = "1"

    cases = load_peer_selection_golden(GOLDEN_PATH)
    if args.limit:
        cases = cases[: args.limit]

    print(f"Loaded {len(cases)} cases from {GOLDEN_PATH.name}")
    print(f"Models to run: {len(MODELS)}")
    print(f"Total calls: {len(cases) * len(MODELS)}")

    results: list[ModelResult] = []
    for spec in MODELS:
        result = run_model(spec, cases, force_budget_bypass=args.force_budget_bypass)
        results.append(result)

    print("\n" + "=" * 70)
    print("FINAL RESULTS")
    print("=" * 70)
    print(f"{'Model':<35} {'Recall':>8} {'Pass%':>8} {'Prec':>8} {'Lat ms':>8} {'ParseFail':>10}")
    print("-" * 80)
    for r in sorted(results, key=lambda x: -x.avg_recall):
        print(
            f"{r.spec.label:<35} {r.avg_recall:>8.3f} {r.pass_rate:>8.1%}"
            f" {r.avg_precision:>8.3f} {r.avg_latency_ms:>8.0f} {r.parse_failures:>10}"
        )

    winner, rationale = pick_winner(results)

    print("\n" + "=" * 70)
    print("RECOMMENDATION")
    print("=" * 70)
    print(rationale)

    out_dir = PROJECT_ROOT / ".tmp"
    out_dir.mkdir(exist_ok=True)
    report = build_report(results, winner, rationale)
    out_path = out_dir / "peer_selection_model_eval.md"
    out_path.write_text(report, encoding="utf-8")
    print(f"\nReport written → {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
