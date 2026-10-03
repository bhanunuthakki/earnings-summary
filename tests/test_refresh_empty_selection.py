"""Empty HTTP selections cannot launch the default refresh chain."""

from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from flask.testing import FlaskClient

pytest_plugins = ("tests.test_comments_server_actions",)


def test_post_refresh_rejects_empty_steps_before_startup(
    client: FlaskClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(code_root: Path) -> str:
        del code_root
        raise AssertionError("empty refresh selection reached process setup")

    server = sys.modules["comments_server"]
    assert isinstance(server, ModuleType)
    monkeypatch.setattr(server, "application_python_executable", refuse)
    response = client.post("/actions/refresh", json={"ticker": "NU", "steps": []})
    assert response.status_code == 400
    assert "at least one" in response.get_json()["error"]
    assert client.get("/actions/jobs").get_json()["jobs"] == []
