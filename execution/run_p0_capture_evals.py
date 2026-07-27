"""Plan or manually execute current-version P0 capture audits.

The default is a provider-free dry run. Pass ``--execute`` to invoke the judge
and persist non-empty summaries. The 1.2M value is a conservative planning
limit, not a provider-enforced hard cap; actual usage remains in ``llm_calls``.
Execution forces one Codex attempt per case with no Claude fallback. This CLI
is intentionally not scheduled.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from evals.harness import persist_summary  # noqa: E402
from evals.p0_capture_runner import (  # noqa: E402
    DEFAULT_MAX_PLANNED_TOKENS,
    DEFAULT_SINCE_DAYS,
    build_p0_capture_plan,
    run_p0_capture_plan,
)
from schema_compat import require_current_for_write  # noqa: E402


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--db-path", type=Path, default=None)
    parser.add_argument(
        "--max-planned-tokens",
        type=int,
        default=DEFAULT_MAX_PLANNED_TOKENS,
    )
    parser.add_argument("--since-days", type=int, default=DEFAULT_SINCE_DAYS)
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Invoke the judge and persist results. Omit for a zero-spend plan.",
    )
    return parser.parse_args()


def _preflight_write(db_path: Path) -> None:
    if not db_path.is_file():
        raise RuntimeError(f"no DB at {db_path}")
    with sqlite3.connect(db_path) as conn:
        require_current_for_write(conn)


def main() -> int:
    args = _parse_args()
    repo_root = args.repo_root.resolve()
    db_path = (args.db_path or (repo_root / "data" / "portfolio.db")).resolve()
    try:
        plan = build_p0_capture_plan(
            repo_root,
            max_planned_tokens=args.max_planned_tokens,
            since_days=args.since_days,
        )
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    output: dict[str, object] = {
        "mode": "execute" if args.execute else "dry_run",
        "plan": plan.model_dump(mode="json"),
        "runs": [],
    }
    if not args.execute or plan.selected_cases == 0:
        print(json.dumps(output, indent=2))
        return 0

    try:
        _preflight_write(db_path)
        from db_paths import db_path_context

        with db_path_context(db_path):
            summaries = run_p0_capture_plan(
                plan,
                repo_root=repo_root,
                code_root=PROJECT_ROOT,
            )
            for summary in summaries:
                persist_summary(summary, db_path=db_path)
    except (RuntimeError, sqlite3.Error) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    output["runs"] = [summary.to_json_dict() for summary in summaries]
    print(json.dumps(output, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
