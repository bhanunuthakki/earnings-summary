# pyright: reportPrivateUsage=false
"""Tested backup-restore drill (sre-3, 2026-06-18 hardening refresh).

A backup you have never restored is not a backup. Exercises cron/restore_db.py
directly (round-trip, refuse-overwrite, force, corrupt-snapshot rejection) and
the full backup_db -> restore_db path end-to-end.
"""

from __future__ import annotations

import gzip
import shutil
import sqlite3
import sys
from pathlib import Path

import pytest
from cryptography.exceptions import InvalidTag

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "cron"))
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import backup_db  # noqa: E402
import migrate_legacy_backups  # noqa: E402
import restore_db  # noqa: E402

from runtime.backup_crypto import (  # noqa: E402
    decrypt_file,
    encrypt_file,
    load_key,
    load_or_create_key,
)


def _make_db(path: Path, value: str) -> None:
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("CREATE TABLE t (k TEXT, v TEXT)")
        conn.execute("INSERT INTO t VALUES ('key', ?)", (value,))
        conn.commit()
    finally:
        conn.close()


def _read_value(path: Path) -> str:
    conn = sqlite3.connect(str(path))
    try:
        row = conn.execute("SELECT v FROM t WHERE k = 'key'").fetchone()
        return str(row[0])
    finally:
        conn.close()


def _gzip_file(src: Path, dst: Path) -> None:
    with open(src, "rb") as raw, gzip.open(dst, "wb") as gz:
        shutil.copyfileobj(raw, gz)


def test_restore_round_trip(tmp_path: Path) -> None:
    src = tmp_path / "src.db"
    _make_db(src, "hello")
    snap = tmp_path / "portfolio.db.20260101_000000.gz"
    _gzip_file(src, snap)
    target = tmp_path / "restored.db"
    restore_db.restore_snapshot(snap, target, force=False, allow_legacy=True)
    assert target.exists()
    assert restore_db.integrity_ok(target)
    assert _read_value(target) == "hello"


def test_restore_refuses_overwrite_without_force(tmp_path: Path) -> None:
    src = tmp_path / "src.db"
    _make_db(src, "x")
    snap = tmp_path / "s.gz"
    _gzip_file(src, snap)
    target = tmp_path / "exists.db"
    target.write_bytes(b"keep")
    with pytest.raises(FileExistsError):
        restore_db.restore_snapshot(snap, target, force=False, allow_legacy=True)
    assert target.read_bytes() == b"keep"


def test_restore_force_overwrites(tmp_path: Path) -> None:
    src = tmp_path / "src.db"
    _make_db(src, "new")
    snap = tmp_path / "s.gz"
    _gzip_file(src, snap)
    target = tmp_path / "exists.db"
    target.write_bytes(b"old")
    restore_db.restore_snapshot(snap, target, force=True, allow_legacy=True)
    assert _read_value(target) == "new"


def test_restore_rejects_corrupt_snapshot(tmp_path: Path) -> None:
    snap = tmp_path / "bad.gz"
    with gzip.open(snap, "wb") as gz:
        gz.write(b"this is not a sqlite database")
    target = tmp_path / "out.db"
    with pytest.raises(RuntimeError):
        restore_db.restore_snapshot(snap, target, force=False, allow_legacy=True)
    assert not target.exists()  # the corrupt restore is discarded, target untouched


