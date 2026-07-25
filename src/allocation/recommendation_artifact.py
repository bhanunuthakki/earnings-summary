"""The governed generate + persist step for the Incremental Dollar
Recommendation (PRD ``docs/design/personal_investment_partner_prd.md`` §7.4
governed-judgment/validation/persistence/fallback, §10 LLM governance).

Flow (see :func:`generate_recommendation`):

1. :func:`allocation.recommendation.build_frontier` — the deterministic P0.3
   layer. An empty frontier (retain_cash only, nothing eligible) short-
   circuits to a deterministic result with NO LLM call — there is nothing for
   a governed judgment to add to "there is no eligible name".
2. Budget pre-check (``should_skip_for_budget``) then the governed structured
   call (``call_llm_structured``, purpose=``incremental_dollar_recommendation``).
3. Schema validation (``IncrementalDollarRecommendation.model_validate``) +
   frontier-grounding validation
   (``IncrementalDollarRecommendation.validate_against_frontier``). Any
   failure reasons feed back into ONE corrective retry; still failing routes
   to the deterministic fallback.
4. Persist via ``llm_artifact_store.upsert`` — scope="portfolio",
   purpose="incremental_dollar_recommendation", cached on
   (frontier.input_sha, cash_usd, horizon).

Error handling (§10.6): ``LLMBudgetExceeded`` is caught explicitly here and
converted into a LABELED deterministic fallback (never silently swapped to a
cheaper model/provider — the artifact's ``selection_mode`` and
``degraded_reasons`` say plainly that budget forced the fallback, which is
"explicit unavailability" per §10.6, not a swallow). Any other
``is_hard_stop`` exception (auth/setup) propagates loud. Everything else
(transient — timeouts, non-zero exits, parse failures beyond the one retry)
degrades to the same labeled fallback.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import llm_artifact_store
from allocation.recommendation import (
    DeterministicFrontier,
    FrontierPlan,
    build_frontier,
    format_allocation_citation,
)
from allocation.recommendation_schema import IncrementalDollarRecommendation
from llm.cli import LLMBudgetExceeded, is_hard_stop
from llm.prompt_versions import prompt_version_for
from llm.structured import StructuredParseError, call_llm_structured
from llm_budget import should_skip_for_budget
from owner_profile.store import list_facts

PURPOSE = "incremental_dollar_recommendation"
ENGINE_VERSION = "v1"

__all__ = ["RecommendationResult", "generate_recommendation"]


@dataclass(frozen=True, slots=True)
class RecommendationResult:
    """The generator's output — always populated, never a bare exception for
    a degraded run (only ``is_hard_stop`` exceptions propagate)."""

    artifact_id: int | None
    recommendation: IncrementalDollarRecommendation
    selection_mode: str  # "llm" | "deterministic_fallback"
    degraded_reasons: tuple[str, ...] = ()


# --------------------------------------------------------------------------- #
# Prompt composition
# --------------------------------------------------------------------------- #


def _owner_context_labels(db_path: Path) -> list[str]:
    """A handful of AFFIRMED owner-profile facts as short labels — context
    for the LLM's judgment, never a source of blocking truth (that's the
    frontier's job). Best-effort: any failure yields an empty list."""
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error:
        return []
    try:
        rows = list_facts(conn, status="affirmed")
    except Exception:
        return []
    finally:
        conn.close()
    return [f"[{r.category}] {r.narrative}"[:200] for r in rows[:10]]


def _format_plan(plan: FrontierPlan) -> str:
    if not plan.allocations:
        lines = [f"- {plan.kind}: no allocation, ${plan.cash_retained_usd:,.2f} retained"]
    else:
        lines = [f"- {plan.kind}:"]
        for a in plan.allocations:
            lines.append(f"    {format_allocation_citation(a)}")
        lines.append(f"    cash retained: ${plan.cash_retained_usd:,.2f}")
    for fact in plan.rationale_facts:
        lines.append(f"    fact: {fact}")
    return "\n".join(lines)


