"""Backtest one prompt candidate against HISTORICAL judged cases
(LLM Quality Program P2 — directives/llm_quality_program_2026_07.md).

The question this answers: *before* spending on a live A/B, would this
candidate template have beaten the incumbent on cases we have already seen?

How it differs from ``run_prompt_ab.py`` (which stays the live experiment
driver): the backtest replays a FIXED historical case set — real captured
production prompts, with their real variable payloads — so two candidates can
be compared on identical inputs, and a candidate can be re-scored later
without re-drawing a sample. That fixed-sample property is what makes a
before/after number meaningful; the live driver deliberately draws FRESH
samples each run to avoid overfitting a single set.

Both sides of every case are generated under the SAME frozen model and judged
by the existing brand-blind, position-swapped dual judge, so the only delta is
the prompt body.

Honesty contracts, inherited and enforced:
* the transport breaker / classification from #1008 — a run under a degraded
  transport reports TRANSPORT_DEGRADED rather than a quality verdict;
* judge failures are infra, never scores (#1027 semantics);
* `--dry-run` prints the plan and the exact spend estimate, and exits without
  calling anything. A backtest that surprises you with its bill is a bad tool.

Usage:
    python execution/backtest_prompt_candidate.py --purpose bear_case \\
        --template-id bear_case.body --candidate-file cand.txt \\
        --repo-root <MAIN> --dry-run
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from evals.sampler import load_frame  # noqa: E402
from llm.backend_judge import CLAUDE, GEMINI, judge_pair  # noqa: E402
from llm.cli import DEFAULT_MODEL, LLM_MODELS  # noqa: E402
from llm.model_eval import run_model  # noqa: E402
from llm.model_ladder import JUDGE_POOL  # noqa: E402
from llm.prompt_reflect import Candidate  # noqa: E402
from llm.prompt_registry import REGISTRY, PromptTemplate  # noqa: E402

log = logging.getLogger("backtest_prompt_candidate")

# Rough per-case cost guard so --dry-run can state a real number. Measured
# 2026-07: a bear_case generation runs ~$0.25 and a judged pair ~$0.02.
_EST_GEN_USD = 0.25
_EST_JUDGE_USD = 0.02

# Purposes whose production calls use Claude's web tools. Their captured
# responses are not reproducible without web access, so a replay comparison is
# structurally confounded (see the guard in run_backtest).
_WEB_SCOPED_PURPOSES = frozenset({"recent_developments", "news_structuring"})


@dataclass(frozen=True, slots=True)
class BacktestResult:
    """One candidate's measured performance against the historical set."""

    purpose: str
    template_id: str
    baseline_version: str
    candidate_version: str
    n_cases: int
    candidate_wins: int
    baseline_wins: int
    ties: int
    judge_agreement: float
    n_candidate_errors: int
    n_baseline_errors: int
    n_judge_errors: int
    candidate_mean_output_chars: float
    baseline_mean_output_chars: float
    verdict: str
    reason: str

    @property
    def win_rate(self) -> float:
        total = self.candidate_wins + self.baseline_wins + self.ties
        return (self.candidate_wins / total) if total else 0.0

    def as_candidate(self) -> Candidate:
        """The Pareto-frontier view: quality = win rate, cost = mean output
        chars (the lever a prompt actually controls)."""
        return Candidate(
            version=self.candidate_version,
            quality=self.win_rate,
            cost=self.candidate_mean_output_chars,
            n_cases=self.n_cases,
        )


