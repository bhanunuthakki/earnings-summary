"""Startup contracts for the localhost comments server."""

from __future__ import annotations

import sys
import threading
from pathlib import Path

import comments_server
import pytest

from tests.comments_server_seams import patch_server_seam


class _StartupApp:
    def __init__(self) -> None:
        self.internal_request = threading.Event()
        self.run_args: tuple[str, int, bool, bool] | None = None

    def test_client(self) -> object:
        self.internal_request.set()
        return object()

    def run(self, *, host: str, port: int, debug: bool, threaded: bool) -> None:
        self.run_args = (host, port, debug, threaded)
        assert not self.internal_request.wait(0.25), "startup issued an internal HTTP request"


def test_main_starts_server_without_internal_requests(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    app = _StartupApp()
    expected_db_path = tmp_path / "data" / "portfolio.db"

    def _configure_runtime_db(_root: Path) -> Path:
        return expected_db_path

    def _create_app(root: Path, *, db_path: Path, server_origin: str) -> _StartupApp:
        assert root == tmp_path.resolve()
        assert db_path == expected_db_path
        assert server_origin == "http://127.0.0.1:7421"
        return app

    monkeypatch.setattr(sys, "argv", ["comments_server.py", "--repo-root", str(tmp_path)])
    patch_server_seam(monkeypatch, "configure_logging", lambda: None)
    patch_server_seam(monkeypatch, "configure_runtime_db", _configure_runtime_db)
    patch_server_seam(monkeypatch, "create_app", _create_app)

    assert comments_server.main() == 0
    assert app.run_args == ("127.0.0.1", 7421, False, True)
