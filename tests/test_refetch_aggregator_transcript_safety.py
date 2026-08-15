"""Evidence-snapshot and transaction regressions for aggregator refetch."""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from compute import evidence_snapshot, transcript_ingest

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import NoReturn


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _forbidden(message: str) -> Callable[..., NoReturn]:
    def fail(*_args: object, **_kwargs: object) -> NoReturn:
        pytest.fail(message)

    return fail


def _load_module() -> Any:
    source = PROJECT_ROOT / "execution" / "refetch_aggregator_transcripts.py"
    spec = importlib.util.spec_from_file_location("refetch_aggregator_safety_test", source)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["refetch_aggregator_safety_test"] = module
    spec.loader.exec_module(module)
    return module


def _body(marker: str) -> str:
    return (
        "Operator\nWelcome.\n\nChief Executive Officer\n"
        + (f"{marker} Revenue grew. " * 80)
        + "\n\nQUESTION AND ANSWER SECTION\n"
    )


def _repo(tmp_path: Path, migrated_db: Callable[..., Path]) -> tuple[Path, Path, Path]:
    repo_root = tmp_path / "repo"
    raw = repo_root / "transcripts" / "raw"
    raw.mkdir(parents=True)
    db_path = migrated_db(repo_root / "data" / "portfolio.db")
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO tracked_companies (ticker, name, list_type) "
            "VALUES ('NU', 'Nu Holdings', 'portfolio')"
        )
    path = raw / "NU_Q1_2026.txt"
    path.write_text(_body("ORIGINAL"), encoding="utf-8")
    return repo_root, db_path, path


def test_refetch_direct_denial_precedes_network_and_snapshot(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mod = _load_module()
    repo_root, db_path, _path = _repo(tmp_path, migrated_db)
    monkeypatch.setattr(
        mod.fetch_qa_transcript,
        "fetch_qa",
        _forbidden("denied refetch crossed the network boundary"),
    )
    monkeypatch.setattr(
        evidence_snapshot,
        "capture_snapshot",
        _forbidden("denied refetch captured mutable evidence"),
    )
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        before = tuple(
            conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("documents", "transcripts", "transcript_segments")
        )
        with pytest.raises(mod.TranscriptAcquisitionDeniedError):
            mod._process_one(
                conn,
                "NU",
                2026,
                1,
                frozenset({"NU"}),
                False,
                repo_root,
                repo_root / "data" / "portfolio.db",
                False,
            )
        after = tuple(
            conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("documents", "transcripts", "transcript_segments")
        )

    assert after == before


def test_refetch_direct_denial_precedes_network_and_transcript_writes(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mod = _load_module()
    repo_root, db_path, _path = _repo(tmp_path, migrated_db)
    monkeypatch.setattr(
        mod.fetch_qa_transcript,
        "fetch_qa",
        _forbidden("denied refetch crossed the network boundary"),
    )
    monkeypatch.setattr(
        transcript_ingest,
        "_insert_segments",
        _forbidden("denied refetch crossed persistence"),
    )
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        before = tuple(
            conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("documents", "transcripts", "transcript_segments")
        )
        with pytest.raises(mod.TranscriptAcquisitionDeniedError):
            mod._process_one(
                conn,
                "NU",
                2026,
                1,
                frozenset({"NU"}),
                False,
                repo_root,
                repo_root / "data" / "portfolio.db",
                False,
            )
        after = tuple(
            conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("documents", "transcripts", "transcript_segments")
        )

    assert after == before
