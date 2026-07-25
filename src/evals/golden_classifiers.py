"""Mode-A graders for the fast classifiers (llm_evals_plan PR 4):
``transcript_metadata``, ``intake_classifier``, ``news_structuring``.

Same design rules as the viewspec pilot — evaluate the PRODUCTION entry
point, deterministic grading, versioned golden sets, hard stops abort — but
no judge layer: these purposes have closed-form ground truth, so a
divergence is just wrong (the exact-format metadata string, the closed
doc_type enum + fiscal-mapping period_end) or a violated output contract
(the news structurer's own HARD RULES, checked as code invariants because
live web output can't be pinned to an expected value).

Per-case scoring:
  * transcript_metadata — exact string match on TICKER_QX_YYYY / UNKNOWN.
  * intake_classifier   — all three pinned fields (ticker, doc_type,
    period_end) must match; score = matched/3 so a near-miss (right doc,
    wrong quarter-end) is distinguishable from garbage. None (production's
    degraded return) scores 0 at stage ``call``.
  * news_structuring    — score = fraction of returned rows satisfying the
    contract (empty array = vacuous pass); any malformed row fails the case.

Hard stops surface as ``EvalAbortError`` — the production wrappers now
propagate them (same PR) instead of degrading to UNKNOWN/None/[].
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
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

log = logging.getLogger(__name__)

GOLDEN_DIR = Path("evals") / "golden"

CLASSIFIER_PURPOSES: tuple[str, ...] = (
    "transcript_metadata",
    "intake_classifier",
    "news_structuring",
    "decision_conditions_extract",
    "wondering_detect",
    "capture_intent",
    "ledger_reply_intent",
    "triage_route_suggest",
    "segment_10q_period_disambiguate",
    "capture_triage",
    # Decision Draft parse (P2.1, personal_investment_partner_prd.md §9.2/§10.5)
    # — the async capture-triage tap that turns a landed free-text/voice
    # musing into a confirmable decision draft. Golden set covers the PRD's
    # own list verbatim: executed buy/sell, pass/watch/promote, ticker
    # ambiguity, question-vs-decision, voice noise, $-vs-%, correction,
    # musing false positives, and prompt-injection — the parser must prefer
    # an ambiguous draft over a false consequential mutation.
    "decision_draft_parse",
    # D1e (disclosure_intelligence_v1_prd.md): ground truth for the two
    # disclosure-judgment purposes that shipped with zero eval coverage.
    # metric_lifecycle_triage — business_metric/accounting_plumbing +
    # concealment/maturity/unclear over XBRL tag name/label/last-value.
    "metric_lifecycle_triage",
    # disclosure_item_specificity_triage — boilerplate_update/substantive
    # over a diff hunk (risk_factors/mdna item_added/removed/reworded events).
    "disclosure_item_specificity_triage",
)

_METADATA_RX = re.compile(r"^(?:[A-Z][A-Z0-9.]*_Q[1-4]_\d{4}|UNKNOWN)$")
_PUBLISHED_AT_RX = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")

# How far in the future a news row's published_at may sit before it counts as
# fabricated. Generous (timezone wobble between ET/UTC), but catches invented
# dates — the contract's whole point.
_MAX_FUTURE_SLACK = timedelta(days=1)


# ---------------------------------------------------------------------------
# golden-file loading (all-problems validation, mirroring load_golden)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ClassifierCase:
    """One golden case; ``inputs`` are the production function's kwargs and
    ``expected`` is purpose-shaped (str / dict / None for contract cases)."""

    case_id: str
    inputs: dict[str, object]
    expected: object


def _load_doc(path: Path, purpose: str) -> list[dict[str, object]]:
    try:
        payload: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"golden file unreadable at {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("golden file must be a JSON object")
    doc = cast("dict[str, object]", payload)
    if doc.get("purpose") != purpose:
        raise ValueError(f"golden file purpose must be {purpose!r}, got {doc.get('purpose')!r}")
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


def load_transcript_metadata_golden(path: Path) -> list[ClassifierCase]:
    errors: list[str] = []
    seen: set[str] = set()
    cases: list[ClassifierCase] = []
    for i, c in enumerate(_load_doc(path, "transcript_metadata")):
        label = f"cases[{i}]"
        case_id = _require_str(c, "id", label, errors)
        if case_id in seen:
            errors.append(f"{label}: duplicate id {case_id!r}")
        seen.add(case_id)
        text = _require_str(c, "text", label, errors)
        expected = _require_str(c, "expected", label, errors)
        if expected and not _METADATA_RX.match(expected):
            errors.append(f"{label} ({case_id}): expected {expected!r} not TICKER_QX_YYYY/UNKNOWN")
        cases.append(ClassifierCase(case_id, {"text_snippet": text}, expected))
    if errors:
        raise ValueError(f"golden file invalid at {path}: " + "; ".join(errors))
    return cases


def load_intake_classifier_golden(path: Path) -> list[ClassifierCase]:
    errors: list[str] = []
    seen: set[str] = set()
    cases: list[ClassifierCase] = []
    for i, c in enumerate(_load_doc(path, "intake_classifier")):
        label = f"cases[{i}]"
        case_id = _require_str(c, "id", label, errors)
        if case_id in seen:
            errors.append(f"{label}: duplicate id {case_id!r}")
        seen.add(case_id)
        filename = _require_str(c, "filename", label, errors)
        text = _require_str(c, "text", label, errors)
        hint = c.get("hint")
        if not isinstance(hint, dict):
            errors.append(f"{label} ({case_id}): `hint` must be an object")
            hint = {}
        expected = c.get("expected")
        if not isinstance(expected, dict):
            errors.append(f"{label} ({case_id}): `expected` must be an object")
            expected = {}
        exp = cast("dict[str, object]", expected)
        for key in ("ticker", "doc_type", "period_end"):
            if not isinstance(exp.get(key), str) or not str(exp.get(key)).strip():
                errors.append(f"{label} ({case_id}): expected.{key} missing")
        cases.append(
            ClassifierCase(
                case_id,
                {"filename": filename, "text": text, "hint": cast("dict[str, object]", hint)},
                exp,
            )
        )
    if errors:
        raise ValueError(f"golden file invalid at {path}: " + "; ".join(errors))
    return cases


_CONDITION_GRADED_FIELDS = ("metric", "metric_source", "op", "threshold", "unit", "for_periods")


def _str_list(value: object) -> list[str] | None:
    """``value`` as a list of strings, or None when it isn't one."""
    if not isinstance(value, list):
        return None
    items = cast("list[object]", value)
    if not all(isinstance(x, str) for x in items):
        return None
    return cast("list[str]", items)


def load_decision_conditions_golden(path: Path) -> list[ClassifierCase]:
    """Cases for decision_conditions.extract_conditions: a memo section +
    the ticker's metric vocabulary; ``expected`` is the pinned condition
    list (graded fields only — ``note`` is freeform)."""
    errors: list[str] = []
    seen: set[str] = set()
    cases: list[ClassifierCase] = []
    for i, c in enumerate(_load_doc(path, "decision_conditions_extract")):
        label = f"cases[{i}]"
        case_id = _require_str(c, "id", label, errors)
        if case_id in seen:
            errors.append(f"{label}: duplicate id {case_id!r}")
        seen.add(case_id)
        ticker = _require_str(c, "ticker", label, errors).upper()
        section = _require_str(c, "section", label, errors)
        kpi_names = _str_list(c.get("kpi_names"))
        line_items = _str_list(c.get("line_items"))
        if kpi_names is None:
            errors.append(f"{label} ({case_id}): `kpi_names` must be a list of strings")
            kpi_names = []
        if line_items is None:
            errors.append(f"{label} ({case_id}): `line_items` must be a list of strings")
            line_items = []
        expected = c.get("expected")
        if not isinstance(expected, list):
            errors.append(f"{label} ({case_id}): `expected` must be a list")
            expected = []
        for j, entry in enumerate(cast("list[object]", expected)):
            if not isinstance(entry, dict):
                errors.append(f"{label} ({case_id}): expected[{j}] must be an object")
                continue
            entry_obj = cast("dict[str, object]", entry)
            missing = [k for k in _CONDITION_GRADED_FIELDS if k not in entry_obj]
            if missing:
                errors.append(f"{label} ({case_id}): expected[{j}] missing {missing}")
        cases.append(
            ClassifierCase(
                case_id,
                {
                    "section": section,
                    "ticker": ticker,
                    "kpi_names": list(kpi_names),
                    "line_items": list(line_items),
                },
                list(cast("list[object]", expected)),
            )
        )
    if errors:
        raise ValueError(f"golden file invalid at {path}: " + "; ".join(errors))
    return cases


