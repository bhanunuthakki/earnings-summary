"""Offline tests for the automated 8-K segment extractor (src/provenance/edgar_8k.py).

Every network/LLM seam is injected, so these run with no EDGAR call and no LLM spend:
HTML strip, CIK resolution, exhibit discovery, exhibit fetch + URL construction, the
LLM parse, the full extract→proposal orchestration, the eval scorer, a
golden-fixture round-trip, and the Phase C anchor-quote verification gate
(docs/design/provenance_clickthrough.md §3.3) -- including the fabricated-quote
rejection guard_test asked for.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import cast

from models.facts import FactLocator, LegacyEscapeHatch, LocatorKind
from provenance import edgar_8k

PROJECT_ROOT = Path(__file__).resolve().parents[1]
_GOLDEN = PROJECT_ROOT / "evals" / "golden" / "extract_8k_overrides.json"

_TICKERS_JSON = {"0": {"cik_str": 1652044, "ticker": "GOOG", "title": "Alphabet Inc."}}
_INDEX_JSON = {
    "directory": {
        "item": [
            {"name": "0001652044-26-000012-index.htm", "type": "", "size": "1"},
            {"name": "goog8k.htm", "type": "8-K", "size": "1"},
            {"name": "googexhibit991q42025.htm", "type": "EX-99.1", "size": "1"},
        ]
    }
}
_EXHIBIT_HTML = (
    "<html><body><h2>Q4 2025</h2>"
    "<table><tr><td>Google Cloud</td><td>$17,664</td></tr>"
    "<tr><td>Other Bets</td><td>$370</td></tr></table>"
    "<p>Google&nbsp;Cloud revenue grew 48%.</p></body></html>"
)


def _get_json(url: str) -> object:
    if "company_tickers" in url:
        return _TICKERS_JSON
    if "index.json" in url:
        return _INDEX_JSON
    raise AssertionError(f"unexpected get_json url: {url}")


def test_strip_html() -> None:
    out = edgar_8k.strip_html(_EXHIBIT_HTML)
    assert "Google Cloud" in out
    assert "17,664" in out
    assert "<" not in out and "&nbsp;" not in out


def test_resolve_cik() -> None:
    assert edgar_8k.resolve_cik("goog", get_json=_get_json) == "0001652044"
    assert edgar_8k.resolve_cik("ZZZZ", get_json=_get_json) is None


def test_discover_exhibit_prefers_ex99() -> None:
    name = edgar_8k.discover_exhibit("1652044", "0001652044-26-000012", get_json=_get_json)
    assert name == "googexhibit991q42025.htm"


def test_fetch_exhibit_text_builds_url() -> None:
    seen: dict[str, str] = {}

    def _get_text(url: str) -> str:
        seen["url"] = url
        return _EXHIBIT_HTML

    fetched = edgar_8k.fetch_exhibit_text(
        ticker="GOOG", accession="0001652044-26-000012", get_json=_get_json, get_text=_get_text
    )
    assert fetched is not None
    text, name, url = fetched
    assert name == "googexhibit991q42025.htm"
    assert url == (
        "https://www.sec.gov/Archives/edgar/data/1652044/"
        "000165204426000012/googexhibit991q42025.htm"
    )
    assert seen["url"] == url
    assert "Google Cloud" in text


def test_extract_segment_map_parses_and_coerces() -> None:
    def _call(prompt: str, **kwargs: object) -> object:
        assert "product" in prompt.lower()
        assert "anchor_quote" in prompt
        return {
            "Google Cloud": {"value": 17664000000, "anchor_quote": "Google Cloud $17,664"},
            "Other Bets": {"value": "370000000"},  # anchor_quote omitted -- tolerated
            "junk": {"value": "n/a"},
        }

    out = edgar_8k.extract_segment_map(
        text="...",
        ticker="GOOG",
        period_end="2025-12-31",
        fiscal_period_type="Q4",
        dim_type="product",
        call=_call,
    )
    assert out["Google Cloud"].value == 17664000000.0
    assert out["Google Cloud"].anchor_quote == "Google Cloud $17,664"
    assert out["Other Bets"].value == 370000000.0
    assert out["Other Bets"].anchor_quote is None
    assert "junk" not in out  # unparseable value dropped


def test_extract_segment_map_tolerates_flat_legacy_shape() -> None:
    """A model that ignores the nested-object instruction and returns a flat
    {name: value} scalar (the pre-Phase-C shape) still parses -- just with no
    anchor_quote, rather than the whole segment being dropped."""
    out = edgar_8k.extract_segment_map(
        text="...",
        ticker="GOOG",
        period_end="2025-12-31",
        fiscal_period_type="Q4",
        dim_type="product",
        call=lambda *_a, **_k: {"Google Cloud": 17664000000},
    )
    assert out["Google Cloud"].value == 17664000000.0
    assert out["Google Cloud"].anchor_quote is None


def test_extract_segment_map_non_dict_is_empty() -> None:
    out = edgar_8k.extract_segment_map(
        text="...",
        ticker="GOOG",
        period_end="2025-12-31",
        fiscal_period_type="Q4",
        dim_type="product",
        call=lambda *_a, **_k: ["not", "a", "dict"],
    )
    assert out == {}


def _happy_call(*_a: object, **_k: object) -> object:
    return {
        "Google Cloud": {"value": 17664000000, "anchor_quote": "Google Cloud $17,664"},
        "Other Bets": {"value": 370000000, "anchor_quote": "Other Bets $370"},
    }


def test_extract_8k_segment_override_end_to_end() -> None:
    segments = {"Google Cloud": 17664000000, "Other Bets": 370000000}

    proposal = edgar_8k.extract_8k_segment_override(
        ticker="GOOG",
        accession="0001652044-26-000012",
        period_end="2025-12-31",
        fiscal_period_type="Q4",
        get_json=_get_json,
        get_text=lambda _u: _EXHIBIT_HTML,
        call=_happy_call,
    )
    assert proposal is not None
    assert proposal.segments == segments
    assert proposal.source_accession == "0001652044-26-000012"
    assert proposal.source_exhibit == "googexhibit991q42025.htm"
    assert proposal.source_url.endswith("googexhibit991q42025.htm")
    assert proposal.dim_type == "product"
    # Every anchor_quote was verbatim in the (stripped) exhibit text -> a real
    # html_span locator, not an escape hatch.
    assert proposal.failed_segments == ()
    assert isinstance(proposal.locator, FactLocator)
    assert proposal.locator.effective_kind() == LocatorKind.HTML_SPAN
    assert proposal.locator.html_span is not None
    assert proposal.locator.html_span.doc_id is None  # patched later by the CLI
    assert "Google Cloud $17,664" in (proposal.locator.verbatim_snippet or "")
    assert "Other Bets $370" in (proposal.locator.verbatim_snippet or "")


def test_extract_8k_segment_override_rejects_fabricated_anchor_quote() -> None:
    """A quote that is NOT verbatim in the fetched exhibit text (the
    hallucination case) must NEVER produce a renderable locator -- this is
    the guard-ratchet's required 'a fabricated quote MUST be rejected' test."""

    def _fabricating_call(*_a: object, **_k: object) -> object:
        return {
            "Google Cloud": {
                "value": 17664000000,
                # Plausible-looking but not present anywhere in _EXHIBIT_HTML's
                # stripped text -- a hallucinated anchor.
                "anchor_quote": "Google Cloud revenue reached a record $17.7 billion this quarter",
            },
            "Other Bets": {"value": 370000000, "anchor_quote": "Other Bets $370"},
        }

    proposal = edgar_8k.extract_8k_segment_override(
        ticker="GOOG",
        accession="0001652044-26-000012",
        period_end="2025-12-31",
        fiscal_period_type="Q4",
        get_json=_get_json,
        get_text=lambda _u: _EXHIBIT_HTML,
        call=_fabricating_call,
    )
    assert proposal is not None
    # The VALUE is still extracted and returned (never dropped for this
    # reason alone) --
    assert proposal.segments["Google Cloud"] == 17664000000
    # -- but the locator is demoted, never a fabricated html_span.
    assert isinstance(proposal.locator, LegacyEscapeHatch)
    assert "anchor_verification_failed" in proposal.locator.reason
    assert proposal.failed_segments == ("Google Cloud",)


