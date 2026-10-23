"""Tests for the generic headless IR-document crawler (PR2).

The Playwright call is injected (no browser): a fake renderer maps a page URL to
its ``[(href, text)]`` anchors. These lock the link harvest (doc-like links
only), bounded same-host history following, Tier-1 period / doc-type attribution,
``max_quarters`` truncation, resilience (dead/empty page → ``[]``), and the
hybrid merge with a precise adapter.
"""

from __future__ import annotations

import pytest

import ir_pipeline.discover as discovery
from ir_pipeline.authority import PublisherEndpointRule
from ir_pipeline.config import IrConfig
from ir_pipeline.discover import (
    IrDiscoveryAuthenticationDeniedError,
    discover_history_hybrid,
    generic,
)
from ir_pipeline.discover._docmeta import CandidateDoc
from ir_pipeline.discover.generic import (
    CrawlPageOutcome,
    DocumentDiscoveryInventory,
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


def test_harvests_only_document_links(monkeypatch: pytest.MonkeyPatch) -> None:
    def public_url(url: str) -> str:
        return url

    monkeypatch.setattr("ir_pipeline.discover.generic.ensure_safe_public_url", public_url)
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


def test_harvest_normalizes_relative_urls_and_fragments() -> None:
    pages = {
        "https://ir.x/results/index.html": [
            ("../files/q3-2025.pdf", "Q3 2025 Results"),
            ("https://ir.x/files/q3-2025.pdf#download", "Q3 duplicate"),
        ]
    }

    docs, _ = _run(pages, ir_url="https://ir.x/results/index.html")

    assert [doc.url for doc in docs] == ["https://ir.x/files/q3-2025.pdf"]


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


def test_follows_same_host_history_navigation() -> None:
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
        *, ir_url: str, timeout_ms: int = 60_000, **kwargs: object
    ) -> DocumentDiscoveryInventory:
        return DocumentDiscoveryInventory(
            (CandidateDoc("u1", "", "f", "press_release", 2025, 3, ir_url),),
            (),
            True,
            "frontier_exhausted",
        )

    def _fake_precise(config: IrConfig) -> dict[str, str]:
        return {"deck": "u2", "press_release": "u1"}  # u1 is a dup of generic's

    monkeypatch.setattr(generic, "discover_document_inventory", _fake_generic)
    monkeypatch.setattr(disc, "discover_documents", _fake_precise)

    cfg = IrConfig(ticker="NU", platform="mz", results_center_url="rc")
    docs = discover_history_hybrid(ir_url="https://ir.x/", config=cfg)
    assert {d.url for d in docs} == {"u1", "u2"}


def test_hybrid_generic_only_when_no_mz_config(monkeypatch: pytest.MonkeyPatch) -> None:
    from ir_pipeline.discover import generic

    def _fake_generic(
        *, ir_url: str, timeout_ms: int = 60_000, **kwargs: object
    ) -> DocumentDiscoveryInventory:
        return DocumentDiscoveryInventory(
            (CandidateDoc("u1", "", "f", None, None, None, ir_url),), (), True, "frontier_exhausted"
        )

    monkeypatch.setattr(generic, "discover_document_inventory", _fake_generic)
    docs = discover_history_hybrid(ir_url="https://ir.x/", config=None)
    assert {d.url for d in docs} == {"u1"}


@pytest.mark.parametrize("inventory", [False, True])
def test_generic_renderer_auth_denial_is_never_swallowed(inventory: bool) -> None:
    from ir_pipeline.discover import generic

    def denied(_url: str, _timeout: int) -> list[tuple[str, str]]:
        raise IrDiscoveryAuthenticationDeniedError(403)

    with pytest.raises(IrDiscoveryAuthenticationDeniedError):
        if inventory:
            generic.discover_document_inventory(
                ir_url="https://ir.example/",
                render=denied,
                check_robots=False,
            )
        else:
            generic.discover_document_history(
                ir_url="https://ir.example/",
                render=denied,
                check_robots=False,
            )


