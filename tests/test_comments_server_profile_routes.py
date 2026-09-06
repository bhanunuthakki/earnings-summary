"""Parity tests for the extracted owner-profile and tenet routes."""

from __future__ import annotations

import sqlite3
import sys
from collections.abc import Callable
from pathlib import Path

import pytest
from flask import Flask
from flask.testing import FlaskClient

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "execution"))
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import comments_server  # noqa: E402
import comments_server_profile_routes  # noqa: E402
from comments_server_profile_routes import (  # noqa: E402
    ProfileRouteContext,
    register_profile_routes,
)

from owner_profile.store import append_fact  # noqa: E402
from synthesis.tenets import record_tenet, supersede_tenet  # noqa: E402

_PRIOR_HEAD = "0059_kpi_facts_restatement"


def _noop_activation_count(panel_id: str) -> None:
    return None


def _internal_failure(
    message: str, exc: object, *, status: int = 500
) -> tuple[dict[str, str], int]:
    return ({"error": message}, status)


@pytest.fixture
def db_file(tmp_path: Path, migrated_db: Callable[..., Path]) -> Path:
    return migrated_db(tmp_path / "data" / "portfolio.db", stamp=_PRIOR_HEAD)


@pytest.fixture
def client(tmp_path: Path, db_file: Path) -> FlaskClient:
    return comments_server.create_app(tmp_path).test_client()


def _seed_fact(db: Path, *, key: str = "home_city") -> int:
    conn = sqlite3.connect(str(db))
    try:
        fid = append_fact(
            conn,
            category="capacity",
            key=key,
            value={"city": "San Francisco"},
            narrative="Home city: San Francisco.",
            provenance="wealthplan_import",
        )
        conn.commit()
    finally:
        conn.close()
    return fid


def _seed_expired_fact(db: Path, *, key: str = "dry_powder_policy") -> int:
    conn = sqlite3.connect(str(db))
    try:
        fid = append_fact(
            conn,
            category="appetite",
            key=key,
            value={"months": 3.0},
            narrative="Dry-powder policy: keep 3 months uninvested.",
            provenance="owner",
            status="affirmed",
            review_horizon_days=90,
        )
        conn.execute(
            "UPDATE owner_profile_facts SET affirmed_at = ? WHERE id = ?",
            ("2020-01-01T00:00:00", fid),
        )
        conn.commit()
    finally:
        conn.close()
    return fid


def _activation_count(db: Path, panel_id: str) -> int:
    conn = sqlite3.connect(str(db))
    try:
        row = conn.execute(
            "SELECT SUM(count) FROM panel_activation_counts WHERE panel_id = ?",
            (panel_id,),
        ).fetchone()
    finally:
        conn.close()
    return int(row[0] or 0) if row is not None else 0


class _WriterProbe:
    def __init__(self) -> None:
        self.commits = 0
        self.rollbacks = 0
        self.closes = 0

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        self.closes += 1


def _raise_inside_profile_writer(path: Path) -> None:
    with comments_server_profile_routes.profile_writer(path):
        raise RuntimeError("boom")


def test_profile_writer_helper_preserves_transaction_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    probe = _WriterProbe()

    def _connect_writer_probe(*_args: object, **_kwargs: object) -> _WriterProbe:
        return probe

    monkeypatch.setattr(comments_server_profile_routes, "connect_sqlite", _connect_writer_probe)

    with comments_server_profile_routes.profile_writer(tmp_path / "portfolio.db"):
        pass

    assert probe.commits == 1
    assert probe.rollbacks == 0
    assert probe.closes == 1

    probe = _WriterProbe()

    def _connect_writer_probe_again(*_args: object, **_kwargs: object) -> _WriterProbe:
        return probe

    monkeypatch.setattr(
        comments_server_profile_routes,
        "connect_sqlite",
        _connect_writer_probe_again,
    )

    with pytest.raises(RuntimeError):
        _raise_inside_profile_writer(tmp_path / "portfolio.db")

    assert probe.commits == 0
    assert probe.rollbacks == 1
    assert probe.closes == 1


def test_profile_routes_register_expected_endpoints(tmp_path: Path) -> None:
    app = Flask(__name__)
    register_profile_routes(
        app,
        ProfileRouteContext(
            db_path=tmp_path / "portfolio.db",
            default_user_id="owner",
            bump_activation_count=_noop_activation_count,
            internal_failure=_internal_failure,
        ),
    )
    routes = {rule.rule for rule in app.url_map.iter_rules()}
    assert {
        "/api/tenets",
        "/api/tenets/<int:tenet_id>/<action>",
        "/api/tenets/distill",
        "/api/profile/fact/<int:fact_id>/affirm",
        "/api/profile/fact/<int:fact_id>/reject",
        "/api/profile/fact/<int:fact_id>/reaffirm",
        "/api/profile/fact/<int:fact_id>/retire",
        "/api/profile/fact/<int:fact_id>/update",
    }.issubset(routes)


