"""Tests for the capture-run coverage log (src/pipeline/capture_coverage.py)."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import cast

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from pipeline.capture_coverage import (  # noqa: E402
    CaptureCoverageRecord,
    load_coverage,
    record_coverage,
    summarize,
)


def _rec(**kw: object) -> CaptureCoverageRecord:
    base: dict[str, object] = {
        "stage": "xbrl_capture",
        "ticker": "GOOG",
        "source": "10k",
        "seen": 1,
        "captured": 1,
    }
    base.update(kw)
    return CaptureCoverageRecord.model_validate(base)


def test_record_and_load_roundtrip(tmp_path: Path) -> None:
    record_coverage(
        tmp_path,
        _rec(
            fiscal_year=2024,
            seen=100,
            captured=80,
            dropped_validation=2,
            skipped={"rate_or_percent": 10},
        ),
    )
    loaded = load_coverage(tmp_path)
    assert len(loaded) == 1
    assert loaded[0].ticker == "GOOG"
    assert loaded[0].captured == 80
    assert loaded[0].skipped == {"rate_or_percent": 10}
    assert loaded[0].captured_at  # stamped on write


def test_record_appends_across_calls(tmp_path: Path) -> None:
    record_coverage(tmp_path, _rec(ticker="A", seen=1, captured=1))
    record_coverage(
        tmp_path, _rec(stage="enumerate_capture", ticker="B", source="ir", seen=2, captured=1)
    )
    loaded = load_coverage(tmp_path)
    assert {r.ticker for r in loaded} == {"A", "B"}


def test_load_tolerates_missing_and_corrupt(tmp_path: Path) -> None:
    assert load_coverage(tmp_path) == []  # no file yet
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "capture_coverage_log.json").write_text("{not json", encoding="utf-8")
    assert load_coverage(tmp_path) == []  # corrupt -> [] (never crashes a run)


def test_summarize_rolls_up_totals_and_skips() -> None:
    recs = [
        _rec(
            stage="xbrl_capture",
            ticker="A",
            seen=100,
            captured=80,
            dropped_validation=2,
            skipped={"rate_or_percent": 10, "share_or_count": 8},
        ),
        _rec(
            stage="enumerate_capture",
            ticker="A",
            source="ir",
            seen=20,
            captured=18,
            skipped={"unparseable_or_blank": 2},
        ),
    ]
    s = summarize(recs)
    assert s["runs"] == 2
    assert s["seen"] == 120
    assert s["captured"] == 98
    assert s["dropped_validation"] == 2
    assert s["capture_rate"] == round(98 / 120, 4)
    by_stage = cast("dict[str, dict[str, int]]", s["by_stage"])
    assert by_stage["xbrl_capture"]["captured"] == 80
    assert by_stage["enumerate_capture"]["seen"] == 20
    # Skip histogram is sorted most-frequent-first.
    skipped = cast("dict[str, int]", s["skipped"])
    assert list(skipped) == ["rate_or_percent", "share_or_count", "unparseable_or_blank"]


def test_summarize_empty_is_zero_rate() -> None:
    s = summarize([])
    assert s["seen"] == 0
    assert s["capture_rate"] == 0.0
