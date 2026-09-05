"""Migrated-schema regressions for transcript evidence-path immutability."""

from __future__ import annotations

import errno
import importlib.util
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import pytest

from compute import transcript_ingest

if TYPE_CHECKING:
    from collections.abc import Callable

    from compute.transcript_ingest import IngestResult


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _load_module() -> Any:
    source = PROJECT_ROOT / "execution" / "ingest_transcripts.py"
    spec = importlib.util.spec_from_file_location("ingest_transcripts_immutable_test", source)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["ingest_transcripts_immutable_test"] = module
    spec.loader.exec_module(module)
    return module


def _body(marker: str) -> str:
    return (
        "Operator\nWelcome to the call.\n\n"
        "Chief Executive Officer\n"
        + marker
        + " Revenue grew and customers expanded. " * 80
        + "\n\nChief Financial Officer\nMargins improved. " * 80
        + "\n\nQUESTION AND ANSWER SECTION\n"
    )


def test_unreceipted_raw_ingest_fails_before_files_or_database_writes(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mod = _load_module()
    repo_root = tmp_path / "repo"
    raw_dir = repo_root / "transcripts" / "raw"
    processed_dir = repo_root / "transcripts" / "processed"
    raw_dir.mkdir(parents=True)
    processed_dir.mkdir(parents=True)
    db_path = migrated_db(repo_root / "data" / "portfolio.db")
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO tracked_companies (ticker, name, list_type) "
            "VALUES ('NU', 'Nu Holdings', 'portfolio')"
        )

    raw_path = raw_dir / "NU_Q1_2026.txt"
    original = _body("ORIGINAL")
    raw_path.write_text(original, encoding="utf-8")
    monkeypatch.setattr(mod, "PROJECT_ROOT", repo_root)
    monkeypatch.setattr(mod, "_TRANSCRIPT_DIRS", (processed_dir, raw_dir))

    monkeypatch.setattr(
        sys,
        "argv",
        ["ingest_transcripts.py", "--db", str(db_path), "--no-promote"],
    )
    assert mod.main() == 2
    assert raw_path.read_text(encoding="utf-8") == original
    assert not (processed_dir / raw_path.name).exists()

    with sqlite3.connect(db_path) as conn:
        first_counts = (
            conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0],
            conn.execute("SELECT COUNT(*) FROM transcripts").fetchone()[0],
        )
        assert first_counts == (0, 0)

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "ingest_transcripts.py",
            "--db",
            str(db_path),
            "--no-promote",
            "--force",
        ],
    )
    assert mod.main() == 2
    with sqlite3.connect(db_path) as conn:
        assert (
            conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0],
            conn.execute("SELECT COUNT(*) FROM transcripts").fetchone()[0],
        ) == first_counts

    raw_path.write_text(_body("DIFFERENT"), encoding="utf-8")
    assert mod.main() == 2
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM transcripts").fetchone()[0] == 0


