"""Tests for the UX9 peek primitive: the /api/peek/* fragment routes, the
/source/<doc_id>?fragment=1 viewer variant, and the markup contracts that make
shell links peek instead of drilling through (data-peek-url / data-peek-ticker
attributes with their real hrefs preserved).

DB substrate: alembic from the pre-CIO head to head (alerts / queued_actions /
advisor_memos at their real shapes), plus hand-rolled minimal tables for the
pre-0059 vintage ones the peeks read (tracked_companies, thesis_evaluations,
documents), mirroring tests/test_source_viewers.py.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path

import pytest
from alembic.config import Config
from flask.testing import FlaskClient

from alembic import command
from alerts import fire_alert, queue_action
from dashboard._card import render_alert_card
from dashboard.inbox import collect_inbox, render_inbox_stream
from pipeline.analytical_dashboard_html import render_tier_coverage_strip
from pipeline.command_center_shell import SHELL_JS, render_shell
from pipeline.dashboard_status import DashboardRow
from pipeline.peeks import render_new_docs_peek, render_provenance_peek
from pipeline.portfolio_panel import render_next_dollar_panel
from pipeline.research_cockpit import CockpitRow, render_research_cockpit
from pipeline.ticker_command_center import build_ticker_command_center, render_ticker_fragment

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "execution"))

import comments_server  # noqa: E402

_PRIOR_HEAD = "0059_kpi_facts_restatement"

# Minimal pre-0059-vintage tables the peek readers touch. Created AFTER the
# alembic upgrade so no migration sees them mid-flight.
_EXTRA_DDL = """
CREATE TABLE IF NOT EXISTS tracked_companies (
    ticker TEXT PRIMARY KEY,
    name TEXT,
    list_type TEXT NOT NULL DEFAULT 'portfolio',
    archived_at TEXT,
    last_built_at TEXT,
    instrument_type TEXT
);
CREATE TABLE IF NOT EXISTS fmp_endpoint_status (
    ticker         TEXT    NOT NULL,
    endpoint       TEXT    NOT NULL,
    period         TEXT    NOT NULL DEFAULT '',
    status         TEXT    NOT NULL,
    last_pulled    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (ticker, endpoint, period)
);
CREATE TABLE IF NOT EXISTS thesis_evaluations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    evaluated_at TEXT NOT NULL,
    overall_status TEXT,
    rule_evaluations_json TEXT
);
CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    source_type TEXT NOT NULL,
    doc_type TEXT NOT NULL,
    file_path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    fetched_at TIMESTAMP NOT NULL,
    fetch_status TEXT NOT NULL,
    raw_bytes_size INTEGER NOT NULL DEFAULT 0,
    source_url TEXT,
    accession_number TEXT,
    filing_date TEXT
);
"""


def _build_db(db_path: Path) -> None:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.stamp(cfg, _PRIOR_HEAD)
    command.upgrade(cfg, "head")
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(_EXTRA_DDL)
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    db = tmp_path / "data" / "portfolio.db"
    db.parent.mkdir(parents=True)
    _build_db(db)
    return tmp_path


@pytest.fixture
def db_path(repo: Path) -> Path:
    return repo / "data" / "portfolio.db"


@pytest.fixture
def client(repo: Path) -> FlaskClient:
    return comments_server.create_app(repo).test_client()


def _seed_alert_with_action(db_path: Path, *, ticker: str = "NU", sig: str = "sig-peek-1"):
    alert = fire_alert(
        ticker=ticker,
        trigger_kind="kpi_inflection",
        fired_at=datetime.now(UTC),
        evidence_json='{"summary": "NIM compressed 40bps"}',
        signature_sha=sig,
        db_path=db_path,
    )
    qa = queue_action(
        alert_id=alert.id,
        action_kind="thesis_update",
        payload={"body": "Deposit beta runs hotter than modeled"},
        db_path=db_path,
    )
    return alert, qa


def _seed_memo(db_path: Path, *, kind: str = "next_dollar") -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO advisor_memos (user_id, kind, title, body_md, created_at) "
            "VALUES ('bhanu', ?, 'June allocation', "
            "'## Verdict' || char(10) || '**MELI** takes the next dollar.', "
            "'2026-06-10 12:00:00')",
            (kind,),
        )
        conn.commit()
    finally:
        conn.close()


def _seed_tracked(db_path: Path, ticker: str = "NU", name: str = "Nu Holdings") -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO tracked_companies (ticker, name, list_type) VALUES (?, ?, 'portfolio')",
            (ticker, name),
        )
        conn.execute(
            "INSERT INTO thesis_evaluations (ticker, evaluated_at, overall_status, "
            "rule_evaluations_json) VALUES (?, '2026-06-01T00:00:00', 'warn', '[]')",
            (ticker,),
        )
        conn.commit()
    finally:
        conn.close()


# ----------------------------------------------------------------------------
# /api/peek/alert + /api/peek/alerts
# ----------------------------------------------------------------------------


def test_peek_alert_serves_full_card_fragment(client: FlaskClient, db_path: Path) -> None:
    alert, qa = _seed_alert_with_action(db_path)
    resp = client.get(f"/api/peek/alert/{alert.id}")
    assert resp.status_code == 200
    body = resp.data.decode()
    assert not body.lstrip().lower().startswith("<!doctype")  # fragment, not a page
    assert "alert-card" in body
    assert "k-tick-sym" in body  # the ticker rides the kit label now
    assert "evidence-drawer" in body
    # The live approve/dismiss links ride inside the peek.
    assert f"/approve?action_id={qa.id}" in body
    assert f"/approve?action_id={qa.id}&dismiss=1" in body


def test_peek_alert_unknown_id_404s(client: FlaskClient) -> None:
    assert client.get("/api/peek/alert/424242").status_code == 404


def test_peek_alerts_list_filters_and_empty_state(client: FlaskClient, db_path: Path) -> None:
    _seed_alert_with_action(db_path)
    resp = client.get("/api/peek/alerts?ticker=NU&status=pending")
    assert resp.status_code == 200
    body = resp.data.decode()
    assert "alert-card" in body
    assert "open in the full feed" in body  # overflow path preserved
    assert "/feed?ticker=NU&amp;status=pending" in body or "/feed?ticker=NU" in body

    empty = client.get("/api/peek/alerts?ticker=ZZZQ")
    assert empty.status_code == 200
    assert "No matching alerts" in empty.data.decode()


# ----------------------------------------------------------------------------
# /api/peek/ticker
# ----------------------------------------------------------------------------


def test_peek_ticker_minicard(client: FlaskClient, repo: Path, db_path: Path) -> None:
    _seed_tracked(db_path)
    fmp = repo / "data" / "historical" / "fmp"
    fmp.mkdir(parents=True, exist_ok=True)
    (fmp / "NU_profile.json").write_text(
        json.dumps([{"price": 13.52, "changePercentage": 1.8}]), encoding="utf-8"
    )
    (fmp / "NU_earnings_calendar.json").write_text(
        json.dumps([{"date": "2099-01-05"}]), encoding="utf-8"
    )
    resp = client.get("/api/peek/ticker/nu")  # lowercase: uppercased internally
    assert resp.status_code == 200
    body = resp.data.decode()
    assert "cc-mini" in body
    assert "Nu Holdings" in body
    assert "$13.52" in body
    assert "warn" in body  # verdict badge from thesis_evaluations
    assert "/#holding=NU" in body  # the open-the-holding path


def test_peek_ticker_untracked_404s(client: FlaskClient) -> None:
    assert client.get("/api/peek/ticker/ZZZQ").status_code == 404


# ----------------------------------------------------------------------------
# /api/peek/memo
# ----------------------------------------------------------------------------


def test_peek_memo_renders_latest_markdown(client: FlaskClient, db_path: Path) -> None:
    _seed_memo(db_path)
    resp = client.get("/api/peek/memo/next_dollar")
    assert resp.status_code == 200
    body = resp.data.decode()
    assert "June allocation" in body
    assert "synthesis-body" in body
    assert "MELI" in body


def test_peek_memo_unknown_kind_and_missing_row_404(client: FlaskClient) -> None:
    assert client.get("/api/peek/memo/bogus_kind").status_code == 404
    # Valid kind, no memo yet — also a 404, not an empty page.
    assert client.get("/api/peek/memo/swap_check").status_code == 404


# ----------------------------------------------------------------------------
# /api/peek/review (PR5 — the instant position-review read + escalation button)
# ----------------------------------------------------------------------------

# decisions predates the 0059 stamp (db.init_db() territory), mirroring
# tests/test_open_loops.py's _DECISIONS_DDL pattern (hand-built post-upgrade).
_DECISIONS_DDL = """
CREATE TABLE decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker VARCHAR(16),
    recommendation_kind VARCHAR(32) NOT NULL,
    conviction VARCHAR(16),
    outcome_label VARCHAR(16) NOT NULL DEFAULT 'pending',
    decided_by VARCHAR(16) NOT NULL DEFAULT 'advisor',
    scope VARCHAR(16) NOT NULL DEFAULT 'ticker',
    falsifier TEXT,
    size_usd FLOAT,
    user_notes TEXT,
    made_at DATETIME NOT NULL,
    created_at DATETIME NOT NULL
);
"""


def _seed_decisions_table(db_path: Path) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(_DECISIONS_DDL)
        conn.commit()
    finally:
        conn.close()


def test_peek_review_renders_deterministic_pre_analysis(client: FlaskClient) -> None:
    # No thesis JSON on file -> the deterministic encode-first degrade, same
    # as the /review chat command; always 200, never a 404.
    resp = client.get("/api/peek/review/RBRK")
    assert resp.status_code == 200
    body = resp.data.decode()
    assert not body.lstrip().lower().startswith("<!doctype")  # fragment, not a page
    assert "cc-review-peek" in body
    assert "RBRK" in body
    assert "Mechanical read" in body
    # The escalation button + its log target ride the footer.
    assert "Full calibrated review (LLM)" in body
    assert 'data-review-ticker="RBRK"' in body
    assert "cc-review-log" in body


def test_peek_review_carries_the_live_graded_base_rate(client: FlaskClient, db_path: Path) -> None:
    _seed_decisions_table(db_path)
    conn = sqlite3.connect(str(db_path))
    try:
        for t in ("MU", "TSM", "NVDA", "AMZN", "GOOGL"):
            conn.execute(
                "INSERT INTO decisions (ticker, recommendation_kind, outcome_label, "
                "decided_by, made_at, created_at) VALUES (?, 'sell', 'wrong', 'owner', "
                "'2026-06-01', '2026-06-01')",
                (t,),
            )
        for t in ("RBRK", "WIX", "NOW"):
            conn.execute(
                "INSERT INTO decisions (ticker, recommendation_kind, outcome_label, "
                "decided_by, made_at, created_at) VALUES (?, 'trim', 'correct', 'owner', "
                "'2026-06-01', '2026-06-01')",
                (t,),
            )
        conn.commit()
    finally:
        conn.close()
    body = client.get("/api/peek/review/RBRK").data.decode()
    assert "Graded record on sells/trims: 5 of 8 wrong (AMZN, GOOGL, MU, NVDA, TSM)" in body
    # n=8 < MIN_CONFIDENT_N — the calibration-guard hedge must reach the
    # rendered surface, not just the raw line.
    assert "n=8, low confidence" in body


def test_peek_review_omits_base_rate_when_nothing_graded(client: FlaskClient) -> None:
    body = client.get("/api/peek/review/RBRK").data.decode()
    assert "Graded record on sells/trims" not in body


# ----------------------------------------------------------------------------
# /source/<doc_id>?fragment=1
# ----------------------------------------------------------------------------


def _seed_doc(
    db_path: Path,
    *,
    doc_type: str,
    file_path: str,
    source_url: str | None = None,
) -> int:
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute(
            "INSERT INTO documents (ticker, source_type, doc_type, file_path, sha256, "
            "fetched_at, fetch_status, source_url) "
            "VALUES ('NU', 'test', ?, ?, 'x', '2026-06-01 10:00:00', 'ok', ?)",
            (doc_type, file_path, source_url),
        )
        conn.commit()
        return int(cur.lastrowid or 0)
    finally:
        conn.close()


def test_source_fragment_transcript_is_chromeless(client: FlaskClient, repo: Path) -> None:
    db = repo / "data" / "portfolio.db"
    tdir = repo / "transcripts" / "processed"
    tdir.mkdir(parents=True)
    (tdir / "NU_Q1_2026.txt").write_text(
        "Operator: Good morning\nPrepared remarks line\nDavid Vélez: Obrigado",
        encoding="utf-8",
    )
    doc_id = _seed_doc(
        db, doc_type="earnings_call_transcript", file_path="transcripts/processed/NU_Q1_2026.txt"
    )

    frag = client.get(f"/source/{doc_id}?fragment=1")
    assert frag.status_code == 200
    body = frag.data.decode()
    assert body.lstrip().startswith('<div class="sv-frag">')
    assert "<!doctype" not in body.lower()
    assert 'id="L3"' in body  # line anchors survive for #L<n> highlighting

    full = client.get(f"/source/{doc_id}")
    assert full.status_code == 200
    assert full.data.decode().lstrip().lower().startswith("<!doctype")


def test_source_fragment_form10k_secnav_is_absolute(client: FlaskClient, repo: Path) -> None:
    db = repo / "data" / "portfolio.db"
    fdir = repo / "data" / "historical" / "fmp"
    fdir.mkdir(parents=True, exist_ok=True)
    (fdir / "NU_form_10k_2025.json").write_text(
        json.dumps(
            {
                "symbol": "NU",
                "period": "FY",
                "year": 2025,
                "Business": [{"Overview": ["Digital bank."]}],
                "Risk Factors": [{"Credit": ["Cycle risk."]}],
            }
        ),
        encoding="utf-8",
    )
    doc_id = _seed_doc(
        db, doc_type="fmp_10k_json", file_path="data/historical/fmp/NU_form_10k_2025.json"
    )

    frag = client.get(f"/source/{doc_id}?fragment=1")
    assert frag.status_code == 200
    body = frag.data.decode()
    # Relative "?section=" would navigate the HOST page the peek sits on.
    assert f'href="/source/{doc_id}?section=Risk%20Factors"' in body

    full = client.get(f"/source/{doc_id}")
    assert 'href="?section=Risk Factors"' in full.data.decode()


def test_source_fragment_never_redirects_externally(client: FlaskClient, repo: Path) -> None:
    db = repo / "data" / "portfolio.db"
    doc_id = _seed_doc(
        db,
        doc_type="ir_presentation",
        file_path="ir_documents/NU/deck.pdf",
        source_url="https://ir.example/deck.pdf",
    )

    full = client.get(f"/source/{doc_id}")
    assert full.status_code == 302  # full route keeps the external redirect

    frag = client.get(f"/source/{doc_id}?fragment=1")
    assert frag.status_code == 200  # the peek fetch can't follow cross-origin
    body = frag.data.decode()
    assert "https://ir.example/deck.pdf" in body  # outbound link is the way out
    assert "No in-app viewer" in body


# ----------------------------------------------------------------------------
# /api/peek/provenance (UX9d — freshness rows + in-place refresh)
# ----------------------------------------------------------------------------


def _seed_provenance(db_path: Path, ticker: str = "NU") -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO tracked_companies (ticker, name, list_type, last_built_at) "
            "VALUES (?, 'Nu Holdings', 'portfolio', '2026-06-09 04:00:00')",
            (ticker,),
        )
        conn.executemany(
            "INSERT INTO fmp_endpoint_status (ticker, endpoint, period, status, last_pulled) "
            "VALUES (?, ?, '', 'ok', ?)",
            [
                (ticker, "profile", "2026-06-10 05:10:00"),
                (ticker, "income_statement", "2026-06-01 05:00:00"),
            ],
        )
        conn.execute(
            "INSERT INTO documents (ticker, source_type, doc_type, file_path, sha256, "
            "fetched_at, fetch_status) VALUES (?, 'ir', 'ir_presentation', "
            "'ir_documents/NU/deck.pdf', 'x', '2026-06-08 01:30:00', 'ok')",
            (ticker,),
        )
        conn.execute(
            "INSERT INTO news (ticker, headline, url, published_at, snippet, source, "
            "source_feed, fetched_at) VALUES (?, 'NU ships a thing', 'https://x.test/nu-1', "
            "'2026-06-10 12:00:00', '', 'wire', 'fmp_stock_news', '2026-06-11 02:00:00')",
            (ticker,),
        )
        conn.execute(
            "INSERT INTO expected_earnings (ticker, expected_date, detected_source, "
            "first_seen_at, last_seen_at) "
            "VALUES (?, '2099-08-13', 'fmp_cache', '2026-06-11 05:35:00', '2026-06-11 05:35:00')",
            (ticker,),
        )
        conn.commit()
    finally:
        conn.close()


def test_provenance_peek_ticker_rows_ages_buttons(client: FlaskClient, db_path: Path) -> None:
    _seed_provenance(db_path)
    resp = client.get("/api/peek/provenance?ticker=nu")  # lowercase: uppercased internally
    assert resp.status_code == 200
    body = resp.data.decode()
    assert not body.lstrip().lower().startswith("<!doctype")  # fragment, not a page
    assert 'data-prov-scope="NU"' in body
    for label in ("Report brief", "FMP endpoints", "IR documents", "News", "Earnings calendar"):
        assert label in body
    # Ages follow the stamp rules: relative inline, the exact UTC in the tooltip.
    assert 'title="2026-06-09 04:00 UTC"' in body  # report build
    assert 'title="2026-06-10 05:10 UTC"' in body  # newest FMP pull
    assert "2 endpoints · oldest" in body
    assert "next ER" in body
    # Refresh buttons POST the EXISTING action endpoints with the right bodies.
    assert 'data-prov-post="/actions/refresh"' in body
    assert "&quot;mode&quot;: &quot;stale&quot;" in body
    assert "&quot;steps&quot;: [&quot;fmp&quot;]" in body
    assert "&quot;steps&quot;: [&quot;news&quot;]" in body
    assert 'data-prov-post="/actions/refresh-ir"' in body
    # The calendar has no on-demand action — cron chip, not a button.
    assert body.count('data-prov-post="') == 4
    assert "cc-prov-cron" in body
    # In-peek SSE wiring (the /actions/stream frame contract) + the deep console path.
    assert "EventSource" in body
    assert "cc-prov-log" in body
    assert 'href="/#actions"' in body


def test_provenance_peek_portfolio_variant(client: FlaskClient, db_path: Path) -> None:
    _seed_provenance(db_path)
    resp = client.get("/api/peek/provenance")
    assert resp.status_code == 200
    body = resp.data.decode()
    assert 'data-prov-scope="portfolio"' in body
    assert "1 of 1 built" in body
    assert "oldest" in body  # the portfolio brief age is the worst case
    # Per-ticker-only actions don't leak into the portfolio variant; the one
    # repo-wide doc action (maintenance process_inbox) is the only button.
    assert "/actions/refresh-ir" not in body
    assert body.count('data-prov-post="') == 1
    assert 'data-prov-post="/actions/maintenance"' in body
    assert "&quot;process_inbox&quot;" in body


def test_provenance_peek_degrades_without_data(client: FlaskClient, tmp_path: Path) -> None:
    # Tables exist but are empty (untracked ticker): em-dash ages, still 200,
    # buttons still offered.
    resp = client.get("/api/peek/provenance?ticker=ZZZQ")
    assert resp.status_code == 200
    body = resp.data.decode()
    assert "—" in body
    assert 'data-prov-post="/actions/refresh"' in body
    # And with no database at all the renderer still produces the fragment.
    html = render_provenance_peek(tmp_path / "missing" / "portfolio.db", None)
    assert "cc-prov" in html
    assert "—" in html


# ----------------------------------------------------------------------------
# /api/peek/documents (S9 — the cockpit "N new docs" pill)
# ----------------------------------------------------------------------------


def _seed_new_docs(db_path: Path, ticker: str = "NU") -> int:
    """A built company with one doc fetched AFTER the build (new) and one
    BEFORE (not new). Returns the new doc's id."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO tracked_companies (ticker, name, list_type, last_built_at) "
            "VALUES (?, 'Nu Holdings', 'portfolio', '2026-06-09 04:00:00')",
            (ticker,),
        )
        cur = conn.execute(
            "INSERT INTO documents (ticker, source_type, doc_type, file_path, sha256, "
            "fetched_at, fetch_status) VALUES (?, 'sec', 'form_10q', "
            "'data/historical/fmp/NU_form_10q_2026.json', 'x', '2026-06-10 08:00:00', 'ok')",
            (ticker,),
        )
        new_id = int(cur.lastrowid or 0)
        conn.execute(
            "INSERT INTO documents (ticker, source_type, doc_type, file_path, sha256, "
            "fetched_at, fetch_status) VALUES (?, 'sec', 'form_10k', "
            "'data/historical/fmp/NU_form_10k_2025.json', 'y', '2026-06-08 08:00:00', 'ok')",
            (ticker,),
        )
        conn.commit()
        return new_id
    finally:
        conn.close()


