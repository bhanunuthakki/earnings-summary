"""Canonical concept vocabulary + per-accounting-standard field mapping.

Every `FormulaDef` (registry.py) names its inputs as `CanonicalConcept`
members, never raw `financial_facts.line_item` strings — the indirection is
what lets `resolve_concept` swap in an IFRS mapping per ticker without
touching the registry. `US_GAAP_FIELD_MAP` is the identity-ish map for the
Phase 1 catalog (confirmed against `compute.balance_sheet`,
`compute.cashflow`, `compute.income_statement`'s `_LINE_ITEM_SPEC` tables —
the canonical snake_case vocabulary `financial_facts.line_item` already
uses). `IFRS_FIELD_MAP` stays empty until Phase 2 populates it per
verified filer (NU, BN, ASML, NVO) — an unmapped concept for an IFRS ticker
returns `None` from `resolve_concept`, never a guessed field name.
"""

from __future__ import annotations

from enum import StrEnum

from models.companies import AccountingStandard


class CanonicalConcept(StrEnum):
    """Vocabulary every FormulaDef's inputs are expressed in.

    Values are documentation only (never persisted) — `resolve_concept`
    is the only place a concept turns into a `financial_facts.line_item`
    string.
    """

    REVENUE = "revenue"
    GROSS_PROFIT = "gross_profit"
    OPERATING_INCOME = "operating_income"
    NET_INCOME = "net_income"
    EBITDA = "ebitda"  # derived intermediate — see engine.compute_ebitda
    TOTAL_DEBT = "total_debt"
    CASH_AND_EQUIVALENTS = "cash_and_equivalents"
    SHORT_TERM_INVESTMENTS = "short_term_investments"
    LONG_TERM_INVESTMENTS = "long_term_investments"
    TOTAL_STOCKHOLDERS_EQUITY = "total_stockholders_equity"
    TOTAL_ASSETS = "total_assets"
    TOTAL_CURRENT_ASSETS = "total_current_assets"
    TOTAL_CURRENT_LIABILITIES = "total_current_liabilities"
    INVENTORY = "inventory"
    ACCOUNTS_RECEIVABLE = "accounts_receivable"
    ACCOUNTS_PAYABLE = "accounts_payable"
    COST_OF_REVENUE = "cost_of_revenue"
    OPERATING_LEASE_LIABILITY = "operating_lease_liability"
    STOCK_BASED_COMPENSATION = "stock_based_compensation"
    INCOME_TAX_EXPENSE = "income_tax_expense"
    PRETAX_INCOME = "pretax_income"
    INTEREST_EXPENSE = "interest_expense"
    EPS_BASIC = "eps_basic"
    EPS_DILUTED = "eps_diluted"
    WEIGHTED_AVG_SHARES_DILUTED = "weighted_avg_shares_diluted"
    FREE_CASH_FLOW = "free_cash_flow"
    OPERATING_CASH_FLOW = "operating_cash_flow"
    CAPITAL_EXPENDITURE = "capital_expenditure"
    MARKET_CAP = "market_cap"  # Phase 3 only, from historical_market_cap cache
    PRICE = "price"  # Phase 3 only, from EOD price cache
    # Deviation from docs/design/bottoms_up_metrics_engine.md §2's inputs.py
    # sketch: the doc's EBITDA definition ("operating_income +
    # depreciation_and_amortization", §1 Margins/ebitda_margin note) needs a
    # D&A input, but the doc's CanonicalConcept enum omits one. Added here as
    # an internal concept — it is never a `required_input`/`optional_input`
    # on any FormulaDef; `io.py`'s EBITDA intermediate resolves it directly.
    DEPRECIATION_AND_AMORTIZATION = "depreciation_and_amortization"


