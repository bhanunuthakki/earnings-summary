"""Connection ownership, truthful availability, and composite-read measurements."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from pathlib import Path
from statistics import median
from time import perf_counter
from typing import ParamSpec, TypeVar

import pytest

from report.renderers.workspace_data import load_workspace_p3_panels
from report.sections import comp_set_context, p3_data
from sqlite_runtime import SQLiteConnectionRole

_Result = TypeVar("_Result")
_Parameters = ParamSpec("_Parameters")


@pytest.fixture
def workspace_database(tmp_path: Path, migrated_db: Callable[..., Path]) -> Path:
    database = migrated_db(tmp_path / "approved.sqlite")
    with sqlite3.connect(database) as conn:
        conn.execute(
            "INSERT INTO macro_sensitivities "
            "(ticker, series_id, beta, r_squared, lookback_window_days, computed_at) "
            "VALUES ('TEST', 'vix', -0.4, 0.15, 90, '2026-05-01 00:00:00')"
        )
        conn.executemany(
            "INSERT INTO advisor_memos "
            "(user_id, kind, ticker, title, body_md, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            [
                (
                    "bhanu",
                    "position_review",
                    "TEST",
                    "Synthetic review",
                    "fixture " * 512,
                    "2026-05-01 00:00:00",
                )
            ]
            * 1000,
        )
    return database


def test_composite_connection_measurement(
    workspace_database: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sqlite_runtime import sqlite3 as runtime_sqlite

    calls = 0
    original = runtime_sqlite.connect

    def counted(
        function: Callable[_Parameters, _Result],
    ) -> Callable[_Parameters, _Result]:
        def wrapped(*args: _Parameters.args, **kwargs: _Parameters.kwargs) -> _Result:
            nonlocal calls
            calls += 1
            return function(*args, **kwargs)

        return wrapped

    monkeypatch.setattr(runtime_sqlite, "connect", counted(original))
    durations: list[float] = []
    per_run: list[int] = []
    for _ in range(7):
        before = calls
        started = perf_counter()
        panels = load_workspace_p3_panels("TEST", tmp_path, db_path=workspace_database)
        durations.append((perf_counter() - started) * 1000)
        per_run.append(calls - before)
        assert panels.position_review_count == 1000
        assert len(panels.macro_sensitivities) == 1
    print(
        json.dumps(
            {
                "connections_per_run": per_run,
                "median_ms": median(durations[1:]),
                "fixture_memo_rows": 1000,
                "samples": len(durations) - 1,
            }
        )
    )
    assert per_run == [1] * 7


@pytest.mark.parametrize(
    "load",
    [
        p3_data.load_macro_sensitivities,
        p3_data.load_strategic_targets,
        p3_data.load_customer_concentrations,
        p3_data.load_lease_ladder,
        p3_data.load_decision_history,
        p3_data.load_saydo_verdicts,
    ],
)
def test_missing_table_closes_owned_connection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    load: Callable[..., object],
) -> None:
    database = tmp_path / "missing-tables.sqlite"
    conn = sqlite3.connect(database)
    conn.row_factory = sqlite3.Row

    def opened(_path: Path | str, *, role: SQLiteConnectionRole) -> sqlite3.Connection:
        assert role is SQLiteConnectionRole.READ_ONLY
        return conn

    monkeypatch.setattr(p3_data, "connect_sqlite", opened)
    load("TEST", db_path=database)
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        conn.execute("SELECT 1")
    conn.close()


def test_comp_set_missing_table_closes_owned_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "missing-tables.sqlite"
    conn = sqlite3.connect(database)

    def opened(_path: Path | str, *, role: SQLiteConnectionRole) -> sqlite3.Connection:
        return conn

    monkeypatch.setattr(comp_set_context, "connect_sqlite", opened)
    assert (
        comp_set_context.load_comp_set_context("TEST", db_path=database, repo_root=tmp_path) is None
    )
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        conn.execute("SELECT 1")
    conn.close()


def test_bundle_preserves_borrowed_connection_and_transaction(
    workspace_database: Path, tmp_path: Path
) -> None:
    from report.renderers.workspace_data import PanelAvailability

    conn = sqlite3.connect(workspace_database)
    try:
        conn.execute("BEGIN")
        panels = load_workspace_p3_panels("TEST", tmp_path, conn=conn)
        assert panels.availability["macro_sensitivities"] is PanelAvailability.PRESENT
        assert panels.availability["standing_rules"] is PanelAvailability.EMPTY
        assert panels.position_review_count == 1000
        assert conn.row_factory is None
        assert conn.in_transaction
        assert conn.execute("SELECT 1").fetchone() == (1,)
    finally:
        conn.close()


def test_missing_configuration_and_unavailable_authority_are_distinct(tmp_path: Path) -> None:
    from report.renderers.workspace_data import PanelAvailability

    offline = load_workspace_p3_panels("TEST", tmp_path)
    unavailable = load_workspace_p3_panels("TEST", tmp_path, db_path=tmp_path / "missing.sqlite")
    assert set(offline.availability.values()) == {PanelAvailability.NOT_CONFIGURED}
    assert set(unavailable.availability.values()) == {PanelAvailability.UNAVAILABLE}
    assert offline.position_review_count is None
    assert unavailable.position_review_count is None
    assert not (tmp_path / "missing.sqlite").exists()


def test_partial_failure_preserves_available_panels_and_owned_connection_closes(
    workspace_database: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from report.renderers import workspace_data
    from report.renderers.workspace_data import PanelAvailability

    conn = sqlite3.connect(workspace_database)
    conn.execute("DROP TABLE advisor_memos")
    conn.commit()

    def opened(_path: str, *, role: SQLiteConnectionRole) -> sqlite3.Connection:
        return conn

    monkeypatch.setattr(workspace_data, "connect_sqlite", opened)
    panels = load_workspace_p3_panels("TEST", tmp_path, db_path=workspace_database)
    assert panels.position_review_count is None
    assert panels.availability["position_review_count"] is PanelAvailability.UNAVAILABLE
    assert panels.availability["macro_sensitivities"] is PanelAvailability.PRESENT
    assert panels.availability["standing_rules"] is PanelAvailability.EMPTY
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        conn.execute("SELECT 1")


def test_query_failure_is_unavailable_and_restores_borrowed_connection(
    workspace_database: Path, tmp_path: Path
) -> None:
    from report.renderers.workspace_data import PanelAvailability

    conn = sqlite3.connect(workspace_database)
    try:
        conn.execute("ALTER TABLE advisor_memos RENAME COLUMN kind TO unavailable_kind")
        panels = load_workspace_p3_panels("TEST", tmp_path, conn=conn)
        assert panels.availability["position_review_count"] is PanelAvailability.UNAVAILABLE
        assert panels.availability["macro_sensitivities"] is PanelAvailability.PRESENT
        assert panels.position_review_count is None
        assert conn.row_factory is None
        assert conn.execute("SELECT 1").fetchone() == (1,)
    finally:
        conn.close()


def test_memo_count_filters_and_has_no_list_limit(workspace_database: Path) -> None:
    from advisor.store import count_memos

    conn = sqlite3.connect(workspace_database)
    try:
        conn.executemany(
            "INSERT INTO advisor_memos (user_id,kind,ticker,title,body_md,created_at) "
            "VALUES ('bhanu','position_review','TEST','Synthetic','fixture','2026-05-01')",
            [()] * 10_001,
        )
        conn.executemany(
            "INSERT INTO advisor_memos (user_id,kind,ticker,title,body_md,created_at) "
            "VALUES (?,?,?,'Synthetic','fixture','2026-05-01')",
            [
                ("other", "position_review", "TEST"),
                ("bhanu", "socratic", "TEST"),
                ("bhanu", "position_review", "OTHER"),
            ],
        )
        assert count_memos(ticker="test", kind="position_review", conn=conn) == 11_001
        assert count_memos(ticker="TEST", kind="position_review", user_id="other", conn=conn) == 1
        assert count_memos(ticker="TEST", conn=conn) == 11_002
        assert count_memos(kind="position_review", conn=conn) == 11_002
        assert count_memos(ticker="NONE", conn=conn) == 0
        assert conn.in_transaction
    finally:
        conn.close()


@pytest.mark.parametrize(
    "load",
    [
        p3_data.load_macro_sensitivities,
        p3_data.load_strategic_targets,
        p3_data.load_customer_concentrations,
        p3_data.load_lease_ladder,
        p3_data.load_decision_history,
        p3_data.load_saydo_verdicts,
    ],
)
def test_accessors_preserve_borrowed_connection_and_row_factory(
    workspace_database: Path, load: Callable[..., object]
) -> None:
    conn = sqlite3.connect(workspace_database)
    try:
        load("TEST", conn=conn)
        assert conn.row_factory is None
        assert conn.execute("SELECT 1").fetchone() == (1,)
    finally:
        conn.close()


def test_peer_tracking_uses_supplied_database(workspace_database: Path, tmp_path: Path) -> None:
    fmp = tmp_path / "data" / "historical" / "fmp"
    fmp.mkdir(parents=True)
    (fmp / "TEST_peers.json").write_text(
        json.dumps([{"symbol": "TEST", "peersList": ["FIRST", "TRACKED"]}]), encoding="utf-8"
    )
    for ticker in ("TEST", "FIRST", "TRACKED"):
        (fmp / f"{ticker}_profile.json").write_text(
            json.dumps([{"companyName": ticker, "sector": "Technology", "mktCap": 1_000_000}]),
            encoding="utf-8",
        )
    conn = sqlite3.connect(workspace_database)
    try:
        conn.execute(
            "INSERT INTO tracked_companies (ticker,name,list_type) VALUES ('TRACKED','Tracked','portfolio')"
        )
        rows = p3_data.load_peer_comp("TEST", repo_root=tmp_path, max_peers=1, conn=conn)
        assert [row.peer_ticker for row in rows] == ["TRACKED"]
        assert conn.execute("SELECT 1").fetchone() == (1,)
        assert not (tmp_path / "data" / "portfolio.db").exists()
    finally:
        conn.close()


@pytest.mark.parametrize("review_count", [None, 0, 3])
def test_position_coaching_does_not_turn_unavailable_history_into_zero(
    review_count: int | None,
) -> None:
    from io import StringIO

    from report.models import PortfolioPositionSection, SectionStatus
    from report.renderers.workspace_sections.position import _position_tab

    body = StringIO()
    _position_tab(
        body,
        PortfolioPositionSection(status=SectionStatus.OK, held=True, total_quantity=10),
        ticker="TEST",
        position_review_count=review_count,
    )
    html = body.getvalue()
    if review_count is None:
        assert "Position review history is unavailable" in html
        assert "never run" not in html
    elif review_count == 0:
        assert "Guard: never run on this name" in html
    else:
        assert "consulted during 3 position reviews" in html


def test_unexpected_loader_failure_closes_owned_bundle_connection(
    workspace_database: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from report.renderers import workspace_data

    conn = sqlite3.connect(workspace_database)

    def opened(_path: str, *, role: SQLiteConnectionRole) -> sqlite3.Connection:
        return conn

    def broken(*_args: object, **_kwargs: object) -> list[p3_data.MacroSensitivityRow]:
        raise RuntimeError("synthetic programming failure")

    monkeypatch.setattr(workspace_data, "connect_sqlite", opened)
    monkeypatch.setattr(workspace_data, "load_macro_sensitivities", broken)
    with pytest.raises(RuntimeError, match="synthetic programming failure"):
        load_workspace_p3_panels("TEST", tmp_path, db_path=workspace_database)
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        conn.execute("SELECT 1")