def test_peek_documents_lists_docs_since_build(client: FlaskClient, db_path: Path) -> None:
    new_id = _seed_new_docs(db_path)
    resp = client.get("/api/peek/documents?ticker=nu")  # lowercase: uppercased internally
    assert resp.status_code == 200
    body = resp.data.decode()
    assert not body.lstrip().lower().startswith("<!doctype")  # fragment, not a page
    # The new doc shows, linked by id to the /source viewer (peek retargets there).
    assert f'href="/source/{new_id}"' in body
    assert "Form 10Q" in body  # humanized doc_type label
    assert "NU_form_10q_2026.json" in body  # basename, derived from file_path
    # The doc fetched BEFORE the build is not "new".
    assert "NU_form_10k_2025.json" not in body
    assert "open the holding" in body  # the overflow path


def test_peek_documents_empty_state(client: FlaskClient, db_path: Path) -> None:
    # Tracked but never built (last_built_at NULL) → nothing is "new".
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO tracked_companies (ticker, name) VALUES ('ABC', 'A')")
    conn.execute(
        "INSERT INTO documents (ticker, source_type, doc_type, file_path, sha256, "
        "fetched_at, fetch_status) VALUES ('ABC', 'sec', 'form_10q', 'x.json', 'z', "
        "'2026-06-10 08:00:00', 'ok')"
    )
    conn.commit()
    conn.close()
    assert "No documents fetched" in client.get("/api/peek/documents?ticker=ABC").data.decode()
    # No ticker → its own empty state, still 200.
    assert client.get("/api/peek/documents").status_code == 200


