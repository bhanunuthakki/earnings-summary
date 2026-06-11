"""Cost-ranked model ladder — the definition of "cheaper" for the downgrade loop.

The model-eval loop tests, per purpose, whether a CHEAPER model reaches parity
with the incumbent pinned model (``LLM_MODELS[purpose]``); when one does, the
purpose can switch down to save cost. "Cheaper" needs a total order over models,
which is what this module provides.

Cost basis is **marginal $/MTok**, output-weighted to reflect these prompts'
roughly 6:1 input:output ratio. Consumer-subscription Gemini is marginal-$0 (a
flat subscription) so it ranks cheapest — but it is rate-limited, which the
recommendation surfaces rather than the ladder hiding. The eval loop is what
decides whether the cheaper model's *quality* holds; this module only orders
them by price.

Prices (per MTok in/out, 2026-06): Haiku 4.5 $1/$5 · Sonnet 4.6 $3/$15 ·
Opus 4.7/4.8 $5/$25. Gemini consumer tiers: marginal $0 (list prices recorded
for reference only).
"""

from __future__ import annotations

from dataclasses import dataclass

CLAUDE = "claude"
GEMINI = "gemini"


@dataclass(frozen=True, slots=True)
class ModelCost:
    """Cost descriptor for one model. ``blended_usd_per_mtok`` is the rank key
    (lower = cheaper); subscription models are ~free at the margin."""

    model_id: str
    family: str
    input_usd_per_mtok: float
    output_usd_per_mtok: float
    subscription: bool = False  # flat-fee tier — marginal cost ~$0 (Gemini consumer)
    rate_limited: bool = False  # subscription tiers carry request/day caps

    @property
    def blended_usd_per_mtok(self) -> float:
        if self.subscription:
            return 0.0
        # Output-weighted to ~6:1 input:output (these analytical prompts are
        # large-prompt / modest-output). Only the ORDERING matters downstream.
        return (6.0 * self.input_usd_per_mtok + self.output_usd_per_mtok) / 7.0


# Registry. Keep in sync with the model ids used in src/llm/cli.py LLM_MODELS and
# src/llm/gemini_backend.py. Unknown models (not here) are treated as unrankable
# by the helpers below — the loop skips a purpose whose incumbent isn't ranked.
MODEL_LADDER: dict[str, ModelCost] = {
    # Claude tiers (metered $/MTok).
    "claude-haiku-4-5-20251001": ModelCost("claude-haiku-4-5-20251001", CLAUDE, 1.0, 5.0),
    "claude-sonnet-4-6": ModelCost("claude-sonnet-4-6", CLAUDE, 3.0, 15.0),
    "claude-opus-4-7": ModelCost("claude-opus-4-7", CLAUDE, 5.0, 25.0),
    "claude-opus-4-8": ModelCost("claude-opus-4-8", CLAUDE, 5.0, 25.0),
    # Gemini consumer-subscription tiers (marginal $0; list price = reference).
    # Current GA tiers: 3.5-flash (I/O 2026) + 3.1-pro-preview (Pro baseline;
    # 3-pro-preview was discontinued, no 3.5 Pro exists). Both CLI-verified.
    "gemini-3.5-flash": ModelCost(
        "gemini-3.5-flash", GEMINI, 1.5, 9.0, subscription=True, rate_limited=True
    ),
    "gemini-3.1-pro-preview": ModelCost(
        "gemini-3.1-pro-preview", GEMINI, 2.0, 12.0, subscription=True, rate_limited=True
    ),
}


def model_cost(model_id: str) -> ModelCost | None:
    return MODEL_LADDER.get(model_id)


def family_of(model_id: str) -> str | None:
    cost = MODEL_LADDER.get(model_id)
    return cost.family if cost is not None else None


def model_rank(model_id: str) -> float | None:
    """Blended marginal $/MTok (lower = cheaper). None if unranked."""
    cost = MODEL_LADDER.get(model_id)
    return cost.blended_usd_per_mtok if cost is not None else None


def is_cheaper(candidate: str, incumbent: str) -> bool:
    """True iff ``candidate`` is strictly cheaper than ``incumbent`` (both must
    be ranked). A same-cost candidate is NOT cheaper — no point switching."""
    c = model_rank(candidate)
    i = model_rank(incumbent)
    if c is None or i is None:
        return False
    return c < i


def _secondary_key(model_id: str) -> float:
    """Tie-breaker among same-blended models (e.g. two subscription Gemini tiers):
    order by list input price so flash precedes pro. Stable + deterministic."""
    cost = MODEL_LADDER.get(model_id)
    return cost.input_usd_per_mtok if cost is not None else 0.0


def cheaper_candidates(incumbent: str, *, include_gemini: bool = True) -> list[str]:
    """All registered models strictly cheaper than ``incumbent``, cheapest first.

    ``include_gemini=False`` restricts to same-family (Claude-tier) downgrades —
    useful when Gemini is excluded from a purpose (e.g. rate-limit sensitivity or
    a not-yet-eval-passed backend) but a cheaper Claude tier is still fair game.
    """
    inc_rank = model_rank(incumbent)
    if inc_rank is None:
        return []
    out = [
        mid
        for mid, cost in MODEL_LADDER.items()
        if mid != incumbent
        and cost.blended_usd_per_mtok < inc_rank
        and (include_gemini or cost.family != GEMINI)
    ]
    out.sort(key=lambda m: (MODEL_LADDER[m].blended_usd_per_mtok, _secondary_key(m)))
    return out


def estimated_call_usd(model_id: str, input_tokens: int, output_tokens: int) -> float:
    """Marginal $ for one call at the given token counts. Subscription tiers
    return 0.0 (measured-free, matching the ledger's cost_estimate_usd=0)."""
    cost = MODEL_LADDER.get(model_id)
    if cost is None or cost.subscription:
        return 0.0
    return (
        input_tokens / 1_000_000.0 * cost.input_usd_per_mtok
        + output_tokens / 1_000_000.0 * cost.output_usd_per_mtok
    )
