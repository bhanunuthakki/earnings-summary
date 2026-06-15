"""Mode-A grader for the LLM key-metrics preselect (directives/key_metrics_picker.md).

The feature surfaces the metrics MOST important to a ticker over its available
extract vocabulary. The thing that can go wrong is the same as peer_selection:
the generator MISSES the load-bearing figures (returns 'total assets' for a
digital bank instead of NIM / NPL / deposits). So the ground truth is a
hand-picked set of must-have key metrics per ticker and the headline score is
**recall** — did the generator surface the metrics a fundamental analyst would
expect, picking ONLY from the supplied vocabulary? Deterministic, no judge layer
(mirrors evals.peer_selection): the suggested tokens vs the pinned set.

Per case:
  * ``score`` = recall = |returned ∩ expected| / |expected| — the fraction of
    the must-have metrics the LLM surfaced. ``--min-score`` gates on the mean.
  * ``passed`` = recall ≥ _CASE_PASS_RECALL.
  * ``also_valid`` tokens are correct-but-not-required: counted in precision so a
    sensible alternative pick isn't punished, never required for recall.

Each case carries its own ``vocabulary`` (the closed token set the production
call picks from), so the eval exercises the real closed-vocabulary discipline:
a returned token outside the vocabulary is impossible by construction (the
generator drops it), and the golden ``expected_metrics`` are a subset of it.

Abort semantics mirror the other graders: a hard stop (budget cap / missing CLI)
raises ``EvalAbortError`` rather than scoring 0 against the prompt; a
``StructuredParseError`` (unusable JSON twice) is an extraction-quality failure
and scores 0 at stage ``call``. The first case erroring twice aborts the run.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast
from uuid import uuid4

from evals.harness import (
    CaseResult,
    EvalAbortError,
    EvalRunSummary,
    dumps_compact,
    now_naive_utc,
    resolve_git_sha,
    sha256_file,
)
from llm.cli import DEFAULT_MODEL, LLM_MODELS, is_hard_stop
from llm.prompt_versions import prompt_version_for
from llm.structured import StructuredParseError

log = logging.getLogger(__name__)

PURPOSE = "key_metrics"
DEFAULT_GOLDEN_RELPATH = Path("evals") / "golden" / "key_metrics.json"

# A case "passes" when at least this fraction of the must-have metrics surfaced.
# The run-level regression gate uses the mean recall (--min-score), not this.
_CASE_PASS_RECALL = 0.5

# suggest_fn returns the list of suggested metric TOKENS for a case (decouples
# the grader from the KeyMetricSuggestion type; the production wrapper extracts).
SuggestFn = Callable[["KeyMetricCase"], list[str]]


@dataclass(frozen=True, slots=True)
class KeyMetricCase:
    case_id: str
    ticker: str
    name: str
    business_description: str
    vocabulary: tuple[tuple[str, str], ...]  # (token, label) the call may pick
    expected_metrics: frozenset[str]
    also_valid: frozenset[str] = field(default_factory=frozenset[str])


# ---------------------------------------------------------------------------
# golden-file loading (all-problems validation, mirroring the other graders)
# ---------------------------------------------------------------------------


def _token_set(c: dict[str, object], key: str, label: str, errors: list[str]) -> frozenset[str]:
    raw = c.get(key, [])
    if not isinstance(raw, list):
        errors.append(f"{label}: `{key}` must be a list")
        return frozenset()
    return frozenset(str(x).strip() for x in cast("list[object]", raw) if str(x).strip())


def _vocabulary(c: dict[str, object], label: str, errors: list[str]) -> tuple[tuple[str, str], ...]:
    raw = c.get("vocabulary", [])
    if not isinstance(raw, list) or not raw:
        errors.append(f"{label}: needs a non-empty `vocabulary` list")
        return ()
    vocab: list[tuple[str, str]] = []
    for entry in cast("list[object]", raw):
        if isinstance(entry, dict):
            rec = cast("dict[str, object]", entry)
            token = str(rec.get("token") or "").strip()
            vlabel = str(rec.get("label") or token).strip()
        else:
            token = str(entry).strip()
            vlabel = token
        if token:
            vocab.append((token, vlabel))
    return tuple(vocab)


def load_key_metrics_golden(path: Path) -> list[KeyMetricCase]:
    import json

    try:
        payload: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"golden file unreadable at {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("golden file must be a JSON object")
    doc = cast("dict[str, object]", payload)
    if doc.get("purpose") != PURPOSE:
        raise ValueError(f"golden file purpose must be {PURPOSE!r}, got {doc.get('purpose')!r}")
    raw_cases = doc.get("cases")
    if not isinstance(raw_cases, list) or not raw_cases:
        raise ValueError("golden file needs a non-empty `cases` list")

    errors: list[str] = []
    seen: set[str] = set()
    cases: list[KeyMetricCase] = []
    for i, entry in enumerate(cast("list[object]", raw_cases)):
        label = f"cases[{i}]"
        if not isinstance(entry, dict):
            errors.append(f"{label}: must be an object")
            continue
        c = cast("dict[str, object]", entry)
        case_id = str(c.get("id") or "")
        if not case_id:
            errors.append(f"{label}: missing/empty `id`")
        if case_id in seen:
            errors.append(f"{label}: duplicate id {case_id!r}")
        seen.add(case_id)
        ticker = str(c.get("ticker") or "").strip().upper()
        if not ticker:
            errors.append(f"{label} ({case_id}): missing/empty `ticker`")
        name = str(c.get("name") or "").strip()
        description = str(c.get("business_description") or "").strip()
        if not description:
            errors.append(f"{label} ({case_id}): missing/empty `business_description`")
        vocabulary = _vocabulary(c, f"{label} ({case_id})", errors)
        vocab_tokens = {t for t, _ in vocabulary}
        expected = _token_set(c, "expected_metrics", f"{label} ({case_id})", errors)
        if "expected_metrics" not in c or not expected:
            errors.append(f"{label} ({case_id}): needs a non-empty `expected_metrics`")
        also_valid = _token_set(c, "also_valid", f"{label} ({case_id})", errors)
        # Every must-have token must be IN the vocabulary, else recall is
        # unwinnable by construction (the call can only return vocabulary tokens).
        stray = sorted((expected | also_valid) - vocab_tokens)
        if vocab_tokens and stray:
            errors.append(
                f"{label} ({case_id}): expected/also_valid tokens not in vocabulary: {stray}"
            )
        cases.append(
            KeyMetricCase(
                case_id=case_id,
                ticker=ticker,
                name=name,
                business_description=description,
                vocabulary=vocabulary,
                expected_metrics=expected,
                also_valid=also_valid,
            )
        )
    if errors:
        raise ValueError(f"golden file invalid at {path}: " + "; ".join(errors))
    return cases


# ---------------------------------------------------------------------------
# grading
# ---------------------------------------------------------------------------


def grade_key_metrics_case(case: KeyMetricCase, *, suggest_fn: SuggestFn) -> CaseResult:
    t0 = time.monotonic()
    try:
        returned_raw = suggest_fn(case)
    except StructuredParseError as exc:
        latency_ms = int((time.monotonic() - t0) * 1000)
        return CaseResult(
            case_id=case.case_id,
            question=f"{PURPOSE}/{case.case_id} ({case.ticker})",
            passed=False,
            score=0.0,
            expected_json=dumps_compact(sorted(case.expected_metrics)),
            actual_json=None,
            failure_stage="call",
            judge_rationale=f"suggest_key_metrics returned unusable JSON twice: {exc}",
            latency_ms=latency_ms,
        )
    except Exception as exc:
        if is_hard_stop(exc):
            raise EvalAbortError(
                f"{PURPOSE} hard stop on case {case.case_id}: {type(exc).__name__}: {exc} — "
                "aborting instead of scoring 0s."
            ) from exc
        raise
    latency_ms = int((time.monotonic() - t0) * 1000)

    returned = frozenset(t.strip() for t in returned_raw if t.strip())
    hits = returned & case.expected_metrics
    recall = len(hits) / len(case.expected_metrics) if case.expected_metrics else 0.0
    acceptable = case.expected_metrics | case.also_valid
    precision = len(returned & acceptable) / len(returned) if returned else 0.0
    passed = recall >= _CASE_PASS_RECALL
    missed = sorted(case.expected_metrics - returned)
    off = sorted(returned - acceptable)
    return CaseResult(
        case_id=case.case_id,
        question=f"{PURPOSE}/{case.case_id} ({case.ticker})",
        passed=passed,
        score=recall,
        expected_json=dumps_compact(sorted(case.expected_metrics)),
        actual_json=dumps_compact(sorted(returned)),
        failure_stage=None if passed else "mismatch",
        judge_rationale=(
            f"recall={recall:.2f} precision={precision:.2f}"
            + (f"; missed {missed}" if missed else "")
            + (f"; off-thesis {off}" if off else "")
        ),
        latency_ms=latency_ms,
    )


# ---------------------------------------------------------------------------
# run orchestration
# ---------------------------------------------------------------------------


def _production_suggest(case: KeyMetricCase) -> list[str]:
    """The real generator over the golden case's pinned inputs — one live LLM
    call per case, picking from the case's own vocabulary."""
    from compute.key_metrics import suggest_key_metrics

    suggestions = suggest_key_metrics(
        ticker=case.ticker,
        name=case.name or None,
        business_description=case.business_description,
        vocabulary=list(case.vocabulary),
    )
    return [s.token for s in suggestions]