def test_new_docs_peek_degrades_without_db(tmp_path: Path) -> None:
    html = render_new_docs_peek(tmp_path / "missing" / "portfolio.db", ticker="NU")
    assert "No documents fetched" in html


# ----------------------------------------------------------------------------
# /api/peek/score (the cockpit Score chip breakdown)
# ----------------------------------------------------------------------------


def _seed_eval_company(db_path: Path, ticker: str = "DLO", name: str = "DLocal") -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO tracked_companies (ticker, name, list_type) VALUES (?, ?, 'evaluation')",
            (ticker, name),
        )
        conn.commit()
    finally:
        conn.close()


def test_peek_score_renders_breakdown_fragment(client: FlaskClient, db_path: Path) -> None:
    _seed_eval_company(db_path)
    resp = client.get("/api/peek/score?ticker=dlo")  # lowercase: uppercased internally
    assert resp.status_code == 200
    body = resp.data.decode()
    assert not body.lstrip().lower().startswith("<!doctype")  # fragment, not a page
    assert "Next-dollar attractiveness" in body
    # Every factor is named; with no DCF/fundamentals/PEG seeded each scores the
    # x0.85 miss, so the product is 0.85**4 = 0.52 and rows read "no data".
    for label in ("DCF upside", "Revenue growth", "FCF margin", "PEG"):
        assert label in body
    assert "no data" in body
    assert "0.52" in body
    assert 'href="/ticker/DLO"' in body  # the evaluation-report overflow path


