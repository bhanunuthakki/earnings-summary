"""Hermetic rendered-DOM/computed-style checks for the design canary."""

from __future__ import annotations

import importlib
import threading
from collections.abc import Generator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Protocol

import pytest

import execution.verify_design_conformance as design_conformance
from execution.design_route_canaries import ROUTE_SCREEN_IDS, write_route_canary_fixtures
from execution.verify_design_conformance import (  # pyright: ignore[reportPrivateUsage]
    CanaryResult,
    RouteCanaryResult,
    _build_receipt,  # pyright: ignore[reportPrivateUsage]
    _route_canary_source,  # pyright: ignore[reportPrivateUsage]
    _route_population_failures,  # pyright: ignore[reportPrivateUsage]
    _scan_canary,  # pyright: ignore[reportPrivateUsage]
    _scan_route_canaries,  # pyright: ignore[reportPrivateUsage]
)
from pipeline.work_os_shell import SCREEN_SPECS
from ui.conformance_scan import scan_surface_evidence

ROOT_TOKENS = """
:root {
  --fs-display: 20px;
  --fs-title: 15px;
  --fs-body: 13px;
  --fs-caption: 11px;
  --radius: 8px;
  --radius-full: 999px;
  --radius-card: 10px;
  --bw-thin: 1px;
  --touch-target-size: 44px;
}
"""

CANONICAL_CSS = """
.k-btn { font-size: var(--fs-body); border-radius: var(--radius); border: var(--bw-thin) solid transparent; }
.k-btn-sm { font-size: var(--fs-caption); border-radius: 2px; border: 1px solid transparent; min-height: 24px; }
.k-chip { font-size: var(--fs-caption); border-radius: var(--radius-full); border: var(--bw-thin) solid currentColor; }
.k-card { border-radius: var(--radius-card); border: var(--bw-thin) solid currentColor; }
.k-well { border-radius: var(--radius); }
.k-overlay { border-radius: var(--radius); border: var(--bw-thin) solid currentColor; }
"""


class _FulfillableRoute(Protocol):
    def fulfill(self, *, body: str, content_type: str) -> object: ...


def _specimen(
    *,
    runtime_override: bool = False,
    delayed_override: bool = False,
    inline_override: bool = False,
    inline_custom_property: bool = False,
) -> str:
    scripts: list[str] = []
    if runtime_override:
        scripts.append(
            "document.styleSheets[0].insertRule('.k-btn { border-radius: ' + '4' + '1' + 'p' + 'x !important; }');"
        )
    if delayed_override:
        scripts.append(
            "setTimeout(() => document.querySelector('.k-btn').style.setProperty('border-radius', '41px', 'important'), 250);"
        )
    if inline_override:
        scripts.append(
            "document.querySelector('.k-btn').style.setProperty('border-radius', '41px', 'important');"
        )
    if inline_custom_property:
        scripts.append("document.documentElement.style.setProperty('--radius', '41px');")
    override = f"<script>{''.join(scripts)}</script>" if scripts else ""
    return f"""<!doctype html>
<html><head><style>{ROOT_TOKENS}{CANONICAL_CSS}</style></head>
<body>
  <button class="k-btn k-btn-primary">Run</button>
  <button class="k-btn k-btn-sm">Small</button>
  <span class="k-chip">Ready</span>
  <section class="k-card">Card</section>
  <aside class="k-well">Well</aside>
  <div class="k-overlay">Overlay</div>
  {override}
</body></html>"""


@pytest.fixture()
def specimen_server() -> Generator[tuple[ThreadingHTTPServer, str, list[str]], None, None]:
    payload = [_specimen()]

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(payload[0].encode("utf-8"))

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    address = server.server_address
    host = str(address[0])
    port = int(address[1])
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, f"http://{host}:{port}/specimen", payload
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_browser_canary_passes_canonical_rendered_specimen(
    specimen_server: tuple[ThreadingHTTPServer, str, list[str]],
) -> None:
    _playwright_or_skip()
    _server, url, _payload = specimen_server
    result = _scan_canary(url, browser_canary=True)
    assert result.status == "passed", result
    assert result.findings == ()