def test_hybrid_does_not_swallow_mz_auth_denial(monkeypatch: pytest.MonkeyPatch) -> None:
    from ir_pipeline.config import IrConfig
    from ir_pipeline.discover import generic

    cfg = IrConfig(
        ticker="NU",
        platform="mz",
        results_center_url="https://ir.example/",
    )

    def no_generic_history(**_kwargs: object) -> DocumentDiscoveryInventory:
        return DocumentDiscoveryInventory((), (), True, "frontier_exhausted")

    def denied_mz(_cfg: IrConfig) -> dict[str, str]:
        raise IrDiscoveryAuthenticationDeniedError(403)

    monkeypatch.setattr(generic, "discover_document_inventory", no_generic_history)
    monkeypatch.setattr(
        "ir_pipeline.discover.discover_documents",
        denied_mz,
    )

    with pytest.raises(IrDiscoveryAuthenticationDeniedError):
        discover_history_hybrid(ir_url="https://ir.example/", config=cfg)


def test_legacy_history_retains_official_html_release() -> None:
    url = "https://ir.x/news/q3-2025-earnings-release"
    docs, renderer = _run({"https://ir.x/": [(url, "Q3 2025 Earnings Release")]})
    assert [candidate.url for candidate in docs] == [url]
    assert docs[0].doc_type_guess == "press_release"
    assert renderer.calls == ["https://ir.x/"]


def test_legacy_history_calls_inventory_once_and_forwards_limits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ir_pipeline.discover import generic

    calls: list[dict[str, object]] = []
    renderer = _Renderer({})

    def inventory(**kwargs: object) -> DocumentDiscoveryInventory:
        calls.append(kwargs)
        return DocumentDiscoveryInventory((), (), False, "time_budget_exhausted")

    monkeypatch.setattr(generic, "discover_document_inventory", inventory)
    assert (
        generic.discover_document_history(
            ir_url="https://ir.x/",
            max_quarters=1,
            timeout_ms=123,
            render=renderer,
            fetch_filename=_no_fetch,
            rate_limit_s=0.2,
            check_robots=False,
            time_budget_s=2.5,
        )
        == []
    )
    assert calls == [
        {
            "ir_url": "https://ir.x/",
            "timeout_ms": 123,
            "render": renderer,
            "fetch_filename": _no_fetch,
            "rate_limit_s": 0.2,
            "check_robots": False,
            "time_budget_s": 2.5,
            "candidate_link_scope": "observed_binary",
        }
    ]


def test_strict_inventory_excludes_unapproved_external_documents() -> None:
    renderer = _Renderer({"https://ir.x/": [("https://cdn.x/results.pdf", "Q3 2025 Results")]})
    inventory = discover_document_inventory(
        ir_url="https://ir.x/",
        render=renderer,
        fetch_filename=_no_fetch,
        check_robots=False,
        rate_limit_s=0,
    )
    assert inventory.candidates == ()
    assert inventory.authority_complete is False


@pytest.mark.parametrize(
    "target",
    [
        "http://cdn.x/results.pdf",
        "https://user:pass@cdn.x/results.pdf",
        "https://cdn.x/news/q3-2025-earnings-release",
        "https://127.0.0.1/results.pdf",
    ],
)
def test_observed_candidate_scope_rejects_unsafe_or_external_html(target: str) -> None:
    docs, renderer = _run({"https://ir.x/": [(target, "Q3 2025 Earnings Release")]})
    assert docs == []
    assert renderer.calls == ["https://ir.x/"]


def test_compatibility_wrapper_uses_inventory_once(monkeypatch: pytest.MonkeyPatch) -> None:
    candidates = (
        CandidateDoc("https://x/new.pdf", "", "new.pdf", "press_release", 2025, 3, "https://x/"),
        CandidateDoc("https://x/old.pdf", "", "old.pdf", "press_release", 2024, 3, "https://x/"),
    )
    calls: list[dict[str, object]] = []

    def inventory(**kwargs: object) -> DocumentDiscoveryInventory:
        calls.append(kwargs)
        return DocumentDiscoveryInventory(candidates, (), False, "page_budget_exhausted")

    monkeypatch.setattr(generic, "discover_document_inventory", inventory)

    def legacy(**kwargs: object) -> list[CandidateDoc]:
        pytest.fail("legacy crawler called")

    monkeypatch.setattr(generic, "discover_document_history", legacy)
    assert discovery.discover_history_hybrid(ir_url="https://x/", max_quarters=1) == [candidates[0]]
    assert len(calls) == 1