def test_peek_score_untracked_and_missing_ticker_404(client: FlaskClient) -> None:
    assert client.get("/api/peek/score?ticker=ZZZQ").status_code == 404
    assert client.get("/api/peek/score").status_code == 404


# ----------------------------------------------------------------------------
# /api/peek/fit (the cockpit Fit chip breakdown)
# ----------------------------------------------------------------------------


def _write_fit_cache(repo: Path, ticker: str = "DLO") -> None:
    (repo / "data" / "candidate_fit.json").write_text(
        json.dumps(
            {
                "computed_at": "2026-06-14T04:00:00",
                "fits": {
                    ticker: {
                        "fit": 1.18,
                        "why": "sharpe 1.12 (...) x divers 1.05 (...) = 1.18",
                        "partial": True,
                        "obs": 200,
                        "factors": [
                            {
                                "key": "sharpe",
                                "label": "Marginal Sharpe",
                                "multiplier": 1.12,
                                "detail": "SR +0.40 ...",
                                "missing": False,
                            },
                            {
                                "key": "divers",
                                "label": "Diversification",
                                "multiplier": 1.05,
                                "detail": "corr +0.30",
                                "missing": False,
                            },
                            {
                                "key": "factor",
                                "label": "Factor fit",
                                "multiplier": 1.0,
                                "detail": "n/a",
                                "missing": True,
                            },
                            {
                                "key": "sector",
                                "label": "Sector fit",
                                "multiplier": 1.0,
                                "detail": "Energy 0% of book",
                                "missing": False,
                            },
                        ],
                    }
                },
            }
        ),
        encoding="utf-8",
    )