def load_wondering_detect_golden(path: Path) -> list[ClassifierCase]:
    """Cases for research.detect.detect_for_eval: a musing ``text``; ``expected``
    is ``{is_wondering: bool, ticker: str|None}`` (the precision gate's fields)."""
    errors: list[str] = []
    seen: set[str] = set()
    cases: list[ClassifierCase] = []
    for i, c in enumerate(_load_doc(path, "wondering_detect")):
        label = f"cases[{i}]"
        case_id = _require_str(c, "id", label, errors)
        if case_id in seen:
            errors.append(f"{label}: duplicate id {case_id!r}")
        seen.add(case_id)
        text = _require_str(c, "text", label, errors)
        expected = c.get("expected")
        if not isinstance(expected, dict):
            errors.append(f"{label} ({case_id}): `expected` must be an object")
            expected = {}
        exp = cast("dict[str, object]", expected)
        if not isinstance(exp.get("is_wondering"), bool):
            errors.append(f"{label} ({case_id}): expected.is_wondering must be a bool")
        cases.append(ClassifierCase(case_id, {"text": text}, exp))
    if errors:
        raise ValueError(f"golden file invalid at {path}: " + "; ".join(errors))
    return cases


def load_capture_intent_golden(path: Path) -> list[ClassifierCase]:
    """Cases for research.intent.classify_intent_for_eval: a musing ``text``;
    ``expected`` is ``{intent: <one of INTENTS>, ticker: str|None}`` (the fields the
    intent gate routes on). A wrong intent routes the musing to the wrong action —
    the whole failure mode — so it is what's pinned."""
    from research.intent import INTENTS

    errors: list[str] = []
    seen: set[str] = set()
    cases: list[ClassifierCase] = []
    for i, c in enumerate(_load_doc(path, "capture_intent")):
        label = f"cases[{i}]"
        case_id = _require_str(c, "id", label, errors)
        if case_id in seen:
            errors.append(f"{label}: duplicate id {case_id!r}")
        seen.add(case_id)
        text = _require_str(c, "text", label, errors)
        expected = c.get("expected")
        if not isinstance(expected, dict):
            errors.append(f"{label} ({case_id}): `expected` must be an object")
            expected = {}
        exp = cast("dict[str, object]", expected)
        intent = exp.get("intent")
        if not isinstance(intent, str) or intent not in INTENTS:
            errors.append(f"{label} ({case_id}): expected.intent must be one of {list(INTENTS)}")
        cases.append(ClassifierCase(case_id, {"text": text}, exp))
    if errors:
        raise ValueError(f"golden file invalid at {path}: " + "; ".join(errors))
    return cases


def load_ledger_reply_intent_golden(path: Path) -> list[ClassifierCase]:
    """Cases for onmymind.reply.classify_reply_for_eval: a feed ``card_text`` +
    the owner's ``reply_text``; ``expected`` is ``{intent: <one of REPLY_INTENTS>}``.
    A wrong intent routes the reply to the wrong action (research/save/worldview/
    dismiss vs converse), so the intent enum is the only pinned field."""
    from onmymind.reply import REPLY_INTENTS

    errors: list[str] = []
    seen: set[str] = set()
    cases: list[ClassifierCase] = []
    for i, c in enumerate(_load_doc(path, "ledger_reply_intent")):
        label = f"cases[{i}]"
        case_id = _require_str(c, "id", label, errors)
        if case_id in seen:
            errors.append(f"{label}: duplicate id {case_id!r}")
        seen.add(case_id)
        card_text = _require_str(c, "card_text", label, errors)
        reply_text = _require_str(c, "reply_text", label, errors)
        expected = c.get("expected")
        if not isinstance(expected, dict):
            errors.append(f"{label} ({case_id}): `expected` must be an object")
            expected = {}
        exp = cast("dict[str, object]", expected)
        intent = exp.get("intent")
        if not isinstance(intent, str) or intent not in REPLY_INTENTS:
            errors.append(
                f"{label} ({case_id}): expected.intent must be one of {list(REPLY_INTENTS)}"
            )
        cases.append(
            ClassifierCase(case_id, {"card_text": card_text, "reply_text": reply_text}, exp)
        )
    if errors:
        raise ValueError(f"golden file invalid at {path}: " + "; ".join(errors))
    return cases


def load_triage_route_suggest_golden(path: Path) -> list[ClassifierCase]:
    """Cases for user_state.triage_suggest.suggest_route_for_eval: a parked
    ``comment_text`` (+ optional ``context_line``); ``expected`` is
    ``{intent: <a ROUTABLE_INTENTS member OR null for park>}``. A null intent is a
    VALID graded outcome (the comment should stay a manual pick) — the loader
    accepts it exactly like capture_intent's null ticker."""
    from user_state.notes import ROUTABLE_INTENTS

    errors: list[str] = []
    seen: set[str] = set()
    cases: list[ClassifierCase] = []
    for i, c in enumerate(_load_doc(path, "triage_route_suggest")):
        label = f"cases[{i}]"
        case_id = _require_str(c, "id", label, errors)
        if case_id in seen:
            errors.append(f"{label}: duplicate id {case_id!r}")
        seen.add(case_id)
        comment_text = _require_str(c, "comment_text", label, errors)
        context_raw = c.get("context_line", "")
        context_line = context_raw if isinstance(context_raw, str) else ""
        expected = c.get("expected")
        if not isinstance(expected, dict):
            errors.append(f"{label} ({case_id}): `expected` must be an object")
            expected = {}
        exp = cast("dict[str, object]", expected)
        # `intent` must be present; None (park) is valid, else a ROUTABLE member.
        if "intent" not in exp:
            errors.append(f"{label} ({case_id}): expected.intent missing (use null for park)")
        else:
            intent = exp.get("intent")
            if intent is not None and (
                not isinstance(intent, str) or intent not in ROUTABLE_INTENTS
            ):
                errors.append(
                    f"{label} ({case_id}): expected.intent must be null (park) or one of "
                    f"{list(ROUTABLE_INTENTS)}"
                )
        cases.append(
            ClassifierCase(
                case_id, {"comment_text": comment_text, "context_line": context_line}, exp
            )
        )
    if errors:
        raise ValueError(f"golden file invalid at {path}: " + "; ".join(errors))
    return cases


def load_capture_triage_golden(path: Path) -> list[ClassifierCase]:
    """Cases for capture.triage.classify_capture_triage_for_eval: a case dict
    carrying its own ``text`` + ``context`` (tenets/stances/musings/decisions —
    the same shape ``capture.triage._gather_from_case`` renders), so the golden
    set never needs a fixture DB. ``expected`` is ``{route: <one of ROUTES>,
    conflicts_with: <token or null>}`` — a wrong route OR a wrong/fabricated
    citation IS the failure mode this classifier exists to prevent."""
    from capture.triage import ROUTES

    errors: list[str] = []
    seen: set[str] = set()
    cases: list[ClassifierCase] = []
    for i, c in enumerate(_load_doc(path, "capture_triage")):
        label = f"cases[{i}]"
        case_id = _require_str(c, "id", label, errors)
        if case_id in seen:
            errors.append(f"{label}: duplicate id {case_id!r}")
        seen.add(case_id)
        text = _require_str(c, "text", label, errors)
        context_raw = c.get("context")
        if not isinstance(context_raw, dict):
            errors.append(f"{label} ({case_id}): `context` must be an object")
        context: dict[str, object] = (
            cast("dict[str, object]", context_raw) if isinstance(context_raw, dict) else {}
        )
        expected = c.get("expected")
        if not isinstance(expected, dict):
            errors.append(f"{label} ({case_id}): `expected` must be an object")
            expected = {}
        exp = cast("dict[str, object]", expected)
        route = exp.get("route")
        if not isinstance(route, str) or route not in ROUTES:
            errors.append(f"{label} ({case_id}): expected.route must be one of {list(ROUTES)}")
        if "conflicts_with" not in exp:
            errors.append(f"{label} ({case_id}): expected.conflicts_with missing (use null)")
        else:
            conflicts_with = exp.get("conflicts_with")
            if conflicts_with is not None and not isinstance(conflicts_with, str):
                errors.append(
                    f"{label} ({case_id}): expected.conflicts_with must be a string or null"
                )
        case_payload: dict[str, object] = {"text": text, "context": context}
        cases.append(ClassifierCase(case_id, {"case": case_payload}, exp))
    if errors:
        raise ValueError(f"golden file invalid at {path}: " + "; ".join(errors))
    return cases


