"""Model-downgrade evaluation engine — can a cheaper model do this purpose?

For one purpose, this runs the incumbent pinned model and a cheaper candidate on
the SAME real prompts and judges them head-to-head; if the candidate reaches
parity (wins-or-ties enough, both judges agreeing), the purpose can switch down.

It REUSES the brand-blind pairwise judge from ``backend_judge`` unchanged: the
incumbent response goes in the judge's slot-A, the candidate in slot-B. The judge
only ever sees "Response A" / "Response B" — it never learns which model wrote
which (and the same position-swap + dual-judge bias controls apply). So
``JudgedPair.winner == CLAUDE`` means **incumbent won**, ``== GEMINI`` means
**candidate won**; ``aggregate_by_purpose`` then reads gemini-wins as
candidate-wins and PROMOTE_CANDIDATE as "switch down to the candidate".

This is the generalization of the Claude-vs-Gemini comparison: Gemini is just one
kind of cheaper candidate, alongside cheaper Claude tiers. "Cheaper" is defined by
``model_ladder``.

Cost note: eval runs go through ``call_llm`` with ``force_budget_bypass=True`` and
``scope="model_eval"`` so the measurement is never throttled by a purpose's
production budget cap and is attributable separately in the ledger. Run the eval
with capture OFF (``LLM_CAPTURE_DIR`` unset) so eval traffic never lands in a
harvest corpus.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from llm.backend_judge import CLAUDE, GEMINI, JudgedPair, judge_pair
from llm.cli import call_llm
from llm.model_ladder import backend_for

log = logging.getLogger(__name__)

# Judge-slot mapping (documented once, used everywhere): the incumbent sits in
# the judge's CLAUDE slot, the candidate in the GEMINI slot.
INCUMBENT_SIDE = CLAUDE
CANDIDATE_SIDE = GEMINI


@dataclass(frozen=True, slots=True)
class ModelRunResult:
    model_id: str
    ok: bool
    response: str
    error: str | None
    elapsed_ms: int
    output_chars: int = 0  # len(response) — proxy for output token cost (~4 chars/token)


def run_model(
    prompt: str,
    *,
    model_id: str,
    purpose: str | None,
    ticker: str | None = None,
    run_id: str | None = None,
    timeout_seconds: int | None = None,
) -> ModelRunResult:
    """Run one model on a prompt via the canonical client. Never raises — a
    failed run is recorded as data (the eval records how each model behaved).
    Budget is bypassed (measurement, not production) and scope tags it.

    The backend is passed EXPLICITLY (``backend_for`` — family, else the
    slash-slug OpenRouter convention for frontier-discovered models): an
    explicit backend makes ``call_llm`` raise on that backend's failure instead
    of silently falling back to Claude — an eval must record the CANDIDATE's
    failure, never grade a stealth Claude answer as the candidate's."""
    backend = backend_for(model_id)
    t0 = time.monotonic()
    try:
        response = call_llm(
            prompt,
            purpose=purpose,
            model=model_id,
            backend=backend,
            ticker=ticker,
            scope="model_eval",
            run_id=run_id,
            timeout_seconds=timeout_seconds,
            force_budget_bypass=True,
        )
        return ModelRunResult(
            model_id, True, response, None, int((time.monotonic() - t0) * 1000), len(response)
        )
    except Exception as exc:  # every failure mode is a recordable outcome
        return ModelRunResult(
            model_id,
            False,
            "",
            f"{type(exc).__name__}: {str(exc)[:500]}",
            int((time.monotonic() - t0) * 1000),
        )


@dataclass(frozen=True, slots=True)
class PromptCase:
    """One real prompt to evaluate a candidate against, with the incumbent's
    already-known response (reused from a capture/harvest — no re-run needed).

    ``prompt_sha256`` (optional, additive) ties the case back to its
    ``llm_calls`` census row: the stratified sampler records it in the
    ``sample_manifest`` (dedup across sweeps) and the per-query criteria layer
    keys its checklist cache on it (meta_eval_governance.md §2-§3)."""

    label: str
    prompt: str
    ticker: str | None
    incumbent_response: str
    prompt_sha256: str | None = None