def test_tenets_routes_keep_http_contract(client: FlaskClient, db_file: Path) -> None:
    assert client.open("/api/tenets", method="OPTIONS").status_code == 204

    created = client.post(
        "/api/tenets",
        json={"body_md": "I sell my winners too early.", "scope_key": "exit-discipline"},
    )
    assert created.status_code == 200
    created_body = created.get_json()
    assert created_body == {
        "ok": True,
        "id": created_body["id"],
        "scope_key": "tenet:exit-discipline",
    }

    prop = record_tenet(
        body_md="Trim winners on a double.",
        scope_key="exit-discipline",
        status="proposed",
        source_note_ids=[1],
        db_path=db_file,
    )
    approved = client.post(f"/api/tenets/{prop.id}/approve")
    assert approved.status_code == 200
    assert approved.get_json() == {
        "ok": True,
        "status": "current",
        "receipt": "Adopted — now a standing Tenet in your decision prompts",
    }

    rejected_tenet = record_tenet(
        body_md="Chase momentum.",
        status="proposed",
        source_note_ids=[1],
        db_path=db_file,
    )
    rejected = client.post(f"/api/tenets/{rejected_tenet.id}/reject")
    assert rejected.status_code == 200
    assert rejected.get_json() == {
        "ok": True,
        "receipt": "Retired — this Tenet was not adopted",
    }

    old = record_tenet(body_md="Let winners run.", scope_key="exit-discipline", db_path=db_file)
    new = supersede_tenet(old.id, body_md="Trim on a double.", db_path=db_file)
    assert new is not None
    reverted = client.post(f"/api/tenets/{new.id}/revert")
    assert reverted.status_code == 200
    assert reverted.get_json() == {
        "ok": True,
        "status": "current",
        "receipt": "Reverted — restores your prior belief",
    }

    distill = client.post("/api/tenets/distill")
    assert distill.status_code == 200
    assert distill.get_json()["ok"] is True


def test_profile_fact_routes_keep_http_contract(client: FlaskClient, db_file: Path) -> None:
    assert client.open("/api/profile/fact/1/affirm", method="OPTIONS").status_code == 204

    fid = _seed_fact(db_file)
    affirmed = client.post(f"/api/profile/fact/{fid}/affirm")
    assert affirmed.status_code == 200
    assert affirmed.get_json() == {
        "ok": True,
        "status": "affirmed",
        "receipt": "Affirmed — the coach may now cite this when reviewing your trades",
    }
    assert _activation_count(db_file, "act:profile:affirm") == 1

    rejected_id = _seed_fact(db_file, key="emergency_fund")
    rejected = client.post(f"/api/profile/fact/{rejected_id}/reject")
    assert rejected.status_code == 200
    assert rejected.get_json() == {
        "ok": True,
        "receipt": "Dropped — never used, won't be re-proposed",
    }
    assert _activation_count(db_file, "act:profile:reject") == 1

    expiring_id = _seed_expired_fact(db_file, key="dry_powder_policy")
    reaffirmed = client.post(f"/api/profile/fact/{expiring_id}/reaffirm")
    assert reaffirmed.status_code == 200
    assert reaffirmed.get_json() == {
        "ok": True,
        "status": "affirmed",
        "receipt": "Confirmed — good for another review cycle",
    }
    assert _activation_count(db_file, "act:profile:reaffirm") == 1

    retired_id = _seed_expired_fact(db_file, key="position_sizing")
    retired = client.post(f"/api/profile/fact/{retired_id}/retire")
    assert retired.status_code == 200
    assert retired.get_json() == {
        "ok": True,
        "receipt": "Dropped — the coach will stop citing this fact",
    }
    assert _activation_count(db_file, "act:profile:retire") == 1

    editable_id = _seed_expired_fact(db_file, key="cash_buffer")
    updated = client.post(
        f"/api/profile/fact/{editable_id}/update",
        json={"narrative": "Dry-powder policy: keep 4 months uninvested."},
    )
    assert updated.status_code == 200
    updated_body = updated.get_json()
    assert updated_body == {
        "ok": True,
        "new_fact_id": updated_body["new_fact_id"],
        "receipt": "Saved — your edit awaits your affirm next walk",
    }
    assert _activation_count(db_file, "act:profile:update") == 1