def test_peek_fit_renders_breakdown_fragment(client: FlaskClient, repo: Path) -> None:
    _write_fit_cache(repo)
    resp = client.get("/api/peek/fit?ticker=dlo")  # lowercase: uppercased internally
    assert resp.status_code == 200
    body = resp.data.decode()
    assert not body.lstrip().lower().startswith("<!doctype")  # fragment, not a page
    assert "Portfolio fit to the held book" in body
    for label in ("Marginal Sharpe", "Diversification", "Factor fit", "Sector fit"):
        assert label in body
    assert "1.18" in body  # the composite + formula
    assert "no data" in body  # the missing factor row
    assert 'href="/ticker/DLO"' in body  # the evaluation-report overflow path


def test_peek_fit_no_cache_or_missing_ticker_404(client: FlaskClient, repo: Path) -> None:
    assert client.get("/api/peek/fit?ticker=DLO").status_code == 404  # no cache file yet
    _write_fit_cache(repo, ticker="DLO")
    assert client.get("/api/peek/fit?ticker=ZZZQ").status_code == 404  # not in the cache
    assert client.get("/api/peek/fit").status_code == 404  # no ticker


# ----------------------------------------------------------------------------
# Markup contracts: peek triggers keep their real hrefs
# ----------------------------------------------------------------------------


