"""HTTP boundary for local owner-governed IR decisions."""

from __future__ import annotations

import sqlite3
import sys
from collections.abc import Callable
from pathlib import Path

from flask.testing import FlaskClient

from pipeline.source_policy import issuer_policy

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "execution"))

import comments_server  # noqa: E402


def _client_with_candidate(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> tuple[FlaskClient, Path, str, str]:
    db = migrated_db(tmp_path / "route.db")
    candidate_id = "1" * 64
    observation_hash = "a" * 64
    policy = issuer_policy("RBRK")
    candidate_url = "https://ir.rubrik.com/static-files/example.pdf"
    with sqlite3.connect(db) as connection:
        connection.execute(
            """
            INSERT INTO ir_approval_candidates (
                candidate_id,request_id,request_sha256,issuer_id,ticker,catalog_sha256,
                issuer_policy_sha256,authority_url,quarter_end,title,candidate_url,disposition,
                doc_type,observation_key,observation_raw_sha256,evidence_locator,recorded_by,
                recorded_at,reason,evidence_json,evidence_sha256
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                candidate_id,
                "route-candidate-1",
                "c" * 64,
                policy.issuer_id,
                "RBRK",
                "d" * 64,
                policy.policy_sha256,
                policy.ir.authority_url,
                "2026-04-30",
                "Q2 earnings release",
                candidate_url,
                "ir_document",
                "ir_press_release",
                "route-observation-1",
                observation_hash,
                "fixture://ir-route",
                "test:fixture",
                "2026-08-12T10:00:00",
                "Owner review required",
                '[{"content_sha256":"ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff","evidence_id":"review-1","locator":"fixture://review"}]',
                "e" * 64,
            ),
        )
    app = comments_server.create_app(tmp_path, db_path=db)
    return app.test_client(), db, candidate_id, observation_hash


def test_route_records_approve_and_returns_refreshed_card(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    client, db, candidate_id, observation_hash = _client_with_candidate(tmp_path, migrated_db)

    response = client.post(
        f"/api/ir-approval/candidates/{candidate_id}/approve",
        json={"reason": "Visible period and issuer evidence confirmed"},
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["ok"] is True
    assert payload["outcome"] == "appended"
    assert payload["revision"] == 1
    assert "Approved RBRK candidate" in payload["receipt"]
    assert 'data-ir-approval-candidate="' + candidate_id + '"' in payload["panel_html"]
    assert "Approved / exact selection pending" in payload["panel_html"]
    with sqlite3.connect(db) as connection:
        row = connection.execute(
            "SELECT owner_actor,reason,evidence_json,selected_content_sha256 "
            "FROM ir_approval_decisions"
        ).fetchone()
    assert row[0] == "bhanu"
    assert row[1] == "Visible period and issuer evidence confirmed"
    assert "review-1" in row[2]
    assert row[3] is None
    assert observation_hash in payload["panel_html"]  # catalog observation stays visibly distinct
    assert "Selected content hash" in payload["panel_html"]
    assert "Not selected" in payload["panel_html"]


def test_route_replay_does_not_append_a_second_revision(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    client, db, candidate_id, _ = _client_with_candidate(tmp_path, migrated_db)
    endpoint = f"/api/ir-approval/candidates/{candidate_id}/reject"
    body = {"reason": "Ambiguous hidden panel"}

    assert client.post(endpoint, json=body).get_json()["outcome"] == "appended"
    replay = client.post(endpoint, json=body)

    assert replay.status_code == 200
    assert replay.get_json()["outcome"] == "exact_replay"
    with sqlite3.connect(db) as connection:
        assert connection.execute("SELECT COUNT(*) FROM ir_approval_decisions").fetchone()[0] == 1


def test_route_requires_reason_and_refuses_browser_owned_governance_fields(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    client, db, candidate_id, _ = _client_with_candidate(tmp_path, migrated_db)
    endpoint = f"/api/ir-approval/candidates/{candidate_id}/approve"

    missing = client.post(endpoint, json={})
    forged = client.post(
        endpoint,
        json={
            "reason": "forge",
            "owner_actor": "attacker",
            "expected_revision": 99,
            "selected_content_sha256": "b" * 64,
        },
    )

    assert missing.status_code == 400
    assert missing.get_json()["code"] == "invalid_request"
    assert forged.status_code == 400
    assert forged.get_json()["code"] == "server_owned_fields"
    with sqlite3.connect(db) as connection:
        assert connection.execute("SELECT COUNT(*) FROM ir_approval_decisions").fetchone()[0] == 0


def test_direct_select_fails_closed_without_captured_document_bytes(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    client, db, candidate_id, observation_hash = _client_with_candidate(tmp_path, migrated_db)

    response = client.post(
        f"/api/ir-approval/candidates/{candidate_id}/select_exact",
        json={"reason": "Select the exact document"},
    )

    assert response.status_code == 409
    assert response.get_json()["code"] == "selection_bytes_unavailable"
    assert "server-owned hash" in response.get_json()["error"]
    assert observation_hash not in response.get_data(as_text=True)
    with sqlite3.connect(db) as connection:
        assert connection.execute("SELECT COUNT(*) FROM ir_approval_decisions").fetchone()[0] == 0


def test_cross_site_browser_is_rejected_by_global_csrf_guard(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    client, db, candidate_id, _ = _client_with_candidate(tmp_path, migrated_db)

    response = client.post(
        f"/api/ir-approval/candidates/{candidate_id}/approve",
        json={"reason": "Cross-site attempt"},
        headers={"Origin": "https://evil.example", "Sec-Fetch-Site": "cross-site"},
    )

    assert response.status_code == 403
    with sqlite3.connect(db) as connection:
        assert connection.execute("SELECT COUNT(*) FROM ir_approval_decisions").fetchone()[0] == 0
