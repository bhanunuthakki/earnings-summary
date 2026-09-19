"""Route-level integration tests for /api/work-os/evaluation-dialogues."""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest
from flask.testing import FlaskClient

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "execution"))

import comments_server  # noqa: E402


@pytest.fixture
def app_repo(tmp_path: Path) -> Path:
    db_path = tmp_path / "data" / "portfolio.db"
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
            UNIQUE(user_id, ticker)
        );
        CREATE TABLE discovery_candidates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT DEFAULT 'bhanu',
            ticker TEXT NOT NULL,
            status TEXT NOT NULL
        );
        CREATE TABLE ask_sessions (
            id TEXT PRIMARY KEY,
            scope TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        INSERT INTO tracked_companies (ticker, name, list_type, instrument_type)
        VALUES
            ('AAPL', 'Apple Inc.', 'evaluation', 'equity'),
            ('MSFT', 'Microsoft Corp.', 'evaluation', 'equity'),
            ('QQQ', 'Invesco QQQ', 'evaluation', 'etf');
        """
    )
    conn.commit()
    conn.close()
    return tmp_path


@pytest.fixture
def client(app_repo: Path) -> FlaskClient:
    app = comments_server.create_app(app_repo)
    return app.test_client()


def test_evaluation_dialogues_api_default_parameters(client: FlaskClient) -> None:
    resp = client.get("/api/work-os/evaluation-dialogues")
    assert resp.status_code == 200
    assert resp.mimetype == "application/json"
    data = resp.get_json()
    assert data["state"] in {"available", "partial"}
    assert data["total_active"] == 3
    assert data["total_matching"] == 3
    assert data["matching_state"] == "complete"
    assert isinstance(data["items"], list)
    assert len(data["items"]) == 3
    assert isinstance(data["reason_codes"], list)


@pytest.mark.parametrize("limit", [3, 5, 10])
@pytest.mark.parametrize("sort", ["relevance", "ticker_asc"])
@pytest.mark.parametrize("filter_state", ["all", "has_dialogue", "has_notes", "ready"])
def test_evaluation_dialogues_api_valid_parameter_matrix(
    client: FlaskClient, limit: int, sort: str, filter_state: str
) -> None:
    resp = client.get(
        f"/api/work-os/evaluation-dialogues?limit={limit}&sort={sort}&filter={filter_state}"
    )
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["matching_state"] in {"complete", "indeterminate"}
    assert len(data["items"]) <= limit


@pytest.mark.parametrize("bad_limit", ["2", "4", "99", "0", "-1", "abc", "3.0", ""])
def test_evaluation_dialogues_api_rejects_invalid_limit(
    client: FlaskClient, bad_limit: str
) -> None:
    resp = client.get(f"/api/work-os/evaluation-dialogues?limit={bad_limit}")
    assert resp.status_code == 400
    data = resp.get_json()
    assert "limit must be" in data.get("error", "")


@pytest.mark.parametrize("bad_sort", ["random", "ticker_desc", "newest", ""])
def test_evaluation_dialogues_api_rejects_invalid_sort(client: FlaskClient, bad_sort: str) -> None:
    resp = client.get(f"/api/work-os/evaluation-dialogues?sort={bad_sort}")
    assert resp.status_code == 400
    data = resp.get_json()
    assert "sort must be one of" in data.get("error", "")


@pytest.mark.parametrize("bad_filter", ["unknown", "dialogues", "active", ""])
def test_evaluation_dialogues_api_rejects_invalid_filter(
    client: FlaskClient, bad_filter: str
) -> None:
    resp = client.get(f"/api/work-os/evaluation-dialogues?filter={bad_filter}")
    assert resp.status_code == 400
    data = resp.get_json()
    assert "filter must be one of" in data.get("error", "")


def test_evaluation_dialogues_api_handles_missing_database(tmp_path: Path) -> None:
    app = comments_server.create_app(tmp_path)
    client = app.test_client()

    resp = client.get("/api/work-os/evaluation-dialogues")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["state"] == "unavailable"
    assert data["items"] == []
    assert data["total_active"] is None
    assert data["total_matching"] is None
    assert data["matching_state"] == "indeterminate"
    assert "evaluation_source_unavailable" in data["reason_codes"]


def test_full_evaluation_screen_golden_non_regression() -> None:
    import pipeline.work_os_shell as work_os_shell

    render_fn = getattr(work_os_shell, "_render_evaluation_shell")
    current = str(render_fn())
    golden_path = (
        Path(__file__).resolve().parent / "fixtures" / "evaluation_shell_pre_bha_148.golden.html"
    )
    golden = golden_path.read_text("utf-8")
    assert current == golden


def test_full_evaluation_data_non_regression(app_repo: Path) -> None:
    db_path = app_repo / "data" / "portfolio.db"
    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        "SELECT ticker, name, list_type, instrument_type FROM tracked_companies ORDER BY id"
    ).fetchall()
    conn.close()
    assert rows == [
        ("AAPL", "Apple Inc.", "evaluation", "equity"),
        ("MSFT", "Microsoft Corp.", "evaluation", "equity"),
        ("QQQ", "Invesco QQQ", "evaluation", "etf"),
    ]
