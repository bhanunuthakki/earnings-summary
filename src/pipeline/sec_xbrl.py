"""SEC XBRL ingestion via the companyfacts API.

For each ticker in the curated CIK map, fetch the SEC's
`/api/xbrl/companyfacts/CIK{cik}.json` endpoint, persist the raw JSON to
`data/historical/sec/{TICKER}_companyfacts.json`, and parse a curated set of
GAAP/IFRS tags into `financial_facts`. Provenance: one `documents` row per
unique SEC accession number (each filing the fact came from), so financial
facts retain accession-level provenance even though they were extracted from
an aggregated companyfacts response.

Tag ladders
-----------
`TAG_LADDERS` maps each canonical `financial_facts.line_item` (the SAME names
the FMP extractors in src/compute/{income_statement,balance_sheet,cashflow}.py
write, so existing readers, source_disagreement validation, and the tier-aware
loader dedup work unchanged) to an ORDERED ladder of (namespace, xbrl_tag)
rungs. Companies tag the same concept differently (`Revenues` vs
`RevenueFromContractWithCustomerExcludingAssessedTax`); per logical period the
first rung that reported a value wins and lower rungs are skipped, so the pick
is deterministic. The winning tag is recorded in the FactLocator json_path.

Sign convention: FMP stores cash OUTFLOWS negative (capital_expenditure,
buybacks, dividends, acquisitions), while the GAAP/IFRS `Payments*` /
`Purchase*` elements are positive payment amounts — those ladders carry
sign=-1 to normalize onto the FMP convention (verified against prod FMP rows
2026-07-02).

Period resolution: duration facts use SEC's `fp` when it names a quarter,
falling back to a fiscal-year-end-relative month partition; 6M/9M YTD
aggregations are skipped. Instant (balance-sheet) facts use the same FYE
partition — the fiscal-year-end month is inferred per payload from the
company's annual filings, so non-December fiscal years (MU, VEEV, BHP) label
correctly. An FYE snapshot is dual-written as FY and Q4, mirroring FMP's
annual + quarterly endpoints.

This runs purely on public SEC infra. No API key needed; the User-Agent string
identifies the caller as required by SEC's policy.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Literal, NamedTuple, cast

import requests

from credibility.observations import FINANCIAL_FACTS, record_restatement_observation
from models.documents import (
    DocType,
    FetchStatus,
    SourceType,
    tier_for_source_type,
)
from models.facts import Currency, FiscalPeriodType, Unit
from models.validation import Severity, ValidationRule
from pipeline import locators
from pipeline.confidence import score_confidence
from pipeline.kpi_persistence import record_validation_issue
from pipeline.restatement_detector import (
    _table_has_column,
    insert_with_restatement_detection,
)

log = logging.getLogger(__name__)

# The units the SEC ladder legitimately persists: monetary/per_share values are
# ACTUAL, share counts are COUNT. Anything else reaching the persist point is a
# unit the pipeline can't faithfully represent — flag it (UNIT_MISMATCH) instead
# of silently stamping it ACTUAL. See the sanity guard in
# ``insert_facts_from_companyfacts``.
_SANE_SEC_UNITS: frozenset[str] = frozenset({Unit.ACTUAL.value, Unit.COUNT.value})

# Reverse-lookup CIK from `https://www.sec.gov/files/company_tickers.json`
# (full portfolio + evaluation + watchlist sweep verified 2026-07-02). Update
# by re-querying that endpoint when adding tickers. Legacy names no longer
# tracked (CNQ/TPL/VALE/WY) stay — a map entry is just a lookup; the fetch
# script scopes to the live tracked universe.
CIK_MAP: dict[str, str] = {
    "ABNB": "0001559720",
    "AMAT": "0000006951",
    "AMD": "0000002488",
    "AMZN": "0001018724",
    "ASML": "0000937966",
    "AVGO": "0001730168",
    "AWK": "0001410636",
    "BAM": "0001937926",
    "BEPC": "0001791863",
    "BHP": "0000811809",
    "BIPC": "0001788348",
    "BKNG": "0001075531",
    "BN": "0001001085",
    "BRK-B": "0001067983",
    "CDNS": "0000813672",
    # Not in company_tickers.json anymore (pending acquisition); the historical
    # CIK still serves full companyfacts.
    "CFLT": "0001699838",
    "CGEH": "0001009759",  # Capstone (SEC title: Capstone Energy Plus, Inc.)
    "CIEN": "0000936395",
    "CNQ": "0001017413",
    "COHR": "0000820318",
    "COST": "0000909832",
    "CRM": "0001108524",
    "CRWD": "0001535527",
    "CRWV": "0001769628",
    "DDOG": "0001561550",
    "DHR": "0000313616",
    "DLO": "0001846832",
    "ENB": "0000895728",
    "EPD": "0001061219",
    "ESTC": "0001707753",
    "FCX": "0000831259",
    "FIGR": "0002064124",
    "FNV": "0001456346",
    "FRVO": "0001853868",
    "FTNT": "0001262039",
    "GOOG": "0001652044",
    "GTLB": "0001653482",
    "HASI": "0001561894",
    "HBM": "0001322422",
    "HDB": "0001144967",
    "HEI": "0000046619",
    "IBN": "0001103838",
    "ISRG": "0001035267",
    "JPM": "0000019617",
    "KLAC": "0000319201",
    "KVYO": "0001835830",
    "LITE": "0001633978",
    "LLY": "0000059478",
    "LMND": "0001691421",
    "MA": "0001141391",
    "MDB": "0001441816",
    "MELI": "0001099590",
    "META": "0001326801",
    "MRVL": "0001835632",
    "MSFT": "0000789019",
    "MU": "0000723125",
    "NBIS": "0001513845",
    "NEE": "0000753308",
    "NET": "0001477333",
    "NOW": "0001373715",
    "NSP": "0001000753",
    "NTRA": "0001604821",
    "NU": "0001691493",
    "NVDA": "0001045810",
    "NVO": "0000353278",
    "NVS": "0001114448",
    "OKTA": "0001660134",
    "ORCL": "0001341439",
    "PANW": "0001327567",
    "RBRK": "0001943896",
    "RGEN": "0000730272",
    "RIO": "0000863064",
    "ROP": "0000882835",
    "SCCO": "0001001838",
    "SE": "0001703399",
    "SNOW": "0001640147",
    "SNPS": "0000883241",
    "SOFI": "0001818874",
    "STNE": "0001745431",
    "TDG": "0001260221",
    "TECH": "0000842023",
    "TECK": "0000886986",
    "TEM": "0001717115",
    "TMO": "0000097745",
    "TOL": "0000794170",
    "TPL": "0001811074",
    "TRP": "0001232384",
    "TSM": "0001046179",
    "TXN": "0000097476",
    "UBER": "0001543151",
    "V": "0001403161",
    "VALE": "0000917851",
    "VEEV": "0001393052",
    "WGS": "0001818331",
    "WIX": "0001576789",
    "WMB": "0000107263",
    "WPM": "0001323404",
    "WY": "0000106535",
    "XEL": "0000072903",
    "ZS": "0001713683",
}

# Tracked names with NO SEC registration — EDGAR can never cover them; FMP /
# yfinance remain their only statement sources (honest degradation, do not
# approximate via a different entity's CIK):
#   FLKR  — Franklin FTSE South Korea ETF (fund, no companyfacts)
#   IVN   — Ivanhoe Mines: TSX-listed, no US registration
#   NTDOY — Nintendo: unsponsored OTC ADR, no SEC filings
NO_SEC_FILERS: frozenset[str] = frozenset({"FLKR", "IVN", "NTDOY"})


# How a ladder's XBRL unit keys are parsed:
#   monetary  — plain currency codes ("USD", "DKK"); Unit.ACTUAL + currency.
#   per_share — "USD/shares"-style keys; Unit.ACTUAL + currency (FMP's eps rows).
#   shares    — the "shares" key; Unit.COUNT + NULL currency (FMP's share-count rows).
LadderKind = Literal["monetary", "per_share", "shares"]


@dataclass(frozen=True)
class LineItemLadder:
    """One canonical line_item's ordered XBRL tag ladder."""

    line_item: str
    kind: LadderKind
    # +1, or -1 to flip GAAP/IFRS positive-payment elements onto FMP's
    # outflow-negative storage convention.
    sign: int
    # Ordered (namespace, tag) rungs; per logical period the first rung
    # with data wins and lower rungs are skipped.
    rungs: tuple[tuple[str, str], ...]