def load_segment_10q_period_disambiguate_golden(path: Path) -> list[ClassifierCase]:
    """Cases for compute.segment_quarterly_10q.disambiguate_periods_for_eval:
    a 10-Q section's column ``labels`` list plus the resolved-context hint
    (nominal quarter, cumulative-months expectation, current/prior-year end
    dates); ``expected`` is ``{"columns": [{"index", "duration_months",
    "is_cumulative"}, ...]}`` — one entry per label, by index. Deliberately
    includes at least one genuinely-ambiguous case per the task's own
    validation requirement (the deterministic path already resolves the
    unambiguous cases; this golden set exists to grade the LLM fallback
    specifically, so every case here is one the regex/duration-prefix parser
    could NOT resolve on its own)."""
    errors: list[str] = []
    seen: set[str] = set()
    cases: list[ClassifierCase] = []
    for i, c in enumerate(_load_doc(path, "segment_10q_period_disambiguate")):
        label = f"cases[{i}]"
        case_id = _require_str(c, "id", label, errors)
        if case_id in seen:
            errors.append(f"{label}: duplicate id {case_id!r}")
        seen.add(case_id)
        labels_raw = c.get("labels")
        labels = _str_list(labels_raw)
        if labels is None or not labels:
            errors.append(f"{label} ({case_id}): `labels` must be a non-empty list of strings")
            labels = []
        nominal_quarter = _require_str(c, "nominal_quarter", label, errors)
        if nominal_quarter not in ("Q1", "Q2", "Q3"):
            errors.append(f"{label} ({case_id}): nominal_quarter must be one of Q1/Q2/Q3")
        cumulative_months = c.get("cumulative_months")
        if not isinstance(cumulative_months, int) or isinstance(cumulative_months, bool):
            errors.append(f"{label} ({case_id}): cumulative_months must be an int")
            cumulative_months = 0
        current_end = _require_str(c, "current_end", label, errors)
        prior_year_end = _require_str(c, "prior_year_end", label, errors)
        expected = c.get("expected")
        if not isinstance(expected, dict):
            errors.append(f"{label} ({case_id}): `expected` must be an object")
            expected = {}
        exp = cast("dict[str, object]", expected)
        exp_columns = exp.get("columns")
        if not isinstance(exp_columns, list):
            errors.append(f"{label} ({case_id}): expected.columns must be a list")
        cases.append(
            ClassifierCase(
                case_id,
                {
                    "labels": labels,
                    "nominal_quarter": nominal_quarter,
                    "cumulative_months": cumulative_months,
                    "current_end": current_end,
                    "prior_year_end": prior_year_end,
                },
                exp,
            )
        )
    if errors:
        raise ValueError(f"golden file invalid at {path}: " + "; ".join(errors))
    return cases


def load_news_structuring_golden(path: Path) -> list[ClassifierCase]:
    errors: list[str] = []
    seen: set[str] = set()
    cases: list[ClassifierCase] = []
    for i, c in enumerate(_load_doc(path, "news_structuring")):
        label = f"cases[{i}]"
        case_id = _require_str(c, "id", label, errors)
        if case_id in seen:
            errors.append(f"{label}: duplicate id {case_id!r}")
        seen.add(case_id)
        ticker = _require_str(c, "ticker", label, errors).upper()
        days_raw = c.get("news_days", 2)
        if isinstance(days_raw, bool) or not isinstance(days_raw, int) or days_raw < 1:
            errors.append(f"{label} ({case_id}): news_days must be a positive int")
            days_raw = 2
        cases.append(ClassifierCase(case_id, {"ticker": ticker, "news_days": days_raw}, None))
    if errors:
        raise ValueError(f"golden file invalid at {path}: " + "; ".join(errors))
    return cases


# ---------------------------------------------------------------------------
# per-purpose grading
# ---------------------------------------------------------------------------


def _abort_on_hard_stop(exc: Exception, purpose: str, case_id: str) -> None:
    if is_hard_stop(exc):
        raise EvalAbortError(
            f"{purpose} hard stop on case {case_id}: {type(exc).__name__}: {exc} — "
            "aborting instead of scoring 0s."
        ) from exc


def grade_transcript_metadata_case(case: ClassifierCase, *, fn: Callable[..., str]) -> CaseResult:
    expected = cast("str", case.expected)
    t0 = time.monotonic()
    try:
        actual = fn(case.inputs["text_snippet"]).strip()
    except Exception as exc:
        _abort_on_hard_stop(exc, "transcript_metadata", case.case_id)
        raise  # production fn degrades transients to "UNKNOWN"; anything else is a bug
    latency_ms = int((time.monotonic() - t0) * 1000)
    passed = actual == expected
    return CaseResult(
        case_id=case.case_id,
        question=f"transcript_metadata/{case.case_id}",
        passed=passed,
        score=1.0 if passed else 0.0,
        expected_json=dumps_compact(expected),
        actual_json=dumps_compact(actual),
        failure_stage=None if passed else "mismatch",
        judge_rationale=None if passed else f"expected {expected!r}, got {actual!r}",
        latency_ms=latency_ms,
    )


_INTAKE_PINNED_FIELDS = ("ticker", "doc_type", "period_end")


def grade_intake_classifier_case(
    case: ClassifierCase, *, fn: Callable[..., dict[str, object] | None]
) -> CaseResult:
    expected = cast("dict[str, object]", case.expected)
    t0 = time.monotonic()
    try:
        actual = fn(
            cast("str", case.inputs["filename"]),
            cast("str", case.inputs["text"]),
            cast("dict[str, object]", case.inputs["hint"]),
        )
    except Exception as exc:
        _abort_on_hard_stop(exc, "intake_classifier", case.case_id)
        raise
    latency_ms = int((time.monotonic() - t0) * 1000)
    expected_json = dumps_compact({k: expected.get(k) for k in _INTAKE_PINNED_FIELDS})
    if not isinstance(actual, dict):
        return CaseResult(
            case_id=case.case_id,
            question=f"intake_classifier/{case.case_id}",
            passed=False,
            score=0.0,
            expected_json=expected_json,
            actual_json=None,
            failure_stage="call",
            judge_rationale="production classifier returned None (call/parse failure)",
            latency_ms=latency_ms,
        )
    obj = actual
    diffs: list[str] = []
    matched = 0
    for key in _INTAKE_PINNED_FIELDS:
        exp_v = str(expected.get(key, "")).strip()
        act_v = str(obj.get(key, "")).strip()
        if exp_v == act_v:
            matched += 1
        else:
            diffs.append(f"{key}: expected {exp_v!r}, got {act_v!r}")
    score = matched / len(_INTAKE_PINNED_FIELDS)
    passed = matched == len(_INTAKE_PINNED_FIELDS)
    return CaseResult(
        case_id=case.case_id,
        question=f"intake_classifier/{case.case_id}",
        passed=passed,
        score=score,
        expected_json=expected_json,
        actual_json=dumps_compact({k: obj.get(k) for k in _INTAKE_PINNED_FIELDS}),
        failure_stage=None if passed else "mismatch",
        judge_rationale=None if passed else "; ".join(diffs),
        latency_ms=latency_ms,
    )


