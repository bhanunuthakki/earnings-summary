"""Focused tests for exact, hash-validated document-parent recovery."""

from __future__ import annotations

import hashlib
import importlib.util
import sqlite3
import sys
from pathlib import Path
from typing import Any

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _load_module() -> Any:
    source = PROJECT_ROOT / "execution" / "repair_document_parent_links.py"
    spec = importlib.util.spec_from_file_location("repair_document_parent_links", source)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["repair_document_parent_links"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def repair_mod() -> Any:
    return _load_module()


_SCHEMA = """
CREATE TABLE alembic_version (
    version_num TEXT NOT NULL
);
INSERT INTO alembic_version VALUES ('0211_data_integrity_foundation');
CREATE TABLE documents (
    id INTEGER PRIMARY KEY,
    ticker TEXT NOT NULL,
    source_type TEXT NOT NULL,
    doc_type TEXT NOT NULL,
    period_end TEXT,
    file_path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    fetch_status TEXT NOT NULL,
    raw_bytes_size INTEGER NOT NULL,
    parent_document_id INTEGER REFERENCES documents(id)
);
CREATE TABLE transcripts (
    id INTEGER PRIMARY KEY,
    document_id INTEGER NOT NULL REFERENCES documents(id),
    ticker TEXT NOT NULL,
    fiscal_period_type TEXT,
    period_end TEXT
);
CREATE TABLE transcript_segments (
    id INTEGER PRIMARY KEY,
    transcript_id INTEGER NOT NULL REFERENCES transcripts(id),
    seq INTEGER NOT NULL,
    speaker TEXT,
    text TEXT NOT NULL
);
"""


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _seed(repo_root: Path, target_db: Path, recovery_db: Path) -> tuple[int, int, int]:
    relative = Path("transcripts") / "processed" / "NU_Q1_2025.txt"
    source_path = repo_root / relative
    source_path.parent.mkdir(parents=True)
    raw = b"Operator: welcome\nCEO: results"
    source_path.write_bytes(raw)
    parent_id, transcript_id, segment_id = 101, 201, 301

    recovery = _connect(recovery_db)
    recovery.executescript(_SCHEMA)
    recovery.execute(
        "INSERT INTO documents VALUES (?, 'NU', 'transcript_audio', 'earnings_call_transcript', "
        "'2025-03-31', ?, ?, '2025-05-01T00:00:00', 'ok', ?, NULL)",
        (parent_id, relative.as_posix(), _sha(raw), len(raw)),
    )
    recovery.execute(
        "INSERT INTO transcripts VALUES (?, ?, 'NU', 'Q1', '2025-03-31')",
        (transcript_id, parent_id),
    )
    recovery.execute(
        "INSERT INTO transcript_segments VALUES (?, ?, 0, 'Operator', 'welcome')",
        (segment_id, transcript_id),
    )
    recovery.commit()
    recovery.close()

    target = _connect(target_db)
    target.executescript(_SCHEMA)
    # Simulate the historical bug: the child persisted while FK checks were off.
    target.commit()
    target.execute("PRAGMA foreign_keys = OFF")
    target.execute(
        "INSERT INTO documents VALUES (401, 'NU', 'llm_extracted', 'llm_summary', "
        "'2025-03-31', '.tmp/NU_Q1_2025_summary.txt', ?, '2025-05-02T00:00:00', 'ok', 4, ?)",
        (_sha(b"sum"), parent_id),
    )
    target.commit()
    target.close()
    return parent_id, transcript_id, segment_id


def test_dry_run_is_read_only_and_plans_exact_rows(tmp_path: Path, repair_mod: Any) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    target_db, recovery_db = tmp_path / "target.db", tmp_path / "recovery.db"
    parent_id, transcript_id, segment_id = _seed(repo_root, target_db, recovery_db)

    result = repair_mod.repair(target_db, recovery_db, repo_root, apply=False)

    assert result.mode == "dry_run"
    assert result.dangling_children == 1
    assert result.blocked_parent_roots == 0
    assert result.items[0].status == "planned"
    assert result.items[0].restored_document_ids == [parent_id]
    assert result.items[0].restored_transcript_ids == [transcript_id]
    assert result.items[0].restored_segment_ids == [segment_id]
    conn = _connect(target_db)
    assert (
        conn.execute("SELECT COUNT(*) FROM documents WHERE id = ?", (parent_id,)).fetchone()[0] == 0
    )
    conn.close()


def test_apply_restores_document_transcript_segments_and_passes_fk_check(
    tmp_path: Path, repair_mod: Any
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    target_db, recovery_db = tmp_path / "target.db", tmp_path / "recovery.db"
    parent_id, transcript_id, segment_id = _seed(repo_root, target_db, recovery_db)

    result = repair_mod.repair(target_db, recovery_db, repo_root, apply=True)

    assert result.restored_documents == 1
    assert result.restored_transcripts == 1
    assert result.restored_segments == 1
    assert result.foreign_key_violations_after == 0
    assert result.items[0].status == "restored"
    conn = _connect(target_db)
    assert (
        conn.execute("SELECT id FROM documents WHERE id = ?", (parent_id,)).fetchone()[0]
        == parent_id
    )
    assert (
        conn.execute("SELECT id FROM transcripts WHERE id = ?", (transcript_id,)).fetchone()[0]
        == transcript_id
    )
    assert (
        conn.execute("SELECT id FROM transcript_segments WHERE id = ?", (segment_id,)).fetchone()[0]
        == segment_id
    )
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    conn.close()


def test_apply_is_idempotent_after_success(tmp_path: Path, repair_mod: Any) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    target_db, recovery_db = tmp_path / "target.db", tmp_path / "recovery.db"
    _seed(repo_root, target_db, recovery_db)

    first = repair_mod.repair(target_db, recovery_db, repo_root, apply=True)
    second = repair_mod.repair(target_db, recovery_db, repo_root, apply=True)

    assert first.restored_documents == 1
    assert second.dangling_children == 0
    assert second.restored_documents == 0
    assert second.foreign_key_violations_after == 0


def test_hash_mismatch_blocks_entire_repair_without_writing(
    tmp_path: Path, repair_mod: Any
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    target_db, recovery_db = tmp_path / "target.db", tmp_path / "recovery.db"
    parent_id, _, _ = _seed(repo_root, target_db, recovery_db)
    (repo_root / "transcripts" / "processed" / "NU_Q1_2025.txt").write_bytes(b"changed")

    result = repair_mod.repair(target_db, recovery_db, repo_root, apply=True)

    assert result.blocked_parent_roots == 1
    assert "SHA-256 mismatch" in str(result.items[0].reason)
    conn = _connect(target_db)
    assert (
        conn.execute("SELECT COUNT(*) FROM documents WHERE id = ?", (parent_id,)).fetchone()[0] == 0
    )
    conn.close()


def test_path_outside_repo_is_blocked(tmp_path: Path, repair_mod: Any) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    target_db, recovery_db = tmp_path / "target.db", tmp_path / "recovery.db"
    _seed(repo_root, target_db, recovery_db)
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"Operator: welcome\nCEO: results")
    conn = _connect(recovery_db)
    conn.execute("UPDATE documents SET file_path = ? WHERE id = 101", (str(outside),))
    conn.commit()
    conn.close()

    result = repair_mod.repair(target_db, recovery_db, repo_root, apply=False)

    assert result.blocked_parent_roots == 1
    assert "escapes repo root" in str(result.items[0].reason)


def test_one_unrecoverable_root_rolls_back_all_planned_restores(
    tmp_path: Path, repair_mod: Any
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    target_db, recovery_db = tmp_path / "target.db", tmp_path / "recovery.db"
    parent_id, _, _ = _seed(repo_root, target_db, recovery_db)
    conn = _connect(target_db)
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute(
        "INSERT INTO documents VALUES (402, 'NU', 'llm_extracted', 'llm_summary', "
        "'2025-03-31', '.tmp/NU_Q1_other_summary.txt', ?, '2025-05-02T00:00:00', 'ok', 4, 999)",
        (_sha(b"other"),),
    )
    conn.commit()
    conn.close()

    result = repair_mod.repair(target_db, recovery_db, repo_root, apply=True)

    assert result.blocked_parent_roots == 1
    conn = _connect(target_db)
    # Parent 101 was valid and planned, but a partial recovery is prohibited.
    assert (
        conn.execute("SELECT COUNT(*) FROM documents WHERE id = ?", (parent_id,)).fetchone()[0] == 0
    )
    conn.close()


def test_revision_mismatch_blocks_before_planning(tmp_path: Path, repair_mod: Any) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    target_db, recovery_db = tmp_path / "target.db", tmp_path / "recovery.db"
    _seed(repo_root, target_db, recovery_db)
    conn = _connect(recovery_db)
    conn.execute("UPDATE alembic_version SET version_num = 'different'")
    conn.commit()
    conn.close()

    with pytest.raises(repair_mod.RepairBlockedError, match="alembic revision mismatch"):
        repair_mod.repair(target_db, recovery_db, repo_root, apply=False)

    allowed = repair_mod.repair(
        target_db,
        recovery_db,
        repo_root,
        apply=False,
        allowed_recovery_revision="different",
    )
    assert allowed.blocked_parent_roots == 0


def test_explicit_historical_revision_allows_nullable_target_column(
    tmp_path: Path, repair_mod: Any
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    target_db, recovery_db = tmp_path / "target.db", tmp_path / "recovery.db"
    parent_id, transcript_id, _ = _seed(repo_root, target_db, recovery_db)
    conn = _connect(recovery_db)
    conn.execute("UPDATE alembic_version SET version_num = 'historical'")
    conn.commit()
    conn.close()
    conn = _connect(target_db)
    conn.execute("ALTER TABLE transcripts ADD COLUMN source TEXT")
    conn.commit()
    conn.close()

    result = repair_mod.repair(
        target_db,
        recovery_db,
        repo_root,
        apply=True,
        allowed_recovery_revision="historical",
    )

    assert result.blocked_parent_roots == 0
    conn = _connect(target_db)
    restored = conn.execute(
        "SELECT source FROM transcripts WHERE id = ?", (transcript_id,)
    ).fetchone()
    assert restored is not None
    assert restored[0] is None
    assert (
        conn.execute("SELECT COUNT(*) FROM documents WHERE id = ?", (parent_id,)).fetchone()[0] == 1
    )
    conn.close()


def test_same_hash_document_is_relinked_and_transcript_lineage_restored(
    tmp_path: Path, repair_mod: Any
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    target_db, recovery_db = tmp_path / "target.db", tmp_path / "recovery.db"
    old_parent_id, transcript_id, segment_id = _seed(repo_root, target_db, recovery_db)
    canonical_id = 777
    raw = b"Operator: welcome\nCEO: results"
    conn = _connect(target_db)
    conn.execute(
        "INSERT INTO documents VALUES (?, 'NU', 'transcript_audio', "
        "'earnings_call_transcript', '2025-03-31', "
        "'transcripts/processed/NU_Q1_2025.txt', ?, "
        "'2025-06-01T00:00:00', 'ok', ?, NULL)",
        (canonical_id, _sha(raw), len(raw)),
    )
    conn.commit()
    conn.close()

    result = repair_mod.repair(target_db, recovery_db, repo_root, apply=True)

    assert result.items[0].status == "relinked"
    assert result.items[0].replacement_parent_document_id == canonical_id
    assert result.restored_documents == 0
    assert result.restored_transcripts == 1
    assert result.restored_segments == 1
    conn = _connect(target_db)
    assert (
        conn.execute("SELECT parent_document_id FROM documents WHERE id = 401").fetchone()[0]
        == canonical_id
    )
    assert (
        conn.execute(
            "SELECT document_id FROM transcripts WHERE id = ?", (transcript_id,)
        ).fetchone()[0]
        == canonical_id
    )
    assert (
        conn.execute(
            "SELECT transcript_id FROM transcript_segments WHERE id = ?", (segment_id,)
        ).fetchone()[0]
        == transcript_id
    )
    assert (
        conn.execute("SELECT COUNT(*) FROM documents WHERE id = ?", (old_parent_id,)).fetchone()[0]
        == 0
    )
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    conn.close()


def test_same_hash_document_with_different_metadata_blocks(tmp_path: Path, repair_mod: Any) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    target_db, recovery_db = tmp_path / "target.db", tmp_path / "recovery.db"
    _seed(repo_root, target_db, recovery_db)
    raw = b"Operator: welcome\nCEO: results"
    conn = _connect(target_db)
    conn.execute(
        "INSERT INTO documents VALUES (777, 'BAD', 'transcript_audio', "
        "'earnings_call_transcript', '2025-03-31', "
        "'transcripts/processed/NU_Q1_2025.txt', ?, "
        "'2025-06-01T00:00:00', 'ok', ?, NULL)",
        (_sha(raw), len(raw)),
    )
    conn.commit()
    conn.close()

    result = repair_mod.repair(target_db, recovery_db, repo_root, apply=False)

    assert result.blocked_parent_roots == 1
    assert result.items[0].status == "blocked"
    assert "same-hash" in str(result.items[0].reason)


def test_existing_natural_key_transcript_is_preserved_during_parent_relink(
    tmp_path: Path, repair_mod: Any
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    target_db, recovery_db = tmp_path / "target.db", tmp_path / "recovery.db"
    _seed(repo_root, target_db, recovery_db)
    raw = b"Operator: welcome\nCEO: results"
    conn = _connect(target_db)
    conn.execute(
        "INSERT INTO documents VALUES (777, 'NU', 'transcript_audio', "
        "'earnings_call_transcript', '2025-03-31', "
        "'transcripts/processed/NU_Q1_2025.txt', ?, "
        "'2025-06-01T00:00:00', 'ok', ?, NULL)",
        (_sha(raw), len(raw)),
    )
    conn.execute("INSERT INTO transcripts VALUES (999, 777, 'NU', 'Q1', '2025-03-31')")
    conn.execute(
        "INSERT INTO transcript_segments VALUES "
        "(888, 999, 0, 'Operator', 'richer canonical segmentation')"
    )
    conn.commit()
    conn.close()

    result = repair_mod.repair(target_db, recovery_db, repo_root, apply=True)

    assert result.items[0].status == "relinked"
    assert result.restored_transcripts == 0
    assert result.restored_segments == 0
    conn = _connect(target_db)
    assert (
        conn.execute("SELECT parent_document_id FROM documents WHERE id = 401").fetchone()[0] == 777
    )
    assert conn.execute("SELECT text FROM transcript_segments WHERE id = 888").fetchone()[0] == (
        "richer canonical segmentation"
    )
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    conn.close()


def test_same_columns_with_different_constraints_is_blocked(
    tmp_path: Path, repair_mod: Any
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    target_db, recovery_db = tmp_path / "target.db", tmp_path / "recovery.db"
    _seed(repo_root, target_db, recovery_db)
    conn = _connect(recovery_db)
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("ALTER TABLE transcript_segments RENAME TO transcript_segments_old")
    conn.execute(
        "CREATE TABLE transcript_segments ("
        "id INTEGER PRIMARY KEY, transcript_id INTEGER NOT NULL, seq INTEGER NOT NULL, "
        "speaker TEXT, text TEXT NOT NULL)"
    )
    conn.execute("INSERT INTO transcript_segments SELECT * FROM transcript_segments_old")
    conn.execute("DROP TABLE transcript_segments_old")
    conn.commit()
    conn.close()

    with pytest.raises(repair_mod.RepairBlockedError, match="schema DDL mismatch"):
        repair_mod.repair(target_db, recovery_db, repo_root, apply=False)


def test_live_target_requires_explicit_override(tmp_path: Path, repair_mod: Any) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    live = repo_root / "data" / "portfolio.db"
    live.parent.mkdir()
    recovery = tmp_path / "recovery.db"
    _seed(repo_root, live, recovery)

    with pytest.raises(repair_mod.RepairBlockedError, match="refusing live DB repair"):
        repair_mod.repair(live, recovery, repo_root, apply=True)


def test_external_env_canonical_live_target_requires_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    repair_mod: Any,
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    live = tmp_path / "canonical.db"
    recovery = tmp_path / "recovery.db"
    _seed(repo_root, live, recovery)
    env_file = tmp_path / "external.env"
    env_file.write_text(f"EARNINGS_SUMMARY_DB_PATH={live}\n", encoding="utf-8")
    monkeypatch.setenv("EARNINGS_SUMMARY_ENV_FILE", str(env_file))
    monkeypatch.delenv("EARNINGS_SUMMARY_DB_PATH", raising=False)

    with pytest.raises(repair_mod.RepairBlockedError, match="refusing live DB repair"):
        repair_mod.repair(live, recovery, repo_root, apply=True)
