"""Telegram summary for the Incremental Dollar Recommendation.

This module builds text and keyboards only; sending remains owned by the
existing Telegram transport. The renderer deliberately accepts no database
or portfolio-balance input, preserving the channel's privacy boundary.
"""

from __future__ import annotations

from allocation.recommendation_schema import IncrementalDollarRecommendation
from capture import telegram

__all__ = [
    "recommendation_callback_prefix",
    "recommendation_keyboard",
    "recommendation_message_text",
]

_DEEP_LINK = "/#portfolio_allocation"
_EXCERPT_CHARS = 300


def _excerpt(text: str, limit: int = _EXCERPT_CHARS) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit].rstrip() + "..."


def _plan_line(rec: IncrementalDollarRecommendation) -> str:
    plan = rec.preferred_plan
    if not plan.allocations:
        return "Retain all new cash."
    parts = [
        f"{allocation.ticker} {allocation.pct_of_cash:.0f}% of the new cash -> "
        f"{allocation.resulting_weight_pct:.1f}% of book"
        + (f" ({allocation.zone})" if allocation.zone else "")
        for allocation in plan.allocations
    ]
    if plan.cash_retained_usd > 0.01:
        total = sum(allocation.dollars for allocation in plan.allocations)
        total += plan.cash_retained_usd
        retained_pct = (plan.cash_retained_usd / total * 100.0) if total > 0 else 0.0
        parts.append(f"retain {retained_pct:.0f}% of the new cash")
    return "; ".join(parts)


def recommendation_message_text(
    rec: IncrementalDollarRecommendation,
    artifact_id: int,
) -> str:
    """Render percentages and rationale without book/account dollar values."""
    status_word = rec.status.replace("_", " ")
    lines = [f"Next dollar ({status_word}): {_plan_line(rec)}"]
    if rec.selection_mode == "deterministic_fallback":
        lines.append("(mechanical fallback -- no governed judgment applied, no confidence shown)")
    else:
        lines.append(f"Confidence: {rec.confidence_verbal}")
    if rec.central_hypothesis:
        lines.append(_excerpt(rec.central_hypothesis))
    if rec.disconfirming_evidence:
        lines.append(f"Main disconfirmer: {_excerpt(rec.disconfirming_evidence[0], 200)}")
    lines.append(f"Review in app: {_DEEP_LINK} (artifact #{artifact_id})")
    return "\n\n".join(lines)


def recommendation_callback_prefix() -> str:
    """Return the compact callback kind token for allocation actions."""
    return "al"


def recommendation_keyboard(artifact_id: int) -> dict[str, object]:
    """Build the Why / Review / Dismiss callback keyboard."""
    prefix = recommendation_callback_prefix()
    return telegram.inline_keyboard(
        [
            [
                ("Why", f"{prefix}:why:{artifact_id}"),
                ("Review in app", f"{prefix}:open:{artifact_id}"),
                ("Dismiss", f"{prefix}:dismiss:{artifact_id}"),
            ]
        ]
    )
