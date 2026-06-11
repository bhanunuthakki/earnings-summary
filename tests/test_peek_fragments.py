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
from pipeline.command_center_shell import SHELL_JS, render_shell
from pipeline.dashboard_status import DashboardRow
from pipeline.portfolio_panel import render_next_dollar_panel
from pipeline.research_cockpit import CockpitRow, render_research_cockpit

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
    archived_at TEXT
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
    assert "ticker-badge" in body
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
# Markup contracts: peek triggers keep their real hrefs
# ----------------------------------------------------------------------------


def test_shell_carries_peek_primitive() -> None:
    html = render_shell(overview_html="<p>x</p>")
    assert 'id="cc-peek"' in html
    assert 'id="cc-peek-scrim"' in html
    assert 'id="cc-hovercard"' in html
    # The JS contract: explicit opt-in attr, automatic /source peeks, the
    # hover endpoint, and the fragment variant marker.
    assert "a[data-peek-url]" in SHELL_JS
    assert 'a[href^="/source/"]' in SHELL_JS
    assert "/api/peek/ticker/" in SHELL_JS
    assert "fragment=1" in SHELL_JS


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
