"""Pairwise Claude-vs-Gemini judge over the backend-compare corpus.

The eval-gated Gemini backend (``src/llm/gemini_backend.py``) ships with an
EMPTY routing allowlist: no purpose reaches Gemini in production until its
output quality has been graded against Claude's. ``execution/compare_backends.py``
produces the paired-output corpus; this module is the judge that grades it and
turns it into a per-purpose promotion recommendation.

Why *pairwise* (A vs B), not the absolute golden-set/rubric scoring the general
eval harness does: the decision here is specifically "is Gemini's answer as good
as Claude's *for this purpose*?". Head-to-head preference judging is the
lower-variance, better-calibrated way to answer a relative question than scoring
each side absolutely and subtracting. Two bias controls make the verdict robust:

  * **Brand-blind.** The judge sees "Response A" / "Response B" and is NEVER told
    which model produced which — judges carry priors about model brands.
  * **Position-swap.** Every pair is judged twice with the responses swapped; a
    winner counts only when both passes agree once mapped back to backend space.
    A flip means the judge is following position, not quality, and the pair is
    recorded as a (non-robust) tie with ``position_consistent=False``.

Optionally a **dual judge** (Claude Opus + Gemini Pro, ``--judges claude,gemini``)
grades the same corpus; cross-judge agreement cancels same-family favouritism and
is the headline signal. Gemini-as-judge uses an explicit ``backend="gemini"``
force, which legitimately bypasses the production allowlist — a judge call is not
production routing.

Contract: **fail closed** (mirrors ``src/evals/judge.py`` on the other track).
An unparseable or failed judge verdict never silently passes a side; the pair
resolves to a tie with the raw text and error preserved for audit. Judge calls go
through the canonical ``call_llm`` under ``purpose="backend_compare_judge"``
(Opus pin in ``LLM_MODELS``; Gemini side resolves to Pro) and carry the run's
``run_id`` so their cost joins back from ``llm_calls``.

The recommendation this module emits is ADVISORY input to the human decision that
edits ``GEMINI_BACKEND_ALLOWED_PURPOSES`` — never an automatic gate.
"""

from __future__ import annotations

import json
import logging
import re
from collections import defaultdict
from dataclasses import dataclass
from typing import cast

from llm.cli import call_llm

log = logging.getLogger(__name__)

JUDGE_PURPOSE = "backend_compare_judge"

# The two real backends compared. "tie" is the third verdict bucket.
CLAUDE = "claude"
GEMINI = "gemini"
# P4 cross-family judges (llm_quality_program_2026_07.md). Same-family
# judging (Claude grading Claude) is the weakest evidence in the loop, and
# with the Gemini key invalid it was ALL the loop had. DEEPSEEK routes
# through OpenRouter; CODEX through the ChatGPT-membership CLI wrapper.
DEEPSEEK = "deepseek"
CODEX = "codex"

# Per-facet preference is reported alongside the overall winner so a promotion
# call can see WHERE a backend wins (e.g. "ties on substance, loses on format").
FACETS = ("faithfulness", "accuracy", "format", "conciseness")

_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$")
_VALID_SIDES = frozenset({"A", "B", "tie"})

# Keep the judge prompt bounded: real production prompts (e.g. bear_case) run to
# thousands of chars. The corpus is small so cost is cents either way, but a
# runaway prompt shouldn't dominate the judge's context window.
DEFAULT_MAX_PROMPT_CHARS = 8000


@dataclass(frozen=True, slots=True)
class PairVerdict:
    """One judge pass, in POSITION space ("A"/"B"/"tie") — not yet mapped to a
    backend. ``facets`` maps each facet name to "A"/"B"/"tie". ``checklist``
    maps per-case criteria ids the same way (meta_eval_governance.md §3) —
    ``None`` when the judge prompt carried no checklist (legacy passes)."""

    winner: str  # "A" | "B" | "tie"
    margin: float  # 0.0..1.0 (clamped); how decisive the call is
    facets: dict[str, str]
    rationale: str
    checklist: dict[str, str] | None = None


