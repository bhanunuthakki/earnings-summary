"""P4 transcript longitudinal tracking — the LLM judgment calls.

Everything in ``transcripts.longitudinal`` is deterministic. This module is
the ONLY place an LLM is invoked: ONE batched per-transcript call scoring
each management speaker's tone (-1.0..+1.0), and ONE batched per-ticker call
triaging KPI/topic relevance — never per question, never per KPI candidate.
Haiku-tier models (``FAST_CLASSIFIER_MODEL`` via ``llm.cli.LLM_MODELS``),
mirroring ``filings.metric_triage``'s precedent (closed classification over
a short, bounded candidate list, not open-ended generation).

DROPPED (D1.6, owner ruling, 2026-07-25): the per-transcript call used to
ALSO ask a "is this answer evasive?" (non-answer) question, one verdict per
Q&A exchange. That construct never reproduced Gow/Larcker/Zakolyukina's
~11% baseline against this book (measured 30.1%, spread widened, not
narrowed) and the owner ruled DROP, not repair. It has been removed from
the prompt entirely (not just unused downstream) — a prior build kept
asking the LLM for verdicts nobody consumed, which is exactly the kind of
half-dead surface that gets rebuilt naively later. Tone scoring is
unaffected and stays the production path for D2.3 (see
``transcripts.longitudinal.fit_tone_residual_model``). This is a prompt-
shape change, so every previously-cached ``transcript_qa_judgment`` artifact
misses on its next read (different input_sha256) and is re-judged ONCE on
the next real (non-``--no-llm``) detector run — a bounded, expected,
Haiku-tier cost, not a new recurring call.

Token efficiency: the tone-judgment prompt below carries ONLY the paired
question/answer excerpts and the management speakers' own answer text —
never the full call, never prepared remarks. The topic-triage prompt
carries ONLY KPI names (from ``kpi_definitions``), matching
``metric_triage``'s "tag names only, never documents" contract exactly.

Both calls degrade honestly on failure (the repo's silent-degradation-class
rule): a failed or unparseable call returns a degraded result carrying NO
verdicts, never a fabricated "neutral" / "not relevant" default for every
candidate. Hard stops (budget cap / missing CLI, per
``llm.cli.is_hard_stop``) propagate untouched so a caller fails the whole
run loudly instead of mistaking a setup problem for a judgment miss.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import cast

from pydantic import BaseModel, Field

from llm.cli import is_hard_stop
from llm.contracts import TRANSCRIPT_TONE_SCHEMA, TRANSCRIPT_TOPIC_SCHEMA
from llm.structured import call_llm_structured
from llm_artifact_store import (
    UpsertRequest,
    artifact_is_reusable,
    compute_input_sha256,
    read_current,
    upsert,
)
from transcripts.longitudinal import CallSnapshot

log = logging.getLogger(__name__)

QA_JUDGMENT_PURPOSE = "transcript_qa_judgment"
TOPIC_TRIAGE_PURPOSE = "transcript_topic_triage"

# No budget row / golden set added yet (same posture as metric_lifecycle_triage,
# src/llm/cli.py) — flag if/when a golden-set-gated downgrade loop is warranted.
# v2 (D1.6): the non-answer question was removed from the prompt entirely —
# bumped so a stale v1-cached artifact (with a "questions" key nothing reads
# anymore) is never mistaken for a fresh v2 read.
_PROMPT_VERSION = "v2"

# Bound prompt size per exchange/speaker so a runaway call (some real
# transcripts run 50+ exchanges) can't blow past a sane token budget. This
# truncates the LOWEST-value tail of very long individual turns, not whole
# exchanges — coverage (how many exchanges got judged) is never silently
# reduced by this cap; `_MAX_EXCHANGES` below is the one knob that reduces
# coverage, and it is reported explicitly in the caller's summary.
_MAX_CHARS_PER_TURN = 700
_MAX_CHARS_PER_SPEAKER_TONE_SAMPLE = 2500
_MAX_EXCHANGES = 30


class ToneVerdict(BaseModel):
    score: float = Field(ge=-1.0, le=1.0)
    rationale: str = Field(max_length=300)


class CallJudgment(BaseModel):
    """Result of the ONE batched per-transcript LLM call.

    ``degraded=True`` means the call/parse failed — ``tone`` is then
    guaranteed empty, never a fabricated "neutral" default for every
    candidate (silent-degradation-class rule).
    """

    transcript_id: int
    tone: dict[str, ToneVerdict] = Field(default_factory=dict["str", "ToneVerdict"])
    exchanges_judged: int = 0
    exchanges_truncated: int = 0
    degraded: bool = False
    degrade_reason: str | None = None


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + " …[truncated]"


def build_qa_judgment_prompt(snapshot: CallSnapshot) -> tuple[str, int]:
    """Render the prompt; returns ``(prompt, exchanges_included)``."""
    exchanges = snapshot.exchanges[:_MAX_EXCHANGES]
    q_lines: list[str] = []
    for ex in exchanges:
        q = _truncate(ex.question_text, _MAX_CHARS_PER_TURN)
        a = _truncate(ex.answer_text, _MAX_CHARS_PER_TURN)
        q_lines.append(
            f'[{ex.index}] Analyst ({ex.analyst_name or "unknown"}): "{q}"\n'
            f'    Management ({", ".join(ex.answer_speakers) or "unknown"}): "{a}"'
        )
    questions_block = "\n\n".join(q_lines) if q_lines else "(no Q&A exchanges detected)"

    management_names = sorted({name for ex in exchanges for name in ex.answer_speakers if name})
    tone_lines: list[str] = []
    for name in management_names:
        combined = " ".join(ex.answer_text for ex in exchanges if name in ex.answer_speakers)
        tone_lines.append(f'- {name}: "{_truncate(combined, _MAX_CHARS_PER_SPEAKER_TONE_SAMPLE)}"')
    tone_block = "\n".join(tone_lines) if tone_lines else "(no management speakers identified)"

    prompt = f"""You are analyzing the Q&A portion of {snapshot.ticker}'s {snapshot.fiscal_period_type} \
{snapshot.fiscal_year} earnings call. You are given ONLY the analyst questions and management's \
answers (no prepared remarks, no other context), for context on what each speaker discussed:

QUESTIONS AND ANSWERS:
{questions_block}

TASK — For EACH management speaker listed below, score their overall tone across their answers \
on a scale from -1.0 (very negative / defensive / hedging) to +1.0 (very positive / confident), \
based on the LANGUAGE alone (not the underlying numbers, which you cannot see). Provide one \
sentence of rationale citing representative phrasing.

MANAGEMENT SPEAKERS:
{tone_block}

Return ONLY a JSON object of this exact shape (every management speaker above MUST appear as a \
key in "tone"):

{{
  "tone": {{"<speaker name>": {{"score": <float -1.0 to 1.0>, "rationale": "..."}}, ...}}
}}"""
    return prompt, len(exchanges)


def _cache_inputs(snapshot: CallSnapshot, prompt: str) -> list[bytes | str]:
    return [
        f"transcript_id={snapshot.transcript_id}",
        f"parse_coverage={snapshot.parse_coverage}",
        "prompt_sha=" + hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
    ]


def judge_call(
    snapshot: CallSnapshot,
    *,
    db_path: Path | str | None = None,
) -> CallJudgment:
    """Run (or fetch from cache) the ONE batched LLM call for this transcript.

    Cached via ``llm_artifact_store`` keyed on
    (ticker, purpose='transcript_qa_judgment', fiscal_period=<Q-year>) so
    re-running the detector against a transcript that has already been
    judged costs zero new LLM tokens — the historical series this module's
    deltas depend on is built up for free after each call's first score.
    """
    if not snapshot.exchanges:
        return CallJudgment(transcript_id=snapshot.transcript_id)

    prompt, n_included = build_qa_judgment_prompt(snapshot)
    fiscal_period = f"{snapshot.fiscal_period_type}-{snapshot.fiscal_year}"
    cache_inputs = _cache_inputs(snapshot, prompt)

    cached = read_current(
        ticker=snapshot.ticker,
        purpose=QA_JUDGMENT_PURPOSE,
        fiscal_period=fiscal_period,
        db_path=db_path,
    )
    expected_sha = compute_input_sha256(prompt_version=_PROMPT_VERSION, cache_inputs=cache_inputs)
    if (
        cached is not None
        and artifact_is_reusable(cached, input_sha256=expected_sha)
        and isinstance(cached.content_json, dict)
    ):
        return _parse_judgment(
            cast("dict[str, object]", cached.content_json),
            snapshot=snapshot,
            n_included=n_included,
        )

    try:
        decoded = call_llm_structured(
            prompt,
            purpose=QA_JUDGMENT_PURPOSE,
            ticker=snapshot.ticker,
            expect="object",
            schema=TRANSCRIPT_TONE_SCHEMA,
            db_path=db_path,
        )
    except Exception as exc:
        if is_hard_stop(exc):
            raise
        log.error(
            {
                "event": "transcript_qa_judgment_failed_degrading",
                "ticker": snapshot.ticker,
                "transcript_id": snapshot.transcript_id,
                "error": f"{type(exc).__name__}: {exc}",
            }
        )
        return CallJudgment(
            transcript_id=snapshot.transcript_id,
            degraded=True,
            degrade_reason=f"{type(exc).__name__}: {str(exc)[:300]}",
        )

    payload = cast("dict[str, object]", decoded)
    upsert(
        UpsertRequest(
            ticker=snapshot.ticker,
            purpose=QA_JUDGMENT_PURPOSE,
            fiscal_period=fiscal_period,
            content_json=payload,
            cache_inputs=cache_inputs,
            prompt_version=_PROMPT_VERSION,
        ),
        db_path=db_path,
    )
    return _parse_judgment(payload, snapshot=snapshot, n_included=n_included)


def _parse_judgment(
    payload: dict[str, object], *, snapshot: CallSnapshot, n_included: int
) -> CallJudgment:
    tone_raw = payload.get("tone")
    tone: dict[str, ToneVerdict] = {}
    if isinstance(tone_raw, dict):
        for k, v in cast("dict[str, object]", tone_raw).items():
            if not isinstance(v, dict):
                continue
            row = cast("dict[str, object]", v)
            try:
                score = float(cast("str | int | float", row.get("score")))
            except (TypeError, ValueError):
                continue
            score = max(-1.0, min(1.0, score))
            try:
                tone[k] = ToneVerdict(score=score, rationale=str(row.get("rationale") or "")[:300])
            except (ValueError, TypeError):
                continue
    return CallJudgment(
        transcript_id=snapshot.transcript_id,
        tone=tone,
        exchanges_judged=len(tone),
        exchanges_truncated=max(0, len(snapshot.exchanges) - n_included),
    )


# ---------------------------------------------------------------------------
# Topic-relevance triage (batched per ticker, KPI names only)
# ---------------------------------------------------------------------------


class TopicVerdict(BaseModel):
    relevant: bool
    rationale: str = Field(max_length=300)


class TopicTriageOutcome(BaseModel):
    ticker: str
    verdicts: dict[str, TopicVerdict] = Field(default_factory=dict["str", "TopicVerdict"])
    degraded: bool = False
    degrade_reason: str | None = None


def _build_topic_prompt(ticker: str, candidate_names: list[str]) -> str:
    listing = "\n".join(f"- {name}" for name in candidate_names)
    return f"""You are triaging KPI/topic names that {ticker}'s management mentioned on the last \
{1 + 2} earnings calls but did NOT mention on the most recent call. You are given ONLY the KPI \
catalog names below — no transcript text.