def test_shell_carries_peek_primitive() -> None:
    html = render_shell(overview_html="<p>x</p>")
    assert 'id="cc-peek"' in html
    # The peek's scrim is the one shared CCOverlay .k-scrim now (no per-surface
    # cc-peek-scrim); the peek registers as a modal surface with its closeId.
    assert 'id="cc-peek-scrim"' not in html
    assert "closeId: 'cc-peek-close'" in SHELL_JS
    assert 'id="cc-hovercard"' in html
    # The JS contract: explicit opt-in attr, automatic /source peeks, the
    # hover endpoint, and the fragment variant marker.
    assert "a[data-peek-url]" in SHELL_JS
    assert 'a[href^="/source/"]' in SHELL_JS
    assert "/api/peek/ticker/" in SHELL_JS
    assert "fragment=1" in SHELL_JS
    # The data-ask-q doorway rail (S9): a separate delegate that reuses goAsk,
    # excludes the Ask panel, and yields to data-fact-ref (precedence contract).
    assert "ev.target.closest('[data-ask-q]')" in SHELL_JS
    assert "a.hasAttribute('data-fact-ref')" in SHELL_JS  # fact_ref wins
    assert "a.closest('[data-panel=\"explore\"]')" in SHELL_JS  # Ask owns its own chips