def _build_prompt(
    frontier: DeterministicFrontier,
    *,
    cash_usd: float,
    horizon: str | None,
    owner_context: list[str],
    corrective_reasons: list[str] | None = None,
) -> str:
    plans_block = "\n".join(_format_plan(p) for p in frontier.plans)
    ineligible_block = (
        "\n".join(f"- {t}: {', '.join(reasons)}" for t, reasons in frontier.ineligible) or "(none)"
    )
    owner_block = "\n".join(f"- {label}" for label in owner_context) or "(no affirmed facts)"
    freshness_block = (
        "\n".join(f"- {k}: {v}" for k, v in sorted(frontier.source_freshness.items())) or "(none)"
    )
    horizon_line = f"Stated horizon: {horizon}" if horizon else "No horizon stated."
    corrective_block = ""
    if corrective_reasons:
        corrective_block = (
            "\n\nYOUR PRIOR ATTEMPT FAILED THESE CHECKS — fix every one:\n"
            + "\n".join(f"- {r}" for r in corrective_reasons)
        )

    return f"""You are giving a governed incremental-dollar recommendation for
${cash_usd:,.2f} of new cash. {horizon_line}

DETERMINISTIC FRONTIER (the ONLY plans/allocations/tickers you may recommend —
never invent a ticker, dollar amount, or weight not derivable from this):
{plans_block}

INELIGIBLE NAMES (never recommend these; you may mention WHY they're blocked):
{ineligible_block}

SOURCE FRESHNESS (cite only these keys, or facts verbatim from the frontier
above, in source_refs):
{freshness_block}

OWNER CONTEXT (affirmed facts — weigh, never override the frontier's gate):
{owner_block}

VALIDATION CONSTRAINTS (a violation here forces a deterministic fallback —
your judgment is discarded, so follow these exactly):
- Every allocation ticker MUST be one already present in the frontier plans
  above (an eligible name or a name the frontier itself sized). No others.
- For each plan: sum(allocations.dollars) + cash_retained_usd MUST equal
  ${cash_usd:,.2f} within a cent.
- resulting_weight_pct MUST match the frontier's own math for that ticker —
  do not invent a different number.
- Any allocation reaching >= 12% of the book MUST carry its concentration
  zone (copy it from the frontier plan above).
- main_unknowns, disconfirming_evidence, and confidence_basis must NEVER be
  empty — state your genuine uncertainty, not a placeholder.
- NEVER state a numeric probability ("62% chance", "70% probability") unless
  you are citing a documented historical base rate — say "we do not confirm
  numeric probabilities" is the house rule; use qualitative confidence
  language instead (PRD §2.3 house style: "This is my preferred plan.", "The
  main reason I could be wrong is...").
- source_refs may only cite the SOURCE FRESHNESS keys above or facts that
  appear verbatim in the frontier plans — never an invented citation.
{corrective_block}

Respond with ONE JSON object matching this shape exactly:
{{
  "as_of_date": "<ISO date>",
  "input_sha": "{frontier.input_sha}",
  "status": "deploy_all" | "deploy_partial" | "retain_all" | "defer",
  "preferred_plan": {{
    "allocations": [{{"ticker": "...", "dollars": <num>, "pct_of_cash": <num>,
                       "resulting_weight_pct": <num>, "zone": "<zone or null>"}}],
    "cash_retained_usd": <num>
  }},
  "best_alternative": <same shape as preferred_plan, or null>,
  "best_diversifier": <same shape as preferred_plan, or null>,
  "central_hypothesis": "This is my preferred plan because ...",
  "personalization_why": "<why THIS owner, THIS book, THIS cash amount>",
  "supporting_evidence": ["<fact>", ...],
  "main_unknowns": ["<what could change this>", ...],
  "disconfirming_evidence": ["<what would argue against this>", ...],
  "scenario_reasoning": "<brief>",
  "confidence_verbal": "low" | "moderate" | "high",
  "confidence_basis": "The main reason I could be wrong is ...",
  "followup_research": ["<optional next research step>", ...],
  "frontier_plan_ids": ["<frontier plan kinds you drew from>", ...],
  "source_refs": ["<source_freshness key or verbatim frontier fact>", ...],
  "risk_snapshot_ref": null
}}"""


