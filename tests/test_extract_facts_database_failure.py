"""Fatal database failures stop extraction and retain truthful run accounting."""

from __future__ import annotations

import sqlite3
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from execution import extract_facts as cli
from models.runs import StageStatus
from pipeline import fmp_batch


@pytest.mark.parametrize("broken_bookkeeping", [False, True])
def test_fatal_extraction_preserves_completed_work_and_aborts_pending_work(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    broken_bookkeeping: bool,
) -> None:
    database = migrated_db(tmp_path / "extraction.sqlite")
    conn = sqlite3.connect(database)
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE synthetic_extractor_output (document_id INTEGER)")
    if broken_bookkeeping:
        conn.executescript(
            "CREATE TRIGGER reject_stage BEFORE INSERT ON stage_transitions "
            "WHEN NEW.status = 'failed' "
            "BEGIN SELECT RAISE(ABORT, 'bookkeeping unavailable'); END;"
            "CREATE TRIGGER reject_run BEFORE UPDATE ON ingestion_runs "
            "WHEN NEW.status = 'failed' "
            "BEGIN SELECT RAISE(ABORT, 'bookkeeping unavailable'); END;"
        )
    conn.commit()
    conn.close()
    attempted: list[int] = []
    original = sqlite3.IntegrityError("accepted fact is immutable")

    def extract(conn: sqlite3.Connection, doc_id: int, root: Path) -> int:
        attempted.append(doc_id)
        conn.execute("INSERT INTO synthetic_extractor_output VALUES (?)", (doc_id,))
        if doc_id == 2:
            raise original
        return 1

    def documents(*args: object) -> list[tuple[int, str, str]]:
        return [(n, "NU", "fmp_income_statement") for n in (1, 2, 3)]

    def fingerprint(*args: object) -> dict[str, str]:
        return {"source": "synthetic"}

    def resolve(*args: object) -> Callable[[sqlite3.Connection, int, Path], int]:
        return extract

    monkeypatch.setattr(fmp_batch, "_resolve_extractor", resolve)
    monkeypatch.setattr(cli, "_documents_for_extraction", documents)
    monkeypatch.setattr(cli, "_invocation_inputs", fingerprint)
    monkeypatch.setattr(sys, "argv", ["extract-facts", "--ticker", "NU", "--db", str(database)])
    with pytest.raises(sqlite3.IntegrityError) as caught:
        cli.main()
    assert caught.value is original
    assert attempted == [1, 2]
    with sqlite3.connect(database) as retained:
        assert retained.execute(
            "SELECT document_id FROM synthetic_extractor_output"
        ).fetchall() == [(1,)]
        if not broken_bookkeeping:
            assert retained.execute("SELECT status FROM ingestion_runs").fetchall() == [
                (StageStatus.FAILED.value,)
            ]
            assert retained.execute("SELECT status FROM pipeline_attempts").fetchall() == [
                (StageStatus.FAILED.value,)
            ]
            assert retained.execute(
                "SELECT status FROM stage_transitions ORDER BY id"
            ).fetchall() == [(StageStatus.OK.value,), (StageStatus.FAILED.value,)]
    output = capsys.readouterr()
    assert "facts_inserted" not in output.out
    if broken_bookkeeping:
        assert "failure bookkeeping failed" in output.err
