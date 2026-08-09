"""Route tests for the Personal-CIO alerting surfaces wired into the command
center: ``GET /feed``, ``/alerts``, the retired ``/digest`` redirect, and the
queued-action cards' one-click ``GET /approve``.

Before this, the alert-feed renderer was reachable only as a static file
(``data/dashboard/...``) — a user living in the :7421 app never saw their
alerts. These tests prove the live routes exist and serve the renderers. (The
standalone /digest page retired 2026-06-11; its route stays as a redirect to
the Home rail.) The substrate is built via alembic (stamp the pre-CIO head,
upgrade to head), mirroring tests/test_dashboard_feed.py.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest
from flask.testing import FlaskClient

from alerts import (
    ACTION_STATUS_APPLIED,
    ACTION_STATUS_CANCELLED,
    ACTION_STATUS_PENDING,
    ALERT_STATUS_APPROVED,
    ALERT_STATUS_DISMISSED,
    ALERT_STATUS_PENDING,
    fire_alert,
    get_action,
    get_alert,
    list_queued_actions_for_alert,
    queue_action,
)
from user_state.ledger import list_entries

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "execution"))

import comments_server  # noqa: E402

_PRIOR_HEAD = "0059_kpi_facts_restatement"


@pytest.fixture
def db_path(tmp_path: Path, migrated_db: Callable[..., Path]) -> Path:
    return migrated_db(tmp_path / "data" / "portfolio.db", stamp=_PRIOR_HEAD)


@pytest.fixture
def client(db_path: Path, tmp_path: Path) -> FlaskClient:
    # db_path builds tmp_path/data/portfolio.db (the path create_app derives
    # from its repo_root) before the app boots.
    assert db_path.exists()
    return comments_server.create_app(tmp_path).test_client()


def _seed_pending_action(
    db_path: Path,
    *,
    ticker: str = "NU",
    body: str = "Deposit franchise scaling ahead of plan",
    signature: str = "sig-approve-route",
) -> int:
    """Seed one pending alert + queued thesis_update action; return the action id.

    The payload deliberately omits ``ticker`` — the real trigger-drafted shape —
    so approving through the route also exercises the parent-alert ticker
    resolution (the #231 regression)."""
    alert = fire_alert(
        ticker=ticker,
        trigger_kind="kpi_inflection",
        fired_at=datetime.now(UTC),
        evidence_json='{"summary": "route test"}',
        signature_sha=signature,
        db_path=db_path,
    )
    qa = queue_action(
        alert_id=alert.id,
        action_kind="thesis_update",
        payload={"body": body},
        db_path=db_path,
    )
    return qa.id


def test_digest_route_redirects_to_home(client) -> None:
    """The standalone digest retired (2026-06-11): /digest 302s to the Home
    rail — old bookmarks (with or without query args) land on the shell,
    never a 404."""
    for url in ("/digest", "/digest?date=2026-06-10", "/digest?date=not-a-date"):
        resp = client.get(url, follow_redirects=False)
        assert resp.status_code == 302
        assert resp.headers["Location"].endswith("/#home")


def test_feed_route_renders_empty_state(client) -> None:
    resp = client.get("/feed")
    assert resp.status_code == 200
    body = resp.data.decode()
    assert "Inbox feed" in body
    assert "No items match" in body  # empty substrate → empty-state document


def test_feed_route_echoes_filters(client) -> None:
    resp = client.get("/feed?ticker=NU&status=pending&limit=10")
    assert resp.status_code == 200
    assert "NU" in resp.data.decode()  # the filter strip echoes the slice


def test_alerts_alias_redirects_to_feed_preserving_filters(client) -> None:
    resp = client.get("/alerts?ticker=NU", follow_redirects=False)
    assert resp.status_code in (301, 302)
    location = resp.headers["Location"]
    assert "/feed" in location
    assert "ticker=NU" in location


# ----------------------------------------------------------------------------
# GET /approve — the queued-action cards' one-click approve / dismiss links.
# Same store transitions + ledger side effects as the approve CLI (the route
# calls the CLI module's shared core).
# ----------------------------------------------------------------------------


def test_approve_get_confirms_then_post_applies_and_writes_ledger(
    client: FlaskClient, db_path: Path
) -> None:
    action_id = _seed_pending_action(db_path, ticker="NU")
    resp = client.get(
        f"/approve?action_id={action_id}",
        headers={"Referer": "http://127.0.0.1:7421/feed?ticker=NU"},
    )
    assert resp.status_code == 200
    assert 'method="post"' in resp.get_data(as_text=True).lower()
    assert get_action(action_id, db_path=db_path).status == ACTION_STATUS_PENDING

    resp = client.post(
        "/approve",
        data={
            "action_id": str(action_id),
            "confirm": "1",
            "return_to": "/feed?ticker=NU",
        },
    )
    assert resp.status_code == 303
    assert resp.headers["Location"].endswith("/feed?ticker=NU")
    assert get_action(action_id, db_path=db_path).status == ACTION_STATUS_APPLIED
    entries = list_entries(ticker="NU", db_path=db_path)
    assert len(entries) == 1
    assert entries[0].entry_kind == "thesis_update"
    assert entries[0].body == "Deposit franchise scaling ahead of plan"


def test_approve_confirmation_dismiss_cancels_without_ledger_write(
    client: FlaskClient, db_path: Path
) -> None:
    action_id = _seed_pending_action(db_path, ticker="GOOG")
    resp = client.get(f"/approve?action_id={action_id}&dismiss=1")
    assert resp.status_code == 200
    assert get_action(action_id, db_path=db_path).status == ACTION_STATUS_PENDING
    resp = client.post(
        "/approve",
        data={"action_id": str(action_id), "dismiss": "1", "confirm": "1"},
    )
    assert resp.status_code == 303
    assert resp.headers["Location"].endswith("/feed")  # no Referer → the feed
    assert get_action(action_id, db_path=db_path).status == ACTION_STATUS_CANCELLED
    assert list_entries(ticker="GOOG", db_path=db_path) == []


def test_approve_route_404_on_unknown_action(client: FlaskClient) -> None:
    resp = client.post("/approve", data={"action_id": "424242"})
    assert resp.status_code == 404
    payload = resp.get_json()
    assert payload is not None
    assert "not found" in payload["error"]


def test_approve_route_409_on_double_click(client: FlaskClient, db_path: Path) -> None:
    action_id = _seed_pending_action(db_path)
    assert client.post("/approve", data={"action_id": str(action_id)}).status_code == 200
    resp = client.post("/approve", data={"action_id": str(action_id)})
    assert resp.status_code == 409
    payload = resp.get_json()
    assert payload is not None
    assert "cannot transition" in payload["error"]
    # The first click's ledger write is not duplicated by the conflict.
    assert len(list_entries(ticker="NU", db_path=db_path)) == 1


def test_approve_route_400_on_missing_or_bad_action_id(client: FlaskClient) -> None:
    assert client.get("/approve").status_code == 400
    assert client.get("/approve?action_id=abc").status_code == 400


def test_approve_get_never_mutates_for_cross_site_or_headerless_click(
    client: FlaskClient, db_path: Path
) -> None:
    action_id = _seed_pending_action(db_path)
    via_referer = client.get(
        f"/approve?action_id={action_id}",
        headers={"Referer": "https://evil.example/payload"},
    )
    assert via_referer.status_code == 200
    via_fetch_metadata = client.get(
        f"/approve?action_id={action_id}",
        headers={"Sec-Fetch-Site": "cross-site"},
    )
    assert via_fetch_metadata.status_code == 200
    assert client.get(f"/approve?action_id={action_id}").status_code == 200
    assert get_action(action_id, db_path=db_path).status == ACTION_STATUS_PENDING
    assert list_entries(ticker="NU", db_path=db_path) == []


def test_feed_renders_absolute_approve_links(client: FlaskClient, db_path: Path) -> None:
    # The card renders on / AND /feed; a relative "approve?..." href
    # resolved to a different (dead) path per surface. It must be absolute.
    # The target is the ALERT, not one of its queued actions — settling a
    # single action left the alert 'pending' and the card in the queue.
    _seed_pending_action(db_path)
    html = client.get("/feed").data.decode()
    assert 'href="/approve?alert_id=' in html
    assert '&dismiss=1"' in html
    assert 'href="approve?' not in html


# ----------------------------------------------------------------------------
# POST /approve — the Home rail's hover ✓/✕ fetch variant (Inbox v2). The
# state-changing POST retains the route and global same-site guards; JSON out
# lets the card update in place without a reload.
# ----------------------------------------------------------------------------


def test_approve_post_applies_and_returns_json(client: FlaskClient, db_path: Path) -> None:
    action_id = _seed_pending_action(db_path, ticker="NU")
    resp = client.post("/approve", data={"action_id": str(action_id)})
    assert resp.status_code == 200
    assert resp.get_json() == {"ok": True, "action_id": action_id, "status": "applied"}
    assert get_action(action_id, db_path=db_path).status == ACTION_STATUS_APPLIED
    entries = list_entries(ticker="NU", db_path=db_path)
    assert len(entries) == 1
    assert entries[0].body == "Deposit franchise scaling ahead of plan"


def test_approve_post_dismiss_cancels_and_returns_json(client: FlaskClient, db_path: Path) -> None:
    action_id = _seed_pending_action(db_path, ticker="GOOG")
    resp = client.post("/approve", data={"action_id": str(action_id), "dismiss": "1"})
    assert resp.status_code == 200
    assert resp.get_json() == {"ok": True, "action_id": action_id, "status": "cancelled"}
    assert get_action(action_id, db_path=db_path).status == ACTION_STATUS_CANCELLED
    assert list_entries(ticker="GOOG", db_path=db_path) == []


def test_approve_post_rejects_cross_site(client: FlaskClient, db_path: Path) -> None:
    # The urlencoded-form POST never triggers a CORS preflight, so the route's
    # own same-site guard is the only thing between a hostile page and the DB.
    action_id = _seed_pending_action(db_path)
    via_fetch_metadata = client.post(
        "/approve",
        data={"action_id": str(action_id)},
        headers={"Sec-Fetch-Site": "cross-site"},
    )
    assert via_fetch_metadata.status_code == 403
    via_referer = client.post(
        "/approve",
        data={"action_id": str(action_id)},
        headers={"Referer": "https://evil.example/payload"},
    )
    assert via_referer.status_code == 403
    assert get_action(action_id, db_path=db_path).status == ACTION_STATUS_PENDING


def test_approve_post_conflict_stays_json_409(client: FlaskClient, db_path: Path) -> None:
    action_id = _seed_pending_action(db_path)
    assert client.post("/approve", data={"action_id": str(action_id)}).status_code == 200
    resp = client.post("/approve", data={"action_id": str(action_id)})
    assert resp.status_code == 409
    payload = resp.get_json()
    assert payload is not None
    assert "cannot transition" in payload["error"]
    assert len(list_entries(ticker="NU", db_path=db_path)) == 1


# ----------------------------------------------------------------------------
# Consequence receipts (REQ-11): the HTMX quick-action path renders the exact
# outcome approve_and_apply produced, instead of a bare "✓ applied" chip.
# ----------------------------------------------------------------------------


def test_approve_htmx_returns_consequence_detail(client: FlaskClient, db_path: Path) -> None:
    action_id = _seed_pending_action(db_path, ticker="NU")
    resp = client.post(
        "/approve", data={"action_id": str(action_id)}, headers={"HX-Request": "true"}
    )
    assert resp.status_code == 200
    body = resp.data.decode()
    assert "ix-acted-detail" in body
    # approve_and_apply's exact returned string (execution/approve_queued_action.py)
    assert f"Approved queued_action id={action_id}" in body
    assert "Ledger entry id=" in body
    assert "NU" in body
    # A written Ledger entry names a doorway to the Decisions panel.
    assert 'href="/#decisions_record"' in body


def test_approve_htmx_dismiss_carries_no_consequence_detail(
    client: FlaskClient, db_path: Path
) -> None:
    """A dismiss (no downstream write) keeps its plain undo-carrying chip —
    detail is only ever populated by approve_and_apply's return value."""
    action_id = _seed_pending_action(db_path, ticker="GOOG")
    resp = client.post(
        "/approve",
        data={"action_id": str(action_id), "dismiss": "1"},
        headers={"HX-Request": "true"},
    )
    assert resp.status_code == 200
    body = resp.data.decode()
    assert "ix-acted-detail" not in body
    assert "ix-undo" in body


# ----------------------------------------------------------------------------
# POST /api/alerts/<id>/dismiss — the inbox rail's hover ✕ on an alert card.
# The alert-level counterpart to /approve's action-level dismiss: it clears
# the whole alert AND cancels its still-pending drafts so none resurface.
# ----------------------------------------------------------------------------


def test_dismiss_alert_route_clears_alert_and_cancels_pending_actions(
    client: FlaskClient, db_path: Path
) -> None:
    action_id = _seed_pending_action(db_path, ticker="NU")
    alert_id = get_action(action_id, db_path=db_path).alert_id
    resp = client.post(f"/api/alerts/{alert_id}/dismiss")
    assert resp.status_code == 200
    assert resp.get_json() == {
        "ok": True,
        "alert_id": alert_id,
        "status": ALERT_STATUS_DISMISSED,
        "dismiss_reason": None,  # 0142: no reason supplied on this dismiss
        "cancelled_actions": 1,
    }
    # The alert is dismissed and its pending draft cancelled — no orphan to
    # resurface as a standalone inbox card — and no ledger write happened.
    assert get_alert(alert_id, db_path=db_path).status == ALERT_STATUS_DISMISSED
    assert get_action(action_id, db_path=db_path).status == ACTION_STATUS_CANCELLED
    assert list_entries(ticker="NU", db_path=db_path) == []


def test_dismiss_alert_route_works_without_a_drafted_action(
    client: FlaskClient, db_path: Path
) -> None:
    alert = fire_alert(
        ticker="NU",
        trigger_kind="earnings_tone",
        fired_at=datetime.now(UTC),
        evidence_json='{"summary": "tone test"}',
        signature_sha="sig-dismiss-no-action",
        db_path=db_path,
    )
    resp = client.post(f"/api/alerts/{alert.id}/dismiss")
    assert resp.status_code == 200
    assert resp.get_json()["cancelled_actions"] == 0
    assert get_alert(alert.id, db_path=db_path).status == ALERT_STATUS_DISMISSED


def test_dismiss_alert_route_404_on_unknown(client: FlaskClient) -> None:
    resp = client.post("/api/alerts/424242/dismiss")
    assert resp.status_code == 404


def test_dismiss_alert_route_409_on_double_click(client: FlaskClient, db_path: Path) -> None:
    alert = fire_alert(
        ticker="NU",
        trigger_kind="earnings_tone",
        fired_at=datetime.now(UTC),
        evidence_json='{"summary": "tone test"}',
        signature_sha="sig-dismiss-twice",
        db_path=db_path,
    )
    assert client.post(f"/api/alerts/{alert.id}/dismiss").status_code == 200
    resp = client.post(f"/api/alerts/{alert.id}/dismiss")
    assert resp.status_code == 409


def test_dismiss_alert_route_rejects_cross_site(client: FlaskClient, db_path: Path) -> None:
    action_id = _seed_pending_action(db_path, ticker="NU")
    alert_id = get_action(action_id, db_path=db_path).alert_id
    resp = client.post(
        f"/api/alerts/{alert_id}/dismiss",
        headers={"Sec-Fetch-Site": "cross-site"},
    )
    assert resp.status_code == 403
    # Nothing mutated — the alert and its draft are both untouched.
    assert get_alert(alert_id, db_path=db_path).status == ALERT_STATUS_PENDING
    assert get_action(action_id, db_path=db_path).status == ACTION_STATUS_PENDING


# ----------------------------------------------------------------------------
# Dismiss-with-reason (alerts lane, v1, 0142): the "why?" affordance's
# deferred round-trip — a second POST to the SAME endpoint, alert already
# dismissed, body carrying only {reason}.
# ----------------------------------------------------------------------------


def test_dismiss_alert_htmx_chip_carries_why_affordance(client: FlaskClient, db_path: Path) -> None:
    alert = fire_alert(
        ticker="NU",
        trigger_kind="earnings_tone",
        fired_at=datetime.now(UTC),
        evidence_json='{"summary": "tone test"}',
        signature_sha="sig-dismiss-why",
        db_path=db_path,
    )
    resp = client.post(f"/api/alerts/{alert.id}/dismiss", headers={"HX-Request": "true"})
    assert resp.status_code == 200
    body = resp.data.decode()
    assert "ix-why-toggle" in body
    assert f'hx-post="/api/alerts/{alert.id}/dismiss"' in body


def test_dismiss_alert_reason_round_trip_attaches_without_retransition(
    client: FlaskClient, db_path: Path
) -> None:
    from alerts import ALERT_STATUS_DISMISSED as _DISMISSED

    alert = fire_alert(
        ticker="NU",
        trigger_kind="earnings_tone",
        fired_at=datetime.now(UTC),
        evidence_json='{"summary": "tone test"}',
        signature_sha="sig-dismiss-reason",
        db_path=db_path,
    )
    first = client.post(f"/api/alerts/{alert.id}/dismiss")
    assert first.status_code == 200
    assert first.get_json()["dismiss_reason"] is None

    second = client.post(
        f"/api/alerts/{alert.id}/dismiss",
        json={"reason": "already knew"},
    )
    assert second.status_code == 200
    payload = second.get_json()
    assert payload == {
        "ok": True,
        "alert_id": alert.id,
        "status": _DISMISSED,
        "dismiss_reason": "already knew",
        "cancelled_actions": 0,
    }
    assert get_alert(alert.id, db_path=db_path).dismiss_reason == "already knew"


def test_dismiss_alert_reason_htmx_returns_empty_body(client: FlaskClient, db_path: Path) -> None:
    """Once a reason lands, nothing further renders (signal capture, not
    ceremony) — the response is empty so HTMX's outerHTML swap removes the
    whole .ix-dismiss-why affordance."""
    alert = fire_alert(
        ticker="NU",
        trigger_kind="earnings_tone",
        fired_at=datetime.now(UTC),
        evidence_json='{"summary": "tone test"}',
        signature_sha="sig-dismiss-reason-htmx",
        db_path=db_path,
    )
    client.post(f"/api/alerts/{alert.id}/dismiss")
    resp = client.post(
        f"/api/alerts/{alert.id}/dismiss",
        data={"reason": "wrong ticker"},
        headers={"HX-Request": "true"},
    )
    assert resp.status_code == 200
    assert resp.data.decode() == ""
    assert get_alert(alert.id, db_path=db_path).dismiss_reason == "wrong ticker"


def test_dismiss_alert_double_click_without_reason_still_409s(
    client: FlaskClient, db_path: Path
) -> None:
    """A bare re-POST on an already-dismissed alert (no reason attached) is
    the stale/double-click case, not the reason round-trip — it must keep
    raising the same 409 the pre-0142 behavior did."""
    alert = fire_alert(
        ticker="NU",
        trigger_kind="earnings_tone",
        fired_at=datetime.now(UTC),
        evidence_json='{"summary": "tone test"}',
        signature_sha="sig-dismiss-doubleclick",
        db_path=db_path,
    )
    assert client.post(f"/api/alerts/{alert.id}/dismiss").status_code == 200
    resp = client.post(f"/api/alerts/{alert.id}/dismiss")
    assert resp.status_code == 409


def test_dismiss_alert_reason_supplied_on_first_call(client: FlaskClient, db_path: Path) -> None:
    """A reason supplied in the SAME call that performs the dismiss is
    honored in one round-trip (not just the deferred second call)."""
    alert = fire_alert(
        ticker="NU",
        trigger_kind="earnings_tone",
        fired_at=datetime.now(UTC),
        evidence_json='{"summary": "tone test"}',
        signature_sha="sig-dismiss-firstcall-reason",
        db_path=db_path,
    )
    resp = client.post(f"/api/alerts/{alert.id}/dismiss", json={"reason": "stale trigger"})
    assert resp.status_code == 200
    assert resp.get_json()["dismiss_reason"] == "stale trigger"
    assert get_alert(alert.id, db_path=db_path).dismiss_reason == "stale trigger"


# ----------------------------------------------------------------------------
# GET / — the Home rail end-to-end (Inbox v2: chips + ranking + quick ✓/✕)
# ----------------------------------------------------------------------------


def test_home_rail_renders_ranked_inbox_with_chips_and_quick_actions(
    client: FlaskClient, db_path: Path
) -> None:
    """The lazy Overview fragment renders the complete Inbox rail."""
    import sqlite3

    # The cockpit half of / reads init_db-owned tables the alembic-built
    # fixture lacks — minimal shapes, mirroring test_comments_server_dashboard.
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS tracked_companies (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT DEFAULT 'bhanu',
            ticker TEXT NOT NULL, name TEXT NOT NULL, list_type TEXT NOT NULL,
            added_at TIMESTAMP, sec_validated INTEGER DEFAULT 0, ir_url TEXT,
            instrument_type TEXT, filing_regime TEXT, fiscal_year_end TEXT,
            fmp_data_saved INTEGER DEFAULT 0, fmp_data_upto TEXT, archived_at TIMESTAMP);
        CREATE TABLE IF NOT EXISTS transcripts (
            id INTEGER PRIMARY KEY AUTOINCREMENT, document_id INTEGER,
            ticker TEXT NOT NULL, call_date TIMESTAMP, fiscal_period_type TEXT,
            period_end TIMESTAMP, source_url TEXT, has_qa_section INTEGER);
        CREATE TABLE IF NOT EXISTS thesis_evaluations (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ticker TEXT NOT NULL,
            evaluated_at TIMESTAMP NOT NULL, overall_status TEXT NOT NULL,
            rule_evaluations_json TEXT, run_id TEXT);
        CREATE TABLE IF NOT EXISTS fmp_endpoint_status (
            ticker TEXT NOT NULL, endpoint TEXT NOT NULL, period TEXT NOT NULL,
            status TEXT, http_code INTEGER, record_count INTEGER, earliest_date TEXT,
            latest_date TEXT, file_path TEXT, file_bytes INTEGER, error_msg TEXT,
            last_pulled TIMESTAMP);
        INSERT INTO tracked_companies (ticker, name, list_type, instrument_type)
            VALUES ('NU', 'Nu Holdings', 'portfolio', 'equity');
        """
    )
    conn.commit()
    conn.close()
    action_id = _seed_pending_action(db_path, ticker="NU")

    shell = client.get("/").data.decode()
    assert 'hx-get="/api/panel/overview"' in shell
    body = client.get("/api/panel/overview").data.decode()
    assert 'data-ix-badge="home"' in body  # unread count badge hook in the rail head
    assert 'class="ix-cats"' in body  # category chips on the rail
    assert 'data-cat="thesis"' in body  # the kpi_inflection alert's facet
    assert f'data-action-id="{action_id}"' in body  # hover quick ✓/✕
    assert 'title="ranked: severity' in body  # why-ranked tooltip
    assert "ix-last-seen:" in body  # INBOX_JS shipped with the rail


# ----------------------------------------------------------------------------
# GET /approve?alert_id=N — the card-level settle.
#
# The feed footer used to target one queued action. Approving settled that
# action and wrote its ledger entry, but left the parent alert 'pending' — and
# the inbox fetches pending alerts unbounded, so the card never left while the
# new ledger entry rendered beside it as a second card. Alerts also carry many
# drafts in prod (9 on FCX 28, 17 on NU 1), so one click cleared one of N.
# ----------------------------------------------------------------------------


def test_approve_alert_settles_every_action_and_clears_the_card(
    client: FlaskClient, db_path: Path
) -> None:
    action_id = _seed_pending_action(db_path, ticker="NU")
    alert_id = get_action(action_id, db_path=db_path).alert_id
    queue_action(
        alert_id=alert_id,
        action_kind="bear_append",
        payload={"body": "second draft on the same alert"},
        db_path=db_path,
    )

    resp = client.get(
        f"/approve?alert_id={alert_id}",
        headers={"Referer": "http://127.0.0.1:7421/feed"},
    )
    assert resp.status_code == 200
    assert get_alert(alert_id, db_path=db_path).status == ALERT_STATUS_PENDING
    resp = client.post(
        "/approve",
        data={"alert_id": str(alert_id), "confirm": "1", "return_to": "/feed"},
    )
    assert resp.status_code == 303
    actions = list_queued_actions_for_alert(alert_id, db_path=db_path)
    assert [qa.status for qa in actions] == [ACTION_STATUS_APPLIED] * 2
    assert get_alert(alert_id, db_path=db_path).status == ALERT_STATUS_APPROVED
    assert len(list_entries(ticker="NU", db_path=db_path)) == 2


def test_dismiss_alert_via_approve_route_cancels_and_clears(
    client: FlaskClient, db_path: Path
) -> None:
    action_id = _seed_pending_action(db_path, ticker="NU")
    alert_id = get_action(action_id, db_path=db_path).alert_id

    resp = client.get(
        f"/approve?alert_id={alert_id}&dismiss=1",
        headers={"Referer": "http://127.0.0.1:7421/feed"},
    )
    assert resp.status_code == 200
    assert get_alert(alert_id, db_path=db_path).status == ALERT_STATUS_PENDING
    resp = client.post(
        "/approve",
        data={
            "alert_id": str(alert_id),
            "dismiss": "1",
            "confirm": "1",
            "return_to": "/feed",
        },
    )
    assert resp.status_code == 303
    assert get_action(action_id, db_path=db_path).status == ACTION_STATUS_CANCELLED
    assert get_alert(alert_id, db_path=db_path).status == ALERT_STATUS_DISMISSED
    assert list_entries(ticker="NU", db_path=db_path) == []


def test_approve_alert_cross_site_get_is_read_only(client: FlaskClient, db_path: Path) -> None:
    """A hostile navigation may show confirmation, but cannot settle the alert."""
    action_id = _seed_pending_action(db_path, ticker="NU")
    alert_id = get_action(action_id, db_path=db_path).alert_id

    resp = client.get(f"/approve?alert_id={alert_id}", headers={"Sec-Fetch-Site": "cross-site"})

    assert resp.status_code == 200
    assert get_alert(alert_id, db_path=db_path).status == ALERT_STATUS_PENDING
    assert get_action(action_id, db_path=db_path).status == ACTION_STATUS_PENDING


def test_approve_alert_bad_id_is_400_not_500(client: FlaskClient, db_path: Path) -> None:
    resp = client.get("/approve?alert_id=notanint")
    assert resp.status_code == 400
    assert "alert_id must be an integer" in resp.get_json()["error"]
