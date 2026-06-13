"""Cost-ranked model ladder — the definition of "cheaper" for the downgrade loop.

The model-eval loop tests, per purpose, whether a CHEAPER model reaches parity
with the incumbent pinned model (``LLM_MODELS[purpose]``); when one does, the
purpose can switch down to save cost. "Cheaper" needs a total order over models,
which is what this module provides.

Cost basis is **public API marginal $/MTok**, output-weighted to reflect these
prompts' roughly 6:1 input:output ratio. The eval loop is what decides whether
the cheaper model's *quality* holds; this module only orders them by price.

Blended ladder order (cheapest → most expensive, 2026-06 API list prices):
  Gemini 3.5 Flash ($0.30/$2.50) → Haiku 4.5 ($0.80/$4.00)
  → Gemini 3.1 Pro ($1.25/$10.00) → Sonnet 4.6 ($3.00/$15.00)
  → Opus 4.7/4.8 ($15.00/$75.00)
"""

from __future__ import annotations

from dataclasses import dataclass

CLAUDE = "claude"
GEMINI = "gemini"


@dataclass(frozen=True, slots=True)
class ModelCost:
    """Cost descriptor for one model. ``blended_usd_per_mtok`` is the rank key
    (lower = cheaper). All prices are public API list prices ($/MTok)."""

    model_id: str
    family: str
    input_usd_per_mtok: float
    output_usd_per_mtok: float

    @property
    def blended_usd_per_mtok(self) -> float:
        # Output-weighted to ~6:1 input:output (these analytical prompts are
        # large-prompt / modest-output). Only the ORDERING matters downstream.
        return (6.0 * self.input_usd_per_mtok + self.output_usd_per_mtok) / 7.0


# Registry. Keep in sync with the model ids used in src/llm/cli.py LLM_MODELS and
# src/llm/gemini_backend.py. Unknown models (not here) are treated as unrankable
# by the helpers below — the loop skips a purpose whose incumbent isn't ranked.
MODEL_LADDER: dict[str, ModelCost] = {
    # Claude tiers (public API prices, $/MTok in/out, 2026-06).
    "claude-haiku-4-5-20251001": ModelCost("claude-haiku-4-5-20251001", CLAUDE, 0.80, 4.00),
    "claude-sonnet-4-6": ModelCost("claude-sonnet-4-6", CLAUDE, 3.00, 15.00),
    "claude-opus-4-7": ModelCost("claude-opus-4-7", CLAUDE, 15.00, 75.00),
    "claude-opus-4-8": ModelCost("claude-opus-4-8", CLAUDE, 15.00, 75.00),
    # Gemini API tiers (public API prices, $/MTok in/out, 2026-06).
    # CLI-verified model ids: gemini-2.5-flash (gemini-3.5-flash is invalid, returns
    # ModelNotFoundError) + gemini-3.1-pro-preview (3-pro-preview discontinued 2026-03,
    # no 3.5 Pro exists). Matches GEMINI_BACKEND_FAST_MODEL / _DEFAULT_MODEL in
    # src/llm/gemini_backend.py.
    "gemini-2.5-flash": ModelCost("gemini-2.5-flash", GEMINI, 0.30, 2.50),
    "gemini-3.1-pro-preview": ModelCost("gemini-3.1-pro-preview", GEMINI, 1.25, 10.00),
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
    useful when a cheaper Claude tier is desired without crossing to the Gemini backend.
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


def estimated_call_usd(model_id: str | None, input_tokens: int, output_tokens: int) -> float:
    """Marginal $ for one call at the given token counts. Returns 0.0 for
    unknown models (not in the ladder) — cost is unknown, not free."""
    if model_id is None:
        return 0.0
    cost = MODEL_LADDER.get(model_id)
    if cost is None:
        return 0.0
    return (
        input_tokens / 1_000_000.0 * cost.input_usd_per_mtok
        + output_tokens / 1_000_000.0 * cost.output_usd_per_mtok
    )
