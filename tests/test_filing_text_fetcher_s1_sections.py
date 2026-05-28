"""Tests for S-1 named-section extraction in filing_text_fetcher.

An S-1 prospectus labels its sections by NAME ("RISK FACTORS",
"MANAGEMENT'S DISCUSSION AND ANALYSIS ...", the audited F-pages) rather than
the 10-K "Item 1A / 7 / 8" numbering, so the 10-K item regexes returned None
for recently-IPO'd issuers — leaving risk-factor extraction empty. These tests
pin the named-section locators (and the Item-style fallback) against the real
layout quirks: ALL-CAPS headers, a title that wraps across lines, table-of-
contents dotted-leader lines, and prose mentions of the section names.
"""

from __future__ import annotations

import json
from pathlib import Path

from filing_text_fetcher import (
    _extract_s1_sections,
    load_canonical_narrative,
    split_risk_factors,
)

# A compact S-1 that reproduces the structural hazards of a real prospectus:
#  - a table of contents with dotted page leaders,
#  - prose that mentions the section titles mid-sentence,
#  - an ALL-CAPS "RISK FACTORS" header,
#  - an ALL-CAPS MD&A header whose title WRAPS onto the next line,
#  - the audited statements behind the auditor's-report header,
#  - a trailing "PART II" that bounds the financials.
_S1_NAMED = """PROSPECTUS

Table of Contents

Prospectus Summary ...................................................... 6
Risk Factors .......................................................... 39
Use of Proceeds ....................................................... 92
Management's Discussion and Analysis of Financial Condition and Results of Operations ... 100
Business ............................................................. 119
Index to Consolidated Financial Statements ............................ F-1

Prospectus Summary

We are a geothermal company. You should read "Risk Factors" before investing.
Management's Discussion and Analysis of Financial Condition and Results of Operations in this prospectus contains forward-looking statements.

RISK FACTORS

Investing in our common stock involves a high degree of risk.

Risks Related to Our Business

We have a limited operating history and may never achieve profitability.

Use of Proceeds

We intend to use the net proceeds for general corporate purposes.

MANAGEMENT'S DISCUSSION AND ANALYSIS OF FINANCIAL CONDITION AND RESULTS OF
OPERATIONS

The following discussion and analysis should be read together with our
consolidated financial statements.

Business

We develop and operate enhanced geothermal power projects.

REPORT OF INDEPENDENT REGISTERED PUBLIC ACCOUNTING FIRM

Opinion on the Financial Statements

We have audited the accompanying consolidated balance sheets of the Company.

PART II — INFORMATION NOT REQUIRED IN PROSPECTUS

Item 13. Other Expenses of Issuance and Distribution.
"""

# A 10-K-style filing (or an older S-1 that does use Item numbering) — exercises
# the Item-style fallback so #176's contract keeps working.
_ITEM_STYLE = """REPORT OF INDEPENDENT REGISTERED PUBLIC ACCOUNTING FIRM

Item 1A. Risk Factors

We may not be able to compete effectively.

Item 1B. Unresolved Staff Comments

None.
"""


def test_named_risk_factors_section_extracted() -> None:
    item_1a, _item_7, _item_8 = _extract_s1_sections(_S1_NAMED)
    assert item_1a is not None
    assert "high degree of risk" in item_1a
    assert "limited operating history" in item_1a
    # Bounded at the next section — must NOT bleed into Use of Proceeds.
    assert "general corporate purposes" not in item_1a


def test_named_mdna_section_handles_wrapped_allcaps_header() -> None:
    _item_1a, item_7, _item_8 = _extract_s1_sections(_S1_NAMED)
    assert item_7 is not None
    assert "should be read together" in item_7
    # Bounded at the Business header.
    assert "operate enhanced geothermal power projects" not in item_7


def test_named_financials_section_bounded_by_part_ii() -> None:
    _item_1a, _item_7, item_8 = _extract_s1_sections(_S1_NAMED)
    assert item_8 is not None
    assert "audited the accompanying consolidated balance sheets" in item_8
    assert "Other Expenses of Issuance" not in item_8


def test_toc_and_prose_mentions_are_not_treated_as_headers() -> None:
    # The risk-factors body must start at the real header, not the TOC line or
    # the prospectus-summary prose that mentions "Risk Factors" / MD&A.
    item_1a, item_7, _item_8 = _extract_s1_sections(_S1_NAMED)
    assert item_1a is not None and item_1a.lstrip().startswith("Investing in our common stock")
    # The MD&A prose mention ("... in this prospectus contains forward-looking
    # statements.") must not start the MD&A section.
    assert item_7 is not None and "forward-looking statements" not in item_7


def test_split_risk_factors_runs_on_extracted_section() -> None:
    item_1a, _item_7, _item_8 = _extract_s1_sections(_S1_NAMED)
    assert item_1a is not None
    risks = split_risk_factors(item_1a)
    assert any("limited operating history" in body for _heading, body in risks)


def test_item_style_fallback_still_works() -> None:
    item_1a, _item_7, _item_8 = _extract_s1_sections(_ITEM_STYLE)
    assert item_1a is not None
    assert "compete effectively" in item_1a
    assert "Unresolved Staff Comments" not in item_1a


def test_missing_section_returns_none() -> None:
    item_1a, item_7, item_8 = _extract_s1_sections("Just some prose with no section headers at all.")
    assert item_1a is None
    assert item_7 is None
    assert item_8 is None


def _write_s1_holding(repo: Path, ticker: str, body: str) -> None:
    holdings = repo / "micro_thesis" / "holdings"
    holdings.mkdir(parents=True, exist_ok=True)
    (holdings / f"{ticker}.json").write_text(
        json.dumps(
            {
                "ticker": ticker,
                "thesis": "stub",
                "data_anchor": "s1",
                "s1_cache_path": f"data/sec_text/{ticker}_s1_2026.txt",
            }
        ),
        encoding="utf-8",
    )
    cache = repo / "data" / "sec_text"
    cache.mkdir(parents=True, exist_ok=True)
    (cache / f"{ticker}_s1_2026.txt").write_text(body, encoding="utf-8")


def test_load_canonical_narrative_populates_s1_sections(tmp_path: Path) -> None:
    _write_s1_holding(tmp_path, "FRVO", _S1_NAMED)
    result = load_canonical_narrative(ticker="FRVO", repo_root=tmp_path)
    assert result is not None
    assert result.item_1a_text and "high degree of risk" in result.item_1a_text
    assert result.item_7_text and "should be read together" in result.item_7_text
    assert result.item_8_text and "audited the accompanying" in result.item_8_text
    # Full text is always available as a fallback for LLM consumers.
    assert "Prospectus Summary" in result.text
