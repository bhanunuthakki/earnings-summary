"""Run an LLM eval (directives/llm_evals_plan.md).

Two modes, one harness:

* mode A (live golden set) — `viewspec_compile` (compile→execute→compare→
  judge-on-divergence) and the fast classifiers `transcript_metadata` /
  `intake_classifier` / `news_structuring` (deterministic compare /
  output-contract invariants; no judge — closed-form ground truth): fresh
  outputs through the PRODUCTION call path vs the checked-in golden set.
* mode B (rubric audit) — `bear_case` / `transcript_summary` /
  `advisor_next_dollar`: judges REAL production artifacts facet-by-facet
  against the versioned rubric in evals/rubrics/<purpose>.md.

Both persist the run + per-case transcripts to eval_runs/eval_case_results
(alembic 0083) and bridge the run average into prompt_calibration_scores so
`summarize_by_prompt_version` compares prompt versions.

`--coverage` prints the eval-coverage report instead (which purposes have NO
eval mode — the analogue of the unknown-purpose model warning).
`--coverage-gate` prints the same report and applies the provider-free CI
ratchet: pre-existing registered gaps are grandfathered, but a newly registered
model/prompt purpose without a configured executable quality-eval mode fails.

Examples:
    python execution/run_llm_evals.py --purpose viewspec_compile
    python execution/run_llm_evals.py --purpose bear_case \
        --repo-root . --limit 5
    python execution/run_llm_evals.py --purpose transcript_summary --since-days 7
    python execution/run_llm_evals.py --purpose intake_classifier --no-persist
    python execution/run_llm_evals.py --purpose viewspec_compile --min-score 0.8  # gate
    python execution/run_llm_evals.py --coverage
    python execution/run_llm_evals.py --coverage-gate

Exit codes: 0 ok (including an empty mode-B audit corpus — nothing to grade)
· 1 hard failure (bad golden set/rubric, missing DB/tables, abort) · 2 capture
audit has no real production cases · 3 ran fine but avg_score below
--min-score (the regression gate) · 4 eval-coverage ratchet failure.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from evals.run_registry import (  # noqa: E402
    AUDIT_PURPOSES,
    CAPTURE_AUDIT_PURPOSES,
    RUNNABLE_PURPOSES,
)
from evals.run_registry import GOLDEN_PURPOSES as GOLDEN_PURPOSES  # noqa: E402

log = logging.getLogger("run_llm_evals")

PURPOSES = RUNNABLE_PURPOSES


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--purpose",
        default=None,
        choices=PURPOSES,
        help="Which purpose's eval to run (graders live in src/evals/). "
        "Required unless --coverage.",
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=PROJECT_ROOT,
        help="Repo root whose data/portfolio.db the eval compiles + executes "
        "against and persists into. Default: this repo.",
    )
    parser.add_argument(
        "--golden",
        type=Path,
        default=None,
        help="Mode A: override the golden-set file "
        "(default: <this repo>/evals/golden/<purpose>.json). "
        "Mode B: override the rubric file "
        "(default: <this repo>/evals/rubrics/<purpose>.md).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Grade only the first N cases (mode B corpora are newest-first).",
    )
    parser.add_argument(
        "--since-days",
        type=int,
        default=None,
        help="Mode B only: audit just the artifacts produced in the last N days "
        "(the weekly-cron scope). Ignored for golden-set purposes.",
    )
    parser.add_argument(
        "--no-persist",
        action="store_true",
        help="Skip the eval-table + calibration writes (dry run; JSON still printed).",
    )
    parser.add_argument(
        "--no-judge",
        action="store_true",
        help="Mode A only: skip judge calls — divergent cases just fail (no judge "
        "spend). Mode B is judge-driven, so this flag is rejected there.",
    )
    parser.add_argument(
        "--min-score",
        type=float,
        default=None,
        help="Gate mode: exit 3 when avg_score falls below this threshold.",
    )
    coverage_mode = parser.add_mutually_exclusive_group()
    coverage_mode.add_argument(
        "--coverage",
        action="store_true",
        help="Print the eval-coverage report (purposes with no eval mode) and exit. No LLM spend.",
    )
    coverage_mode.add_argument(
        "--coverage-gate",
        action="store_true",
        help="Print coverage and fail if a new registered model/prompt purpose "
        "lacks a real golden/audit/outcome/meta eval. No LLM spend.",
    )
    return parser.parse_args()


def _sync_db_to_repo(repo_root: Path) -> None:
    """Point the db module at --repo-root so the llm_calls ledger rows the
    eval's calls produce land in the SAME portfolio.db the eval reads and
    persists to (mirrors build_artifacts.py)."""
    import db

    db.PROJECT_ROOT = str(repo_root)
    db.DATA_DIR = str(repo_root / "data")
    db.DB_PATH = str(repo_root / "data" / "portfolio.db")


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    args = _parse_args()
    repo_root = args.repo_root.resolve()
    db_path = repo_root / "data" / "portfolio.db"

    if args.coverage or args.coverage_gate:
        # Observability only — works without a DB (registry-derived universe;
        # observed call counts just come back 0).
        from evals.coverage import (
            eval_coverage,
            eval_coverage_gate,
            render_coverage_gate_text,
            render_coverage_text,
        )

        rows = eval_coverage(db_path)
        print(render_coverage_text(rows))
        if args.coverage_gate:
            gate = eval_coverage_gate(rows)
            print()
            print(render_coverage_gate_text(gate))
            return 0 if gate.passed else 4
        return 0

    if not args.purpose:
        print("ERROR: --purpose is required (or pass --coverage).", file=sys.stderr)
        return 1
    if not db_path.exists():
        print(f"ERROR: no DB at {db_path}", file=sys.stderr)
        return 1
    _sync_db_to_repo(repo_root)

    from evals.harness import EvalAbortError, persist_summary

    try:
        if args.purpose in CAPTURE_AUDIT_PURPOSES:
            if args.no_judge:
                print(
                    "ERROR: --no-judge cannot score a capture replay; "
                    "capture audits are judge-driven.",
                    file=sys.stderr,
                )
                return 1
            if args.golden is not None:
                print(
                    "ERROR: --golden does not apply to versioned capture-quality specs.",
                    file=sys.stderr,
                )
                return 1
            from evals.capture_quality import run_capture_quality_eval

            summary = run_capture_quality_eval(
                args.purpose,
                repo_root=repo_root,
                code_root=PROJECT_ROOT,
                limit=args.limit,
                since_days=args.since_days,
            )
        elif args.purpose in AUDIT_PURPOSES:
            if args.no_judge:
                print(
                    "ERROR: --no-judge is a mode-A flag; rubric audits are judge-driven.",
                    file=sys.stderr,
                )
                return 1
            from evals.rubric_judge import run_rubric_eval

            summary = run_rubric_eval(
                args.purpose,
                db_path=db_path,
                repo_root=repo_root,
                code_root=PROJECT_ROOT,  # rubric + git sha come from the checkout under eval
                rubric_path=args.golden.resolve() if args.golden else None,
                limit=args.limit,
                since_days=args.since_days,
            )
        elif args.purpose == "viewspec_compile":
            from evals.judge import run_judge
            from evals.viewspec_compile import DEFAULT_GOLDEN_RELPATH, run_viewspec_eval

            golden_path = (args.golden or (PROJECT_ROOT / DEFAULT_GOLDEN_RELPATH)).resolve()
            summary = run_viewspec_eval(
                db_path=db_path,
                golden_path=golden_path,
                code_root=PROJECT_ROOT,  # the sha of the code/prompt under eval, not the data repo
                limit=args.limit,
                judge=None if args.no_judge else run_judge,
            )
        elif args.purpose == "key_metrics":
            # Mode-A recall over a hand-picked key-metrics set, no judge —
            # --no-judge is a no-op like the other deterministic golden purposes.
            from evals.key_metrics import DEFAULT_GOLDEN_RELPATH as KM_GOLDEN
            from evals.key_metrics import run_key_metrics_eval

            golden_path = (args.golden or (PROJECT_ROOT / KM_GOLDEN)).resolve()
            summary = run_key_metrics_eval(
                golden_path=golden_path,
                code_root=PROJECT_ROOT,
                limit=args.limit,
            )
        elif args.purpose == "sector_benchmark_proposal":
            # Mode-A exact-match over a hand-picked industry->ETF golden set,
            # no judge — --no-judge is a no-op like the other deterministic
            # golden purposes.
            from evals.sector_benchmark_proposal import (
                DEFAULT_GOLDEN_RELPATH as SBP_GOLDEN,
            )
            from evals.sector_benchmark_proposal import run_sector_benchmark_proposal_eval

            golden_path = (args.golden or (PROJECT_ROOT / SBP_GOLDEN)).resolve()
            summary = run_sector_benchmark_proposal_eval(
                golden_path=golden_path,
                code_root=PROJECT_ROOT,
                limit=args.limit,
            )
        elif args.purpose == "scenario_prior":
            # Mode-A: deterministic directional-skew + grounded-call grading over
            # pinned thesis/bear anchors, no judge — --no-judge is a no-op like the
            # other deterministic golden purposes.
            from evals.scenario_prior import DEFAULT_GOLDEN_RELPATH as SP_GOLDEN
            from evals.scenario_prior import run_scenario_prior_eval

            golden_path = (args.golden or (PROJECT_ROOT / SP_GOLDEN)).resolve()
            summary = run_scenario_prior_eval(
                golden_path=golden_path,
                code_root=PROJECT_ROOT,
                limit=args.limit,
            )
        elif args.purpose == "ask_pack_router":
            # Set-vs-set + contract grading, no judge — --no-judge is a no-op
            # here like the other deterministic golden purposes.
            from evals.ask_router import DEFAULT_GOLDEN_RELPATH as ROUTER_GOLDEN
            from evals.ask_router import run_ask_router_eval

            golden_path = (args.golden or (PROJECT_ROOT / ROUTER_GOLDEN)).resolve()
            summary = run_ask_router_eval(
                db_path=db_path,
                golden_path=golden_path,
                code_root=PROJECT_ROOT,
                limit=args.limit,
            )
        elif args.purpose == "ask_evidence_followup":
            # S7 agentic loop: drives the production ask path end-to-end
            # (pass 1 + follow-up rounds — real spend per case) and grades
            # the loop structurally from the event stream; no judge.
            from evals.ask_loop import DEFAULT_GOLDEN_RELPATH as LOOP_GOLDEN
            from evals.ask_loop import run_ask_loop_eval

            golden_path = (args.golden or (PROJECT_ROOT / LOOP_GOLDEN)).resolve()
            summary = run_ask_loop_eval(
                db_path=db_path,
                repo_root=repo_root,  # transcripts/filings live in the data repo
                golden_path=golden_path,
                code_root=PROJECT_ROOT,
                limit=args.limit,
            )
        elif args.purpose == "ask_claim_grounding":
            # Citation accuracy (S8 PR2): map cases are deterministic
            # precision/recall on the production claim audit; answer cases
            # generate + judge. --no-judge runs the map cases only (no
            # generation or judge spend).
            from evals.ask_citations import DEFAULT_GOLDEN_RELPATH as CITATIONS_GOLDEN
            from evals.ask_citations import run_ask_citations_eval

            golden_path = (args.golden or (PROJECT_ROOT / CITATIONS_GOLDEN)).resolve()
            summary = run_ask_citations_eval(
                db_path=db_path,
                golden_path=golden_path,
                code_root=PROJECT_ROOT,
                limit=args.limit,
                include_answer_cases=not args.no_judge,
            )
        elif args.purpose == "ask_claim_audit":
            from evals.ask_claim_audit import (
                DEFAULT_GOLDEN_RELPATH as CLAIM_AUDIT_GOLDEN,
            )
            from evals.ask_claim_audit import run_claim_audit_eval

            golden_path = (args.golden or (PROJECT_ROOT / CLAIM_AUDIT_GOLDEN)).resolve()
            summary = run_claim_audit_eval(
                db_path=db_path,
                golden_path=golden_path,
                code_root=PROJECT_ROOT,
                limit=args.limit,
            )
        elif args.purpose == "injection_canaries":
            # Security invariant graded by code (no canary leak / no spurious
            # fire) over the production paths — no judge, --no-judge is a no-op.
            from evals.injection_canaries import GOLDEN_DIR as CANARY_DIR
            from evals.injection_canaries import run_canary_eval

            golden_path = (
                args.golden or (PROJECT_ROOT / CANARY_DIR / "injection_canaries.json")
            ).resolve()
            summary = run_canary_eval(
                golden_path=golden_path,
                code_root=PROJECT_ROOT,
                limit=args.limit,
            )
        elif args.purpose == "provenance_caution":
            # Provenance invariant graded by code (the answer must flag/discount
            # a contested figure, not assert it) — no judge, --no-judge no-op.
            from evals.provenance_caution import GOLDEN_DIR as CAUTION_DIR
            from evals.provenance_caution import run_caution_eval

            golden_path = (
                args.golden or (PROJECT_ROOT / CAUTION_DIR / "provenance_caution.json")
            ).resolve()
            summary = run_caution_eval(
                golden_path=golden_path,
                code_root=PROJECT_ROOT,
                limit=args.limit,
            )
        else:
            # Fast classifiers (PR 4): deterministic golden sets, no judge —
            # --no-judge is a no-op here rather than an error.
            from evals.golden_classifiers import GOLDEN_DIR, run_classifier_eval

            golden_path = (
                args.golden or (PROJECT_ROOT / GOLDEN_DIR / f"{args.purpose}.json")
            ).resolve()
            summary = run_classifier_eval(
                args.purpose,
                golden_path=golden_path,
                code_root=PROJECT_ROOT,
                limit=args.limit,
            )
    except (EvalAbortError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    if summary.n_cases == 0:
        if args.purpose in CAPTURE_AUDIT_PURPOSES:
            print(
                f"ERROR: capture audit for {args.purpose} has no executable cases.",
                file=sys.stderr,
            )
            return 2
        # An empty audit corpus (e.g. --since-days over a quiet week) is
        # "nothing to grade", not a quality signal — don't write a run row
        # and don't trip the gate.
        print(
            f"No cases to grade for {args.purpose}"
            + (f" (since_days={args.since_days})" if args.since_days is not None else "")
            + " — exiting without persisting.",
        )
        return 0

    if not args.no_persist:
        try:
            persist_summary(summary, db_path=db_path)
        except RuntimeError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1

    print(json.dumps(summary.to_json_dict(), indent=2, default=str))

    avg = summary.avg_score
    if args.min_score is not None and (avg is None or avg < args.min_score):
        print(
            f"GATE: avg_score {avg if avg is not None else 'n/a'} < {args.min_score}",
            file=sys.stderr,
        )
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