def _extract_variables(rendered: str, template: PromptTemplate) -> dict[str, str] | None:
    """Recover the per-case variable payload from a captured render.

    Only exact single-slot bodies are recoverable in general; for the common
    case the template's literal segments bracket each variable, so a simple
    scan works. Returns None when the render doesn't match the template — that
    case is SKIPPED and counted, never guessed at (a mis-parsed variable would
    silently change what the candidate is being tested on).
    """
    import re

    pattern = re.escape(template.body)
    for var in template.variables:
        token = re.escape("{" + var + "}")
        # A variable may appear MORE THAN ONCE in a body (e.g. {ticker} in both
        # the framing and the output spec). The first occurrence captures; the
        # rest are BACKREFERENCES — naming the group twice is a regex error,
        # and matching them independently would let a render whose two
        # occurrences disagree parse "successfully" into a wrong payload.
        first, sep, rest = pattern.partition(token)
        if not sep:
            continue
        pattern = first + f"(?P<{var}>.*?)" + rest.replace(token, f"(?P={var})")
    # Literal braces in the body were doubled for .format; undo for matching.
    pattern = pattern.replace(re.escape("{{"), re.escape("{")).replace(
        re.escape("}}"), re.escape("}")
    )
    try:
        m = re.fullmatch(pattern, rendered, flags=re.DOTALL)
    except re.error:
        return None  # unmatchable template shape — skipped and counted, never guessed
    if m is None:
        return None
    return {k: (v or "") for k, v in m.groupdict().items()}


def load_historical_cases(
    capture_dir: Path, purpose: str, template: PromptTemplate, *, limit: int
) -> tuple[list[tuple[str, dict[str, str], str]], int]:
    """[(label, variables, incumbent_response)], plus the count SKIPPED.

    Skips are reported, never hidden: a low match rate means the template
    drifted from what production actually sent, which is a finding in itself.
    """
    files = sorted(capture_dir.glob("capture_*.jsonl"))
    frame = load_frame(files, purpose)
    cases: list[tuple[str, dict[str, str], str]] = []
    skipped = 0
    for sha, rec in frame.items():
        if len(cases) >= limit:
            break
        variables = _extract_variables(rec.prompt, template)
        if variables is None:
            skipped += 1
            continue
        label = f"{purpose}:{rec.ticker or '-'}:{sha[:8]}"
        cases.append((label, variables, rec.response))
    return cases, skipped


