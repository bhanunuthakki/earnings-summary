"""Offline tests for the automated 8-K segment extractor (src/provenance/edgar_8k.py).

Every network/LLM seam is injected, so these run with no EDGAR call and no LLM spend:
HTML strip, CIK resolution, exhibit discovery, exhibit fetch + URL construction, the
LLM parse, the full extract→proposal orchestration, the eval scorer, and a
golden-fixture round-trip.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

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
    "<table><tr><td>Google Cloud</td><td>$17,664</td></tr></table>"
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
        return {"Google Cloud": 17664000000, "Other Bets": "370000000", "junk": "n/a"}

    out = edgar_8k.extract_segment_map(
        text="...",
        ticker="GOOG",
        period_end="2025-12-31",
        fiscal_period_type="Q4",
        dim_type="product",
        call=_call,
    )
    assert out["Google Cloud"] == 17664000000.0
    assert out["Other Bets"] == 370000000.0
    assert "junk" not in out  # unparseable value dropped


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


def test_extract_8k_segment_override_end_to_end() -> None:
    segments = {"Google Cloud": 17664000000, "Other Bets": 370000000}

    proposal = edgar_8k.extract_8k_segment_override(
        ticker="GOOG",
        accession="0001652044-26-000012",
        period_end="2025-12-31",
        fiscal_period_type="Q4",
        get_json=_get_json,
        get_text=lambda _u: _EXHIBIT_HTML,
        call=lambda *_a, **_k: dict(segments),
    )
    assert proposal is not None
    assert proposal.segments == segments
    assert proposal.source_accession == "0001652044-26-000012"
    assert proposal.source_exhibit == "googexhibit991q42025.htm"
    assert proposal.source_url.endswith("googexhibit991q42025.htm")
    assert proposal.dim_type == "product"


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

    # A perfect model (returns the expected) scores 1.0 through the real parse path.
    out = edgar_8k.extract_segment_map(
        text=str(case["exhibit_text"]),
        ticker=str(case["ticker"]),
        period_end=str(case["period_end"]),
        fiscal_period_type=str(case["fiscal_period_type"]),
        dim_type=str(case["dim_type"]),
        call=lambda *_a, **_k: dict(expected),
    )
    assert edgar_8k.score_segment_extraction(out, expected) == 1.0
