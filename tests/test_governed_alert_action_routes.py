"""HTTP contract for evidence-bound governed alert lifecycle actions."""

from __future__ import annotations

import sqlite3
import sys
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from flask.testing import FlaskClient

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "execution"))

import comments_server  # noqa: E402

NOW = datetime(2026, 8, 23, 12, tzinfo=UTC)
SIGNATURE = "e" * 64


def _seed_episode(connection: sqlite3.Connection, episode_id: str, *, digit: str = "a") -> None:
    connection.execute(
        "INSERT INTO thesis_evaluation_episodes "
        "(episode_id,ticker,fingerprint_policy_version,semantic_input_json,"
        "semantic_input_sha256,thesis_content_sha256,ruleset_sha256,"
        "evaluator_semantic_version,result_sha256,overall_status,provenance_completeness,"
        "first_evaluated_at,last_seen_at,last_checked_at,duplicate_run_count,"
        "rule_evaluations_json,created_at) "
        "VALUES (?, 'WIX', 'forward_v1', '{}', ?, ?, ?, 'test/v1', ?, 'warn', 'partial', "
        "?, ?, ?, 0, '[]', ?)",
        (episode_id, digit * 64, "b" * 64, "c" * 64, "d" * 64, *(NOW.isoformat(),) * 4),
    )


def _seed_alert(
    connection: sqlite3.Connection,
    *,
    episode_id: str | None = None,
    trigger_kind: str = "material_news",
) -> None:
    connection.execute(
        "INSERT INTO alerts "
        "(id,user_id,ticker,trigger_kind,fired_at,status,evidence_json,signature_sha,"
        "thesis_evaluation_episode_id,review_cycle_id) "
        "VALUES (1,'bhanu','WIX',?,?,'pending','{\"summary\":\"Fixture evidence\"}',?,?,?)",
        (trigger_kind, NOW.isoformat(), SIGNATURE, episode_id, "initial" if episode_id else None),
    )


def _client(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    *,
    thesis_alert: bool = False,
) -> tuple[FlaskClient, Path]:
    database = migrated_db(tmp_path / "governed-action-routes.db", target="head")
    with sqlite3.connect(database) as connection:
        if thesis_alert:
            _seed_episode(connection, "episode-old")
            _seed_alert(connection, episode_id="episode-old", trigger_kind="thesis_drift")
        else:
            _seed_alert(connection)
    return comments_server.create_app(tmp_path, db_path=database).test_client(), database


def _body(*, action_type: str, key: str, **overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "idempotency_key": key,
        "evidence_ref": SIGNATURE,
        "action_type": action_type,
        "occurred_at": NOW.isoformat(),
    }
    values.update(overrides)
    return values