# --------------------------------------------------------------------------- #
# Deterministic fallback
# --------------------------------------------------------------------------- #


def _fallback_plan_from_frontier(plan: FrontierPlan, cash_usd: float) -> dict[str, object]:
    return {
        "allocations": [
            {
                "ticker": a.ticker,
                "dollars": a.dollars,
                "pct_of_cash": round((a.dollars / cash_usd * 100.0), 4) if cash_usd > 0 else 0.0,
                "resulting_weight_pct": a.resulting_weight_pct,
                "zone": a.zone,
            }
            for a in plan.allocations
        ],
        "cash_retained_usd": plan.cash_retained_usd,
    }


def _deterministic_fallback(
    frontier: DeterministicFrontier,
    *,
    cash_usd: float,
    extra_degraded: str | None = None,
) -> IncrementalDollarRecommendation:
    """§7.4's mechanical fallback: the frontier's balanced plan if it clears
    the cash-conservation invariant, else retain_cash. NEVER an empty
    artifact disguised as no-recommendation — retain_cash (allocations=[],
    cash_retained_usd=cash) is itself a valid, fully-populated
    recommendation."""
    by_kind = {p.kind: p for p in frontier.plans}
    chosen = by_kind.get("balanced")
    if (
        chosen is None
        or abs(sum(a.dollars for a in chosen.allocations) + chosen.cash_retained_usd - cash_usd)
        > 0.01
    ):
        chosen = by_kind.get("retain_cash") or FrontierPlan(
            kind="retain_cash",
            allocations=(),
            cash_retained_usd=cash_usd,
            rationale_facts=("no deterministic plan available — cash retained",),
        )

    if not chosen.allocations:
        status = "retain_all"
    elif chosen.cash_retained_usd <= 0.01:
        status = "deploy_all"
    else:
        status = "deploy_partial"

    unknowns = [
        "this is a mechanical fallback — no LLM judgment was applied, so no "
        "scenario-specific unknowns were reasoned about beyond the frontier's own facts"
    ]
    disconfirming = [
        "the deterministic frontier does not weigh qualitative catalysts or "
        "near-term news — a fresh read could change the picture"
    ]
    facts = list(chosen.rationale_facts) or ["no rationale facts available from the frontier"]
    degraded_note = f" ({extra_degraded})" if extra_degraded else ""

    # Built as a plain JSON-shaped dict and re-validated through
    # model_validate — the repo's one-cast JSON-boundary pattern — rather
    # than constructed field-by-field, so the Literal fields (status,
    # confidence_verbal, selection_mode) are validated the same way an LLM
    # payload is, with no scattered per-field casts.
    payload: dict[str, object] = {
        "as_of_date": frontier.as_of,
        "input_sha": frontier.input_sha,
        "status": status,
        "preferred_plan": _fallback_plan_from_frontier(chosen, cash_usd),
        "best_alternative": None,
        "best_diversifier": None,
        "central_hypothesis": f"Mechanical fallback plan{degraded_note}: {facts[0]}",
        "personalization_why": (
            "No governed LLM judgment ran for this cash amount — this is the "
            "deterministic frontier's balanced (or retain-cash) plan, unmodified."
        ),
        "supporting_evidence": list(facts),
        "main_unknowns": unknowns,
        "disconfirming_evidence": disconfirming,
        "scenario_reasoning": "mechanical fallback — no scenario reasoning applied",
        "confidence_verbal": "low",
        "confidence_basis": "mechanical fallback — no synthesized confidence",
        "followup_research": [],
        "frontier_plan_ids": [chosen.kind],
        "source_refs": [],
        "risk_snapshot_ref": None,
        "engine_version": ENGINE_VERSION,
        "prompt_version": prompt_version_for(PURPOSE),
        "selection_mode": "deterministic_fallback",
    }
    return IncrementalDollarRecommendation.model_validate(payload)