def test_browser_canary_confirms_cssom_override_static_guard_also_catches(
    specimen_server: tuple[ThreadingHTTPServer, str, list[str]],
) -> None:
    _playwright_or_skip()
    _server, url, payload = specimen_server
    # Swap the response body without changing the route or transport.  The
    # The override is inserted into CSSOM after navigation. The strengthened
    # source guard now rejects the mutation API itself, while the browser
    # canary independently confirms the resulting computed-style drift.
    payload[0] = _specimen(runtime_override=True)
    static_evidence = scan_surface_evidence("<canary>", _specimen(runtime_override=True))
    assert "runtime-visual-mutation" in static_evidence.violations()
    result = _scan_canary(url, browser_canary=True)
    assert result.status == "failed"
    assert any(".k-btn" in finding and "border-radius" in finding for finding in result.findings)


def test_browser_canary_catches_delayed_primitive_mutation(
    specimen_server: tuple[ThreadingHTTPServer, str, list[str]],
) -> None:
    _playwright_or_skip()
    _server, url, payload = specimen_server
    payload[0] = _specimen(delayed_override=True)
    static_evidence = scan_surface_evidence("<canary>", payload[0])
    assert static_evidence.violations()["runtime-visual-mutation"]
    result = _scan_canary(url, browser_canary=True)
    assert result.status == "failed"
    assert any("border-radius" in finding for finding in result.findings)


def test_browser_canary_catches_evil_inline_style_and_custom_property(
    specimen_server: tuple[ThreadingHTTPServer, str, list[str]],
) -> None:
    _playwright_or_skip()
    _server, url, payload = specimen_server
    payload[0] = _specimen(inline_override=True, inline_custom_property=True)
    static_evidence = scan_surface_evidence("<canary>", payload[0])
    assert static_evidence.violations()["runtime-visual-mutation"]
    result = _scan_canary(url, browser_canary=True)
    assert result.status == "failed"
    assert any("inline border-" in finding and "radius" in finding for finding in result.findings)
    assert any("inline --radius" in finding for finding in result.findings)


def test_route_canary_matrix_covers_all_required_routes_and_viewports() -> None:
    """The hosted gate must exercise every required fixture at both widths."""

    _require_playwright()
    results = _scan_route_canaries()
    assert len(results) == len(ROUTE_SCREEN_IDS) * 2
    assert {(item.route, item.viewport) for item in results} == {
        (route, viewport) for route in ROUTE_SCREEN_IDS for viewport in ("desktop", "narrow")
    }
    assert all(item.status == "passed" for item in results), results


def test_guarded_persistent_routes_exactly_match_the_production_screen_registry() -> None:
    guarded = {screen_id for route, screen_id in ROUTE_SCREEN_IDS.items() if route != "full-brief"}
    assert guarded == {screen.screen_id for screen in SCREEN_SPECS}


def test_performance_canary_contains_deterministic_index_benchmarking_without_driver_grid() -> None:
    from execution.design_route_canaries import canary_portfolio_fragment

    html = canary_portfolio_fragment("performance", None)
    assert "Index Benchmarking" in html
    assert '<details class="pf-alpha-details">' not in html


def test_cockpit_canary_uses_the_current_portfolio_hydration_contract() -> None:
    from execution.design_route_canaries import render_route_canary

    html = render_route_canary(route="performance", viewport="desktop")

    assert '"status": "ok"' in html
    assert '"tracker_state": "current"' in html
    assert '"tracker_detail": "Tracker connected \\u00b7 current \\u00b7 As of 2026-01-01"' in html
    assert '"generated_at": "2026-01-01T00:00:00Z"' in html
    assert '"warnings": []' in html


