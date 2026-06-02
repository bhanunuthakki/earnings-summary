"""Tests for src/industry_classifier.py — golden tests + template parsing.

The classifier is deterministic + rule-based; the goldens fix the expected
buckets for the canonical tickers each industry template was designed around.
A regression here means either (a) a rule unintentionally tightened, or
(b) entity_seed.py's PORTFOLIO_HOLDINGS / WATCHLIST_BIZ_MODELS lost a row.

We also lock down the template-loader contract: the 5 shipped YAMLs parse to
the expected shape (industry slug, display name, ≥1 canonical KPI each).
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from industry_classifier import (  # noqa: E402
    SECTION_CUSTOMER_CONCENTRATION,
    SECTION_LEASE_LADDER,
    SECTION_STRATEGIC_TARGETS,
    SUPPRESSIBLE_SECTIONS,
    CanonicalKPI,
    IndustryTemplate,
    _apply_rules,
    _parse_template_yaml,
    available_industries,
    classify_ticker,
    load_template,
    suppressed_sections_for_ticker,
)

# ---------------------------------------------------------------------------
# Golden classifications — 10+ tickers across all 5 industries
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("ticker", "expected"),
    [
        # hyperscaler — hardcoded set
        ("AMZN", "hyperscaler"),
        ("GOOG", "hyperscaler"),
        ("GOOGL", "hyperscaler"),
        ("META", "hyperscaler"),
        ("MSFT", "hyperscaler"),
        # software_saas — explicit allowlist of well-known SaaS tickers
        ("CRWD", "software_saas"),
        ("NOW", "software_saas"),
        ("DDOG", "software_saas"),
        ("MDB", "software_saas"),
        ("SNOW", "software_saas"),
        ("NET", "software_saas"),
        ("PANW", "software_saas"),
        ("VEEV", "software_saas"),
        # bank — name regex matches in Financials sector
        ("NU", "bank"),
        ("HDB", "bank"),
        ("IBN", "bank"),
        # pharma — name regex matches in Health Care sector
        ("NVO", "pharma"),
        ("NVS", "pharma"),
        # commodity_royalty — Materials sector + royalty/streaming hints
        ("WPM", "commodity_royalty"),
        ("FNV", "commodity_royalty"),
        # Negative cases — known sectors but not a match for any template
        ("COST", None),  # Consumer Staples, no template
        ("ISRG", None),  # Health Care but medical device, not pharma
        ("FCX", None),   # Materials but pure miner, not royalty
        ("BAM", None),   # Financials but asset manager, not bank
        ("AMAT", None),  # Technology but semiconductor, not software_saas
        ("UNKNOWN_TICKER", None),
    ],
)
def test_classify_ticker_golden(ticker: str, expected: str | None) -> None:
    assert classify_ticker(ticker, PROJECT_ROOT) == expected


def test_classify_ticker_uppercases_input() -> None:
    assert classify_ticker("amzn", PROJECT_ROOT) == "hyperscaler"
    assert classify_ticker("Nu", PROJECT_ROOT) == "bank"


def test_classify_ticker_strips_whitespace() -> None:
    assert classify_ticker("  AMZN  ", PROJECT_ROOT) == "hyperscaler"


def test_classify_ticker_empty_string() -> None:
    assert classify_ticker("", PROJECT_ROOT) is None
    assert classify_ticker("   ", PROJECT_ROOT) is None


# ---------------------------------------------------------------------------
# Rule-application unit tests — pure function, independent of seed registry
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("sector", "name", "expected"),
    [
        # Bank hints
        ("Financials", "Banco Santander S.A.", "bank"),
        ("Financials", "Wells Fargo & Company", None),  # name doesn't match hint regex
        ("Financial Services", "ICICI Bank Limited", "bank"),
        ("Financials", "Itau Unibanco Bancorp", "bank"),
        # Pharma hints
        ("Health Care", "Pfizer Inc.", "pharma"),
        ("Health Care", "AstraZeneca plc", "pharma"),
        ("Health Care", "Veeva Systems, Inc.", None),  # software, not pharma
        ("Healthcare", "Merck & Co., Inc.", "pharma"),
        # Royalty hints
        ("Materials", "Sandstorm Gold Royalties", "commodity_royalty"),
        ("Materials", "Osisko Gold Royalties Ltd.", "commodity_royalty"),
        ("Materials", "Rio Tinto Group", None),  # pure miner
        # Software_saas via name hint (no allowlist)
        ("Technology", "Atlassian Software Corp.", "software_saas"),
        ("Communication Services", "Generic Cloud Networks Inc.", "software_saas"),
        # No sector → no match
        (None, "Anything", None),
        ("", "", None),
    ],
)
def test_apply_rules(sector: str | None, name: str | None, expected: str | None) -> None:
    assert _apply_rules(sector=sector, name=name) == expected


# ---------------------------------------------------------------------------
# DB-backed lookup (preferred over seed registry when both available)
# ---------------------------------------------------------------------------


@pytest.fixture()
def mini_db(tmp_path: Path) -> Path:
    """Spin up a tiny portfolio.db with just the entities table; the
    classifier should pick up the sector from there."""
    db_path = tmp_path / "data" / "portfolio.db"
    db_path.parent.mkdir(parents=True)
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE entities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind VARCHAR(32) NOT NULL,
            canonical_name VARCHAR(255) NOT NULL,
            display_name VARCHAR(255),
            external_ids TEXT,
            parent_entity_id INTEGER,
            meta_json TEXT,
            effective_from DATETIME,
            effective_to DATETIME,
            created_at DATETIME NOT NULL,
            last_observed_at DATETIME,
            UNIQUE(kind, canonical_name)
        );
        INSERT INTO entities(kind, canonical_name, display_name, external_ids, meta_json, created_at)
        VALUES (
          'company',
          'Acme Cybersecurity Cloud, Inc.',
          'Acme',
          '{"ticker": "ACME"}',
          '{"sector": "Technology"}',
          '2026-01-01T00:00:00Z'
        );
        """,
    )
    conn.commit()
    conn.close()
    return tmp_path  # repo_root style


