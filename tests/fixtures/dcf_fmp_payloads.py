"""Reusable FMP payload fixtures for the redesigned-DCF chain.

``tests/test_dcf_redesign.py`` owns the reader/refresh layers; the cross-process
writer-gate evidence tests drive the REAL ``refresh_dcf`` and
``daily_fetch_and_brief`` subprocesses against a disposable migrated database.
Both need the same minimal FMP shape (4 full fiscal years of growing
quarterlies + a profile + forward estimates) and the same canonical statement
facts, so the payload builders live here once.

The schema template (a fresh migrated database) is passed explicitly: callers
own how they obtain it (the ``migrated_db`` fixture). The single-ticker writer
keeps a content-keyed seed cache; the multi-ticker writer seeds every ticker
into one database copy because the evidence chain runs several tickers through
ONE resolved database path.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from provenance.population_canonical_resolution import (
    CanonicalResolutionPopulationRequest,
    populate_canonical_resolution,
)
from provenance.population_metric_ontology import (
    MetricOntologyPopulationRequest,
    populate_metric_ontology,
)
from tests.fixtures.dcf_statements import STAMP, seed_dcf_statements


@dataclass(slots=True)
class FmpPayload:
    """One ticker's minimal FMP shape: quarterly statements + forward estimates."""

    income: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    balance: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    cashflow: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    segment_rows: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    estimates: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    terminal_revenue: float = 0.0


def build_fmp_payload(ticker: str, *, currency: str = "USD", segments: bool = False) -> FmpPayload:
    """4 full fiscal years of growing quarterlies + forward estimates.

    Consensus sits comfortably ABOVE the last-FY actual (~1.6bn) and grows, the
    realistic shape — so the model's seeded near-term growth is a smooth
    continuation and the value-of-record tracks the builder's mirror tightly.
    """
    payload = FmpPayload()
    rev = 250.0
    for year in (2022, 2023, 2024, 2025):
        for q in ("Q1", "Q2", "Q3", "Q4"):
            rev *= 1.03
            payload.income.append(
                {
                    "fiscalYear": year,
                    "period": q,
                    "reportedCurrency": currency,
                    "date": f"{year}-03-31",
                    "revenue": rev * 1e6,
                    "costOfRevenue": rev * 0.50 * 1e6,
                    "grossProfit": rev * 0.50 * 1e6,
                    "researchAndDevelopmentExpenses": rev * 0.12 * 1e6,
                    "sellingGeneralAndAdministrativeExpenses": rev * 0.15 * 1e6,
                    "operatingExpenses": rev * 0.40 * 1e6,
                    "operatingIncome": rev * 0.12 * 1e6,
                    "netIncome": rev * 0.09 * 1e6,
                    "weightedAverageShsOutDil": 100 * 1e6,
                }
            )
            payload.balance.append(
                {
                    "fiscalYear": year,
                    "period": q,
                    "reportedCurrency": currency,
                    "date": f"{year}-03-31",
                    "cashAndShortTermInvestments": rev * 0.30 * 1e6,
                    "totalCurrentAssets": rev * 0.60 * 1e6,
                    "propertyPlantEquipmentNet": rev * 0.50 * 1e6,
                    "totalAssets": rev * 1.50 * 1e6,
                    "totalCurrentLiabilities": rev * 0.30 * 1e6,
                    "longTermDebt": rev * 0.20 * 1e6,
                    "totalDebt": rev * 0.20 * 1e6,
                    "financeLeaseLiability": 0,
                    "totalStockholdersEquity": rev * 0.80 * 1e6,
                }
            )
            payload.cashflow.append(
                {
                    "fiscalYear": year,
                    "period": q,
                    "depreciationAndAmortization": rev * 0.08 * 1e6,
                    "stockBasedCompensation": rev * 0.05 * 1e6,
                    "changeInWorkingCapital": -rev * 0.01 * 1e6,
                    "operatingCashFlow": rev * 0.15 * 1e6,
                    "capitalExpenditure": -rev * 0.10 * 1e6,
                    "freeCashFlow": rev * 0.05 * 1e6,
                }
            )
            if segments:
                payload.segment_rows.append(
                    {
                        "fiscalYear": year,
                        "period": q,
                        "data": {"Cloud": rev * 0.6 * 1e6, "Devices": rev * 0.4 * 1e6},
                    }
                )
    payload.estimates.extend(
        {
            "date": f"{y}-12-31",
            "revenueAvg": 1750 * (1.08 ** (y - 2026)) * 1e6,
            "netIncomeAvg": 210 * (1.08 ** (y - 2026)) * 1e6,
            "ebitdaAvg": 320 * (1.08 ** (y - 2026)) * 1e6,
            "ebitAvg": 260 * (1.08 ** (y - 2026)) * 1e6,
            "sgaExpenseAvg": 240 * 1e6,
            "epsAvg": 2.1 * (1.08 ** (y - 2026)),
        }
        for y in range(2026, 2031)
    )
    payload.terminal_revenue = rev
    return payload


