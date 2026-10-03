"""Isolated browser regressions for the Explore read lifecycle.

No server, provider, or application database is used. The production renderer,
controls, and runtime execute against explicit synthetic browser responses.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

pytest.importorskip("playwright")
from playwright.sync_api import Browser, Error, Page, Playwright, sync_playwright

from integrations.portfolio_tracker_client import (
    LivePortfolio,
    PerformancePoint,
    PerformanceSeries,
    PortfolioAnalytics,
)
from pipeline.explore_panel import render_explore_panel
from pipeline.portfolio_panel import compose_portfolio_page
from pipeline.triage_panel import render_triage_panel
from redteam.brief import render_red_team_brief
from redteam.models import RedTeamItemRow
from ui.controls import controls_js


def _launch(playwright: Playwright) -> Browser:
    try:
        return playwright.chromium.launch(headless=True)
    except Error as error:
        if "Executable doesn't exist" in str(error):
            pytest.skip("Chromium browser is not installed")
        raise


def _document(tmp_path: Path, fixture: str) -> str:
    fragment = render_explore_panel(
        tmp_path / "absent-synthetic.db", initial_tickers=["CANARY"], include_runtime=True
    )
    return (
        '<!doctype html><html><body><input id="vx-tickers" value="CANARY">'
        f"<script>{fixture}</script><script>{controls_js()}</script>{fragment}</body></html>"
    )


def _load(page: Page, html: str) -> None:
    page.route(
        "**/*",
        lambda route: (
            route.fulfill(body=html, content_type="text/html")
            if route.request.url == "http://fixture.local/"
            else route.abort()
        ),
    )
    page.goto("http://fixture.local/", wait_until="domcontentloaded")


def test_explore_defers_catalog_and_rejects_late_company_response(tmp_path: Path) -> None:
    fixture = r"""
    window.catalogRequests=0;
    window.fetch=function(){
      const call=++window.catalogRequests;
      const label=call===1?'Old company':'New company';
      // Ignore cancellation deliberately: even an uncooperative old response
      // must not overwrite the selected company's catalog.
      return new Promise(resolve=>setTimeout(()=>resolve(new Response(JSON.stringify({
        fin:[{token:'fin:revenue',label:label,tickers:1}],kpi:[],seg:[],detail:[]
      }),{headers:{'Content-Type':'application/json'}})),call===1?250:5));
    };
    """
    with sync_playwright() as playwright:
        browser = _launch(playwright)
        try:
            page = browser.new_page()
            _load(page, _document(tmp_path, fixture))
            assert page.evaluate("window.catalogRequests") == 0
            page.locator("#vx-open-empty").click()
            page.locator("#vx-workbench-company").evaluate(
                "node=>{node.add(new Option('OTHER','OTHER'));node.value='OTHER';node.dispatchEvent(new Event('change',{bubbles:true}));}"
            )
            page.locator("#vx-browse-fields").click()
            page.locator("#vx-field-catalog").get_by_text("New company", exact=True).wait_for()
            page.wait_for_timeout(300)
            assert "Old company" not in page.locator("#vx-field-catalog").inner_text()
            assert page.evaluate("window.catalogRequests") == 2
        finally:
            browser.close()


def test_explore_analysis_timeout_recovers_and_close_rejects_late_result(tmp_path: Path) -> None:
    fixture = r"""
    const originalTimeout=window.setTimeout;
    window.accelerateDeadline=false;
    window.setTimeout=function(fn,ms){return originalTimeout(fn,window.accelerateDeadline&&ms===30000?20:ms);};
    window.fetch=function(input,options){
      if(String(input).includes('/catalog'))return Promise.resolve(new Response(JSON.stringify({
        fin:[{token:'fin:revenue',label:'Synthetic revenue',tickers:1}],kpi:[],seg:[],detail:[]
      }),{headers:{'Content-Type':'application/json'}}));
      return new Promise((resolve,reject)=>{
        originalTimeout(()=>resolve(new Response('<div class="vx-result">Late synthetic result</div>')),250);
        if(window.accelerateDeadline)options.signal.addEventListener('abort',()=>reject(options.signal.reason),{once:true});
      });
    };
    """
    with sync_playwright() as playwright:
        browser = _launch(playwright)
        try:
            page = browser.new_page()
            _load(page, _document(tmp_path, fixture))
            page.locator("#vx-open-empty").click()
            page.locator("#vx-browse-fields").click()
            page.locator("#vx-field-catalog [data-toggle-metric]").click()
            page.locator("#vx-run").click()
            page.locator("#vx-back").click()
            page.wait_for_timeout(300)
            assert "Late synthetic result" not in page.locator("#vx-result").inner_text()
            page.locator("#vx-open-empty").click()
            page.locator("#vx-field-catalog [data-toggle-metric]").click()
            page.evaluate("window.accelerateDeadline=true")
            page.locator("#vx-run").click()
            page.locator("#vx-result [role=alert]").get_by_text(
                "Analysis timed out. Try again."
            ).wait_for()
            assert page.locator("#vx-run").is_enabled()
        finally:
            browser.close()


def test_saved_analysis_read_failure_has_recovery(tmp_path: Path) -> None:
    fixture = r"""
    window.savedCalls=0;
    window.fetch=function(){return Promise.resolve(++window.savedCalls===1
      ?new Response('unavailable',{status:503})
      :new Response('<span>Recovered synthetic saved analysis</span>'));};
    """
    with sync_playwright() as playwright:
        browser = _launch(playwright)
        try:
            page = browser.new_page()
            _load(page, _document(tmp_path, fixture))
            page.locator("#vx-open-empty").click()
            # Opening Work Bench also asks for its catalog. Give the saved read
            # its own failure sequence after that unrelated read finishes.
            page.wait_for_timeout(30)
            page.evaluate("window.savedCalls=0")
            page.locator("#vx-saved-toggle").click()
            page.locator("#vx-saved-list [role=alert]").wait_for()
            page.locator("#vx-saved-toggle").click()
            page.locator("#vx-saved-toggle").click()
            page.locator("#vx-saved-list").get_by_text(
                "Recovered synthetic saved analysis"
            ).wait_for()
        finally:
            browser.close()


def test_failed_analysis_preserves_labeled_previous_result(tmp_path: Path) -> None:
    fixture = r"""
    window.runCalls=0;
    window.fetch=function(input){
      if(String(input).includes('/catalog'))return Promise.resolve(new Response(JSON.stringify({
        fin:[{token:'fin:revenue',label:'Synthetic revenue',tickers:1}],kpi:[],seg:[],detail:[]
      }),{headers:{'Content-Type':'application/json'}}));
      return Promise.resolve(++window.runCalls===1
        ?new Response('<div class="vx-result">Previous synthetic calculation</div>')
        :new Response(JSON.stringify({error:'Synthetic analysis unavailable'}),{status:503,headers:{'Content-Type':'application/json'}}));
    };
    """
    with sync_playwright() as playwright:
        browser = _launch(playwright)
        try:
            page = browser.new_page()
            _load(page, _document(tmp_path, fixture))
            page.locator("#vx-open-empty").click()
            page.locator("#vx-browse-fields").click()
            page.locator("#vx-field-catalog [data-toggle-metric]").click()
            page.locator("#vx-run").click()
            page.locator("#vx-result").get_by_text("Previous synthetic calculation").wait_for()
            page.locator("#vx-transform").select_option("yoy")
            page.locator("#vx-result [data-prior-analysis]").wait_for()
            page.locator("#vx-run").click()
            page.locator("#vx-result [role=alert]").wait_for()
            assert "Previous synthetic calculation" in page.locator("#vx-result").inner_text()
            assert "Previous analysis" in page.locator("#vx-result [role=status]").inner_text()
        finally:
            browser.close()


@pytest.mark.parametrize("panel", ["red-team", "triage"])
def test_legacy_refresh_retains_prior_view_and_retries_without_repeating_write(
    tmp_path: Path, panel: str
) -> None:
    fixture = r"""
    window.readCalls=0;window.writeCalls=0;
    window.CCAction={busy(){},release(){},receipt(){}};
    window.fetch=function(input,options){
      if(options&&options.method==='POST'){window.writeCalls++;return Promise.resolve(new Response('{}'));}
      return Promise.resolve(++window.readCalls===1
        ?new Response('Synthetic unavailable',{status:503})
        :new Response('<p>Recovered synthetic view</p>'));
    };
    """
    if panel == "red-team":
        item = RedTeamItemRow(
            id=1,
            run_key="synthetic",
            ticker="CANARY",
            lens="fx_translation",
            kind="per_name",
            attack_md="Previous synthetic attack",
            question_md="Synthetic question",
            proposed_change_md="Synthetic proposal",
            severity="high",
            status="open",
            defer_count=0,
            response_md=None,
            responded_at=None,
            created_at=datetime(2026, 1, 1),
        )
        fragment = '<div data-panel="red_team">' + render_red_team_brief([item]) + "</div>"
        action = '[data-rt-act="accept"]'
        retry = "[data-rt-refresh-retry]"
        retained = "Previous synthetic attack"
    else:
        fragment = render_triage_panel(tmp_path / "absent-synthetic.db")
        action = '#triage-list [data-act="dismiss"]'
        retry = "[data-triage-refresh-retry]"
        retained = "Previous synthetic note"
    html = f"<!doctype html><html><body><script>{fixture}</script>{fragment}</body></html>"
    with sync_playwright() as playwright:
        browser = _launch(playwright)
        try:
            page = browser.new_page()
            _load(page, html)
            if panel == "triage":
                page.locator("#triage-list").evaluate(
                    "node=>node.innerHTML='<div data-note-id=1>Previous synthetic note<button data-act=dismiss>Dismiss</button></div>'"
                )
            page.locator(action).click()
            page.locator(retry).wait_for()
            assert retained in page.locator("body").inner_text()
            assert page.evaluate("window.writeCalls") == 1
            page.locator(retry).click()
            page.get_by_text("Recovered synthetic view").wait_for()
            assert page.evaluate("window.readCalls") == 2
            assert page.evaluate("window.writeCalls") == 1
        finally:
            browser.close()


def test_portfolio_window_failure_retains_chart_filters_and_refresh_retry(tmp_path: Path) -> None:
    analytics = PortfolioAnalytics(
        available=True,
        api_url="http://synthetic.invalid",
        performance=PerformanceSeries(
            start_date="2025-10-01",
            end_date="2026-01-01",
            base_value=10000,
            net_external_cashflow_in=0,
            backfill_start_unreliable=False,
            points=[
                PerformancePoint("2025-10-01", 0, 0, 0, 0),
                PerformancePoint("2026-01-01", 12, 6, 7, 6),
            ],
            calculation_status="available",
        ),
    )
    fragment = compose_portfolio_page(
        analytics,
        LivePortfolio(available=True, api_url="http://synthetic.invalid"),
        include_live=False,
        include_position_drivers=False,
    )
    fixture = r"""
    window.readCalls=0;
    window.fetch=function(){return Promise.resolve(++window.readCalls===1
      ?new Response('unavailable',{status:503})
      :new Response('<div>Recovered synthetic chart</div>'));};
    """
    html = f'<!doctype html><html><body><script>{fixture}</script><div class="cc-panel-body"><p data-prior-chart>Previous synthetic chart</p>{fragment}</div></body></html>'
    with sync_playwright() as playwright:
        browser = _launch(playwright)
        try:
            page = browser.new_page()
            _load(page, html)
            page.locator("#pf-start").fill("2025-12-01")
            page.locator("#pf-end").fill("2026-01-01")
            page.locator("#pf-apply").click()
            page.locator("[data-pf-window-retry]").wait_for()
            assert page.locator("[data-prior-chart]").is_visible()
            assert page.locator("#pf-start").input_value() == "2025-12-01"
            assert page.locator("#pf-end").input_value() == "2026-01-01"
            assert (
                "does not reflect the selected window"
                in page.locator("[data-pf-window-status]").inner_text()
            )
            page.locator("[data-pf-window-retry]").click()
            page.get_by_text("Recovered synthetic chart").wait_for()
            assert page.evaluate("window.readCalls") == 2
        finally:
            browser.close()
