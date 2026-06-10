"""Behaviour lock for the canonical formatting module (master build P0.1):
compact USD (consolidating the per-renderer `_fmt_compact_usd` helpers),
percent vs percentage-point deltas, calendar dates, and relative time.
"""

from __future__ import annotations

import pathlib
import sys
from datetime import UTC, datetime

_SRC = pathlib.Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from report.renderers.numfmt import (  # noqa: E402
    fmt_compact_usd,
    fmt_date,
    fmt_pct,
    fmt_pp,
    fmt_reltime,
)


def test_fmt_compact_usd_magnitude_tiers() -> None:
    assert fmt_compact_usd(135_000_000_000) == "135.0B"
    assert fmt_compact_usd(45_000_000) == "45M"
    assert fmt_compact_usd(678_000) == "678K"
    assert fmt_compact_usd(1_234) == "1K"  # >= 1e3 hits the K tier (existing behaviour)
    assert fmt_compact_usd(750) == "750"  # sub-1e3 integer fallback


def test_fmt_compact_usd_handles_sign_and_zero() -> None:
    assert fmt_compact_usd(0) == "0"
    assert fmt_compact_usd(-2_500_000_000) == "-2.5B"


def test_fmt_pct_levels_and_deltas() -> None:
    assert fmt_pct(12.34) == "12.3%"
    assert fmt_pct(-3.456, decimals=2) == "-3.46%"
    assert fmt_pct(0.82, signed=True) == "+0.8%"
    assert fmt_pct(-0.82, signed=True) == "-0.8%"


def test_fmt_pp_always_signed() -> None:
    assert fmt_pp(-1.5) == "-1.5pp"
    assert fmt_pp(2.04) == "+2.0pp"
    assert fmt_pp(0.0) == "+0.0pp"


def test_fmt_date_variants_and_tolerance() -> None:
    assert fmt_date("2026-08-13") == "Aug 13, 2026"
    assert fmt_date("2026-08-13", include_year=False) == "Aug 13"
    assert fmt_date("2026-08-13T14:30:00") == "Aug 13, 2026"  # datetime input
    assert fmt_date("not-a-date") == "not-a-date"  # unparseable passes through


def test_fmt_reltime_tiers_past_and_future() -> None:
    now = datetime(2026, 6, 10, 12, 0, 0, tzinfo=UTC)
    assert fmt_reltime("2026-06-10T11:59:30", now=now) == "just now"
    assert fmt_reltime("2026-06-10T11:15:00", now=now) == "45m ago"
    assert fmt_reltime("2026-06-10T06:00:00", now=now) == "6h ago"
    assert fmt_reltime("2026-06-07T12:00:00", now=now) == "3d ago"
    assert fmt_reltime("2026-03-01T00:00:00", now=now) == "3mo ago"
    assert fmt_reltime("2026-06-13T12:00:00", now=now) == "in 3d"
    # Aware input against a naive-UTC convention reference still works.
    assert fmt_reltime("2026-06-10T10:00:00+00:00", now=now) == "2h ago"
    assert fmt_reltime("garbage") == "garbage"
