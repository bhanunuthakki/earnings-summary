"""Tests for src/pipeline/segment_cache_audit.py — raw-cache reconciliation audit."""

from __future__ import annotations

import json
from pathlib import Path

from pipeline.segment_cache_audit import (
    audit_ticker_cache,
    segment_cache_present,
)


def _write(fmp: Path, name: str, body: object) -> None:
    fmp.mkdir(parents=True, exist_ok=True)
    (fmp / name).write_text(json.dumps(body), encoding="utf-8")


def _product(cloud: int) -> list[dict[str, object]]:
    return [
        {
            "symbol": "GOOG",
            "period": "Q4",
            "date": "2025-12-31",
            "data": {"Search": 60_000_000_000, "Cloud": cloud},
        }
    ]


def _income(revenue: int) -> list[dict[str, object]]:
    return [{"symbol": "GOOG", "period": "Q4", "date": "2025-12-31", "revenue": revenue}]


def test_flags_record_when_sum_exceeds_revenue(tmp_path: Path) -> None:
    """60B + 60B = 120B segments vs 77B revenue (1.56x) -> flagged."""
    _write(tmp_path, "GOOG_product_segments_quarterly.json", _product(60_000_000_000))
    _write(tmp_path, "GOOG_income_statement_quarterly.json", _income(77_000_000_000))
    flags = audit_ticker_cache(str(tmp_path), "GOOG")
    assert len(flags) == 1
    assert flags[0].period_end == "2025-12-31"
    assert flags[0].ratio > 1.10
    assert flags[0].file == "GOOG_product_segments_quarterly.json"


def test_clean_record_not_flagged(tmp_path: Path) -> None:
    """60B + 17B = 77B segments vs 77B revenue -> within tolerance, no flag."""
    _write(tmp_path, "GOOG_product_segments_quarterly.json", _product(17_000_000_000))
    _write(tmp_path, "GOOG_income_statement_quarterly.json", _income(77_000_000_000))
    assert audit_ticker_cache(str(tmp_path), "GOOG") == []


def test_no_flag_without_revenue_to_reconcile(tmp_path: Path) -> None:
    """Segment file present but no income-statement revenue -> can't disprove, no flag."""
    _write(tmp_path, "GOOG_product_segments_quarterly.json", _product(60_000_000_000))
    assert audit_ticker_cache(str(tmp_path), "GOOG") == []


def test_sum_below_revenue_never_flagged(tmp_path: Path) -> None:
    """Missing-bucket case (segments sum < revenue) is legitimate and accepted."""
    _write(tmp_path, "GOOG_product_segments_quarterly.json", _product(5_000_000_000))
    _write(tmp_path, "GOOG_income_statement_quarterly.json", _income(90_000_000_000))
    assert audit_ticker_cache(str(tmp_path), "GOOG") == []


def test_segment_cache_present(tmp_path: Path) -> None:
    assert segment_cache_present(str(tmp_path), "GOOG") is False
    _write(tmp_path, "GOOG_product_segments_quarterly.json", _product(1))
    assert segment_cache_present(str(tmp_path), "GOOG") is True


def test_case_insensitive_ticker(tmp_path: Path) -> None:
    """Lower-case ticker resolves the upper-cased cache filenames."""
    _write(tmp_path, "GOOG_product_segments_quarterly.json", _product(60_000_000_000))
    _write(tmp_path, "GOOG_income_statement_quarterly.json", _income(77_000_000_000))
    flags = audit_ticker_cache(str(tmp_path), "goog")
    assert len(flags) == 1
    assert flags[0].ticker == "GOOG"