@pytest.mark.parametrize(
    ("route", "stat_key", "screen_id", "mount_id", "endpoint"),
    (
        (
            "performance",
            "performance",
            "screen-performance",
            "workOsPerformanceMount",
            "/api/panel/performance_risk",
        ),
    ),
)
@pytest.mark.parametrize("viewport", [(1440, 900), (390, 844)])
def test_cockpit_stat_links_use_native_hash_navigation_without_layout_shift(
    route: str,
    stat_key: str,
    screen_id: str,
    mount_id: str,
    endpoint: str,
    viewport: tuple[int, int],
) -> None:
    """The keyed Cockpit controls remain truthful and keyboard-reachable at both layouts."""

    _require_playwright()
    playwright_api = importlib.import_module("playwright.sync_api")
    from execution.design_route_canaries import render_route_canary

    html = render_route_canary(route=route, viewport="desktop" if viewport[0] > 400 else "narrow")
    with playwright_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": viewport[0], "height": viewport[1]},
            reduced_motion="reduce",
        )
        try:
            page = context.new_page()
            page_errors: list[str] = []

            def collect_page_error(error: object) -> None:
                page_errors.append(str(error))

            page.on("pageerror", collect_page_error)

            def serve_canary(route: _FulfillableRoute) -> None:
                route.fulfill(body=html, content_type="text/html")

            page.route("http://design-canary.invalid/", serve_canary)
            page.goto("http://design-canary.invalid/", wait_until="load")
            page.wait_for_function(
                """() => document.getElementById('workOsLiveStatus')?.textContent ===
                'Tracker connected · current · As of 2026-01-01'"""
            )

            stats = page.locator("#workOsPortfolioStats")
            assert stats.locator(".k-stat-cell").count() == 4
            assert stats.locator("a[data-work-os-stat-key]").count() == 2
            assert stats.locator("div[data-work-os-stat-key]").count() == 2
            assert (
                stats.locator("[data-work-os-stat-key='nav'] .stat-subtext").text_content()
                == "Tracker connected · current · As of 2026-01-01"
            )

            control = stats.locator(f"a[data-work-os-stat-key='{stat_key}']")
            before_focus = control.bounding_box()
            assert before_focus is not None
            control.focus()
            assert control.evaluate("element => document.activeElement === element")
            assert control.bounding_box() == before_focus
            control.press("Enter")

            page.wait_for_function(
                "screenId => window.location.hash === '#' + screenId",
                arg=screen_id,
            )
            page.wait_for_function(
                "screenId => document.getElementById(screenId)?.classList.contains('is-active')",
                arg=screen_id,
            )
            page.wait_for_function(
                "([id, expected]) => document.getElementById(id)?.dataset.loadedEndpoint === expected",
                arg=[mount_id, endpoint],
            )
            assert page_errors == []
        finally:
            context.close()
            browser.close()


@pytest.mark.parametrize("viewport", [(1440, 900), (390, 844)])
def test_full_brief_canary_uses_production_loader_and_controls_shadow_content(
    viewport: tuple[int, int],
) -> None:
    """A persisted artifact must be interactive across the real shadow boundary."""

    _require_playwright()
    playwright_api = importlib.import_module("playwright.sync_api")
    from execution.design_route_canaries import render_route_canary

    html = render_route_canary(route="full-brief", viewport="desktop")
    with playwright_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            context = browser.new_context(
                viewport={"width": viewport[0], "height": viewport[1]},
                reduced_motion="reduce",
            )
            page = context.new_page()
            page.set_content(html, wait_until="load")
            page.wait_for_selector("#workOsBriefReader .work-os-report-host", state="visible")
            buttons = page.locator("#workOsBriefReaderSections .work-os-reader-group-button")
            assert buttons.count() == 6
            assert buttons.all_text_contents() == [
                "Overview & Moat",
                "Quarter & Guidance",
                "Financials & DCF",
                "Thesis & Risk",
                "Valuation & Comps",
                "Sources & Citations",
            ]

            def visible_reader_state() -> dict[str, list[str]]:
                return page.locator(".work-os-report-host").evaluate(
                    """
                    host => {
                      const groups = [...host.shadowRoot.querySelectorAll('.tab-group-pane[data-tab-group]')];
                      const sections = [...host.shadowRoot.querySelectorAll('.subtab-pane[data-tab]')];
                      return {
                            groups: groups.filter(node => node.getClientRects().length > 0).map(node => node.dataset.tabGroup),
                            sections: sections.filter(node => node.getClientRects().length > 0).map(node => node.dataset.tab),
                      };
                    }
                    """
                )

            page.wait_for_function(
                "getComputedStyle(document.querySelector('.work-os-report-host').shadowRoot.querySelector('[data-reader-group-active=\"false\"]')).display === 'none'"
            )
            initial = visible_reader_state()
            assert initial["groups"] == ["overview"]
            assert len(initial["sections"]) == 1

            page.evaluate(
                """
                window.__readerScrollBehaviors = [];
                Element.prototype.scrollIntoView = function(options) {
                  window.__readerScrollBehaviors.push(options && options.behavior);
                };
                """
            )

            page.locator('button[data-group-id="quarter"]').click()
            quarter = visible_reader_state()
            assert quarter["groups"] == ["quarter"]
            assert quarter["sections"] == ["earnings"]
            assert (
                page.locator('button[data-group-id="quarter"]').get_attribute("aria-current")
                == "location"
            )

            page.locator('button[data-section-id="news"]').click()
            news = visible_reader_state()
            assert news["groups"] == ["quarter"]
            assert news["sections"] == ["news"]
            assert page.evaluate("window.__readerScrollBehaviors.filter(Boolean)") == [
                "auto",
                "auto",
            ]
            assert (
                page.locator('button[data-section-id="news"]').get_attribute("aria-current")
                == "location"
            )
        finally:
            browser.close()


