"""Tests for FMP analyst-estimates split-basis normalization + guard.

Covers `src/compute/split_normalization.py`:
- the per-share rescale that forces a spliced series onto ONE current basis,
- the `netIncomeAvg` repair of the corrupted historical aggregates,
- the split-discontinuity guard,
- quarantine when there is no income-statement authority,
- idempotence,
- a regression fixture built from the REAL BKNG discontinuity (pre-split
  historical actuals spliced with post-split forward consensus).
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from compute.split_normalization import (  # noqa: E402
    SPLIT_STEP_THRESHOLD,
    detect_split_discontinuity,
    normalize_estimates,
)

# ---------------------------------------------------------------------------
# Regression fixture: the real BKNG contamination, trimmed to the boundary.
#
# Estimates file splices TWO bases: pre-2024 rows on the OLD pre-split per-share
# basis (2019 epsAvg=101.53) and 2024+ rows split-adjusted (2024 epsAvg=7.31).
# The 2023 netIncomeAvg is separately corrupted ($118.7B vs a real ~$4.3B).
# The income statement is fully split-adjusted onto the CURRENT ~816M-share basis
# for every year, so it is the authority.
# ---------------------------------------------------------------------------

_BKNG_ESTIMATES: list[dict[str, object]] = [
    # forward consensus rows — already on the current (post-split) basis
    {
        "date": "2026-12-31",
        "symbol": "BKNG",
        "revenueAvg": 29_442_509_888,
        "netIncomeAvg": 8_177_473_800,
        "epsAvg": 10.45799,
        "epsHigh": 10.9,
        "epsLow": 10.0,
    },
    {
        "date": "2025-12-31",
        "symbol": "BKNG",
        "revenueAvg": 26_707_323_167,
        "netIncomeAvg": 7_433_536_338,
        "epsAvg": 9.0994,
        "epsHigh": 9.5,
        "epsLow": 8.7,
    },
    {
        "date": "2024-12-31",
        "symbol": "BKNG",
        "revenueAvg": 23_457_215_643,
        "netIncomeAvg": 5_956_437_992,
        "epsAvg": 7.30722,
        "epsHigh": 7.5,
        "epsLow": 7.1,
    },
    # historical actual rows — OLD pre-split per-share basis (~20x too large),
    # and 2023 netIncomeAvg grossly corrupted ($118.7B).
    {
        "date": "2023-12-31",
        "symbol": "BKNG",
        "revenueAvg": 22_399_000_000,
        "netIncomeAvg": 118_696_721_492,
        "epsAvg": 145.46822,
        "epsHigh": 149.44,
        "epsLow": 141.49,
    },
    {
        "date": "2022-12-31",
        "symbol": "BKNG",
        "revenueAvg": 16_940_842_287,
        "netIncomeAvg": 648_790_265,
        "epsAvg": 97.14105,
        "epsHigh": 99.0,
        "epsLow": 95.0,
    },
    {
        "date": "2019-12-31",
        "symbol": "BKNG",
        "revenueAvg": 15_011_816_409,
        "netIncomeAvg": 4_732_789_135,
        "epsAvg": 101.5258,
        "epsHigh": 104.0,
        "epsLow": 99.0,
    },
]

# Income statement is split-consistent on the current basis for every year.
_BKNG_INCOME: list[dict[str, object]] = [
    {"date": "2025-12-31", "symbol": "BKNG", "eps": 6.66, "weightedAverageShsOutDil": 815_975_000},
    {
        "date": "2024-12-31",
        "symbol": "BKNG",
        "eps": 6.9984,
        "weightedAverageShsOutDil": 851_600_000,
    },
    {
        "date": "2023-12-31",
        "symbol": "BKNG",
        "eps": 4.7468,
        "weightedAverageShsOutDil": 913_250_000,
    },
    {
        "date": "2022-12-31",
        "symbol": "BKNG",
        "eps": 3.068,
        "weightedAverageShsOutDil": 1_001_300_000,
    },
    {
        "date": "2019-12-31",
        "symbol": "BKNG",
        "eps": 4.5168,
        "weightedAverageShsOutDil": 1_087_725_000,
    },
]


def _by_year(rows: list[dict[str, object]]) -> dict[str, dict[str, object]]:
    return {str(r["date"])[:4]: r for r in rows}


# ---------------------------------------------------------------------------
# Regression: the BKNG splice is fully reconciled.
# ---------------------------------------------------------------------------


def test_bkng_regression_per_share_rescaled_onto_current_basis() -> None:
    res = normalize_estimates(_BKNG_ESTIMATES, _BKNG_INCOME)
    out = _by_year(res.rows)
    # historical rows now match the income-statement (current-basis) eps
    assert out["2019"]["epsAvg"] == pytest_approx(4.5168)
    assert out["2022"]["epsAvg"] == pytest_approx(3.068)
    assert out["2023"]["epsAvg"] == pytest_approx(4.7468)


def test_bkng_regression_forward_rows_untouched() -> None:
    res = normalize_estimates(_BKNG_ESTIMATES, _BKNG_INCOME)
    out = _by_year(res.rows)
    # forward consensus already on the current basis — must NOT be rescaled
    assert out["2024"]["epsAvg"] == pytest_approx(7.30722)
    assert out["2025"]["epsAvg"] == pytest_approx(9.0994)
    assert out["2026"]["epsAvg"] == pytest_approx(10.45799)
    assert out["2026"]["netIncomeAvg"] == pytest_approx(8_177_473_800)


def test_bkng_regression_corrupted_net_income_repaired() -> None:
    res = normalize_estimates(_BKNG_ESTIMATES, _BKNG_INCOME)
    out = _by_year(res.rows)
    repaired = float(out["2023"]["netIncomeAvg"])  # type: ignore[arg-type]
    # was $118.7B; repaired to eps(normalized) * current_shares ~ $3.9B, plausible
    assert repaired < 10_000_000_000
    assert repaired > 1_000_000_000
    # a clean aggregate (2022 = $649M) is left untouched
    assert out["2022"]["netIncomeAvg"] == pytest_approx(648_790_265)


def test_bkng_regression_high_low_rescaled_together() -> None:
    res = normalize_estimates(_BKNG_ESTIMATES, _BKNG_INCOME)
    out = _by_year(res.rows)
    row = out["2019"]
    # epsHigh/epsLow move onto the current basis by the SAME factor as epsAvg,
    # so the High/Low ordering and the ~22x scale-down are preserved.
    assert float(row["epsHigh"]) < 6.0  # type: ignore[arg-type]
    assert float(row["epsLow"]) < 6.0  # type: ignore[arg-type]
    assert float(row["epsHigh"]) > float(row["epsLow"])  # type: ignore[operator]


# ---------------------------------------------------------------------------
# Idempotence + non-mutation
# ---------------------------------------------------------------------------


def test_normalize_is_idempotent() -> None:
    once = normalize_estimates(_BKNG_ESTIMATES, _BKNG_INCOME)
    twice = normalize_estimates(once.rows, _BKNG_INCOME)
    assert twice.events == []
    # values identical across the second pass
    assert _by_year(once.rows)["2019"]["epsAvg"] == _by_year(twice.rows)["2019"]["epsAvg"]


def test_normalize_does_not_mutate_input() -> None:
    normalize_estimates(_BKNG_ESTIMATES, _BKNG_INCOME)
    # the module-level fixture row is unchanged (deep-copied internally)
    raw_2019 = _by_year(_BKNG_ESTIMATES)["2019"]
    assert raw_2019["epsAvg"] == pytest_approx(101.5258)


def test_clean_series_is_noop() -> None:
    """A series already on one basis produces no events and identical values."""
    clean_est = [
        {
            "date": "2026-12-31",
            "symbol": "X",
            "revenueAvg": 1_100,
            "netIncomeAvg": 110,
            "epsAvg": 1.1,
        },
        {
            "date": "2025-12-31",
            "symbol": "X",
            "revenueAvg": 1_000,
            "netIncomeAvg": 100,
            "epsAvg": 1.0,
        },
        {"date": "2024-12-31", "symbol": "X", "revenueAvg": 900, "netIncomeAvg": 90, "epsAvg": 0.9},
    ]
    clean_inc = [
        {"date": "2025-12-31", "symbol": "X", "eps": 1.0, "weightedAverageShsOutDil": 100},
        {"date": "2024-12-31", "symbol": "X", "eps": 0.9, "weightedAverageShsOutDil": 100},
    ]
    res = normalize_estimates(clean_est, clean_inc)
    assert res.events == []
    assert res.quarantined is False
    assert _by_year(res.rows)["2024"]["epsAvg"] == pytest_approx(0.9)


# ---------------------------------------------------------------------------
# Guard
# ---------------------------------------------------------------------------


def test_guard_flags_split_boundary() -> None:
    findings = detect_split_discontinuity(_BKNG_ESTIMATES)
    boundaries = {f.boundary for f in findings}
    # the split boundary (2023->2024, ~20x down) must be flagged
    assert "2023->2024" in boundaries


def test_guard_ignores_ordinary_growth() -> None:
    """A ~1.2x EPS growth year with growing revenue is NOT a split — not flagged."""
    ordinary = [
        {"date": "2025-12-31", "symbol": "X", "revenueAvg": 1_200, "epsAvg": 1.2},
        {"date": "2024-12-31", "symbol": "X", "revenueAvg": 1_000, "epsAvg": 1.0},
    ]
    assert detect_split_discontinuity(ordinary) == []


def test_guard_respects_revenue_continuity() -> None:
    """A large EPS jump RIDING a large revenue jump is a real inflection, not a
    split — the revenue-continuity gate must suppress it."""
    real_inflection = [
        {"date": "2025-12-31", "symbol": "X", "revenueAvg": 5_000, "epsAvg": 4.0},
        {"date": "2024-12-31", "symbol": "X", "revenueAvg": 1_000, "epsAvg": 1.0},
    ]
    # revenue 5x, eps 4x -> revenue not continuous -> not flagged as a split
    assert detect_split_discontinuity(real_inflection) == []


def test_guard_threshold_boundary() -> None:
    """A step exactly at the threshold with flat revenue IS a split candidate;
    just under it is not."""
    over = [
        {"date": "2025-12-31", "symbol": "X", "revenueAvg": 1_000, "epsAvg": SPLIT_STEP_THRESHOLD},
        {"date": "2024-12-31", "symbol": "X", "revenueAvg": 1_000, "epsAvg": 1.0},
    ]
    under = [
        {
            "date": "2025-12-31",
            "symbol": "X",
            "revenueAvg": 1_000,
            "epsAvg": SPLIT_STEP_THRESHOLD - 0.5,
        },
        {"date": "2024-12-31", "symbol": "X", "revenueAvg": 1_000, "epsAvg": 1.0},
    ]
    assert len(detect_split_discontinuity(over)) >= 1
    assert detect_split_discontinuity(under) == []


# ---------------------------------------------------------------------------
# Quarantine
# ---------------------------------------------------------------------------


def test_quarantine_when_no_income_authority() -> None:
    """A spliced series with NO income statement to reconcile against is
    quarantined rather than silently normalized on a guess."""
    res = normalize_estimates(_BKNG_ESTIMATES, income_rows=[])
    assert res.quarantined is True
    assert res.quarantine_reason is not None
    assert "no income-statement authority" in res.quarantine_reason


def test_clean_series_no_income_is_not_quarantined() -> None:
    """No authority + no discontinuity => pass through, not quarantine."""
    clean = [
        {"date": "2025-12-31", "symbol": "X", "revenueAvg": 1_000, "epsAvg": 1.0},
        {"date": "2024-12-31", "symbol": "X", "revenueAvg": 900, "epsAvg": 0.9},
    ]
    res = normalize_estimates(clean, income_rows=[])
    assert res.quarantined is False


# ---------------------------------------------------------------------------
# tiny local approx (avoid importing pytest.approx name at module top for clarity)
# ---------------------------------------------------------------------------


def pytest_approx(expected: float, rel: float = 1e-3) -> object:
    import pytest

    return pytest.approx(expected, rel=rel)
