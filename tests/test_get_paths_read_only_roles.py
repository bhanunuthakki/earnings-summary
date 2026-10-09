"""VAL-SERVE-030: the named M2 GET paths acquire only READ_ONLY connections.

The cockpit's read paths must stay read-only for the whole validation surface:
the app-level hydration routes read through the request-scoped read connection
(``get_read_db``/``get_operations_read_db``), and the holding renderer's own
DB readers (the analytical dashboard rereads, position lifecycle, attribution,
the you-said strip and the position-lifecycle store) open their connections
through ``connect_sqlite`` with the ``READ_ONLY`` role. A GET path that
acquires a writable connection fails this contract -- the tracker client is
pinned to the dead loopback so no request can leave localhost.
"""

from __future__ import annotations

import os
import sqlite3
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from flask import Flask

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "execution"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import comments_server

import attribution
import position_lifecycle
from pipeline import ticker_command_center, you_said
from sqlite_runtime import SQLiteConnectionRole
from sqlite_runtime import connect_sqlite as real_connect_sqlite
from tests.comments_server_seams import patch_server_seam

DEAD_TRACKER_URL = "http://127.0.0.1:9"

# The holding fragment's DB readers, each importing its own connect_sqlite seam.
HOLDING_SEAM_MODULES: tuple[ModuleType, ...] = (
    ticker_command_center,
    attribution,
    you_said,
    position_lifecycle,
)

# The app-level named M2 GET paths (VAL-SERVE-001 request set minus the holding
# fragment, which is covered by the dedicated seam test, and healthz).
APP_LEVEL_GET_PATHS: tuple[str, ...] = (
    "/",
    "/api/panel/overview",
    "/api/work-os/portfolio",
    "/api/work-os/evaluation",
    "/api/work-os/evaluation-dialogues?limit=3&sort=relevance&filter=all",
    "/api/work-os/evaluation-dialogues?limit=10&sort=ticker_asc&filter=has_dialogue",
    "/api/panel/operations",
    "/api/dashboard",
    "/api/work-os/briefs",
    "/api/work-os/companies/NU/desk",
)


def _recording_connect(
    module: ModuleType, roles: list[tuple[str, SQLiteConnectionRole]]
) -> Callable[..., sqlite3.Connection]:
    """Wrap one module's connect_sqlite seam, recording every acquired role."""
    module_name = module.__name__

    def connect(
        path: str | os.PathLike[str],
        *,
        role: SQLiteConnectionRole,
        schema_preflight: bool | None = None,
    ) -> sqlite3.Connection:
        roles.append((module_name, role))
        return real_connect_sqlite(path, role=role, schema_preflight=schema_preflight)

    return connect


@pytest.fixture
def app(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> Flask:
    monkeypatch.setenv("PORTFOLIO_TRACKER_API_URL", DEAD_TRACKER_URL)
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    db_path = migrated_db(data_dir / "portfolio.db")
    # The desk route answers only for tracked tickers; seed one portfolio row
    # on this test's hermetic copy so the request set matches the live roster.
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO tracked_companies (user_id, ticker, name, list_type) "
            "VALUES ('bhanu', 'NU', 'Nu Holdings', 'portfolio')"
        )
    return comments_server.create_app(tmp_path)


def test_app_level_get_paths_open_only_read_only_connections(
    app: Flask, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every DB connection the named hydration/desk GETs acquire is READ_ONLY."""
    roles: list[tuple[str, SQLiteConnectionRole]] = []
    patch_server_seam(monkeypatch, "connect_sqlite", _recording_connect(comments_server, roles))

    with app.test_client() as client:
        for path in APP_LEVEL_GET_PATHS:
            response = client.get(path)
            assert response.status_code == 200, (path, response.status_code)

    assert roles, "the request paths must actually have touched the database"
    acquired_roles = {role for _module, role in roles}
    assert acquired_roles == {SQLiteConnectionRole.READ_ONLY}


def test_holding_get_path_opens_only_read_only_connections(
    app: Flask, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The holding fragment's own DB readers all open with the READ_ONLY role."""
    roles: list[tuple[str, SQLiteConnectionRole]] = []
    for module in HOLDING_SEAM_MODULES:
        monkeypatch.setattr(module, "connect_sqlite", _recording_connect(module, roles))

    with app.test_client() as client:
        response = client.get("/api/panel/holding?ticker=NU")
        assert response.status_code == 200

    assert roles, "the holding renderers must actually have opened connections"
    acquired_roles = {role for _module, role in roles}
    assert acquired_roles == {SQLiteConnectionRole.READ_ONLY}