@pytest.mark.parametrize(
    (
        "source_type",
        "doc_type",
        "fetch_status",
        "is_active",
        "is_current",
        "segment_count",
        "expected",
    ),
    [
        ("transcript_audio", "earnings_call_transcript", "ok", 1, 1, 1, True),
        ("transcript_audio", "earnings_call_transcript", "ok", 0, 0, 1, False),
        ("transcript_audio", "ir_press_release", "ok", 1, 1, 1, False),
        ("ir_doc", "earnings_call_transcript", "ok", 1, 1, 1, False),
        ("transcript_audio", "earnings_call_transcript", "failed", 1, 1, 1, False),
        ("transcript_audio", "earnings_call_transcript", "ok", 1, 1, 0, False),
    ],
)
def test_legacy_processed_skip_requires_exact_canonical_ingest_evidence(
    source_type: str,
    doc_type: str,
    fetch_status: str,
    is_active: int,
    is_current: int,
    segment_count: int,
    expected: bool,
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    mod = _load_module()
    repo_root = tmp_path / "repo"
    processed_root = repo_root / "transcripts" / "processed"
    processed_root.mkdir(parents=True)
    path = processed_root / "NU_Q1_2026.txt"
    payload = _body("LEGACY")
    path.write_text(payload, encoding="utf-8")
    digest = transcript_ingest.sha256_of(path)
    parsed = transcript_ingest.parse_transcript_filename(path)
    assert parsed is not None
    db_path = migrated_db(repo_root / "data" / "portfolio.db")

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        document = conn.execute(
            "INSERT INTO documents "
            "(ticker,source_type,doc_type,period_end,file_path,sha256,fetched_at,fetch_status,"
            "raw_bytes_size) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                "NU",
                source_type,
                doc_type,
                "2026-03-31 00:00:00",
                "transcripts/processed/NU_Q1_2026.txt",
                digest,
                "2026-04-01 00:00:00",
                fetch_status,
                len(payload.encode("utf-8")),
            ),
        )
        assert document.lastrowid is not None
        superseded_at: str | None = None
        superseded_by: int | None = None
        version_number = 1
        if not is_current:
            winner_document = conn.execute(
                "INSERT INTO documents "
                "(ticker,source_type,doc_type,period_end,file_path,sha256,fetched_at,fetch_status,"
                "raw_bytes_size) VALUES "
                "('NU','transcript_audio','earnings_call_transcript','2026-03-31 00:00:00',"
                "'transcripts/processed/NU_Q1_2026_winner.txt',?,'2026-04-02 00:00:00','ok',1)",
                ("0" * 64,),
            )
            assert winner_document.lastrowid is not None
            winner_transcript = conn.execute(
                "INSERT INTO transcripts "
                "(document_id,ticker,fiscal_period_type,period_end,source,is_active,is_current) "
                "VALUES (?,'NU','Q1','2026-03-31 00:00:00','issuer_ir',1,1)",
                (winner_document.lastrowid,),
            )
            assert winner_transcript.lastrowid is not None
            superseded_at = "2026-04-02 00:00:00"
            superseded_by = winner_transcript.lastrowid
            version_number = 2
        transcript = conn.execute(
            "INSERT INTO transcripts "
            "(document_id,ticker,fiscal_period_type,period_end,source,is_active,is_current,"
            "superseded_at,superseded_by_transcript_id,superseded_by_id,version_number) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                document.lastrowid,
                "NU",
                "Q1",
                "2026-03-31 00:00:00",
                "unknown_legacy",
                is_active,
                is_current,
                superseded_at,
                superseded_by,
                superseded_by,
                version_number,
            ),
        )
        assert transcript.lastrowid is not None
        if segment_count:
            conn.execute(
                "INSERT INTO transcript_segments (transcript_id,seq,text) VALUES (?,0,?)",
                (transcript.lastrowid, "Legacy transcript segment."),
            )

        assert (
            mod._is_exactly_ingested_processed_candidate(
                conn,
                path=path,
                parsed=parsed,
                project_root=repo_root,
                processed_root=processed_root,
            )
            is expected
        )


def test_staging_rejects_source_mutation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    mod = _load_module()
    raw = tmp_path / "transcripts" / "raw"
    processed = tmp_path / "transcripts" / "processed"
    raw.mkdir(parents=True)
    processed.mkdir(parents=True)
    source = raw / "NU_Q1_2026.txt"
    source.write_text(_body("ORIGINAL"), encoding="utf-8")
    identities = [
        (1, 1, 1, 1),
        (1, 1, 1, 1),
        (1, 2, 3, 4),
        (1, 2, 5, 4),
    ]

    def changing_identity(_stat: os.stat_result) -> tuple[int, int, int, int]:
        return identities.pop(0)

    monkeypatch.setattr(mod.evidence_snapshot, "_identity", changing_identity)

    with pytest.raises(mod.evidence_snapshot.EvidenceSourceChangedError):
        mod._stage_evidence_file(source, tmp_path, raw, processed)


