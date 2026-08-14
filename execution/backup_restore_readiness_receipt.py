"""Verify a restored SQLite snapshot and emit a typed Phase-0 receipt.

The input snapshot must be a plain restored SQLite file with the strict
``sqlite-reader-snapshot/v1`` manifest produced by :mod:`sqlite_snapshot`.
This verifier opens both databases read-only. It does not create a backup,
restore an encrypted artifact, or authorize a downstream write.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from provenance.verifier_identity import verifier_source_artifact_sha256  # noqa: E402
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite  # noqa: E402
from sqlite_snapshot import SnapshotManifest  # noqa: E402

_SUPPORTED_SNAPSHOT_SCHEMA_VERSION = "sqlite-reader-snapshot/v1"
_SUPPORTED_SNAPSHOT_CODE_CONFIG_VERSIONS = frozenset({"sqlite-reader-snapshot/v1"})


class BackupRestoreReadinessReceipt(BaseModel):
    """Point-in-time evidence that one restored snapshot matches its source."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["backup-restore-readiness/v1"] = "backup-restore-readiness/v1"
    evidence_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    observed_at: datetime
    source_db_requested_path: str
    source_db_resolved_path: str
    source_db_revision: str | None
    source_db_byte_size: int | None = Field(default=None, ge=0)
    source_db_mtime_ns: int | None = Field(default=None, ge=0)
    snapshot_requested_path: str
    snapshot_resolved_path: str
    snapshot_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    snapshot_byte_size: int | None = Field(default=None, ge=0)
    restored_db_revision: str | None
    integrity_check: tuple[str, ...]
    foreign_key_violation_count: int | None = Field(default=None, ge=0)
    verifier_code_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    verified: bool
    blocking_reasons: tuple[str, ...]
    authorizes_downstream_write: Literal[False] = False
    downstream_locked_revalidation_required: Literal[True] = True


