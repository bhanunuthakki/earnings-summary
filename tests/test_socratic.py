"""Retired exercise entrypoints cannot dispatch; stored exercise history survives."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import comments_server
import pytest

from advisor.store import list_memos
from dispatch_registry import Job, Registry


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/actions/socratic-questions"),
        ("POST", "/api/socratic/questions"),
        ("GET", "/api/socratic/questions/TEST"),
        ("POST", "/api/socratic/memo"),
        ("GET", "/socratic/TEST"),
    ],
)
def test_retired_exercise_routes_are_absent_without_dispatch(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    path: str,
) -> None:
    db = migrated_db(tmp_path / "synthetic.db", target="head")
    registry = Registry(repo_root=tmp_path)
    start = registry.start

    def no_spawn(*, ticker: str, kind: str, argv: list[str], **_kwargs: object) -> Job:
        return start(ticker=ticker, kind=kind, argv=argv, spawn=False)

    monkeypatch.setattr(registry, "start", no_spawn)
    app = comments_server.create_app(tmp_path, db_path=db, registry=registry)
    response = app.test_client().open(path, method=method, json={"ticker": "TEST"})
    assert response.status_code == 404
    assert registry.list_jobs() == []
    assert list_memos(db_path=db) == []
    rules = {rule.rule for rule in app.url_map.iter_rules()}
    assert "/actions/position-review" in rules
    assert "/actions/advisor-memo" not in rules
