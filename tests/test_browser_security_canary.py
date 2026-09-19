"""Mandatory real-browser checks using synthetic state and owned loopback listeners."""

from __future__ import annotations

import json
import socket
import threading
from collections.abc import Generator
from contextlib import contextmanager
from datetime import date
from io import StringIO
from pathlib import Path

import pytest
from flask import Flask, Response, request
from playwright.sync_api import Browser, Request, sync_playwright
from pydantic import TypeAdapter
from werkzeug.serving import make_server

import comments
from execution import comments_server
from report.models import ReportSpec
from report.renderers.workspace_sections.boot import _comment_boot_data

_RESULTS = TypeAdapter(dict[str, object])
_CAPABILITY = "synthetic-browser-canary-capability"
_PRIVATE = "synthetic-private-canary-data"
_ATTACK = '</script><script id="injected">window.canaryInjected=true</script>'


@contextmanager
def _serve(app: Flask) -> Generator[str]:
    server = make_server("127.0.0.1", 0, app, threaded=True)
    port = server.server_port
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
        assert not thread.is_alive(), "canary server did not stop"
        with socket.socket() as probe:
            probe.settimeout(1)
            assert probe.connect_ex(("127.0.0.1", port)) != 0, "owned canary listener remains open"


@pytest.fixture
def browser() -> Generator[Browser]:
    # This module belongs to the Chromium-equipped CI job. Missing browser
    # support is a failed required check, never a silently skipped security gate.
    with sync_playwright() as playwright:
        instance = playwright.chromium.launch(headless=True)
        try:
            yield instance
        finally:
            instance.close()


def test_browser_origin_capability_and_script_boundaries(
    browser: Browser, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COMMENTS_SERVER_REPORT_CAPABILITY", _CAPABILITY)
    monkeypatch.delenv("EARNINGS_SUMMARY_PRIVATE_BASE_URL", raising=False)
    root = tmp_path / "synthetic"
    root.mkdir()
    comments.append_comment(
        root,
        "TEST",
        date(2026, 9, 19),
        comments.Anchor(type="kpi_ledger_row", key="synthetic-row"),
        _ATTACK,
    )
    spec = ReportSpec.model_construct(
        ticker="TEST", generation_date=date(2026, 9, 19), repo_root=str(root)
    )
    output = StringIO()
    _comment_boot_data(output, spec)
    document = "<!doctype html><title>Synthetic security canary</title>" + output.getvalue()
    report = root / "output/research/TEST/2026_workspace.html"
    report.parent.mkdir(parents=True)
    report.write_text(document, encoding="utf-8")
    app = comments_server.create_app(root, db_path=root / "isolated.db")
    observations: list[tuple[str, str, str | None, int]] = []
    mutations: list[str] = []

    @app.after_request
    def observe(response: Response) -> Response:
        observations.append(
            (request.method, request.path, request.headers.get("Origin"), response.status_code)
        )
        return response

    @app.get("/canary-page")
    def page_document() -> str:
        return "<!doctype html><title>Synthetic harness</title>"

    @app.get("/canary-private")
    def private_document() -> dict[str, str]:
        return {"private": _PRIVATE}

    @app.post("/canary-write")
    def write_document() -> dict[str, int]:
        mutations.append("synthetic write")
        return {"writes": len(mutations)}

    attacker = Flask("synthetic-attacker")

    @attacker.get("/")
    def attacker_document() -> str:
        return "<!doctype html><title>Synthetic other origin</title>"

    with _serve(app) as base, _serve(attacker) as other, browser.new_context() as context:

        def observe_browser_request(event: Request) -> None:
            assert event.url.startswith((base + "/", other + "/", "file://"))

        # Observe without interception: route fulfillment/overrides can bypass
        # browser CORS preflights and would weaken this evidence.
        context.on("request", observe_browser_request)
        page = context.new_page()
        page.goto(base + "/canary-page")
        # A real opaque sandbox origin reaches the server. Assert its actual
        # response, rather than treating browser/network denial as proof.
        for path in ("/canary-private", "/reports/TEST"):
            result = _RESULTS.validate_python(
                page.evaluate(
                    """url => new Promise((resolve, reject) => {
                    const frame = document.createElement('iframe');
                    frame.sandbox = 'allow-scripts';
                    const timer = setTimeout(() => reject(new Error('sandbox request timeout')), 5000);
                    const listener = event => {
                        if (event.source !== frame.contentWindow) return;
                        clearTimeout(timer); window.removeEventListener('message', listener);
                        frame.remove(); resolve(event.data);
                    };
                    window.addEventListener('message', listener);
                    frame.srcdoc = '<script>fetch(' + JSON.stringify(url) +
                      ').then(async response => parent.postMessage({status:response.status,body:await response.text()}, "*"))' +
                      '.catch(error => parent.postMessage({error:String(error)}, "*"))<' + '/script>';
                    document.body.appendChild(frame);
                })""",
                    base + path,
                )
            )
            assert result.get("status") == 403, result
            assert _PRIVATE not in str(result) and _CAPABILITY not in str(result)
            assert ("GET", path, "null", 403) in observations

        page.goto(report.as_uri())
        assert page.locator("#injected").count() == 0
        assert page.evaluate("window.canaryInjected === undefined") is True
        assert (
            page.evaluate(
                "JSON.parse(document.querySelector('#workspace-comments').textContent).comments[0].comment"
            )
            == _ATTACK
        )
        # The legitimate file report uses the capability emitted by the real
        # boot renderer. Exercise browser OPTIONS/CORS as well as authorization.
        for path, method in (("/canary-private", "GET"), ("/canary-write", "POST")):
            result = _RESULTS.validate_python(
                page.evaluate(
                    """async ({url, method}) => {
                    const token = JSON.parse(document.querySelector('#workspace-boot').textContent).report_capability;
                    const response = await fetch(url, {method, headers:{'X-Report-Capability':token}});
                    return {status:response.status,body:await response.text()};
                }""",
                    {"url": base + path, "method": method},
                )
            )
            assert result["status"] == 200
            assert (method, path, "null", 200) in observations
        assert any(method == "OPTIONS" for method, _, _, _ in observations)
        without_token = page.evaluate(
            "async url => (await fetch(url)).status", base + "/canary-private"
        )
        assert without_token == 403

        page.goto(other + "/")
        # A simple POST reaches the guard without a preflight short circuit.
        page.evaluate(
            "async url => {try {await fetch(url,{method:'POST',body:'untrusted'})} catch {}}",
            base + "/canary-write",
        )
        assert ("POST", "/canary-write", other, 403) in observations
        assert len(mutations) == 1
        page.goto(base + "/canary-page")
        assert (
            page.evaluate(
                "async url => (await fetch(url,{method:'POST'})).status", base + "/canary-write"
            )
            == 200
        )
        assert len(mutations) == 2
        # Retain only synthetic request evidence, never bearer headers.
        (tmp_path / "browser-security-evidence.json").write_text(
            json.dumps(observations, indent=2), encoding="utf-8"
        )