def _condition_key(obj: dict[str, object]) -> tuple[object, ...]:
    """The graded identity of one condition. metric compares case-insensitively
    (the model copies vocabulary names verbatim, but a stray case change is
    not an analytical error); threshold through float() so 7 == 7.0."""
    threshold = obj.get("threshold")
    threshold_f = (
        float(threshold)
        if isinstance(threshold, (int, float)) and not isinstance(threshold, bool)
        else None
    )
    metric = obj.get("metric")
    return (
        metric.strip().lower() if isinstance(metric, str) else None,
        obj.get("metric_source"),
        obj.get("op"),
        threshold_f,
        obj.get("unit"),
        obj.get("for_periods"),
    )


def grade_decision_conditions_case(
    case: ClassifierCase, *, fn: Callable[..., list[object]]
) -> CaseResult:
    """Order-insensitive compare of extracted vs pinned conditions on the
    graded fields. score = matches / max(n_expected, n_actual); both empty is
    a vacuous pass (the no-falsifiable-conditions negative case)."""
    expected = cast("list[object]", case.expected)
    t0 = time.monotonic()
    try:
        actual = fn(
            cast("str", case.inputs["section"]),
            ticker=cast("str", case.inputs["ticker"]),
            kpi_names=cast("list[str]", case.inputs["kpi_names"]),
            line_items=cast("list[str]", case.inputs["line_items"]),
        )
    except Exception as exc:
        _abort_on_hard_stop(exc, "decision_conditions_extract", case.case_id)
        # StructuredParseError (both attempts unusable) is an extraction
        # quality failure, not a config problem — score it 0, keep running.
        latency_ms = int((time.monotonic() - t0) * 1000)
        return CaseResult(
            case_id=case.case_id,
            question=f"decision_conditions_extract/{case.case_id}",
            passed=False,
            score=0.0,
            expected_json=dumps_compact(expected),
            actual_json=None,
            failure_stage="call",
            judge_rationale=f"extraction raised {type(exc).__name__}: {exc}",
            latency_ms=latency_ms,
        )
    latency_ms = int((time.monotonic() - t0) * 1000)

    expected_keys = [
        _condition_key(cast("dict[str, object]", e)) for e in expected if isinstance(e, dict)
    ]
    actual_objs: list[dict[str, object]] = []
    for cond in actual:
        as_json = getattr(cond, "as_json_obj", None)
        obj = as_json() if callable(as_json) else cond
        if isinstance(obj, dict):
            actual_objs.append(cast("dict[str, object]", obj))
    actual_keys = [_condition_key(o) for o in actual_objs]

    remaining = list(actual_keys)
    matched = 0
    misses: list[str] = []
    for exp_key in expected_keys:
        if exp_key in remaining:
            remaining.remove(exp_key)
            matched += 1
        else:
            misses.append(f"expected condition not extracted: {exp_key}")
    for extra in remaining:
        misses.append(f"unexpected condition extracted: {extra}")

    denom = max(len(expected_keys), len(actual_keys))
    score = 1.0 if denom == 0 else matched / denom
    passed = score == 1.0
    return CaseResult(
        case_id=case.case_id,
        question=f"decision_conditions_extract/{case.case_id}",
        passed=passed,
        score=score,
        expected_json=dumps_compact(expected),
        actual_json=dumps_compact(actual_objs),
        failure_stage=None if passed else "mismatch",
        judge_rationale=None if passed else "; ".join(misses[:5]),
        latency_ms=latency_ms,
    )