def write_fmp_files(repo: Path, ticker: str, payload: FmpPayload) -> None:
    """Write one ticker's FMP JSON files (statements, profile, estimates)."""
    fmp = repo / "data" / "historical" / "fmp"
    fmp.mkdir(parents=True, exist_ok=True)
    (fmp / f"{ticker}_income_statement_quarterly.json").write_text(
        json.dumps(payload.income), encoding="utf-8"
    )
    (fmp / f"{ticker}_balance_sheet_quarterly.json").write_text(
        json.dumps(payload.balance), encoding="utf-8"
    )
    (fmp / f"{ticker}_cash_flow_quarterly.json").write_text(
        json.dumps(payload.cashflow), encoding="utf-8"
    )
    if payload.segment_rows:
        (fmp / f"{ticker}_product_segments_quarterly.json").write_text(
            json.dumps(payload.segment_rows), encoding="utf-8"
        )
    (fmp / f"{ticker}_profile.json").write_text(
        json.dumps(
            [{"companyName": f"{ticker} Co", "beta": 1.2, "price": 50.0, "currency": "USD"}]
        ),
        encoding="utf-8",
    )
    (fmp / f"{ticker}_geo_segments_annual.json").write_text(
        json.dumps(
            [
                {
                    "fiscalYear": 2025,
                    "period": "FY",
                    "data": {"United States": payload.terminal_revenue},
                }
            ]
        ),
        encoding="utf-8",
    )
    (fmp / f"{ticker}_analyst_estimates_annual.json").write_text(
        json.dumps(payload.estimates), encoding="utf-8"
    )


def write_canonical_statement_facts(
    schema_template: Path,
    repo: Path,
    ticker: str,
    income: list[dict[str, object]],
    balance: list[dict[str, object]],
    cashflow: list[dict[str, object]],
    *,
    currency: str,
    canonical_bindings: bool = True,
) -> None:
    """Seed the canonical statement facts into a copy of the migrated schema."""
    statements = {"income": income, "balance": balance, "cash_flow": cashflow}
    digest = hashlib.sha256(
        json.dumps([ticker, currency, canonical_bindings, statements], sort_keys=True).encode()
    ).hexdigest()
    cached = schema_template.parent / f"{digest}.sqlite"
    if not cached.exists():
        shutil.copyfile(schema_template, cached)
        with sqlite3.connect(cached) as conn:
            seed_dcf_statements(
                conn,
                ticker,
                {"income": income, "balance": balance, "cash_flow": cashflow},
                currency=currency,
                canonical_bindings=canonical_bindings,
            )
    shutil.copyfile(cached, repo / "data" / "portfolio.db")


def write_fmp(
    schema_template: Path,
    repo: Path,
    ticker: str,
    *,
    currency: str = "USD",
    segments: bool = False,
    canonical_bindings: bool = True,
) -> None:
    """Minimal FMP fixture: 4 full fiscal years of growing quarterlies + a profile
    + forward estimates. Optionally a two-line product-segment file."""
    payload = build_fmp_payload(ticker, currency=currency, segments=segments)
    write_fmp_files(repo, ticker, payload)
    write_canonical_statement_facts(
        schema_template,
        repo,
        ticker,
        payload.income,
        payload.balance,
        payload.cashflow,
        currency=currency,
        canonical_bindings=canonical_bindings,
    )


def write_fmp_multi(
    schema_template: Path,
    repo: Path,
    tickers: tuple[str, ...],
    *,
    currency: str = "USD",
    canonical_bindings: bool = True,
) -> None:
    """Every ticker's FMP files + ALL tickers' canonical facts in ONE database.

    The single-ticker writer replaces ``repo/data/portfolio.db`` per call; the
    real-chain evidence runs several tickers through one resolved database path,
    so this seeds each ticker's canonical facts into the same migrated copy:
    every per-ticker key is namespaced (document version, extraction runs,
    nodes, observations), the shared foundation graph is seeded once, and the
    metric-ontology / canonical-resolution population runs once over the whole
    multi-ticker population."""
    payloads = {ticker: build_fmp_payload(ticker, currency=currency) for ticker in tickers}
    for ticker, payload in payloads.items():
        write_fmp_files(repo, ticker, payload)
    shutil.copyfile(schema_template, repo / "data" / "portfolio.db")
    total_facts = 0
    with sqlite3.connect(repo / "data" / "portfolio.db") as conn:
        for index, (ticker, payload) in enumerate(payloads.items()):
            total_facts += seed_dcf_statements(
                conn,
                ticker,
                {
                    "income": payload.income,
                    "balance": payload.balance,
                    "cash_flow": payload.cashflow,
                },
                currency=currency,
                canonical_bindings=False,
                namespace=f"{ticker}-",
                document_id=index + 1,
                seed_foundation=index == 0,
            )
        if not canonical_bindings:
            conn.commit()
            return
        ontology = populate_metric_ontology(
            conn,
            MetricOntologyPopulationRequest(
                knowledge_cutoff=STAMP, operation_recorded_at=STAMP, apply=True
            ),
        )
        assert ontology.snapshot_id is not None
        result = populate_canonical_resolution(
            conn,
            CanonicalResolutionPopulationRequest(
                cutoff_at=STAMP, operation_recorded_at=STAMP, apply=True
            ),
        )
        assert result.resolved_cell_count == total_facts
        conn.commit()
