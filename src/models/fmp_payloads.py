"""Pydantic models for FMP API response shapes.

Structured types for the JSON payloads stored in data/historical/fmp/. We model
only the fields we actually consume in compute/* extractors; FMP returns many
more, which we ignore via model_config.extra='ignore'.

Note: monetary values come from FMP as int (raw dollars). We accept int | float
to be permissive of edge cases. Per the data-provenance contract, currency is
required (reportedCurrency) and is validated on extraction.

Field names mirror FMP's exact JSON keys (camelCase) — by design. Aliasing each
of the 100+ fields would add boilerplate without semantic value. ruff N815 is
suppressed for this file in pyproject.toml.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class FmpIncomeStatementRecord(BaseModel):
    """One period record from FMP /api/v3/income-statement/{ticker}."""

    model_config = ConfigDict(extra="ignore")

    date: str
    symbol: str
    reportedCurrency: str | None = None
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


class FmpBalanceSheetRecord(BaseModel):
    """One period record from FMP /api/v3/balance-sheet-statement/{ticker}."""

    model_config = ConfigDict(extra="ignore")

    date: str
    symbol: str
    reportedCurrency: str | None = None
    period: str
    fiscalYear: str | None = None
    cashAndCashEquivalents: int | float | None = None
    shortTermInvestments: int | float | None = None
    cashAndShortTermInvestments: int | float | None = None
    netReceivables: int | float | None = None
    accountsReceivables: int | float | None = None
    inventory: int | float | None = None
    otherCurrentAssets: int | float | None = None
    totalCurrentAssets: int | float | None = None
    propertyPlantEquipmentNet: int | float | None = None
    goodwill: int | float | None = None
    intangibleAssets: int | float | None = None
    goodwillAndIntangibleAssets: int | float | None = None
    longTermInvestments: int | float | None = None
    otherNonCurrentAssets: int | float | None = None
    totalNonCurrentAssets: int | float | None = None
    totalAssets: int | float | None = None
    accountPayables: int | float | None = None
    shortTermDebt: int | float | None = None
    deferredRevenue: int | float | None = None
    otherCurrentLiabilities: int | float | None = None
    totalCurrentLiabilities: int | float | None = None
    longTermDebt: int | float | None = None
    deferredRevenueNonCurrent: int | float | None = None
    otherNonCurrentLiabilities: int | float | None = None
    totalNonCurrentLiabilities: int | float | None = None
    totalLiabilities: int | float | None = None
    commonStock: int | float | None = None
    retainedEarnings: int | float | None = None
    additionalPaidInCapital: int | float | None = None
    totalStockholdersEquity: int | float | None = None
    totalEquity: int | float | None = None
    totalDebt: int | float | None = None
    netDebt: int | float | None = None


class FmpCashFlowRecord(BaseModel):
    """One period record from FMP /api/v3/cash-flow-statement/{ticker}."""

    model_config = ConfigDict(extra="ignore")

    date: str
    symbol: str
    reportedCurrency: str | None = None
    period: str
    fiscalYear: str | None = None
    netIncome: int | float | None = None
    depreciationAndAmortization: int | float | None = None
    deferredIncomeTax: int | float | None = None
    stockBasedCompensation: int | float | None = None
    changeInWorkingCapital: int | float | None = None
    otherNonCashItems: int | float | None = None
    netCashProvidedByOperatingActivities: int | float | None = None
    operatingCashFlow: int | float | None = None
    investmentsInPropertyPlantAndEquipment: int | float | None = None
    acquisitionsNet: int | float | None = None
    purchasesOfInvestments: int | float | None = None
    salesMaturitiesOfInvestments: int | float | None = None
    netCashProvidedByInvestingActivities: int | float | None = None
    netDebtIssuance: int | float | None = None
    commonStockRepurchased: int | float | None = None
    netDividendsPaid: int | float | None = None
    commonDividendsPaid: int | float | None = None
    netCashProvidedByFinancingActivities: int | float | None = None
    netChangeInCash: int | float | None = None
    cashAtEndOfPeriod: int | float | None = None
    capitalExpenditure: int | float | None = None
    freeCashFlow: int | float | None = None
    incomeTaxesPaid: int | float | None = None
    interestPaid: int | float | None = None


class FmpSegmentRecord(BaseModel):
    """One period record from FMP product or geographic segment endpoints.

    The `data` field maps segment_name -> revenue dollar value. Both product and
    geographic endpoints share this shape; the metric column in segment_facts
    differentiates them ('revenue_by_product' vs 'revenue_by_geography').
    """

    model_config = ConfigDict(extra="ignore")

    date: str
    symbol: str
    reportedCurrency: str | None = None
    period: str
    fiscalYear: int | str | None = None
    data: dict[str, int | float]
