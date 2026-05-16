"""Tests for the §2 Company description section.

The LLM call lives behind `extract_for_ticker`; tests cover the section
builder, which reads the on-disk cache, plus the renderer's expandable-shape
contract. The LLM is never invoked here.
"""

from __future__ import annotations

import json
import sqlite3
from io import StringIO
from pathlib import Path

import pytest

from compute.company_description import (
    _coerce_named_rows,
    _extract_relevant_text,
    _flatten,
    load_description,
)
from report.models import (
    CompanyDescriptionSection,
    SectionStatus,
    SegmentWeighting,
)
from report.renderers.html import _company_description as _company_description_html
from report.renderers.markdown import _company_description as _company_description_md
from report.sections.company_description import build as build_company_description

# ---------------------------------------------------------------------------
# Test fixtures: minimal-but-real on-disk layout
# ---------------------------------------------------------------------------


_CACHED_RESULT = {
    "ticker": "TEST",
    "fiscal_year": 2024,
    "source_path": "/fake/path",
    "source_sha256": "deadbeef",
    "extracted_at_start": "2026-05-11T10:00:00Z",
    "extracted_at_end": "2026-05-11T10:00:08Z",
    "elapsed_ms": 8000,
    "model": "claude-sonnet-4-6",
    "sector": "Communication Services",
    "industry": "Internet Content",
    "segment_names_requested": ["Cloud", "Services"],
    "geo_names_requested": ["United States", "EMEA"],
    "elevator_pitch": "TestCo provides cloud + internet services.",
    "business_overview": "Para A.\n\nPara B.",
    "revenue_model": "Ads + subscriptions.",
    "segments": [
        {"name": "Cloud", "description": "GCP-like."},
        {"name": "Services", "description": "Ads + apps."},
    ],
    "geographies": [
        {"name": "United States", "description": "Largest market."},
    ],
    "skipped_reason": None,
}


def _create_repo(tmp_path: Path) -> Path:
    """Build the minimum repo layout the section builder needs."""
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "company_description").mkdir()
    (tmp_path / "data" / "historical" / "fmp").mkdir(parents=True)
    # Empty DB with segment_facts table — section can still tolerate empty rows.
    conn = sqlite3.connect(str(tmp_path / "data" / "portfolio.db"))
    conn.executescript(
        """
        CREATE TABLE segment_facts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            period_end TIMESTAMP NOT NULL,
            fiscal_period_type TEXT NOT NULL,
            segment_name TEXT NOT NULL,
            metric TEXT NOT NULL,
            value NUMERIC NOT NULL,
            currency TEXT,
            unit TEXT NOT NULL,
            source_doc_id INTEGER NOT NULL
        );
        """
    )
    conn.commit()
    conn.close()
    return tmp_path