def run_backtest(
    *,
    db_path: Path,
    capture_dir: Path,
    purpose: str,
    baseline: PromptTemplate,
    candidate: PromptTemplate,
    limit: int,
    judges: list[str],
    timeout_seconds: int | None,
    dry_run: bool,
) -> BacktestResult | None:
    from llm.model_eval import CANDIDATE_ERROR_RATE_THRESHOLD, JUDGE_ERROR_RATE_THRESHOLD

    # Web-scoped purposes are STRUCTURALLY unbacktestable here and must be
    # refused, not silently mismeasured. The captured baseline responses were
    # produced WITH WebSearch/WebFetch; a replay runs `run_model`, which has no
    # web tools. The candidate would lose on missing live data rather than on
    # prompt quality — a confidently wrong verdict. (The §2 sampler excludes
    # web rows from the model loop for exactly this reason; the same confound
    # applies to prompt candidates.)
    if purpose in _WEB_SCOPED_PURPOSES:
        log.error(
            "[%s] web-scoped purpose: captured baselines used WebSearch/WebFetch but a "
            "replay has no web tools, so any verdict would measure missing data, not "
            "prompt quality. Backtest refused — use a live A/B (run_prompt_ab.py) for "
            "web purposes.",
            purpose,
        )
        return None

    frozen_model = LLM_MODELS.get(purpose, DEFAULT_MODEL)
    cases, skipped = load_historical_cases(capture_dir, purpose, baseline, limit=limit)
    if not cases:
        log.error(
            "[%s] no replayable historical cases (%d captured renders did not match "
            "the registered template) — harvest more, or the template has drifted "
            "from what production sends",
            purpose,
            skipped,
        )
        return None
    log.info(
        "[%s] %d replayable case(s), %d skipped (non-matching renders); model=%s",
        purpose,
        len(cases),
        skipped,
        frozen_model,
    )

    est = len(cases) * (_EST_GEN_USD + _EST_JUDGE_USD * len(judges))
    log.info("estimated spend: ~$%.2f (%d cases x %d judge(s))", est, len(cases), len(judges))
    if dry_run:
        print(
            json.dumps(
                {
                    "dry_run": True,
                    "purpose": purpose,
                    "template_id": baseline.template_id,
                    "baseline_version": baseline.version,
                    "candidate_version": candidate.version,
                    "n_cases": len(cases),
                    "n_skipped": skipped,
                    "frozen_model": frozen_model,
                    "estimated_usd": round(est, 2),
                    "sample_labels": [c[0] for c in cases[:5]],
                },
                indent=2,
            )
        )
        return None

    run_id = uuid.uuid4().hex
    tally = {jb: [0, 0, 0] for jb in judges}  # candidate, baseline, tie
    cand_errors = base_errors = judge_errors = n_judgments = 0
    cand_chars: list[int] = []
    base_chars: list[int] = []
    agreements: list[bool] = []

    for label, variables, incumbent_response in cases:
        base_prompt = baseline.render(**variables)
        cand_prompt = candidate.render(**variables)

        # Baseline: reuse the captured response when the incumbent model made
        # it (zero spend); else re-run under the frozen model.
        baseline_out = incumbent_response
        if not baseline_out:
            base = run_model(
                base_prompt,
                model_id=frozen_model,
                purpose=purpose,
                run_id=run_id,
                timeout_seconds=timeout_seconds,
                scope="prompt_ab",
            )
            if not base.ok:
                base_errors += 1
                continue
            baseline_out = base.response

        cand = run_model(
            cand_prompt,
            model_id=frozen_model,
            purpose=purpose,
            run_id=run_id,
            timeout_seconds=timeout_seconds,
            scope="prompt_ab",
        )
        if not cand.ok:
            cand_errors += 1
            continue
        cand_chars.append(len(cand.response))
        base_chars.append(len(baseline_out))

        winners: list[str] = []
        for jb in judges:
            jp = judge_pair(
                purpose=purpose,
                label=label,
                ticker=None,
                claude_response=baseline_out,  # slot A == baseline
                gemini_response=cand.response,  # slot B == candidate
                task_prompt=str(base_prompt),  # the BASELINE defines the task
                judge_backend=jb,
                judge_model=JUDGE_POOL.get(jb),
                run_id=run_id,
            )
            n_judgments += 1
            if jp.error:
                judge_errors += 1
                continue
            winners.append(jp.winner)
            if jp.winner == GEMINI:
                tally[jb][0] += 1
            elif jp.winner == CLAUDE:
                tally[jb][1] += 1
            else:
                tally[jb][2] += 1
        if len(winners) >= 2:
            agreements.append(len(set(winners)) == 1)

    n_attempted = len(cases)
    agreement = (sum(agreements) / len(agreements)) if agreements else 0.0
    cand_wins = sum(t[0] for t in tally.values())
    base_wins = sum(t[1] for t in tally.values())
    ties = sum(t[2] for t in tally.values())

    # Honesty gates BEFORE any quality claim (the #1008/#1027 taxonomy).
    if n_attempted and base_errors / n_attempted >= 0.25:
        verdict, reason = (
            "TRANSPORT_DEGRADED",
            f"the UNEDITED baseline failed on {base_errors}/{n_attempted} case(s) — "
            "the transport was measured, not the prompt",
        )
    elif n_judgments and judge_errors / n_judgments >= JUDGE_ERROR_RATE_THRESHOLD:
        verdict, reason = (
            "JUDGE_DEGRADED",
            f"judges errored on {judge_errors}/{n_judgments} judgment(s) — nothing "
            "was measured about the candidate",
        )
    elif n_attempted and cand_errors / n_attempted >= CANDIDATE_ERROR_RATE_THRESHOLD:
        verdict, reason = (
            "CANDIDATE_ERRORED",
            f"the candidate failed operationally on {cand_errors}/{n_attempted} case(s) — "
            "an authoring failure, not a quality verdict",
        )
    elif (cand_wins + base_wins + ties) == 0:
        verdict, reason = "INSUFFICIENT_DATA", "no judged cases survived"
    else:
        rate = cand_wins / (cand_wins + base_wins + ties)
        if rate >= 0.60 and base_wins / (cand_wins + base_wins + ties) <= 0.20:
            verdict, reason = "CANDIDATE_BETTER", f"candidate wins {rate:.0%} of judged cases"
        elif base_wins > cand_wins:
            verdict, reason = "BASELINE_BETTER", f"baseline wins {base_wins} vs {cand_wins}"
        else:
            verdict, reason = "INCONCLUSIVE", f"candidate wins {rate:.0%} — below the 60% bar"

    return BacktestResult(
        purpose=purpose,
        template_id=baseline.template_id,
        baseline_version=baseline.version,
        candidate_version=candidate.version,
        n_cases=n_attempted,
        candidate_wins=cand_wins,
        baseline_wins=base_wins,
        ties=ties,
        judge_agreement=round(agreement, 4),
        n_candidate_errors=cand_errors,
        n_baseline_errors=base_errors,
        n_judge_errors=judge_errors,
        candidate_mean_output_chars=(sum(cand_chars) / len(cand_chars)) if cand_chars else 0.0,
        baseline_mean_output_chars=(sum(base_chars) / len(base_chars)) if base_chars else 0.0,
        verdict=verdict,
        reason=reason,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--capture-dir", type=Path, default=None)
    parser.add_argument("--purpose", required=True)
    parser.add_argument("--template-id", required=True)
    parser.add_argument(
        "--candidate-file", type=Path, help="file holding the candidate template body"
    )
    parser.add_argument("--limit", type=int, default=8)
    parser.add_argument("--judges", default=f"{CLAUDE},{GEMINI}")
    parser.add_argument("--timeout", type=int, default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    repo_root = args.repo_root.resolve()
    db_path = repo_root / "data" / "portfolio.db"
    capture_dir = args.capture_dir or (repo_root / "data" / "llm_capture")
    if os.environ.pop("LLM_CAPTURE_DIR", None):
        log.warning("cleared inherited LLM_CAPTURE_DIR (backtest traffic must not be captured)")
    import db as _db

    _db.set_db_path(db_path)

    # Importing the modules that REGISTER templates.
    # Import for SIDE EFFECT: these modules register their templates.
    import llm_client  # noqa: F401  # pyright: ignore[reportUnusedImport]
    from dcf import scenario_prior  # noqa: F401  # pyright: ignore[reportUnusedImport]

    baseline = REGISTRY.get(args.template_id)
    if baseline is None:
        log.error("template %r not registered — known: %s", args.template_id, sorted(REGISTRY))
        return 1
    if not args.candidate_file:
        log.error("--candidate-file is required (the candidate template body)")
        return 1
    body = args.candidate_file.read_text(encoding="utf-8")
    try:
        candidate = PromptTemplate(
            template_id=baseline.template_id,
            body=body,
            variables=baseline.variables,
            description=baseline.description,
        )
    except ValueError as exc:
        log.error("candidate is not a valid template for this call site: %s", exc)
        return 1

    result = run_backtest(
        db_path=db_path,
        capture_dir=capture_dir,
        purpose=args.purpose,
        baseline=baseline,
        candidate=candidate,
        limit=args.limit,
        judges=[j.strip() for j in str(args.judges).split(",") if j.strip()],
        timeout_seconds=args.timeout,
        dry_run=args.dry_run,
    )
    if result is None:
        return 0 if args.dry_run else 1
    print(json.dumps(asdict(result) | {"win_rate": round(result.win_rate, 4)}, indent=2))
    log.info("[%s] %s — %s", args.purpose, result.verdict, result.reason)
    return 0


if __name__ == "__main__":
    sys.exit(main())
