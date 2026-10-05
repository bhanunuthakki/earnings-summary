"""Onboarding keeps code, retained state and database authorities distinct."""

from __future__ import annotations

import sqlite3
import sys
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

import db
from execution import backfill_transcripts, process_ir_documents_state, save_fmp_data
from pipeline import quarterly_refresh
from pipeline.sec_xbrl import IngestStats


def _no_environment(*_args: object) -> bool:
    return False


@pytest.fixture
def state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    root = tmp_path / "retained"
    root.mkdir()
    database = tmp_path / "separate-authority.sqlite"
    database.touch()
    for field in ("DB_PATH", "DATA_DIR", "FMP_DIR", "PROJECT_ROOT"):
        monkeypatch.setattr(db, field, getattr(db, field))
    if hasattr(db, "STATE_ROOT"):
        monkeypatch.setattr(db, "STATE_ROOT", db.STATE_ROOT)
    monkeypatch.setenv("EARNINGS_SUMMARY_DB_PATH", str(database))
    return root, database


def test_explicit_state_is_independent_of_database_parent(state: tuple[Path, Path]) -> None:
    root, database = state
    code_root = db.PROJECT_ROOT
    db.set_db_path(database, state_root=root)
    assert Path(db.DB_PATH) == database
    assert Path(db.DATA_DIR) == root / "data"
    assert Path(db.FMP_DIR) == root / "data/historical/fmp"
    assert code_root == db.PROJECT_ROOT


def test_fmp_retained_bytes_and_telemetry_use_selected_authorities(
    state: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, database = state
    for name in (
        "PROJECT_ROOT",
        "FMP_DIR",
        "SNAP_DIR",
        "SECTOR_DIR",
        "_BUDGET_DIR",
        "_VALIDATION_DUMP_DIR",
    ):
        monkeypatch.setattr(save_fmp_data, name, getattr(save_fmp_data, name))
    monkeypatch.setattr(save_fmp_data, "load_project_env", _no_environment)
    save_fmp_data.configure_runtime(root, database)
    assert root / "data/historical/fmp" == save_fmp_data.FMP_DIR
    assert root / "data/historical/fmp_snapshots" == save_fmp_data.SNAP_DIR
    assert root / ".tmp/cacher" == getattr(save_fmp_data, "_BUDGET_DIR")
    assert Path(db.DB_PATH) == database
    assert not (root / "data/portfolio.db").exists()


def test_transcript_retarget_preserves_explicit_database(
    state: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, database = state
    for name in ("_RAW_DIR", "_PROCESSED_DIR"):
        monkeypatch.setattr(backfill_transcripts, name, getattr(backfill_transcripts, name))
    cast(Callable[..., None], getattr(backfill_transcripts, "_retarget_paths"))(
        root, db_path=database
    )
    assert Path(db.DB_PATH) == database
    assert root / "transcripts/raw" == getattr(backfill_transcripts, "_RAW_DIR")
    assert root / "transcripts/processed" == getattr(backfill_transcripts, "_PROCESSED_DIR")


def test_ir_state_adapter_preserves_database_and_missing_summary_selector(
    state: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, database = state
    index = SimpleNamespace()
    seen: list[str] = []
    processor = SimpleNamespace(
        PROJECT_ROOT=Path("."),
        CACHE_DIR=Path("."),
        index_manager=index,
        main=lambda: seen.extend(sys.argv),
    )
    monkeypatch.setattr(process_ir_documents_state, "_load_processor", lambda: processor)
    assert (
        process_ir_documents_state.main(
            [
                "--ticker",
                "NEWCO",
                "--repo-root",
                str(root),
                "--db",
                str(database),
                "--regenerate-missing",
            ]
        )
        == 0
    )
    assert Path(db.DB_PATH) == database
    assert root == processor.PROJECT_ROOT
    assert seen == ["process_ir_documents.py", "--ticker", "NEWCO", "--regenerate-missing"]


def test_quarterly_sec_selection_does_not_depend_on_static_cik_map(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    conn = sqlite3.connect(":memory:")
    calls: list[str] = []

    def authorize(*_args: object, **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(allowed=True)

    def ingest(_conn: sqlite3.Connection, *, ticker: str, project_root: Path) -> IngestStats:
        del _conn, project_root
        calls.append(ticker)
        return IngestStats(accessions_inserted=1, facts_inserted=2)

    monkeypatch.setattr(
        quarterly_refresh,
        "authorize_collection_target_in_connection",
        authorize,
    )
    monkeypatch.setattr(
        quarterly_refresh,
        "ingest_sec_for_ticker",
        ingest,
    )
    try:
        result = cast(
            Callable[..., quarterly_refresh.StageResult],
            getattr(quarterly_refresh, "_stage_fetch_sec_xbrl"),
        )(conn, ticker="NEWCO", project_root=tmp_path, owner_requested=False)
    finally:
        conn.close()
    assert calls == ["NEWCO"]
    assert result.status is quarterly_refresh.StageStatus.OK
    assert result.rows_processed == 2