@dataclass(frozen=True, slots=True)
class JudgeOnce:
    """What one judge pass produced. ``verdict is None`` ⇒ the call failed or the
    output didn't validate; the caller must fail that pass closed."""

    verdict: PairVerdict | None
    raw: str
    error: str | None = None


@dataclass(frozen=True, slots=True)
class JudgedPair:
    """The consolidated verdict for one corpus record under one judge, in BACKEND
    space. ``winner`` ∈ {"claude", "gemini", "tie"}; a real backend wins only
    when both position-swapped passes agree on it."""

    purpose: str | None
    label: str
    ticker: str | None
    judge_backend: str
    judge_model: str | None
    winner: str  # CLAUDE | GEMINI | "tie"
    margin: float  # mean of the two passes' margins when they agree, else 0.0
    facet_winners: dict[str, str]  # facet -> CLAUDE | GEMINI | "tie"
    position_consistent: bool
    rationales: list[str]
    error: str | None = None
    # Per-case checklist items (§3), consolidated across the position-swapped
    # passes with the same agree-or-tie rule as facets. None when the pair was
    # judged without a checklist (legacy / deriver-missing cases).
    checklist_winners: dict[str, str] | None = None


@dataclass(frozen=True, slots=True)
class PurposeRollup:
    """Per-(purpose, judge) tally + the advisory recommendation."""

    purpose: str | None
    judge_backend: str
    n: int
    gemini_wins: int
    claude_wins: int
    ties: int
    # Signed mean margin in gemini-minus-claude space: +ve favours Gemini.
    signed_margin: float
    position_consistent_rate: float
    facet_gemini_loss: dict[str, int]  # facet -> # records where Claude won it
    recommendation: str
    reason: str


# Recommendation labels (advisory).
PROMOTE_CANDIDATE = "PROMOTE_CANDIDATE"
HOLD = "HOLD"
REJECT = "REJECT"
INSUFFICIENT_DATA = "INSUFFICIENT_DATA"


_BRAND_SELF_ID_RX = re.compile(
    r"(?i)\b(as an? (ai|language model|assistant) (trained|created|developed)? (by|from|at)? (anthropic|google|openai|meta|mistral|deepseek|qwen)?|"
    r"i am (claude|gemini|gpt-4|gpt-5|chatgpt|deepseek))\b"
)


def _scrub_brand_self_id(text: str) -> str:
    """Scrub provider self-identification phrases from model responses before judging."""
    return _BRAND_SELF_ID_RX.sub("[AI assistant]", text)


_PROMPT_TEMPLATE = """\
You are grading two AI assistants' answers to the SAME task, head to head, for a
financial-analysis pipeline. The task was issued for the purpose "{purpose}".

You are NOT told which model wrote which answer. Judge only the text in front of
you. Do NOT reward wordiness or length. A concise, dense response that answers the prompt
with zero padding is STRICTLY PREFERRED over a verbose or repetitive response.

=== TASK GIVEN TO BOTH ===
{prompt}

=== RESPONSE A ===
{response_a}

=== RESPONSE B ===
{response_b}
{criteria_block}
Judge on four facets, each independently — pick "A", "B", or "tie":
- faithfulness: which answer better does what the task actually asked?
- accuracy: which answer is more factually correct / better grounded, with fewer
  invented or wrong specifics?
- format: which answer better obeys the required output shape (JSON validity,
  required fields, the exact string format, no stray prose/markdown when forbidden)?
- conciseness: which answer is appropriately tight — no padding, no omission?

Then an OVERALL winner and a margin: 1.0 = decisively better, 0.0 = a dead heat.

Output ONLY this JSON object — no prose, no markdown fences:
{{"winner": "A"|"B"|"tie", "margin": <0.0-1.0>, "faithfulness": "A"|"B"|"tie", \
"accuracy": "A"|"B"|"tie", "format": "A"|"B"|"tie", "conciseness": "A"|"B"|"tie", \
"rationale": "<one sentence naming the deciding difference>"}}
"""


