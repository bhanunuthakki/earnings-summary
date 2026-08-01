"""Boot warm-up: the daemon thread that renders Today once at startup so the
owner's first visit after an ``es-dashboard`` restart is warm rather than
paying the 35-40s cold build (cold imports + cold OS/SQLite page caches).

The contract under test is narrow and entirely about isolation: the warm-up
must actually populate the panel cache, must survive a panel that raises, and
must never propagate a failure into serving.
"""

from __future__ import annotations

import sqlite3
import sys
import threading
from pathlib import Path

import pytest
from flask import Flask

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "execution"))
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import comments_server  # noqa: E402


@pytest.fixture
def app(tmp_path: Path) -> Flask:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    sqlite3.connect(str(data_dir / "portfolio.db")).close()
    return comments_server.create_app(tmp_path)


def _finish(thread: threading.Thread) -> None:
    thread.join(timeout=120)
    assert not thread.is_alive(), "warm-up thread did not finish"


def test_warmup_primes_the_panel_cache(app: Flask) -> None:
    # /api/panel/actions renders with no DB dependency — deterministic, and it
    # goes through the same before/after_request cache hooks as Overview.
    assert app.test_client().get("/api/panel/actions").headers.get("X-Panel-Cache") is None
    _finish(comments_server.start_boot_warmup(app, paths=("/api/panel/actions",)))
    warmed = app.test_client().get("/api/panel/actions")
    assert warmed.status_code == 200
    assert warmed.headers.get("X-Panel-Cache") == "hit"


def test_warmup_covers_overview_and_cockpit_by_default() -> None:
    # The two builds the owner's first visit actually blocks on.
    assert comments_server.WARMUP_PATHS == ("/api/panel/overview", "/api/cockpit")


def test_warmup_survives_a_failing_path(app: Flask) -> None:
    # /api/panel/<unknown> 404s; that must not abort the paths after it.
    _finish(
        comments_server.start_boot_warmup(
            app, paths=("/api/panel/no_such_panel", "/api/panel/actions")
        )
    )
    assert app.test_client().get("/api/panel/actions").headers.get("X-Panel-Cache") == "hit"


def test_warmup_swallows_a_transport_failure(app: Flask, monkeypatch: pytest.MonkeyPatch) -> None:
    # If even building the client fails, the thread logs and exits — no
    # unhandled exception, and the app still serves.
    def _boom() -> object:
        raise RuntimeError("client construction failed")

    monkeypatch.setattr(app, "test_client", _boom)
    _finish(comments_server.start_boot_warmup(app, paths=("/api/panel/actions",)))
    monkeypatch.undo()
    assert app.test_client().get("/api/panel/actions").status_code == 200


def test_warmup_thread_is_a_daemon(app: Flask) -> None:
    thread = comments_server.start_boot_warmup(app, paths=())
    assert thread.daemon, "a non-daemon warm-up would block interpreter shutdown"
    _finish(thread)
