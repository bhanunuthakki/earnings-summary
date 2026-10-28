"""The retired profile packet has no owner-facing HTTP routes."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import comments_server
from flask.testing import FlaskClient

_PRIOR_HEAD = "0059_kpi_facts_restatement"


def test_retired_profile_routes_return_not_found(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    migrated_db(tmp_path / "data" / "portfolio.db", stamp=_PRIOR_HEAD)
    client: FlaskClient = comments_server.create_app(tmp_path).test_client()

    for action in ("affirm", "reject", "reaffirm", "retire", "update"):
        response = client.post(f"/api/profile/fact/1/{action}")
        assert response.status_code == 404