def test_staging_rejects_reparse_source_root(tmp_path: Path) -> None:
    mod = _load_module()
    outside = tmp_path / "outside"
    outside.mkdir()
    raw = tmp_path / "transcripts" / "raw"
    raw.parent.mkdir(parents=True)
    try:
        raw.symlink_to(outside, target_is_directory=True)
    except OSError:
        if os.name != "nt":
            pytest.skip("directory symlinks unavailable")
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(raw), str(outside)],
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            pytest.skip("directory symlinks/junctions unavailable")
    processed = tmp_path / "transcripts" / "processed"
    processed.mkdir()
    source = raw / "NU_Q1_2026.txt"
    source.write_text(_body("ORIGINAL"), encoding="utf-8")

    with pytest.raises(mod.UnsafeEvidencePathError):
        mod._stage_evidence_file(source, tmp_path, raw, processed)


@pytest.mark.skipif(os.name == "nt", reason="POSIX O_NOFOLLOW contract")
def test_posix_nofollow_error_is_typed_unsafe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from compute import evidence_snapshot

    def reject_link(_path: Path, _flags: int) -> int:
        raise OSError(errno.ELOOP, "too many symbolic links")

    monkeypatch.setattr(evidence_snapshot.os, "open", reject_link)
    with pytest.raises(evidence_snapshot.UnsafeEvidencePathError, match="unsafe link"):
        evidence_snapshot.capture_snapshot(tmp_path / "link", tmp_path)


def test_opened_handle_containment_rejects_deterministic_swap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mod = _load_module()
    raw = tmp_path / "transcripts" / "raw"
    raw.mkdir(parents=True)
    source = raw / "NU_Q1_2026.txt"
    source.write_text(_body("ORIGINAL"), encoding="utf-8")
    outside = tmp_path / "outside" / source.name
    outside.parent.mkdir()
    outside.write_text(_body("SWAPPED"), encoding="utf-8")
    finals = [outside.resolve(), raw.resolve()]

    def swapped_final_path(_fd: int, _fallback: Path) -> Path:
        return finals.pop(0)

    monkeypatch.setattr(mod.evidence_snapshot, "_final_path", swapped_final_path)

    with pytest.raises(mod.evidence_snapshot.UnsafeEvidencePathError):
        mod.evidence_snapshot.capture_snapshot(source, raw)


def test_per_file_savepoint_rolls_back_partial_writes_but_keeps_failure_receipt(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mod = _load_module()
    repo_root = tmp_path / "repo"
    raw = repo_root / "transcripts" / "raw"
    processed = repo_root / "transcripts" / "processed"
    raw.mkdir(parents=True)
    processed.mkdir(parents=True)
    db_path = migrated_db(repo_root / "data" / "portfolio.db")
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO tracked_companies (ticker, name, list_type) "
            "VALUES ('NU', 'Nu Holdings', 'portfolio')"
        )
        before = tuple(
            conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("documents", "transcripts", "transcript_segments")
        )
    (raw / "NU_Q1_2026.txt").write_text(_body("ORIGINAL"), encoding="utf-8")
    monkeypatch.setattr(mod, "PROJECT_ROOT", repo_root)
    monkeypatch.setattr(mod, "_TRANSCRIPT_DIRS", (processed, raw))
    original_insert = cast("Callable[..., int]", getattr(transcript_ingest, "_insert_segments"))

    def partial_then_fail(*args: Any, **kwargs: Any) -> int:
        original_insert(*args, **kwargs)
        raise RuntimeError("injected after partial writes")

    monkeypatch.setattr(transcript_ingest, "_insert_segments", partial_then_fail)
    monkeypatch.setattr(
        sys,
        "argv",
        ["ingest_transcripts.py", "--db", str(db_path), "--no-promote", "--force"],
    )
    assert mod.main() == 2
    with sqlite3.connect(db_path) as conn:
        after = tuple(
            conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("documents", "transcripts", "transcript_segments")
        )
        assert after == before
        assert conn.execute("SELECT COUNT(*) FROM stage_transitions").fetchone()[0] == 0


