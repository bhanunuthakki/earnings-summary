"""Telegram summary for the Incremental Dollar Recommendation (P0.4b, PRD
``docs/design/personal_investment_partner_prd.md`` §7.4 surface parity /
§11.4 privacy).

This module builds TEXT only — sending stays manual/deferred (the Senior
Partner Brief owns proactive delivery in P2; no new scheduled send is added
here, matching the directive: "do not add any scheduled send"). The render
follows the same shape as ``pipeline.weekly_packet.item_message_text`` /
``item_keyboard`` (a pure text builder + a pure keyboard builder, both
independent of any send call), and the callback dispatch mirrors
``capture.research_notify``'s ``kind:verb:id`` triples (``al:why:<id>`` /
``al:dismiss:<id>`` / ``al:open:<id>``).

§11.4 privacy — ALLOWED: ticker, action, recommended PERCENTAGES, rationale,
verbal confidence, uncertainty, disconfirmers, deep links, and the
owner-SUPPLIED deploy amount itself (the cash figure the owner typed in —
not a balance the app is disclosing). NEVER shown: total portfolio value,
exact account balances, tax-lot detail, household income/capacity amounts,
or any per-allocation DOLLAR breakdown (that arithmetic is reachable only in
the Tailscale-protected web surface, PRD §11.4's "full sensitive detail
remains" clause) — only the resulting weight PERCENTAGE per name.
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
        f"{a.ticker} {a.pct_of_cash:.0f}% of the new cash -> {a.resulting_weight_pct:.1f}% of book"
        + (f" ({a.zone})" if a.zone else "")
        for a in plan.allocations
    ]
    if plan.cash_retained_usd > 0.01:
        total = sum(a.dollars for a in plan.allocations) + plan.cash_retained_usd
        retained_pct = (plan.cash_retained_usd / total * 100.0) if total > 0 else 0.0
        parts.append(f"retain {retained_pct:.0f}% of the new cash")
    return "; ".join(parts)


def recommendation_message_text(rec: IncrementalDollarRecommendation, artifact_id: int) -> str:
    """The concise Telegram summary for one recommendation artifact:
    action(s) + percentages, one-line rationale, verbal confidence, main
    disconfirmer, deep link. No book total, no account balances, no
    per-allocation dollar breakdown (see module docstring)."""
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
    """The ``kind`` token used in ``kind:verb:id`` callback data for this
    surface — exposed so ``capture.research_notify`` can match on it without
    a magic-string duplicate."""
    return "al"


def recommendation_keyboard(artifact_id: int) -> dict[str, object]:
    """Why / Review in app / Dismiss — all callback buttons (no URL button:
    the web surface is Tailscale-only, not a public link Telegram can open
    without VPN context, so "Review in app" answers with the deep-link path
    instead of navigating there directly, same as every other card here)."""
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
