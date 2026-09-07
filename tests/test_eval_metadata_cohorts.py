"""Hermetic checks for the thickened metadata and intake golden cohorts."""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path
from typing import cast

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from evals.golden_classifiers import (  # noqa: E402
    GOLDEN_DIR,
    grade_intake_classifier_case,
    grade_transcript_metadata_case,
    load_intake_classifier_golden,
    load_transcript_metadata_golden,
)


def test_metadata_and_intake_cohorts_have_the_new_contract_cases() -> None:
    metadata = load_transcript_metadata_golden(GOLDEN_DIR / "transcript_metadata.json")
    intake = load_intake_classifier_golden(GOLDEN_DIR / "intake_classifier.json")

    assert len(metadata) >= 6
    assert {case.case_id for case in metadata} >= {
        "tm-005-non-calendar-fiscal-header",
        "tm-006-ambiguous-metadata",
    }
    assert len(intake) >= 6
    assert {case.case_id for case in intake} >= {
        "ic-005-text-overrides-wrong-hint",
        "ic-006-veeva-fiscal-year-end",
    }


def _metadata_result(value: str) -> Callable[[str], str]:
    return lambda _text: value


def test_non_calendar_metadata_case_is_really_scored() -> None:
    _assert_metadata_case("tm-005-non-calendar-fiscal-header", "VEEV_Q1_2026", "VEEV_Q4_2026")


def test_ambiguous_metadata_case_is_really_scored() -> None:
    _assert_metadata_case("tm-006-ambiguous-metadata", "UNKNOWN", "VEEV_Q1_2026")


def _assert_metadata_case(case_id: str, expected: str, wrong: str) -> None:
    case = next(
        case
        for case in load_transcript_metadata_golden(GOLDEN_DIR / "transcript_metadata.json")
        if case.case_id == case_id
    )
    assert case.expected == expected
    assert grade_transcript_metadata_case(case, fn=_metadata_result(expected)).passed
    failed = grade_transcript_metadata_case(case, fn=_metadata_result(wrong))
    assert not failed.passed and failed.score == 0.0


def test_text_overrides_wrong_hint_case_is_really_scored() -> None:
    _assert_intake_case(
        "ic-005-text-overrides-wrong-hint",
        "2025-06-30",
        wrong_ticker="NU",
    )


def test_veeva_fiscal_year_end_case_is_really_scored() -> None:
    _assert_intake_case("ic-006-veeva-fiscal-year-end", "2026-01-31", "2026-12-31")


def _assert_intake_case(
    case_id: str,
    expected_period: str,
    wrong_period: str | None = None,
    *,
    wrong_ticker: str | None = None,
) -> None:
    case = next(
        case
        for case in load_intake_classifier_golden(GOLDEN_DIR / "intake_classifier.json")
        if case.case_id == case_id
    )
    expected = case.expected
    assert isinstance(expected, dict)
    assert expected["period_end"] == expected_period
    expected_ticker = cast(str, expected.get("ticker"))
    expected_doc_type = cast(str, expected.get("doc_type"))
    assert isinstance(expected_ticker, str)
    assert isinstance(expected_doc_type, str)

    def result(period_end: str, ticker: str) -> dict[str, object]:
        return {
            "ticker": ticker,
            "doc_type": expected_doc_type,
            "period_end": period_end,
        }

    def classify(*_args: object) -> dict[str, object]:
        return result(expected_period, expected_ticker)

    assert grade_intake_classifier_case(case, fn=classify).passed

    def misclassify(*_args: object) -> dict[str, object]:
        return result(
            wrong_period if wrong_period is not None else expected_period,
            wrong_ticker if wrong_ticker is not None else expected_ticker,
        )

    failed = grade_intake_classifier_case(case, fn=misclassify)
    assert not failed.passed and failed.score == 2 / 3