def test_precise_failure_keeps_candidates_and_marks_incomplete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = CandidateDoc("https://x/a.pdf", "", "a.pdf", "press_release", 2025, 3, "https://x/")
    original = DocumentDiscoveryInventory(
        (candidate,), (CrawlPageOutcome("https://x/", "succeeded", 1),), True, "frontier_exhausted"
    )

    def inventory(**kwargs: object) -> DocumentDiscoveryInventory:
        return original

    monkeypatch.setattr(generic, "discover_document_inventory", inventory)

    def fail(config: IrConfig) -> dict[str, str]:
        raise TimeoutError("not retained")

    monkeypatch.setattr(discovery, "discover_documents", fail)
    config = IrConfig(ticker="NU", platform="mz", results_center_url="https://x/")
    result = discovery.discover_inventory_hybrid(ir_url="https://x/", config=config)
    assert result.candidates == (candidate,)
    assert result.crawl_complete is False
    assert result.pages[-1].failure_reason == "precise_adapter:TimeoutError"


def test_precise_merge_preserves_generic_failure_receipt(monkeypatch: pytest.MonkeyPatch) -> None:
    candidate = CandidateDoc("https://x/a.pdf", "", "a.pdf", "press_release", 2025, 3, "https://x/")
    original = DocumentDiscoveryInventory((candidate,), (), False, "page_budget_exhausted")

    def inventory(**kwargs: object) -> DocumentDiscoveryInventory:
        return original

    monkeypatch.setattr(generic, "discover_document_inventory", inventory)

    def precise(config: IrConfig) -> dict[str, str]:
        return {"press_release": candidate.url, "deck": "https://x/deck.pdf"}

    monkeypatch.setattr(discovery, "discover_documents", precise)
    config = IrConfig(ticker="NU", platform="mz", results_center_url="https://x/")
    result = discovery.discover_inventory_hybrid(ir_url="https://x/", config=config)
    assert {item.url for item in result.candidates} == {candidate.url, "https://x/deck.pdf"}
    assert result.crawl_stop_reason == "page_budget_exhausted"
    assert result.crawl_complete is False


@pytest.mark.parametrize("status", [401, 403])
def test_precise_authentication_denial_remains_loud(
    monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    def inventory(**kwargs: object) -> DocumentDiscoveryInventory:
        return DocumentDiscoveryInventory((), (), True, "frontier_exhausted")

    monkeypatch.setattr(generic, "discover_document_inventory", inventory)

    def deny(config: IrConfig) -> dict[str, str]:
        raise discovery.IrDiscoveryAuthenticationDeniedError(status)

    monkeypatch.setattr(discovery, "discover_documents", deny)
    config = IrConfig(ticker="NU", platform="mz", results_center_url="https://x/")
    with pytest.raises(discovery.IrDiscoveryAuthenticationDeniedError):
        discovery.discover_inventory_hybrid(ir_url="https://x/", config=config)


def test_hybrid_retains_observed_cdn_binary_without_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = generic.discover_document_inventory
    calls: list[dict[str, object]] = []

    def public_url(url: str) -> str:
        return url

    monkeypatch.setattr(generic, "ensure_safe_public_url", public_url)

    def render(url: str, timeout_ms: int) -> list[tuple[str, str]]:
        return [
            ("https://cdn.x/q3-2025.pdf", "Q3 2025 Press Release"),
            ("https://cdn.x/news/q3-2025-earnings-release", "Q3 2025 Earnings Release"),
        ]

    def inventory(**kwargs: object) -> DocumentDiscoveryInventory:
        calls.append(kwargs)
        return original(
            ir_url="https://ir.x/",
            render=render,
            check_robots=False,
            rate_limit_s=0,
            candidate_link_scope="observed_binary",
        )

    monkeypatch.setattr(generic, "discover_document_inventory", inventory)
    result = discovery.discover_inventory_hybrid(ir_url="https://ir.x/")
    assert [candidate.url for candidate in result.candidates] == ["https://cdn.x/q3-2025.pdf"]
    assert result.authority_complete is False
    assert len(calls) == 1
    assert calls[0]["candidate_link_scope"] == "observed_binary"
