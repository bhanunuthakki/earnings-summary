"""Pydantic models for FMP API response shapes.

Structured types for the JSON payloads stored in data/historical/fmp/. We model
only the fields we actually consume in compute/* extractors; FMP returns many
more, which we ignore via model_config.extra='ignore'.

Note: monetary values come from FMP as int (raw dollars). We accept int | float
to be permissive of edge cases. Per the data-provenance contract, currency is
required (reportedCurrency) and is validated on extraction.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class FmpIncomeStatementRecord(BaseModel):
    """One period record from FMP /api/v3/income-statement/{ticker}.

    Each record represents a single fiscal period (Q1/Q2/Q3/Q4/FY) for the
    ticker. The annual file has ~25-40 records (one per year of history); the
    quarterly file has up to 100 records.
    """

    model_config = ConfigDict(extra="ignore")

    date: str
    symbol: str
    reportedCurrency: str
    period: str
    fiscalYear: str | None = None
    revenue: int | float | None = None
    costOfRevenue: int | float | None = None
    grossProfit: int | float | None = None
    researchAndDevelopmentExpenses: int | float | None = None
    sellingGeneralAndAdministrativeExpenses: int | float | None = None
    operatingExpenses: int | float | None = None
    operatingIncome: int | float | None = None
    ebit: int | float | None = None
    ebitda: int | float | None = None
    netIncome: int | float | None = None
    incomeBeforeTax: int | float | None = None
    incomeTaxExpense: int | float | None = None
    interestIncome: int | float | None = None
    interestExpense: int | float | None = None
    depreciationAndAmortization: int | float | None = None
    eps: float | None = None
    epsDiluted: float | None = None
    weightedAverageShsOut: int | None = None
    weightedAverageShsOutDil: int | None = None
