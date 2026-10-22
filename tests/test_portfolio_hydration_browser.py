"""Synthetic browser proof for the portfolio request's reduced projection."""

from __future__ import annotations

import os
import time
from pathlib import Path
from urllib.parse import urlsplit

import comments_server
import pytest

from integrations.portfolio_allocation import unavailable_portfolio_allocation
from integrations.portfolio_tracker_client import LivePortfolio
from tests.comments_server_seams import patch_server_seam
from tests.test_comments_server_dashboard import app_repo as app_repo

pytest.importorskip("playwright")
from playwright.sync_api import Route, expect, sync_playwright


@pytest.mark.parametrize("width", [1440, 1024])
def test_portfolio_hydrates_with_tracker_unavailable_and_no_fundamentals(
    app_repo: Path, monkeypatch: pytest.MonkeyPatch, width: int
) -> None:
    def forbidden(*_args: object) -> None:
        pytest.fail("portfolio requested unused fundamentals")

    monkeypatch.setattr("pipeline.research_cockpit.read_materialized_fundamentals", forbidden)
    monkeypatch.setattr("pipeline.research_cockpit._eval_fundamentals", forbidden)
    patch_server_seam(
        monkeypatch,
        "fetch_live_portfolio",
        lambda: LivePortfolio(available=False, api_url="http://tracker.test", error="unavailable"),
    )
    patch_server_seam(
        monkeypatch,
        "fetch_portfolio_allocation",
        lambda: unavailable_portfolio_allocation("portfolio_allocation_unavailable"),
    )
    client = comments_server.create_app(app_repo).test_client()
    timings: list[float] = []
    errors: list[str] = []

    def handle(route: Route) -> None:
        url = urlsplit(route.request.url)
        if url.hostname != "hydration.test":
            route.abort()
            return
        started = time.perf_counter()
        response = client.get(url.path + ("?" + url.query if url.query else ""))
        if url.path == "/api/work-os/portfolio":
            timings.append(time.perf_counter() - started)
            assert response.status_code == 200
        route.fulfill(
            status=response.status_code, content_type=response.content_type, body=response.data
        )

    with sync_playwright() as runtime:
        browser = runtime.chromium.launch(headless=True)
        try:
            page = browser.new_page(viewport={"width": width, "height": 1000})
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.route("**/*", handle)
            for attempt in range(2):
                started = time.perf_counter()
                page.goto("https://hydration.test/")
                expect(page.locator("#workOsPortfolioRows")).to_contain_text(
                    "Nu Holdings", timeout=5000
                )
                expect(page.locator("#workOsPortfolioNavDetail")).not_to_contain_text("Loading")
                assert time.perf_counter() - started < 5
                page.locator('[data-work-os-portfolio-sort="company"]').click()
                expect(page.locator("#workOsPortfolioRows")).to_contain_text("Nu Holdings")
                capture = os.environ.get("HYDRATION_EVIDENCE_DIR")
                if capture and attempt == 0:
                    page.screenshot(path=str(Path(capture) / f"synthetic-hydrated-{width}.png"))
            assert len(timings) == 2
            assert max(timings) < 1
            assert not errors
        finally:
            browser.close()
