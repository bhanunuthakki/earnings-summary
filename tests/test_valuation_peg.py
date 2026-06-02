# pyright: reportPrivateUsage=false
# (^ this suite exercises internal compute / render seams directly —
# _compute_peg, _compute_forward_eps_growth, _ltm_eps, _valuation_tab — the
# project convention for unit-testing private helpers; cf.
# tests/test_fmp_tier_ladder.py, tests/test_comment_processor_hardstop.py.)
"""PEG ratio on the §Valuation tab (NU report comment #12: "add PEG across the
board, except where it doesn't make sense").

Covers the compute-layer math (`_compute_forward_eps_growth` / `_compute_peg`),
the applicability gate (P/E (NTM) + positive forward EPS growth only), and the
renderer's self-skipping PEG row.

The analyst-estimate fixtures use far-future fiscal-year-end dates so they stay
strictly greater than ``date.today()`` regardless of when the suite runs —
``_compute_forward_eps_growth`` partitions on ``date.today()`` and preserves the
caller's newest-first ordering (matching ``_load_quarterly``).
"""

from __future__ import annotations

from io import StringIO

import pytest

from compute.valuation_basis import (
    _compute_forward_eps_growth,
    _compute_peg,
    _ltm_eps,
)
from report.models import SectionStatus, ValuationBasisSection
from report.renderers.workspace_html import _valuation_tab

# NU-shaped consensus (newest-first): closest forward FY (the P/E(NTM) basis)
# epsAvg 0.86938 → following FY 1.15352 ⇒ +32.68% forward EPS growth.
_NU_ESTIMATES: list[dict[str, object]] = [
    {"date": "2091-12-31", "epsAvg": 1.15352},  # future[-2] — next FY
    {"date": "2090-12-31", "epsAvg": 0.86938},  # future[-1] — closest forward (P/E basis)
    {"date": "2024-12-31", "epsAvg": 0.42449},  # past — filtered out
]


# ---------------------------------------------------------------------------
# Forward EPS growth
# ---------------------------------------------------------------------------


def test_forward_growth_forward_over_forward() -> None:
    # Primary path: future[-2].epsAvg vs future[-1].epsAvg.
    g = _compute_forward_eps_growth(_NU_ESTIMATES, None)
    assert g == pytest.approx(32.6829, rel=1e-4)


def test_forward_growth_fallback_to_ltm_when_single_forward_year() -> None:
    estimates: list[dict[str, object]] = [
        {"date": "2090-12-31", "epsAvg": 1.10},  # only one forward year
        {"date": "2024-12-31", "epsAvg": 0.50},  # past — filtered
    ]
    income_q: list[dict[str, object]] = [  # newest-first; 4 quarters ⇒ LTM EPS = 1.00
        {"date": "2026-03-31", "eps": 0.25},
        {"date": "2025-12-31", "eps": 0.25},
        {"date": "2025-09-30", "eps": 0.25},
        {"date": "2025-06-30", "eps": 0.25},
    ]
    g = _compute_forward_eps_growth(estimates, income_q)
    assert g == pytest.approx(10.0)  # (1.10 - 1.00) / 1.00 * 100


def test_forward_growth_none_when_single_forward_year_and_no_ltm() -> None:
    estimates: list[dict[str, object]] = [{"date": "2090-12-31", "epsAvg": 1.10}]
    assert _compute_forward_eps_growth(estimates, None) is None
    # Fewer than 4 quarters on file ⇒ no LTM ⇒ None.
    assert _compute_forward_eps_growth(estimates, [{"date": "2026-03-31", "eps": 0.25}]) is None


def test_forward_growth_none_on_nonpositive_base() -> None:
    estimates: list[dict[str, object]] = [
        {"date": "2091-12-31", "epsAvg": 0.50},
        {"date": "2090-12-31", "epsAvg": -0.10},  # loss-making base ⇒ growth undefined
    ]
    assert _compute_forward_eps_growth(estimates, None) is None


def test_forward_growth_none_when_no_forward_years() -> None:
    estimates: list[dict[str, object]] = [
        {"date": "2024-12-31", "epsAvg": 0.42},
        {"date": "2023-12-31", "epsAvg": 0.32},
    ]
    assert _compute_forward_eps_growth(estimates, None) is None