def test_cockpit_new_docs_pill_peeks_documents() -> None:
    base = DashboardRow(
        ticker="NU",
        list_type="portfolio",
        fmp_last_pulled=None,
        last_transcript=None,
        last_build_at=None,
        open_comments_count=0,
        breach_status=None,
    )
    html = render_research_cockpit({"portfolio": [CockpitRow(base=base, new_docs=3)]})
    assert "data-peek-url='/api/peek/documents?ticker=NU'" in html
    assert "href='/#holding=NU'" in html  # real destination preserved for middle-click
    assert "3 new docs" in html


def test_inbox_review_link_peeks_with_href_fallback(db_path: Path) -> None:
    alert, _qa = _seed_alert_with_action(db_path)
    items = collect_inbox(db_path, limit=10)
    html = render_inbox_stream(items, db_path=db_path, compact=True)
    assert f'data-peek-url="/api/peek/alert/{alert.id}"' in html
    assert 'href="/feed?ticker=NU"' in html  # real destination preserved


def test_cockpit_alert_pill_peeks_with_href_fallback() -> None:
    base = DashboardRow(
        ticker="NU",
        list_type="portfolio",
        fmp_last_pulled=None,
        last_transcript=None,
        last_build_at=None,
        open_comments_count=0,
        breach_status="warn",
    )
    html = render_research_cockpit({"portfolio": [CockpitRow(base=base, pending_alerts=2)]})
    assert "data-peek-url='/api/peek/alerts?ticker=NU&status=pending'" in html
    assert "href='/feed?ticker=NU&status=pending'" in html


def test_cockpit_portfolio_row_carries_review_doorway() -> None:
    """PR5: every Portfolio-list row gets a review pill (not count-gated like
    the alert/doc pills), peeking the instant pre-analysis in place."""
    base = DashboardRow(
        ticker="NU",
        list_type="portfolio",
        fmp_last_pulled=None,
        last_transcript=None,
        last_build_at=None,
        open_comments_count=0,
        breach_status=None,
    )
    html = render_research_cockpit({"portfolio": [CockpitRow(base=base)]})
    assert "data-peek-url='/api/peek/review/NU'" in html
    assert "href='/ticker/NU'" in html  # real destination preserved for middle-click


def test_cockpit_evaluation_row_has_no_review_doorway() -> None:
    """Evaluation/screen candidates aren't held positions — no review pill."""
    base = DashboardRow(
        ticker="DLO",
        list_type="evaluation",
        fmp_last_pulled=None,
        last_transcript=None,
        last_build_at=None,
        open_comments_count=0,
        breach_status=None,
    )
    html = render_research_cockpit({"evaluation": [CockpitRow(base=base)]})
    assert "/api/peek/review/" not in html


def test_next_dollar_memo_link_peeks(db_path: Path) -> None:
    _seed_memo(db_path)
    html = render_next_dollar_panel(db_path)
    assert 'data-peek-url="/api/peek/memo/next_dollar"' in html
    assert 'href="#advisor_memos"' in html  # tab deep-link kept for middle-click


