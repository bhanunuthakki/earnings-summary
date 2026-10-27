"""Anchor database reads follow the active DB authority, never repo_root."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import timeseries
from db_paths import db_path_context
from llm import anchors
from sqlite_runtime import SQLiteConnectionRole


def _create_kpi_database(path: Path, name: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE kpi_definitions "
            "(id INTEGER PRIMARY KEY, ticker TEXT NOT NULL, name TEXT NOT NULL)"
        )
        conn.execute("INSERT INTO kpi_definitions (ticker, name) VALUES ('ABC', ?)", (name,))


def test_statistical_patterns_use_scoped_database_not_checkout_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = tmp_path / "alternate-repo"
    fake_checkout_db = repo_root / "data" / "portfolio.db"
    configured_db = tmp_path / "configured" / "portfolio.db"
    _create_kpi_database(fake_checkout_db, "checkout-only KPI")
    _create_kpi_database(configured_db, "configured KPI")

    observed_connect_paths: list[Path] = []
    connect_sqlite = anchors.connect_sqlite

    def capture_connect(path: Path, *, role: SQLiteConnectionRole) -> sqlite3.Connection:
        observed_connect_paths.append(Path(path))
        return connect_sqlite(path, role=role)

    kpi_loader_paths: list[Path | None] = []

    def empty_financial_series(*, db_path: Path | None = None, **_kwargs: object) -> list[object]:
        assert db_path is not None
        kpi_loader_paths.append(db_path)
        return []

    def empty_kpi_series(
        *, kpi_name: str, db_path: Path | None = None, **_kwargs: object
    ) -> list[object]:
        assert db_path is not None
        assert kpi_name == "configured KPI"
        kpi_loader_paths.append(db_path)
        return []

    monkeypatch.setattr(anchors, "connect_sqlite", capture_connect)
    monkeypatch.setattr(timeseries, "load_financial_series", empty_financial_series)
    monkeypatch.setattr(timeseries, "load_kpi_series", empty_kpi_series)

    with db_path_context(configured_db):
        assert anchors.statistical_patterns_block(repo_root, "ABC", {}) == []

    assert observed_connect_paths == [configured_db]
    assert kpi_loader_paths
    assert all(path == configured_db for path in kpi_loader_paths)


def test_missing_scoped_database_never_opens_checkout_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = tmp_path / "alternate-repo"
    fake_checkout_db = repo_root / "data" / "portfolio.db"
    missing_configured_db = tmp_path / "unavailable" / "portfolio.db"
    _create_kpi_database(fake_checkout_db, "checkout-only KPI")

    def forbidden_connect(*_args: object, **_kwargs: object) -> sqlite3.Connection:
        pytest.fail("anchor attempted to open a database after configured state was unavailable")

    monkeypatch.setattr(anchors, "connect_sqlite", forbidden_connect)

    with db_path_context(missing_configured_db):
        assert anchors.statistical_patterns_block(repo_root, "ABC", {}) == []
        assert anchors.load_owner_profile_anchor(repo_root) == ""
