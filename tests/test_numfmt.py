"""Behaviour lock for the canonical compact-USD formatter, which consolidates
the previously duplicated `_fmt_compact_usd` helpers in html.py and markdown.py.
"""

from __future__ import annotations

import pathlib
import sys

_SRC = pathlib.Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from report.renderers.numfmt import fmt_compact_usd  # noqa: E402


def test_fmt_compact_usd_magnitude_tiers() -> None:
    assert fmt_compact_usd(135_000_000_000) == "135.0B"
    assert fmt_compact_usd(45_000_000) == "45M"
    assert fmt_compact_usd(678_000) == "678K"
    assert fmt_compact_usd(1_234) == "1K"  # >= 1e3 hits the K tier (existing behaviour)
    assert fmt_compact_usd(750) == "750"  # sub-1e3 integer fallback


def test_fmt_compact_usd_handles_sign_and_zero() -> None:
    assert fmt_compact_usd(0) == "0"
    assert fmt_compact_usd(-2_500_000_000) == "-2.5B"