def test_extract_8k_segment_override_no_anchor_quotes_is_legacy() -> None:
    """Every segment omitting anchor_quote (no fabrication, just nothing to
    verify) also demotes to a legacy escape hatch -- not a failure, but
    honestly nothing renderable either."""
    proposal = edgar_8k.extract_8k_segment_override(
        ticker="GOOG",
        accession="0001652044-26-000012",
        period_end="2025-12-31",
        fiscal_period_type="Q4",
        get_json=_get_json,
        get_text=lambda _u: _EXHIBIT_HTML,
        call=lambda *_a, **_k: {
            "Google Cloud": {"value": 17664000000},
            "Other Bets": {"value": 370000000},
        },
    )
    assert proposal is not None
    assert isinstance(proposal.locator, LegacyEscapeHatch)
    assert proposal.failed_segments == ()  # honest gap, not a hallucination


def test_extract_returns_none_on_empty_breakdown() -> None:
    proposal = edgar_8k.extract_8k_segment_override(
        ticker="GOOG",
        accession="0001652044-26-000012",
        period_end="2025-12-31",
        fiscal_period_type="Q4",
        get_json=_get_json,
        get_text=lambda _u: _EXHIBIT_HTML,
        call=lambda *_a, **_k: {},
    )
    assert proposal is None


