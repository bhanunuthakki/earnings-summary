"""Tests for the analyst-defined segment override (``src/dcf/analyst_segments.py``).

The parser/validator is the single decision point the builder calls to decide whether
to REPLACE the FMP-resolved product segments with an analyst's own base-revenue split.
It must accept a well-formed block, reject a malformed one LOUD (so the builder falls
back to FMP rather than half-applying), and split base revenue by ``base_pct``.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from dcf import analyst_segments as aseg  # noqa: E402

_GOOD = {
    "Core": {"base_pct": 0.95, "near_term_growth": 0.12, "terminal_growth": -0.02},
    "Base44": {"base_pct": 0.05, "near_term_growth": 0.40, "terminal_growth": 0.03},
}


def test_absent_block_is_invalid_but_not_an_error() -> None:
    res = aseg.parse_analyst_segments(None)
    assert res.valid is False
    assert res.reason is None  # absent, not malformed


def test_valid_block_parses_in_order() -> None:
    res = aseg.parse_analyst_segments(_GOOD)
    assert res.valid is True
    assert res.reason is None
    assert res.names == ["Core", "Base44"]  # JSON insertion order preserved
    assert res.near_growth() == {"Core": 0.12, "Base44": 0.40}
    assert res.terminal_growth() == {"Core": -0.02, "Base44": 0.03}


def test_base_revenue_splits_by_pct() -> None:
    res = aseg.parse_analyst_segments(_GOOD)
    split = res.base_revenue(2000.0)
    assert split["Core"] == pytest.approx(1900.0)
    assert split["Base44"] == pytest.approx(100.0)


def test_base_pct_must_sum_to_one() -> None:
    bad = {
        "A": {"base_pct": 0.5, "near_term_growth": 0.1, "terminal_growth": 0.02},
        "B": {"base_pct": 0.3, "near_term_growth": 0.1, "terminal_growth": 0.02},
    }  # sums to 0.8
    res = aseg.parse_analyst_segments(bad)
    assert res.valid is False
    assert res.reason is not None and "sums to" in res.reason


def test_tiny_residual_is_tolerated() -> None:
    """A few tenths of a percent unallocated (rounding) must NOT trip the guard."""
    near = {
        "A": {"base_pct": 0.60, "near_term_growth": 0.1, "terminal_growth": 0.02},
        "B": {"base_pct": 0.395, "near_term_growth": 0.1, "terminal_growth": 0.02},
    }  # sums to 0.995
    assert aseg.parse_analyst_segments(near).valid is True


def test_missing_growth_rejected() -> None:
    bad = {"A": {"base_pct": 1.0, "near_term_growth": 0.1}}  # no terminal_growth
    res = aseg.parse_analyst_segments(bad)
    assert res.valid is False
    assert res.reason is not None and "near_term_growth/terminal_growth" in res.reason


def test_out_of_range_base_pct_rejected() -> None:
    for pct in (0.0, -0.1, 1.5):
        bad = {"A": {"base_pct": pct, "near_term_growth": 0.1, "terminal_growth": 0.02}}
        res = aseg.parse_analyst_segments(bad)
        assert res.valid is False
        assert res.reason is not None and "base_pct" in res.reason


def test_non_numeric_base_pct_rejected() -> None:
    bad = {"A": {"base_pct": "big", "near_term_growth": 0.1, "terminal_growth": 0.02}}
    res = aseg.parse_analyst_segments(bad)
    assert res.valid is False
    assert res.reason is not None and "base_pct" in res.reason


def test_bool_base_pct_rejected() -> None:
    """A stray boolean must never coerce to 1.0."""
    bad = {"A": {"base_pct": True, "near_term_growth": 0.1, "terminal_growth": 0.02}}
    assert aseg.parse_analyst_segments(bad).valid is False


def test_empty_and_non_mapping_rejected() -> None:
    assert aseg.parse_analyst_segments({}).valid is False
    assert aseg.parse_analyst_segments([1, 2, 3]).valid is False
    assert aseg.parse_analyst_segments("core:0.95").valid is False
    assert aseg.parse_analyst_segments({"A": "not-an-object"}).valid is False