def _truncate(text: str, limit: int) -> str:
    if limit <= 0 or len(text) <= limit:
        return text
    return text[:limit] + f"\n…[truncated {len(text) - limit} chars]"


def build_judge_prompt(
    purpose: str | None,
    task_prompt: str,
    response_a: str,
    response_b: str,
    *,
    max_prompt_chars: int = DEFAULT_MAX_PROMPT_CHARS,
    criteria_block: str | None = None,
) -> str:
    """Assemble the brand-blind A/B judge prompt for one pass with brand scrubbing."""
    block = f"\n{criteria_block}\n" if criteria_block else ""
    return _PROMPT_TEMPLATE.format(
        purpose=purpose or "(unspecified)",
        prompt=_truncate(task_prompt, max_prompt_chars),
        response_a=_scrub_brand_self_id(response_a),
        response_b=_scrub_brand_self_id(response_b),
        criteria_block=block,
    )


def parse_pair_verdict(raw: str) -> PairVerdict | None:
    """Strict, fail-closed verdict parsing: fence-strip, one JSON object, every
    required key present with the right type and an allowed value. ``None`` on any
    deviation — the caller fails the pass closed and preserves ``raw``."""
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

    winner = obj.get("winner")
    if winner not in _VALID_SIDES:
        return None

    margin = obj.get("margin")
    if isinstance(margin, bool) or not isinstance(margin, (int, float)):
        return None

    facets: dict[str, str] = {}
    for facet in FACETS:
        side = obj.get(facet)
        if side not in _VALID_SIDES:
            return None
        facets[facet] = cast("str", side)

    rationale = obj.get("rationale")
    if not isinstance(rationale, str) or not rationale.strip():
        return None

    # Per-case checklist (§3): TOLERANT on absence (legacy verdicts), fail-closed
    # on malformation — a present-but-broken checklist fails the pass exactly
    # like a bad facet would.
    checklist: dict[str, str] | None = None
    if "checklist" in obj:
        raw_checklist = obj.get("checklist")
        if not isinstance(raw_checklist, dict):
            return None
        checklist = {}
        for cid, side in cast("dict[str, object]", raw_checklist).items():
            if side not in _VALID_SIDES:
                return None
            checklist[str(cid)] = cast("str", side)

    return PairVerdict(
        winner=cast("str", winner),
        margin=max(0.0, min(1.0, float(margin))),
        facets=facets,
        rationale=rationale.strip(),
        checklist=checklist,
    )


def _judge_once(
    purpose: str | None,
    task_prompt: str,
    response_a: str,
    response_b: str,
    *,
    judge_backend: str,
    run_id: str | None,
    max_prompt_chars: int,
    criteria_block: str | None = None,
) -> JudgeOnce:
    """One judge pass over a fixed A/B assignment. Never raises — every failure
    mode returns a fail-closed JudgeOnce with the error recorded."""
    prompt = build_judge_prompt(
        purpose,
        task_prompt,
        response_a,
        response_b,
        max_prompt_chars=max_prompt_chars,
        criteria_block=criteria_block,
    )
    try:
        if judge_backend == CODEX:
            raw = call_llm(
                prompt,
                purpose=JUDGE_PURPOSE,
                scope="backend_judge",
                run_id=run_id,
                backend=CODEX,
            )
        elif judge_backend == DEEPSEEK:
            from llm.model_ladder import DEEPSEEK_JUDGE_MODEL

            raw = call_llm(
                prompt,
                purpose=JUDGE_PURPOSE,
                scope="backend_judge",
                run_id=run_id,
                model=DEEPSEEK_JUDGE_MODEL,
                backend="openrouter",
            )
        else:
            raw = call_llm(
                prompt,
                purpose=JUDGE_PURPOSE,
                scope="backend_judge",
                run_id=run_id,
                backend=judge_backend,
            )
    except Exception as exc:  # a judge that won't run is a recordable outcome
        log.warning(
            {
                "event": "backend_judge_call_failed",
                "judge_backend": judge_backend,
                "purpose": purpose,
                "error": f"{type(exc).__name__}: {exc}",
            }
        )
        return JudgeOnce(verdict=None, raw="", error=f"{type(exc).__name__}: {exc}")
    verdict = parse_pair_verdict(raw)
    if verdict is None:
        log.warning(
            {
                "event": "backend_judge_unparseable",
                "judge_backend": judge_backend,
                "raw_head": raw[:200],
            }
        )
        return JudgeOnce(verdict=None, raw=raw, error="unparseable verdict")
    return JudgeOnce(verdict=verdict, raw=raw)