def test_backup_then_restore_end_to_end(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    live = tmp_path / "live.db"
    _make_db(live, "e2e")
    backup_dir = tmp_path / "backups"
    monkeypatch.setattr(backup_db, "SRC_DB", live)
    monkeypatch.setenv("EARNINGS_SUMMARY_DB_PATH", str(live))
    monkeypatch.setenv("ES_DB_BACKUP_DIR", str(backup_dir))
    monkeypatch.setenv("EARNINGS_SUMMARY_SECRETS_DIR", str(tmp_path / "secrets"))
    accounting = (sqlite3.connect(":memory:"), "backup-test")
    monkeypatch.setattr(backup_db, "_start_accounting", lambda *_args: accounting)
    monkeypatch.setattr(
        backup_db,
        "_finish_accounting",
        lambda _accounting, *, success, error_msg=None: _accounting[0].close(),
    )
    assert backup_db.main() == 0

    snaps = restore_db.list_snapshots(backup_dir)
    assert snaps, "backup_db wrote no snapshot"
    target = tmp_path / "recovered.db"
    rc = restore_db.main(["--latest", "--backup-dir", str(backup_dir), "--to", str(target)])
    assert rc == 0
    assert _read_value(target) == "e2e"
    assert snaps[-1].name.endswith(".gz.enc")
    assert not list(backup_dir.glob("portfolio.db.*.gz"))


def test_encrypted_envelope_round_trip_and_unique_nonce(tmp_path: Path) -> None:
    source = tmp_path / "payload.gz"
    source.write_bytes(b"payload" * 1000)
    key = b"k" * 32
    first = tmp_path / "first.enc"
    second = tmp_path / "second.enc"
    encrypt_file(source, first, key=key)
    encrypt_file(source, second, key=key)

    assert first.read_bytes() != second.read_bytes()
    restored = tmp_path / "restored.gz"
    decrypt_file(first, restored, key=key)
    assert restored.read_bytes() == source.read_bytes()


def test_encrypted_envelope_rejects_wrong_key_and_truncation(tmp_path: Path) -> None:
    source = tmp_path / "payload.gz"
    source.write_bytes(b"payload")
    encrypted = tmp_path / "snapshot.enc"
    encrypt_file(source, encrypted, key=b"a" * 32)

    with pytest.raises(RuntimeError, match="does not match"):
        decrypt_file(encrypted, tmp_path / "wrong.gz", key=b"b" * 32)
    assert not (tmp_path / "wrong.gz").exists()

    encrypted.write_bytes(encrypted.read_bytes()[:10])
    with pytest.raises(RuntimeError, match="truncated"):
        decrypt_file(encrypted, tmp_path / "truncated.gz", key=b"a" * 32)
    assert not (tmp_path / "truncated.gz").exists()


def test_encrypted_envelope_rejects_version_and_non_256_bit_key(tmp_path: Path) -> None:
    source = tmp_path / "payload.gz"
    source.write_bytes(b"payload")
    encrypted = tmp_path / "snapshot.enc"
    with pytest.raises(ValueError, match="256-bit"):
        encrypt_file(source, encrypted, key=b"short")

    encrypt_file(source, encrypted, key=b"a" * 32)
    tampered = encrypted.read_bytes().replace(b'"version":1', b'"version":2', 1)
    encrypted.write_bytes(tampered)
    with pytest.raises(RuntimeError, match="unsupported"):
        decrypt_file(encrypted, tmp_path / "version.gz", key=b"a" * 32)


@pytest.mark.parametrize("position", [0, -1, 40])
def test_encrypted_envelope_tamper_never_publishes_plaintext(tmp_path: Path, position: int) -> None:
    source = tmp_path / "payload.gz"
    source.write_bytes(b"secret payload")
    encrypted = tmp_path / "snapshot.enc"
    key = b"k" * 32
    encrypt_file(source, encrypted, key=key)
    damaged = bytearray(encrypted.read_bytes())
    damaged[position] ^= 1
    encrypted.write_bytes(damaged)
    target = tmp_path / "target.gz"

    with pytest.raises((InvalidTag, RuntimeError)):
        decrypt_file(encrypted, target, key=key)
    assert not target.exists()
    assert not list(tmp_path.glob(".target.gz.*"))


def test_restore_missing_key_does_not_create_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.db"
    _make_db(source, "value")
    plain_gz = tmp_path / "plain.gz"
    _gzip_file(source, plain_gz)
    encrypted = tmp_path / "portfolio.db.20260101_000000.gz.enc"
    encrypt_file(plain_gz, encrypted, key=b"k" * 32)
    key_path = tmp_path / "secrets" / "backup_encryption.key"
    monkeypatch.setenv("ES_DB_BACKUP_KEY_FILE", str(key_path))
    target = tmp_path / "target.db"

    with pytest.raises(RuntimeError, match="key is missing"):
        restore_db.restore_snapshot(encrypted, target, force=False)
    assert not key_path.exists()
    assert not target.exists()


def test_load_key_rejects_noncanonical_base64(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key_path = tmp_path / "bad.key"
    key_path.write_text("%%%%" * 11, encoding="ascii")
    monkeypatch.setenv("ES_DB_BACKUP_KEY_FILE", str(key_path))
    with pytest.raises(RuntimeError, match="cannot read"):
        load_key()


def test_legacy_migration_verifies_before_removing_plaintext(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_db = tmp_path / "source.db"
    _make_db(source_db, "legacy")
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    plaintext = backup_dir / "portfolio.db.20260101_000000.gz"
    _gzip_file(source_db, plaintext)
    monkeypatch.setenv("EARNINGS_SUMMARY_SECRETS_DIR", str(tmp_path / "secrets"))

    dry = migrate_legacy_backups.migrate(backup_dir, apply=False)
    assert dry["candidates"] == 1
    assert plaintext.exists()

    applied = migrate_legacy_backups.migrate(backup_dir, apply=True)
    assert applied["migrated"] == 1
    assert not plaintext.exists()
    encrypted = plaintext.with_name(plaintext.name + ".enc")
    target = tmp_path / "restored.db"
    restore_db.restore_snapshot(encrypted, target, force=False)
    assert _read_value(target) == "legacy"


def test_latest_ignores_newer_plaintext_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    monkeypatch.setenv("EARNINGS_SUMMARY_SECRETS_DIR", str(tmp_path / "secrets"))
    source = tmp_path / "source.db"
    _make_db(source, "trusted")
    plain = tmp_path / "trusted.gz"
    _gzip_file(source, plain)
    encrypted = backup_dir / "portfolio.db.20260101_000000.gz.enc"
    encrypt_file(plain, encrypted, key=load_or_create_key())
    injected = backup_dir / "portfolio.db.20990101_000000.gz"
    _gzip_file(source, injected)

    assert restore_db.list_snapshots(backup_dir) == [encrypted]
    with pytest.raises(RuntimeError, match="legacy migration"):
        restore_db.restore_snapshot(injected, tmp_path / "unsafe.db", force=False)


def _make_archive(live_db: Path) -> Path:
    """Create data/archive/portfolio_gc_archive.db beside *live_db*."""
    archive = live_db.parent / "archive" / backup_db.ARCHIVE_PREFIX
    archive.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(archive))
    try:
        conn.execute("CREATE TABLE gc_manifest (run_at TEXT, rows_archived INTEGER)")
        conn.execute("INSERT INTO gc_manifest VALUES ('r1', 5)")
        conn.commit()
    finally:
        conn.close()
    return archive


def _prime_backup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    live = tmp_path / "live.db"
    _make_db(live, "e2e")
    backup_dir = tmp_path / "backups"
    monkeypatch.setattr(backup_db, "SRC_DB", live)
    monkeypatch.setenv("ES_DB_BACKUP_DIR", str(backup_dir))
    monkeypatch.setenv("EARNINGS_SUMMARY_SECRETS_DIR", str(tmp_path / "secrets"))
    accounting: tuple[sqlite3.Connection, str] = (sqlite3.connect(":memory:"), "backup-test")

    def _start(*_a: object) -> tuple[sqlite3.Connection, str]:
        return accounting

    def _finish(
        acc: tuple[sqlite3.Connection, str], *, success: bool, error_msg: str | None = None
    ) -> None:
        acc[0].close()

    monkeypatch.setattr(backup_db, "_start_accounting", _start)
    monkeypatch.setattr(backup_db, "_finish_accounting", _finish)
    return live, backup_dir


def test_backup_captures_archive_sidecar(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    live, backup_dir = _prime_backup(tmp_path, monkeypatch)
    _make_archive(live)
    assert backup_db.main() == 0
    archive_snaps = sorted(backup_dir.glob(f"{backup_db.ARCHIVE_PREFIX}.*.gz.enc"))
    assert len(archive_snaps) == 1, "archive sidecar was not backed up"
    # It decrypts + gunzips back to a valid, non-empty archive DB.
    recovered = tmp_path / "recovered_archive.db"
    restore_db.restore_snapshot(archive_snaps[-1], recovered, force=False)
    conn = sqlite3.connect(str(recovered))
    try:
        assert conn.execute("SELECT rows_archived FROM gc_manifest").fetchone()[0] == 5
    finally:
        conn.close()


def test_missing_archive_does_not_fail_db_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _live, backup_dir = _prime_backup(tmp_path, monkeypatch)
    # No archive created.
    assert backup_db.main() == 0
    assert restore_db.list_snapshots(backup_dir), "primary DB backup must still run"
    assert not list(backup_dir.glob(f"{backup_db.ARCHIVE_PREFIX}.*.gz.enc"))


def test_unchanged_archive_is_not_re_encrypted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    live, backup_dir = _prime_backup(tmp_path, monkeypatch)
    _make_archive(live)
    assert backup_db.main() == 0
    first = sorted(backup_dir.glob(f"{backup_db.ARCHIVE_PREFIX}.*.gz.enc"))
    assert len(first) == 1
    # A second run with the archive untouched must not write a new snapshot.
    assert backup_db.main() == 0
    second = sorted(backup_dir.glob(f"{backup_db.ARCHIVE_PREFIX}.*.gz.enc"))
    assert second == first


def test_archive_backup_failure_does_not_fail_primary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    live, backup_dir = _prime_backup(tmp_path, monkeypatch)
    _make_archive(live)

    real = backup_db._consistent_snapshot

    def _boom_on_archive(src_db: Path, tmp_path: Path) -> None:
        # _consistent_snapshot is shared with the primary backup; only the
        # archive leg (source under archive/) should fail.
        if src_db.parent.name == "archive":
            raise RuntimeError("simulated archive snapshot failure")
        real(src_db, tmp_path)

    monkeypatch.setattr(backup_db, "_consistent_snapshot", _boom_on_archive)
    # Primary DB backup succeeds; the archive leg swallows its error.
    assert backup_db.main() == 0
    assert not list(backup_dir.glob(f"{backup_db.ARCHIVE_PREFIX}.*.gz.enc"))
