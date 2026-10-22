"""Summary extraction must share one database across all of its consumers."""

from __future__ import annotations

import sqlite3
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

import db
from compute.kpi_extract_summaries import TickerExtractionLog
from db_paths import resolve_db_path
from execution import extract_kpis_from_summaries as cli


@pytest.mark.parametrize("explicit", [False, True])
@pytest.mark.parametrize("capture", [False, True])
def test_roster_extraction_and_bookkeeping_share_database(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    explicit: bool,
    capture: bool,
) -> None:
    database = migrated_db(tmp_path / "authority.sqlite")
    with sqlite3.connect(database) as conn:
        conn.execute(
            "INSERT INTO tracked_companies (ticker, name, list_type) VALUES ('NU', 'Synthetic NU', 'portfolio')"
        )
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()
    # An explicit target must also override a conflicting process default.
    monkeypatch.setattr(
        db, "DB_PATH", str(tmp_path / "unavailable.sqlite") if explicit else str(database)
    )
    argv = ["summary-kpis", "--all", "--repo-root", str(artifact_root)]
    if explicit:
        argv.extend(["--db-path", str(database)])
    if capture:
        argv.append("--capture")
    monkeypatch.setattr(sys, "argv", argv)
    observed: list[tuple[str, Path, Path, Path | None]] = []

    def extract(
        ticker: str, repo_root: Path, conn: sqlite3.Connection, **kwargs: object
    ) -> TickerExtractionLog:
        target = Path(conn.execute("PRAGMA database_list").fetchone()[2])
        observed.append((ticker, repo_root, target, resolve_db_path(None)))
        return TickerExtractionLog(ticker=ticker)

    monkeypatch.setattr(cli, "capture_for_ticker", extract)
    monkeypatch.setattr(cli, "extract_for_ticker", extract)
    original_context = resolve_db_path(None)
    assert cli.main() == 0
    assert observed == [("NU", artifact_root, database, database)]
    assert resolve_db_path(None) == original_context
    assert not (artifact_root / "data" / "portfolio.db").exists()
    assert (artifact_root / "data" / "kpi_extraction_log.json").is_file()


@pytest.mark.parametrize("target", ["missing", "checkout"])
def test_invalid_database_stops_before_connection_or_extraction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, target: str
) -> None:
    database = (
        Path(cli.__file__).resolve().parents[1] / "data" / "portfolio.db"
        if target == "checkout"
        else tmp_path / "missing.sqlite"
    )
    monkeypatch.setattr(db, "DB_PATH", str(database))
    monkeypatch.setattr(
        sys, "argv", ["summary-kpis", "--ticker", "NU", "--repo-root", str(tmp_path)]
    )

    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("An invalid database reached extraction or connection creation")

    monkeypatch.setattr(cli, "connect_sqlite", forbidden)
    monkeypatch.setattr(cli, "extract_for_ticker", forbidden)
    assert cli.main() == 1
    assert not database.exists()
