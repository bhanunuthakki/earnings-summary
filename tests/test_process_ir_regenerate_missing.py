"""CLI validation for the registered transcript processor."""

from __future__ import annotations

from pathlib import Path

import pytest

from execution import process_ir_documents as processor


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["--ticker", "NU", "--all"],
        ["--ticker", "NU", "--regenerate-missing"],
    ],
)
def test_cli_requires_one_supported_selection_mode(argv: list[str]) -> None:
    with pytest.raises(SystemExit):
        processor.main(argv)


@pytest.mark.parametrize(
    ("ticker", "all_tickers", "year", "quarter", "message"),
    [
        ("NU", False, 2027, None, "--year and --quarter must be supplied together"),
        ("NU", False, None, 2, "--year and --quarter must be supplied together"),
        ("NU", False, 1800, 2, "--year must be between 1900 and 2200"),
        ("NU", False, 2027, 5, "--quarter must be 1, 2, 3, or 4"),
    ],
)
def test_selector_rejects_unproved_or_invalid_coordinates_before_database_access(
    tmp_path: Path,
    ticker: str | None,
    all_tickers: bool,
    year: int | None,
    quarter: int | None,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        processor.select_registered_transcripts(
            tmp_path / "no-database.sqlite",
            ticker=ticker,
            all_tickers=all_tickers,
            fiscal_year=year,
            fiscal_quarter=quarter,
        )
