"""Request-scoped SQLite connection contracts for live report reads."""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "execution"))

import comments_server  # noqa: E402

from report import builder  # noqa: E402
from report.models import ReportFlavor  # noqa: E402
from sqlite_runtime import SQLiteConnectionRole  # noqa: E402


class _TrackingConnection(sqlite3.Connection):
    pass


def test_dashboard_read_connection_is_read_only_and_closed_at_teardown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    db_path = data_dir / "portfolio.db"
    sqlite3.connect(db_path).close()

    opened: list[_TrackingConnection] = []
    roles: list[SQLiteConnectionRole] = []

    def connect(
        path: Path,
        *,
        role: SQLiteConnectionRole,
        schema_preflight: bool = False,
    ) -> sqlite3.Connection:
        assert Path(path) == db_path
        assert schema_preflight is True
        roles.append(role)
        conn = sqlite3.connect(db_path, factory=_TrackingConnection)
        opened.append(conn)
        return conn

    seen: list[sqlite3.Connection] = []

    def build_rows(conn: sqlite3.Connection, _repo_root: Path) -> dict[str, list[object]]:
        conn.execute("SELECT 1").fetchone()
        seen.append(conn)
        return {}

    monkeypatch.setattr(comments_server, "connect_sqlite", connect)
    monkeypatch.setattr(comments_server, "build_dashboard_rows", build_rows)

    response = comments_server.create_app(tmp_path).test_client().get("/api/dashboard")

    assert response.status_code == 200
    assert roles == [SQLiteConnectionRole.READ_ONLY]
    assert seen == opened
    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        opened[0].execute("SELECT 1")


def test_overview_composition_uses_one_request_reader(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    db_path = data_dir / "portfolio.db"
    sqlite3.connect(db_path).close()

    opened: list[_TrackingConnection] = []
    roles: list[SQLiteConnectionRole] = []

    def connect(
        path: Path,
        *,
        role: SQLiteConnectionRole,
        schema_preflight: bool = False,
    ) -> sqlite3.Connection:
        assert Path(path) == db_path
        assert schema_preflight is True
        roles.append(role)
        conn = sqlite3.connect(db_path, factory=_TrackingConnection)
        opened.append(conn)
        return conn

    def empty_cockpit(*_args: object) -> dict[str, list[object]]:
        return {}

    def overview_stub(*_args: object, **_kwargs: object) -> str:
        return "ok"

    monkeypatch.setattr(comments_server, "connect_sqlite", connect)
    monkeypatch.setattr(comments_server, "build_cockpit_rows", empty_cockpit)
    monkeypatch.setattr(comments_server, "render_overview_panel", overview_stub)

    response = comments_server.create_app(tmp_path).test_client().get("/api/panel/overview")

    assert response.status_code == 200
    assert response.get_data(as_text=True) == "ok"
    assert roles == [SQLiteConnectionRole.READ_ONLY]
    assert len(opened) == 1
    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        opened[0].execute("SELECT 1")


def test_panel_activation_metrics_never_create_schema_from_request_path() -> None:
    source = (PROJECT_ROOT / "execution" / "comments_server.py").read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS panel_activation_counts" not in source


def test_build_report_threads_one_borrowed_connection_to_db_sections(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shared = sqlite3.connect(":memory:")
    received: dict[str, sqlite3.Connection | None] = {}
    stub = SimpleNamespace(held=False, budget_skip=None)

    db_sections = (
        builder.snapshot,
        builder.evaluation_snapshot,
        builder.company_description,
        builder.thesis,
        builder.financials,
        builder.signals,
        builder.segments,
        builder.earnings,
        builder.saydo,
        builder.ir_docs,
        builder.bear_case,
        builder.provenance,
        builder.valuation,
        builder.exec_compensation,
        builder.synthesis,
        builder.investment_decision_card,
    )
    for module in db_sections:
        name = module.__name__

        def record(*_args: object, _name: str = name, **kwargs: object) -> object:
            value = kwargs.get("conn")
            assert isinstance(value, sqlite3.Connection)
            received[_name] = value
            return stub

        monkeypatch.setattr(module, "build", record)

    for module in (
        builder.portfolio_position,
        builder.recent_developments,
        builder.appendix,
        builder.qa_roster,
        builder.filing_intelligence,
    ):

        def return_stub(*_args: object, **_kwargs: object) -> object:
            return stub

        monkeypatch.setattr(module, "build", return_stub)

    def no_suppressed_sections(*_args: object) -> set[str]:
        return set()

    def report_spec(**kwargs: object) -> dict[str, object]:
        return kwargs

    monkeypatch.setattr(builder, "suppressed_sections_for_ticker", no_suppressed_sections)
    monkeypatch.setattr(builder, "ReportSpec", report_spec)

    result = builder.build_report(
        "NU",
        tmp_path,
        flavor=ReportFlavor.EVALUATION,
        conn=shared,
    )

    payload = cast("dict[str, object]", result)
    assert payload["ticker"] == "NU"
    assert received == {module.__name__: shared for module in db_sections}
    assert shared.execute("SELECT 1").fetchone() == (1,)
    shared.close()