def judge_case(
    case: PromptCase,
    candidate_response: str,
    *,
    purpose: str,
    judge_backend: str,
    judge_model: str | None = None,
    run_id: str | None = None,
    max_prompt_chars: int = 8000,
    criteria_block: str | None = None,
) -> JudgedPair:
    """Judge incumbent vs candidate on one case. Thin wrapper over judge_pair with
    the slot mapping fixed: incumbent -> CLAUDE slot, candidate -> GEMINI slot.
    ``criteria_block`` is the optional per-case checklist (§3) — judge-side only;
    the generation replay never sees it (isolation invariant I1)."""
    return judge_pair(
        purpose=purpose,
        label=case.label,
        ticker=case.ticker,
        claude_response=case.incumbent_response,  # slot A == incumbent
        gemini_response=candidate_response,  # slot B == candidate
        task_prompt=case.prompt,
        judge_backend=judge_backend,
        judge_model=judge_model,
        run_id=run_id,
        max_prompt_chars=max_prompt_chars,
        criteria_block=criteria_block,
    )


# Switch-decision labels (advisory). Mirror backend_judge's recommendation
# vocabulary but in downgrade terms.
SWITCH_DOWN = "SWITCH_DOWN"  # candidate at parity-or-better -> cheaper model wins
KEEP_INCUMBENT = "KEEP_INCUMBENT"  # incumbent clearly better -> do not switch
HOLD = "HOLD"  # mixed / not robust
INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
# The candidate failed OPERATIONALLY (timeout / CLI error / empty output) on
# most cases — an infrastructure verdict, not a quality one. Without this
# label, a broken backend scored parity=0.0 and was recorded KEEP_INCUMBENT:
# the 2026-06-28 sweep recorded every Gemini candidate at 0.0 parity across
# every purpose while the Gemini CLI was erroring 60-100% of its runs, so the
# cost optimizer looked like it had measured a quality loss it never measured.
CANDIDATE_ERRORED = "CANDIDATE_ERRORED"

# Above this fraction of operationally-failed cases the quality tallies are
# noise: the surviving n is too small and error-as-incumbent-win dominates.
CANDIDATE_ERROR_RATE_THRESHOLD = 0.5

# The sample was not representative enough to grade (meta_eval_governance.md
# §2.1): the replayable capture frame covers too little of the purpose's ledger
# census (frame_share < MIN_FRAME_SHARE) or the eligible pool is below min_n.
# An ADVISORY honesty label — like INSUFFICIENT_DATA it is streak-neutral in
# apply_model_switches (never switch/keep evidence); the fix is more harvest,
# not a different candidate.
INSUFFICIENT_FRAME = "INSUFFICIENT_FRAME"


@dataclass(frozen=True, slots=True)
class CandidateVerdict:
    """The accumulated verdict for one (purpose, candidate) across cases + judges."""

    purpose: str
    incumbent: str
    candidate: str
    n: int
    candidate_wins: int  # candidate strictly beat incumbent
    incumbent_wins: int
    ties: int
    parity_rate: float  # (candidate_wins + ties) / n  — the switch-down signal
    judge_agreement: float  # fraction of cases both judges agreed on the winner
    recommendation: str
    reason: str
    # Token-efficiency signals (output chars as ~4-chars/token proxy). The ratio
    # is candidate / incumbent: <1 is more concise, >1 is more verbose. A cheaper
    # model that uses far more tokens may cost more than it saves, so the ratio is
    # a secondary signal even when parity is met. 0.0 when no successful runs.
    candidate_output_chars_mean: float = 0.0
    incumbent_output_chars_mean: float = 0.0
    token_efficiency_ratio: float = 0.0  # candidate_chars / incumbent_chars
    # Operational-failure accounting: how many cases the candidate errored on
    # (timeout / CLI failure / empty output) out of the cases attempted. When
    # the rate crosses CANDIDATE_ERROR_RATE_THRESHOLD the recommendation is
    # CANDIDATE_ERRORED — infra breakage, excluded from switch/keep streaks.
    n_candidate_errors: int = 0
    candidate_error_rate: float = 0.0


