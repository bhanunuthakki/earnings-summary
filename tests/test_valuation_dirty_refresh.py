"""The dirty-artifact seam must bypass valuation's native cache exactly."""

from __future__ import annotations

from pathlib import Path

import pytest

from compute.valuation_basis import ValuationBasisResult
from report.models import SectionStatus
from report.sections import valuation


def test_force_refresh_reaches_valuation_compute_cache_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "portfolio.db").write_bytes(b"")
    observed_refresh: list[bool] = []

    def _extract(
        ticker: str,
        repo_root: Path,
        db_conn: object,
        refresh: bool = False,
    ) -> ValuationBasisResult:
        del repo_root, db_conn
        observed_refresh.append(refresh)
        return ValuationBasisResult(ticker=ticker, skipped_reason="fixture")

    monkeypatch.setattr(valuation.compute_valuation, "extract_for_ticker", _extract)

    section = valuation.build(
        ticker="NU",
        repo_root=tmp_path,
        enable_llm=True,
        force_refresh=True,
    )

    assert observed_refresh == [True]
    assert section.status == SectionStatus.MISSING_DATA
