"""Deterministic local fixture inputs for the redesigned DCF builder."""

from __future__ import annotations

import contextlib
import io
import json
import logging
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path


@dataclass(frozen=True)
class DcfFixtureEvidence:
    """Schema evidence for the disposable DCF input database."""

    alembic_revision: str
    alembic_invocations: int
    migration_elapsed_seconds: float
    schema_object_count: int


def _upgrade_fixture_database(database: Path, migration_root: Path) -> DcfFixtureEvidence:
    """Build the fixture DB through the repository's real Alembic graph."""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    from alembic import command

    config = Config(str(migration_root / "alembic.ini"))
    config.set_main_option("script_location", str(migration_root / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database}")
    expected_head = ScriptDirectory.from_config(config).get_current_head()
    if expected_head is None:
        raise RuntimeError("fixture migration graph has no single Alembic head")
    started = time.perf_counter()
    logging.getLogger("alembic").setLevel(logging.CRITICAL)
    with contextlib.redirect_stderr(io.StringIO()):
        command.upgrade(config, "head")
    elapsed = max(0.000001, time.perf_counter() - started)
    with sqlite3.connect(database) as conn:
        revision_row = conn.execute("SELECT version_num FROM alembic_version").fetchone()
        if revision_row is None or str(revision_row[0]) != expected_head:
            raise RuntimeError(
                f"fixture database is not at Alembic head: expected={expected_head!r} "
                f"observed={revision_row!r}"
            )
        object_count = int(
            conn.execute(
                "SELECT COUNT(*) FROM sqlite_master "
                "WHERE type IN ('table', 'index', 'trigger', 'view')"
            ).fetchone()[0]
        )
    return DcfFixtureEvidence(
        alembic_revision=expected_head,
        alembic_invocations=1,
        migration_elapsed_seconds=elapsed,
        schema_object_count=object_count,
    )


def write_fixture(repo: Path, ticker: str, *, migration_root: Path) -> DcfFixtureEvidence:
    fmp = repo / "data" / "historical" / "fmp"
    fmp.mkdir(parents=True, exist_ok=True)
    inc: list[dict[str, object]] = []
    bal: list[dict[str, object]] = []
    cf: list[dict[str, object]] = []
    rev = 250.0
    for year in (2022, 2023, 2024, 2025):
        for q in ("Q1", "Q2", "Q3", "Q4"):
            rev *= 1.03
            inc.append(
                {
                    "fiscalYear": year,
                    "period": q,
                    "reportedCurrency": "USD",
                    "date": f"{year}-03-31",
                    "revenue": rev * 1e6,
                    "costOfRevenue": rev * 0.5e6,
                    "grossProfit": rev * 0.5e6,
                    "researchAndDevelopmentExpenses": rev * 0.12e6,
                    "sellingGeneralAndAdministrativeExpenses": rev * 0.15e6,
                    "operatingExpenses": rev * 0.4e6,
                    "operatingIncome": rev * 0.12e6,
                    "netIncome": rev * 0.09e6,
                    "weightedAverageShsOutDil": 100e6,
                }
            )
            bal.append(
                {
                    "fiscalYear": year,
                    "period": q,
                    "reportedCurrency": "USD",
                    "date": f"{year}-03-31",
                    "cashAndShortTermInvestments": rev * 0.3e6,
                    "totalCurrentAssets": rev * 0.6e6,
                    "propertyPlantEquipmentNet": rev * 0.5e6,
                    "totalAssets": rev * 1.5e6,
                    "totalCurrentLiabilities": rev * 0.3e6,
                    "longTermDebt": rev * 0.2e6,
                    "totalDebt": rev * 0.2e6,
                    "financeLeaseLiability": 0,
                    "totalStockholdersEquity": rev * 0.8e6,
                }
            )
            cf.append(
                {
                    "fiscalYear": year,
                    "period": q,
                    "depreciationAndAmortization": rev * 0.08e6,
                    "stockBasedCompensation": rev * 0.05e6,
                    "changeInWorkingCapital": -rev * 0.01e6,
                    "operatingCashFlow": rev * 0.15e6,
                    "capitalExpenditure": -rev * 0.1e6,
                    "freeCashFlow": rev * 0.05e6,
                }
            )
    for name, rows in (
        ("income_statement_quarterly", inc),
        ("balance_sheet_quarterly", bal),
        ("cash_flow_quarterly", cf),
    ):
        (fmp / f"{ticker}_{name}.json").write_text(json.dumps(rows), encoding="utf-8")
    (fmp / f"{ticker}_profile.json").write_text(
        json.dumps(
            [
                {
                    "companyName": "Test Co",
                    "sector": "Tech",
                    "beta": 1.2,
                    "price": 50.0,
                    "currency": "USD",
                }
            ]
        ),
        encoding="utf-8",
    )
    estimates = [
        {
            "date": f"{year}-12-31",
            "revenueAvg": 1100 * 1.1 ** (year - 2026) * 1e6,
            "netIncomeAvg": 120 * 1.1 ** (year - 2026) * 1e6,
            "ebitdaAvg": 200e6,
            "ebitAvg": 150e6,
            "sgaExpenseAvg": 160e6,
            "epsAvg": 1.2 * 1.1 ** (year - 2026),
        }
        for year in range(2026, 2031)
    ]
    (fmp / f"{ticker}_analyst_estimates_annual.json").write_text(
        json.dumps(estimates), encoding="utf-8"
    )
    database = repo / "data" / "portfolio.db"
    database.parent.mkdir(parents=True, exist_ok=True)
    evidence = _upgrade_fixture_database(database, migration_root)
    with sqlite3.connect(database) as conn:
        conn.row_factory = sqlite3.Row
        # Index the local source bytes through the production evidence bridge,
        # then seed the legacy fact table with parameterized SQL. This keeps
        # the DCF equity bridge on the same evidence-backed path as production.
        from pipeline.fmp_doc_index import index_fmp_files_for_ticker

        index_fmp_files_for_ticker(conn, ticker, repo)
        balance_doc = conn.execute(
            "SELECT id FROM documents WHERE file_path = ? ORDER BY id LIMIT 1",
            (f"data/historical/fmp/{ticker}_balance_sheet_quarterly.json",),
        ).fetchone()
        if balance_doc is None:
            raise RuntimeError("fixture balance-sheet document was not indexed")
        balance_doc_id = int(balance_doc[0])
        conn.execute(
            "UPDATE documents SET source_type = 'sec_xbrl', source_quality_tier = 'sec_official' "
            "WHERE id = ?",
            (balance_doc_id,),
        )
        latest = bal[-1]
        # Route fixture facts through the production admission/resolution
        # helper; this exercises the migrated canonical relation rather than
        # manufacturing rows that a live writer could not produce.
        from pipeline.restatement_detector import insert_with_restatement_detection

        period_end = datetime.fromisoformat(str(latest["date"]))
        for line_item, value in (
            ("cash_and_short_term_investments", latest["cashAndShortTermInvestments"]),
            ("total_debt", latest["totalDebt"]),
            ("finance_lease_liability", 0),
        ):
            insert_with_restatement_detection(
                conn,
                ticker=ticker,
                period_end=period_end,
                fiscal_period_type=str(latest["period"]),
                line_item=line_item,
                value=Decimal(str(value)),
                currency="USD",
                unit="actual",
                source_doc_id=balance_doc_id,
                extracted_by="fmp_fixture",
            )
        conn.execute(
            "INSERT INTO dcf_runs "
            "(ticker, valuation_date, horizon_years, revenue_growths_json, fcf_margin, "
            "wacc, terminal_growth, npv, live_price, run_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (ticker, "2026-01-15", 10, "[]", 0.05, 0.10, 0.03, 0.0, 50.0, "bha115-fixture"),
        )
        conn.commit()
    return evidence
