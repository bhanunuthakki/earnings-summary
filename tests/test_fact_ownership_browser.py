"""Synthetic browser proof for selected evidence and backend-owned valuation."""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Generator
from io import StringIO
from pathlib import Path
from urllib.parse import urlencode, urlsplit

import pytest
from flask.testing import FlaskClient

pytest.importorskip("playwright")
from playwright.sync_api import Browser, Page, Route, expect, sync_playwright

from report.models import SectionStatus, SegmentsSection
from report.renderers import workspace_dcf, workspace_styles
from report.renderers.workspace_sections.financials import _line_items_levels_panel
from report.sections import financials
from tests.test_canonical_financial_peek import content_client
from tests.test_comments_server_dcf import BASE_INPUTS
from tests.test_comments_server_dcf import client as client
from tests.test_report_canonical_financials import STAMP, seed_table
from tests.test_report_canonical_financials import database as database
from ui.source_chip import SOURCE_CHIP_JS


@pytest.fixture
def browser() -> Generator[Browser]:
    with sync_playwright() as runtime:
        instance = runtime.chromium.launch(headless=True)
        try:
            yield instance
        finally:
            instance.close()


def _capture(page: Page, filename: str) -> None:
    root = os.environ.get("OWNERSHIP_EVIDENCE_DIR")
    if root:
        destination = Path(root)
        destination.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(destination / filename), full_page=True)


def _document(body: str, script: str = "") -> str:
    return (
        '<!doctype html><html><meta charset="utf-8"><style>'
        + workspace_styles.CSS
        + workspace_dcf.CSS
        + '</style><body><main class="tab-body">'
        + body
        + "</main>"
        + script
        + "</body></html>"
    )


@pytest.mark.parametrize("width", [1440, 1024])
@pytest.mark.parametrize("entrypoint", ["https", "file"])
def test_financial_value_opens_its_exact_retained_evidence(
    browser: Browser, database: sqlite3.Connection, tmp_path: Path, width: int, entrypoint: str
) -> None:
    seed_table(
        database,
        [
            ("revenue", "2025-01-01", "2025-03-31", "Q1", "100000000", "USD"),
            ("revenue", "2025-04-01", "2025-06-30", "Q2", "120000000", "USD"),
        ],
    )
    report = financials.build("SYNTH", tmp_path, conn=database, as_of=STAMP)
    output = StringIO()
    _line_items_levels_panel(output, report, SegmentsSection(status=SectionStatus.MISSING_DATA))
    document = _document(
        "<h1>Synthetic Financials</h1>" + output.getvalue(),
        '<script type="application/json" id="workspace-boot">'
        + json.dumps({"server_url": "https://configured.synthetic.invalid"})
        + "</script><script>"
        + SOURCE_CHIP_JS
        + "</script>",
    )
    api, _ = content_client(database, tmp_path)
    context = browser.new_context(viewport={"width": width, "height": 1000})

    def handle(route: Route) -> None:
        url = urlsplit(route.request.url)
        if url.path == "/fixture" or url.scheme == "file":
            route.fulfill(content_type="text/html", body=document)
        elif url.path == "/api/peek/canonical-financial":
            response = api.get(url.path + "?" + url.query)
            route.fulfill(
                status=response.status_code,
                content_type=response.content_type,
                body=response.text,
            )
        else:
            route.abort()

    context.route("**/*", handle)
    try:
        page = context.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        if entrypoint == "file":
            fixture = tmp_path / "synthetic-report.html"
            fixture.write_text(document, encoding="utf-8")
            page.goto(fixture.as_uri())
        else:
            page.goto("https://synthetic.invalid/fixture")
        chips = page.locator("td.num a.src-chip")
        assert chips.count() == 2
        expect(page.locator("td.num").first).to_contain_text("100.0")
        chips.first.focus()
        with page.expect_popup() as pending:
            page.keyboard.press("Enter")
        evidence = pending.value
        if entrypoint == "file":
            assert evidence.url.startswith("https://configured.synthetic.invalid/")
        expect(evidence.locator("body")).to_contain_text("Evidence for the selected value")
        expect(evidence.locator("body")).to_contain_text("100000000")
        expect(evidence.locator("body")).not_to_contain_text("120000000")
        expect(evidence.locator("body")).to_contain_text("/facts/0/value")
        source = report.line_items[0].sources_full[0]
        assert source is not None and source.canonical_reference is not None
        expect(evidence.locator("body")).to_contain_text(source.canonical_reference.observation_id)
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        _capture(page, f"financials-after-{entrypoint}-{width}.png")
        _capture(evidence, f"evidence-after-{entrypoint}-{width}.png")
        assert evidence.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        expect(evidence.locator("body")).not_to_contain_text("Â")
        unavailable = source.canonical_reference.model_copy(update={"observation_id": "missing"})
        evidence.goto(
            "https://synthetic.invalid/api/peek/canonical-financial?"
            + urlencode({"reference": unavailable.model_dump_json()})
        )
        expect(evidence.locator("body")).to_contain_text("Evidence unavailable for this selection")
        expect(evidence.locator("body")).not_to_contain_text("100000000")
        _capture(evidence, f"evidence-unavailable-{width}.png")
        assert not errors
    finally:
        context.close()


