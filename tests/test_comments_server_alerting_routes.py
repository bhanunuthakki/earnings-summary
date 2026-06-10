"""Route tests for the Personal-CIO alerting surfaces wired into the command
center: ``GET /digest``, ``/feed``, ``/alerts``, and the queued-action cards'
one-click ``GET /approve``.

Before this, the morning-digest / alert-feed renderers were reachable only as
static files (``data/dashboard/...``) — a user living in the :7421 app never saw
their alerts. These tests prove the live routes exist and serve the renderers.
The substrate is built via alembic (stamp the pre-CIO head, upgrade to head),
mirroring tests/test_dashboard_feed.py.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic.config import Config
from flask.testing import FlaskClient

from alembic import command
from alerts import (
    ACTION_STATUS_APPLIED,
    ACTION_STATUS_CANCELLED,
    ACTION_STATUS_PENDING,
    fire_alert,
    get_action,
    queue_action,
)
from user_state.ledger import list_entries

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "execution"))

import comments_server  # noqa: E402

_PRIOR_HEAD = "0059_kpi_facts_restatement"


def _build_db(db_path: Path) -> None:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.stamp(cfg, _PRIOR_HEAD)
    command.upgrade(cfg, "head")


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    db = tmp_path / "data" / "portfolio.db"
    db.parent.mkdir(parents=True)
    _build_db(db)
    return db


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


def test_digest_route_renders_html(client) -> None:
    resp = client.get("/digest")
    assert resp.status_code == 200
    assert resp.mimetype == "text/html"
    assert b"<html" in resp.data.lower()


def test_digest_route_tolerates_bad_date(client) -> None:
    # A malformed ?date= falls back to today rather than 500-ing.
    resp = client.get("/digest?date=not-a-date")
    assert resp.status_code == 200


def test_feed_route_renders_empty_state(client) -> None:
    resp = client.get("/feed")
    assert resp.status_code == 200
    body = resp.data.decode()
    assert "Alert feed" in body
    assert "No alerts match" in body  # empty substrate → empty-state document


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


def test_approve_route_applies_action_and_writes_ledger(client: FlaskClient, db_path: Path) -> None:
    action_id = _seed_pending_action(db_path, ticker="NU")
    resp = client.get(
        f"/approve?action_id={action_id}",
        headers={"Referer": "http://127.0.0.1:7421/digest?date=2026-06-10"},
    )
    assert resp.status_code == 303
    # Bounced back to the surface the click came from — path + query only.
    assert resp.headers["Location"].endswith("/digest?date=2026-06-10")
    assert not resp.headers["Location"].startswith("http")
    assert get_action(action_id, db_path=db_path).status == ACTION_STATUS_APPLIED
    entries = list_entries(ticker="NU", db_path=db_path)
    assert len(entries) == 1
    assert entries[0].entry_kind == "thesis_update"
    assert entries[0].body == "Deposit franchise scaling ahead of plan"


def test_approve_route_dismiss_cancels_without_ledger_write(
    client: FlaskClient, db_path: Path
) -> None:
    action_id = _seed_pending_action(db_path, ticker="GOOG")
    resp = client.get(f"/approve?action_id={action_id}&dismiss=1")
    assert resp.status_code == 303
    assert resp.headers["Location"].endswith("/feed")  # no Referer → the feed
    assert get_action(action_id, db_path=db_path).status == ACTION_STATUS_CANCELLED
    assert list_entries(ticker="GOOG", db_path=db_path) == []


def test_approve_route_404_on_unknown_action(client: FlaskClient) -> None:
    resp = client.get("/approve?action_id=424242")
    assert resp.status_code == 404
    payload = resp.get_json()
    assert payload is not None
    assert "not found" in payload["error"]


def test_approve_route_409_on_double_click(client: FlaskClient, db_path: Path) -> None:
    action_id = _seed_pending_action(db_path)
    assert client.get(f"/approve?action_id={action_id}").status_code == 303
    resp = client.get(f"/approve?action_id={action_id}")
    assert resp.status_code == 409
    payload = resp.get_json()
    assert payload is not None
    assert "cannot transition" in payload["error"]
    # The first click's ledger write is not duplicated by the conflict.
    assert len(list_entries(ticker="NU", db_path=db_path)) == 1


def test_approve_route_400_on_missing_or_bad_action_id(client: FlaskClient) -> None:
    assert client.get("/approve").status_code == 400
    assert client.get("/approve?action_id=abc").status_code == 400


def test_approve_route_rejects_cross_site_click(client: FlaskClient, db_path: Path) -> None:
    # State-changing GET → the route carries its own same-site guard (the
    # JSON-POST CORS defense doesn't cover top-level navigations). Neither a
    # foreign Referer nor an explicit cross-site fetch may mutate.
    action_id = _seed_pending_action(db_path)
    via_referer = client.get(
        f"/approve?action_id={action_id}",
        headers={"Referer": "https://evil.example/payload"},
    )
    assert via_referer.status_code == 403
    via_fetch_metadata = client.get(
        f"/approve?action_id={action_id}",
        headers={"Sec-Fetch-Site": "cross-site"},
    )
    assert via_fetch_metadata.status_code == 403
    assert get_action(action_id, db_path=db_path).status == ACTION_STATUS_PENDING
    assert list_entries(ticker="NU", db_path=db_path) == []


def test_feed_renders_absolute_approve_links(client: FlaskClient, db_path: Path) -> None:
    # The card renders on /, /digest, AND /feed; a relative "approve?..." href
    # resolved to a different (dead) path per surface. It must be absolute.
    action_id = _seed_pending_action(db_path)
    html = client.get("/feed").data.decode()
    assert f'href="/approve?action_id={action_id}"' in html
    assert f'href="/approve?action_id={action_id}&dismiss=1"' in html
