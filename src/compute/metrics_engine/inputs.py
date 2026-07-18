"""Canonical concept vocabulary + per-accounting-standard field mapping.

Every `FormulaDef` (registry.py) names its inputs as `CanonicalConcept`
members, never raw `financial_facts.line_item` strings — the indirection is
what lets `resolve_concept` swap in an IFRS mapping per ticker without
touching the registry. `US_GAAP_FIELD_MAP` is the identity-ish map for the
Phase 1 catalog (confirmed against `compute.balance_sheet`,
`compute.cashflow`, `compute.income_statement`'s `_LINE_ITEM_SPEC` tables —
the canonical snake_case vocabulary `financial_facts.line_item` already
uses). `IFRS_FIELD_MAP` is populated in Phase 2 per verified filer (NU, BN,
ASML, NVO) — an unmapped concept for an IFRS ticker returns `None` from
`resolve_concept`, never a guessed field name.

Phase 2 addition — `OPERATING_LEASE_LIABILITY`: Phase 1 left this concept
unmapped for US-GAAP ("no `_LINE_ITEM_SPEC` entry exists yet"), but
`roic_lease_adjusted` (registry.py) needs it. Verified directly against a
live `data/portfolio.db`: `financial_facts.line_item = 'operating_lease_liability'`
is a real, populated field for MELI/META/NOW/VEEV/RBRK/WIX/UBER/BKNG/GOOG
(and ASML among the IFRS roster) — it is written by
`compute.as_reported._XBRL_TAG_MAP` (the XBRL capture path), not
`compute.balance_sheet`'s `_LINE_ITEM_SPEC`, which is why Phase 1's search
missed it. Added to `US_GAAP_FIELD_MAP` as an identity mapping now that a
formula actually consumes it.
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
    # Phase 2: no _LINE_ITEM_SPEC entry, but the XBRL capture path (see
    # module docstring) writes this field; roic_lease_adjusted consumes it.
    CanonicalConcept.OPERATING_LEASE_LIABILITY: "operating_lease_liability",
}

# IFRS: only entries actually verified against a roster filer's normalized
# facts go here. Populated in Phase 2, verified directly against
# `data/portfolio.db` (read-only query, not guessed) for every live IFRS
# roster name — NU (bank), BN (holdco), ASML, NVO (both operating_company).
#
# Finding: FMP's normalization layer already collapses IFRS filers onto the
# EXACT SAME `financial_facts.line_item` vocabulary as US-GAAP filers — every
# concept below was confirmed present (non-empty, plausible-range values) for
# all four tickers under the identical field name used in
# `US_GAAP_FIELD_MAP`. This is a genuine per-concept verification, not a
# blanket copy: OPERATING_LEASE_LIABILITY was checked case-by-case (see
# below) rather than assumed to follow the same pattern.
#
# OPERATING_LEASE_LIABILITY is included because the field-name mapping
# itself is verified (ASML reports real, non-null values under this exact
# name — proving FMP's IFRS normalization uses the identical tag). NU, BN,
# and NVO simply have no data under that name for the periods checked; that
# is a per-period data gap (ReasonCode.MISSING_INPUT via the normal
# resolution path), not a naming/mapping gap (ReasonCode.
# MISSING_INPUT_MAPPING) — the two failure modes are deliberately distinct
# per docs/design/bottoms_up_metrics_engine.md §3, and conflating them by
# leaving the concept unmapped here would misreport a real data absence as
# "we don't understand IFRS's vocabulary for this concept", which is false.
IFRS_FIELD_MAP: dict[CanonicalConcept, str] = dict(US_GAAP_FIELD_MAP)

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
