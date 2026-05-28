"""Tests for the S-1 narrative fallback wiring.

The recently-IPO'd anchor pattern routes 10-K-narrative consumers through
``filing_text_fetcher.load_canonical_narrative``, which dispatches to the
S-1 cache when the holdings JSON sets ``data_anchor: "s1"`` and to the
SEC 10-K fetcher otherwise.

These tests pin the contract end-to-end without hitting SEC:

  1. The helper reads the holdings JSON, sees data_anchor=="s1", and
     returns the S-1 cache text wrapped as a FilingTextResult.
  2. compute.company_description.extract_for_ticker, given no FMP 10-K
     JSON but a populated S-1 cache, passes the S-1 narrative to the LLM
     prompt as the ``form_10k_text`` argument (instead of an empty string).
  3. The thesis-anchor render in llm.anchors.load_thesis_anchor surfaces
     the "Narrative source: S-1" marker so prompts consuming the anchor
     know the analyst is operating off the prospectus, not a 10-K.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest


def _make_holdings_s1(repo: Path, ticker: str = "FRVO") -> Path:
    """Write a minimal recently-IPO'd holdings JSON."""
    holdings_dir = repo / "micro_thesis" / "holdings"
    holdings_dir.mkdir(parents=True, exist_ok=True)
    path = holdings_dir / f"{ticker}.json"
    path.write_text(
        json.dumps(
            {
                "ticker": ticker,
                "name": "Fervo Energy",
                "thesis": "Next-gen enhanced geothermal systems.",
                "key_driver": "Contracted MWh",
                "recently_ipod": True,
                "ipo_date": "2026-05-13",
                "data_anchor": "s1",
                "s1_cache_path": f"data/sec_text/{ticker}_s1_2026.txt",
                "tier_1_kpis": [],
                "break_rules": [],
                "business_model_rules": [],
                "schema_version": 2,
            }
        ),
        encoding="utf-8",
    )
    return path


def _make_s1_cache(repo: Path, ticker: str = "FRVO", body: str | None = None) -> Path:
    """Drop a fake S-1 text file at the canonical cache location."""
    cache_dir = repo / "data" / "sec_text"
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / f"{ticker}_s1_2026.txt"
    path.write_text(
        body
        or (
            "FORM S-1/A AMENDMENT NO. 3\n\n"
            "Prospectus Summary\n\n"
            "Our Company. Fervo Energy is a next-generation geothermal "
            "energy company building Cape Station in Beaver County, Utah. "
            "We use horizontal drilling and multistage fracturing techniques "
            "developed in the shale industry to produce baseload zero-carbon "
            "electricity from enhanced geothermal systems (EGS). Our power is "
            "sold under long-term power purchase agreements (PPAs) with "
            "utilities and corporates.\n\n"
            "Item 1A. Risk Factors\n\n"
            "We have a limited operating history and may not be able to "
            "execute our business strategy. We have incurred net losses and "
            "expect to continue to incur net losses for the foreseeable "
            "future. The market for enhanced geothermal systems is nascent.\n\n"
            "Item 1B. Unresolved Staff Comments\n\nNone.\n"
        ),
        encoding="utf-8",
    )
    return path


def test_load_canonical_narrative_returns_s1_text_when_anchored(tmp_path: Path) -> None:
    """data_anchor=="s1" + cached S-1 text → helper returns FilingTextResult
    populated with the cache contents and never touches SEC."""
    from filing_text_fetcher import load_canonical_narrative

    _make_holdings_s1(tmp_path)
    _make_s1_cache(tmp_path)

    result = load_canonical_narrative(ticker="FRVO", repo_root=tmp_path)
    assert result is not None
    assert "Fervo Energy" in result.text
    assert "geothermal" in result.text
    assert result.fiscal_year == 2026
    # Risk-factor extractor section should populate from the S-1's Item 1A.
    assert result.item_1a_text is not None
    assert "limited operating history" in result.item_1a_text


def test_load_canonical_narrative_defaults_to_10k_anchor(tmp_path: Path) -> None:
    """No data_anchor field set (or holdings JSON missing) → helper falls
    back to fetch_latest_10k_text. We verify by monkeypatching that
    function and asserting it gets called with the expected ticker."""
    import filing_text_fetcher

    holdings_dir = tmp_path / "micro_thesis" / "holdings"
    holdings_dir.mkdir(parents=True, exist_ok=True)
    (holdings_dir / "AMZN.json").write_text(
        json.dumps({"ticker": "AMZN", "thesis": "..."}),
        encoding="utf-8",
    )

    called_with: dict[str, Any] = {}

    def fake_fetch_10k(**kwargs: Any) -> None:
        called_with.update(kwargs)

    fake_fetch_s1 = lambda **kwargs: pytest.fail("S-1 fetcher invoked for 10-K anchor")  # noqa: E731

    monkey_target_10k = "fetch_latest_10k_text"
    monkey_target_s1 = "fetch_latest_s1_text"
    orig_10k = getattr(filing_text_fetcher, monkey_target_10k)
    orig_s1 = getattr(filing_text_fetcher, monkey_target_s1)
    try:
        setattr(filing_text_fetcher, monkey_target_10k, fake_fetch_10k)
        setattr(filing_text_fetcher, monkey_target_s1, fake_fetch_s1)
        filing_text_fetcher.load_canonical_narrative(ticker="AMZN", repo_root=tmp_path)
    finally:
        setattr(filing_text_fetcher, monkey_target_10k, orig_10k)
        setattr(filing_text_fetcher, monkey_target_s1, orig_s1)

    assert called_with.get("ticker") == "AMZN"


