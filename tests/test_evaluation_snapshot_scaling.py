"""The public evaluation builder preserves the original display-scale oracle."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Generator
from pathlib import Path
from typing import cast

import pytest

from report.sections import evaluation_snapshot
from tests import test_report_canonical_financials as canonical
from tests import test_source_fact_repository as foundation


@pytest.fixture
def database(tmp_path: Path, migrated_db: Callable[..., Path]) -> Generator[sqlite3.Connection]:
    factory = cast(
        Callable[..., Generator[sqlite3.Connection]], getattr(foundation.conn, "__wrapped__")
    )
    yield from factory(tmp_path, migrated_db)


def test_public_builder_preserves_scale_margin_and_cagr_oracle(
    database: sqlite3.Connection, tmp_path: Path
) -> None:
    rows: list[tuple[str, str, str, str, str, str]] = []
    for year, revenue, operating, fcf, eps in (
        (2022, 30_000, 600, 2_100, 1),
        (2023, 37_000, 962, 2_960, 2),
        (2024, 41_000, 2_542, 3_690, 3),
        (2025, 44_000, 3_124, 4_400, 4),
    ):
        rows.extend(
            (concept, f"{year}-01-01", f"{year}-12-31", "FY", str(value * 1_000_000), "USD")
            for concept, value in (
                ("revenue", revenue),
                ("operating_income", operating),
                ("free_cash_flow", fcf),
            )
        )
        rows.append(("eps_diluted", f"{year}-01-01", f"{year}-12-31", "FY", str(eps), "USD/share"))
    for quarter, start, end, revenue, operating, fcf in (
        ("Q1", "2025-01-01", "2025-03-31", 11_000, 700, 1_100),
        ("Q2", "2025-04-01", "2025-06-30", 11_250, 750, 1_125),
        ("Q3", "2025-07-01", "2025-09-30", 11_500, 800, 1_150),
        ("Q4", "2025-10-01", "2025-12-31", 11_750, 866.75, 1_175),
    ):
        rows.extend(
            (concept, start, end, quarter, str(value * 1_000_000), "USD")
            for concept, value in (
                ("revenue", revenue),
                ("operating_income", operating),
                ("free_cash_flow", fcf),
            )
        )
    canonical.seed_table(database, rows)

    result = evaluation_snapshot.build("SYNTH", tmp_path, conn=database, as_of=foundation.STAMP)

    revenue = next(row for row in result.rows if row.metric == "Revenue")
    operating = next(row for row in result.rows if row.metric == "Operating margin")
    eps = next(row for row in result.rows if row.metric == "EPS diluted")
    roe = next(row for row in result.rows if row.metric == "ROE")
    assert result.fiscal_years == [2023, 2024, 2025]
    assert (revenue.lfy, revenue.ttm) == (44_000.0, 45_500.0)
    assert revenue.cagr_3y == pytest.approx((44 / 30) ** (1 / 3) - 1)
    assert (operating.lfy_minus_2, operating.lfy_minus_1, operating.lfy) == pytest.approx(
        (2.6, 6.2, 7.1)
    )
    assert operating.ttm == pytest.approx(6.85)
    assert eps.ttm is None and roe.ttm is None
    assert result.canonical_financial_table is not None
    assert result.source_manifest["Revenue FY2025"] == (revenue.source_cell_ids[2],)
    assert len(result.source_manifest["Revenue TTM"]) == 4
    assert result.source_manifest["Revenue 3y CAGR"] == (
        revenue.source_cell_ids[3],
        revenue.source_cell_ids[2],
    )
    assert result.unavailable_reasons["EPS diluted TTM"] == (
        "per_share_ttm_requires_weighted_share_count",
    )
    assert result.unavailable_reasons["ROE TTM"] == ("canonical_equity_fact_unavailable",)