def _side_to_backend(side: str, *, a_is: str, b_is: str) -> str:
    """Map a position-space side ("A"/"B"/"tie") to a backend given which backend
    sat in each position for this pass."""
    if side == "A":
        return a_is
    if side == "B":
        return b_is
    return "tie"


def judge_pair(
    *,
    purpose: str | None,
    label: str,
    ticker: str | None,
    claude_response: str,
    gemini_response: str,
    task_prompt: str,
    judge_backend: str,
    judge_model: str | None = None,
    run_id: str | None = None,
    max_prompt_chars: int = DEFAULT_MAX_PROMPT_CHARS,
    criteria_block: str | None = None,
) -> JudgedPair:
    """Judge one Claude-vs-Gemini pair under one judge, with position-swap.

    Pass 1 puts Claude in A, Gemini in B; pass 2 swaps. Each pass's winner is
    mapped to backend space; a real backend wins ONLY if both passes agree on it
    (consistency filter against position bias). Facets — and the optional
    per-case checklist items (§3, ``criteria_block``) — are consolidated the
    same way. If either pass fails to produce a verdict, the pair fails closed
    to a tie with the error recorded.
    """
    pass1 = _judge_once(
        purpose,
        task_prompt,
        claude_response,
        gemini_response,
        judge_backend=judge_backend,
        run_id=run_id,
        max_prompt_chars=max_prompt_chars,
        criteria_block=criteria_block,
    )
    pass2 = _judge_once(
        purpose,
        task_prompt,
        gemini_response,
        claude_response,
        judge_backend=judge_backend,
        run_id=run_id,
        max_prompt_chars=max_prompt_chars,
        criteria_block=criteria_block,
    )

    if pass1.verdict is None or pass2.verdict is None:
        err = pass1.error or pass2.error or "judge failed"
        return JudgedPair(
            purpose=purpose,
            label=label,
            ticker=ticker,
            judge_backend=judge_backend,
            judge_model=judge_model,
            winner="tie",
            margin=0.0,
            facet_winners={facet: "tie" for facet in FACETS},
            position_consistent=False,
            rationales=[v.rationale for v in (pass1.verdict, pass2.verdict) if v is not None],
            error=err,
        )

    # Map both passes to backend space.
    w1 = _side_to_backend(pass1.verdict.winner, a_is=CLAUDE, b_is=GEMINI)
    w2 = _side_to_backend(pass2.verdict.winner, a_is=GEMINI, b_is=CLAUDE)

    if w1 == w2 and w1 != "tie":
        winner = w1
        margin = (pass1.verdict.margin + pass2.verdict.margin) / 2.0
        consistent = True
    elif w1 == "tie" and w2 == "tie":
        winner, margin, consistent = "tie", 0.0, True
    else:
        # The two passes disagree (a flip, or one-sided tie) — not robust.
        winner, margin, consistent = "tie", 0.0, False

    facet_winners: dict[str, str] = {}
    for facet in FACETS:
        f1 = _side_to_backend(pass1.verdict.facets[facet], a_is=CLAUDE, b_is=GEMINI)
        f2 = _side_to_backend(pass2.verdict.facets[facet], a_is=GEMINI, b_is=CLAUDE)
        facet_winners[facet] = f1 if (f1 == f2 and f1 != "tie") else "tie"

    # Checklist items (§3): same agree-or-tie consolidation, over the ids BOTH
    # passes returned (an id one pass dropped can't be robustly scored).
    checklist_winners: dict[str, str] | None = None
    c1, c2 = pass1.verdict.checklist, pass2.verdict.checklist
    if c1 is not None and c2 is not None:
        checklist_winners = {}
        for cid in sorted(set(c1) & set(c2)):
            s1 = _side_to_backend(c1[cid], a_is=CLAUDE, b_is=GEMINI)
            s2 = _side_to_backend(c2[cid], a_is=GEMINI, b_is=CLAUDE)
            checklist_winners[cid] = s1 if (s1 == s2 and s1 != "tie") else "tie"

    return JudgedPair(
        purpose=purpose,
        label=label,
        ticker=ticker,
        judge_backend=judge_backend,
        judge_model=judge_model,
        winner=winner,
        margin=margin,
        facet_winners=facet_winners,
        position_consistent=consistent,
        rationales=[pass1.verdict.rationale, pass2.verdict.rationale],
        checklist_winners=checklist_winners,
    )