def decide_switch(
    *,
    purpose: str,
    incumbent: str,
    candidate: str,
    per_judge: dict[str, tuple[int, int, int]],  # judge -> (cand_wins, inc_wins, ties)
    judge_agreement: float,
    min_n: int,
    parity_threshold: float,
    candidate_output_chars_mean: float = 0.0,
    incumbent_output_chars_mean: float = 0.0,
    n_cases_attempted: int = 0,
    n_candidate_errors: int = 0,
) -> CandidateVerdict:
    """Turn the per-judge tallies into a switch recommendation. Conservative:
    a downgrade ships only when EVERY judge says the cheaper model holds parity
    on a strong majority AND the judges agree — the cost of a bad downgrade
    (worse production output) outweighs the saving.

    ``candidate_output_chars_mean`` / ``incumbent_output_chars_mean`` are token-
    efficiency signals (output character count as a ~4-chars/token proxy). They
    are advisory — the switch gate is quality-only — but are recorded in the
    verdict so a verbose candidate that costs more than it saves can be spotted.

    ``n_cases_attempted`` / ``n_candidate_errors`` separate operational failure
    from quality: when the candidate errored on >= CANDIDATE_ERROR_RATE_THRESHOLD
    of attempted cases, the verdict is CANDIDATE_ERRORED — the quality tallies
    (where each errored case was booked as an incumbent win) are noise, and
    recording KEEP_INCUMBENT would let a broken backend masquerade as a
    measured quality loss. Callers that don't track errors (older paths) pass
    the defaults and get the pre-existing behavior."""
    # n is the case count (same across judges); take the max judge's total.
    totals = [cw + iw + ti for (cw, iw, ti) in per_judge.values()]
    n = max(totals) if totals else 0
    cand_wins = sum(cw for (cw, _iw, _ti) in per_judge.values())
    inc_wins = sum(iw for (_cw, iw, _ti) in per_judge.values())
    ties = sum(ti for (_cw, _iw, ti) in per_judge.values())

    error_rate = n_candidate_errors / n_cases_attempted if n_cases_attempted > 0 else 0.0

    if n_cases_attempted > 0 and error_rate >= CANDIDATE_ERROR_RATE_THRESHOLD:
        rec, reason = (
            CANDIDATE_ERRORED,
            f"{candidate} failed operationally on {n_candidate_errors}/{n_cases_attempted} "
            f"case(s) ({error_rate:.0%}) — infrastructure failure, not a quality verdict; "
            "fix the backend before trusting any tally",
        )
    elif n < min_n:
        rec, reason = INSUFFICIENT_DATA, f"only {n} case(s); need >={min_n}"
    else:
        # Per-judge parity: candidate wins-or-ties / that judge's total.
        parity_by_judge = [
            (cw + ti) / (cw + iw + ti) if (cw + iw + ti) else 0.0
            for (cw, iw, ti) in per_judge.values()
        ]
        incumbent_majority = any(iw > cw for (cw, iw, _ti) in per_judge.values())
        all_parity = parity_by_judge and all(p >= parity_threshold for p in parity_by_judge)
        if incumbent_majority:
            rec, reason = (
                KEEP_INCUMBENT,
                f"a judge has the incumbent winning a majority ({incumbent}); do not downgrade",
            )
        elif all_parity and judge_agreement >= 0.6:
            rec, reason = (
                SWITCH_DOWN,
                f"{candidate} holds parity for every judge (>={parity_threshold:.0%}); "
                f"agreement {judge_agreement:.0%}",
            )
        elif judge_agreement < 0.6:
            rec, reason = HOLD, f"judges disagree (agreement {judge_agreement:.0%})"
        else:
            rec, reason = HOLD, "mixed: parity below the switch bar for some judge"

    total = cand_wins + inc_wins + ties
    parity_rate = (cand_wins + ties) / total if total else 0.0
    token_efficiency_ratio = (
        candidate_output_chars_mean / incumbent_output_chars_mean
        if incumbent_output_chars_mean > 0
        else 0.0
    )
    return CandidateVerdict(
        purpose=purpose,
        incumbent=incumbent,
        candidate=candidate,
        n=n,
        candidate_wins=cand_wins,
        incumbent_wins=inc_wins,
        ties=ties,
        parity_rate=parity_rate,
        judge_agreement=judge_agreement,
        recommendation=rec,
        reason=reason,
        candidate_output_chars_mean=candidate_output_chars_mean,
        incumbent_output_chars_mean=incumbent_output_chars_mean,
        token_efficiency_ratio=token_efficiency_ratio,
        n_candidate_errors=n_candidate_errors,
        candidate_error_rate=error_rate,
    )
