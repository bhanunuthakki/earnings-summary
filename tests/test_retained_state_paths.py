"""Retained commands bind explicit/configured state without inferring it from junctions."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from execution import backfill_report_fiscal_periods as report_cli
from execution import refresh_dcf
from report.artifacts import ReportFiscalPeriodBackfillResult


@pytest.mark.parametrize("explicit_root", [False, True])
def test_report_backfill_uses_configured_root_only_when_no_root_is_requested(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, explicit_root: bool
) -> None:
    configured_root = tmp_path / "configured-state"
    requested_root = tmp_path / "requested-artifacts"
    monkeypatch.setenv("EARNINGS_SUMMARY_DB_PATH", str(configured_root / "data" / "portfolio.db"))
    observed: list[Path] = []

    def capture(
        root: Path, *, tickers: set[str] | None, apply: bool
    ) -> ReportFiscalPeriodBackfillResult:
        observed.append(root)
        return ReportFiscalPeriodBackfillResult(
            apply=apply,
            candidates=0,
            eligible=0,
            applied=0,
            skipped_existing=0,
            unresolved=0,
            failed=0,
            items=(),
        )

    monkeypatch.setattr(report_cli, "backfill_report_fiscal_periods", capture)
    argv = ["backfill_report_fiscal_periods.py"]
    if explicit_root:
        argv.extend(["--repo-root", str(requested_root)])
    monkeypatch.setattr(sys, "argv", argv)
    assert report_cli.main() == 0
    assert observed == [(requested_root if explicit_root else configured_root).resolve()]


def test_report_backfill_without_explicit_or_configured_root_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("EARNINGS_SUMMARY_DB_PATH", raising=False)
    monkeypatch.setattr(sys, "argv", ["backfill_report_fiscal_periods.py"])
    with pytest.raises(ValueError, match="EARNINGS_SUMMARY_DB_PATH is required"):
        report_cli.main()


def test_dcf_refresh_binds_work_and_source_ledger_to_configured_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "code"
    database = tmp_path / "state" / "portfolio.db"
    database.parent.mkdir()
    database.touch()
    monkeypatch.setenv("EARNINGS_SUMMARY_DB_PATH", str(database))
    ledger_paths: list[Path] = []
    refresh_paths: list[Path] = []
    monkeypatch.setattr(refresh_dcf.source_calls_registry, "set_db_path", ledger_paths.append)

    def capture(
        ticker: str,
        repo_root: Path,
        db_path: Path,
        *,
        workbook_override: Path | None,
        valuation_year: int,
    ) -> dict[str, object]:
        assert ticker == "TEST"
        assert repo_root == root.resolve()
        refresh_paths.append(db_path)
        return {"ticker": ticker, "status": "synthetic"}

    monkeypatch.setattr(refresh_dcf, "refresh_one", capture)
    monkeypatch.setattr(
        sys, "argv", ["refresh_dcf.py", "--ticker", "TEST", "--repo-root", str(root)]
    )
    assert refresh_dcf.main() == 0
    assert refresh_paths == ledger_paths == [database.resolve()]
