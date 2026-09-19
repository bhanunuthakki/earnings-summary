from pathlib import Path

import pytest

from filings.fmp_sections import locate_annual_filing


def test_annual_filing_exact_year_does_not_scan_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "data" / "historical" / "fmp"
    directory.mkdir(parents=True)
    filing = directory / "BRK.B_form_10k_2025.json"
    filing.write_text("{}")

    def no_scan(_self: Path):
        raise AssertionError("exact-year lookup must not enumerate the directory")

    monkeypatch.setattr(Path, "iterdir", no_scan)
    assert locate_annual_filing(tmp_path, "BRK.B", 2025) == (filing, 2025)
    assert locate_annual_filing(tmp_path, "BRK.B", 2024) == (None, None)


def test_annual_filing_selects_latest_matching_ticker(tmp_path: Path) -> None:
    directory = tmp_path / "data" / "historical" / "fmp"
    directory.mkdir(parents=True)
    for name in (
        "BRK.B_form_10k_2023.json",
        "BRK.B_form_10k_2025.json",
        "BRKXB_form_10k_2026.json",
    ):
        (directory / name).write_text("{}")
    assert locate_annual_filing(tmp_path, "BRK.B", None) == (
        directory / "BRK.B_form_10k_2025.json",
        2025,
    )
    assert locate_annual_filing(tmp_path, "MISSING", None) == (None, None)
    assert locate_annual_filing(tmp_path, "../BRK.B", 2025) == (None, None)