def test_classify_via_db_lookup(mini_db: Path) -> None:
    # Picks up sector=Technology + name has "Cybersecurity Cloud" → matches
    # the software_saas regex.
    assert classify_ticker("ACME", mini_db) == "software_saas"


def test_classify_falls_back_to_seed_when_db_missing(tmp_path: Path) -> None:
    # repo_root with no data/portfolio.db at all
    assert classify_ticker("AMZN", tmp_path) == "hyperscaler"  # hardcoded set
    assert classify_ticker("NU", tmp_path) == "bank"  # via entity_seed


# ---------------------------------------------------------------------------
# Template loader
# ---------------------------------------------------------------------------


def test_all_five_templates_present() -> None:
    slugs = available_industries(PROJECT_ROOT)
    assert set(slugs) == {
        "software_saas", "bank", "pharma", "commodity_royalty", "hyperscaler",
    }


@pytest.mark.parametrize(
    "slug",
    ["software_saas", "bank", "pharma", "commodity_royalty", "hyperscaler"],
)
def test_load_template_returns_well_formed(slug: str) -> None:
    t = load_template(slug, PROJECT_ROOT)
    assert isinstance(t, IndustryTemplate)
    assert t.industry == slug
    assert t.display_name
    assert len(t.canonical_kpis) >= 1
    for kpi in t.canonical_kpis:
        assert isinstance(kpi, CanonicalKPI)
        assert kpi.name
        # aliases is a list (possibly empty)
        assert isinstance(kpi.aliases, list)


def test_load_template_software_saas_has_ndr() -> None:
    t = load_template("software_saas", PROJECT_ROOT)
    names = [k.name for k in t.canonical_kpis]
    assert "Net Revenue Retention" in names
    ndr = next(k for k in t.canonical_kpis if k.name == "Net Revenue Retention")
    assert "NDR" in ndr.aliases
    assert ndr.unit_kind == "pct"
    assert ndr.expected_range == (1.05, 1.50)


def test_load_template_bank_has_nim() -> None:
    t = load_template("bank", PROJECT_ROOT)
    names = [k.name for k in t.canonical_kpis]
    assert "Net Interest Margin" in names


def test_load_template_pharma_has_pipeline_npv() -> None:
    t = load_template("pharma", PROJECT_ROOT)
    names = [k.name for k in t.canonical_kpis]
    assert any("Pipeline" in n for n in names)


def test_load_template_unknown_slug_raises() -> None:
    with pytest.raises(FileNotFoundError):
        load_template("nonexistent_industry", PROJECT_ROOT)


def test_to_tier_1_kpis_shape() -> None:
    t = load_template("software_saas", PROJECT_ROOT)
    rows = t.to_tier_1_kpis()
    assert len(rows) == len(t.canonical_kpis)
    for row in rows:
        # All rows must have the holding-JSON expected fields
        assert "name" in row
        assert row["current"] == "TBV from latest earnings release"
        assert row["prior"] is None
        assert row["yoy"] is None
        assert row["status"] == "Unknown"
        assert "break_condition" in row
        assert "source" in row


# ---------------------------------------------------------------------------
# Section-relevance map (suppress_sections)
# ---------------------------------------------------------------------------


def test_suppressible_sections_vocabulary() -> None:
    # The three Company-tab P3 panels are the gateable vocabulary.
    assert SECTION_LEASE_LADDER in SUPPRESSIBLE_SECTIONS
    assert SECTION_CUSTOMER_CONCENTRATION in SUPPRESSIBLE_SECTIONS
    assert SECTION_STRATEGIC_TARGETS in SUPPRESSIBLE_SECTIONS
    assert len(SUPPRESSIBLE_SECTIONS) == 3


