"""Route tests for the Personal-CIO alerting surfaces wired into the command
center: ``GET /digest``, ``/feed``, ``/alerts``.

Before this, the morning-digest / alert-feed renderers were reachable only as
static files (``data/dashboard/...``) — a user living in the :7421 app never saw
their alerts. These tests prove the live routes exist and serve the renderers.
The substrate is built via alembic (stamp the pre-CIO head, upgrade to head),
mirroring tests/test_dashboard_feed.py.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from alembic.config import Config

from alembic import command

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
def client(tmp_path: Path):
    db = tmp_path / "data" / "portfolio.db"
    db.parent.mkdir(parents=True)
    _build_db(db)
    return comments_server.create_app(tmp_path).test_client()


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