def _recommend(
    *,
    n: int,
    gemini_wins: int,
    claude_wins: int,
    consistent_rate: float,
    min_n: int,
    promote_win_or_tie_rate: float,
) -> tuple[str, str]:
    """Advisory recommendation for one (purpose, judge) tally. Conservative by
    design: the cost of wrongly promoting (worse production output) outweighs the
    cost of holding (keep judging)."""
    if n < min_n:
        return INSUFFICIENT_DATA, f"only {n} judged pair(s); need >={min_n} for a call"
    if claude_wins > gemini_wins:
        return REJECT, f"Claude wins {claude_wins}/{n}; Gemini not at parity"
    win_or_tie = (n - claude_wins) / n
    if win_or_tie >= promote_win_or_tie_rate and consistent_rate >= 0.6:
        return (
            PROMOTE_CANDIDATE,
            f"Gemini at parity-or-better on {win_or_tie:.0%} of pairs "
            f"(claude won {claude_wins}/{n}); position-consistent {consistent_rate:.0%}",
        )
    if consistent_rate < 0.6:
        return HOLD, f"verdicts not robust (position-consistent only {consistent_rate:.0%})"
    return HOLD, f"mixed: Gemini parity-or-better on {win_or_tie:.0%}, below promote bar"


def aggregate_by_purpose(
    judged: list[JudgedPair],
    *,
    min_n: int = 3,
    promote_win_or_tie_rate: float = 0.8,
) -> list[PurposeRollup]:
    """Tally judged pairs into per-(purpose, judge) rollups with a recommendation.

    Grouped by ``(purpose, judge_backend)`` so a dual-judge run yields one rollup
    per judge — the caller compares them for cross-judge agreement.
    """
    groups: dict[tuple[str | None, str], list[JudgedPair]] = defaultdict(list)
    for jp in judged:
        groups[(jp.purpose, jp.judge_backend)].append(jp)

    rollups: list[PurposeRollup] = []
    for (purpose, judge_backend), pairs in groups.items():
        n = len(pairs)
        gemini_wins = sum(1 for p in pairs if p.winner == GEMINI)
        claude_wins = sum(1 for p in pairs if p.winner == CLAUDE)
        ties = n - gemini_wins - claude_wins
        # Signed margin: +margin when Gemini won, -margin when Claude won, 0 tie.
        signed = sum(
            p.margin if p.winner == GEMINI else (-p.margin if p.winner == CLAUDE else 0.0)
            for p in pairs
        )
        signed_margin = signed / n if n else 0.0
        consistent_rate = (sum(1 for p in pairs if p.position_consistent) / n) if n else 0.0
        facet_gemini_loss = {
            facet: sum(1 for p in pairs if p.facet_winners.get(facet) == CLAUDE) for facet in FACETS
        }
        recommendation, reason = _recommend(
            n=n,
            gemini_wins=gemini_wins,
            claude_wins=claude_wins,
            consistent_rate=consistent_rate,
            min_n=min_n,
            promote_win_or_tie_rate=promote_win_or_tie_rate,
        )
        rollups.append(
            PurposeRollup(
                purpose=purpose,
                judge_backend=judge_backend,
                n=n,
                gemini_wins=gemini_wins,
                claude_wins=claude_wins,
                ties=ties,
                signed_margin=signed_margin,
                position_consistent_rate=consistent_rate,
                facet_gemini_loss=facet_gemini_loss,
                recommendation=recommendation,
                reason=reason,
            )
        )
    rollups.sort(key=lambda r: (str(r.purpose), r.judge_backend))
    return rollups


