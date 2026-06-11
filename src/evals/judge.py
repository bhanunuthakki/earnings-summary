"""The eval judge — a cheap model deciding ambiguous near-misses.

Deterministic grading comes first (schema-valid, executes, exact normalized
match); the judge fires ONLY when the actual output diverges from the golden
expectation while still being valid — the one question code can't answer:
"is the difference material to what was asked?".

Contract: fail closed. An unparseable or failed verdict never passes a case;
the raw judge text is preserved on the case row so the judge itself stays
auditable. Judge calls go through the canonical client under
``purpose="eval_judge"`` (model pin + rationale in LLM_MODELS, budget row
seeded by migration 0083) and carry the eval run's ``run_id`` so their cost
joins back from llm_calls.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import cast

from llm.cli import call_llm

log = logging.getLogger(__name__)

JUDGE_PURPOSE = "eval_judge"

_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$")


@dataclass(frozen=True, slots=True)
class JudgeVerdict:
    """A parsed, schema-valid verdict."""

    equivalent: bool
    score: float  # 0.0..1.0 (clamped)
    rationale: str


@dataclass(frozen=True, slots=True)
class JudgeOutcome:
    """What one judge call produced. ``verdict is None`` means the call
    failed or the output didn't validate — the caller must fail the case."""

    verdict: JudgeVerdict | None
    raw: str
    error: str | None = None


_PROMPT_TEMPLATE = """\
You are grading a natural-language -> ViewSpec compiler for a financial pivot engine.
An analyst asked a question; a golden EXPECTED spec encodes the intended analysis; the
compiler produced the ACTUAL spec. Both are schema-valid and executable — decide whether
the ACTUAL spec answers the question as well as the EXPECTED one.

Question: {question}

EXPECTED spec:
{expected}

ACTUAL spec:
{actual}

Field differences:
{diff}

Judge analytical equivalence, not textual equality:
- same tickers and same underlying metrics matter most;
- transform must fit the question (growth -> yoy, "% of revenue" -> margin, compounding -> cagr);
- display-window differences (periods) only matter when the question pins a number;
- extra or missing metrics/tickers that change what the analyst sees are NOT equivalent.

Output ONLY a JSON object, no prose, no markdown fences:
{{"equivalent": true|false, "score": <0.0-1.0>, "rationale": "<one sentence>"}}
score: 1.0 = fully interchangeable answer; ~0.5 = partially answers the question;
0.0 = a different analysis than the one asked for.
"""


def build_judge_prompt(question: str, expected_json: str, actual_json: str, diff: str) -> str:
    return _PROMPT_TEMPLATE.format(
        question=question, expected=expected_json, actual=actual_json, diff=diff or "(none)"
    )


def parse_verdict(raw: str) -> JudgeVerdict | None:
    """Strict, fail-closed verdict parsing: fence-strip, one JSON object,
    required keys with the right types. None on any deviation."""
    text = raw.strip()
    if text.startswith("```"):
        text = _FENCE_RE.sub("", text).strip()
    try:
        payload: object = json.loads(text)
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    obj = cast("dict[str, object]", payload)
    equivalent = obj.get("equivalent")
    score = obj.get("score")
    rationale = obj.get("rationale")
    if not isinstance(equivalent, bool):
        return None
    if isinstance(score, bool) or not isinstance(score, (int, float)):
        return None
    if not isinstance(rationale, str) or not rationale.strip():
        return None
    return JudgeVerdict(
        equivalent=equivalent,
        score=max(0.0, min(1.0, float(score))),
        rationale=rationale.strip(),
    )


def run_judge(
    question: str,
    expected_json: str,
    actual_json: str,
    diff: str,
    *,
    run_id: str | None = None,
) -> JudgeOutcome:
    """One judge call. Never raises — every failure mode returns a
    fail-closed JudgeOutcome with the error recorded."""
    prompt = build_judge_prompt(question, expected_json, actual_json, diff)
    try:
        raw = call_llm(prompt, purpose=JUDGE_PURPOSE, scope="eval", run_id=run_id)
    except Exception as exc:
        log.warning(
            {
                "event": "eval_judge_call_failed",
                "error": f"{type(exc).__name__}: {exc}",
            }
        )
        return JudgeOutcome(verdict=None, raw="", error=f"{type(exc).__name__}: {exc}")
    verdict = parse_verdict(raw)
    if verdict is None:
        log.warning({"event": "eval_judge_unparseable", "raw_head": raw[:200]})
        return JudgeOutcome(verdict=None, raw=raw, error="unparseable verdict")
    return JudgeOutcome(verdict=verdict, raw=raw)
