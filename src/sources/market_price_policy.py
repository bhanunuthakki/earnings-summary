"""Shared market-bar freshness limit used by risk/reward and decision grading."""

# Preserve the existing risk_reward policy: a market price older than a week is stale.
PRICE_STALE_DAYS = 7
