"""Route tests for the position-lifecycle surfaces (fund-grade S5, PR 2):
``GET /api/position-lifecycle/<ticker>`` (the holding page's refreshable
timeline fragment) and ``POST /api/position-entries/<id>`` (the analyst's
post-exit grading).

The substrate is built via alembic (stamp a prior head, upgrade to head) —
mirroring test_comments_server_alerting_routes — so these tests also exercise
migration 0088 end-to-end in CI.
"""

from __future__ import annotations

import sqlite3
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest
from flask.testing import FlaskClient

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "execution"))

import comments_server  # noqa: E402

_PRIOR_HEAD = "0059_kpi_facts_restatement"
_NOW = datetime.now(UTC).replace(tzinfo=None).isoformat()


@pytest.fixture
def db_path(tmp_path: Path, migrated_db: Callable[..., Path]) -> Path:
    return migrated_db(tmp_path / "data" / "portfolio.db", stamp=_PRIOR_HEAD)


@pytest.fixture
def client(db_path: Path, tmp_path: Path) -> FlaskClient:
    assert db_path.exists()
    return comments_server.create_app(tmp_path).test_client()


def _seed_closed_entry(db_path: Path, *, ticker: str = "NU") -> int:
    conn = sqlite3.connect(str(db_path))
    cur = conn.execute(
        "INSERT INTO position_entries (user_id, ticker, entry_date, entry_price, "
        "exit_date, exit_price, source, created_at, updated_at) "
        "VALUES ('bhanu', ?, '2026-01-10', 11.8, '2026-06-10', 12.8, 'reconciler', ?, ?)",
        (ticker, _NOW, _NOW),
    )
    conn.commit()
    entry_id = int(cur.lastrowid or 0)
    conn.close()
    return entry_id


def test_lifecycle_fragment_serves_timeline(client: FlaskClient, db_path: Path) -> None:
    entry_id = _seed_closed_entry(db_path)
    resp = client.get("/api/position-lifecycle/nu")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert 'data-plc-ticker="NU"' in html
    assert "2026-01-10" in html and "2026-06-10" in html
    assert f'data-plc-grade="{entry_id}"' in html  # ungraded close → form


def test_lifecycle_fragment_empty_state(client: FlaskClient) -> None:
    resp = client.get("/api/position-lifecycle/MELI")
    assert resp.status_code == 200
    assert "No lifecycle rows yet" in resp.get_data(as_text=True)


def test_grade_post_roundtrip(client: FlaskClient, db_path: Path) -> None:
    entry_id = _seed_closed_entry(db_path)
    resp = client.post(
        f"/api/position-entries/{entry_id}",
        json={
            "exit_reason": "funding costs broke the spread story",
            "lessons": "watch deposit beta",
            "outcome_vs_thesis": "broke",
        },
    )
    assert resp.status_code == 200
    assert resp.get_json() == {"id": entry_id, "ok": True}

    conn = sqlite3.connect(str(db_path))
    row = conn.execute(
        "SELECT exit_reason, lessons, outcome_vs_thesis FROM position_entries WHERE id = ?",
        (entry_id,),
    ).fetchone()
    conn.close()
    assert row == ("funding costs broke the spread story", "watch deposit beta", "broke")

    # The refreshed fragment now shows the grading instead of the form.
    html = client.get("/api/position-lifecycle/NU").get_data(as_text=True)
    assert f'data-plc-grade="{entry_id}"' not in html
    assert "thesis broke" in html


def test_grade_post_validation_and_missing_row(client: FlaskClient, db_path: Path) -> None:
    entry_id = _seed_closed_entry(db_path)
    bad = client.post(f"/api/position-entries/{entry_id}", json={"outcome_vs_thesis": "vibes"})
    assert bad.status_code == 400
    missing = client.post("/api/position-entries/999999", json={"lessons": "x"})
    assert missing.status_code == 404