def _ladder(
    line_item: str,
    *rungs: tuple[str, str],
    kind: LadderKind = "monetary",
    sign: int = 1,
) -> LineItemLadder:
    return LineItemLadder(line_item=line_item, kind=kind, sign=sign, rungs=rungs)


# Canonical line_item -> tag ladder. line_item names MUST match the FMP
# extractor specs (src/compute/*.py _LINE_ITEM_SPEC) verbatim. Curated against
# the cached companyfacts payloads for the tracked book (GAAP: AMZN/META/MU/
# VEEV/...; IFRS: NVO/TSM/NU/BHP/HDB). A company that doesn't report a concept
# (META gross profit, AMZN standard-tagged R&D) simply gets no SEC row — FMP
# keeps filling it; degradation is honest, never approximated.
TAG_LADDERS: tuple[LineItemLadder, ...] = (
    # ---- income statement -------------------------------------------------
    _ladder(
        "revenue",
        ("us-gaap", "Revenues"),
        ("us-gaap", "RevenueFromContractWithCustomerExcludingAssessedTax"),
        ("us-gaap", "RevenueFromContractWithCustomerIncludingAssessedTax"),
        ("us-gaap", "SalesRevenueNet"),
        ("ifrs-full", "Revenue"),
        ("ifrs-full", "RevenueFromContractsWithCustomers"),
    ),
    _ladder(
        "cost_of_revenue",
        ("us-gaap", "CostOfRevenue"),
        ("us-gaap", "CostOfGoodsAndServicesSold"),
        ("us-gaap", "CostOfGoodsSold"),
        ("ifrs-full", "CostOfSales"),
    ),
    _ladder(
        "gross_profit",
        ("us-gaap", "GrossProfit"),
        ("ifrs-full", "GrossProfit"),
    ),
    _ladder(
        "research_and_development",
        ("us-gaap", "ResearchAndDevelopmentExpense"),
        ("ifrs-full", "ResearchAndDevelopmentExpense"),
    ),
    _ladder(
        "sga",
        ("us-gaap", "SellingGeneralAndAdministrativeExpense"),
    ),
    _ladder(
        "operating_income",
        ("us-gaap", "OperatingIncomeLoss"),
        ("ifrs-full", "ProfitLossFromOperatingActivities"),
        ("ifrs-full", "OperatingProfitLoss"),
    ),
    _ladder(
        "depreciation_and_amortization",
        ("us-gaap", "DepreciationDepletionAndAmortization"),
        ("us-gaap", "DepreciationAndAmortization"),
        ("us-gaap", "DepreciationAmortizationAndAccretionNet"),
        ("ifrs-full", "DepreciationAndAmortisationExpense"),
    ),
    _ladder(
        "interest_expense",
        ("us-gaap", "InterestExpense"),
        ("us-gaap", "InterestExpenseNonoperating"),
        ("us-gaap", "InterestAndDebtExpense"),
    ),
    _ladder(
        "interest_income",
        ("us-gaap", "InvestmentIncomeInterest"),
    ),
    _ladder(
        "income_before_tax",
        (
            "us-gaap",
            "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
        ),
        (
            "us-gaap",
            "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments",
        ),
        ("ifrs-full", "ProfitLossBeforeTax"),
    ),
    _ladder(
        "income_tax_expense",
        ("us-gaap", "IncomeTaxExpenseBenefit"),
        ("ifrs-full", "IncomeTaxExpenseContinuingOperations"),
    ),
    _ladder(
        "net_income",
        ("us-gaap", "NetIncomeLoss"),
        # FMP's netIncome is parent-attributable; prefer the parent variant,
        # fall back to total ProfitLoss (identical when there's no NCI).
        ("ifrs-full", "ProfitLossAttributableToOwnersOfParent"),
        ("ifrs-full", "ProfitLoss"),
    ),
    _ladder(
        "eps",
        ("us-gaap", "EarningsPerShareBasic"),
        ("ifrs-full", "BasicEarningsLossPerShare"),
        kind="per_share",
    ),
    _ladder(
        "eps_diluted",
        ("us-gaap", "EarningsPerShareDiluted"),
        ("ifrs-full", "DilutedEarningsLossPerShare"),
        kind="per_share",
    ),
    _ladder(
        "weighted_avg_shares",
        ("us-gaap", "WeightedAverageNumberOfSharesOutstandingBasic"),
        ("ifrs-full", "WeightedAverageShares"),
        kind="shares",
    ),
    _ladder(
        "weighted_avg_shares_diluted",
        ("us-gaap", "WeightedAverageNumberOfDilutedSharesOutstanding"),
        ("ifrs-full", "AdjustedWeightedAverageShares"),
        kind="shares",
    ),
    # ---- balance sheet (instant facts) ------------------------------------
    _ladder(
        "cash_and_equivalents",
        ("us-gaap", "CashAndCashEquivalentsAtCarryingValue"),
        ("ifrs-full", "CashAndCashEquivalents"),
    ),
    _ladder(
        "short_term_investments",
        ("us-gaap", "ShortTermInvestments"),
        ("us-gaap", "MarketableSecuritiesCurrent"),
        ("us-gaap", "AvailableForSaleSecuritiesDebtSecuritiesCurrent"),
    ),
    _ladder(
        "net_receivables",
        ("us-gaap", "AccountsReceivableNetCurrent"),
        ("us-gaap", "ReceivablesNetCurrent"),
        ("ifrs-full", "TradeAndOtherCurrentReceivables"),
        ("ifrs-full", "CurrentTradeReceivables"),
    ),
    _ladder(
        "inventory",
        ("us-gaap", "InventoryNet"),
        ("ifrs-full", "Inventories"),
    ),
    _ladder(
        "total_current_assets",
        ("us-gaap", "AssetsCurrent"),
        ("ifrs-full", "CurrentAssets"),
    ),
    # property_plant_equipment_net is DELIBERATELY absent: FMP folds operating-
    # lease right-of-use assets into propertyPlantEquipmentNet (AMZN FY24:
    # GAAP tag 252.7B vs FMP 328.8B) and no single XBRL tag matches that
    # composite — a SEC row would silently shift lease-heavy names ~25%.
    _ladder(
        "goodwill",
        ("us-gaap", "Goodwill"),
        ("ifrs-full", "Goodwill"),
    ),
    _ladder(
        "intangible_assets",
        # NOT FiniteLivedIntangibleAssetsNet — that excludes indefinite-lived
        # intangibles, a subset of FMP's intangibleAssets concept.
        ("us-gaap", "IntangibleAssetsNetExcludingGoodwill"),
        ("ifrs-full", "IntangibleAssetsOtherThanGoodwill"),
    ),
    _ladder(
        "goodwill_and_intangible_assets",
        ("us-gaap", "IntangibleAssetsNetIncludingGoodwill"),
    ),
    _ladder(
        "total_assets",
        ("us-gaap", "Assets"),
        ("ifrs-full", "Assets"),
    ),
    _ladder(
        "accounts_payable",
        ("us-gaap", "AccountsPayableCurrent"),
        ("us-gaap", "AccountsPayableTradeCurrent"),
        ("ifrs-full", "TradeAndOtherCurrentPayables"),
    ),
    _ladder(
        "deferred_revenue",
        ("us-gaap", "ContractWithCustomerLiabilityCurrent"),
        ("us-gaap", "DeferredRevenueCurrent"),
    ),
    _ladder(
        "total_current_liabilities",
        ("us-gaap", "LiabilitiesCurrent"),
        ("ifrs-full", "CurrentLiabilities"),
    ),
    _ladder(
        "long_term_debt",
        ("us-gaap", "LongTermDebtNoncurrent"),
        ("us-gaap", "LongTermDebt"),
        ("ifrs-full", "LongtermBorrowings"),
    ),
    _ladder(
        "deferred_revenue_non_current",
        ("us-gaap", "ContractWithCustomerLiabilityNoncurrent"),
        ("us-gaap", "DeferredRevenueNoncurrent"),
    ),
    _ladder(
        "total_liabilities",
        ("us-gaap", "Liabilities"),
        ("ifrs-full", "Liabilities"),
    ),
    _ladder(
        "retained_earnings",
        ("us-gaap", "RetainedEarningsAccumulatedDeficit"),
        ("ifrs-full", "RetainedEarnings"),
    ),
    _ladder(
        "additional_paid_in_capital",
        ("us-gaap", "AdditionalPaidInCapital"),
        ("us-gaap", "AdditionalPaidInCapitalCommonStock"),
    ),
    _ladder(
        "total_stockholders_equity",
        ("us-gaap", "StockholdersEquity"),
        ("ifrs-full", "EquityAttributableToOwnersOfParent"),
    ),
    _ladder(
        "total_equity",
        # FMP's totalEquity INCLUDES noncontrolling interests; the parent-only
        # StockholdersEquity feeds total_stockholders_equity above.
        ("us-gaap", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"),
        ("ifrs-full", "Equity"),
    ),
    # ---- cash flow ---------------------------------------------------------
    _ladder(
        "net_cash_from_operating",
        ("us-gaap", "NetCashProvidedByUsedInOperatingActivities"),
        ("us-gaap", "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"),
        ("ifrs-full", "CashFlowsFromUsedInOperatingActivities"),
    ),
    _ladder(
        # FMP dual-writes CFO under both names; mirror it so either reader
        # sees the SEC value.
        "operating_cash_flow",
        ("us-gaap", "NetCashProvidedByUsedInOperatingActivities"),
        ("us-gaap", "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"),
        ("ifrs-full", "CashFlowsFromUsedInOperatingActivities"),
    ),
    _ladder(
        "net_cash_from_investing",
        ("us-gaap", "NetCashProvidedByUsedInInvestingActivities"),
        ("us-gaap", "NetCashProvidedByUsedInInvestingActivitiesContinuingOperations"),
        ("ifrs-full", "CashFlowsFromUsedInInvestingActivities"),
    ),
    _ladder(
        "net_cash_from_financing",
        ("us-gaap", "NetCashProvidedByUsedInFinancingActivities"),
        ("us-gaap", "NetCashProvidedByUsedInFinancingActivitiesContinuingOperations"),
        ("ifrs-full", "CashFlowsFromUsedInFinancingActivities"),
    ),
    _ladder(
        "capital_expenditure",
        ("us-gaap", "PaymentsToAcquirePropertyPlantAndEquipment"),
        ("us-gaap", "PaymentsToAcquireProductiveAssets"),
        ("ifrs-full", "PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities"),
        sign=-1,
    ),
    _ladder(
        # FMP dual-writes capex under both names; mirror it.
        "investments_in_ppe",
        ("us-gaap", "PaymentsToAcquirePropertyPlantAndEquipment"),
        ("us-gaap", "PaymentsToAcquireProductiveAssets"),
        ("ifrs-full", "PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities"),
        sign=-1,
    ),
    _ladder(
        "stock_based_compensation",
        ("us-gaap", "ShareBasedCompensation"),
        ("ifrs-full", "AdjustmentsForSharebasedPayments"),
    ),
    _ladder(
        "common_stock_repurchased",
        ("us-gaap", "PaymentsForRepurchaseOfCommonStock"),
        sign=-1,
    ),
    _ladder(
        "net_dividends_paid",
        ("us-gaap", "PaymentsOfDividends"),
        ("ifrs-full", "DividendsPaidClassifiedAsFinancingActivities"),
        sign=-1,
    ),
    _ladder(
        "common_dividends_paid",
        ("us-gaap", "PaymentsOfDividendsCommonStock"),
        sign=-1,
    ),
    _ladder(
        "acquisitions_net",
        ("us-gaap", "PaymentsToAcquireBusinessesNetOfCashAcquired"),
        sign=-1,
    ),
    _ladder(
        "income_taxes_paid",
        ("us-gaap", "IncomeTaxesPaid"),
        ("us-gaap", "IncomeTaxesPaidNet"),
        ("ifrs-full", "IncomeTaxesPaidClassifiedAsOperatingActivities"),
    ),
    _ladder(
        "interest_paid",
        ("us-gaap", "InterestPaid"),
        ("us-gaap", "InterestPaidNet"),
        ("ifrs-full", "InterestPaidClassifiedAsOperatingActivities"),
        ("ifrs-full", "InterestPaidClassifiedAsFinancingActivities"),
    ),
)


# Map SEC form -> our DocType for the per-accession documents row.
_FORM_TO_DOC_TYPE: dict[str, DocType] = {
    "10-K": DocType.SEC_10K,
    "10-K/A": DocType.SEC_10K,
    "10-Q": DocType.SEC_10Q,
    "10-Q/A": DocType.SEC_10Q,
    "20-F": DocType.SEC_20F,
    "20-F/A": DocType.SEC_20F,
    "40-F": DocType.SEC_40F,
    "40-F/A": DocType.SEC_40F,
    "8-K": DocType.SEC_8K,
    "8-K/A": DocType.SEC_8K,
    "6-K": DocType.SEC_6K,
    "6-K/A": DocType.SEC_6K,
}

# Annual forms whose fp="FY" entries anchor the fiscal-year-end month inference.
_ANNUAL_FORMS = frozenset({"10-K", "10-K/A", "20-F", "20-F/A", "40-F", "40-F/A"})


_HEADERS = {
    "User-Agent": "EarningsSummary/1.0 (research@example.com)",
    "Accept-Encoding": "gzip, deflate",
}

_CURRENCY_CODES = frozenset(c.value for c in Currency)


def fetch_companyfacts(cik: str, *, timeout: int = 30) -> dict[str, object]:
    """Hit /api/xbrl/companyfacts/CIK{cik}.json. CIK must be 10-digit zero-padded."""
    url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
    response = requests.get(url, headers=_HEADERS, timeout=timeout)
    response.raise_for_status()
    return response.json()


def write_companyfacts_to_disk(
    payload: dict[str, object], *, ticker: str, project_root: Path
) -> Path:
    """Persist companyfacts JSON; returns the absolute path."""
    out_dir = project_root / "data" / "historical" / "sec"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{ticker.upper()}_companyfacts.json"
    out_path.write_text(json.dumps(payload), encoding="utf-8")
    return out_path


@dataclass(frozen=True)
class _AccessionRecord:
    """One SEC filing's metadata extracted from companyfacts entries."""

    accession: str
    form: str
    filed: str
    fy: int | None
    fp: str | None


def _enumerate_accessions(payload: dict[str, object]) -> dict[str, _AccessionRecord]:
    """Walk every fact in companyfacts, collect unique accession numbers + their metadata."""
    out: dict[str, _AccessionRecord] = {}
    facts = payload.get("facts", {})
    if not isinstance(facts, dict):
        return out
    for namespace_facts in facts.values():
        if not isinstance(namespace_facts, dict):
            continue
        for tag_data in namespace_facts.values():
            if not isinstance(tag_data, dict):
                continue
            units = tag_data.get("units", {})
            if not isinstance(units, dict):
                continue
            for entries in units.values():
                if not isinstance(entries, list):
                    continue
                for entry in entries:
                    accn = entry.get("accn")
                    form = entry.get("form")
                    if not accn or not form:
                        continue
                    if accn in out:
                        continue
                    out[accn] = _AccessionRecord(
                        accession=accn,
                        form=form,
                        filed=str(entry.get("filed", "")),
                        fy=entry.get("fy"),
                        fp=entry.get("fp"),
                    )
    return out


def upsert_accession_documents(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    accessions: dict[str, _AccessionRecord],
    project_root: Path,
) -> dict[str, int]:
    """Insert one `documents` row per unique accession; returns accession -> documents.id map."""
    accession_to_doc_id: dict[str, int] = {}
    for accn, record in accessions.items():
        doc_type = _FORM_TO_DOC_TYPE.get(record.form)
        if doc_type is None:
            continue
        sha256 = hashlib.sha256(accn.encode("utf-8")).hexdigest()
        cur = conn.execute("SELECT id FROM documents WHERE sha256 = ? LIMIT 1", (sha256,))
        row = cur.fetchone()
        if row is not None:
            accession_to_doc_id[accn] = int(row["id"])
            continue
        rel_path = f"data/historical/sec/{ticker.upper()}_companyfacts.json#accn={accn}"
        source_url = (
            f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany"
            f"&CIK={ticker}&type={record.form}&dateb=&owner=include&count=40"
        )
        tier = tier_for_source_type(SourceType.SEC_XBRL).value
        common_args = (
            ticker.upper(),
            SourceType.SEC_XBRL.value,
            doc_type.value,
            rel_path,
            sha256,
            datetime.now(),
            FetchStatus.OK.value,
            source_url,
        )
        if _table_has_column(conn, "documents", "source_quality_tier"):
            cur = conn.execute(
                "INSERT INTO documents "
                "(ticker, source_type, doc_type, period_start, period_end, file_path, "
                " sha256, fetched_at, fetch_status, http_code, raw_bytes_size, source_url, "
                " parent_document_id, source_quality_tier) "
                "VALUES (?, ?, ?, NULL, NULL, ?, ?, ?, ?, NULL, 0, ?, NULL, ?)",
                (*common_args, tier),
            )
        else:
            cur = conn.execute(
                "INSERT INTO documents "
                "(ticker, source_type, doc_type, period_start, period_end, file_path, "
                " sha256, fetched_at, fetch_status, http_code, raw_bytes_size, source_url, "
                " parent_document_id) "
                "VALUES (?, ?, ?, NULL, NULL, ?, ?, ?, ?, NULL, 0, ?, NULL)",
                common_args,
            )
        accession_to_doc_id[accn] = int(cur.lastrowid) if cur.lastrowid is not None else 0
    conn.commit()
    return accession_to_doc_id


def _currency_of_unit_code(unit_code: str, kind: LadderKind) -> str | None:
    """Extract the currency a ladder-kind expects from an XBRL unit key.

    monetary:  "USD" -> "USD"; "shares"/"pure" -> None (skip).
    per_share: "USD/shares" -> "USD"; anything else -> None.
    shares:    exactly "shares" -> "" (sentinel: valid, currency-less).
    """
    if kind == "monetary":
        return unit_code if unit_code in _CURRENCY_CODES else None
    if kind == "per_share":
        left, sep, right = unit_code.partition("/")
        if sep and right == "shares" and left in _CURRENCY_CODES:
            return left
        return None
    return "" if unit_code == "shares" else None


def _period_span_months(start_date: str | None, end_date: str | None) -> int | None:
    """Approximate span in months from start->end ISO dates. None if either missing."""
    if not start_date or not end_date:
        return None
    start = datetime.fromisoformat(start_date)
    end = datetime.fromisoformat(end_date)
    delta_days = (end - start).days
    return round(delta_days / 30.4375)  # average month length


def _infer_fye_month(payload: dict[str, object]) -> int:
    """Infer the company's fiscal-year-end MONTH from its annual filings.

    Mode of the end-month across every fp="FY" entry filed on an annual form
    (10-K/20-F/40-F). 52/53-week calendars drift a few days but stay in the
    same month almost always; the offset partition below tolerates ±1 month
    anyway. Falls back to 12 (December, the overwhelming majority) when the
    payload has no annual entries yet (young IPOs with only 10-Qs).
    """
    months: Counter[int] = Counter()
    facts = payload.get("facts", {})
    if not isinstance(facts, dict):
        return 12
    for namespace_facts_raw in cast("dict[str, object]", facts).values():
        if not isinstance(namespace_facts_raw, dict):
            continue
        for tag_data_raw in cast("dict[str, object]", namespace_facts_raw).values():
            if not isinstance(tag_data_raw, dict):
                continue
            units = cast("dict[str, object]", tag_data_raw).get("units", {})
            if not isinstance(units, dict):
                continue
            for entries_raw in cast("dict[str, object]", units).values():
                if not isinstance(entries_raw, list):
                    continue
                for entry_raw in cast("list[object]", entries_raw):
                    if not isinstance(entry_raw, dict):
                        continue
                    entry = cast("dict[str, object]", entry_raw)
                    if entry.get("fp") != "FY":
                        continue
                    if entry.get("form") not in _ANNUAL_FORMS:
                        continue
                    end = entry.get("end")
                    if not end or len(str(end)) < 7:
                        continue
                    months[int(str(end)[5:7])] += 1
    if not months:
        return 12
    # most_common ties broken deterministically by month number
    best = max(months.items(), key=lambda kv: (kv[1], -kv[0]))
    return best[0]


# Offset partition: (fye_month - end_month) % 12 -> quarter position within
# the fiscal year. ±1-month tolerance absorbs 52/53-week calendar drift.
_OFFSET_TO_QUARTER: dict[int, FiscalPeriodType] = {
    8: FiscalPeriodType.Q1,
    9: FiscalPeriodType.Q1,
    10: FiscalPeriodType.Q1,
    5: FiscalPeriodType.Q2,
    6: FiscalPeriodType.Q2,
    7: FiscalPeriodType.Q2,
    2: FiscalPeriodType.Q3,
    3: FiscalPeriodType.Q3,
    4: FiscalPeriodType.Q3,
    11: FiscalPeriodType.Q4,
    0: FiscalPeriodType.Q4,
    1: FiscalPeriodType.Q4,
}


def _quarter_from_end_month(end_date: str, fye_month: int) -> FiscalPeriodType:
    offset = (fye_month - int(end_date[5:7])) % 12
    return _OFFSET_TO_QUARTER[offset]


def _resolve_fiscal_period_type(
    *,
    fp: str | None,
    start_date: str | None,
    end_date: str | None,
    fye_month: int = 12,
) -> FiscalPeriodType | None:
    """Resolve the SEC fact's period to FiscalPeriodType, or None if it's a
    YTD/cumulative value we cannot map cleanly (6M or 9M aggregations distinct
    from the standalone quarter at the same end_date).

    Duration facts (start+end):
      ~3-month span: standalone quarter — SEC's `fp` when it names Q1-Q4,
      else the FYE-relative month partition.
      ~12-month span: FY.
      ~6/9-month span: skipped (YTD aggregation).

    Instant facts (end only — balance sheet): resolved purely by the
    FYE-relative month partition; the entry's `fp` names the FILING's period,
    not the snapshot's (a 10-Q carries the prior FYE comparative), so it is
    deliberately ignored. A snapshot at the FYE month resolves to FY — the
    caller dual-writes it as Q4 too, mirroring FMP's annual + quarterly
    endpoints.
    """
    if start_date and end_date:
        span = _period_span_months(start_date, end_date)
        if span is None:
            return None
        if 2 <= span <= 4:
            if fp:
                try:
                    fpt = FiscalPeriodType(fp)
                    if fpt in (
                        FiscalPeriodType.Q1,
                        FiscalPeriodType.Q2,
                        FiscalPeriodType.Q3,
                        FiscalPeriodType.Q4,
                    ):
                        return fpt
                except ValueError:
                    pass
            return _quarter_from_end_month(end_date, fye_month)
        if 11 <= span <= 13:
            return FiscalPeriodType.FY
        # 6-month or 9-month YTD aggregations — skip
        return None
    # Balance-sheet items: only `end` present (point-in-time)
    if end_date:
        quarter = _quarter_from_end_month(end_date, fye_month)
        if quarter is FiscalPeriodType.Q4:
            return FiscalPeriodType.FY
        return quarter
    return None


@dataclass(frozen=True)
class IngestStats:
    accessions_inserted: int
    facts_inserted: int


def _modal_currency(units: dict[str, object], kind: LadderKind) -> str | None:
    """Pick ONE deterministic currency for a tag: the code carrying the most
    entries across the tag's unit keys (the local reporting currency has the
    full history; USD convenience translations are sparse). Ties break
    alphabetically. Returns None when no unit key parses for the kind.

    Keeps multi-currency filers (TSM tags TWD *and* USD) on a single currency
    consistent with the FMP series instead of letting payload order decide.
    """
    counts: Counter[str] = Counter()
    for unit_code, entries in units.items():
        currency = _currency_of_unit_code(unit_code, kind)
        if currency is None or not isinstance(entries, list):
            continue
        counts[currency] += len(cast("list[object]", entries))
    if not counts:
        return None
    best = min(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return best[0]


def _flag_non_actual_unit(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    ticker: str,
    line_item: str,
    period_end: str,
    unit_code: str,
    resolved_unit: str,
    source_doc_id: int,
) -> None:
    """Record a WARN ``UNIT_MISMATCH`` for a SEC fact whose resolved unit is
    neither ACTUAL nor COUNT, so it surfaces in the Provenance console instead of
    being persisted with a wrong unit. Best-effort — a missing validation_issues
    table (pre-0006 fixture) must never break ingest."""
    try:
        record_validation_issue(
            conn,
            run_id=run_id,
            source_doc_id=source_doc_id,
            ticker=ticker,
            severity=Severity.WARN,
            rule=ValidationRule.UNIT_MISMATCH,
            raw_value=(
                f"{line_item} @ {period_end[:10]}: SEC unit_code '{unit_code}' "
                f"resolved to non-standard unit '{resolved_unit}'"
            ),
            expected="unit in {actual, count}",
        )
    except sqlite3.Error:
        log.warning(
            {
                "event": "sec_xbrl_unit_guard_write_failed",
                "ticker": ticker,
                "line_item": line_item,
            }
        )


class _PendingFact(NamedTuple):
    """One resolved SEC fact ready to write, with the tiebreak key used to
    collapse multi-frame same-document collisions (see
    ``_same_doc_pick_key``)."""

    pick_key: tuple[str, int, Decimal]
    period_end: datetime
    fiscal_period_type: str
    value: Decimal
    currency: str | None
    unit: str
    source_doc_id: int
    locator_json: str | None


def _same_doc_pick_key(
    entry: Mapping[str, object], signed_value: Decimal
) -> tuple[str, int, Decimal]:
    """Deterministic, order-independent tiebreak for two companyfacts entries
    that collapse onto the SAME write 5-tuple (ticker, period_end,
    fiscal_period_type, line_item, source_doc_id).

    A single SEC accession can report the same fiscal-period-end under more than
    one duration context — e.g. LITE (Lumentum) net_income @ 2016-07-02 appears
    in one 10-K both as the company's own first fiscal year (``start``
    2015-08-02 → 21.0M) and as a longer recast period (``start`` 2015-06-28 →
    9.3M). Both resolve to ``FY @ 2016-07-02`` because the logical key carries no
    duration, so the extractor would otherwise write one then INSERT-OR-IGNORE
    the other, and ``_correct_same_document_fact`` would UPDATE-churn it on every
    ingest — a value that flipped with companyfacts iteration order.

    The winning entry (MAX pick_key) is the one with the latest ``start`` — the
    tightest period ending at that fiscal-year-end, i.e. the company's own
    reported figure rather than a recast super-annual span. Instant
    (balance-sheet) facts have no ``start`` (all sort equal on the first
    component) and repeat the same value across contexts, so this is a no-op for
    them — which is why the churn only ever surfaced on a flow line item.
    Remaining components (``frame`` presence, then the value itself) only break a
    genuine ``start`` tie, keeping the order a total one."""
    start_raw = entry.get("start")
    start_key = start_raw if isinstance(start_raw, str) else ""
    has_frame = 1 if entry.get("frame") else 0
    return (start_key, has_frame, signed_value)


def insert_facts_from_companyfacts(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    payload: dict[str, object],
    accession_to_doc_id: dict[str, int],
    run_id: str = "sec_xbrl_ingest",
) -> int:
    """Walk TAG_LADDERS; insert financial_facts, first rung winning per period.

    ``run_id`` tags any ``UNIT_MISMATCH`` validation_issues the unit sanity guard
    raises (a value whose resolved unit is neither ACTUAL nor COUNT); production
    passes the ingest run's id, ad-hoc callers get a stable default."""
    facts_raw = payload.get("facts", {})
    if not isinstance(facts_raw, dict):
        return 0
    facts_block = cast("dict[str, object]", facts_raw)
    fye_month = _infer_fye_month(payload)
    inserted = 0
    for ladder in TAG_LADDERS:
        # (period_end, fiscal_period_type) -> rung index that claimed it.
        # A key claimed by an earlier rung is invisible to later rungs, so the
        # pick is deterministic regardless of payload ordering.
        claimed: dict[tuple[str, str], int] = {}
        # (source_doc_id, period_end, fiscal_period_type) -> the single fact we
        # will write for that exact provenance 5-tuple. Multiple companyfacts
        # entries can collapse onto one 5-tuple (a SEC accession reporting the
        # same fiscal-period-end under two duration contexts); we keep only the
        # deterministic winner (`_same_doc_pick_key`) instead of writing both and
        # letting the second INSERT-OR-IGNORE trigger same-document correction
        # churn. Inserts are deferred to after the ladder walk.
        chosen: dict[tuple[int, str, str], _PendingFact] = {}
        for rung_idx, (namespace, tag_name) in enumerate(ladder.rungs):
            namespace_facts_raw = facts_block.get(namespace)
            if not isinstance(namespace_facts_raw, dict):
                continue
            tag_data_raw = cast("dict[str, object]", namespace_facts_raw).get(tag_name)
            if not isinstance(tag_data_raw, dict):
                continue
            units_raw = cast("dict[str, object]", tag_data_raw).get("units", {})
            if not isinstance(units_raw, dict):
                continue
            units = cast("dict[str, object]", units_raw)
            chosen_currency = _modal_currency(units, ladder.kind)
            if chosen_currency is None:
                continue
            for unit_code, entries_raw in units.items():
                if _currency_of_unit_code(unit_code, ladder.kind) != chosen_currency:
                    continue
                if not isinstance(entries_raw, list):
                    continue
                # Manual counter instead of enumerate(): the walk is legacy
                # untyped JSON and enumerate(<unknown>) trips the pyright
                # strict ratchet, while typing the soup would cascade further.
                entry_idx = -1
                for entry_raw in cast("list[object]", entries_raw):
                    entry_idx += 1
                    if not isinstance(entry_raw, dict):
                        continue
                    entry = cast("dict[str, object]", entry_raw)
                    accn_val = entry.get("accn")
                    if not accn_val:
                        continue
                    accn = str(accn_val)
                    if accn not in accession_to_doc_id:
                        continue
                    end_val = entry.get("end")
                    if not end_val:
                        continue
                    end = str(end_val)
                    val = entry.get("val")
                    if val is None:
                        continue
                    fp_val = entry.get("fp")
                    fp = str(fp_val) if isinstance(fp_val, str) else None
                    start_val = entry.get("start")
                    start = str(start_val) if isinstance(start_val, str) else None
                    fpt = _resolve_fiscal_period_type(
                        fp=fp, start_date=start, end_date=end, fye_month=fye_month
                    )
                    if fpt is None:
                        continue  # YTD/6M/9M aggregations skipped
                    # An FYE balance-sheet snapshot is both the FY and the Q4
                    # value — FMP writes it from both its annual and quarterly
                    # endpoints, so mirror the dual label.
                    is_instant = not start
                    fpts = (
                        (FiscalPeriodType.FY, FiscalPeriodType.Q4)
                        if is_instant and fpt is FiscalPeriodType.FY
                        else (fpt,)
                    )
                    period_end = datetime.fromisoformat(end)
                    # Exact position of the value in the companyfacts JSON the
                    # document row's file_path points at (data_provenance.md
                    # §7); records the winning tag for the ladder pick. Row =
                    # the XBRL tag, column = the fact's own end date — both
                    # already local at this call site.
                    locator = locators.table_cell_locator(
                        section=None,
                        table_title=namespace,
                        row_label=tag_name,
                        column_header=end,
                        json_path=f"facts.{namespace}.{tag_name}.units.{unit_code}[{entry_idx}]",
                        cell_value_as_extracted=str(val),
                    )
                    value = Decimal(str(val))
                    if ladder.sign < 0:
                        value = -value
                    # Unit sanity guard: the ladder kind fixes the persisted unit
                    # (shares → COUNT, everything else → ACTUAL). Should a future
                    # kind (or a bug) yield anything outside {ACTUAL, COUNT}, flag
                    # it into validation_issues and SKIP the row rather than
                    # silently stamping a wrong unit — an UNIT_MISMATCH the
                    # Provenance console then surfaces. Belt-and-suspenders behind
                    # the unit-code family filter above; today it never fires,
                    # which is the point of the guard.
                    resolved_unit = (
                        Unit.COUNT.value if ladder.kind == "shares" else Unit.ACTUAL.value
                    )
                    if resolved_unit not in _SANE_SEC_UNITS:
                        _flag_non_actual_unit(
                            conn,
                            run_id=run_id,
                            ticker=ticker,
                            line_item=ladder.line_item,
                            period_end=end,
                            unit_code=unit_code,
                            resolved_unit=resolved_unit,
                            source_doc_id=accession_to_doc_id[accn],
                        )
                        continue
                    for fpt_out in fpts:
                        key = (end, fpt_out.value)
                        claimed_by = claimed.setdefault(key, rung_idx)
                        if claimed_by != rung_idx:
                            continue  # an earlier rung already owns this period
                        source_doc_id = accession_to_doc_id[accn]
                        pick_key = _same_doc_pick_key(entry, value)
                        wkey = (source_doc_id, end, fpt_out.value)
                        prev = chosen.get(wkey)
                        if prev is not None and pick_key <= prev.pick_key:
                            continue  # a tighter-period frame already won this 5-tuple
                        chosen[wkey] = _PendingFact(
                            pick_key=pick_key,
                            period_end=period_end,
                            fiscal_period_type=fpt_out.value,
                            value=value,
                            currency=chosen_currency or None,
                            unit=resolved_unit,
                            source_doc_id=source_doc_id,
                            locator_json=locator.to_json(),
                        )
        # Ladder walk complete: emit each provenance 5-tuple's deterministic
        # winner exactly once. sorted() fixes the insertion order independent of
        # dict/payload iteration, so row ids and restatement observations are
        # reproducible run to run.
        for wkey in sorted(chosen):
            pf = chosen[wkey]
            new_id, superseded_id = insert_with_restatement_detection(
                conn,
                ticker=ticker,
                period_end=pf.period_end,
                fiscal_period_type=pf.fiscal_period_type,
                line_item=ladder.line_item,
                value=pf.value,
                currency=pf.currency,
                unit=pf.unit,
                source_doc_id=pf.source_doc_id,
                confidence=score_confidence(
                    tier=tier_for_source_type(SourceType.SEC_XBRL),
                    extracted_by="sec_xbrl",
                ),
                extracted_by="sec_xbrl",
                locator=pf.locator_json,
            )
            if new_id is not None:
                inserted += 1
            # L10: a SEC restatement of an earlier filing grades the old fact's
            # confidence — capture it (best-effort), don't discard.
            if superseded_id is not None:
                _ = record_restatement_observation(
                    conn,
                    fact_table=FINANCIAL_FACTS,
                    superseded_id=superseded_id,
                    new_value=pf.value,
                )
    conn.commit()
    return inserted


def ingest_for_ticker(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    project_root: Path,
    run_id: str = "sec_xbrl_ingest",
) -> IngestStats:
    """End-to-end: fetch + write to disk + insert documents + insert facts.

    ``run_id`` flows to the unit sanity guard (see
    :func:`insert_facts_from_companyfacts`); the fetch driver passes its
    ingestion-run id so any UNIT_MISMATCH ties back to the run that raised it."""
    cik = CIK_MAP.get(ticker.upper())
    if cik is None:
        raise ValueError(f"No CIK registered for {ticker}; add to CIK_MAP")
    payload = fetch_companyfacts(cik)
    write_companyfacts_to_disk(payload, ticker=ticker, project_root=project_root)
    accessions = _enumerate_accessions(payload)
    accession_to_doc_id = upsert_accession_documents(
        conn, ticker=ticker, accessions=accessions, project_root=project_root
    )
    facts_inserted = insert_facts_from_companyfacts(
        conn,
        ticker=ticker,
        payload=payload,
        accession_to_doc_id=accession_to_doc_id,
        run_id=run_id,
    )
    return IngestStats(accessions_inserted=len(accession_to_doc_id), facts_inserted=facts_inserted)
