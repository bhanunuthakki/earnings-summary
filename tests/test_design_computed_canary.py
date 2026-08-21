"""Hermetic rendered-DOM/computed-style checks for the design canary."""

from __future__ import annotations

import importlib
import threading
from collections.abc import Generator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from execution.design_route_canaries import write_route_canary_fixtures
from execution.verify_design_conformance import (  # pyright: ignore[reportPrivateUsage]
    _scan_canary,  # pyright: ignore[reportPrivateUsage]
    _scan_route_canaries,  # pyright: ignore[reportPrivateUsage]
)
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
    assert len(results) == 10
    assert {(item.route, item.viewport) for item in results} == {
        (route, viewport)
        for route in (
            "cockpit",
            "company-desk",
            "fact-metric-playground",
            "operations",
            "full-brief",
        )
        for viewport in ("desktop", "narrow")
    }
    assert all(item.status == "passed" for item in results), results


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
            page = browser.new_page(viewport={"width": viewport[0], "height": viewport[1]})
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
            assert (
                page.locator('button[data-section-id="news"]').get_attribute("aria-current")
                == "location"
            )
        finally:
            browser.close()


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
        for item in _scan_route_canaries(root)
        if item.route == "company-desk" and item.viewport == "desktop"
    )
    assert result.status == "failed"
    assert any("border-radius" in finding for finding in result.findings)


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
        for item in _scan_route_canaries(root)
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
        for item in _scan_route_canaries(root)
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
