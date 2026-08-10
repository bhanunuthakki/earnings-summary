"""Mode-A provenance-caution canaries (Close-the-Loops L2).

L2 pushed the scored confidence and the cross-source ⚠ disagreement that S2
computes OUT of the UI popover and INTO the text the answer-writing model
reads (``ask.grounding._fact_item`` appends a ``[conf 79%; ⚠ SEC says $101M,
0.99% delta]`` tag; ``build_evidence_block`` carries the rule to discount such
figures). This eval proves that change earns its keep: given a CONTESTED figure
in the evidence, the model must FLAG / DISCOUNT it, not assert it as settled.

Like the injection canaries (and unlike the golden-set graders) there is no
expected output to match — the ground truth is an invariant checked as code:

  * **the contested figure is not asserted bare** — the answer must surface a
    caution signal (acknowledge the uncertainty, name the disagreeing source,
    or hedge the number). A response with no such signal stated the contested
    value as fact — a fail.

Design rules carried from the injection-canary pilot: drive the PRODUCTION
prompt path (the real ``build_evidence_block`` assembles the evidence + the
discount rule, so both are under test), grade deterministically (no judge — a
hedge is a hedge), version the golden set, abort (not score 0) on a hard stop.
The default runner composes the answer prompt and streams it through the live
``ask.narrative_transport.stream_llm_text`` transport (the same governed path production
answers ride); tests inject a fake runner, so the suite is hermetic and only
the live proof run spends.

Known limit (stated, not hidden): the grader keys off a caution lexicon, so a
genuinely cautious answer that hedges in words outside the lexicon would score
a false fail, and a response that name-drops a marker without truly discounting
would score a false pass. The two-direction unit tests (asserting → fail,
hedging → pass) keep the invariant honest; tighten the lexicon if live runs
drift.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import cast
from uuid import uuid4

from ask.grounding import EvidenceItem, build_evidence_block
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

log = logging.getLogger(__name__)

PURPOSE = "provenance_caution"
GOLDEN_DIR = Path("evals") / "golden"

# The contested-figure kinds a case can present (documents the S2 signal under
# test; both ride the same [conf …; ⚠ …] tag _fact_item appends).
_CAUTION_KINDS: frozenset[str] = frozenset({"low_confidence", "source_disagreement"})

# Words/marks a cautious answer uses to discount or flag a shaky figure. A
# response carrying NONE of these stated the contested number as settled fact.
# Lower-cased substring match; kept broad enough to catch genuine hedging
# without the judge, narrow enough that a flat assertion misses every one.
_CAUTION_MARKERS: tuple[str, ...] = (
    "uncertain",
    "unverified",
    "not verified",
    "not confirmed",
    "unconfirmed",
    "provisional",
    "low confidence",
    "low-confidence",
    "less confident",
    "less reliable",
    "questionable",
    "caveat",
    "caution",
    "disagree",
    "conflict",
    "discrepan",
    "differ",
    "contested",
    "contradic",
    "two sources",
    "other source",
    "delta",
    "restat",
    "revis",
    "approximate",
    "roughly",
    "estimate",
    "reportedly",
    "⚠",
    "~",
    "confidence",
    "should be treated",
    "take with",
    "grain of salt",
)


@dataclass(frozen=True, slots=True)
class CautionCase:
    """One provenance-caution canary. ``evidence_text`` is the enriched fact
    text the model reads (with the ``[conf …; ⚠ …]`` tag); the invariant is
    that the answer discounts the figure rather than asserting it."""

    case_id: str
    ticker: str
    metric: str
    question: str
    evidence_text: str
    caution_kind: str


# ---------------------------------------------------------------------------
# golden-file loading (mirrors injection_canaries / golden_classifiers)
# ---------------------------------------------------------------------------


def _load_doc(path: Path) -> list[dict[str, object]]:
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
    out: list[dict[str, object]] = []
    for i, entry in enumerate(cast("list[object]", raw_cases)):
        if not isinstance(entry, dict):
            raise ValueError(f"cases[{i}]: must be an object")
        out.append(cast("dict[str, object]", entry))
    return out


def _require_str(c: dict[str, object], key: str, label: str, errors: list[str]) -> str:
    v = c.get(key)
    if not isinstance(v, str) or not v.strip():
        errors.append(f"{label}: missing/empty `{key}`")
        return ""
    return v


def _has_caution_tag(evidence_text: str) -> bool:
    """The evidence must actually present a contested figure: a bracketed tag
    carrying a confidence score and/or a ⚠ disagreement (the shape
    ``_fact_item`` appends). Otherwise the case can't fail — it's theatre."""
    start = evidence_text.rfind("[")
    end = evidence_text.rfind("]")
    if start == -1 or end <= start:
        return False
    tag = evidence_text[start + 1 : end]
    return "conf" in tag or "⚠" in tag


def load_caution_golden(path: Path) -> list[CautionCase]:
    errors: list[str] = []
    seen: set[str] = set()
    cases: list[CautionCase] = []
    for i, c in enumerate(_load_doc(path)):
        label = f"cases[{i}]"
        case_id = _require_str(c, "id", label, errors)
        if case_id in seen:
            errors.append(f"{label}: duplicate id {case_id!r}")
        seen.add(case_id)
        ticker = _require_str(c, "ticker", label, errors).upper()
        metric = _require_str(c, "metric", label, errors)
        question = _require_str(c, "question", label, errors)
        evidence_text = _require_str(c, "evidence_text", label, errors)
        kind = _require_str(c, "caution_kind", label, errors)
        if kind and kind not in _CAUTION_KINDS:
            errors.append(
                f"{label} ({case_id}): caution_kind {kind!r} not one of {sorted(_CAUTION_KINDS)}"
            )
        if evidence_text and not _has_caution_tag(evidence_text):
            errors.append(
                f"{label} ({case_id}): evidence_text must carry a [conf …] / [⚠ …] caution tag"
            )
        cases.append(
            CautionCase(
                case_id=case_id,
                ticker=ticker,
                metric=metric,
                question=question,
                evidence_text=evidence_text,
                caution_kind=kind,
            )
        )
    if errors:
        raise ValueError(f"golden file invalid at {path}: " + "; ".join(errors))
    return cases


# ---------------------------------------------------------------------------
# production-path runner — compose the real prompt, return the answer text
# ---------------------------------------------------------------------------

Runner = Callable[[CautionCase], str]

_SYSTEM = (
    "You are a portfolio research assistant. Answer the analyst's question using "
    "ONLY the numbered evidence. Be concise."
)


def build_answer_prompt(case: CautionCase) -> str:
    """The answer prompt for one case, built through the REAL
    ``build_evidence_block`` so the enriched fact text and the discount rule
    are exactly what production injects."""
    item = EvidenceItem(
        n=1,
        kind="fact",
        label=f"{case.ticker} · {case.metric}",
        text=case.evidence_text,
        doc_id=None,
        href=None,
    )
    block = build_evidence_block([item])
    return f"{_SYSTEM}\n\n{block}\n\n---\n\nUSER:\n{case.question}"


def _run_answer(case: CautionCase) -> str:
    """Stream the answer through the live transport (the bare-CLI path
    production answers ride) and return the final text."""
    from ask.narrative_transport import stream_llm_text

    final = ""
    for ev in stream_llm_text(build_answer_prompt(case)):
        kind = ev.get("type")
        if kind == "final":
            final = str(ev.get("text") or "")
        elif kind == "error":
            raise RuntimeError(str(ev.get("error") or "transport error"))
    return final


# ---------------------------------------------------------------------------
# grading
# ---------------------------------------------------------------------------


def caution_markers_in(response_text: str) -> list[str]:
    """Which caution markers the response surfaced (lower-cased substring)."""
    low = response_text.lower()
    return [m for m in _CAUTION_MARKERS if m in low]


def grade_caution_case(case: CautionCase, *, runner: Runner | None = None) -> CaseResult:
    """Run the case's answer path and score the invariant: the contested figure
    must be flagged/discounted, not asserted bare. score is 1.0 (flagged) or
    0.0 (asserted as settled fact)."""
    run = runner or _run_answer
    t0 = time.monotonic()
    try:
        response_text = run(case)
    except Exception as exc:
        _abort_on_hard_stop(exc, case.case_id)
        # A transient/transport failure produced no answer to grade — not a
        # silent pass: record the degradation and score 0 (no proof the model
        # discounted anything), but never let it masquerade as held.
        latency_ms = int((time.monotonic() - t0) * 1000)
        return CaseResult(
            case_id=case.case_id,
            question=f"{PURPOSE}/{case.case_id} ({case.caution_kind})",
            passed=False,
            score=0.0,
            expected_json=dumps_compact({"invariant": "contested figure flagged"}),
            actual_json=dumps_compact({"degraded": f"{type(exc).__name__}: {exc}"}),
            failure_stage="execute",
            judge_rationale=f"answer path degraded ({type(exc).__name__}); no answer to grade",
            latency_ms=latency_ms,
        )
    latency_ms = int((time.monotonic() - t0) * 1000)

    markers = caution_markers_in(response_text)
    passed = bool(markers)
    return CaseResult(
        case_id=case.case_id,
        question=f"{PURPOSE}/{case.case_id} ({case.caution_kind})",
        passed=passed,
        score=1.0 if passed else 0.0,
        expected_json=dumps_compact({"invariant": "contested figure flagged, not asserted bare"}),
        actual_json=dumps_compact(
            {"markers": markers[:8], "response_preview": response_text[:300]}
        ),
        failure_stage=None if passed else "mismatch",
        judge_rationale=(
            f"flagged the contested figure ({', '.join(markers[:4])})"
            if passed
            else "asserted the contested figure with no caution signal"
        ),
        response_text=response_text,
        latency_ms=latency_ms,
    )


def _abort_on_hard_stop(exc: Exception, case_id: str) -> None:
    if is_hard_stop(exc):
        raise EvalAbortError(
            f"{PURPOSE} hard stop on case {case_id}: {type(exc).__name__}: {exc} — "
            "aborting instead of scoring (budget/setup is config, not a quality signal)."
        ) from exc


# ---------------------------------------------------------------------------
# run orchestration
# ---------------------------------------------------------------------------


def run_caution_eval(
    *,
    golden_path: Path,
    code_root: Path,
    limit: int | None = None,
    runner: Runner | None = None,
) -> EvalRunSummary:
    """Full mode-A provenance-caution run. Does NOT persist (the caller
    decides). ``runner`` overrides the production answer path for ALL cases —
    tests inject a fake here to stay hermetic; None streams each case through
    the live transport."""
    cases = load_caution_golden(golden_path)
    if limit is not None:
        cases = cases[: max(0, limit)]

    summary = EvalRunSummary(
        run_id=uuid4().hex,
        purpose=PURPOSE,
        mode="live",
        prompt_version=prompt_version_for(PURPOSE),
        model=LLM_MODELS.get(PURPOSE, DEFAULT_MODEL),
        judge_model=None,  # invariant graded by code — no judge
        golden_set_sha=sha256_file(golden_path),
        started_at=now_naive_utc(),
        git_sha=resolve_git_sha(code_root),
    )
    for case in cases:
        result = grade_caution_case(case, runner=runner)
        summary.cases.append(result)
        log.info(
            {
                "event": "caution_case_graded",
                "case_id": result.case_id,
                "caution_kind": case.caution_kind,
                "passed": result.passed,
            }
        )
    summary.finished_at = now_naive_utc()
    return summary
