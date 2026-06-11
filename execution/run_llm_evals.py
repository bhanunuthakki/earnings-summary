"""Run an LLM eval (directives/llm_evals_plan.md).

Grades a purpose's outputs against its golden set through the PRODUCTION call
path, persists the run + per-case transcripts to eval_runs/eval_case_results
(alembic 0083), and bridges the run average into prompt_calibration_scores so
`summarize_by_prompt_version` compares prompt versions.

Examples:
    python execution/run_llm_evals.py --purpose viewspec_compile
    python execution/run_llm_evals.py --purpose viewspec_compile \
        --repo-root C:/Users/Bhanu/.gemini/antigravity/scratch/earnings-summary
    python execution/run_llm_evals.py --purpose viewspec_compile --no-persist --limit 3
    python execution/run_llm_evals.py --purpose viewspec_compile --min-score 0.8  # gate

Exit codes: 0 ok · 1 hard failure (bad golden set, missing DB/tables, abort)
· 3 ran fine but avg_score below --min-score (the regression gate).
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

log = logging.getLogger("run_llm_evals")

PURPOSES = ("viewspec_compile",)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--purpose",
        required=True,
        choices=PURPOSES,
        help="Which purpose's eval to run (graders live in src/evals/).",
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
        help="Override the golden-set file (default: <this repo>/evals/golden/<purpose>.json).",
    )
    parser.add_argument("--limit", type=int, default=None, help="Grade only the first N cases.")
    parser.add_argument(
        "--no-persist",
        action="store_true",
        help="Skip the eval-table + calibration writes (dry run; JSON still printed).",
    )
    parser.add_argument(
        "--no-judge",
        action="store_true",
        help="Skip judge calls — divergent cases just fail (no judge spend).",
    )
    parser.add_argument(
        "--min-score",
        type=float,
        default=None,
        help="Gate mode: exit 3 when avg_score falls below this threshold.",
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
    if not db_path.exists():
        print(f"ERROR: no DB at {db_path}", file=sys.stderr)
        return 1
    _sync_db_to_repo(repo_root)

    from evals.harness import EvalAbortError, persist_summary
    from evals.judge import run_judge
    from evals.viewspec_compile import DEFAULT_GOLDEN_RELPATH, run_viewspec_eval

    golden_path = (args.golden or (PROJECT_ROOT / DEFAULT_GOLDEN_RELPATH)).resolve()
    try:
        summary = run_viewspec_eval(
            db_path=db_path,
            golden_path=golden_path,
            code_root=PROJECT_ROOT,  # the sha of the code/prompt under eval, not the data repo
            limit=args.limit,
            judge=None if args.no_judge else run_judge,
        )
    except (EvalAbortError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

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
