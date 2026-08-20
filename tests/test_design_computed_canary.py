"""Hermetic rendered-DOM/computed-style checks for the design canary."""

from __future__ import annotations

import importlib
import threading
from collections.abc import Generator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from execution.verify_design_conformance import _scan_canary  # pyright: ignore[reportPrivateUsage]
from ui.conformance_scan import scan_surface_evidence

ROOT_TOKENS = """
:root {
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
    assert result.status == "passed"
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


def _playwright_or_skip() -> None:
    pytest.importorskip("playwright")
    try:
        playwright_api = importlib.import_module("playwright.sync_api")
        with playwright_api.sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            browser.close()
    except Exception as exc:  # pragma: no cover - environment-dependent
        pytest.skip(f"Playwright Chromium unavailable: {type(exc).__name__}")
