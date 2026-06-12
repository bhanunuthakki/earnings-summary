"""Tests for the onboarding pre-flight budget gate.

`check_onboarding_budget` is the pure decision function used by
execution/onboard_pending_tickers.py to refuse bulk onboards that would
blow the daily FMP tier cap halfway through.
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from pipeline.cadence_policy import (  # noqa: E402
    ESTIMATED_FMP_CALLS_PER_ONBOARD,
    check_onboarding_budget,
)


def test_allows_run_when_budget_covers_pending() -> None:
    """Basic tier with 250 calls/day cap, 100 already used, want to onboard
    3 tickers (3*40=120 calls projected, 150 remaining → fits)."""
    allowed, reason = check_onboarding_budget(pending_count=3, remaining_calls=150)
    assert allowed is True
    assert "120 calls projected" in reason
    assert "150 remaining" in reason


def test_blocks_when_projected_exceeds_remaining() -> None:
    """10 tickers × 40 = 400 calls, but only 200 remaining → block."""
    allowed, reason = check_onboarding_budget(pending_count=10, remaining_calls=200)
    assert allowed is False
    assert "insufficient tier budget" in reason
    # Helpful: tells operator how many would fit.
    assert "--max 5" in reason


def test_blocks_when_cap_already_exhausted() -> None:
    """Zero remaining → block immediately, even for 1 ticker."""
    allowed, reason = check_onboarding_budget(pending_count=1, remaining_calls=0)
    assert allowed is False
    assert "tier cap exhausted" in reason


def test_allows_run_at_unlimited_tier() -> None:
    """Starter/premium tier passes sys.maxsize as remaining_calls — always
    permitted."""
    allowed, _ = check_onboarding_budget(pending_count=100, remaining_calls=10**9)
    assert allowed is True


def test_estimate_constant_is_sane() -> None:
    """The default per-ticker call estimate must be plausible for FMP onboard:
    ~8 statement endpoints × 2 periods + ~10 other endpoints ≈ 40."""
    assert 10 <= ESTIMATED_FMP_CALLS_PER_ONBOARD <= 200


def test_custom_calls_per_onboard_argument_honored() -> None:
    """Operator can override the per-onboard estimate (e.g. via CLI flag)."""
    allowed, reason = check_onboarding_budget(
        pending_count=5,
        remaining_calls=50,
        calls_per_onboard=20,  # 5 * 20 = 100 needed, 50 remaining → block
    )
    assert allowed is False
    assert "100 needed" in reason


def test_empty_pending_list_is_allowed() -> None:
    """Zero pending tickers — gate is a no-op."""
    allowed, _ = check_onboarding_budget(pending_count=0, remaining_calls=10)
    assert allowed is True