def test_fact_playground_canary_renders_production_panel_without_stylesheet_leak() -> None:
    """The real Explore fragment must not expose canonical CSS as page text."""

    _require_playwright()
    playwright_api = importlib.import_module("playwright.sync_api")
    from execution.design_route_canaries import render_route_canary

    html = render_route_canary(route="fact-metric-playground", viewport="desktop")
    with playwright_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            page.set_content(html, wait_until="load")
            page.locator("#screen-cockpit").evaluate(
                """
                node => {
                  const link = document.createElement('a');
                  link.href = '#design-canary-non-viewer';
                  link.dataset.designCanaryNonViewerLink = '1';
                  link.textContent = 'Canary non-viewer link';
                  node.appendChild(link);
                }
                """
            )
            non_viewer_link = page.locator("[data-design-canary-non-viewer-link]")
            global_style_before = non_viewer_link.evaluate(
                "node => ({bodyLineHeight: getComputedStyle(document.body).lineHeight, linkColor: getComputedStyle(node).color})"
            )
            page.evaluate("window.navigateTo('screen-analytics-playground', {fromHistory: true})")
            page.wait_for_selector("#workOsFactPlayground #vx-root", state="visible")
            visible_text = page.locator("#screen-analytics-playground").inner_text()
            assert "The provenance substrate" not in visible_text
            assert "render_transcript_page" not in visible_text
            assert ".cc-score-head {" not in visible_text
            page.evaluate("window.navigateTo('screen-cockpit', {fromHistory: true})")
            global_style_after = non_viewer_link.evaluate(
                "node => ({bodyLineHeight: getComputedStyle(document.body).lineHeight, linkColor: getComputedStyle(node).color})"
            )
            assert global_style_after == global_style_before
        finally:
            browser.close()


def test_route_canary_population_is_an_exact_fail_closed_census() -> None:
    results = tuple(
        RouteCanaryResult(
            route=route,
            viewport=viewport,
            fixture=f"{route}.{viewport}.html",
            status="passed",
        )
        for route in ROUTE_SCREEN_IDS
        for viewport in ("desktop", "narrow")
    )
    assert _route_population_failures(results) == ()

    missing = _route_population_failures(results[:-1])
    assert len(missing) == 1
    assert missing[0].route == "full-brief"
    assert missing[0].viewport == "narrow"
    assert missing[0].status == "unavailable"

    duplicated = _route_population_failures((*results, results[0]))
    assert len(duplicated) == 1
    assert duplicated[0].status == "failed"
    assert "duplicate" in str(duplicated[0].reason)