For EACH name, decide whether an investor would consider it a BUSINESS-MEANINGFUL metric whose \
disappearance from a call is worth noticing (examples: a named growth metric, a segment KPI, a \
margin figure, a user/volume metric) versus a name that is too vague, too compound (lists several \
unrelated concepts), or too generic to treat as a single trackable topic. Provide one sentence of \
rationale.

CANDIDATE NAMES:
{listing}

Return ONLY a JSON object: {{"<name>": {{"relevant": true|false, "rationale": "..."}}, ...}}. \
Every name above MUST appear as a key exactly once, using the exact text given."""


def triage_topics(
    ticker: str,
    candidate_names: list[str],
    *,
    db_path: Path | str | None = None,
) -> TopicTriageOutcome:
    """ONE batched call over candidate KPI/topic names for this ticker. Never
    raises except on a hard stop (per ``is_hard_stop``) — every other
    failure degrades to an empty verdict map, never a fabricated
    all-relevant or all-irrelevant default."""
    if not candidate_names:
        return TopicTriageOutcome(ticker=ticker)
    prompt = _build_topic_prompt(ticker, candidate_names)
    try:
        decoded = call_llm_structured(
            prompt,
            purpose=TOPIC_TRIAGE_PURPOSE,
            ticker=ticker,
            expect="object",
            schema=TRANSCRIPT_TOPIC_SCHEMA,
            db_path=db_path,
        )
    except Exception as exc:
        if is_hard_stop(exc):
            raise
        log.error(
            {
                "event": "transcript_topic_triage_failed_degrading",
                "ticker": ticker,
                "n_candidates": len(candidate_names),
                "error": f"{type(exc).__name__}: {exc}",
            }
        )
        return TopicTriageOutcome(
            ticker=ticker,
            degraded=True,
            degrade_reason=f"{type(exc).__name__}: {str(exc)[:300]}",
        )

    verdicts: dict[str, TopicVerdict] = {}
    for name, raw in cast("dict[str, object]", decoded).items():
        if not isinstance(raw, dict):
            continue
        row = cast("dict[str, object]", raw)
        try:
            verdicts[name] = TopicVerdict(
                relevant=bool(row.get("relevant")),
                rationale=str(row.get("rationale") or "")[:300],
            )
        except (ValueError, TypeError):
            continue
    missing = set(candidate_names) - verdicts.keys()
    if missing:
        log.warning(
            {
                "event": "transcript_topic_triage_missing_verdicts",
                "ticker": ticker,
                "missing": sorted(missing),
            }
        )
    return TopicTriageOutcome(ticker=ticker, verdicts=verdicts)