def _write_cache(repo: Path, ticker: str, payload: dict[str, object]) -> None:
    (repo / "data" / "company_description" / f"{ticker.upper()}.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )


def _seed_segment_rows(
    repo: Path, ticker: str, metric: str, rows: list[tuple[str, str, float]]
) -> None:
    """Each row: (period_end ISO, segment_name, value_usd)."""
    conn = sqlite3.connect(str(repo / "data" / "portfolio.db"))
    for period_end, seg, value in rows:
        conn.execute(
            "INSERT INTO segment_facts "
            "(ticker, period_end, fiscal_period_type, segment_name, metric, value, currency, unit, source_doc_id) "
            "VALUES (?, ?, 'Q4', ?, ?, ?, 'USD', 'actual', 1)",
            (ticker, period_end, seg, metric, value),
        )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Section builder — MISSING_DATA branches
# ---------------------------------------------------------------------------


def test_missing_data_when_no_cache(tmp_path: Path) -> None:
    """No cache file → MISSING_DATA with a fix command pointing at the extractor."""
    repo = _create_repo(tmp_path)
    section = build_company_description("AAPL", repo)
    assert section.status == SectionStatus.MISSING_DATA
    assert section.missing is not None
    assert "extract_company_description.py" in section.missing.fix_command
    assert "AAPL" in section.missing.fix_command


def test_skipped_reason_propagates(tmp_path: Path) -> None:
    """A cached `skipped_reason` (e.g. no 10-K on file) surfaces as MISSING_DATA with detail."""
    repo = _create_repo(tmp_path)
    _write_cache(
        repo,
        "TEST",
        {
            **_CACHED_RESULT,
            "elevator_pitch": None,
            "business_overview": None,
            "revenue_model": None,
            "segments": [],
            "geographies": [],
            "skipped_reason": "no _form_10k_*.json under data/historical/fmp/ for TEST",
        },
    )
    section = build_company_description("TEST", repo)
    assert section.status == SectionStatus.MISSING_DATA
    assert section.missing is not None
    assert "no _form_10k_" in (section.missing.detail or "")
    # Still expose sector/industry chips so the section header isn't fully empty.
    assert section.sector == "Communication Services"


# ---------------------------------------------------------------------------
# Section builder — OK branch with live segment_facts weighting
# ---------------------------------------------------------------------------


def test_ok_with_weighting_overlays_live_segment_facts(tmp_path: Path) -> None:
    """Cache + live segment_facts → sorted descending by share with descriptions attached."""
    repo = _create_repo(tmp_path)
    _write_cache(repo, "TEST", _CACHED_RESULT)
    # Latest quarter (2024-12-31) — two segments, two geographies.
    _seed_segment_rows(
        repo,
        "TEST",
        "revenue_by_product",
        [
            ("2024-12-31", "Cloud", 30_000_000_000.0),
            ("2024-12-31", "Services", 70_000_000_000.0),
            # Earlier quarter shouldn't show up.
            ("2024-09-30", "Cloud", 25_000_000_000.0),
        ],
    )
    _seed_segment_rows(
        repo,
        "TEST",
        "revenue_by_geography",
        [
            ("2024-12-31", "United States", 60_000_000_000.0),
            ("2024-12-31", "EMEA", 40_000_000_000.0),
        ],
    )

    section = build_company_description("TEST", repo)
    assert section.status == SectionStatus.OK
    assert section.elevator_pitch == "TestCo provides cloud + internet services."
    assert section.source_fiscal_year == 2024
    assert section.sector == "Communication Services"

    seg_by_name = {r.name: r for r in section.segment_breakdown}
    assert section.segment_breakdown[0].name == "Services"  # sorted by share desc
    assert seg_by_name["Services"].share_pct == pytest.approx(0.70)
    assert seg_by_name["Services"].revenue_usd_m == pytest.approx(70_000.0)
    assert seg_by_name["Services"].description == "Ads + apps."
    assert seg_by_name["Cloud"].description == "GCP-like."

    geo_by_name = {r.name: r for r in section.geographic_breakdown}
    assert section.geographic_breakdown[0].name == "United States"
    assert geo_by_name["United States"].description == "Largest market."
    assert geo_by_name["EMEA"].description is None  # not described in cache


def test_below_1pct_segments_roll_up_to_other(tmp_path: Path) -> None:
    """Segments under 1% of the bucket aggregate into a single 'Other' row."""
    repo = _create_repo(tmp_path)
    _write_cache(repo, "TEST", _CACHED_RESULT)
    _seed_segment_rows(
        repo,
        "TEST",
        "revenue_by_product",
        [
            ("2024-12-31", "Cloud", 99_000_000_000.0),  # 99%
            ("2024-12-31", "Services", 500_000_000.0),  # 0.5%
            ("2024-12-31", "Tiny", 500_000_000.0),  # 0.5%
        ],
    )
    section = build_company_description("TEST", repo)
    names = [r.name for r in section.segment_breakdown]
    assert "Cloud" in names
    # The two sub-1% segments collapse into one synthetic row.
    assert any(n.startswith("Other (2 < 1%)") for n in names)


def test_ok_without_segment_facts_still_renders_descriptions(tmp_path: Path) -> None:
    """No segment_facts rows → fall back to LLM-described rows (no weighting)."""
    repo = _create_repo(tmp_path)
    _write_cache(repo, "TEST", _CACHED_RESULT)
    section = build_company_description("TEST", repo)
    assert section.status == SectionStatus.OK
    # No revenue numbers, but the LLM segment descriptions still surface.
    names = {r.name for r in section.segment_breakdown}
    assert names == {"Cloud", "Services"}
    assert all(r.revenue_usd_m is None for r in section.segment_breakdown)


# ---------------------------------------------------------------------------
# Renderers — shape contract
# ---------------------------------------------------------------------------


def test_html_renderer_emits_expandable_details(tmp_path: Path) -> None:
    """OK section renders an always-visible elevator pitch + a <details> block."""
    repo = _create_repo(tmp_path)
    _write_cache(repo, "TEST", _CACHED_RESULT)
    _seed_segment_rows(
        repo, "TEST", "revenue_by_product", [("2024-12-31", "Cloud", 1.0)]
    )
    section = build_company_description("TEST", repo)
    out = StringIO()
    _company_description_html(out, section)
    html = out.getvalue()
    # Always-visible elements
    assert 'company-pitch' in html
    assert 'TestCo provides cloud + internet services' in html
    # Expandable block + its summary
    assert '<details class="company-details">' in html
    assert 'Segment weighting (latest quarter)' in html
    # Anchor matches the nav
    assert 'id="company-description"' in html
    # Share bar emitted with the live %
    assert 'share-fill' in html


def test_html_renderer_missing_data_shows_fix_callout(tmp_path: Path) -> None:
    repo = _create_repo(tmp_path)
    section = build_company_description("AAPL", repo)
    out = StringIO()
    _company_description_html(out, section)
    html = out.getvalue()
    # Missing-data callout
    assert "extract_company_description.py" in html
    # No expandable details when there's nothing to expand
    assert "<details" not in html


def test_markdown_renderer_emits_pipe_tables(tmp_path: Path) -> None:
    repo = _create_repo(tmp_path)
    _write_cache(repo, "TEST", _CACHED_RESULT)
    _seed_segment_rows(
        repo, "TEST", "revenue_by_product", [("2024-12-31", "Cloud", 1.0)]
    )
    _seed_segment_rows(
        repo,
        "TEST",
        "revenue_by_geography",
        [("2024-12-31", "United States", 1.0)],
    )
    section = build_company_description("TEST", repo)
    out = StringIO()
    _company_description_md(out, section)
    md = out.getvalue()
    assert "## §2 Company description" in md
    assert "> TestCo provides cloud + internet services." in md
    assert "### Segment weighting (latest quarter)" in md
    assert "### Geographic weighting (latest quarter)" in md
    assert "| Cloud |" not in md  # name is wrapped in bold
    assert "| **Cloud** |" in md


# ---------------------------------------------------------------------------
# Platform diagram — optional §2 visual sourced from a sibling cache
# ---------------------------------------------------------------------------


def _write_diagram_cache(repo: Path, ticker: str, payload: dict[str, object]) -> None:
    diagram_dir = repo / "data" / "platform_diagram"
    diagram_dir.mkdir(parents=True, exist_ok=True)
    (diagram_dir / f"{ticker.upper()}.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )


_DIAGRAM_PAYLOAD: dict[str, object] = {
    "ticker": "TEST",
    "fiscal_year": 2024,
    "source_path": "/fake/path",
    "source_sha256": "feedface",
    "transcript_paths": ["/fake/transcripts/TEST_Q4_2024.txt"],
    "extracted_at_start": "2026-05-11T10:00:00Z",
    "extracted_at_end": "2026-05-11T10:00:20Z",
    "elapsed_ms": 20000,
    "model": "claude-sonnet-4-6",
    "diagram": "┌──────┐   ┌──────┐\n│ A    │──→│ B    │\n└──────┘   └──────┘",
    "caption": "A flows into B.",
    "skipped_reason": None,
}


def test_platform_diagram_attaches_when_cache_present(tmp_path: Path) -> None:
    """Sibling `data/platform_diagram/<TICKER>.json` flows into the section."""
    repo = _create_repo(tmp_path)
    _write_cache(repo, "TEST", _CACHED_RESULT)
    _write_diagram_cache(repo, "TEST", _DIAGRAM_PAYLOAD)
    section = build_company_description("TEST", repo)
    assert section.platform_diagram is not None
    assert "──→" in section.platform_diagram
    assert section.platform_caption == "A flows into B."


def test_platform_diagram_absent_leaves_section_intact(tmp_path: Path) -> None:
    """Missing diagram cache → §2 still renders, just without the visual."""
    repo = _create_repo(tmp_path)
    _write_cache(repo, "TEST", _CACHED_RESULT)
    section = build_company_description("TEST", repo)
    assert section.status == SectionStatus.OK
    assert section.platform_diagram is None
    assert section.platform_caption is None


def test_markdown_renderer_emits_platform_overview_when_diagram_present(
    tmp_path: Path,
) -> None:
    """Diagram is wrapped in a fenced code block with the caption in italics."""
    repo = _create_repo(tmp_path)
    _write_cache(repo, "TEST", _CACHED_RESULT)
    _write_diagram_cache(repo, "TEST", _DIAGRAM_PAYLOAD)
    section = build_company_description("TEST", repo)
    out = StringIO()
    _company_description_md(out, section)
    md = out.getvalue()
    assert "### Platform overview" in md
    # Fenced code block carries the box-drawing chars verbatim
    assert "```\n┌──────┐" in md
    # Caption rendered in italics right under the diagram
    assert "_A flows into B._" in md
    # Ordering: diagram block lands between the elevator pitch and "Lines of business"
    elevator_idx = md.index("> TestCo provides cloud + internet services.")
    platform_idx = md.index("### Platform overview")
    lines_idx = md.index("### Lines of business")
    assert elevator_idx < platform_idx < lines_idx


def test_markdown_renderer_omits_platform_block_when_diagram_absent(
    tmp_path: Path,
) -> None:
    """No diagram cache → no '### Platform overview' header in output."""
    repo = _create_repo(tmp_path)
    _write_cache(repo, "TEST", _CACHED_RESULT)
    section = build_company_description("TEST", repo)
    out = StringIO()
    _company_description_md(out, section)
    md = out.getvalue()
    assert "### Platform overview" not in md


# ---------------------------------------------------------------------------
# compute helpers — text extraction + coercion
# ---------------------------------------------------------------------------


def test_flatten_collects_strings_longer_than_80_chars() -> None:
    payload = {
        "short": "skip me",
        "nested": [{"a": "b" * 100}, ["c" * 200, {"d": "skip"}]],
    }
    out = sorted(_flatten(payload), key=len)
    assert out == ["b" * 100, "c" * 200]


def test_extract_relevant_text_only_keyword_sections() -> None:
    """Only sections whose key matches a business-description keyword are included."""
    payload = {
        "Cover": ["unrelated table"],
        "Description of Business": [{"text": "z" * 200}],
        "NATURE OF BUSINESS": [{"text": "y" * 200}],
        "Income Taxes": [{"text": "tax stuff " * 50}],
    }
    text = _extract_relevant_text(payload)
    assert "Description of Business" in text
    assert "NATURE OF BUSINESS" in text
    assert "Income Taxes" not in text


def test_coerce_named_rows_filters_to_allowed_set() -> None:
    """LLM cannot smuggle in segment names not in the allowed list."""
    raw = [
        {"name": "Cloud", "description": "ok"},
        {"name": "PHANTOM", "description": "should drop"},
        {"name": "Services", "description": "  "},  # empty desc → None
        "junk",
    ]
    coerced = _coerce_named_rows(raw, allowed={"Cloud", "Services"})
    assert coerced == [
        {"name": "Cloud", "description": "ok"},
        {"name": "Services", "description": None},
    ]


# ---------------------------------------------------------------------------
# Round-trip — load_description on a real cache file
# ---------------------------------------------------------------------------


def test_load_description_reads_cache(tmp_path: Path) -> None:
    repo = _create_repo(tmp_path)
    _write_cache(repo, "TEST", _CACHED_RESULT)
    result = load_description(repo, "TEST")
    assert result is not None
    assert result.ticker == "TEST"
    assert result.elevator_pitch == "TestCo provides cloud + internet services."
    assert result.segments == [
        {"name": "Cloud", "description": "GCP-like."},
        {"name": "Services", "description": "Ads + apps."},
    ]


def test_load_description_absent_returns_none(tmp_path: Path) -> None:
    repo = _create_repo(tmp_path)
    assert load_description(repo, "MISSING") is None


# ---------------------------------------------------------------------------
# Model invariants
# ---------------------------------------------------------------------------


def test_section_construct_with_defaults() -> None:
    """Pydantic model accepts a minimal MISSING_DATA shape."""
    s = CompanyDescriptionSection(status=SectionStatus.MISSING_DATA)
    assert s.elevator_pitch is None
    assert s.segment_breakdown == []
    assert s.geographic_breakdown == []


def test_segment_weighting_clamps_share_pct_range() -> None:
    """`share_pct` is a decimal fraction in [0,1]; we don't enforce in the model but
    the section builder is the only producer — sanity-check the type."""
    r = SegmentWeighting(name="X", revenue_usd_m=100.0, share_pct=0.42, description=None)
    assert 0.0 <= r.share_pct <= 1.0
