"""Generate the Socratic think-through's Step 1 questions as a background job
(wave3b Task 4 — "Socratic becomes an honest background job").

The flow used to run synchronously inside ``POST /api/socratic/questions``:
build_advisor_context -> compose_premortem (Opus, measured ~68s) ->
gate_premortem -> generate_questions (~14s) -> the browser's fetch() sat
blocked the whole time. This script is what
``POST /actions/socratic-questions`` now runs through the jobs registry
(``dispatch_registry.Registry`` / ``/actions/stream/<job_id>``) instead: the
request returns immediately with a job id, the page streams progress, and
this script PERSISTS the result (``advisor.socratic.persist_prelude``) so the
page can read it back via ``GET /api/socratic/questions/<ticker>`` once the
job's SSE stream reports done.

Usage:
    python execution/run_socratic_questions.py NU --repo-root <path>

Exit status: 0 on a persisted prelude, 1 on a transient generation failure
(unparseable completion / LLM hiccup — the owner retries from the page) OR
a persistence failure (DB unavailable — the questions generated but the page
would never see them; this job's deliverable IS the persisted row, not the
LLM call alone), 2 on a hard stop (budget block / missing CLI — see
llm.cli.is_hard_stop).
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

log = logging.getLogger("run_socratic_questions")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ticker")
    parser.add_argument("--repo-root", type=Path, default=PROJECT_ROOT)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    repo_root = args.repo_root.resolve()
    ticker = args.ticker.upper()

    from advisor.socratic import generate_questions, persist_prelude
    from llm.cli import is_hard_stop

    print(f"generating think-through questions for {ticker} (grounding + premortem)...", flush=True)
    try:
        prelude = generate_questions(repo_root, ticker)
    except Exception as exc:
        if is_hard_stop(exc):
            print(f"HARD STOP: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
            return 2
        print(
            f"question generation failed: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True
        )
        return 1

    artifact_id = persist_prelude(repo_root / "data" / "portfolio.db", prelude)
    if artifact_id is None:
        # The LLM call succeeded but persistence didn't — the page has no
        # other way to see this job's result, so an un-persisted prelude is
        # a failure from the caller's perspective, not a degraded success.
        print("persistence failed — DB unavailable; the page will never see these questions", file=sys.stderr, flush=True)
        return 1
    print(f"questions ready: {len(prelude.questions)} for {ticker} (artifact #{artifact_id})", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
