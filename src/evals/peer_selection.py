"""Mode-A grader for LLM peer selection (directives/peer_selection_llm.md).

The owner's complaint was MISSING comparables (NU → Barclays not Inter/StoneCo;
NOW → Applied Materials not the SaaS platform set). So the ground truth is a
hand-picked set of business-model comparables per ticker and the headline score
is **recall** — did the generator surface the comps a fundamental analyst would
expect? Deterministic, reproducible, no judge layer (mirrors evals.ask_router's
set-vs-set philosophy): the suggested tickers vs the pinned set.

Per case:
  * ``score`` = recall = |returned ∩ expected| / |expected| — the fraction of
    the must-have comps the LLM surfaced. ``--min-score`` gates on the mean.
  * ``passed`` = recall ≥ _CASE_PASS_RECALL (a case is "good enough" when most
    of the known comps appear), reported alongside precision in the rationale.
  * ``also_valid`` tickers are correct-but-not-required: counted in precision
    so a sensible alternative comp isn't punished, never required for recall.

Abort semantics mirror the other graders: a hard stop (budget cap / missing
CLI) raises ``EvalAbortError`` rather than scoring 0 against the prompt; a
``StructuredParseError`` (unusable JSON twice) is an extraction-quality failure
and scores 0 at stage ``call``. The first case erroring twice aborts the run
(transport down ≠ prompt quality).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

from evals.harness import (
    CaseResult,
    EvalRunSummary,
    grade_recall_set_case,
    run_golden_eval,
)
from llm.cli import DEFAULT_MODEL, LLM_MODELS
from llm.prompt_versions import prompt_version_for

log = logging.getLogger(__name__)

PURPOSE = "peer_selection"
DEFAULT_GOLDEN_RELPATH = Path("evals") / "golden" / "peer_selection.json"

# A case "passes" when at least this fraction of the must-have comps surfaced.
# The run-level regression gate uses the mean recall (--min-score), not this.
_CASE_PASS_RECALL = 0.5

# suggest_fn returns the list of suggested peer TICKERS for a case (decouples
# the grader from the PeerSuggestion type; the production wrapper extracts them).
SuggestFn = Callable[["PeerCase"], list[str]]


@dataclass(frozen=True, slots=True)
class PeerCase:
    case_id: str
    ticker: str
    name: str
    business_description: str
    sector: str | None
    industry: str | None
    segments: tuple[str, ...]
    expected_peers: frozenset[str]
    also_valid: frozenset[str] = field(default_factory=frozenset[str])


# ---------------------------------------------------------------------------
# golden-file loading (all-problems validation, mirroring the other graders)
# ---------------------------------------------------------------------------


def _ticker_set(c: dict[str, object], key: str, label: str, errors: list[str]) -> frozenset[str]:
    raw = c.get(key, [])
    if not isinstance(raw, list):
        errors.append(f"{label}: `{key}` must be a list")
        return frozenset()
    return frozenset(str(x).strip().upper() for x in cast("list[object]", raw) if str(x).strip())


def load_peer_selection_golden(path: Path) -> list[PeerCase]:
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
    cases: list[PeerCase] = []
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
        sector = str(c.get("sector")).strip() if c.get("sector") else None
        industry = str(c.get("industry")).strip() if c.get("industry") else None
        seg_raw = c.get("segments", [])
        segments = (
            tuple(str(s).strip() for s in cast("list[object]", seg_raw) if str(s).strip())
            if isinstance(seg_raw, list)
            else ()
        )
        expected = _ticker_set(c, "expected_peers", f"{label} ({case_id})", errors)
        if "expected_peers" not in c or not expected:
            errors.append(f"{label} ({case_id}): needs a non-empty `expected_peers`")
        also_valid = _ticker_set(c, "also_valid", f"{label} ({case_id})", errors)
        cases.append(
            PeerCase(
                case_id=case_id,
                ticker=ticker,
                name=name,
                business_description=description,
                sector=sector,
                industry=industry,
                segments=segments,
                expected_peers=expected,
                also_valid=also_valid,
            )
        )
    if errors:
        raise ValueError(f"golden file invalid at {path}: " + "; ".join(errors))
    return cases


# ---------------------------------------------------------------------------
# grading
# ---------------------------------------------------------------------------


def grade_peer_selection_case(case: PeerCase, *, suggest_fn: SuggestFn) -> CaseResult:
    return grade_recall_set_case(
        purpose=PURPOSE,
        case_id=case.case_id,
        ticker=case.ticker,
        expected=case.expected_peers,
        also_valid=case.also_valid,
        produce=lambda: suggest_fn(case),
        produce_label="suggest_peers",
        normalize=lambda token: token.strip().upper(),
        pass_recall=_CASE_PASS_RECALL,
    )


# ---------------------------------------------------------------------------
# run orchestration
# ---------------------------------------------------------------------------


def _production_suggest(case: PeerCase) -> list[str]:
    """The real generator over the golden case's pinned inputs — one live LLM
    call per case (no FMP fetch; the eval scores the SUGGESTION, not metrics)."""
    from compute.peer_selection import suggest_peers

    suggestions = suggest_peers(
        ticker=case.ticker,
        name=case.name or None,
        business_description=case.business_description,
        segments=list(case.segments),
        sector=case.sector,
        industry=case.industry,
    )
    return [s.ticker for s in suggestions]


def run_peer_selection_eval(
    *,
    golden_path: Path,
    code_root: Path,
    limit: int | None = None,
    suggest_fn: SuggestFn | None = None,
) -> EvalRunSummary:
    """The full mode-A run. Does NOT persist — the caller decides
    (execution/run_llm_evals.py). ``suggest_fn`` injects a fake generator for
    tests; None = the production ``compute.peer_selection.suggest_peers``."""
    target: SuggestFn = suggest_fn if suggest_fn is not None else _production_suggest
    return run_golden_eval(
        purpose=PURPOSE,
        golden_path=golden_path,
        code_root=code_root,
        cases=load_peer_selection_golden(golden_path),
        grade_case=lambda case: grade_peer_selection_case(case, suggest_fn=target),
        prompt_version=prompt_version_for(PURPOSE),
        model=LLM_MODELS.get(PURPOSE, DEFAULT_MODEL),
        limit=limit,
    )
