"""Tests for src/compute/s1_financials.py (S-1 audited-statement parser)."""

from __future__ import annotations

from decimal import Decimal

from compute.s1_financials import build_financial_facts, parse_s1_text
from models.facts import Currency, FiscalPeriodType, Unit

# Synthetic S-1 text mimicking the HTML-stripped flattened shape: each line-item
# label (with dotted leaders) on its own line, then one line per period column,
# separated by blank lines. Includes a TOC + a "summary" operations table BEFORE
# the auditor's report to prove the parser anchors on the audited F-pages and
# ignores the earlier, unaudited figures.
_SAMPLE_S1 = """
Index to Consolidated Financial Statements

Report of Independent Registered Public Accounting Firm ............... 5
Consolidated Balance Sheets ............... 6
Consolidated Statements of Operations ............... 7
Consolidated Statements of Cash Flows ............... 9

Summary Consolidated Statements of Operations

Year ended December 31,

2025

2024

Revenues ...................................................................

$999

$888

REPORT OF INDEPENDENT REGISTERED PUBLIC ACCOUNTING FIRM

We have audited the accompanying consolidated balance sheets of Example Co.

Consolidated Balance Sheets

(Dollars in thousands)

As of December 31,

2025

2024

Cash and cash equivalents ...................................................

$461,836

$193,428

Total current assets ........................................................

482,130

205,297

Total assets ................................................................

$1,365,168

$531,299

Total liabilities ...........................................................

408,794

146,994

Total stockholders' (deficit) equity ........................................

(246,498)

(177,195)

Consolidated Statements of Operations

(Dollars and shares in thousands except per share amounts)

Year ended December 31,

2025

2024

Revenues ...................................................................

$138

$199

Operating loss .............................................................

(48,806)

(41,838)

Net loss ...................................................................

$(57,788)

$(41,110)

Net loss per share attributable to common stockholders, basic and diluted ...

$(5.66)

$(3.31)

Weighted average shares, basic and diluted .................................

12,462

12,438

Consolidated Statements of Cash Flows

(Dollars in thousands)

Year ended December 31,

2025

2024

Net loss ...................................................................

$(57,788)

$(41,110)

Stock-based compensation ...................................................

2,665

—

Net cash used in operating activities ......................................

(31,757)

(54,748)

Capital expenditures .......................................................

(465,659)

(178,693)

Notes to Consolidated Financial Statements

NOTE 1 - NATURE OF BUSINESS
"""


def _by_key(data):
    return {(d.line_item, d.period_end.year): d for d in data}


def test_parse_core_line_items_values_and_signs() -> None:
    facts = _by_key(parse_s1_text(_SAMPLE_S1))
    # Revenue scaled thousands -> actual; audited (138), NOT the summary 999.
    assert facts[("revenue", 2025)].value == Decimal(138_000)
    assert facts[("revenue", 2024)].value == Decimal(199_000)
    # Net loss is parenthesised AND $-prefixed ("$(57,788)") — the case that
    # silently dropped before the strip-$-then-parens fix.
    assert facts[("net_income", 2025)].value == Decimal(-57_788_000)
    assert facts[("total_assets", 2025)].value == Decimal(1_365_168_000)
    assert facts[("cash_and_equivalents", 2025)].value == Decimal(461_836_000)
    # Deficit / loss subtotals stay negative.
    assert facts[("total_stockholders_equity", 2025)].value == Decimal(-246_498_000)
    assert facts[("operating_income", 2025)].value == Decimal(-48_806_000)


def test_summary_section_before_audit_report_is_ignored() -> None:
    # The pre-audit "Summary" operations table reports revenue 999/888; the
    # parser must anchor on the audited F-pages and never emit those.
    facts = _by_key(parse_s1_text(_SAMPLE_S1))
    assert facts[("revenue", 2025)].value == Decimal(138_000)
    assert all(d.value != Decimal(999_000) for d in parse_s1_text(_SAMPLE_S1))


def test_per_share_amounts_are_not_scaled() -> None:
    facts = _by_key(parse_s1_text(_SAMPLE_S1))
    eps = facts[("eps", 2025)]
    assert eps.value == Decimal("-5.66")
    assert eps.unit is Unit.ACTUAL
    assert eps.currency is Currency.USD
    assert facts[("eps_diluted", 2024)].value == Decimal("-3.31")


def test_share_counts_scaled_and_currency_none() -> None:
    facts = _by_key(parse_s1_text(_SAMPLE_S1))
    shares = facts[("weighted_avg_shares", 2025)]
    assert shares.value == Decimal(12_462_000)
    assert shares.unit is Unit.COUNT
    assert shares.currency is None


def test_em_dash_is_nil() -> None:
    facts = _by_key(parse_s1_text(_SAMPLE_S1))
    assert facts[("stock_based_compensation", 2025)].value == Decimal(2_665_000)
    assert facts[("stock_based_compensation", 2024)].value == Decimal(0)


def test_region_disambiguates_repeated_labels() -> None:
    # "Net loss" appears in both operations and cash flow; each maps to its own
    # canonical line_item with the same magnitude.
    facts = _by_key(parse_s1_text(_SAMPLE_S1))
    assert facts[("net_income", 2025)].value == Decimal(-57_788_000)
    assert facts[("net_income_cf", 2025)].value == Decimal(-57_788_000)


def test_free_cash_flow_is_derived() -> None:
    facts = _by_key(parse_s1_text(_SAMPLE_S1))
    # FCF = operating_cash_flow + capital_expenditure (both negative).
    assert facts[("operating_cash_flow", 2025)].value == Decimal(-31_757_000)
    assert facts[("capital_expenditure", 2025)].value == Decimal(-465_659_000)
    assert facts[("free_cash_flow", 2025)].value == Decimal(-497_416_000)
    assert facts[("free_cash_flow", 2024)].value == Decimal(-233_441_000)


def test_all_periods_are_fy_and_build_facts_carry_ticker() -> None:
    data = parse_s1_text(_SAMPLE_S1)
    assert data, "expected parsed facts"
    assert all(d.fiscal_period_type is FiscalPeriodType.FY for d in data)
    facts = build_financial_facts(data, source_doc_id=42, ticker="exco")
    assert all(f.ticker == "EXCO" for f in facts)
    assert all(f.source_doc_id == 42 for f in facts)
    assert len(facts) == len(data)