# US-GAAP: financial_facts.line_item already uses this exact vocabulary for
# most concepts (verified directly against compute.balance_sheet,
# compute.cashflow, compute.income_statement's _LINE_ITEM_SPEC tables — the
# canonical mapping FMP-sourced facts are actually stored under). Concepts
# with no Phase-1 mapping (OPERATING_LEASE_LIABILITY — no _LINE_ITEM_SPEC
# entry exists yet; MARKET_CAP/PRICE — Phase 3, read from cached JSON, not
# financial_facts) are intentionally absent so resolve_concept returns None
# rather than guessing.
US_GAAP_FIELD_MAP: dict[CanonicalConcept, str] = {
    CanonicalConcept.REVENUE: "revenue",
    CanonicalConcept.GROSS_PROFIT: "gross_profit",
    CanonicalConcept.OPERATING_INCOME: "operating_income",
    CanonicalConcept.NET_INCOME: "net_income",
    CanonicalConcept.TOTAL_DEBT: "total_debt",
    CanonicalConcept.CASH_AND_EQUIVALENTS: "cash_and_equivalents",
    CanonicalConcept.SHORT_TERM_INVESTMENTS: "short_term_investments",
    CanonicalConcept.LONG_TERM_INVESTMENTS: "long_term_investments",
    CanonicalConcept.TOTAL_STOCKHOLDERS_EQUITY: "total_stockholders_equity",
    CanonicalConcept.TOTAL_ASSETS: "total_assets",
    CanonicalConcept.TOTAL_CURRENT_ASSETS: "total_current_assets",
    CanonicalConcept.TOTAL_CURRENT_LIABILITIES: "total_current_liabilities",
    CanonicalConcept.INVENTORY: "inventory",
    CanonicalConcept.ACCOUNTS_RECEIVABLE: "accounts_receivable",
    CanonicalConcept.ACCOUNTS_PAYABLE: "accounts_payable",
    CanonicalConcept.COST_OF_REVENUE: "cost_of_revenue",
    CanonicalConcept.STOCK_BASED_COMPENSATION: "stock_based_compensation",
    CanonicalConcept.INCOME_TAX_EXPENSE: "income_tax_expense",
    # financial_facts stores this under FMP's own field name, not the
    # concept's own spelling — resolve_concept is exactly the indirection
    # that absorbs this kind of naming mismatch.
    CanonicalConcept.PRETAX_INCOME: "income_before_tax",
    CanonicalConcept.INTEREST_EXPENSE: "interest_expense",
    # Same naming-mismatch case: FMP's raw field is `eps` (see
    # compute.income_statement._LINE_ITEM_SPEC), stored as line_item "eps".
    CanonicalConcept.EPS_BASIC: "eps",
    CanonicalConcept.EPS_DILUTED: "eps_diluted",
    CanonicalConcept.WEIGHTED_AVG_SHARES_DILUTED: "weighted_avg_shares_diluted",
    CanonicalConcept.FREE_CASH_FLOW: "free_cash_flow",
    CanonicalConcept.OPERATING_CASH_FLOW: "operating_cash_flow",
    CanonicalConcept.CAPITAL_EXPENDITURE: "capital_expenditure",
    CanonicalConcept.DEPRECIATION_AND_AMORTIZATION: "depreciation_and_amortization",
}

# IFRS: only entries actually verified against a roster filer's normalized
# facts go here. Empty in Phase 1 (US-GAAP mapping only, per doc §6 Phase 1
# scope) — populated incrementally in Phase 2 per verified filer (NU, BN,
# ASML, NVO).
IFRS_FIELD_MAP: dict[CanonicalConcept, str] = {}

_STANDARD_MAPS: dict[AccountingStandard, dict[CanonicalConcept, str]] = {
    AccountingStandard.US_GAAP: US_GAAP_FIELD_MAP,
    AccountingStandard.IFRS: IFRS_FIELD_MAP,
}


def resolve_concept(standard: AccountingStandard, concept: CanonicalConcept) -> str | None:
    """Map a CanonicalConcept to a financial_facts.line_item string.

    Returns None when `standard` has no verified mapping for `concept` —
    the caller (io.py) must turn that into
    ReasonCode.MISSING_INPUT_MAPPING, never a guessed field name.
    """
    return _STANDARD_MAPS[standard].get(concept)
