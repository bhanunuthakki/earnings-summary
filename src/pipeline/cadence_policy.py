"""Single source of truth for refresh cadence constants.

Three concerns split across three modules historically — and they drifted apart:

  * `tier_runner.py::_DUE_THRESHOLDS_DAYS` — which TICKERS are due for any
    refresh tick (P1 always, P2 ≤ 7d, P3 ≤ 30d).
  * `refresh_cache.py::_CLASS_CADENCE_MULT` — how long each ENDPOINT CLASS
    stays fresh (statement = 14d for portfolio, etc.).
  * `refresh_dispatch.py::stale_fmp_days` — interactive single-ticker
    dispatcher; was hardcoded to 7d, conflicting with the 14d cadence
    refresh_cache enforces for the same FMP statement endpoints.

This module centralizes the constants so the dispatcher and the bulk cron
agree on "when is an FMP statement endpoint considered fresh". Tier-runner's
per-ticker thresholds stay where they are — that question is "is this ticker
on the daily cadence" rather than "is this endpoint stale", and the two
shouldn't be conflated.

Whenever an endpoint's expected update frequency changes (e.g. FMP starts
pushing intraday statement revisions), edit `STATEMENT_STALE_DAYS` here and
both callers pick up the new policy.
"""

from __future__ import annotations

# How long an FMP statement endpoint (income / balance / cash-flow / ratios /
# key-metrics / enterprise-values / owner-earnings / financial-reports /
# form-10-k-json) stays fresh for portfolio + watchlist tickers. Statements
# advance on earnings (quarterly cadence) so 14 days covers the publication
# window without burning the basic FMP tier's 250-call/day cap.
#
# refresh_cache.py multiplies the portfolio/watchlist base (24h) by 14.0 to
# arrive at the same number — keep the math explicit so a future contributor
# doesn't change one without the other.
STATEMENT_STALE_DAYS: int = 14

# How long FMP "time_sensitive" endpoints (DCF, ratings, prices, profile,
# analyst estimates) stay fresh for portfolio + watchlist. These advance
# daily; refreshing more often than that just burns calls without value.
TIME_SENSITIVE_STALE_DAYS: int = 1

# "Growth" endpoints (financial-growth, growth-by-period) sit between
# statements and time-sensitive: derived from quarter-on-quarter deltas but
# don't shift mid-quarter. 3 days = roughly the post-earnings window when
# revisions are most likely.
GROWTH_STALE_DAYS: int = 3

# Reference endpoints (peers, executives, employee count) change rarely.
# Monthly cadence keeps the universe in sync without burning calls.
REFERENCE_STALE_DAYS: int = 30


# Conservative estimate of FMP calls required to fully onboard one ticker
# (statements x4 endpoints x2 periods + segments + ratios + key-metrics +
# enterprise-values + DCF + profile + executives + ... = ~40). Used by the
# bulk-onboard pre-flight to refuse runs that would blow the daily tier cap
# halfway through.
ESTIMATED_FMP_CALLS_PER_ONBOARD: int = 40


def check_onboarding_budget(
    *,
    pending_count: int,
    remaining_calls: int,
    calls_per_onboard: int = ESTIMATED_FMP_CALLS_PER_ONBOARD,
) -> tuple[bool, str]:
    """Pre-flight gate for bulk-onboard runs.

    Returns (allowed, reason). `allowed=False` means the projected FMP call
    cost of onboarding `pending_count` tickers exceeds today's remaining
    tier budget — better to halt loudly than to silently queue work that
    will fail mid-run when the tier hits its cap.

    Callers should still respect the result even when remaining_calls is
    effectively unlimited (starter / premium) — the helper returns
    `allowed=True` immediately in that case.
    """
    if remaining_calls <= 0:
        return (
            False,
            f"tier cap exhausted: 0 calls remaining today, "
            f"need {pending_count * calls_per_onboard} for {pending_count} tickers",
        )
    needed = pending_count * calls_per_onboard
    if needed > remaining_calls:
        max_safe = remaining_calls // max(1, calls_per_onboard)
        return (
            False,
            f"insufficient tier budget: {pending_count} tickers x ~{calls_per_onboard} calls "
            f"= {needed} needed, only {remaining_calls} remaining today. "
            f"Re-run with --max {max_safe} to fit the budget or wait for cap reset.",
        )
    return (True, f"{needed} calls projected, {remaining_calls} remaining today")
