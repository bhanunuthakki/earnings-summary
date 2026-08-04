"""Tests for the generic headless IR-document crawler (PR2).

The Playwright call is injected (no browser): a fake renderer maps a page URL to
its ``[(href, text)]`` anchors. These lock the link harvest (doc-like links
only), one-hop same-host history following, Tier-1 period / doc-type attribution,
``max_quarters`` truncation, resilience (dead/empty page → ``[]``), and the
hybrid merge with a precise adapter.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from ir_pipeline.authority import PublisherEndpointRule  # noqa: E402
from ir_pipeline.config import IrConfig  # noqa: E402
from ir_pipeline.discover import discover_history_hybrid  # noqa: E402
from ir_pipeline.discover._docmeta import CandidateDoc  # noqa: E402
from ir_pipeline.discover.generic import (  # noqa: E402
    discover_document_history,
    discover_document_inventory,
    precise_to_candidates,
)


class _Renderer:
    """Fake page renderer: url → [(href, text)]; records the URLs it was asked for."""

    def __init__(self, pages: dict[str, list[tuple[str, str]]]) -> None:
        self._pages = pages
        self.calls: list[str] = []

    def __call__(self, url: str, timeout_ms: int) -> list[tuple[str, str]]:
        self.calls.append(url)
        return self._pages.get(url, [])


def _no_fetch(_href: str) -> str:
    return ""


def _run(
    pages: dict[str, list[tuple[str, str]]],
    *,
    ir_url: str = "https://ir.x/",
    max_quarters: int = 8,
) -> tuple[list[CandidateDoc], _Renderer]:
    r = _Renderer(pages)
    docs = discover_document_history(
        ir_url=ir_url,
        render=r,
        fetch_filename=_no_fetch,
        rate_limit_s=0.0,
        check_robots=False,
        max_quarters=max_quarters,
    )
    return docs, r


def test_harvests_only_document_links() -> None:
    pages = {
        "https://ir.x/": [
            ("https://ir.x/q3-2025-press-release.pdf", "Q3 2025 Press Release"),
            ("https://cdn.x/data.xlsx", "Financial Supplement"),
            ("https://ir.x/about", "About Us"),
            ("mailto:ir@x.com", "Email IR"),
        ]
    }
    docs, _ = _run(pages)
    assert {d.url for d in docs} == {
        "https://ir.x/q3-2025-press-release.pdf",
        "https://cdn.x/data.xlsx",
    }


def test_attributes_period_and_doc_type() -> None:
    pages = {"https://ir.x/": [("https://ir.x/q3-2025-press-release.pdf", "Q3 2025 Press Release")]}
    docs, _ = _run(pages)
    (d,) = docs
    assert d.year_guess == 2025
    assert d.quarter_guess == 3
    assert d.doc_type_guess == "press_release"


@pytest.mark.parametrize(
    ("text", "year", "quarter"),
    [
        ("Q3 2025 Press Release", 2025, 3),
        ("1Q26 Earnings", 2026, 1),
        ("1T26 Resultados", 2026, 1),  # Portuguese "trimestre" (NU/MELI/DLO)
        ("FY2025 Q2 Results", 2025, 2),
        ("2024-Q4 deck", 2024, 4),
        ("Annual governance doc", None, None),
    ],
)
def test_period_attribution_formats(text: str, year: int | None, quarter: int | None) -> None:
    pages = {"https://ir.x/": [("https://ir.x/doc.pdf", text)]}
    docs, _ = _run(pages)
    (d,) = docs
    assert (d.year_guess, d.quarter_guess) == (year, quarter)


def test_follows_same_host_history_nav_one_hop() -> None:
    pages = {
        "https://ir.x/": [
            ("https://ir.x/quarterly-results", "Quarterly Results"),
            ("https://ir.x/latest.pdf", "Latest Release"),
        ],
        "https://ir.x/quarterly-results": [
            ("https://ir.x/q2-2025-presentation.pdf", "Q2 2025 Earnings Presentation"),
        ],
    }
    docs, r = _run(pages)
    urls = {d.url for d in docs}
    assert "https://ir.x/q2-2025-presentation.pdf" in urls
    assert "https://ir.x/latest.pdf" in urls
    assert "https://ir.x/quarterly-results" in r.calls


def test_does_not_follow_offsite_or_nonhistory_nav() -> None:
    pages = {
        "https://ir.x/": [
            ("https://other.com/quarterly-results", "Quarterly Results"),
            ("https://ir.x/leadership-team", "Leadership Team"),
        ],
    }
    docs, r = _run(pages)
    assert docs == []
    assert r.calls == ["https://ir.x/"]  # nothing followed


def test_truncates_dated_to_max_quarters_keeps_undated() -> None:
    links: list[tuple[str, str]] = []
    for y in (2024, 2025):
        for q in (1, 2, 3, 4):
            links.append((f"https://ir.x/{y}-q{q}.pdf", f"Q{q} {y} Press Release"))
    links.append(("https://ir.x/overview.pdf", "Company Overview"))  # undated
    docs, _ = _run({"https://ir.x/": links}, max_quarters=2)
    dated = {(d.year_guess, d.quarter_guess) for d in docs if d.year_guess is not None}
    assert dated == {(2025, 4), (2025, 3)}
    assert any(d.year_guess is None for d in docs)


def test_dead_page_returns_empty() -> None:
    def _boom(url: str, timeout_ms: int) -> list[tuple[str, str]]:
        raise RuntimeError("browser crashed")

    docs = discover_document_history(
        ir_url="https://ir.x/",
        render=_boom,
        fetch_filename=_no_fetch,
        rate_limit_s=0.0,
        check_robots=False,
    )
    assert docs == []


def test_no_ir_url_returns_empty() -> None:
    assert discover_document_history(ir_url="") == []


def test_inventory_is_untruncated_and_records_page_failure() -> None:
    root = "https://ir.x/"
    archive = "https://ir.x/results"
    links = [
        (f"https://ir.x/{year}-q{quarter}.pdf", f"Q{quarter} {year} Results")
        for year in (2023, 2024, 2025)
        for quarter in (1, 2, 3, 4)
    ]

    def _render(url: str, timeout_ms: int) -> list[tuple[str, str]]:
        if url == root:
            return [(archive, "Results Archive"), *links]
        raise RuntimeError("archive unavailable")

    inventory = discover_document_inventory(
        ir_url=root,
        render=_render,
        fetch_filename=_no_fetch,
        rate_limit_s=0,
        check_robots=False,
    )
    assert len(inventory.candidates) == 12
    assert inventory.crawl_stop_reason == "page_failure"
    assert not inventory.crawl_complete
    assert [page.outcome for page in inventory.pages] == ["succeeded", "failed"]
    assert inventory.pages[1].failure_reason == "RuntimeError"


def test_inventory_requires_explicit_authority_for_cross_host_files() -> None:
    pages = {
        "https://ir.x/": [
            ("https://cdn.x/results/q4-2025.pdf#download", "Q4 2025 Results"),
            ("https://evil.x/q4-2025.pdf", "Q4 2025 Results"),
        ]
    }
    renderer = _Renderer(pages)
    inventory = discover_document_inventory(
        ir_url="https://ir.x/",
        render=renderer,
        fetch_filename=_no_fetch,
        rate_limit_s=0,
        check_robots=False,
        publisher_file_rules=(PublisherEndpointRule(host="cdn.x", path_prefix="/results"),),
    )
    assert [candidate.url for candidate in inventory.candidates] == [
        "https://cdn.x/results/q4-2025.pdf"
    ]
    assert inventory.crawl_complete
    assert not inventory.authority_complete


def test_press_release_listing_page_is_navigation_not_a_document() -> None:
    root = "https://ir.rubrik.com/"
    listing = "https://ir.rubrik.com/news-events/press-releases/default.aspx"
    release = "https://ir.rubrik.com/news-events/press-releases/detail/2026-results.aspx"
    renderer = _Renderer(
        {
            root: [
                (listing, "Press Releases"),
                (release, "Rubrik Announces Fourth Quarter and Fiscal 2026 Results"),
            ],
            listing: [],
        }
    )

    inventory = discover_document_inventory(
        ir_url=root,
        render=renderer,
        fetch_filename=_no_fetch,
        rate_limit_s=0,
        check_robots=False,
    )

    assert [candidate.url for candidate in inventory.candidates] == [release]
    assert listing in renderer.calls


def test_precise_to_candidates_maps_aliases() -> None:
    cands = precise_to_candidates(
        {"deck": "u-deck", "spreadsheet": "u-sheet", "transcript": "u-tx"}, "rc"
    )
    by_url = {c.url: c for c in cands}
    assert by_url["u-deck"].doc_type_guess == "presentation"
    assert by_url["u-sheet"].doc_type_guess == "supplement"
    assert by_url["u-tx"].doc_type_guess == "transcript"


def test_hybrid_merges_precise_without_dup(monkeypatch: pytest.MonkeyPatch) -> None:
    from ir_pipeline import discover as disc
    from ir_pipeline.discover import generic

    def _fake_generic(
        *, ir_url: str, max_quarters: int = 8, timeout_ms: int = 60_000
    ) -> list[CandidateDoc]:
        return [CandidateDoc("u1", "", "f", "press_release", 2025, 3, ir_url)]

    def _fake_precise(config: IrConfig) -> dict[str, str]:
        return {"deck": "u2", "press_release": "u1"}  # u1 is a dup of generic's

    monkeypatch.setattr(generic, "discover_document_history", _fake_generic)
    monkeypatch.setattr(disc, "discover_documents", _fake_precise)

    cfg = IrConfig(ticker="NU", platform="mz", results_center_url="rc")
    docs = discover_history_hybrid(ir_url="https://ir.x/", config=cfg)
    assert {d.url for d in docs} == {"u1", "u2"}


def test_hybrid_generic_only_when_no_mz_config(monkeypatch: pytest.MonkeyPatch) -> None:
    from ir_pipeline.discover import generic

    def _fake_generic(
        *, ir_url: str, max_quarters: int = 8, timeout_ms: int = 60_000
    ) -> list[CandidateDoc]:
        return [CandidateDoc("u1", "", "f", None, None, None, ir_url)]

    monkeypatch.setattr(generic, "discover_document_history", _fake_generic)
    docs = discover_history_hybrid(ir_url="https://ir.x/", config=None)
    assert {d.url for d in docs} == {"u1"}
