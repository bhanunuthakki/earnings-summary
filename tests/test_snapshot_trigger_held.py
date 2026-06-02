"""Trigger status is holding-aware (`snapshot._trigger_status`).

A held position trading below its margin-of-safety bar is an ADD (accumulate the
discount), not INITIATE_CANDIDATE — that label is for *unowned* names. Regression
for NU's "Trigger status: INITIATE" on a name already in the book (comment #20).
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from report.sections.snapshot import (  # noqa: E402
    _trigger_status,  # pyright: ignore[reportPrivateUsage]  # testing an internal seam
)


def test_deep_discount_is_add_when_held_else_initiate() -> None:
    # 39% below fair value, MoS bar 25% → below the bar.
    assert _trigger_status(-0.39, 0.25, held=True) == "add"
    assert _trigger_status(-0.39, 0.25, held=False) == "initiate_candidate"


def test_overvalued_and_fair_are_unaffected_by_held_state() -> None:
    # Overvalued / fair rungs are the same whether or not the name is held.
    for held in (True, False):
        assert _trigger_status(0.30, 0.25, held=held) == "sell"
        assert _trigger_status(0.15, 0.25, held=held) == "trim"
        assert _trigger_status(-0.10, 0.25, held=held) == "hold"  # above -mos_bar
        assert _trigger_status(None, 0.25, held=held) == "unknown"