# --------------------------------------------------------------------------- #
# Top level
# --------------------------------------------------------------------------- #


def _render_markdown(rec: IncrementalDollarRecommendation) -> str:
    lines = [
        f"# Incremental Dollar Recommendation ({rec.as_of_date})",
        "",
        f"**Status:** {rec.status}  |  **Confidence:** {rec.confidence_verbal}  "
        f"|  **Mode:** {rec.selection_mode}",
        "",
        "## Preferred plan",
    ]
    if not rec.preferred_plan.allocations:
        lines.append(f"Retain ${rec.preferred_plan.cash_retained_usd:,.2f} in cash.")
    else:
        for a in rec.preferred_plan.allocations:
            lines.append(
                f"- **{a.ticker}**: ${a.dollars:,.2f} ({a.pct_of_cash:.1f}% of cash) -> "
                f"{a.resulting_weight_pct:.2f}% of book" + (f" (zone: {a.zone})" if a.zone else "")
            )
        lines.append(f"- Cash retained: ${rec.preferred_plan.cash_retained_usd:,.2f}")
    lines += [
        "",
        f"**This is my preferred plan.** {rec.central_hypothesis}",
        "",
        f"**Why this fits you:** {rec.personalization_why}",
        "",
        "## What I could be wrong about",
        f"The main reason I could be wrong is: {rec.confidence_basis}",
    ]
    if rec.main_unknowns:
        lines.append("")
        lines.append("Main unknowns:")
        lines += [f"- {u}" for u in rec.main_unknowns]
    if rec.disconfirming_evidence:
        lines.append("")
        lines.append("Disconfirming evidence:")
        lines += [f"- {d}" for d in rec.disconfirming_evidence]
    return "\n".join(lines)


def generate_recommendation(
    db_path: Path,
    repo_root: Path,
    *,
    cash_usd: float,
    horizon: str | None = None,
    api_url: str | None = None,
) -> RecommendationResult:
    """The full P0.4a governed pipeline for one cash amount. Never raises
    except for a genuine hard stop (auth/setup) — every other failure mode
    (budget, transient, validation-rejected) degrades to a labeled
    deterministic-fallback ``RecommendationResult``."""
    frontier = build_frontier(db_path, repo_root, cash_usd=cash_usd, api_url=api_url)
    degraded: list[str] = list(frontier.degraded)

    only_retain_cash = len(frontier.plans) == 1 and frontier.plans[0].kind == "retain_cash"
    if only_retain_cash and not frontier.eligible_tickers:
        rec = _deterministic_fallback(frontier, cash_usd=cash_usd)
        artifact_id = _persist(frontier, rec, cash_usd=cash_usd, horizon=horizon, db_path=db_path)
        return RecommendationResult(
            artifact_id=artifact_id,
            recommendation=rec,
            selection_mode="deterministic_fallback",
            degraded_reasons=tuple(degraded),
        )

    skip_check = should_skip_for_budget(PURPOSE, db_path=db_path)
    if skip_check is not None:
        degraded.append(f"forgone due to budget ({skip_check.reason or 'over cap'})")
        rec = _deterministic_fallback(frontier, cash_usd=cash_usd, extra_degraded="budget forgone")
        artifact_id = _persist(frontier, rec, cash_usd=cash_usd, horizon=horizon, db_path=db_path)
        return RecommendationResult(
            artifact_id=artifact_id,
            recommendation=rec,
            selection_mode="deterministic_fallback",
            degraded_reasons=tuple(degraded),
        )

    owner_context = _owner_context_labels(db_path)

    rec, mode, extra_degraded = _try_llm(
        frontier, cash_usd=cash_usd, horizon=horizon, owner_context=owner_context, db_path=db_path
    )
    if extra_degraded:
        degraded.append(extra_degraded)

    artifact_id = _persist(frontier, rec, cash_usd=cash_usd, horizon=horizon, db_path=db_path)
    return RecommendationResult(
        artifact_id=artifact_id,
        recommendation=rec,
        selection_mode=mode,
        degraded_reasons=tuple(degraded),
    )


