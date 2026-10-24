"""Startup contracts for the localhost comments server."""

from __future__ import annotations

import sys
import threading
from pathlib import Path

import comments_server
import pytest

from dcf.input_evidence import SourceReadContext
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


@pytest.mark.parametrize("root_mode", ("explicit_repo", "repo_and_state", "state_only", "implicit"))
def test_main_starts_server_without_internal_requests(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    root_mode: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    app = _StartupApp()
    expected_db_path = tmp_path / "data" / "portfolio.db"
    sources = tmp_path / "source-state"
    explicit_repo = root_mode in {"explicit_repo", "repo_and_state"}
    explicit_state = root_mode in {"repo_and_state", "state_only"}
    expected_repo = tmp_path.resolve() if explicit_repo else comments_server.PROJECT_ROOT.resolve()
    expected_source = sources if explicit_state else expected_repo if explicit_repo else None

    def _configure_runtime_db(root: Path) -> Path:
        assert root == expected_repo
        return expected_db_path

    def _create_app(
        root: Path,
        *,
        db_path: Path,
        server_origin: str,
        source_state_root: Path | None,
        source_context: SourceReadContext | None,
    ) -> _StartupApp:
        assert root == expected_repo
        assert db_path == expected_db_path
        assert server_origin == "http://127.0.0.1:7421"
        assert source_state_root == expected_source
        assert source_context == (
            SourceReadContext.for_product_state_root(expected_source)
            if expected_source is not None
            else None
        )
        return app

    argv = ["comments_server.py"]
    if explicit_repo:
        argv.extend(("--repo-root", str(tmp_path)))
    if explicit_state:
        argv.extend(("--state-root", str(sources)))
    monkeypatch.setattr(sys, "argv", argv)
    patch_server_seam(monkeypatch, "configure_logging", lambda: None)
    patch_server_seam(monkeypatch, "configure_runtime_db", _configure_runtime_db)
    patch_server_seam(monkeypatch, "create_app", _create_app)

    with caplog.at_level("INFO"):
        assert comments_server.main() == 0
    assert "ask_retrieval_readiness deferred_to_request=true fail_closed=true" in caplog.text
    assert app.run_args == ("127.0.0.1", 7421, False, True)