@dataclass(frozen=True, slots=True)
class CrossJudgeAgreement:
    """How often the judges agreed on the winner, for pairs graded by ≥2 judges."""

    purpose: str | None
    n_pairs: int  # pairs judged by >= 2 judges
    n_agree: int  # of those, judges unanimous on the winner
    agreement_rate: float


def cross_judge_agreement(judged: list[JudgedPair]) -> list[CrossJudgeAgreement]:
    """Per purpose, the unanimity rate across judges on the same pair (keyed by
    ``label`` within a purpose). Only pairs seen by ≥2 judges count."""
    by_purpose_label: dict[tuple[str | None, str], list[JudgedPair]] = defaultdict(list)
    for jp in judged:
        by_purpose_label[(jp.purpose, jp.label)].append(jp)

    agg: dict[str | None, list[bool]] = defaultdict(list)
    for (purpose, _label), pairs in by_purpose_label.items():
        if len({p.judge_backend for p in pairs}) < 2:
            continue
        winners = {p.winner for p in pairs}
        agg[purpose].append(len(winners) == 1)

    out: list[CrossJudgeAgreement] = []
    for purpose, flags in agg.items():
        n = len(flags)
        n_agree = sum(1 for f in flags if f)
        out.append(
            CrossJudgeAgreement(
                purpose=purpose,
                n_pairs=n,
                n_agree=n_agree,
                agreement_rate=(n_agree / n) if n else 0.0,
            )
        )
    out.sort(key=lambda c: str(c.purpose))
    return out


@dataclass(frozen=True, slots=True)
class GradableRecord:
    """A compare-corpus record reduced to what the judge needs. Built by the CLI
    from a JSONL line; ``skip_reason`` set ⇒ a backend failed and the record can't
    be judged head-to-head."""

    purpose: str | None
    label: str
    ticker: str | None
    task_prompt: str
    claude_response: str
    gemini_response: str
    skip_reason: str | None = None


def gradable_from_record(record: dict[str, object]) -> GradableRecord:
    """Reduce one compare_backends JSONL record to a GradableRecord, deciding
    whether it can be judged (both backends produced a response)."""
    purpose_raw = record.get("purpose")
    purpose = purpose_raw if isinstance(purpose_raw, str) else None
    label = str(record.get("label", "adhoc"))
    ticker_raw = record.get("ticker")
    ticker = ticker_raw if isinstance(ticker_raw, str) else None
    task_prompt = str(record.get("prompt", ""))

    claude = record.get("claude")
    gemini = record.get("gemini")
    claude_d = cast("dict[str, object]", claude) if isinstance(claude, dict) else {}
    gemini_d = cast("dict[str, object]", gemini) if isinstance(gemini, dict) else {}

    skip_reason: str | None = None
    if not claude_d or not bool(claude_d.get("ok")):
        skip_reason = "claude side did not succeed"
    elif not gemini_d or not bool(gemini_d.get("ok")):
        skip_reason = "gemini side did not succeed"

    return GradableRecord(
        purpose=purpose,
        label=label,
        ticker=ticker,
        task_prompt=task_prompt,
        claude_response=str(claude_d.get("response") or ""),
        gemini_response=str(gemini_d.get("response") or ""),
        skip_reason=skip_reason,
    )