def test_ticker_badge_carries_hover_attr(db_path: Path) -> None:
    alert, _qa = _seed_alert_with_action(db_path)
    out = StringIO()
    render_alert_card(out, alert, actions=[], show_status_badge=True)
    assert 'data-peek-ticker="NU"' in out.getvalue()


def test_cockpit_score_chip_peeks_breakdown() -> None:
    base = DashboardRow(
        ticker="DLO",
        list_type="evaluation",
        fmp_last_pulled=None,
        last_transcript=None,
        last_build_at=None,
        open_comments_count=0,
        breach_status=None,
    )
    row = CockpitRow(
        base=base,
        attractiveness=4.49,
        attractiveness_why="dcf 1.80 (+216.0% upside) x growth 1.60 (+65.2% YoY) = 4.49",
        attractiveness_partial=False,
    )
    html = render_research_cockpit({"evaluation": [row]})
    assert "data-peek-url='/api/peek/score?ticker=DLO'" in html
    assert "data-peek-title='Why score · DLO'" in html
    assert "href='/ticker/DLO'" in html  # real destination preserved for middle-click
    assert ">4.49</a>" in html  # the chip is the doorway <a>
    assert "k-chip k-chip-mono k-chip-ok" in html  # 4.49 >= 1.25 -> hi tone (kit ok chip)
    assert "title='dcf 1.80 (+216.0% upside)" in html  # the why stays in the hover


def test_cockpit_fit_chip_peeks_breakdown() -> None:
    base = DashboardRow(
        ticker="DLO",
        list_type="evaluation",
        fmp_last_pulled=None,
        last_transcript=None,
        last_build_at=None,
        open_comments_count=0,
        breach_status=None,
    )
    row = CockpitRow(
        base=base,
        attractiveness=4.49,
        fit=1.18,
        fit_why="sharpe 1.12 (...) x divers 1.05 (...) = 1.18",
        fit_partial=False,
    )
    html = render_research_cockpit({"evaluation": [row]})
    assert "data-peek-url='/api/peek/fit?ticker=DLO'" in html
    assert "data-peek-title='Portfolio fit · DLO'" in html
    assert "href='/ticker/DLO'" in html  # real destination preserved for middle-click
    assert ">1.18</a>" in html  # the fit chip is the doorway <a>
    assert "k-chip k-chip-mono k-chip-ok" in html  # 1.18 >= 1.10 -> accretive (kit ok chip)
    assert "title='sharpe 1.12 (...) x divers 1.05 (...) = 1.18'" in html  # why in the hover


def test_cockpit_staleness_dot_opens_provenance_peek() -> None:
    base = DashboardRow(
        ticker="NU",
        list_type="portfolio",
        fmp_last_pulled=None,
        last_transcript=None,
        last_build_at=None,
        open_comments_count=0,
        breach_status=None,
    )
    html = render_research_cockpit({"portfolio": [CockpitRow(base=base)]})
    assert "data-peek-url='/api/peek/provenance?ticker=NU'" in html
    assert "data-peek-title='Data provenance · NU'" in html
    assert "href='/#system'" in html  # real destination preserved for middle-click
    assert "FMP never" in html  # the tooltip stays the hover layer


def test_holding_band_freshness_dot_opens_provenance_peek(repo: Path) -> None:
    tcc = build_ticker_command_center(repo, "NU")
    frag = render_ticker_fragment(tcc)
    assert 'class="cc-fdot' in frag
    assert 'data-peek-url="/api/peek/provenance?ticker=NU"' in frag
    assert 'href="/#system"' in frag


def test_holding_band_carries_review_doorway(repo: Path) -> None:
    """PR5: the Holding band's utility links row (Report/DCF/Tracker) gains a
    Review link peeking the instant pre-analysis in place."""
    tcc = build_ticker_command_center(repo, "NU")
    frag = render_ticker_fragment(tcc)
    assert 'data-peek-url="/api/peek/review/NU"' in frag
    assert 'href="/ticker/NU"' in frag  # real destination preserved for middle-click
    assert ">Review<" in frag


def test_tier_strip_chips_open_provenance_peek() -> None:
    html = render_tier_coverage_strip(
        {
            "P1": {"fresh": 2, "stale": 0, "total": 2},
            "P2": {"fresh": 1, "stale": 3, "total": 4},
            "P3": {"fresh": 0, "stale": 0, "total": 0},
        }
    )
    # Every chip peeks the portfolio-wide provenance card...
    assert html.count('data-peek-url="/api/peek/provenance"') == 3
    assert 'href="/#system"' in html
    # ...while the cron-hint tooltips stay the hover layer.
    assert 'title="P1 — all fresh"' in html
    assert "To force-refresh" in html