def _create_empty_segment_schema(db_path: Path) -> None:
    """Build the segment junction tables the company-description renderer expects."""
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE segment_periods (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker VARCHAR(16) NOT NULL,
            period_end DATETIME NOT NULL,
            fiscal_period_type VARCHAR(8) NOT NULL,
            source_doc_id INTEGER NOT NULL,
            currency VARCHAR(8),
            unit VARCHAR(16) NOT NULL,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE segment_dimensions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            period_id INTEGER NOT NULL,
            dim_type VARCHAR(16) NOT NULL,
            dim_name VARCHAR(128) NOT NULL,
            value NUMERIC(20, 4) NOT NULL,
            metric VARCHAR(32) NOT NULL
        );
        """
    )
    conn.commit()
    conn.close()


def test_compute_company_description_passes_s1_text_to_llm(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The full flow: no FMP 10-K JSON for FRVO, holdings sets data_anchor=="s1",
    S-1 cache populated → the LLM call receives a non-empty form_10k_text
    sliced from the S-1 prospectus content."""
    import compute.company_description as cd_module

    repo = tmp_path
    (repo / "data" / "company_description").mkdir(parents=True, exist_ok=True)
    (repo / "data" / "historical" / "fmp").mkdir(parents=True, exist_ok=True)
    _create_empty_segment_schema(repo / "data" / "portfolio.db")
    _make_holdings_s1(repo)
    _make_s1_cache(repo)

    captured: dict[str, Any] = {}

    def fake_generate(**kwargs: Any) -> str:
        captured.update(kwargs)
        # Minimum-valid schema-shaped response so the assembler doesn't trip.
        return json.dumps(
            {
                "value_driver_phrase": "geothermal baseload provider",
                "central_bet": "Cape Station ramp + PPA pipeline",
                "swing_variable": "drilling cost curve",
                "paragraphs": [
                    {
                        "opener": "Fervo is anchored on Cape Station.",
                        "body": "EGS turns dry-hole geothermal acreage into baseload power.",
                    }
                ],
                "revenue_mechanics": [
                    {"topic": "ppa_pricing", "body": "Long-term PPAs at fixed $/MWh."}
                ],
                "segments": [],
                "geographies": [],
            }
        )

    monkeypatch.setattr(cd_module, "generate_company_description", fake_generate)

    conn = sqlite3.connect(str(repo / "data" / "portfolio.db"))
    conn.row_factory = sqlite3.Row
    try:
        result = cd_module.extract_for_ticker(
            "FRVO", repo, conn, fiscal_year=None, refresh=True
        )
    finally:
        conn.close()

    assert result.skipped_reason is None, (
        f"expected non-skipped result, got: {result.skipped_reason}"
    )
    # The prompt that just ran must have seen the S-1 narrative as 10-K text.
    form_10k_text = captured.get("form_10k_text") or ""
    assert form_10k_text, "LLM received empty form_10k_text for FRVO — S-1 fallback broken"
    assert "Fervo Energy" in form_10k_text
    assert "geothermal" in form_10k_text
    # FY parsed from the cache filename (FRVO_s1_2026.txt).
    assert result.fiscal_year == 2026


def test_thesis_anchor_surfaces_s1_marker(tmp_path: Path) -> None:
    """When holdings has data_anchor=="s1", the thesis anchor must include
    a "Narrative source: S-1" marker so consumer prompts phrase claims
    correctly (no "the company's 10-K disclosed..." for an issuer with no
    10-K on file)."""
    from llm.anchors import load_thesis_anchor

    _make_holdings_s1(tmp_path)
    anchor = load_thesis_anchor(tmp_path, "FRVO")
    assert "S-1" in anchor or "s-1" in anchor.lower()
    assert "Narrative source" in anchor
    assert "IPO 2026-05-13" in anchor


def test_filing_diff_lens_short_circuits_on_s1_anchor(tmp_path: Path) -> None:
    """The filing-diff narrative lens depends on a prior 10-K to diff
    against — for recently-IPO'd issuers it must short-circuit before the
    DB query, returning None so the synthesis bundle drops the lens
    silently."""
    from synthesis.lenses.filing_diff_narrative import LENS

    _make_holdings_s1(tmp_path)
    ctx = LENS.build_context("FRVO", tmp_path)
    assert ctx is None


def test_filing_intelligence_section_marks_s1_anchor_as_not_applicable(
    tmp_path: Path,
) -> None:
    """The §7.5 renderer must mark recently-IPO'd issuers as NOT_APPLICABLE
    (with a deferred-fix message), not MISSING_DATA — the underlying
    extractor can't run without a 10-K and a prior-year baseline."""
    from report.models import SectionStatus
    from report.sections.filing_intelligence import build

    _make_holdings_s1(tmp_path)
    section = build("FRVO", tmp_path)
    assert section.status == SectionStatus.NOT_APPLICABLE
    assert section.missing is not None
    assert "10-K" in (section.missing.detail or "")
