"""Integration test for GET /api/panel/red_team (execution/comments_server.py)
— the Portfolio -> Red Team panel route (PR5).
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "execution"))
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import comments_server  # noqa: E402


def _create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE tracked_companies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT DEFAULT 'bhanu',
            ticker TEXT NOT NULL,
            name TEXT NOT NULL,
            list_type TEXT NOT NULL,
            added_at TIMESTAMP,
            sec_validated INTEGER DEFAULT 0,
            ir_url TEXT,
            instrument_type TEXT,
            filing_regime TEXT,
            fiscal_year_end TEXT,
            fmp_data_saved INTEGER DEFAULT 0,
            fmp_data_upto TEXT,
            archived_at TIMESTAMP,
            UNIQUE(user_id, ticker)
        );
        CREATE TABLE red_team_items (
            id INTEGER PRIMARY KEY,
            run_key TEXT NOT NULL,
            ticker TEXT,
            lens TEXT NOT NULL,
            kind TEXT NOT NULL,
            attack_md TEXT NOT NULL,
            question_md TEXT NOT NULL,
            proposed_change_md TEXT NOT NULL,
            severity TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'open',
            defer_count INTEGER NOT NULL DEFAULT 0,
            response_md TEXT,
            responded_at TEXT,
            created_at TEXT NOT NULL
        );
        """
    )
    conn.commit()


@pytest.fixture
def app_repo(tmp_path: Path) -> Path:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    db_path = data_dir / "portfolio.db"
    conn = sqlite3.connect(str(db_path))
    _create_schema(conn)
    conn.execute(
        "INSERT INTO red_team_items "
        "(run_key, ticker, lens, kind, attack_md, question_md, proposed_change_md, "
        " severity, status, defer_count, created_at) "
        "VALUES ('red_team_2026_08', 'NU', 'fx_translation', 'per_name', "
        " 'Attack text.', 'Question text?', 'Proposed change.', 'high', 'open', 0, "
        " '2026-08-01T10:00:00')"
    )
    conn.commit()
    conn.close()
    return tmp_path


@pytest.fixture
def client(app_repo: Path):
    app = comments_server.create_app(app_repo)
    return app.test_client()


@pytest.fixture
def empty_client(tmp_path: Path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    db_path = data_dir / "portfolio.db"
    conn = sqlite3.connect(str(db_path))
    _create_schema(conn)
    conn.commit()
    conn.close()
    app = comments_server.create_app(tmp_path)
    return app.test_client()


def test_red_team_panel_route_renders_seeded_item(client) -> None:
    resp = client.get("/api/panel/red_team")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "NU" in body
    assert "Attack text." in body
    assert "red_team_2026_08" in body


def test_red_team_panel_route_renders_empty_state(empty_client) -> None:
    resp = empty_client.get("/api/panel/red_team")
    assert resp.status_code == 200
    body = empty_client.get("/api/panel/red_team").get_data(as_text=True)
    assert "No red-team items yet" in body


def test_red_team_aliases_into_ask(client) -> None:
    """Health redesign (2026-07-30): Red Team is an on-demand Ask question,
    not a standing console section — the old #red_team deep-link lands on the
    Ask panel. The builder's own /api/panel/red_team route stays live for
    direct fetch (see the route tests above)."""
    from pipeline.command_center_shell import (
        _LEGACY_PANEL_REDIRECTS,  # pyright: ignore[reportPrivateUsage]
    )
    from pipeline.work_os_shell import _LEGACY_HASHES  # pyright: ignore[reportPrivateUsage]

    resp = client.get("/")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    # Both shells keep the old id as an alias to the on-demand Ask surface.
    assert _LEGACY_PANEL_REDIRECTS["red_team"] == "explore"
    assert _LEGACY_HASHES["red_team"] == "screen-analytics-playground"
    assert '"red_team": "screen-analytics-playground"' in body
