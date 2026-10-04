"""Resume source onboarding and deliver a full memo with exact readiness evidence."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from db_paths import require_db_path
from research.decision_brief_workflow import (
    DecisionBriefPreparationRequest,
    prepare_decision_brief,
)
from runtime.job_runtime import JobLock
from runtime.secrets import load_project_env

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ticker", required=True)
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--db", type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--skip-fmp", action="store_true")
    parser.add_argument("--enable-llm", action="store_true")
    parser.add_argument("--companyfacts-review", type=Path, action="append", default=[])
    parser.add_argument("--context-review", type=Path)
    parser.add_argument("--processor-installation", type=Path)
    parser.add_argument("--native-statement-reviews", type=Path)
    parser.add_argument("--valuation-request", type=Path)
    parser.add_argument("--valuation-request-sha256")
    parser.add_argument("--valuation-artifact", type=Path)
    args = parser.parse_args(argv)
    args.repo_root = args.repo_root.resolve()
    load_project_env(args.repo_root)
    configured = args.db or os.environ.get("EARNINGS_SUMMARY_DB_PATH", "").strip()
    if not configured:
        raise RuntimeError("An explicit configured portfolio database is required")
    database = require_db_path(configured)
    request = DecisionBriefPreparationRequest(
        ticker=args.ticker.strip().upper(),
        code_root=PROJECT_ROOT,
        repo_root=args.repo_root,
        database=database,
        apply=args.apply,
        skip_fmp=args.skip_fmp,
        enable_llm=args.enable_llm,
        companyfacts_reviews=tuple(path.resolve() for path in args.companyfacts_review),
        context_review=args.context_review.resolve() if args.context_review is not None else None,
        processor_installation=args.processor_installation.resolve()
        if args.processor_installation is not None
        else None,
        native_statement_reviews=args.native_statement_reviews.resolve()
        if args.native_statement_reviews is not None
        else None,
        valuation_request=args.valuation_request.resolve()
        if args.valuation_request is not None
        else None,
        valuation_request_sha256=args.valuation_request_sha256,
        valuation_artifact=args.valuation_artifact.resolve()
        if args.valuation_artifact is not None
        else None,
    )
    with JobLock(args.repo_root, "prepare-decision-brief", [f"requested-memo:{request.ticker}"]):
        receipt = prepare_decision_brief(request)
    print(receipt.model_dump_json(indent=2))
    return 0 if receipt.status in {"planned", "delivered"} else 3


if __name__ == "__main__":
    raise SystemExit(main())