@pytest.mark.parametrize("width", [1440, 1024])
def test_driver_preview_uses_backend_country_risk_and_preserves_override(
    browser: Browser, client: FlaskClient, width: int
) -> None:
    inputs = BASE_INPUTS.to_dict()
    inputs["country_risk_premium"] = 0.03
    editor = StringIO()
    workspace_dcf.render_dcf_editor(editor, "SYNTH")
    script = (
        '<script type="application/json" id="workspace-boot">'
        + json.dumps({"ticker": "SYNTH"})
        + "</script><script>"
        + workspace_dcf.JS
        + "</script>"
    )
    document = _document("<h1>Synthetic valuation preview</h1>" + editor.getvalue(), script)
    context = browser.new_context(viewport={"width": width, "height": 1000})
    fail_next = False

    def handle(route: Route) -> None:
        nonlocal fail_next
        path = urlsplit(route.request.url).path
        if path == "/fixture":
            route.fulfill(content_type="text/html", body=document)
        elif path == "/api/dcf/inputs/SYNTH":
            route.fulfill(json={"inputs": inputs})
        elif path == "/api/dcf/recompute":
            if fail_next:
                fail_next = False
                route.fulfill(status=503, json={"error": "Synthetic preview unavailable"})
                return
            response = client.post(path, json=route.request.post_data_json)
            route.fulfill(status=response.status_code, json=response.get_json())
        else:
            route.abort()

    context.route("**/*", handle)
    try:
        page = context.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto("https://synthetic.invalid/fixture")
        page.locator("#dcf-edit-toggle").click()
        expect(page.locator("#dcf-edit-status")).to_contain_text("Base")
        beta = (
            page.locator(".dcf-edit-field")
            .filter(has=page.get_by_text("Beta", exact=True))
            .locator("input")
        )
        beta.fill("1.3")
        wacc = (
            page.locator(".dcf-edit-field")
            .filter(has=page.get_by_text("WACC (preview only) (%)", exact=True))
            .locator("input")
        )
        # Independent expected CAPM+CRP with market-value weights: 12.77577...%.
        expect(wacc).to_have_value("12.78")
        expect(page.locator("#dcf-edit-status")).to_contain_text("WACC 12.8%")
        _capture(page, f"dcf-after-{width}.png")
        wacc.fill("15")
        expect(wacc).to_have_value("15.00")
        expect(page.locator("#dcf-edit-status")).to_contain_text("preview-only")
        page.evaluate("window.dcfSetDriver('tax_rate',0.30,'Synthetic tax')")
        expect(wacc).to_have_value("12.77")
        expect(page.locator("#dcf-edit-status")).not_to_contain_text("preview-only")
        page.locator("#dcf-edit-reset").click()
        expect(wacc).to_have_value("9.00")
        fail_next = True
        beta.fill("1.4")
        expect(page.locator("#dcf-edit-status")).to_contain_text("Synthetic preview unavailable")
        _capture(page, f"dcf-error-{width}.png")
        beta.fill("1.3")
        expect(wacc).to_have_value("12.78")
        expect(page.locator("#dcf-edit-status")).to_contain_text("Base")
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        assert not errors
    finally:
        context.close()
