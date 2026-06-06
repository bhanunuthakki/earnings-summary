"""L1 KPI-strip tile formatting (`select_kpi_strip` in `workspace_data`).

The strip picks up-to-4 tier-1 KPIs and pre-renders the big "latest value" and
its delta. The headline regression these tests guard: a raw currency level such
as ``364000000000.0`` (Amazon AWS RPO) must humanize to ``$364.0B`` — not render
as a bare twelve-digit float. Pure formatting, so pinned here without a DB.
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from report.models import KpiLedgerRow  # noqa: E402
from report.renderers.workspace_data import (  # noqa: E402
    KpiStripTile,
    format_ledger_value,
    select_kpi_strip,
)

_TWO_Q = ["2025-12-31", "2026-03-31"]
_TIMES = chr(0x00D7)  # ratio multiplier; chr() keeps the source ASCII (RUF001)


def _tile(name: str, unit: str | None, values: list[float]) -> KpiStripTile:
    """Build a single tier-1 ledger row and return its rendered strip tile."""
    history: list[tuple[str, float | None]] = [(p, v) for p, v in zip(_TWO_Q, values, strict=True)]
    tiles = select_kpi_strip([KpiLedgerRow(name=name, tier="tier_1", unit=unit, history=history)])
    assert len(tiles) == 1
    return tiles[0]


# --- the bug: a raw currency level renders as a bare float -----------------


def test_rpo_level_humanizes_to_compact_dollars() -> None:
    # AWS RPO: 244B -> 364B in raw dollars. No decisive unit, no percent/ratio/
    # count name hint -> 'money'. Used to render "364000000000.0".
    tile = _tile(
        "AWS Remaining Performance Obligations (RPO)",
        None,
        [244_000_000_000.0, 364_000_000_000.0],
    )
    assert tile.latest_display == "$364.0B"
    assert tile.delta_display == "↑ $120.0B"
    assert tile.delta_sign == "pos"
    # Parenthetical qualifier is still stripped from the display name.
    assert tile.name == "AWS Remaining Performance Obligations"


def test_explicit_usd_unit_routes_to_money() -> None:
    tile = _tile("Net revenue", "USD", [30_000_000_000.0, 28_500_000_000.0])
    assert tile.latest_display == "$28.5B"
    assert tile.delta_display == "↓ $1.5B"
    assert tile.delta_sign == "neg"


def test_money_via_name_fallback_when_unit_absent() -> None:
    # No unit and no percent/ratio/count hint -> currency level.
    tile = _tile("Free cash flow", None, [1_000_000_000.0, 1_200_000_000.0])
    assert tile.latest_display == "$1.2B"
    assert tile.delta_display == "↑ $200.0M"


# --- counts render without a "$" -------------------------------------------


def test_count_unit_drops_the_dollar_sign() -> None:
    tile = _tile("Total customers", "count", [100_000_000.0, 118_000_000.0])
    assert tile.latest_display == "118.0M"
    assert tile.delta_display == "↑ 18.0M"


def test_count_via_name_hint_when_unit_absent() -> None:
    tile = _tile("Active subscribers", None, [40_000_000.0, 41_500_000.0])
    assert tile.latest_display == "41.5M"
    assert tile.delta_display == "↑ 1.5M"


# --- percents / ratios / bps keep their suffixes ---------------------------


def test_percent_unit_reads_as_points_delta() -> None:
    tile = _tile("Operating margin", "%", [40.0, 42.0])
    assert tile.latest_display == "42%"
    assert tile.delta_display == "↑ 2.0pp"


def test_growth_rate_reads_as_percent_not_count() -> None:
    # "Customer growth rate" contains a count word ("customer") but the percent
    # hint must win — the ordering guard.
    tile = _tile("Customer growth rate", None, [20.0, 18.0])
    assert tile.latest_display == "18%"
    assert tile.delta_display == "↓ 2.0pp"


def test_ratio_unit_keeps_multiplier_and_bare_delta() -> None:
    tile = _tile("Coverage ratio", "ratio", [1.10, 1.23])
    assert tile.latest_display == f"1.23{_TIMES}"
    assert tile.delta_display == "↑ 0.13"


def test_bps_unit_keeps_bps_suffix() -> None:
    tile = _tile("Risk-adj NIM change", "bps", [120.0, 150.0])
    assert tile.latest_display == "150 bps"
    assert tile.delta_display == "↑ 30 bps"


# --- selection guards (unchanged behavior) ---------------------------------


def test_non_tier_1_rows_are_skipped() -> None:
    row = KpiLedgerRow(
        name="Revenue",
        tier="tier_2",
        unit="USD",
        history=[("2025-12-31", 1.0e9), ("2026-03-31", 1.1e9)],
    )
    assert select_kpi_strip([row]) == []


def test_rows_with_under_two_observations_are_skipped() -> None:
    row = KpiLedgerRow(
        name="Revenue",
        tier="tier_1",
        unit="USD",
        history=[("2026-03-31", 1.0e9), ("2025-12-31", None)],
    )
    assert select_kpi_strip([row]) == []


# --- §2 ledger value cell (format_ledger_value) ----------------------------
# The ledger has a separate Unit column, so money/count levels humanize (with
# a $ for money) while percent/ratio/bps keep a trimmed number (no double suffix).


def test_ledger_money_level_humanizes() -> None:
    # The RPO regression: raw 364e9 (unit resolves to None after the def-unit
    # fix) must not render as a bare 12-digit integer.
    assert (
        format_ledger_value(364_000_000_000.0, None, "AWS Remaining Performance Obligations")
        == "$364.0B"
    )
    assert format_ledger_value(28_500_000_000.0, "USD", "Net revenue") == "$28.5B"


def test_ledger_count_humanizes_without_dollar() -> None:
    assert format_ledger_value(118_000_000.0, "count", "Total customers") == "118.0M"


def test_ledger_percent_keeps_trimmed_number_no_suffix() -> None:
    # Unit column carries the "%"; value cell stays a bare trimmed number.
    assert format_ledger_value(42.30, "%", "Operating margin") == "42.3"


def test_ledger_ratio_and_bps_keep_trimmed_number() -> None:
    assert format_ledger_value(1.23, "ratio", "Coverage ratio") == "1.23"
    assert format_ledger_value(150.0, "bps", "Risk-adj NIM change") == "150"


def test_ledger_huge_low_base_percent_is_not_compacted() -> None:
    # A real (if low-base-distorted) percentage of 26.3M% must stay a percent
    # number, NOT get mis-humanized to "26.3M" — guards against a magnitude rule.
    assert format_ledger_value(26_300_000.0, "%", "Operating Cash Flow YoY Growth") == "26300000"
