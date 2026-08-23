"""Regression tests for the sealed, read-only issuer document inventory capture CLI."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable
from copy import deepcopy
from datetime import date
from pathlib import Path

import pytest

from execution import capture_issuer_document_inventory as cli
from pipeline import issuer_document_inventory as inventory
from pipeline.issuer_document_inventory import (
    ExpectedIssuerDocument,
    IssuerDocumentInventoryReceipt,
    IssuerDocumentInventoryRequest,
)
from provenance.verifier_identity import verifier_source_artifact_sha256
from sqlite_runtime import SQLiteConnectionRole


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _sidecars(db_path: Path) -> tuple[bool, bool, bool]:
    return (
        (db_path.parent / f"{db_path.name}-wal").exists(),
        (db_path.parent / f"{db_path.name}-shm").exists(),
        (db_path.parent / f"{db_path.name}-journal").exists(),
    )


def _document_count(db_path: Path) -> int:
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute("SELECT COUNT(*) FROM documents").fetchone()
    finally:
        conn.close()
    assert row is not None
    return int(row[0])


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / ".tmp").mkdir(parents=True)
    return repo


def _seed_document(
    db_path: Path,
    repo: Path,
    *,
    document_id: int,
    document_type: str,
    payload: bytes,
    ticker: str = "MELI",
    period_end: str = "2026-06-30",
    file_path: str | None = None,
    source_url: str = "https://investor.example.test/meli/document.pdf",
) -> None:
    relative = file_path or f"ir_documents/MELI/{document_type}-{document_id}.pdf"
    document_path = repo / relative
    document_path.parent.mkdir(parents=True, exist_ok=True)
    if file_path is None:
        document_path.write_bytes(payload)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO documents "
            "(id,ticker,source_type,doc_type,period_end,file_path,sha256,fetched_at,"
            "fetch_status,raw_bytes_size,source_url) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                document_id,
                ticker,
                "ir_doc",
                document_type,
                period_end,
                relative,
                hashlib.sha256(payload).hexdigest(),
                "2026-08-05T00:00:00Z",
                "ok",
                len(payload),
                source_url,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _request(repo: Path, *documents: tuple[str, str]) -> Path:
    request = IssuerDocumentInventoryRequest(
        ticker="MELI",
        fiscal_year=2026,
        fiscal_quarter=2,
        period_end=date(2026, 6, 30),
        expected_documents=tuple(
            ExpectedIssuerDocument(source_url=url, document_type=document_type)
            for url, document_type in sorted(documents)
        ),
    )
    path = repo / "request.json"
    path.write_text(request.canonical_json, encoding="utf-8")
    return path


def _run(db: Path, repo: Path, output: Path, request: Path) -> int:
    return cli.main(
        [
            "--db",
            str(db),
            "--repo-root",
            str(repo),
            "--request",
            str(request),
            "--output",
            str(output),
        ]
    )


def _reason(capsys: pytest.CaptureFixture[str]) -> str:
    return str(json.loads(capsys.readouterr().err.strip().splitlines()[-1])["reason_code"])


def _resign_receipt(payload: dict[str, object]) -> None:
    unsigned = {key: value for key, value in payload.items() if key != "receipt_sha256"}
    canonical = json.dumps(unsigned, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    payload["receipt_sha256"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def test_read_only_inventory_seals_sorted_byte_verified_records_without_db_side_effects(
    migrated_db: Callable[..., Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = migrated_db(tmp_path / "portfolio.db")
    repo = _repo(tmp_path)
    _seed_document(
        db_path,
        repo,
        document_id=22,
        document_type="ir_transcript",
        payload=b"transcript",
    )
    _seed_document(
        db_path,
        repo,
        document_id=11,
        document_type="ir_presentation",
        payload=b"presentation",
    )
    requests: list[tuple[SQLiteConnectionRole, bool | None]] = []
    real_connect = cli.connect_sqlite

    def observed_connect(
        path: Path,
        *,
        role: SQLiteConnectionRole,
        schema_preflight: bool | None = None,
    ) -> sqlite3.Connection:
        requests.append((role, schema_preflight))
        return real_connect(path, role=role, schema_preflight=schema_preflight)

    monkeypatch.setattr(cli, "connect_sqlite", observed_connect)
    output = repo / ".tmp" / "inventory.json"

    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "UPDATE documents SET source_url = ? WHERE id = 22",
            ("https://investor.example.test/meli/transcript.pdf",),
        )
        conn.commit()
    finally:
        conn.close()
    request = _request(
        repo,
        ("https://investor.example.test/meli/document.pdf", "ir_presentation"),
        ("https://investor.example.test/meli/transcript.pdf", "ir_transcript"),
    )
    before_sha256 = _sha256(db_path)
    before_count = _document_count(db_path)
    before_sidecars = _sidecars(db_path)

    assert _run(db_path, repo, output, request) == 0

    result = json.loads(capsys.readouterr().out)
    receipt = IssuerDocumentInventoryReceipt.model_validate_json(output.read_text(encoding="utf-8"))
    assert requests == [(SQLiteConnectionRole.READ_ONLY, True)]
    assert result["receipt_sha256"] == receipt.receipt_sha256
    assert receipt.request_sha256 == receipt.request.request_sha256
    assert receipt.document_set_sha256
    assert receipt.verifier_code_sha256
    assert receipt.verifier_code_sha256 == verifier_source_artifact_sha256(
        {
            "execution/capture_issuer_document_inventory.py": Path(cli.__file__),
            "src/pipeline/issuer_document_inventory.py": Path(__file__).parents[1]
            / "src"
            / "pipeline"
            / "issuer_document_inventory.py",
        }
    )
    assert [record.document_type for record in receipt.records] == [
        "ir_presentation",
        "ir_transcript",
    ]
    assert [record.local_path for record in receipt.records] == [
        "ir_documents/MELI/ir_presentation-11.pdf",
        "ir_documents/MELI/ir_transcript-22.pdf",
    ]
    assert _sha256(db_path) == before_sha256
    assert _document_count(db_path) == before_count
    assert _sidecars(db_path) == before_sidecars == (False, False, False)


def test_replay_is_identical_and_conflicting_output_is_preserved(
    migrated_db: Callable[..., Path], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db_path = migrated_db(tmp_path / "portfolio.db")
    repo = _repo(tmp_path)
    _seed_document(
        db_path,
        repo,
        document_id=1,
        document_type="ir_presentation",
        payload=b"presentation",
    )
    request = _request(repo, ("https://investor.example.test/meli/document.pdf", "ir_presentation"))
    output = repo / ".tmp" / "inventory.json"
    assert _run(db_path, repo, output, request) == 0
    first = output.read_bytes()
    capsys.readouterr()
    assert _run(db_path, repo, output, request) == 0
    assert json.loads(capsys.readouterr().out)["replayed"] is True
    assert output.read_bytes() == first

    output.write_text('{"conflict":true}\n', encoding="utf-8")
    assert _run(db_path, repo, output, request) == 2
    assert _reason(capsys) == "output_conflict"
    assert output.read_text(encoding="utf-8") == '{"conflict":true}\n'


def test_receipt_rejects_resigned_storage_binding_tampering(
    migrated_db: Callable[..., Path], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db_path = migrated_db(tmp_path / "portfolio.db")
    repo = _repo(tmp_path)
    _seed_document(
        db_path,
        repo,
        document_id=1,
        document_type="ir_presentation",
        payload=b"presentation",
    )
    request = _request(repo, ("https://investor.example.test/meli/document.pdf", "ir_presentation"))
    output = repo / ".tmp" / "inventory.json"
    assert _run(db_path, repo, output, request) == 0
    capsys.readouterr()
    payload = json.loads(output.read_text(encoding="utf-8"))

    absent_member_with_identity = deepcopy(payload)
    absent_member_with_identity["database"]["storage_members"][1]["sha256"] = "a" * 64

    noncanonical_member_order = deepcopy(payload)
    noncanonical_member_order["database"]["storage_members"].reverse()

    mismatched_main_sha256 = deepcopy(payload)
    mismatched_main_sha256["database"]["sha256"] = "a" * 64

    mismatched_main_size = deepcopy(payload)
    mismatched_main_size["database"]["byte_size"] += 1

    mismatched_bundle = deepcopy(payload)
    mismatched_bundle["database"]["storage_bundle_sha256"] = "a" * 64

    cases = (
        (absent_member_with_identity, "absent storage member"),
        (noncanonical_member_order, "ordered main then WAL"),
        (mismatched_main_sha256, "legacy identity"),
        (mismatched_main_size, "legacy identity"),
        (mismatched_bundle, "storage_bundle_sha256"),
    )
    for tampered, message in cases:
        _resign_receipt(tampered)
        with pytest.raises(ValueError, match=message):
            IssuerDocumentInventoryReceipt.model_validate(tampered)


def test_closed_meli_q2_request_binds_the_five_expected_document_classes(
    migrated_db: Callable[..., Path], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db_path = migrated_db(tmp_path / "portfolio.db")
    repo = _repo(tmp_path)
    expected = (
        ("letter", "ir_investor_update"),
        ("presentation", "ir_presentation"),
        ("10q", "sec_10q"),
        ("transcript", "ir_transcript"),
        ("press-release", "ir_press_release"),
    )
    request_items: list[tuple[str, str]] = []
    for document_id, (slug, document_type) in enumerate(expected, start=1):
        source_url = f"https://investor.example.test/meli/{slug}.pdf"
        _seed_document(
            db_path,
            repo,
            document_id=document_id,
            document_type=document_type,
            payload=f"{slug}-bytes".encode(),
            source_url=source_url,
        )
        request_items.append((source_url, document_type))
    request = _request(repo, *request_items)
    output = repo / ".tmp" / "inventory.json"

    assert _run(db_path, repo, output, request) == 0
    capsys.readouterr()
    receipt = IssuerDocumentInventoryReceipt.model_validate_json(output.read_text(encoding="utf-8"))
    assert {
        (record.source_url.rsplit("/", 1)[-1], record.document_type) for record in receipt.records
    } == {
        ("letter.pdf", "ir_investor_update"),
        ("presentation.pdf", "ir_presentation"),
        ("10q.pdf", "sec_10q"),
        ("transcript.pdf", "ir_transcript"),
        ("press-release.pdf", "ir_press_release"),
    }


@pytest.mark.parametrize(
    ("mutation", "expected_reason"),
    [
        ("missing", "missing_local_file"),
        ("hash", "document_hash_mismatch"),
        ("size", "byte_size_mismatch"),
        ("traversal", "unsafe_local_path"),
        ("status", "invalid_fetch_status"),
        ("fetched_at", "invalid_fetched_at"),
    ],
)
def test_inventory_fails_closed_on_unbound_document_bytes_or_provenance(
    migrated_db: Callable[..., Path],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    mutation: str,
    expected_reason: str,
) -> None:
    db_path = migrated_db(tmp_path / f"{mutation}.db")
    repo = _repo(tmp_path)
    _seed_document(
        db_path,
        repo,
        document_id=1,
        document_type="ir_presentation",
        payload=b"presentation",
    )
    request = _request(repo, ("https://investor.example.test/meli/document.pdf", "ir_presentation"))
    local = repo / "ir_documents/MELI/ir_presentation-1.pdf"
    conn = sqlite3.connect(db_path)
    try:
        if mutation == "missing":
            local.unlink()
        elif mutation == "hash":
            local.write_bytes(b"Presentation")
        elif mutation == "size":
            conn.execute("UPDATE documents SET raw_bytes_size = raw_bytes_size + 1 WHERE id = 1")
        elif mutation == "traversal":
            conn.execute("UPDATE documents SET file_path = '../outside.pdf' WHERE id = 1")
        elif mutation == "status":
            conn.execute("UPDATE documents SET fetch_status = 'http_error' WHERE id = 1")
        elif mutation == "fetched_at":
            conn.execute("UPDATE documents SET fetched_at = 'not-a-timestamp' WHERE id = 1")
        conn.commit()
    finally:
        conn.close()

    assert _run(db_path, repo, repo / ".tmp" / "inventory.json", request) == 2
    assert _reason(capsys) == expected_reason


def test_inventory_rejects_duplicate_url_and_wrong_source_metadata(
    migrated_db: Callable[..., Path], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db_path = migrated_db(tmp_path / "portfolio.db")
    repo = _repo(tmp_path)
    _seed_document(
        db_path,
        repo,
        document_id=1,
        document_type="ir_presentation",
        payload=b"presentation-one",
    )
    request = _request(repo, ("https://investor.example.test/meli/document.pdf", "ir_presentation"))
    _seed_document(
        db_path,
        repo,
        document_id=2,
        document_type="ir_transcript",
        payload=b"presentation-two",
        source_url="https://investor.example.test/meli/document.pdf",
    )
    assert _run(db_path, repo, repo / ".tmp" / "inventory.json", request) == 2
    assert _reason(capsys) == "duplicate_document_url"

    conn = sqlite3.connect(db_path)
    try:
        conn.execute("DELETE FROM documents WHERE id = 2")
        conn.execute("UPDATE documents SET source_type='fmp' WHERE id = 1")
        conn.commit()
    finally:
        conn.close()
    assert _run(db_path, repo, repo / ".tmp" / "inventory.json", request) == 2
    assert _reason(capsys) == "wrong_source_type"


def test_inventory_rejects_output_escape_and_symlink(
    migrated_db: Callable[..., Path], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db_path = migrated_db(tmp_path / "portfolio.db")
    repo = _repo(tmp_path)
    _seed_document(
        db_path,
        repo,
        document_id=1,
        document_type="ir_presentation",
        payload=b"presentation",
    )
    request = _request(repo, ("https://investor.example.test/meli/document.pdf", "ir_presentation"))
    outside = tmp_path / "outside.json"
    assert _run(db_path, repo, outside, request) == 2
    assert _reason(capsys) == "output_outside_tmp"

    target = repo / ".tmp" / "target.json"
    target.write_text("target", encoding="utf-8")
    link = repo / ".tmp" / "inventory.json"
    link.symlink_to(target)
    assert _run(db_path, repo, link, request) == 2
    assert _reason(capsys) == "output_conflict"
    assert target.read_text(encoding="utf-8") == "target"

    linked_directory = repo / ".tmp" / "linked"
    linked_directory.symlink_to(repo / ".tmp", target_is_directory=True)
    assert _run(db_path, repo, linked_directory / "inventory.json", request) == 2
    assert _reason(capsys) == "output_conflict"


def test_inventory_rejects_document_symlink_or_windows_style_reparse_point(
    migrated_db: Callable[..., Path], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db_path = migrated_db(tmp_path / "portfolio.db")
    repo = _repo(tmp_path)
    _seed_document(
        db_path,
        repo,
        document_id=1,
        document_type="ir_presentation",
        payload=b"presentation",
    )
    source = repo / "ir_documents/MELI/ir_presentation-1.pdf"
    target = repo / "target.pdf"
    target.write_bytes(source.read_bytes())
    source.unlink()
    source.symlink_to(target)
    request = _request(repo, ("https://investor.example.test/meli/document.pdf", "ir_presentation"))

    assert _run(db_path, repo, repo / ".tmp" / "inventory.json", request) == 2
    assert _reason(capsys) == "unsafe_local_path"


def test_inventory_rejects_symlinked_parent_component(
    migrated_db: Callable[..., Path], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db_path = migrated_db(tmp_path / "portfolio.db")
    repo = _repo(tmp_path)
    _seed_document(
        db_path,
        repo,
        document_id=1,
        document_type="ir_presentation",
        payload=b"presentation",
    )
    source = repo / "ir_documents/MELI/ir_presentation-1.pdf"
    target_dir = repo / "inside-repo-target"
    target_dir.mkdir()
    (target_dir / source.name).write_bytes(source.read_bytes())
    source.unlink()
    source.parent.rmdir()
    source.parent.symlink_to(target_dir, target_is_directory=True)
    request = _request(repo, ("https://investor.example.test/meli/document.pdf", "ir_presentation"))

    assert _run(db_path, repo, repo / ".tmp" / "inventory.json", request) == 2
    assert _reason(capsys) == "unsafe_local_path"


def test_schema_drift_emits_stable_failure_without_database_side_effects(
    migrated_db: Callable[..., Path], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db_path = migrated_db(tmp_path / "portfolio.db")
    repo = _repo(tmp_path)
    _seed_document(
        db_path,
        repo,
        document_id=1,
        document_type="ir_presentation",
        payload=b"presentation",
    )
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("UPDATE alembic_version SET version_num = 'mismatched-schema'")
        conn.commit()
    finally:
        conn.close()
    before_sha256 = _sha256(db_path)
    before_count = _document_count(db_path)
    before_sidecars = _sidecars(db_path)
    request = _request(repo, ("https://investor.example.test/meli/document.pdf", "ir_presentation"))

    assert _run(db_path, repo, repo / ".tmp" / "inventory.json", request) == 2
    captured = capsys.readouterr()
    assert json.loads(captured.err.strip().splitlines()[-1])["reason_code"] == "schema_drift"
    assert "Traceback" not in captured.err
    assert _sha256(db_path) == before_sha256
    assert _document_count(db_path) == before_count
    assert _sidecars(db_path) == before_sidecars == (False, False, False)


def test_live_wal_commits_produce_distinct_storage_bundle_bindings(
    migrated_db: Callable[..., Path], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db_path = migrated_db(tmp_path / "portfolio.db")
    repo = _repo(tmp_path)
    _seed_document(
        db_path,
        repo,
        document_id=1,
        document_type="ir_presentation",
        payload=b"aug-five",
    )
    request = _request(repo, ("https://investor.example.test/meli/document.pdf", "ir_presentation"))
    writer = sqlite3.connect(db_path)
    try:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
        writer.execute("CREATE TABLE wal_marker (value TEXT NOT NULL)")
        writer.execute("INSERT INTO wal_marker VALUES ('2026-08-05')")
        writer.commit()
        first = repo / ".tmp" / "aug-five.json"
        assert _run(db_path, repo, first, request) == 0
        capsys.readouterr()
        first_receipt = IssuerDocumentInventoryReceipt.model_validate_json(
            first.read_text(encoding="utf-8")
        )

        writer.execute("INSERT INTO wal_marker VALUES ('2026-08-06')")
        writer.commit()
        second = repo / ".tmp" / "aug-six.json"
        assert _run(db_path, repo, second, request) == 0
        capsys.readouterr()
        second_receipt = IssuerDocumentInventoryReceipt.model_validate_json(
            second.read_text(encoding="utf-8")
        )
    finally:
        writer.close()

    assert (
        first_receipt.database.storage_bundle_sha256
        != second_receipt.database.storage_bundle_sha256
    )
    assert first_receipt.database.storage_members[1].present is True
    assert second_receipt.database.storage_members[1].present is True


def test_concurrent_wal_commit_during_multi_document_capture_fails_closed(
    migrated_db: Callable[..., Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = migrated_db(tmp_path / "portfolio.db")
    repo = _repo(tmp_path)
    first_url = "https://investor.example.test/meli/a.pdf"
    second_url = "https://investor.example.test/meli/b.pdf"
    _seed_document(
        db_path,
        repo,
        document_id=1,
        document_type="ir_presentation",
        payload=b"first",
        source_url=first_url,
    )
    _seed_document(
        db_path,
        repo,
        document_id=2,
        document_type="ir_transcript",
        payload=b"second",
        source_url=second_url,
    )
    writer = sqlite3.connect(db_path)
    assert writer.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
    writer.commit()
    first_file = repo / "ir_documents/MELI/ir_presentation-1.pdf"
    real_read_bytes = Path.read_bytes
    committed = False

    def interleaved_read_bytes(path: Path) -> bytes:
        nonlocal committed
        result = real_read_bytes(path)
        if path == first_file and not committed:
            committed = True
            writer.execute("CREATE TABLE concurrent_marker (value TEXT NOT NULL)")
            writer.execute("INSERT INTO concurrent_marker VALUES ('committed')")
            writer.commit()
        return result

    monkeypatch.setattr(Path, "read_bytes", interleaved_read_bytes)
    request = _request(repo, (first_url, "ir_presentation"), (second_url, "ir_transcript"))
    try:
        assert _run(db_path, repo, repo / ".tmp" / "inventory.json", request) == 2
    finally:
        writer.close()
    assert _reason(capsys) == "database_changed"
    assert not (repo / ".tmp" / "inventory.json").exists()


def test_reparse_bits_and_blocked_output_parent_fail_with_stable_reasons(
    migrated_db: Callable[..., Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = migrated_db(tmp_path / "portfolio.db")
    repo = _repo(tmp_path)
    _seed_document(
        db_path,
        repo,
        document_id=1,
        document_type="ir_presentation",
        payload=b"presentation",
    )
    request = _request(repo, ("https://investor.example.test/meli/document.pdf", "ir_presentation"))
    source = repo / "ir_documents/MELI/ir_presentation-1.pdf"
    real_lstat = Path.lstat

    class _ReparseStat:
        def __init__(self, wrapped: object) -> None:
            self.st_mode = int(getattr(wrapped, "st_mode"))
            self.st_file_attributes = 8

    def reparse_lstat(path: Path) -> object:
        result = real_lstat(path)
        return _ReparseStat(result) if path == source else result

    monkeypatch.setattr(inventory.stat, "FILE_ATTRIBUTE_REPARSE_POINT", 8, raising=False)
    monkeypatch.setattr(Path, "lstat", reparse_lstat)
    assert _run(db_path, repo, repo / ".tmp" / "inventory.json", request) == 2
    assert _reason(capsys) == "unsafe_local_path"
    monkeypatch.undo()

    tmp_root = repo / ".tmp"

    def output_reparse_lstat(path: Path) -> object:
        result = real_lstat(path)
        return _ReparseStat(result) if path == tmp_root else result

    monkeypatch.setattr(cli.stat, "FILE_ATTRIBUTE_REPARSE_POINT", 8, raising=False)
    monkeypatch.setattr(Path, "lstat", output_reparse_lstat)
    assert _run(db_path, repo, repo / ".tmp" / "inventory.json", request) == 2
    assert _reason(capsys) == "output_conflict"
    monkeypatch.undo()

    blocked = repo / ".tmp" / "blocked-parent"
    blocked.write_text("preserve", encoding="utf-8")
    assert _run(db_path, repo, blocked / "inventory.json", request) == 2
    assert _reason(capsys) == "output_write_failed"
    assert blocked.read_text(encoding="utf-8") == "preserve"


@pytest.mark.parametrize(
    "url", ("https://user@example.test/a.pdf", "https://:secret@example.test/a.pdf")
)
def test_request_rejects_any_url_credentials(url: str) -> None:
    with pytest.raises(ValueError, match="credential-free"):
        IssuerDocumentInventoryRequest(
            ticker="MELI",
            fiscal_year=2026,
            fiscal_quarter=2,
            period_end=date(2026, 6, 30),
            expected_documents=(
                ExpectedIssuerDocument(source_url=url, document_type="ir_presentation"),
            ),
        )