@pytest.mark.parametrize(
    ("action_type", "extra", "thesis_alert", "expected"),
    [
        ("review", {}, False, "reviewed"),
        ("dismiss", {"dismiss_reason": "No longer material."}, False, "dismissed"),
        ("acknowledge", {"note": "Owner reviewed."}, True, "acknowledged"),
        (
            "defer",
            {"note": "Await filing.", "defer_until": (NOW + timedelta(days=7)).isoformat()},
            True,
            "deferred",
        ),
    ],
)
def test_route_executes_allowed_action_classes(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    action_type: str,
    extra: dict[str, object],
    thesis_alert: bool,
    expected: str,
) -> None:
    client, _ = _client(tmp_path, migrated_db, thesis_alert=thesis_alert)

    response = client.post(
        "/api/governed-alerts/1/actions",
        json=_body(action_type=action_type, key=f"route-{action_type}", **extra),
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["ok"] is True
    assert payload["receipt"]["result_state"] == expected
    assert payload["receipt"]["alert_id"] == 1
    assert payload["receipt"]["source_ref"] == "alert:1"
    assert payload["receipt"]["evidence_ref"] == SIGNATURE


def test_route_executes_complete_and_supersede(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    client, database = _client(tmp_path / "complete", migrated_db, thesis_alert=True)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO decisions (ticker,recommendation_kind,made_at,created_at,decided_by) "
            "VALUES ('WIX','hold',?,?, 'owner')",
            (NOW.isoformat(), NOW.isoformat()),
        )
        decision_id = int(connection.execute("SELECT MAX(id) FROM decisions").fetchone()[0])
    complete = client.post(
        "/api/governed-alerts/1/actions",
        json=_body(action_type="complete", key="route-complete", decision_id=decision_id),
    )
    assert complete.status_code == 200
    assert complete.get_json()["receipt"]["result_state"] == "completed"

    client, database = _client(tmp_path / "supersede", migrated_db, thesis_alert=True)
    with sqlite3.connect(database) as connection:
        _seed_episode(connection, "episode-new", digit="f")
    supersede = client.post(
        "/api/governed-alerts/1/actions",
        json=_body(
            action_type="supersede",
            key="route-supersede",
            replacement_episode_id="episode-new",
        ),
    )
    assert supersede.status_code == 200
    assert supersede.get_json()["receipt"]["result_state"] == "superseded"


def test_route_replay_conflict_and_stale_evidence_fail_closed(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    client, database = _client(tmp_path, migrated_db)
    first = _body(action_type="review", key="same-key")
    assert client.post("/api/governed-alerts/1/actions", json=first).status_code == 200
    assert client.post("/api/governed-alerts/1/actions", json=first).status_code == 200
    conflict = client.post(
        "/api/governed-alerts/1/actions",
        json=_body(action_type="dismiss", key="same-key", dismiss_reason="Different request."),
    )
    stale = client.post(
        "/api/governed-alerts/1/actions",
        json=_body(action_type="review", key="stale", evidence_ref="f" * 64),
    )
    assert conflict.status_code == 409
    assert conflict.get_json()["code"] == "action_conflict"
    assert stale.status_code == 409
    with sqlite3.connect(database) as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM governed_alert_action_receipts").fetchone()[0]
            == 1
        )


def test_route_rejects_server_owned_fields_and_cross_site_requests(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    client, database = _client(tmp_path, migrated_db)
    forged = client.post(
        "/api/governed-alerts/1/actions",
        json={**_body(action_type="review", key="forged"), "actor": "attacker"},
    )
    cross_site = client.post(
        "/api/governed-alerts/1/actions",
        json=_body(action_type="review", key="cross-site"),
        headers={"Origin": "https://evil.example", "Sec-Fetch-Site": "cross-site"},
    )
    assert forged.status_code == 400
    assert forged.get_json()["code"] == "server_owned_fields"
    assert cross_site.status_code == 403
    with sqlite3.connect(database) as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM governed_alert_action_receipts").fetchone()[0]
            == 0
        )


def test_routes_fail_closed_for_another_users_alert(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    client, database = _client(tmp_path, migrated_db)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO alerts "
            "(id,user_id,ticker,trigger_kind,fired_at,status,evidence_json,signature_sha) "
            "VALUES (2,'other-owner','WIX','material_news',?,'pending','{}',?)",
            (NOW.isoformat(), SIGNATURE),
        )

    evidence = client.get("/api/governed-alerts/2/evidence")
    action = client.post(
        "/api/governed-alerts/2/actions",
        json=_body(action_type="review", key="cross-owner"),
    )

    assert evidence.status_code == 404
    assert action.status_code == 409
    assert action.get_json()["code"] == "action_conflict"
    with sqlite3.connect(database) as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM governed_alert_action_receipts").fetchone()[0]
            == 0
        )


def test_route_rolls_back_when_receipt_append_fails(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    client, database = _client(tmp_path, migrated_db)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TRIGGER abort_governed_alert_receipt BEFORE INSERT "
            "ON governed_alert_action_receipts BEGIN SELECT RAISE(ABORT, 'no receipt'); END"
        )
    response = client.post(
        "/api/governed-alerts/1/actions",
        json=_body(action_type="dismiss", key="rollback", dismiss_reason="No longer material."),
    )
    assert response.status_code == 409
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT status FROM alerts WHERE id=1").fetchone()[0] == "pending"
        assert (
            connection.execute("SELECT COUNT(*) FROM governed_alert_action_receipts").fetchone()[0]
            == 0
        )


def test_evidence_route_is_read_only_and_has_no_legacy_mutation_links(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    client, database = _client(tmp_path, migrated_db)
    response = client.get("/api/governed-alerts/1/evidence")

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert 'data-source-ref="alert:1"' in html
    assert SIGNATURE in html
    assert "Fixture evidence" in html
    assert 'href="/approve' not in html
    assert 'hx-post="/api/alerts/' not in html
    with sqlite3.connect(database) as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM governed_alert_action_receipts").fetchone()[0]
            == 0
        )