def test_ingest_parses_the_same_snapshot_bytes_used_for_hash(tmp_path: Path) -> None:
    ingest_one = cast(
        "Callable[..., IngestResult | None]", getattr(transcript_ingest, "_ingest_one")
    )

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE documents (
            id INTEGER PRIMARY KEY, ticker TEXT, source_type TEXT, doc_type TEXT,
            period_start TEXT, period_end TEXT, file_path TEXT, sha256 TEXT,
            fetched_at TEXT, fetch_status TEXT, http_code INTEGER,
            raw_bytes_size INTEGER, source_url TEXT, parent_document_id INTEGER
        );
        CREATE TABLE transcripts (
            id INTEGER PRIMARY KEY, document_id INTEGER, ticker TEXT, call_date TEXT,
            fiscal_period_type TEXT, period_end TEXT, source_url TEXT,
            has_qa_section INTEGER, source TEXT, version_number INTEGER DEFAULT 1,
            is_current INTEGER DEFAULT 1, recorded_at TEXT, superseded_at TEXT,
            superseded_by_transcript_id INTEGER
        );
        CREATE TABLE transcript_segments (
            id INTEGER PRIMARY KEY, transcript_id INTEGER, seq INTEGER,
            speaker TEXT, speaker_role TEXT, time_code_start TEXT,
            time_code_end TEXT, text TEXT
        );
        """
    )
    path = tmp_path / "NU_Q1_2026.txt"
    snapshot = _body("SNAPSHOT").encode()
    path.write_bytes(_body("MUTATED").encode())
    result = ingest_one(
        conn,
        file_path=path,
        project_root=tmp_path,
        tracked_tickers=frozenset({"NU"}),
        snapshot_bytes=snapshot,
    )
    assert result is not None
    import hashlib

    assert (
        conn.execute("SELECT sha256 FROM documents").fetchone()[0]
        == hashlib.sha256(snapshot).hexdigest()
    )
    text = " ".join(row[0] for row in conn.execute("SELECT text FROM transcript_segments"))
    assert "SNAPSHOT" in text
    assert "MUTATED" not in text


@pytest.mark.parametrize("source_state", ["mismatch", "missing"])
def test_existing_ir_document_requires_exact_immutable_receipt(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    source_state: str,
) -> None:
    mod = _load_module()
    repo_root = tmp_path / "repo"
    ir_root = repo_root / "ir_documents"
    path = ir_root / "NU" / "2026-03-31" / "ir_transcript__receipt.txt"
    path.parent.mkdir(parents=True)
    if source_state == "mismatch":
        path.write_text(_body("BYTES_B"), encoding="utf-8")
    db_path = migrated_db(repo_root / "data" / "portfolio.db")
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO tracked_companies (ticker, name, list_type) "
            "VALUES ('NU', 'Nu Holdings', 'portfolio')"
        )
        conn.execute(
            "INSERT INTO documents "
            "(ticker, source_type, doc_type, period_end, file_path, sha256, fetched_at, "
            "fetch_status, raw_bytes_size) VALUES "
            "('NU', 'ir_doc', 'ir_transcript', '2026-03-31', ?, ?, "
            "'2026-04-01T00:00:00', 'ok', 1)",
            (str(path.relative_to(repo_root)).replace("\\", "/"), "a" * 64),
        )
        conn.commit()

    monkeypatch.setattr(mod, "PROJECT_ROOT", repo_root)
    monkeypatch.setattr(
        mod,
        "_TRANSCRIPT_DIRS",
        (repo_root / "transcripts" / "processed", repo_root / "transcripts" / "raw"),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "ingest_transcripts.py",
            "--db",
            str(db_path),
            "--ticker",
            "NU",
            "--include-ir-transcripts",
            "--no-promote",
            "--force",
        ],
    )

    assert mod.main() == 2
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM transcripts").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM transcript_segments").fetchone()[0] == 0
