"""Browser-level Playwright integration tests for homepage evaluation dialogues."""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import threading
import time
from collections.abc import Generator
from contextlib import contextmanager, suppress
from hashlib import sha256
from pathlib import Path
from typing import Any

import pytest
from werkzeug.serving import make_server

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "execution"))

import comments_server  # noqa: E402


def _make_context_json(
    ticker: str, candidate_id: int | None = None, instrument: str = "stock"
) -> tuple[str, str]:
    payload = {
        "schema_version": "session_context.v1",
        "company_ticker": ticker,
        "evaluation_candidate_id": candidate_id,
        "evaluation_instrument_type": instrument,
    }
    raw = json.dumps(payload)
    return raw, sha256(raw.encode("utf-8")).hexdigest()


def _playwright_or_skip() -> None:
    pytest.importorskip("playwright")
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            browser.close()
    except Exception as exc:
        pytest.skip(f"Playwright Chromium unavailable: {type(exc).__name__}")


def _populate_test_db(db_path: Path, *, include_discovery: bool = True) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE tracked_companies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT DEFAULT 'bhanu',
            ticker TEXT NOT NULL,
            name TEXT NOT NULL,
            list_type TEXT NOT NULL,
            added_at TIMESTAMP,
            instrument_type TEXT,
            archived_at TIMESTAMP,
            sec_validated BOOLEAN DEFAULT 0,
            UNIQUE(user_id, ticker)
        );
        INSERT INTO tracked_companies (ticker, name, list_type, instrument_type)
        VALUES
            ('AAPL', 'Apple Inc.', 'evaluation', 'equity'),
            ('MSFT', 'Microsoft Corp.', 'evaluation', 'equity'),
            ('NVDA', 'Nvidia Corp.', 'evaluation', 'equity'),
            ('QQQ', 'Invesco QQQ', 'evaluation', 'etf'),
            ('TSLA', 'Tesla Inc.', 'evaluation', 'equity');
        """
    )
    if include_discovery:
        conn.executescript(
            """
            CREATE TABLE discovery_candidates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT DEFAULT 'bhanu',
                ticker TEXT NOT NULL,
                status TEXT NOT NULL
            );
            INSERT INTO discovery_candidates (id, ticker, status)
            VALUES
                (1, 'AAPL', 'active'),
                (2, 'MSFT', 'active'),
                (3, 'NVDA', 'active'),
                (4, 'QQQ', 'active'),
                (5, 'TSLA', 'active');
            """
        )
    conn.executescript(
        """
        CREATE TABLE ask_sessions (
            id TEXT PRIMARY KEY,
            scope TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        INSERT INTO ask_sessions (id, scope, updated_at)
        VALUES
            ('sess-aapl-1', 'portfolio', '2026-08-20T12:00:00Z'),
            ('sess-msft-1', 'portfolio', '2026-08-18T10:00:00Z');

        CREATE TABLE ask_session_contexts (
            session_id TEXT PRIMARY KEY,
            context_json TEXT NOT NULL,
            context_sha256 TEXT NOT NULL
        );

        CREATE TABLE analyst_notes (
            id INTEGER PRIMARY KEY,
            user_id TEXT DEFAULT 'bhanu',
            ticker TEXT NOT NULL,
            kind TEXT DEFAULT 'observation',
            status TEXT NOT NULL,
            body TEXT DEFAULT '',
            anchor_type TEXT,
            anchor_key TEXT,
            source TEXT DEFAULT 'user',
            source_ref TEXT,
            supersedes_id INTEGER,
            resolution_note TEXT,
            context_json TEXT,
            created_at TEXT,
            updated_at TEXT DEFAULT '2026-08-20T00:00:00Z',
            resolved_at TEXT,
            decision_id INTEGER,
            position_entry_id INTEGER,
            link_auto_resolve INTEGER DEFAULT 0,
            fact_ref TEXT
        );
        INSERT INTO analyst_notes (ticker, status, created_at, updated_at)
        VALUES
            ('AAPL', 'open', '2026-08-21T14:00:00Z', '2026-08-21T14:00:00Z'),
            ('NVDA', 'open', '2026-08-19T09:00:00Z', '2026-08-19T09:00:00Z');
        """
    )
    if include_discovery:
        c1, s1 = _make_context_json("AAPL", 1, "stock")
        c2, s2 = _make_context_json("MSFT", 2, "stock")
        conn.execute("INSERT INTO ask_session_contexts VALUES (?, ?, ?)", ("sess-aapl-1", c1, s1))
        conn.execute("INSERT INTO ask_session_contexts VALUES (?, ?, ?)", ("sess-msft-1", c2, s2))
    conn.commit()
    conn.close()


@contextmanager
def _run_test_server(repo_root: Path) -> Generator[str, None, None]:
    app = comments_server.create_app(repo_root)
    server = make_server("127.0.0.1", 0, app)
    port: int = server.server_port
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()
        thread.join(timeout=2.0)


def test_browser_evaluation_dialogues_combined_controls(tmp_path: Path) -> None:
    """Test combined controls (Has notes + Ticker A-Z + Show 5) and capture screenshots."""
    _playwright_or_skip()
    from playwright.sync_api import sync_playwright

    db_path = tmp_path / "data" / "portfolio.db"
    _populate_test_db(db_path, include_discovery=True)

    with _run_test_server(tmp_path) as base_url, sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            # 1. Desktop viewport: 1440x900
            context = browser.new_context(viewport={"width": 1440, "height": 900})
            page = context.new_page()
            page.goto(base_url, wait_until="networkidle")

            # Verify default rendering (3 items ordered by relevance: AAPL, NVDA, MSFT)
            threads = page.locator("#workOsEvaluationDialogues .work-os-evaluation-thread")
            threads.first.wait_for(timeout=5000)
            assert threads.count() == 3
            initial_tickers = [
                threads.nth(i).get_attribute("data-work-os-evaluation-ticker")
                for i in range(threads.count())
            ]
            assert initial_tickers == ["AAPL", "NVDA", "MSFT"]

            # Click filter chip: 'Has notes'
            notes_chip = page.locator('[data-work-os-eval-filter="has_notes"]')
            notes_chip.click()
            page.wait_for_function(
                "() => {"
                " const chip = document.querySelector('[data-work-os-eval-filter=\"has_notes\"]');"
                " const count = document.getElementById('workOsEvaluationCount');"
                " return chip && chip.classList.contains('is-active') && count && count.textContent.includes('matching');"
                "}"
            )
            assert notes_chip.get_attribute("aria-pressed") == "true"

            # Change sort: 'Ticker A-Z'
            sort_select = page.locator("#workOsEvaluationSort")
            sort_select.select_option("ticker_asc")

            # Change limit: '5'
            limit_select = page.locator("#workOsEvaluationLimit")
            limit_select.select_option("5")

            # Wait for updated content
            page.wait_for_function(
                "() => {"
                " const count = document.getElementById('workOsEvaluationCount');"
                " return count && count.textContent === 'Showing 2 of 2 matching · 5 active total';"
                "}"
            )

            filtered_tickers = [
                threads.nth(i).get_attribute("data-work-os-evaluation-ticker")
                for i in range(threads.count())
            ]
            # AAPL and NVDA have notes, sorted alphabetically AAPL then NVDA
            assert filtered_tickers == ["AAPL", "NVDA"]

            count_text = page.locator("#workOsEvaluationCount").text_content() or ""
            assert count_text == "Showing 2 of 2 matching · 5 active total"

            # Capture desktop screenshot at 1440x900
            artifact_env = os.environ.get("ARTIFACT_DIR")
            artifact_dir = Path(artifact_env) if artifact_env else tmp_path / "artifacts"
            artifact_dir.mkdir(parents=True, exist_ok=True)
            desktop_shot = artifact_dir / "evaluation_dialogues_desktop_1440.png"
            eval_section = page.locator('section[aria-labelledby="workOsEvaluationHeading"]')
            eval_section.screenshot(path=str(desktop_shot))
            assert desktop_shot.exists() and desktop_shot.stat().st_size > 0
            context.close()

            # 2. Mobile viewport: 390x844
            mobile_context = browser.new_context(viewport={"width": 390, "height": 844})
            mobile_page = mobile_context.new_page()
            mobile_page.goto(base_url, wait_until="networkidle")

            mobile_threads = mobile_page.locator(
                "#workOsEvaluationDialogues .work-os-evaluation-thread"
            )
            mobile_threads.first.wait_for(timeout=5000)

            mobile_shot = artifact_dir / "evaluation_dialogues_mobile_390.png"
            mobile_eval_section = mobile_page.locator(
                'section[aria-labelledby="workOsEvaluationHeading"]'
            )
            mobile_eval_section.screenshot(path=str(mobile_shot))
            assert mobile_shot.exists() and mobile_shot.stat().st_size > 0
            mobile_context.close()

        finally:
            browser.close()


def test_browser_evaluation_dialogues_race_condition_handling(tmp_path: Path) -> None:
    """Test that rapid filter changes ignore stale out-of-order asynchronous responses."""
    _playwright_or_skip()
    from playwright.sync_api import Route, sync_playwright

    db_path = tmp_path / "data" / "portfolio.db"
    _populate_test_db(db_path, include_discovery=True)

    with _run_test_server(tmp_path) as base_url, sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page()

            def handle_route(route: Route) -> None:
                url = route.request.url
                if "filter=has_dialogue" in url:
                    # Artificially delay this response by 400ms
                    def delayed_fulfill() -> None:
                        payload: dict[str, Any] = {
                            "state": "available",
                            "total_active": 5,
                            "total_matching": 1,
                            "matching_state": "complete",
                            "reason_codes": [],
                            "items": [
                                {
                                    "ticker": "SLOW",
                                    "name": "Slow Stale Ticker",
                                    "instrument_type": "stock",
                                    "workup_readiness": "partial",
                                    "freshness": "partial",
                                    "open_note_count": 0,
                                    "ask_session_id": "s1",
                                    "ask_session_link_state": "linked",
                                }
                            ],
                        }
                        with suppress(Exception):
                            route.fulfill(
                                status=200,
                                content_type="application/json",
                                body=json.dumps(payload),
                            )

                    timer = threading.Timer(0.4, delayed_fulfill)
                    timer.daemon = True
                    timer.start()
                elif "filter=has_notes" in url:
                    # Fast response
                    payload = {
                        "state": "available",
                        "total_active": 5,
                        "total_matching": 1,
                        "matching_state": "complete",
                        "reason_codes": [],
                        "items": [
                            {
                                "ticker": "FAST",
                                "name": "Fast Fresh Ticker",
                                "instrument_type": "stock",
                                "workup_readiness": "partial",
                                "freshness": "partial",
                                "open_note_count": 1,
                                "ask_session_id": "",
                                "ask_session_link_state": "unlinked",
                            }
                        ],
                    }
                    route.fulfill(
                        status=200,
                        content_type="application/json",
                        body=json.dumps(payload),
                    )
                else:
                    route.continue_()

            page.route("**/api/work-os/evaluation-dialogues*", handle_route)
            page.goto(base_url, wait_until="networkidle")

            # Trigger slow request, then immediately trigger fast request
            page.locator('[data-work-os-eval-filter="has_dialogue"]').click()
            page.locator('[data-work-os-eval-filter="has_notes"]').click()

            # Wait for all timers and responses to settle
            time.sleep(0.7)
            page.wait_for_selector('[data-work-os-evaluation-ticker="FAST"]', timeout=3000)

            # Confirm that FAST is rendered and SLOW was ignored due to request generation token
            tickers = [
                el.get_attribute("data-work-os-evaluation-ticker")
                for el in page.locator(".work-os-evaluation-thread").all()
            ]
            assert "FAST" in tickers
            assert "SLOW" not in tickers

        finally:
            browser.close()


def test_browser_evaluation_dialogues_partial_relevance_notice_toggle(tmp_path: Path) -> None:
    """Test that the relevance_partial notice renders only under relevance sort."""
    _playwright_or_skip()
    from playwright.sync_api import Route, sync_playwright

    db_path = tmp_path / "data" / "portfolio.db"
    _populate_test_db(db_path, include_discovery=True)

    with _run_test_server(tmp_path) as base_url, sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page()

            def handle_route(route: Route) -> None:
                url = route.request.url
                if "/api/work-os/evaluation-dialogues" in url:
                    payload = {
                        "state": "partial",
                        "total_active": 5,
                        "total_matching": 1,
                        "matching_state": "complete",
                        "reason_codes": ["relevance_partial"],
                        "items": [
                            {
                                "ticker": "TEST",
                                "name": "Test Company",
                                "instrument_type": "stock",
                                "workup_readiness": "partial",
                                "freshness": "partial",
                                "open_note_count": 0,
                                "ask_session_id": "",
                                "ask_session_link_state": "unlinked",
                            }
                        ],
                    }
                    route.fulfill(
                        status=200,
                        content_type="application/json",
                        body=json.dumps(payload),
                    )
                else:
                    route.continue_()

            page.route("**/api/work-os/evaluation-dialogues*", handle_route)
            page.goto(base_url, wait_until="networkidle")

            # 1. Under default 'relevance' sort, notice must be visible
            notice = page.locator(".work-os-eval-notice")
            assert notice.is_visible()
            assert "Activity recency is partially unavailable" in (notice.text_content() or "")

            # 2. Switch to 'ticker_asc' -> notice must disappear
            sort_select = page.locator("#workOsEvaluationSort")
            sort_select.select_option("ticker_asc")
            page.wait_for_timeout(200)
            assert notice.count() == 0

            # 3. Switch back to 'relevance' -> notice must reappear
            sort_select.select_option("relevance")
            page.wait_for_timeout(200)
            assert notice.is_visible()

        finally:
            browser.close()


def test_browser_evaluation_dialogues_unknown_link_state_disabled_button(tmp_path: Path) -> None:
    """Test that an unknown link state renders a disabled button with explicit title."""
    _playwright_or_skip()
    from playwright.sync_api import Route, sync_playwright

    db_path = tmp_path / "data" / "portfolio.db"
    _populate_test_db(db_path, include_discovery=True)

    with _run_test_server(tmp_path) as base_url, sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page()

            def handle_route(route: Route) -> None:
                if "/api/work-os/evaluation-dialogues" in route.request.url:
                    payload = {
                        "state": "partial",
                        "total_active": 1,
                        "total_matching": 1,
                        "matching_state": "complete",
                        "reason_codes": ["discovery_source_unavailable"],
                        "items": [
                            {
                                "ticker": "UNLINKED",
                                "name": "Unlinked Corp",
                                "instrument_type": "stock",
                                "workup_readiness": "unavailable",
                                "freshness": "unavailable",
                                "open_note_count": 0,
                                "ask_session_id": "",
                                "ask_session_link_state": "unknown",
                            }
                        ],
                    }
                    route.fulfill(
                        status=200,
                        content_type="application/json",
                        body=json.dumps(payload),
                    )
                else:
                    route.continue_()

            page.route("**/api/work-os/evaluation-dialogues*", handle_route)
            page.goto(base_url, wait_until="networkidle")

            btn = page.locator(".work-os-evaluation-actions button").first
            assert btn.is_disabled()
            assert btn.text_content() == "Dialogue unavailable"
            assert btn.get_attribute("title") == "Dialogue status temporarily unavailable"

        finally:
            browser.close()


def test_e2e_discovery_failure_renders_unknown_dialogue_state(tmp_path: Path) -> None:
    """Live end-to-end integration: missing discovery_candidates table degrades to unknown dialogue state."""
    _playwright_or_skip()
    from playwright.sync_api import sync_playwright

    db_path = tmp_path / "data" / "portfolio.db"
    # Populate DB WITHOUT discovery_candidates table
    _populate_test_db(db_path, include_discovery=False)

    with _run_test_server(tmp_path) as base_url, sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            # 100% real end-to-end HTTP request to comments_server and SQLite DB
            page.goto(base_url, wait_until="networkidle")

            threads = page.locator("#workOsEvaluationDialogues .work-os-evaluation-thread")
            threads.first.wait_for(timeout=5000)
            assert threads.count() > 0

            # All primary dialogue buttons should be disabled "Dialogue unavailable"
            dialogue_buttons = page.locator(
                ".work-os-evaluation-thread .work-os-evaluation-actions button:first-child"
            ).all()
            assert len(dialogue_buttons) > 0
            for btn in dialogue_buttons:
                assert btn.is_disabled()
                assert btn.text_content() == "Dialogue unavailable"
                assert btn.get_attribute("title") == "Dialogue status temporarily unavailable"

        finally:
            browser.close()