def verifier_code_sha256() -> str:
    return verifier_source_artifact_sha256(
        {
            "execution/backup_restore_readiness_receipt.py": Path(__file__),
            "src/sqlite_snapshot.py": PROJECT_ROOT / "src" / "sqlite_snapshot.py",
        }
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _revision_and_verification(path: Path) -> tuple[str, tuple[str, ...], int]:
    conn = connect_sqlite(path, role=SQLiteConnectionRole.READ_ONLY, schema_preflight=False)
    try:
        revisions = tuple(
            str(row[0])
            for row in conn.execute("SELECT version_num FROM alembic_version ORDER BY version_num")
        )
        if len(revisions) != 1 or not revisions[0]:
            raise ValueError("database revision is not singular")
        integrity = tuple(str(row[0]) for row in conn.execute("PRAGMA integrity_check"))
        foreign_keys = len(conn.execute("PRAGMA foreign_key_check").fetchall())
    finally:
        conn.close()
    return revisions[0], integrity, foreign_keys


def _evidence_id(receipt: BackupRestoreReadinessReceipt) -> str:
    payload = receipt.model_dump(mode="json", exclude={"evidence_id"})
    canonical = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def evidence_id_is_valid(receipt: BackupRestoreReadinessReceipt) -> bool:
    return receipt.evidence_id == _evidence_id(receipt)


def validate_receipt_for_source(
    receipt: BackupRestoreReadinessReceipt,
    *,
    source_db: Path,
    source_revision: str | None,
    require_current_identity: bool = True,
) -> tuple[str, ...]:
    """Revalidate a stored receipt against its source path and snapshot artifact.

    Migration preconditions require the source revision/size/mtime to remain
    exact. Post-migration operational checks retain the pre-migration snapshot
    as rollback evidence, so they validate its immutable artifact and source
    path without pretending the now-upgraded database still has the old bytes.
    """

    reasons: list[str] = []
    source = source_db.resolve()
    if not evidence_id_is_valid(receipt):
        reasons.append("backup_restore_evidence_id_invalid")
    if receipt.verifier_code_sha256 != verifier_code_sha256():
        reasons.append("backup_restore_verifier_code_changed")
    if not receipt.verified or receipt.blocking_reasons:
        reasons.append("backup_restore_not_verified")
    if Path(receipt.source_db_resolved_path).resolve() != source:
        reasons.append("backup_restore_source_path_mismatch")
    if require_current_identity:
        if receipt.source_db_revision != source_revision:
            reasons.append("backup_restore_source_revision_mismatch")
        try:
            source_stat = source.stat()
        except OSError:
            reasons.append("backup_restore_source_unavailable")
        else:
            if (
                receipt.source_db_byte_size != source_stat.st_size
                or receipt.source_db_mtime_ns != source_stat.st_mtime_ns
            ):
                reasons.append("backup_restore_source_identity_stale")

    snapshot = Path(receipt.snapshot_resolved_path)
    try:
        snapshot_stat = snapshot.stat()
    except OSError:
        reasons.append("backup_restore_snapshot_unavailable")
    else:
        if (
            receipt.snapshot_byte_size != snapshot_stat.st_size
            or receipt.snapshot_sha256 != _sha256(snapshot)
        ):
            reasons.append("backup_restore_snapshot_identity_mismatch")
    return tuple(dict.fromkeys(reasons))


def collect_backup_restore_receipt(
    *,
    source_db: Path,
    snapshot_db: Path,
    manifest_path: Path | None = None,
) -> BackupRestoreReadinessReceipt:
    source_requested = source_db
    snapshot_requested = snapshot_db
    source = source_db.expanduser().resolve()
    snapshot = snapshot_db.expanduser().resolve()
    manifest_file = (
        manifest_path.expanduser().resolve()
        if manifest_path is not None
        else snapshot.with_suffix(snapshot.suffix + ".manifest.json")
    )
    reasons: list[str] = []
    source_revision: str | None = None
    source_size: int | None = None
    source_mtime: int | None = None
    snapshot_sha: str | None = None
    snapshot_size: int | None = None
    restored_revision: str | None = None
    integrity: tuple[str, ...] = ()
    foreign_keys: int | None = None
    manifest: SnapshotManifest | None = None

    if source.is_file():
        stat = source.stat()
        source_size = stat.st_size
        source_mtime = stat.st_mtime_ns
        try:
            source_revision, _, _ = _revision_and_verification(source)
        except Exception:
            reasons.append("source_database_unreadable")
    else:
        reasons.append("source_database_missing")

    if snapshot.is_file():
        snapshot_size = snapshot.stat().st_size
        snapshot_sha = _sha256(snapshot)
        try:
            restored_revision, integrity, foreign_keys = _revision_and_verification(snapshot)
        except Exception:
            reasons.append("restored_database_unreadable")
    else:
        reasons.append("restored_database_missing")

    try:
        manifest = SnapshotManifest.model_validate_json(manifest_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        reasons.append("snapshot_manifest_invalid")

    if manifest is not None:
        if manifest.schema_version != _SUPPORTED_SNAPSHOT_SCHEMA_VERSION:
            reasons.append("snapshot_manifest_schema_unsupported")
        if manifest.code_config_version not in _SUPPORTED_SNAPSHOT_CODE_CONFIG_VERSIONS:
            reasons.append("snapshot_manifest_code_unsupported")
        if Path(manifest.source.path).resolve() != source:
            reasons.append("manifest_source_path_mismatch")
        if Path(manifest.snapshot.path).resolve() != snapshot:
            reasons.append("manifest_snapshot_path_mismatch")
        if manifest.source.alembic_revision != source_revision:
            reasons.append("manifest_source_revision_mismatch")
        if manifest.source.byte_size != source_size or manifest.source.mtime_ns != source_mtime:
            reasons.append("source_identity_changed_since_snapshot")
        if manifest.snapshot.sha256 != snapshot_sha or manifest.snapshot.byte_size != snapshot_size:
            reasons.append("snapshot_identity_mismatch")
        if manifest.verification.integrity_check != integrity:
            reasons.append("manifest_integrity_mismatch")
        if len(manifest.verification.foreign_key_check) != foreign_keys:
            reasons.append("manifest_foreign_key_mismatch")

    if source_revision is not None and restored_revision != source_revision:
        reasons.append("restored_revision_mismatch")
    if integrity != ("ok",):
        reasons.append("restored_integrity_failed")
    if foreign_keys != 0:
        reasons.append("restored_foreign_keys_failed")

    blocking_reasons = tuple(dict.fromkeys(reasons))
    draft = BackupRestoreReadinessReceipt(
        evidence_id="0" * 64,
        observed_at=datetime.now(UTC),
        source_db_requested_path=str(source_requested),
        source_db_resolved_path=str(source),
        source_db_revision=source_revision,
        source_db_byte_size=source_size,
        source_db_mtime_ns=source_mtime,
        snapshot_requested_path=str(snapshot_requested),
        snapshot_resolved_path=str(snapshot),
        snapshot_sha256=snapshot_sha,
        snapshot_byte_size=snapshot_size,
        restored_db_revision=restored_revision,
        integrity_check=integrity,
        foreign_key_violation_count=foreign_keys,
        verifier_code_sha256=verifier_code_sha256(),
        verified=not blocking_reasons,
        blocking_reasons=blocking_reasons,
    )
    return draft.model_copy(update={"evidence_id": _evidence_id(draft)})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-db", type=Path, required=True)
    parser.add_argument("--snapshot-db", type=Path, required=True)
    parser.add_argument("--manifest", type=Path)
    args = parser.parse_args(argv)
    receipt = collect_backup_restore_receipt(
        source_db=args.source_db,
        snapshot_db=args.snapshot_db,
        manifest_path=args.manifest,
    )
    print(receipt.model_dump_json(indent=2))
    return 0 if receipt.verified else 1


if __name__ == "__main__":
    raise SystemExit(main())