def test_register_8k_exhibit_document_idempotent(tmp_path: Path) -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        "CREATE TABLE documents (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "ticker TEXT NOT NULL, source_type TEXT NOT NULL, doc_type TEXT NOT NULL, "
        "period_start TIMESTAMP, period_end TIMESTAMP, file_path TEXT NOT NULL, "
        "sha256 TEXT NOT NULL, fetched_at TIMESTAMP NOT NULL, fetch_status TEXT NOT NULL, "
        "http_code INTEGER, raw_bytes_size INTEGER NOT NULL, source_url TEXT, "
        "parent_document_id INTEGER, accession_number TEXT);"
    )
    doc_id = edgar_8k.register_8k_exhibit_document(
        conn,
        ticker="GOOG",
        accession="0001652044-26-000012",
        exhibit_name="googexhibit991q42025.htm",
        exhibit_text="Google Cloud $17,664",
        source_url="https://www.sec.gov/example.htm",
        period_end="2025-12-31",
        repo_root=tmp_path,
    )
    row = conn.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()
    assert row is not None
    assert row["doc_type"] == "sec_8k"
    assert row["source_type"] == "sec_xbrl"
    assert row["accession_number"] == "0001652044-26-000012"
    assert (tmp_path / str(row["file_path"])).exists()

    # Re-registering the SAME exhibit text returns the existing row, not a duplicate.
    doc_id_2 = edgar_8k.register_8k_exhibit_document(
        conn,
        ticker="GOOG",
        accession="0001652044-26-000012",
        exhibit_name="googexhibit991q42025.htm",
        exhibit_text="Google Cloud $17,664",
        source_url="https://www.sec.gov/example.htm",
        period_end="2025-12-31",
        repo_root=tmp_path,
    )
    assert doc_id_2 == doc_id
    count = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
    assert count == 1
    conn.close()


def test_score_segment_extraction() -> None:
    expected = {"Google Cloud": 17664000000, "Other Bets": 370000000}
    assert edgar_8k.score_segment_extraction(dict(expected), expected) == 1.0
    # A miss halves the score.
    assert edgar_8k.score_segment_extraction({"Google Cloud": 17664000000}, expected) == 0.5
    # A spurious extra segment also lowers it (denominator = larger map).
    noisy = {"Google Cloud": 17664000000, "Other Bets": 370000000, "Google Inc.": 48030000000}
    assert edgar_8k.score_segment_extraction(noisy, expected) < 1.0
    # Within tolerance still matches.
    assert (
        edgar_8k.score_segment_extraction(
            {"Google Cloud": 17664000000, "Other Bets": 370000001}, expected
        )
        == 1.0
    )


def test_golden_fixture_round_trips() -> None:
    cases = cast("list[dict[str, object]]", json.loads(_GOLDEN.read_text(encoding="utf-8")))
    case = cases[0]
    expected = {
        str(k): float(cast("float", v))
        for k, v in cast("dict[str, object]", case["expected"]).items()
    }

    # A perfect model (returns the expected, nested value/anchor_quote shape)
    # scores 1.0 through the real parse path.
    out = edgar_8k.extract_segment_map(
        text=str(case["exhibit_text"]),
        ticker=str(case["ticker"]),
        period_end=str(case["period_end"]),
        fiscal_period_type=str(case["fiscal_period_type"]),
        dim_type=str(case["dim_type"]),
        call=lambda *_a, **_k: {k: {"value": v} for k, v in expected.items()},
    )
    out_values = {k: sx.value for k, sx in out.items()}
    assert edgar_8k.score_segment_extraction(out_values, expected) == 1.0