def test_receipt_fails_when_route_canary_population_shrinks(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    results = tuple(
        RouteCanaryResult(
            route=route,
            viewport=viewport,
            fixture=f"{route}.{viewport}.html",
            status="passed",
        )
        for route in ROUTE_SCREEN_IDS
        for viewport in ("desktop", "narrow")
    )

    def fake_static(_source_root: Path) -> tuple[object, ...]:
        return ((), (), (), (), (), (), (), (), 0, (), "clean")

    def fake_canary(_url: str | None, *, browser_canary: bool = False) -> CanaryResult:
        del browser_canary
        return CanaryResult(status="skipped:not-requested")

    def fake_routes(_project_root: Path) -> tuple[RouteCanaryResult, ...]:
        return results[:-1]

    monkeypatch.setattr(
        design_conformance,
        "_scan_static",
        fake_static,
    )
    monkeypatch.setattr(
        design_conformance,
        "_scan_canary",
        fake_canary,
    )
    monkeypatch.setattr(
        design_conformance,
        "_scan_route_canaries",
        fake_routes,
    )

    receipt = _build_receipt(tmp_path / "src", None, route_canaries=True)

    assert receipt.verdict == "fail"
    assert any(
        result.route == "full-brief"
        and result.viewport == "narrow"
        and result.status == "unavailable"
        for result in receipt.route_canaries
    )


def test_normal_route_canary_source_ignores_stale_committed_fixture(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    stale = tmp_path / "tests" / "fixtures" / "design_canaries" / "cockpit.desktop.html"
    stale.parent.mkdir(parents=True)
    stale.write_text("stale fixture", encoding="utf-8")

    def fake_render(*, route: str, viewport: str) -> str:
        return f"production:{route}:{viewport}"

    monkeypatch.setattr(
        design_conformance,
        "render_route_canary",
        fake_render,
    )

    assert _route_canary_source("cockpit", "desktop", None) == "production:cockpit:desktop"
    assert _route_canary_source("cockpit", "desktop", tmp_path) == "stale fixture"


def test_route_canary_rejects_freehand_visual_override(tmp_path: Path) -> None:
    _require_playwright()
    root = _copy_route_fixtures(tmp_path)
    target = root / "tests" / "fixtures" / "design_canaries" / "company-desk.desktop.html"
    target.write_text(
        target.read_text(encoding="utf-8").replace(
            "</style>", ".company-picker-trigger { border-radius: 41px !important; } </style>"
        ),
        encoding="utf-8",
    )
    result = next(
        item
        for item in _scan_route_canaries(fixture_root=root)
        if item.route == "company-desk" and item.viewport == "desktop"
    )
    assert result.status == "failed"
    assert any("border-radius" in finding for finding in result.findings)


@pytest.mark.parametrize(
    ("override", "needle"),
    [
        (".k-card-section { padding: var(--sp-5) !important; }", "padding-top"),
        (".k-card-title { font-size: var(--fs-body) !important; }", "title[0] font-size"),
        (".k-card-head { align-items: center !important; }", "header alignment"),
        (".k-card { box-shadow: none !important; }", "box-shadow"),
        (
            ".research-toolbar .k-card-title { transform: translateY(120px) !important; }",
            "upper title zone",
        ),
    ],
)
def test_route_canary_rejects_deep_card_contract_drift(
    tmp_path: Path, override: str, needle: str
) -> None:
    _require_playwright()
    root = _copy_route_fixtures(tmp_path)
    target = root / "tests" / "fixtures" / "design_canaries" / "company-desk.desktop.html"
    target.write_text(
        target.read_text(encoding="utf-8").replace("</style>", override + "</style>"),
        encoding="utf-8",
    )
    result = next(
        item
        for item in _scan_route_canaries(fixture_root=root)
        if item.route == "company-desk" and item.viewport == "desktop"
    )
    assert result.status == "failed"
    assert any(needle in finding for finding in result.findings), result.findings


def test_route_canary_rejects_untyped_or_overflowing_card(tmp_path: Path) -> None:
    _require_playwright()
    root = _copy_route_fixtures(tmp_path)
    desktop = root / "tests" / "fixtures" / "design_canaries" / "company-desk.desktop.html"
    desktop.write_text(
        desktop.read_text(encoding="utf-8").replace(
            '<div class="research-screen" id="workOsCompanyDesk" aria-live="polite">\n'
            '    <header class="k-card k-card-section research-toolbar">',
            '<div class="research-screen" id="workOsCompanyDesk" aria-live="polite">\n'
            '    <header class="k-card research-toolbar">',
            1,
        ),
        encoding="utf-8",
    )
    narrow = root / "tests" / "fixtures" / "design_canaries" / "cockpit.narrow.html"
    narrow.write_text(
        narrow.read_text(encoding="utf-8").replace(
            "</style>", "#screen-cockpit .k-card { width: 900px !important; }</style>"
        ),
        encoding="utf-8",
    )
    results = _scan_route_canaries(fixture_root=root)
    company = next(
        item for item in results if item.route == "company-desk" and item.viewport == "desktop"
    )
    cockpit = next(
        item for item in results if item.route == "cockpit" and item.viewport == "narrow"
    )
    assert any("exactly one archetype" in finding for finding in company.findings)
    assert any("overflows viewport" in finding for finding in cockpit.findings)


def test_route_canary_rejects_nested_unregistered_boxed_card(tmp_path: Path) -> None:
    _require_playwright()
    root = _copy_route_fixtures(tmp_path)
    target = root / "tests" / "fixtures" / "design_canaries" / "company-desk.desktop.html"
    markup = target.read_text(encoding="utf-8")
    markup = markup.replace(
        '<h2 class="k-card-title">Thesis contracts</h2>',
        '<div class="legacy-card">Rogue nested card</div>'
        '<h2 class="k-card-title">Thesis contracts</h2>',
        1,
    ).replace(
        "</style>",
        ".legacy-card { background: var(--surface); border: var(--bw-thin) solid var(--border); border-radius: var(--radius-card); }</style>",
    )
    target.write_text(markup, encoding="utf-8")
    result = next(
        item
        for item in _scan_route_canaries(fixture_root=root)
        if item.route == "company-desk" and item.viewport == "desktop"
    )
    assert any("unregistered boxed card candidate" in finding for finding in result.findings)


def test_route_canary_rejects_unnamed_semantic_box_and_shadow_card(tmp_path: Path) -> None:
    _require_playwright()
    root = _copy_route_fixtures(tmp_path)
    target = root / "tests" / "fixtures" / "design_canaries" / "company-desk.desktop.html"
    target.write_text(
        target.read_text(encoding="utf-8").replace(
            "</body>",
            """
<style>.rogue { background:var(--surface);border:var(--bw-thin) solid var(--border);border-radius:var(--radius-card); }</style>
<script>
const route = document.getElementById('screen-workspace');
const unnamed = document.createElement('article');
unnamed.className = 'rogue'; unnamed.textContent = 'Unnamed boxed semantic surface';
route.appendChild(unnamed);
const host = document.createElement('div'); route.appendChild(host);
const shadow = host.attachShadow({mode:'open'});
shadow.innerHTML = '<style>.rogue{background:var(--surface);border:var(--bw-thin) solid var(--border);border-radius:var(--radius-card)}</style><article class="rogue">Shadow boxed surface</article>';
</script></body>
""",
        ),
        encoding="utf-8",
    )

    result = next(
        item
        for item in _scan_route_canaries(fixture_root=root)
        if item.route == "company-desk" and item.viewport == "desktop"
    )
    assert result.status == "failed"
    assert sum("unregistered boxed card candidate" in item for item in result.findings) >= 2


def test_outer_card_cannot_borrow_nested_card_title(tmp_path: Path) -> None:
    _require_playwright()
    root = _copy_route_fixtures(tmp_path)
    target = root / "tests" / "fixtures" / "design_canaries" / "company-desk.desktop.html"
    target.write_text(
        target.read_text(encoding="utf-8").replace(
            '<div class="research-grid k-grid-split-rail-lg">',
            '<section class="k-card k-card-section">'
            '<section class="k-card k-card-section"><h2 class="k-card-title">Nested only</h2></section>'
            '</section><div class="research-grid k-grid-split-rail-lg">',
            1,
        ),
        encoding="utf-8",
    )

    result = next(
        item
        for item in _scan_route_canaries(fixture_root=root)
        if item.route == "company-desk" and item.viewport == "desktop"
    )
    assert result.status == "failed"
    assert any("missing visible title" in finding for finding in result.findings)


def test_route_canary_rejects_card_motion_under_reduced_motion(tmp_path: Path) -> None:
    _require_playwright()
    root = _copy_route_fixtures(tmp_path)
    target = root / "tests" / "fixtures" / "design_canaries" / "cockpit.desktop.html"
    target.write_text(
        target.read_text(encoding="utf-8").replace(
            "</style>", ".k-card { transition-duration: 1s !important; }</style>"
        ),
        encoding="utf-8",
    )
    result = next(
        item
        for item in _scan_route_canaries(fixture_root=root)
        if item.route == "cockpit" and item.viewport == "desktop"
    )
    assert any("descendant motion is not reduced" in finding for finding in result.findings)


def test_route_canary_rejects_unresolved_visible_loading_shell(tmp_path: Path) -> None:
    _require_playwright()
    root = _copy_route_fixtures(tmp_path)
    target = root / "tests" / "fixtures" / "design_canaries" / "company-desk.desktop.html"
    target.write_text(
        target.read_text(encoding="utf-8").replace(
            '<div class="research-grid k-grid-split-rail-lg">',
            '<p class="cc-loading" hx-get="/api/panel/never">Loading forever…</p>'
            '<div class="research-grid k-grid-split-rail-lg">',
            1,
        ),
        encoding="utf-8",
    )
    result = next(
        item
        for item in _scan_route_canaries(fixture_root=root)
        if item.route == "company-desk" and item.viewport == "desktop"
    )
    assert result.status == "failed"
    assert any("unresolved visible loading shell" in finding for finding in result.findings)


def test_route_canary_rejects_clipped_or_occluded_overlay(tmp_path: Path) -> None:
    _require_playwright()
    root = _copy_route_fixtures(tmp_path)
    target = root / "tests" / "fixtures" / "design_canaries" / "company-desk.narrow.html"
    target.write_text(
        target.read_text(encoding="utf-8").replace(
            "</style>", "#companyPickerPopover { left: -500px !important; } </style>"
        ),
        encoding="utf-8",
    )
    result = next(
        item
        for item in _scan_route_canaries(fixture_root=root)
        if item.route == "company-desk" and item.viewport == "narrow"
    )
    assert result.status == "failed"
    assert any("occluded" in finding or "clipped" in finding for finding in result.findings)


def test_route_canary_rejects_delayed_runtime_mutation(tmp_path: Path) -> None:
    _require_playwright()
    root = _copy_route_fixtures(tmp_path)
    target = root / "tests" / "fixtures" / "design_canaries" / "full-brief.desktop.html"
    target.write_text(
        target.read_text(encoding="utf-8").replace(
            "</body>",
            "<script>setTimeout(() => document.querySelector('#workOsBriefReader button.k-btn').style.setProperty('border-radius', '41px', 'important'), 250);</script></body>",
        ),
        encoding="utf-8",
    )
    result = next(
        item
        for item in _scan_route_canaries(fixture_root=root)
        if item.route == "full-brief" and item.viewport == "desktop"
    )
    assert result.status == "failed"
    assert any("border-radius" in finding for finding in result.findings)


def _copy_route_fixtures(tmp_path: Path) -> Path:
    write_route_canary_fixtures(tmp_path)
    return tmp_path


def _require_playwright() -> None:
    try:
        playwright_api = importlib.import_module("playwright.sync_api")
        with playwright_api.sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            browser.close()
    except Exception as exc:  # pragma: no cover - environment-dependent
        pytest.fail(f"Playwright Chromium unavailable: {type(exc).__name__}")


def _playwright_or_skip() -> None:
    pytest.importorskip("playwright")
    try:
        playwright_api = importlib.import_module("playwright.sync_api")
        with playwright_api.sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            browser.close()
    except Exception as exc:  # pragma: no cover - environment-dependent
        pytest.skip(f"Playwright Chromium unavailable: {type(exc).__name__}")
