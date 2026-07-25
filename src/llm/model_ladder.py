"""Cost-ranked model ladder — the definition of "cheaper" for the downgrade loop.

The model-eval loop tests, per purpose, whether a CHEAPER model reaches parity
with the incumbent pinned model (``LLM_MODELS[purpose]``); when one does, the
purpose can switch down to save cost. "Cheaper" needs a total order over models,
which is what this module provides.

Cost basis is **public API marginal $/MTok**, output-weighted to reflect these
prompts' roughly 6:1 input:output ratio. The eval loop is what decides whether
the cheaper model's *quality* holds; this module only orders them by price.

Blended ladder order (cheapest → most expensive, 2026-07-21 API list prices —
see ``C:\\Users\\Bhanu\\.gemini\\procedures\\model-frontier.REFERENCE.md``, the
dated cross-provider authority; verify there before editing prices below):
  Gemini 2.5 Flash ($0.30/$2.50) → Haiku 4.5 ($1.00/$5.00)
  → Gemini 3.1 Pro ($1.25/$10.00) → Sonnet 5 ($2.00/$10.00, launch price
  through 2026-08-31, then $3.00/$15.00) → Opus 4.8 ($5.00/$25.00)
  → Fable 5 ($10.00/$50.00).

2026-07 migration note: ``claude-sonnet-5`` and the rolling ``claude-haiku-4-5``
alias are newly REGISTERED here as candidates so the standing weekly sweep
(``execution/run_weekly_model_eval.py``) and ``execution/eval_model_downgrade.py``
can discover and eval-gate them per purpose — registration alone does NOT
promote anything. ``claude-sonnet-4-6`` and the dated
``claude-haiku-4-5-20251001`` snapshot remain the ``LLM_MODELS`` incumbent
pins in ``src/llm/cli.py`` (unchanged) until a real per-purpose SWITCH_DOWN
verdict lands — see ``directives/cheapest_model_routing.md`` §4 for the
2026-07 attempt's status (blocked on an expired Claude CLI OAuth session, no
promotions shipped this round). ``claude-opus-4-8`` keeps its id (no swap,
already the incumbent) but its price corrects sharply downward from the
stale $15/$75 entry to the verified 2026-07-21 $5/$25.
"""

from __future__ import annotations

from dataclasses import dataclass

CLAUDE = "claude"
GEMINI = "gemini"
OPENROUTER = "openrouter"


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
    # Claude tiers (public API list prices, $/MTok in/out; verified against
    # model-frontier.REFERENCE.md 2026-07-21 unless noted).
    # Dated Haiku snapshot — the CURRENT LLM_MODELS incumbent pin
    # (FAST_CLASSIFIER_MODEL). Price is its historical, still-accurate value.
    "claude-haiku-4-5-20251001": ModelCost("claude-haiku-4-5-20251001", CLAUDE, 0.80, 4.00),
    # Rolling alias of the same Haiku 4.5 model family — NEWLY REGISTERED
    # 2026-07 as an eval candidate for the dated snapshot above. Not yet an
    # LLM_MODELS pin; promote only after a per-purpose SWITCH_DOWN verdict.
    "claude-haiku-4-5": ModelCost("claude-haiku-4-5", CLAUDE, 1.00, 5.00),
    # The CURRENT LLM_MODELS incumbent pin (DEFAULT_MODEL).
    "claude-sonnet-4-6": ModelCost("claude-sonnet-4-6", CLAUDE, 3.00, 15.00),
    # NEWLY REGISTERED 2026-07 as an eval candidate for claude-sonnet-4-6 (see
    # directives/cheapest_model_routing.md §4). Launch pricing through
    # 2026-08-31; becomes $3.00/$15.00 after — restamp this row when that date
    # passes (frontier reference notes). Not yet an LLM_MODELS pin.
    "claude-sonnet-5": ModelCost("claude-sonnet-5", CLAUDE, 2.00, 10.00),
    # Superseded tier, not in the current frontier reference — no verified
    # 2026-07 price exists for it, so it's left at its last-known value
    # (historical replay only; no purpose pins this id).
    "claude-opus-4-7": ModelCost("claude-opus-4-7", CLAUDE, 15.00, 75.00),
    # Same id as the incumbent purpose pins (no model swap) — price corrected
    # from the stale $15/$75 entry to the verified 2026-07-21 $5/$25.
    "claude-opus-4-8": ModelCost("claude-opus-4-8", CLAUDE, 5.00, 25.00),
    "claude-fable-5": ModelCost("claude-fable-5", CLAUDE, 10.00, 50.00),
    # Gemini API tiers (public API prices, $/MTok in/out, 2026-06).
    # Matches GEMINI_BACKEND_FAST_MODEL / _DEFAULT_MODEL in src/llm/gemini_backend.py
    # (the Gemini Developer API backend — see directives/gemini_backend.md for the
    # 2026-07 migration off gemini-cli). The fast tier (Flash) self-anneals on
    # NotFound: gemini-3-flash-preview → API-catalog-discovered model → gemini-2.5-flash
    # (stable GA fallback).
    # Both Flash ids live here so family_of() routes either to the Gemini backend.
    "gemini-3-flash-preview": ModelCost("gemini-3-flash-preview", GEMINI, 0.30, 2.50),
    "gemini-2.5-flash": ModelCost("gemini-2.5-flash", GEMINI, 0.30, 2.50),
    "gemini-3.1-pro-preview": ModelCost("gemini-3.1-pro-preview", GEMINI, 1.25, 10.00),
    # OpenRouter cheap-candidate pool (metered gateway; src/llm/openrouter_backend.py).
    # The pareto optimizer's WIDE, cheap axis — open-weight models graded per purpose
    # by the same brand-blind judge before any promotion. Ids are OpenRouter's
    # provider/model slugs. PRICES ARE SEED APPROXIMATIONS for pre-call RANKING ONLY:
    # the backend records OpenRouter's REAL charged cost per call (usage.cost), so the
    # ledger stays accurate regardless of drift here. Curate/verify against
    # https://openrouter.ai/models before trusting a rank. These are EXCLUDED from the
    # automatic sweep by default (cheaper_candidates(..., include_openrouter=False)) —
    # opt a purpose in deliberately once the meta-eval nominates it.
    "deepseek/deepseek-chat": ModelCost("deepseek/deepseek-chat", OPENROUTER, 0.30, 1.10),
    "qwen/qwen-2.5-72b-instruct": ModelCost("qwen/qwen-2.5-72b-instruct", OPENROUTER, 0.35, 0.40),
}