def run_key_metrics_eval(
    *,
    golden_path: Path,
    code_root: Path,
    limit: int | None = None,
    suggest_fn: SuggestFn | None = None,
) -> EvalRunSummary:
    """The full mode-A run. Does NOT persist — the caller decides
    (execution/run_llm_evals.py). ``suggest_fn`` injects a fake generator for
    tests; None = the production ``compute.key_metrics.suggest_key_metrics``."""
    cases = load_key_metrics_golden(golden_path)
    if limit is not None:
        cases = cases[: max(0, limit)]
    target: SuggestFn = suggest_fn if suggest_fn is not None else _production_suggest

    summary = EvalRunSummary(
        run_id=uuid4().hex,
        purpose=PURPOSE,
        mode="live",
        prompt_version=prompt_version_for(PURPOSE),
        model=LLM_MODELS.get(PURPOSE, DEFAULT_MODEL),
        judge_model=None,  # hand-picked ground truth — deterministic recall, no judge
        golden_set_sha=sha256_file(golden_path),
        started_at=now_naive_utc(),
        git_sha=resolve_git_sha(code_root),
    )
    for i, case in enumerate(cases):
        result = grade_key_metrics_case(case, suggest_fn=target)
        if i == 0 and result.failure_stage == "call":
            retry = grade_key_metrics_case(case, suggest_fn=target)
            if retry.failure_stage == "call":
                raise EvalAbortError(
                    f"{PURPOSE}: the first case errored twice ({result.judge_rationale}) — "
                    "transport looks down; aborting instead of scoring 0s."
                )
            result = retry
        summary.cases.append(result)
        log.info(
            {
                "event": "eval_case_graded",
                "purpose": PURPOSE,
                "case_id": result.case_id,
                "passed": result.passed,
                "score": result.score,
                "failure_stage": result.failure_stage,
            }
        )
    summary.finished_at = now_naive_utc()
    return summary
