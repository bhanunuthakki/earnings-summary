"""Unit tests for table_extractors.xbrl_value_classify — the scale/rate/
count/equity guards extracted out of generic_xbrl_capture.py
(segment_quarterly_framework.md §2.2). generic_xbrl_capture's own test suite
already exercises these behaviorally end-to-end; these tests pin the shared
module's public contract directly since compute.segment_quarterly_10q now
also depends on it.
"""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from table_extractors.xbrl_value_classify import (  # noqa: E402
    build_name,
    classify_value,
    is_unit_ambiguous_section,
    semantic_section_title,
)


def test_classify_value_normal_monetary_cell() -> None:
    value, reason, confidence = classify_value("Total revenues", 1234, 1_000_000)
    assert value == Decimal("1234000000")
    assert reason == ""
    assert confidence == 1.0


def test_classify_value_rate_token_deferred() -> None:
    value, reason, _ = classify_value("Coupon Rate", 0.0338, 1_000_000)
    assert value is None
    assert reason == "rate_or_percent"


def test_classify_value_count_token_deferred() -> None:
    value, reason, _ = classify_value("Shares outstanding (in shares)", 4_609_988_545, 1_000)
    assert value is None
    assert reason == "share_or_count"


def test_classify_value_per_share_not_scaled() -> None:
    value, reason, confidence = classify_value("Net income per share, diluted", 1.42, 1_000_000)
    assert value == Decimal("1.42")
    assert reason == ""
    assert confidence == 1.0


def test_classify_value_subunit_deferred() -> None:
    value, reason, _ = classify_value("Common equity tier 1 capital ratio", 0.5, 1_000_000)
    assert value is None
    assert reason in ("subunit_magnitude", "rate_or_percent")


def test_classify_value_residual_risk_downweighted() -> None:
    value, reason, confidence = classify_value("Common equity tier 1 capital", 13.5, 1_000_000)
    assert value == Decimal("13500000")
    assert reason == ""
    assert confidence < 1.0


def test_is_unit_ambiguous_section_per_share() -> None:
    assert is_unit_ambiguous_section("Earnings Per Share (Details) - USD ($) $ / shares")


def test_is_unit_ambiguous_section_equity_rollforward() -> None:
    assert is_unit_ambiguous_section("Stockholders' Equity (Details)")


def test_is_unit_ambiguous_section_false_for_equity_method() -> None:
    # "equity method investments" is an ordinary $ table — must NOT be
    # deferred as the equity/share-rollforward family.
    assert not is_unit_ambiguous_section("Equity Method Investments (Details)")


def test_semantic_section_title_strips_unit_and_details_suffix() -> None:
    title = semantic_section_title(
        "Segment Reporting - Schedule of Segment Reporting Information (Details) - USD ($) $ in Millions"
    )
    assert title == "Segment Reporting - Schedule of Segment Reporting Information"


def test_build_name_qualifies_and_dedupes() -> None:
    name = build_name("Segment Reporting", ["AWS"], "Total revenues")
    assert name == "Segment Reporting — AWS — Total revenues"


def test_build_name_drops_taxonomy_boilerplate() -> None:
    name = build_name("Section", ["Foo [Line Items]"], "Total")
    assert name == "Section — Total"
