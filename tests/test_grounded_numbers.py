"""Grounded-numbers registry + drift validator (src/synthesis/grounded_numbers.py).

LLM lenses can restate a DCF figure wrong — NU's 5-min-reread said "$55 / 0% MoS"
while dcf_runs holds ~$20.88 / 25%. check_numeric_drift flags the contradiction so
the report surfaces the figures of record. These tests pin the catch, the
no-false-positive behavior, and the canonical loader.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from synthesis.grounded_numbers import (  # noqa: E402
    GroundedNumbers,
    check_numeric_drift,
    load_grounded_numbers,
)

_NU = GroundedNumbers(npv_per_share=20.88, live_price=12.72, over_under_pct=-0.391, mos_bar=0.25)


def test_drift_catches_the_nu_5min_reread_hallucination() -> None:
    text = "Bottom line: the DCF fair value is $55 with 0% MoS, so the stock looks cheap."
    drifts = check_numeric_drift(text, _NU)
    assert len(drifts) == 2  # the $55 fair-value claim + the 0% MoS claim
    assert any("55" in d and "20.88" in d for d in drifts)
    assert any("0%" in d and "25%" in d for d in drifts)


def test_no_drift_when_prose_matches_figures_of_record() -> None:
    text = "DCF fair value ~$21/share at a 25% MoS bar; trades ~39% below."
    assert check_numeric_drift(text, _NU) == []


def test_no_false_positive_on_unrelated_dollar_amounts() -> None:
    # A dollar figure not tied to a fair-value / NPV / MoS keyword is never flagged.
    text = "Revenue rose to $4.98B and the company added $300M of cash this quarter."
    assert check_numeric_drift(text, _NU) == []


def test_dcf_line_formats_figures_of_record() -> None:
    line = _NU.dcf_line()
    assert "$20.88" in line
    assert "MoS bar 25%" in line
    assert "-39%" in line


def test_load_grounded_numbers_prefers_consolidated_run(tmp_path: Path) -> None:
    (tmp_path / "data").mkdir()
    conn = sqlite3.connect(str(tmp_path / "data" / "portfolio.db"))
    conn.executescript(
        """
        CREATE TABLE dcf_runs (
            ticker TEXT, segment_name TEXT, valuation_date TEXT,
            npv_per_share REAL, live_price REAL, over_under_pct REAL, mos_bar_used REAL
        );
        INSERT INTO dcf_runs VALUES ('NU', NULL, '2026-05-26', 20.88, 12.72, -0.391, 0.25);
        INSERT INTO dcf_runs VALUES ('NU', 'Brazil', '2026-05-26', 99.0, 12.72, 0.0, 0.25);
        """
    )
    conn.commit()
    conn.close()
    gn = load_grounded_numbers("NU", tmp_path)
    assert gn is not None
    assert gn.npv_per_share == 20.88  # consolidated row, not the Brazil segment's 99
    assert load_grounded_numbers("ZZ", tmp_path) is None
