"""Provider-neutral DCF command retains the old executable alias."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest


def _write_complete_assumption_inputs(root: Path) -> tuple[Path, Path]:
    fmp = root / "data" / "historical" / "fmp"
    fmp.mkdir(parents=True)
    income: list[dict[str, object]] = []
    cash_flow: list[dict[str, object]] = []
    for fiscal_year in (2024, 2025):
        for quarter in ("Q1", "Q2", "Q3", "Q4"):
            income.append(
                {
                    "fiscalYear": fiscal_year,
                    "period": quarter,
                    "revenue": 1_000_000_000,
                    "operatingIncome": 200_000_000,
                    "netIncome": 150_000_000,
                    "reportedCurrency": "USD",
                }
            )
            cash_flow.append(
                {
                    "fiscalYear": fiscal_year,
                    "period": quarter,
                    "capitalExpenditure": -100_000_000,
                    "depreciationAndAmortization": 80_000_000,
                }
            )
    income_path = fmp / "TEST_income_statement_quarterly.json"
    cash_path = fmp / "TEST_cash_flow_quarterly.json"
    income_path.write_text(json.dumps(income), encoding="utf-8")
    cash_path.write_text(json.dumps(cash_flow), encoding="utf-8")
    (fmp / "TEST_product_segments_quarterly.json").write_text("[]", encoding="utf-8")
    (fmp / "TEST_analyst_estimates_annual.json").write_text("[]", encoding="utf-8")
    (fmp / "TEST_profile.json").write_text("[]", encoding="utf-8")
    return income_path, cash_path


@pytest.mark.parametrize("script", ["refresh_dcf_assumptions.py", "dcf_opus_assumptions.py"])
def test_assumption_cli_skips_incomplete_history_without_paid_work(
    script: str, tmp_path: Path, migrated_db: Callable[[Path], Path]
) -> None:
    repository = Path(__file__).resolve().parents[1]
    database = migrated_db(tmp_path / "external.sqlite")
    state_root = tmp_path / "isolated-state"
    state_root.mkdir()
    result = subprocess.run(
        [sys.executable, str(repository / "execution" / script)],
        cwd=tmp_path,
        env={
            **os.environ,
            "DCF_TICKER": "TEST",
            "DCF_REPO_ROOT": str(state_root),
            "EARNINGS_SUMMARY_DB_PATH": str(database),
        },
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("SKIP\tTEST\tno complete fiscal year yet")
    assert not (state_root / "data" / "dcf_assumptions").exists()
    assert not (state_root / "data" / "portfolio.db").exists()


@pytest.mark.parametrize("invalid_kind", ["malformed_income", "missing_cash_quarter"])
def test_assumption_cli_rejects_invalid_required_actuals_without_cache_mutation(
    invalid_kind: str, tmp_path: Path, migrated_db: Callable[[Path], Path]
) -> None:
    repository = Path(__file__).resolve().parents[1]
    database = migrated_db(tmp_path / "external.sqlite")
    state_root = tmp_path / "isolated-state"
    income_path, cash_path = _write_complete_assumption_inputs(state_root)
    if invalid_kind == "malformed_income":
        income = json.loads(income_path.read_text(encoding="utf-8"))
        income[-1]["operatingIncome"] = "not-a-number"
        income_path.write_text(json.dumps(income), encoding="utf-8")
    else:
        cash_flow = json.loads(cash_path.read_text(encoding="utf-8"))
        cash_path.write_text(json.dumps(cash_flow[:-1]), encoding="utf-8")
    cache = state_root / "data" / "dcf_assumptions" / "TEST.json"
    cache.parent.mkdir(parents=True)
    sentinel = b'{"redesign":{"narrative":"keep"}}'
    cache.write_bytes(sentinel)

    result = subprocess.run(
        [sys.executable, str(repository / "execution" / "refresh_dcf_assumptions.py")],
        cwd=tmp_path,
        env={
            **os.environ,
            "DCF_TICKER": "TEST",
            "DCF_REPO_ROOT": str(state_root),
            "EARNINGS_SUMMARY_DB_PATH": str(database),
        },
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 2
    assert "required_actual_unavailable" in result.stderr
    assert cache.read_bytes() == sentinel