@pytest.mark.parametrize(
    "slug",
    ["software_saas", "bank", "pharma", "commodity_royalty", "hyperscaler"],
)
def test_template_suppress_sections_only_use_known_keys(slug: str) -> None:
    """Every key a shipped template lists must be in the renderer's vocabulary,
    so a typo in a YAML can't silently fail to suppress a panel."""
    t = load_template(slug, PROJECT_ROOT)
    assert isinstance(t.suppress_sections, list)
    for key in t.suppress_sections:
        assert key in SUPPRESSIBLE_SECTIONS, f"{slug}: unknown suppress key {key!r}"


def test_bank_template_suppresses_lease_and_concentration() -> None:
    t = load_template("bank", PROJECT_ROOT)
    assert SECTION_LEASE_LADDER in t.suppress_sections
    assert SECTION_CUSTOMER_CONCENTRATION in t.suppress_sections
    # Strategic targets stay -- banks set long-term ROTCE/efficiency targets.
    assert SECTION_STRATEGIC_TARGETS not in t.suppress_sections


def test_hyperscaler_and_pharma_suppress_nothing() -> None:
    assert load_template("hyperscaler", PROJECT_ROOT).suppress_sections == []
    assert load_template("pharma", PROJECT_ROOT).suppress_sections == []


def test_software_and_royalty_suppress_lease_only() -> None:
    for slug in ("software_saas", "commodity_royalty"):
        t = load_template(slug, PROJECT_ROOT)
        assert t.suppress_sections == [SECTION_LEASE_LADDER]


# Declared type so the empty `frozenset()` rows infer `frozenset[str]` (not
# `frozenset[Unknown]`) under pyright strict.
_SUPPRESS_GOLDEN: list[tuple[str, frozenset[str]]] = [
    # bank -> hide lease ladder + customer concentration (NU report comment #15)
    ("NU", frozenset({SECTION_LEASE_LADDER, SECTION_CUSTOMER_CONCENTRATION})),
    # hyperscaler -> keep everything (data-center leases are material)
    ("AMZN", frozenset()),
    ("GOOGL", frozenset()),
    # software_saas + royalty -> hide just the lease ladder
    ("CRWD", frozenset({SECTION_LEASE_LADDER})),
    ("WPM", frozenset({SECTION_LEASE_LADDER})),
    # pharma -> keep everything
    ("NVO", frozenset()),
    # unclassified -> DEFAULT TO SHOW (empty set so the panel isn't blanked)
    ("COST", frozenset()),
    ("UNKNOWN_TICKER", frozenset()),
]


@pytest.mark.parametrize(("ticker", "expected"), _SUPPRESS_GOLDEN)
def test_suppressed_sections_for_ticker_golden(ticker: str, expected: frozenset[str]) -> None:
    assert suppressed_sections_for_ticker(ticker, PROJECT_ROOT) == expected


def test_suppressed_sections_returns_frozenset() -> None:
    assert isinstance(suppressed_sections_for_ticker("NU", PROJECT_ROOT), frozenset)


def test_suppressed_sections_unclassified_default_show(tmp_path: Path) -> None:
    # No DB, no seed match -> classify returns None -> show everything.
    assert suppressed_sections_for_ticker("ZZZZ", tmp_path) == frozenset()


# ---------------------------------------------------------------------------
# YAML parser
# ---------------------------------------------------------------------------


def test_parse_template_yaml_simple_mapping() -> None:
    raw = """
industry: foo
display_name: "Foo Bar"
canonical_kpis:
  - name: "K1"
    aliases: ["A", "B"]
    unit_kind: pct
"""
    parsed = _parse_template_yaml(raw)
    assert parsed["industry"] == "foo"
    assert parsed["display_name"] == "Foo Bar"
    kpis = parsed["canonical_kpis"]
    assert isinstance(kpis, list)
    assert len(kpis) == 1
    first = kpis[0]
    assert isinstance(first, dict)
    assert first["name"] == "K1"
    assert first["aliases"] == ["A", "B"]
    assert first["unit_kind"] == "pct"


def test_parse_template_yaml_inline_null_list() -> None:
    raw = """
industry: foo
display_name: bar
canonical_kpis:
  - name: K1
    expected_range: [null, null]
"""
    parsed = _parse_template_yaml(raw)
    kpis = parsed["canonical_kpis"]
    assert isinstance(kpis, list)
    first = kpis[0]
    assert isinstance(first, dict)
    assert first["expected_range"] == [None, None]


def test_parse_template_yaml_handles_comments() -> None:
    raw = """
# top-level comment
industry: foo  # inline comment
display_name: bar
canonical_kpis:
  - name: K1
"""
    parsed = _parse_template_yaml(raw)
    assert parsed["industry"] == "foo"


def test_parse_template_yaml_inline_floats_in_range() -> None:
    raw = """
industry: foo
display_name: bar
canonical_kpis:
  - name: K1
    expected_range: [0.05, 0.30]
"""
    parsed = _parse_template_yaml(raw)
    kpis = parsed["canonical_kpis"]
    assert isinstance(kpis, list)
    first = kpis[0]
    assert isinstance(first, dict)
    assert first["expected_range"] == [0.05, 0.30]
