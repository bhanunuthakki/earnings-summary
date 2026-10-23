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


@pytest.mark.parametrize("capture", [False, True])
@pytest.mark.parametrize("mixed", [False, True])
def test_extraction_errors_return_failure_and_preserve_structured_results(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    capture: bool,
    mixed: bool,
) -> None:
    import json

    database = migrated_db(tmp_path / "result.sqlite")
    tickers = ["NU", "ZZ"] if mixed else ["NU"]
    with sqlite3.connect(database) as conn:
        conn.executemany(
            "INSERT INTO tracked_companies (ticker,name,list_type) VALUES (?,?,'portfolio')",
            [(t, f"Synthetic {t}") for t in tickers],
        )
    argv = ["summary-kpis", "--all", "--db-path", str(database), "--repo-root", str(tmp_path)]
    if capture:
        argv.append("--capture")
    monkeypatch.setattr(sys, "argv", argv)

    def extract(ticker: str, *args: object, **kwargs: object) -> TickerExtractionLog:
        return TickerExtractionLog(
            ticker=ticker,
            error="synthetic extraction failure" if ticker == "NU" else None,
            kpis_inserted_total=0 if ticker == "NU" else 2,
        )

    monkeypatch.setattr(cli, "capture_for_ticker", extract)
    monkeypatch.setattr(cli, "extract_for_ticker", extract)
    assert cli.main() == 1
    result = json.loads(capsys.readouterr().out)
    assert [r["ticker"] for r in result["summary"]] == tickers
    assert result["summary"][0]["error"] == "synthetic extraction failure"
    if mixed:
        assert result["summary"][1]["error"] is None
        assert result["summary"][1]["kpis_inserted"] == 2


def test_active_suppression_keeps_explicit_cli_result(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import json

    from models.runs import StageStatus
    from pipeline.run_accounting import PipelineRunSuppressedError

    database = migrated_db(tmp_path / "suppression.sqlite")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "summary-kpis",
            "--ticker",
            "NU",
            "--db-path",
            str(database),
            "--repo-root",
            str(tmp_path),
        ],
    )

    def suppressed(*args: object, **kwargs: object) -> TickerExtractionLog:
        raise PipelineRunSuppressedError("pipeline-test", "active-test", StageStatus.IN_PROGRESS)

    monkeypatch.setattr(cli, "extract_for_ticker", suppressed)
    assert cli.main() == 0
    assert json.loads(capsys.readouterr().out) == {
        "status": "already_running",
        "pipeline_key": "pipeline-test",
        "attempt_id": "active-test",
    }


@pytest.mark.parametrize("requested", [False, True])
def test_summary_cli_request_context_is_explicit_and_not_inferred_from_ticker(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    requested: bool,
) -> None:
    database = migrated_db(tmp_path / "request.db")
    argv = [
        "summary-kpis",
        "--ticker",
        "NU",
        "--db-path",
        str(database),
        "--repo-root",
        str(tmp_path),
    ]
    if requested:
        argv.append("--requested")
    monkeypatch.setattr(sys, "argv", argv)
    seen: list[bool] = []

    def extract(ticker: str, *args: object, **kwargs: object) -> TickerExtractionLog:
        seen.append(bool(kwargs["requested"]))
        return TickerExtractionLog(ticker=ticker)

    monkeypatch.setattr(cli, "extract_for_ticker", extract)
    assert cli.main() == 0
    assert seen == [requested]


def test_summary_cli_request_cannot_authorize_all_ticker_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "argv", ["summary-kpis", "--all", "--requested"])
    with pytest.raises(SystemExit):
        cli.main()