def validate_news_row(row: object, *, now: datetime | None = None) -> str | None:
    """One row against the structurer's HARD RULES. None = valid, else the
    violation (the first one found — enough to fail the row)."""
    if not isinstance(row, dict):
        return f"row is {type(row).__name__}, not an object"
    obj = cast("dict[str, object]", row)
    headline = obj.get("headline")
    if not isinstance(headline, str) or not headline.strip():
        return "missing/empty headline"
    url = obj.get("url")
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        return f"url missing or not http(s): {url!r}"
    published_at = obj.get("published_at")
    if not isinstance(published_at, str) or not _PUBLISHED_AT_RX.match(published_at):
        return f"published_at not 'YYYY-MM-DD HH:MM:SS': {published_at!r}"
    tz = obj.get("published_tz")
    if tz not in ("UTC", "ET"):
        return f"published_tz not UTC/ET: {tz!r}"
    try:
        stamp = datetime.strptime(published_at, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return f"published_at unparseable: {published_at!r}"
    ref = now or datetime.now(UTC).replace(tzinfo=None)
    if stamp > ref + _MAX_FUTURE_SLACK:
        return f"published_at in the future (fabricated date?): {published_at}"
    source = obj.get("source")
    if not isinstance(source, str) or not source.strip():
        return "missing/empty source"
    return None


def grade_news_structuring_case(
    case: ClassifierCase, *, fn: Callable[..., list[object]]
) -> CaseResult:
    t0 = time.monotonic()
    try:
        rows = fn(
            cast("str", case.inputs["ticker"]),
            news_days=cast("int", case.inputs["news_days"]),
        )
    except Exception as exc:
        _abort_on_hard_stop(exc, "news_structuring", case.case_id)
        raise
    latency_ms = int((time.monotonic() - t0) * 1000)
    violations = [
        f"row[{i}]: {problem}"
        for i, row in enumerate(rows)
        if (problem := validate_news_row(row)) is not None
    ]
    n = len(rows)
    score = 1.0 if n == 0 else (n - len(violations)) / n
    passed = not violations
    return CaseResult(
        case_id=case.case_id,
        question=f"news_structuring/{case.case_id} ({case.inputs['ticker']})",
        passed=passed,
        score=score,
        expected_json=dumps_compact({"contract": "every row satisfies the HARD RULES"}),
        actual_json=dumps_compact({"n_rows": n, "violations": violations[:10]}),
        failure_stage=None if passed else "contract",
        judge_rationale=(f"{n} rows, all contract-valid" if passed else "; ".join(violations[:5])),
        latency_ms=latency_ms,
    )


def grade_wondering_detect_case(
    case: ClassifierCase, *, fn: Callable[..., dict[str, object]]
) -> CaseResult:
    """The precision gate: the binary ``is_wondering`` must match (a wrong verdict
    IS the whole failure mode — a false positive wastes a research run). When the
    verdict is a wondering AND the golden pins a ticker, the ticker must match too
    for a full pass (partial credit 0.5 for the right verdict, wrong ticker)."""
    expected = cast("dict[str, object]", case.expected)
    exp_wonder = bool(expected.get("is_wondering"))
    t0 = time.monotonic()
    try:
        actual = fn(cast("str", case.inputs["text"]))
    except Exception as exc:
        _abort_on_hard_stop(exc, "wondering_detect", case.case_id)
        raise
    latency_ms = int((time.monotonic() - t0) * 1000)
    act = actual  # fn is typed dict[str, object]; detect_for_eval always returns one
    wonder_ok = bool(act.get("is_wondering")) == exp_wonder
    exp_ticker = expected.get("ticker")
    ticker_ok = True
    if wonder_ok and exp_wonder and isinstance(exp_ticker, str) and exp_ticker.strip():
        act_ticker = act.get("ticker")
        ticker_ok = (
            isinstance(act_ticker, str) and act_ticker.strip().upper() == exp_ticker.strip().upper()
        )
    passed = wonder_ok and ticker_ok
    score = 1.0 if passed else (0.5 if wonder_ok else 0.0)
    return CaseResult(
        case_id=case.case_id,
        question=f"wondering_detect/{case.case_id}",
        passed=passed,
        score=score,
        expected_json=dumps_compact({"is_wondering": exp_wonder, "ticker": exp_ticker}),
        actual_json=dumps_compact(act),
        failure_stage=None if passed else ("ticker" if wonder_ok else "verdict"),
        judge_rationale=None
        if passed
        else f"expected is_wondering={exp_wonder} ticker={exp_ticker!r}",
        latency_ms=latency_ms,
    )


def grade_capture_intent_case(
    case: ClassifierCase, *, fn: Callable[..., dict[str, object]]
) -> CaseResult:
    """The intent gate: the returned ``intent`` enum must match (a wrong intent
    routes the musing to the wrong action — observation/wondering/brief/stress each
    fire a different downstream). When the golden pins a ticker, a ticker mismatch
    drops a full pass to partial credit 0.5 (right intent, wrong name)."""
    expected = cast("dict[str, object]", case.expected)
    exp_intent = str(expected.get("intent") or "")
    t0 = time.monotonic()
    try:
        actual = fn(cast("str", case.inputs["text"]))
    except Exception as exc:
        _abort_on_hard_stop(exc, "capture_intent", case.case_id)
        raise
    latency_ms = int((time.monotonic() - t0) * 1000)
    act = actual  # fn is typed dict[str, object]; classify_intent_for_eval returns one
    act_intent = str(act.get("intent") or "")
    intent_ok = act_intent == exp_intent
    exp_ticker = expected.get("ticker")
    ticker_ok = True
    if intent_ok and isinstance(exp_ticker, str) and exp_ticker.strip():
        act_ticker = act.get("ticker")
        ticker_ok = (
            isinstance(act_ticker, str) and act_ticker.strip().upper() == exp_ticker.strip().upper()
        )
    passed = intent_ok and ticker_ok
    score = 1.0 if passed else (0.5 if intent_ok else 0.0)
    return CaseResult(
        case_id=case.case_id,
        question=f"capture_intent/{case.case_id}",
        passed=passed,
        score=score,
        expected_json=dumps_compact({"intent": exp_intent, "ticker": exp_ticker}),
        actual_json=dumps_compact(act),
        failure_stage=None if passed else ("ticker" if intent_ok else "intent"),
        judge_rationale=None
        if passed
        else f"expected intent={exp_intent!r} ticker={exp_ticker!r}, got {act_intent!r}",
        latency_ms=latency_ms,
    )


def grade_ledger_reply_intent_case(
    case: ClassifierCase, *, fn: Callable[..., dict[str, object]]
) -> CaseResult:
    """The reply router: the returned ``intent`` enum must exactly match. Each of
    the six intents (research/save/worldview/dismiss/question/note) fires a
    different downstream, so a wrong intent IS the whole failure mode — exact
    match, score 1.0/0.0, no partial credit."""
    expected = cast("dict[str, object]", case.expected)
    exp_intent = str(expected.get("intent") or "")
    t0 = time.monotonic()
    try:
        actual = fn(
            cast("str", case.inputs["card_text"]),
            cast("str", case.inputs["reply_text"]),
        )
    except Exception as exc:
        _abort_on_hard_stop(exc, "ledger_reply_intent", case.case_id)
        raise
    latency_ms = int((time.monotonic() - t0) * 1000)
    act = actual  # fn is typed dict[str, object]; classify_reply_for_eval returns one
    act_intent = str(act.get("intent") or "")
    passed = act_intent == exp_intent
    return CaseResult(
        case_id=case.case_id,
        question=f"ledger_reply_intent/{case.case_id}",
        passed=passed,
        score=1.0 if passed else 0.0,
        expected_json=dumps_compact({"intent": exp_intent}),
        actual_json=dumps_compact(act),
        failure_stage=None if passed else "intent",
        judge_rationale=None if passed else f"expected intent={exp_intent!r}, got {act_intent!r}",
        latency_ms=latency_ms,
    )


def grade_capture_triage_case(
    case: ClassifierCase, *, fn: Callable[..., dict[str, object]]
) -> CaseResult:
    """The capture->answer primary gate: ``route`` must match exactly (each of
    the three routes fires a completely different downstream — answer via the
    engine, a deterministic challenge, or nothing at all). When the expected
    route is ``contradiction``, a route match with the WRONG (or a fabricated/
    missing) ``conflicts_with`` token drops to partial credit — the grounding
    gate is what stops a hallucinated citation, so route-right/citation-wrong
    is a real miss, not a full pass."""
    expected = cast("dict[str, object]", case.expected)
    exp_route = str(expected.get("route") or "")
    exp_token = expected.get("conflicts_with")
    t0 = time.monotonic()
    try:
        actual = fn(cast("dict[str, object]", case.inputs["case"]))
    except Exception as exc:
        _abort_on_hard_stop(exc, "capture_triage", case.case_id)
        raise
    latency_ms = int((time.monotonic() - t0) * 1000)
    act = actual  # fn is typed dict[str, object]; classify_capture_triage_for_eval returns one
    act_route = str(act.get("route") or "")
    route_ok = act_route == exp_route
    token_ok = True
    if (
        route_ok
        and exp_route == "contradiction"
        and isinstance(exp_token, str)
        and exp_token.strip()
    ):
        act_token = act.get("conflicts_with")
        token_ok = isinstance(act_token, str) and act_token.strip() == exp_token.strip()
    passed = route_ok and token_ok
    score = 1.0 if passed else (0.5 if route_ok else 0.0)
    return CaseResult(
        case_id=case.case_id,
        question=f"capture_triage/{case.case_id}",
        passed=passed,
        score=score,
        expected_json=dumps_compact({"route": exp_route, "conflicts_with": exp_token}),
        actual_json=dumps_compact(act),
        failure_stage=None if passed else ("conflicts_with" if route_ok else "route"),
        judge_rationale=None
        if passed
        else f"expected route={exp_route!r} conflicts_with={exp_token!r}, got {act_route!r}",
        latency_ms=latency_ms,
    )


def grade_triage_route_suggest_case(
    case: ClassifierCase, *, fn: Callable[..., dict[str, object]]
) -> CaseResult:
    """Second-chance routing for parked comments. Intent-correctness is the whole
    grade, weighted by HARM asymmetry (a wrong high-confidence answer auto-routes —
    the worst outcome):

      * intent correct (incl. park==park)            → 1.0
      * model parked when a route was expected       → 0.5 (safe miss: stays manual)
      * wrong route, LOW confidence                  → 0.25 (only a suggestion)
      * wrong route, HIGH confidence                 → 0.0 (auto-routes wrongly — worst)

    ``expected.intent`` is ``None`` for a park case; the model returns ``intent``
    None too when it parks."""
    expected = cast("dict[str, object]", case.expected)
    exp_intent_raw = expected.get("intent")
    exp_intent = exp_intent_raw if isinstance(exp_intent_raw, str) else None
    t0 = time.monotonic()
    try:
        actual = fn(
            cast("str", case.inputs["comment_text"]),
            cast("str", case.inputs["context_line"]),
        )
    except Exception as exc:
        _abort_on_hard_stop(exc, "triage_route_suggest", case.case_id)
        raise
    latency_ms = int((time.monotonic() - t0) * 1000)
    act = actual  # fn is typed dict[str, object]; suggest_route_for_eval returns one
    act_intent_raw = act.get("intent")
    act_intent = act_intent_raw if isinstance(act_intent_raw, str) else None
    act_conf = str(act.get("confidence") or "low").strip().lower()

    if act_intent == exp_intent:
        score, stage, why = 1.0, None, None
    elif act_intent is None:
        # Model parked when a route was expected — a recall miss, but harmless
        # (the row stays a manual pick, nothing is mis-filed).
        score, stage, why = 0.5, "under_route", f"parked; expected route {exp_intent!r}"
    elif act_conf == "high":
        # Wrong route at high confidence — the sweep auto-files it. Worst case.
        score, stage, why = (
            0.0,
            "auto_route",
            f"wrong HIGH-confidence route {act_intent!r}, expected {exp_intent!r}",
        )
    else:
        # Wrong route at low confidence — surfaces only as a one-tap suggestion.
        score, stage, why = (
            0.25,
            "route",
            f"wrong low-confidence route {act_intent!r}, expected {exp_intent!r}",
        )
    passed = score == 1.0
    return CaseResult(
        case_id=case.case_id,
        question=f"triage_route_suggest/{case.case_id}",
        passed=passed,
        score=score,
        expected_json=dumps_compact({"intent": exp_intent}),
        actual_json=dumps_compact(act),
        failure_stage=stage,
        judge_rationale=why,
        latency_ms=latency_ms,
    )


def grade_segment_10q_period_disambiguate_case(
    case: ClassifierCase, *, fn: Callable[..., dict[str, object]]
) -> CaseResult:
    """Per-column duration_months + is_cumulative must both match (a wrong
    is_cumulative flag is the classification error that matters most — it
    decides whether a value lands as a discrete quarter or a YTD cumulative
    fact). score = matched/n_expected; a missing index (the LLM omitted an
    ambiguous column rather than guessing) counts as a miss, not a crash."""
    expected = cast("dict[str, object]", case.expected)
    exp_columns_raw = expected.get("columns")
    exp_columns = cast("list[object]", exp_columns_raw) if isinstance(exp_columns_raw, list) else []
    exp_by_index: dict[int, tuple[object, object]] = {}
    for entry in exp_columns:
        if not isinstance(entry, dict):
            continue
        e = cast("dict[str, object]", entry)
        idx = e.get("index")
        if isinstance(idx, int) and not isinstance(idx, bool):
            exp_by_index[idx] = (e.get("duration_months"), bool(e.get("is_cumulative")))

    t0 = time.monotonic()
    try:
        actual = fn(
            cast("list[str]", case.inputs["labels"]),
            nominal_quarter=cast("str", case.inputs["nominal_quarter"]),
            cumulative_months=cast("int", case.inputs["cumulative_months"]),
            current_end=cast("str", case.inputs["current_end"]),
            prior_year_end=cast("str", case.inputs["prior_year_end"]),
        )
    except Exception as exc:
        _abort_on_hard_stop(exc, "segment_10q_period_disambiguate", case.case_id)
        latency_ms = int((time.monotonic() - t0) * 1000)
        return CaseResult(
            case_id=case.case_id,
            question=f"segment_10q_period_disambiguate/{case.case_id}",
            passed=False,
            score=0.0,
            expected_json=dumps_compact(expected),
            actual_json=None,
            failure_stage="call",
            judge_rationale=f"disambiguation raised {type(exc).__name__}: {exc}",
            latency_ms=latency_ms,
        )
    latency_ms = int((time.monotonic() - t0) * 1000)

    act_columns_raw = actual.get("columns")
    act_by_index: dict[int, tuple[object, object]] = {}
    if isinstance(act_columns_raw, list):
        for entry in cast("list[object]", act_columns_raw):
            if not isinstance(entry, dict):
                continue
            e = cast("dict[str, object]", entry)
            idx = e.get("index")
            if isinstance(idx, int) and not isinstance(idx, bool):
                act_by_index[idx] = (e.get("duration_months"), bool(e.get("is_cumulative")))

    misses: list[str] = []
    matched = 0
    for idx, exp_pair in exp_by_index.items():
        act_pair = act_by_index.get(idx)
        if act_pair == exp_pair:
            matched += 1
        else:
            misses.append(f"col[{idx}]: expected {exp_pair}, got {act_pair}")
    denom = len(exp_by_index)
    score = 1.0 if denom == 0 else matched / denom
    passed = score == 1.0
    return CaseResult(
        case_id=case.case_id,
        question=f"segment_10q_period_disambiguate/{case.case_id}",
        passed=passed,
        score=score,
        expected_json=dumps_compact(expected),
        actual_json=dumps_compact(actual),
        failure_stage=None if passed else "mismatch",
        judge_rationale=None if passed else "; ".join(misses[:5]),
        latency_ms=latency_ms,
    )


def load_decision_draft_parse_golden(path: Path) -> list[ClassifierCase]:
    """Cases for ``capture.decision_draft.parse_note_for_eval``: a case dict
    carrying its own ``text`` + ``roster`` (tracked symbols) -> the pinned
    intent/ticker/action fields. ``expected`` is either
    ``{"intent", "proposed_ticker", "proposed_action"}`` (a null field means
    "don't check this one") or ``{"prefer_ambiguous": true}`` for the
    ambiguity/injection/voice-noise cases — the parser must leave BOTH
    ``proposed_ticker`` and ``proposed_action`` null (or emit an ambiguity
    note) rather than force a consequential guess on those."""
    from capture.decision_draft import INTENTS

    errors: list[str] = []
    seen: set[str] = set()
    cases: list[ClassifierCase] = []
    for i, c in enumerate(_load_doc(path, "decision_draft_parse")):
        label = f"cases[{i}]"
        case_id = _require_str(c, "id", label, errors)
        if case_id in seen:
            errors.append(f"{label}: duplicate id {case_id!r}")
        seen.add(case_id)
        text = _require_str(c, "text", label, errors)
        roster_raw = c.get("roster")
        roster = (
            [str(t) for t in cast("list[object]", roster_raw)]
            if isinstance(roster_raw, list)
            else []
        )
        expected = c.get("expected")
        if not isinstance(expected, dict):
            errors.append(f"{label} ({case_id}): `expected` must be an object")
            expected = {}
        exp = cast("dict[str, object]", expected)
        if not exp.get("prefer_ambiguous"):
            intent = exp.get("intent")
            if intent is not None and (not isinstance(intent, str) or intent not in INTENTS):
                errors.append(
                    f"{label} ({case_id}): expected.intent must be null or one of {list(INTENTS)}"
                )
        cases.append(ClassifierCase(case_id, {"text": text, "roster": roster}, exp))
    if errors:
        raise ValueError(f"golden file invalid at {path}: " + "; ".join(errors))
    return cases


_METRIC_RELEVANCE = ("business_metric", "accounting_plumbing")
_METRIC_PRIOR = ("concealment", "maturity", "unclear")


def load_metric_lifecycle_triage_golden(path: Path) -> list[ClassifierCase]:
    """Cases for ``filings.metric_triage.triage_candidates`` (D1e,
    disclosure_intelligence_v1_prd.md). Each case is ONE hand-labeled
    ``metric_discontinued`` prod row, reconstructed as a real
    ``MetricCandidate`` from the same tag/label/last-value/silence fields the
    production prompt renders (never a document/filing text — this purpose
    never sees one). ``expected`` pins BOTH axes (``relevance``, ``prior``);
    a case counts as passed only when both match, mirroring
    ``grade_intake_classifier_case``'s partial-credit pattern."""
    from filings.metric_lifecycle import Axis, CandidateKind, MetricCandidate, XbrlObservation

    errors: list[str] = []
    seen: set[str] = set()
    cases: list[ClassifierCase] = []
    for i, c in enumerate(_load_doc(path, "metric_lifecycle_triage")):
        label = f"cases[{i}]"
        case_id = _require_str(c, "id", label, errors)
        if case_id in seen:
            errors.append(f"{label}: duplicate id {case_id!r}")
        seen.add(case_id)
        ticker = _require_str(c, "ticker", label, errors).upper()
        taxonomy = _require_str(c, "taxonomy", label, errors)
        tag = _require_str(c, "tag", label, errors)
        tag_label = _require_str(c, "label", label, errors)
        axis_raw = c.get("axis")
        try:
            axis = Axis(str(axis_raw))
        except ValueError:
            errors.append(f"{label} ({case_id}): axis must be 'annual' or 'quarterly'")
            axis = Axis.ANNUAL
        fiscal_year = c.get("fiscal_year")
        fiscal_period = c.get("fiscal_period")
        if not isinstance(fiscal_year, int) or isinstance(fiscal_year, bool):
            errors.append(f"{label} ({case_id}): fiscal_year must be an int")
            fiscal_year = 2020
        if not isinstance(fiscal_period, str) or not fiscal_period:
            errors.append(f"{label} ({case_id}): fiscal_period must be a non-empty str")
            fiscal_period = "FY"
        last_value = c.get("last_value")
        if not isinstance(last_value, (int, float)) or isinstance(last_value, bool):
            errors.append(f"{label} ({case_id}): last_value must be a number")
            last_value = 0.0
        current_silence = c.get("current_silence")
        historical_max_gap = c.get("historical_max_gap")
        if not isinstance(current_silence, int) or isinstance(current_silence, bool):
            errors.append(f"{label} ({case_id}): current_silence must be an int")
            current_silence = 0
        if not isinstance(historical_max_gap, int) or isinstance(historical_max_gap, bool):
            errors.append(f"{label} ({case_id}): historical_max_gap must be an int")
            historical_max_gap = 0
        expected = c.get("expected")
        if not isinstance(expected, dict):
            errors.append(f"{label} ({case_id}): `expected` must be an object")
            expected = {}
        exp = cast("dict[str, object]", expected)
        relevance = exp.get("relevance")
        prior = exp.get("prior")
        if relevance not in _METRIC_RELEVANCE:
            errors.append(
                f"{label} ({case_id}): expected.relevance must be one of {_METRIC_RELEVANCE}"
            )
        if prior not in _METRIC_PRIOR:
            errors.append(f"{label} ({case_id}): expected.prior must be one of {_METRIC_PRIOR}")
        candidate = MetricCandidate(
            ticker=ticker,
            taxonomy=taxonomy,
            tag=tag,
            label=tag_label,
            axis=axis,
            last_observation=XbrlObservation(
                fiscal_year=fiscal_year,
                fiscal_period=str(fiscal_period),
                form="10-K" if axis is Axis.ANNUAL else "10-Q",
                end=f"{fiscal_year}-12-31",
                val=float(last_value),
                accn="0000000000-00-000000",
                filed=f"{fiscal_year}-12-31",
            ),
            current_silence=current_silence,
            historical_max_gap=historical_max_gap,
            kind=CandidateKind.DISCONTINUED,
        )
        cases.append(ClassifierCase(case_id, {"ticker": ticker, "candidate": candidate}, exp))
    if errors:
        raise ValueError(f"golden file invalid at {path}: " + "; ".join(errors))
    return cases


def load_disclosure_item_specificity_triage_golden(path: Path) -> list[ClassifierCase]:
    """Cases for ``filings.boilerplate_triage.triage_events`` (D1e,
    disclosure_intelligence_v1_prd.md). Each case is ONE hand-labeled
    item-level prod row, carrying the exact diff hunk / excerpt the
    production prompt would see (reconstructed with the SAME reduction —
    ``filings.specificity.extract_diff_hunk`` for reworded items — the
    production classifier applies), never a whole item body/section/
    document. ``expected`` pins the ``verdict`` field only (``confidence``
    is continuous and not graded)."""
    errors: list[str] = []
    seen: set[str] = set()
    cases: list[ClassifierCase] = []
    for i, c in enumerate(_load_doc(path, "disclosure_item_specificity_triage")):
        label = f"cases[{i}]"
        case_id = _require_str(c, "id", label, errors)
        if case_id in seen:
            errors.append(f"{label}: duplicate id {case_id!r}")
        seen.add(case_id)
        ticker = _require_str(c, "ticker", label, errors).upper()
        event_type = _require_str(c, "event_type", label, errors)
        heading = _require_str(c, "heading", label, errors)
        hunk = _require_str(c, "hunk", label, errors)
        prod_event_id = c.get("prod_event_id")
        if not isinstance(prod_event_id, int) or isinstance(prod_event_id, bool):
            errors.append(f"{label} ({case_id}): prod_event_id must be an int")
            prod_event_id = 0
        expected = c.get("expected")
        if not isinstance(expected, dict):
            errors.append(f"{label} ({case_id}): `expected` must be an object")
            expected = {}
        exp = cast("dict[str, object]", expected)
        verdict = exp.get("verdict")
        if verdict not in ("boilerplate_update", "substantive"):
            errors.append(
                f"{label} ({case_id}): expected.verdict must be boilerplate_update or substantive"
            )
        candidate: tuple[int, str, str, str] = (prod_event_id, event_type, heading, hunk)
        cases.append(ClassifierCase(case_id, {"ticker": ticker, "candidate": candidate}, exp))
    if errors:
        raise ValueError(f"golden file invalid at {path}: " + "; ".join(errors))
    return cases


def grade_decision_draft_parse_case(
    case: ClassifierCase, *, fn: Callable[..., dict[str, object]]
) -> CaseResult:
    """Intent + ticker + action correctness, EXCEPT for ``prefer_ambiguous``
    cases (ticker ambiguity, prompt-injection, voice noise) where the only
    thing graded is that the parser did NOT force a consequential
    ticker+action pair — the PRD's own bar ("the parser must prefer an
    ambiguous draft over a false consequential mutation")."""
    expected = cast("dict[str, object]", case.expected)
    t0 = time.monotonic()
    try:
        actual = fn(case.inputs)
    except Exception as exc:
        _abort_on_hard_stop(exc, "decision_draft_parse", case.case_id)
        raise
    latency_ms = int((time.monotonic() - t0) * 1000)
    act_ticker = actual.get("proposed_ticker")
    act_action = actual.get("proposed_action")

    if expected.get("prefer_ambiguous"):
        safe = act_ticker is None and act_action is None
        return CaseResult(
            case_id=case.case_id,
            question=f"decision_draft_parse/{case.case_id}",
            passed=safe,
            score=1.0 if safe else 0.0,
            expected_json=dumps_compact({"prefer_ambiguous": True}),
            actual_json=dumps_compact(actual),
            failure_stage=None if safe else "false_consequential_mutation",
            judge_rationale=(
                None if safe else "parser forced a ticker/action on ambiguous/adversarial input"
            ),
            latency_ms=latency_ms,
        )

    exp_intent = expected.get("intent")
    exp_ticker = expected.get("proposed_ticker")
    exp_action = expected.get("proposed_action")
    act_intent = actual.get("intent")
    intent_ok = exp_intent is None or act_intent == exp_intent
    ticker_ok = exp_ticker is None or act_ticker == exp_ticker
    action_ok = exp_action is None or act_action == exp_action
    passed = intent_ok and ticker_ok and action_ok
    score = (
        (0.5 if intent_ok else 0.0) + (0.25 if ticker_ok else 0.0) + (0.25 if action_ok else 0.0)
    )
    return CaseResult(
        case_id=case.case_id,
        question=f"decision_draft_parse/{case.case_id}",
        passed=passed,
        score=score,
        expected_json=dumps_compact(expected),
        actual_json=dumps_compact(actual),
        failure_stage=None if passed else "mismatch",
        judge_rationale=(
            None
            if passed
            else (
                f"expected intent={exp_intent!r} ticker={exp_ticker!r} action={exp_action!r}, "
                f"got {act_intent!r}/{act_ticker!r}/{act_action!r}"
            )
        ),
        latency_ms=latency_ms,
    )


def grade_metric_lifecycle_triage_case(
    case: ClassifierCase, *, fn: Callable[..., object]
) -> CaseResult:
    """Calls the REAL ``triage_candidates(ticker, [candidate])`` — a single-
    candidate batch, same call shape production uses per-ticker just with
    one member. Both ``relevance`` and ``prior`` must match; a degraded
    outcome (LLM failure) or a missing verdict both score 0 at stage
    ``call`` — never treated as a pass."""
    from filings.metric_lifecycle import MetricCandidate

    expected = cast("dict[str, object]", case.expected)
    ticker = cast("str", case.inputs["ticker"])
    candidate = cast("MetricCandidate", case.inputs["candidate"])
    t0 = time.monotonic()
    try:
        outcome = fn(ticker, [candidate])
    except Exception as exc:
        _abort_on_hard_stop(exc, "metric_lifecycle_triage", case.case_id)
        raise
    latency_ms = int((time.monotonic() - t0) * 1000)
    degraded = bool(getattr(outcome, "degraded", False))
    verdict = None if degraded else getattr(outcome, "verdicts", {}).get(candidate.qualified_name)
    if verdict is None:
        return CaseResult(
            case_id=case.case_id,
            question=f"metric_lifecycle_triage/{case.case_id}",
            passed=False,
            score=0.0,
            expected_json=dumps_compact(expected),
            actual_json=None,
            failure_stage="call",
            judge_rationale=(
                "triage degraded (LLM call/parse failure)"
                if degraded
                else "no verdict returned for this candidate"
            ),
            latency_ms=latency_ms,
        )
    act_relevance = verdict.relevance.value
    act_prior = verdict.prior.value
    relevance_ok = act_relevance == expected.get("relevance")
    prior_ok = act_prior == expected.get("prior")
    passed = relevance_ok and prior_ok
    score = (0.5 if relevance_ok else 0.0) + (0.5 if prior_ok else 0.0)
    return CaseResult(
        case_id=case.case_id,
        question=f"metric_lifecycle_triage/{case.case_id}",
        passed=passed,
        score=score,
        expected_json=dumps_compact(expected),
        actual_json=dumps_compact({"relevance": act_relevance, "prior": act_prior}),
        failure_stage=None if passed else "mismatch",
        judge_rationale=(
            None
            if passed
            else f"expected relevance={expected.get('relevance')!r} prior={expected.get('prior')!r}, "
            f"got {act_relevance!r}/{act_prior!r}"
        ),
        latency_ms=latency_ms,
    )


def grade_disclosure_item_specificity_triage_case(
    case: ClassifierCase, *, fn: Callable[..., object]
) -> CaseResult:
    """Calls the REAL ``triage_events(ticker, [candidate])`` — a single-item
    batch. Only ``verdict`` is graded (``confidence`` is continuous)."""
    expected = cast("dict[str, object]", case.expected)
    ticker = cast("str", case.inputs["ticker"])
    candidate = cast("tuple[int, str, str, str]", case.inputs["candidate"])
    event_id = candidate[0]
    t0 = time.monotonic()
    try:
        outcome = fn(ticker, [candidate])
    except Exception as exc:
        _abort_on_hard_stop(exc, "disclosure_item_specificity_triage", case.case_id)
        raise
    latency_ms = int((time.monotonic() - t0) * 1000)
    degraded = bool(getattr(outcome, "degraded", False))
    verdict = None if degraded else getattr(outcome, "verdicts", {}).get(event_id)
    if verdict is None:
        return CaseResult(
            case_id=case.case_id,
            question=f"disclosure_item_specificity_triage/{case.case_id}",
            passed=False,
            score=0.0,
            expected_json=dumps_compact(expected),
            actual_json=None,
            failure_stage="call",
            judge_rationale=(
                "triage degraded (LLM call/parse failure)"
                if degraded
                else "no verdict returned for this candidate"
            ),
            latency_ms=latency_ms,
        )
    act_verdict = verdict.verdict.value
    passed = act_verdict == expected.get("verdict")
    return CaseResult(
        case_id=case.case_id,
        question=f"disclosure_item_specificity_triage/{case.case_id}",
        passed=passed,
        score=1.0 if passed else 0.0,
        expected_json=dumps_compact(expected),
        actual_json=dumps_compact({"verdict": act_verdict}),
        failure_stage=None if passed else "mismatch",
        judge_rationale=(
            None if passed else f"expected verdict={expected.get('verdict')!r}, got {act_verdict!r}"
        ),
        latency_ms=latency_ms,
    )


# ---------------------------------------------------------------------------
# run orchestration
# ---------------------------------------------------------------------------


def _production_fn(purpose: str) -> Callable[..., object]:
    # Late imports — llm_client pulls the full client stack; the eval CLI
    # imports this module before deciding which purpose runs. The casts pin
    # the boundary types (llm_client predates strict typing).
    import llm_client

    if purpose == "transcript_metadata":
        return cast("Callable[..., object]", llm_client.identify_transcript_metadata)
    if purpose == "intake_classifier":
        return cast("Callable[..., object]", llm_client.classify_intake_document)
    if purpose == "decision_conditions_extract":
        import decision_conditions

        return cast("Callable[..., object]", decision_conditions.extract_conditions)
    if purpose == "wondering_detect":
        import research.detect as detect

        return cast("Callable[..., object]", detect.detect_for_eval)
    if purpose == "capture_intent":
        import research.intent as intent

        return cast("Callable[..., object]", intent.classify_intent_for_eval)
    if purpose == "ledger_reply_intent":
        import onmymind.reply as reply

        return cast("Callable[..., object]", reply.classify_reply_for_eval)
    if purpose == "triage_route_suggest":
        import user_state.triage_suggest as triage_suggest

        return cast("Callable[..., object]", triage_suggest.suggest_route_for_eval)
    if purpose == "segment_10q_period_disambiguate":
        import compute.segment_quarterly_10q as segment_quarterly_10q

        return cast("Callable[..., object]", segment_quarterly_10q.disambiguate_periods_for_eval)
    if purpose == "capture_triage":
        import capture.triage as triage

        return cast("Callable[..., object]", triage.classify_capture_triage_for_eval)
    if purpose == "decision_draft_parse":
        import capture.decision_draft as decision_draft

        return cast("Callable[..., object]", decision_draft.parse_note_for_eval)
    if purpose == "metric_lifecycle_triage":
        import filings.metric_triage as metric_triage

        return cast("Callable[..., object]", metric_triage.triage_candidates)
    if purpose == "disclosure_item_specificity_triage":
        import filings.boilerplate_triage as boilerplate_triage

        return cast("Callable[..., object]", boilerplate_triage.triage_events)
    return llm_client.structure_recent_news_json


def run_classifier_eval(
    purpose: str,
    *,
    golden_path: Path,
    code_root: Path,
    limit: int | None = None,
    fn: Callable[..., object] | None = None,
) -> EvalRunSummary:
    """The full mode-A run for one classifier purpose. Does NOT persist —
    the caller decides (execution/run_llm_evals.py). ``fn`` injects a fake
    production function for tests; None = the real one."""
    grade: Callable[..., CaseResult]
    if purpose == "transcript_metadata":
        cases = load_transcript_metadata_golden(golden_path)
        grade = grade_transcript_metadata_case
    elif purpose == "intake_classifier":
        cases = load_intake_classifier_golden(golden_path)
        grade = grade_intake_classifier_case
    elif purpose == "news_structuring":
        cases = load_news_structuring_golden(golden_path)
        grade = grade_news_structuring_case
    elif purpose == "decision_conditions_extract":
        cases = load_decision_conditions_golden(golden_path)
        grade = grade_decision_conditions_case
    elif purpose == "wondering_detect":
        cases = load_wondering_detect_golden(golden_path)
        grade = grade_wondering_detect_case
    elif purpose == "capture_intent":
        cases = load_capture_intent_golden(golden_path)
        grade = grade_capture_intent_case
    elif purpose == "ledger_reply_intent":
        cases = load_ledger_reply_intent_golden(golden_path)
        grade = grade_ledger_reply_intent_case
    elif purpose == "triage_route_suggest":
        cases = load_triage_route_suggest_golden(golden_path)
        grade = grade_triage_route_suggest_case
    elif purpose == "segment_10q_period_disambiguate":
        cases = load_segment_10q_period_disambiguate_golden(golden_path)
        grade = grade_segment_10q_period_disambiguate_case
    elif purpose == "capture_triage":
        cases = load_capture_triage_golden(golden_path)
        grade = grade_capture_triage_case
    elif purpose == "decision_draft_parse":
        cases = load_decision_draft_parse_golden(golden_path)
        grade = grade_decision_draft_parse_case
    elif purpose == "metric_lifecycle_triage":
        cases = load_metric_lifecycle_triage_golden(golden_path)
        grade = grade_metric_lifecycle_triage_case
    elif purpose == "disclosure_item_specificity_triage":
        cases = load_disclosure_item_specificity_triage_golden(golden_path)
        grade = grade_disclosure_item_specificity_triage_case
    else:
        raise ValueError(
            f"no classifier eval for purpose {purpose!r} — known: {list(CLASSIFIER_PURPOSES)}"
        )
    if limit is not None:
        cases = cases[: max(0, limit)]
    target_fn = fn or _production_fn(purpose)

    summary = EvalRunSummary(
        run_id=uuid4().hex,
        purpose=purpose,
        mode="live",
        prompt_version=prompt_version_for(purpose),
        model=LLM_MODELS.get(purpose, DEFAULT_MODEL),
        judge_model=None,  # closed-form ground truth — no judge layer
        golden_set_sha=sha256_file(golden_path),
        started_at=now_naive_utc(),
        git_sha=resolve_git_sha(code_root),
    )
    for case in cases:
        result = grade(case, fn=target_fn)
        summary.cases.append(result)
        log.info(
            {
                "event": "eval_case_graded",
                "purpose": purpose,
                "case_id": result.case_id,
                "passed": result.passed,
                "score": result.score,
                "failure_stage": result.failure_stage,
            }
        )
    summary.finished_at = now_naive_utc()
    return summary