# Judge pool by family (meta_eval_governance.md §6): the pairwise judge for a
# verdict comes from >=2 DISTINCT families. OpenRouter open-weight models are
# candidates, NOT judges, until a judge-agreement spot-check certifies one —
# judging needs the discriminating model to out-class both contestants.
# The cheap, INDEPENDENT (non-Anthropic, non-Google) judge. Picked from the
# live OpenRouter catalogue 2026-07-25: $0.094/$0.188 per MTok with a 1M
# context, so a judge prompt never truncates, and DeepSeek's reasoning
# lineage is the strongest of the cheap open-weight families. Verified
# live as a judge before wiring.
DEEPSEEK_JUDGE_MODEL = "deepseek/deepseek-v4-flash"

# Judge backend -> model. Cross-family by construction: a pool that is all
# one family measures its own preferences (see llm_quality_program §P4).
# The judges a SCHEDULED run uses by default. Gemini is deliberately absent:
# its key is invalid (measured 2026-07-25), so including it made half of every
# judgment error -> JUDGE_DEGRADED -> nothing could ever clear a promotion bar.
# The pair below is cross-family by construction (open-weight + OpenAI), which
# is the point: a same-family pool scored the same candidate 100% where these
# two scored it 50%. Re-add gemini here once its key is rotated.
DEFAULT_JUDGES: tuple[str, ...] = ("deepseek", "codex")

JUDGE_POOL: dict[str, str] = {
    CLAUDE: "claude-opus-4-8",
    GEMINI: "gemini-3.1-pro-preview",
    "deepseek": DEEPSEEK_JUDGE_MODEL,
    "codex": "gpt-5.6-terra",
}


def model_cost(model_id: str) -> ModelCost | None:
    return MODEL_LADDER.get(model_id)


def family_of(model_id: str) -> str | None:
    cost = MODEL_LADDER.get(model_id)
    return cost.family if cost is not None else None


def backend_for(model_id: str) -> str:
    """The backend a model id dispatches to — ladder family when registered,
    else the ``provider/model`` slash-slug convention marks OpenRouter (the
    frontier-research overlay discovers models the static ladder hasn't seen;
    they must still route to the right transport), else Claude."""
    fam = family_of(model_id)
    if fam is not None:
        return fam
    return OPENROUTER if "/" in model_id else CLAUDE


def ladder_sha() -> str:
    """Deterministic fingerprint of the STATIC ladder (ids + prices). A change
    (new model, price restamp) is exactly when re-nomination pays — the weekly
    orchestrator compares this against the newest nomination run's stamp."""
    import hashlib

    payload = "|".join(
        f"{mid}:{c.family}:{c.input_usd_per_mtok}:{c.output_usd_per_mtok}"
        for mid, c in sorted(MODEL_LADDER.items())
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


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


def cheaper_candidates(
    incumbent: str, *, include_gemini: bool = True, include_openrouter: bool = False
) -> list[str]:
    """All registered models strictly cheaper than ``incumbent``, cheapest first.

    ``include_gemini=False`` restricts to same-family (Claude-tier) downgrades —
    useful when a cheaper Claude tier is desired without crossing to the Gemini backend.

    ``include_openrouter`` defaults to **False**: the OpenRouter open-weight pool is
    the widest, cheapest axis but is OPT-IN, so the automatic sweep doesn't flood
    every purpose with untested third-backend models before the meta-eval (or the
    owner) deliberately nominates them. Pass ``include_openrouter=True`` to fold
    them into the candidate set once a purpose is opted in.
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
        and (include_openrouter or cost.family != OPENROUTER)
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
