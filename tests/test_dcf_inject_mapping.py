"""Wave 5: the conservative KPI → DCF-driver mapping (dcf_inject_for_kpi).

Only a clean name + unit-safe value maps; unknown KPIs, ambiguous units, and
out-of-range values produce no affordance (better to omit the link than inject a
garbage assumption into the model).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from report.renderers.workspace_dcf import dcf_inject_for_kpi


def test_percent_kpi_converts_to_ratio() -> None:
    assert dcf_inject_for_kpi("Effective tax rate", 21.0, "%") == ("tax_rate", 0.21)
    assert dcf_inject_for_kpi("Cost of debt", 5.0, "%") == ("cost_of_debt", 0.05)


def test_ratio_unit_passes_through() -> None:
    assert dcf_inject_for_kpi("Tax rate", 0.21, "ratio") == ("tax_rate", 0.21)


def test_raw_value_fields_are_not_scaled() -> None:
    assert dcf_inject_for_kpi("Beta", 1.1, None) == ("beta", 1.1)


def test_unmapped_kpi_returns_none() -> None:
    assert dcf_inject_for_kpi("Net interest margin", 18.0, "%") is None
    assert dcf_inject_for_kpi("Take rate", 2.0, "%") is None


def test_ambiguous_unit_on_percent_field_is_skipped() -> None:
    # A ratio field whose unit isn't % / ratio and whose value can't be a ratio.
    assert dcf_inject_for_kpi("Tax rate", 21.0, "bps") is None


def test_out_of_range_is_skipped() -> None:
    assert dcf_inject_for_kpi("Tax rate", 9999.0, "%") is None  # 99.99 ratio > 1.5
    assert dcf_inject_for_kpi("Effective tax", -80.0, "%") is None  # -0.8 < -0.5


def test_none_value_is_skipped() -> None:
    assert dcf_inject_for_kpi("Tax rate", None, "%") is None
