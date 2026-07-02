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