def _try_llm(
    frontier: DeterministicFrontier,
    *,
    cash_usd: float,
    horizon: str | None,
    owner_context: list[str],
    db_path: Path,
) -> tuple[IncrementalDollarRecommendation, str, str | None]:
    """Returns (recommendation, selection_mode, extra_degraded_reason)."""
    prompt = _build_prompt(
        frontier, cash_usd=cash_usd, horizon=horizon, owner_context=owner_context
    )
    try:
        rec, reasons = _call_and_validate(prompt, frontier, cash_usd=cash_usd, db_path=db_path)
        if not reasons:
            return rec, "llm", None
        # ONE corrective retry, feeding the failure reasons back.
        retry_prompt = _build_prompt(
            frontier,
            cash_usd=cash_usd,
            horizon=horizon,
            owner_context=owner_context,
            corrective_reasons=reasons,
        )
        rec, reasons = _call_and_validate(
            retry_prompt, frontier, cash_usd=cash_usd, db_path=db_path
        )
        if not reasons:
            return rec, "llm", None
        fallback = _deterministic_fallback(
            frontier,
            cash_usd=cash_usd,
            extra_degraded="LLM output failed frontier validation twice",
        )
        return (
            fallback,
            "deterministic_fallback",
            f"LLM output rejected: {'; '.join(reasons[:5])}",
        )
    except LLMBudgetExceeded as exc:
        # Explicit unavailability (§10.6) — never a silent provider swap.
        fallback = _deterministic_fallback(
            frontier, cash_usd=cash_usd, extra_degraded="budget exceeded"
        )
        return fallback, "deterministic_fallback", f"budget exceeded: {exc}"
    except Exception as exc:
        if is_hard_stop(exc):
            raise
        fallback = _deterministic_fallback(
            frontier, cash_usd=cash_usd, extra_degraded="transient LLM failure"
        )
        return fallback, "deterministic_fallback", f"transient LLM failure: {exc}"


def _call_and_validate(
    prompt: str, frontier: DeterministicFrontier, *, cash_usd: float, db_path: Path
) -> tuple[IncrementalDollarRecommendation, list[str]]:
    payload = call_llm_structured(
        prompt,
        purpose=PURPOSE,
        scope="portfolio",
        expect="object",
        required_keys=("status", "preferred_plan", "central_hypothesis"),
        db_path=db_path,
    )
    raw = cast("dict[str, object]", payload)
    raw.setdefault("engine_version", ENGINE_VERSION)
    raw.setdefault("prompt_version", prompt_version_for(PURPOSE))
    raw.setdefault("selection_mode", "llm")
    raw.setdefault("as_of_date", frontier.as_of)
    raw.setdefault("input_sha", frontier.input_sha)
    try:
        rec = IncrementalDollarRecommendation.model_validate(raw)
    except Exception as exc:
        raise StructuredParseError(
            f"{PURPOSE}: LLM JSON failed schema validation: {exc}", raw_head=str(raw)[:500]
        ) from exc
    reasons = rec.validate_against_frontier(frontier, cash_usd)
    return rec, reasons


def _persist(
    frontier: DeterministicFrontier,
    rec: IncrementalDollarRecommendation,
    *,
    cash_usd: float,
    horizon: str | None,
    db_path: Path,
) -> int | None:
    content_json = rec.model_dump(mode="json")
    artifact_id, _cache_hit = llm_artifact_store.upsert(
        llm_artifact_store.UpsertRequest(
            ticker=None,
            scope="portfolio",
            purpose=PURPOSE,
            content_json=content_json,
            content_md=_render_markdown(rec),
            prompt_version=rec.prompt_version,
            cache_inputs=[frontier.input_sha, str(cash_usd), horizon or ""],
        ),
        db_path=db_path,
    )
    return artifact_id
