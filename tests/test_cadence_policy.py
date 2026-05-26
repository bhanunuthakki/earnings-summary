"""Tests for src/pipeline/cadence_policy.py — the cadence single-source-of-truth.

The audit flagged a 7d (refresh_dispatch) vs 14d (refresh_cache) split for
the same FMP statement endpoints. cadence_policy.py is the new shared module
that both callers read from so the values cannot drift apart again.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

# refresh_cache.py transitively imports save_fmp_data which exits on
# missing FMP_API_KEY at module load — fine for production but blocks
# pure-policy tests. Inject a placeholder so the imports succeed.
os.environ.setdefault("FMP_API_KEY", "test-placeholder")

from pipeline import cadence_policy  # noqa: E402


def test_statement_stale_days_is_canonical_constant() -> None:
    """Both refresh_dispatch and refresh_cache must derive their FMP statement
    cadence from this constant. Changing this value should change behavior in
    both places — the test enforces the import wiring."""
    assert cadence_policy.STATEMENT_STALE_DAYS == 14


def test_refresh_dispatch_default_uses_cadence_policy() -> None:
    """The interactive single-ticker dispatcher's `stale_fmp_days` default
    must match the policy constant. Before this PR it was 7 (inconsistent with
    the 14d refresh_cache enforces for the same FMP statement endpoints)."""
    sys.path.insert(0, str(PROJECT_ROOT / "execution"))
    import refresh_dispatch  # noqa: E402

    # The parameter default is bound at function-definition time, so we read
    # it via the function's signature.
    import inspect

    sig = inspect.signature(refresh_dispatch.build_plan)
    default = sig.parameters["stale_fmp_days"].default
    assert default == cadence_policy.STATEMENT_STALE_DAYS


def test_refresh_cache_class_cadence_reads_from_policy() -> None:
    """refresh_cache.py's _CLASS_CADENCE_MULT must derive from the policy
    module. The mapping is: for portfolio/watchlist (base 24h), the multiplier
    in days equals the freshness in days."""
    sys.path.insert(0, str(PROJECT_ROOT / "execution"))
    import refresh_cache  # noqa: E402

    mult = refresh_cache._CLASS_CADENCE_MULT  # type: ignore[attr-defined]
    assert mult["statement"] == float(cadence_policy.STATEMENT_STALE_DAYS)
    assert mult["segment"] == float(cadence_policy.STATEMENT_STALE_DAYS)
    assert mult["time_sensitive"] == float(cadence_policy.TIME_SENSITIVE_STALE_DAYS)
    assert mult["growth"] == float(cadence_policy.GROWTH_STALE_DAYS)
    assert mult["reference"] == float(cadence_policy.REFERENCE_STALE_DAYS)


def test_all_constants_are_positive_ints() -> None:
    """Cadence policy values are durations in days — must be positive ints."""
    for name in (
        "STATEMENT_STALE_DAYS",
        "TIME_SENSITIVE_STALE_DAYS",
        "GROWTH_STALE_DAYS",
        "REFERENCE_STALE_DAYS",
    ):
        v = getattr(cadence_policy, name)
        assert isinstance(v, int)
        assert v > 0
