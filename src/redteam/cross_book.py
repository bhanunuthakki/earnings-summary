"""The three cross-book adversarial passes (monthly_red_team.md Phase 2):
factor-block detection, style drift vs. stated strategy, and a human-capital
overlay. Each is ONE structured LLM call over deterministic, code-assembled
inputs — the pass never asks the LLM to compute a correlation or a factor
loading, only to judge what a human-readable summary of already-computed
numbers implies.

Evidence assembly reuses the existing disk-read substrates
(``portfolio_correlation``, ``portfolio_style_factors``) rather than
recomputing anything — pure Layer-3 composition.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from pathlib import Path

from pydantic import TypeAdapter, ValidationError

from llm.structured import StructuredParseError, call_llm_structured
from redteam.lenses import load_holdings_json
from redteam.models import RedTeamLLMItem

log = logging.getLogger(__name__)

PURPOSE = "red_team_cross_book"

# The owner's income is Meta/tech/AI-correlated (task directive, human-capital
# overlay spec). This is a small STATIC config, not LLM-inferred — the pass
# only asks the LLM to judge whether the book's OWN AI-linked bets compound
# that already-known factor, and propose a concrete diversification rule.
OWNER_INCOME_CORRELATED_SECTOR = "Big Tech / AI (compensation tied to a large-cap AI/tech employer)"
AI_LINKED_TICKERS: frozenset[str] = frozenset(
    {"META", "GOOGL", "GOOG", "MSFT", "AMZN", "NVDA", "NOW", "VEEV", "RBRK"}
)


def _business_one_liner(repo_root: Path, ticker: str) -> str:
    """A short, code-assembled business-description line for ``ticker`` —
    ``key_driver`` from the holdings JSON, falling back to a bare ticker
    when no holdings file exists (the correlation/style rollups can include
    names with no written thesis)."""
    payload = load_holdings_json(repo_root, ticker)
    if payload is None:
        return ticker
    key_driver = payload.get("key_driver")
    if isinstance(key_driver, str) and key_driver.strip():
        return f"{ticker} ({key_driver.strip()})"
    return ticker


_SCHEMA_INSTRUCTIONS = (
    "Return ONLY a JSON object with this exact shape:\n"
    '{"attack_md": "<2-4 sentences: the falsifiable claim, anchored to the '
    'numbers given>", '
    '"question_md": "<ONE direct, falsifiable question the analyst must '
    'answer>", '
    '"proposed_change_md": "<ONE concrete rule or scenario change>", '
    '"severity": "<one of high, med, low>"}\n\n'
    "Output the JSON object and nothing else — no markdown fences, no "
    "commentary, no prefatory prose."
)


class RedTeamCrossBookError(RuntimeError):
    """Raised when a cross-book pass's call returns unusable JSON on both
    attempts or fails schema validation. See ``lenses.RedTeamLensError`` for
    the same contract on the per-name side."""


def _call(prompt: str, *, lens: str, run_key: str) -> RedTeamLLMItem:
    try:
        payload = call_llm_structured(
            prompt,
            purpose=PURPOSE,
            scope=f"red_team:{lens}",
            run_id=run_key,
            expect="object",
            required_keys=("attack_md", "question_md", "proposed_change_md", "severity"),
            schema=TypeAdapter(RedTeamLLMItem),
        )
    except StructuredParseError as exc:
        raise RedTeamCrossBookError(f"{lens}: unusable JSON: {exc}") from exc
    try:
        return RedTeamLLMItem.model_validate(payload)
    except ValidationError as exc:
        raise RedTeamCrossBookError(f"{lens}: schema validation failed: {exc}") from exc


def factor_block_prompt(
    repo_root: Path,
    *,
    clusters: Sequence[tuple[tuple[str, ...], float, float]],
    weights: Mapping[str, float],
) -> str | None:
    """``clusters`` is ``(tickers, combined_weight_pct, avg_corr)`` tuples
    (from ``portfolio_correlation.CrowdingCluster``). ``None`` when there are
    no clusters — nothing to attack, the caller skips the pass entirely
    (zero LLM spend on an empty book-shape)."""
    if not clusters:
        return None
    lines: list[str] = []
    for tickers, combined_weight_pct, avg_corr in clusters:
        names = ", ".join(_business_one_liner(repo_root, t) for t in tickers)
        lines.append(
            f"- {names}: combined {combined_weight_pct:.1f}% of book, "
            f"avg pairwise correlation {avg_corr:.2f}"
        )
    other = ", ".join(f"{t} ({w:.1%})" for t, w in sorted(weights.items()))
    return (
        "You are the monthly adversarial Red Team reviewing a long-only "
        "equity book for hidden CONCENTRATION masquerading as diversification.\n\n"
        "ATTACK ANGLE: shared-factor / crowding block detection. The "
        "following clusters of held names move together (correlation >= 0.70 "
        "on trailing daily returns):\n" + "\n".join(lines) + "\n\n"
        f"Full book weights for context: {other}\n\n"
        "Identify the SINGLE most dangerous cluster — name the shared factor "
        "explicitly (a macro driver, a geography, a supply chain, a funding "
        "regime) — and argue why the owner is running a bigger single bet "
        "than the position sizes individually suggest.\n\n" + _SCHEMA_INSTRUCTIONS
    )


def style_drift_prompt(
    *,
    legs: Sequence[tuple[str, str, float | None]],
    strategy_summary: str,
) -> str | None:
    """``legs`` is ``(label, spread_label, book_beta)`` tuples (from
    ``portfolio_style_factors.StyleFactorLeg``). ``None`` when no leg has a
    usable beta."""
    usable = [(label, spread, beta) for label, spread, beta in legs if beta is not None]
    if not usable:
        return None
    lines = [f"- {label} ({spread}): book beta {beta:+.2f}" for label, spread, beta in usable]
    return (
        "You are the monthly adversarial Red Team checking a long-only "
        "equity book for STYLE DRIFT against its own stated strategy.\n\n"
        f"Stated strategy: {strategy_summary}\n\n"
        "Current style-factor loadings (book beta to each factor spread, "
        "renormalized over priced names):\n" + "\n".join(lines) + "\n\n"
        "ATTACK ANGLE: identify the loading that is MOST inconsistent with "
        "the stated strategy, and argue what unintended bet the book is "
        "actually running as a result (e.g. a value tilt hiding as GARP, "
        "momentum-chasing beyond the stated sleeve).\n\n" + _SCHEMA_INSTRUCTIONS
    )


def human_capital_prompt(*, weights: Mapping[str, float]) -> str | None:
    """Static config (``AI_LINKED_TICKERS``) + the book's actual weights —
    ``None`` when no held name is AI-linked (nothing to compound)."""
    linked = {t: w for t, w in weights.items() if t.upper() in AI_LINKED_TICKERS}
    if not linked:
        return None
    combined_pct = 100.0 * sum(linked.values())
    lines = ", ".join(f"{t} ({w:.1%})" for t, w in sorted(linked.items(), key=lambda kv: -kv[1]))
    return (
        "You are the monthly adversarial Red Team checking whether a "
        "long-only equity book compounds the owner's OWN human-capital risk.\n\n"
        f"Owner's income is correlated with: {OWNER_INCOME_CORRELATED_SECTOR}.\n\n"
        f"Held names also exposed to that same factor (AI/Big-Tech demand or "
        f"disruption): {lines} — combined {combined_pct:.1f}% of book.\n\n"
        "ATTACK ANGLE: the owner's paycheck AND a meaningful slice of the "
        "portfolio would draw down together in a Big-Tech/AI-capex "
        "slowdown. Argue how large that compounding bet actually is once "
        "human capital is treated as an (illiquid, un-diversifiable) asset "
        "in the total portfolio, and propose a concrete trim or hedge "
        "rule.\n\n" + _SCHEMA_INSTRUCTIONS
    )


def generate_factor_block_item(
    repo_root: Path,
    *,
    clusters: Sequence[tuple[tuple[str, ...], float, float]],
    weights: Mapping[str, float],
    run_key: str,
) -> RedTeamLLMItem | None:
    prompt = factor_block_prompt(repo_root, clusters=clusters, weights=weights)
    if prompt is None:
        return None
    return _call(prompt, lens="factor_block", run_key=run_key)


def generate_style_drift_item(
    *,
    legs: Sequence[tuple[str, str, float | None]],
    strategy_summary: str,
    run_key: str,
) -> RedTeamLLMItem | None:
    prompt = style_drift_prompt(legs=legs, strategy_summary=strategy_summary)
    if prompt is None:
        return None
    return _call(prompt, lens="style_drift", run_key=run_key)


def generate_human_capital_item(
    *,
    weights: Mapping[str, float],
    run_key: str,
) -> RedTeamLLMItem | None:
    prompt = human_capital_prompt(weights=weights)
    if prompt is None:
        return None
    return _call(prompt, lens="human_capital", run_key=run_key)