def test_forward_growth_none_on_missing_eps() -> None:
    estimates: list[dict[str, object]] = [
        {"date": "2091-12-31"},  # no epsAvg
        {"date": "2090-12-31", "epsAvg": 0.86938},
    ]
    assert _compute_forward_eps_growth(estimates, None) is None


def test_ltm_eps_requires_four_quarters() -> None:
    assert _ltm_eps(None) is None
    assert _ltm_eps([{"eps": 0.25}, {"eps": 0.25}]) is None  # only 2 quarters
    assert _ltm_eps([{"eps": 0.25}, {"eps": 0.25}, {"eps": 0.25}, {"eps": 0.25}]) == pytest.approx(
        1.0
    )
    # A None in any of the trailing 4 quarters disqualifies the sum.
    assert _ltm_eps([{"eps": 0.25}, {"eps": None}, {"eps": 0.25}, {"eps": 0.25}]) is None


# ---------------------------------------------------------------------------
# PEG applicability gate
# ---------------------------------------------------------------------------


def test_peg_applies_for_pe_ntm_with_positive_growth() -> None:
    peg, growth = _compute_peg("P/E (NTM)", 16.9, _NU_ESTIMATES, None)
    assert growth == pytest.approx(32.6829, rel=1e-4)
    assert peg == pytest.approx(16.9 / 32.6829, rel=1e-4)  # ≈ 0.517


@pytest.mark.parametrize(
    "multiple",
    ["P/B", "P/TBV", "EV/NTM EBITDA", "EV/NTM Revenue", "EV/LTM EBITDA", "P/FCF", "EV/FCF"],
)
def test_peg_skipped_for_non_earnings_multiples(multiple: str) -> None:
    # "Except where it doesn't make sense": book-value banks, EV/EBITDA, FCF, …
    assert _compute_peg(multiple, 2.5, _NU_ESTIMATES, None) == (None, None)


def test_peg_skipped_for_ltm_pe() -> None:
    # The gate is specifically the forward earnings multiple — a trailing P/E
    # paired with forward growth would be an apples-to-oranges PEG.
    assert _compute_peg("P/E (LTM)", 16.9, _NU_ESTIMATES, None) == (None, None)


def test_peg_skipped_on_negative_forward_growth() -> None:
    declining: list[dict[str, object]] = [
        {"date": "2091-12-31", "epsAvg": 0.70},  # next FY below the basis year
        {"date": "2090-12-31", "epsAvg": 1.00},
    ]
    assert _compute_peg("P/E (NTM)", 16.9, declining, None) == (None, None)


@pytest.mark.parametrize("current_value", [None, 0.0, -5.0])
def test_peg_skipped_on_nonpositive_pe(current_value: float | None) -> None:
    # Unprofitable name: the P/E(NTM) numerator is null / non-positive.
    assert _compute_peg("P/E (NTM)", current_value, _NU_ESTIMATES, None) == (None, None)


def test_peg_skipped_when_no_multiple() -> None:
    assert _compute_peg(None, 16.9, _NU_ESTIMATES, None) == (None, None)


# ---------------------------------------------------------------------------
# Renderer — the PEG row self-skips off peg_ratio
# ---------------------------------------------------------------------------


def _render(section: ValuationBasisSection) -> str:
    body = StringIO()
    _valuation_tab(body, section)
    return body.getvalue()


def test_renderer_shows_peg_row_when_populated() -> None:
    html = _render(
        ValuationBasisSection(
            status=SectionStatus.OK,
            multiple_name="P/E (NTM)",
            current_value=16.9,
            current_value_display="16.9x",
            peg_ratio=0.52,
            peg_growth_pct=32.7,
        )
    )
    assert "valuation-peg" in html
    assert "PEG (NTM)" in html
    assert "0.52" in html
    assert "32.7%" in html


def test_renderer_omits_peg_row_for_book_value_name() -> None:
    html = _render(
        ValuationBasisSection(
            status=SectionStatus.OK,
            multiple_name="P/B",
            current_value=2.5,
            current_value_display="2.5x",
            peg_ratio=None,
            peg_growth_pct=None,
        )
    )
    assert "valuation-peg" not in html
    assert "PEG" not in html
