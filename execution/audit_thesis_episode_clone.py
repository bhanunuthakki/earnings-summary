"""Emit a source-bound, read-only receipt for the thesis episode migration.

The source must be a verified ``sqlite_snapshot`` artifact.  The migrated
database must be a distinct clone of that artifact.  Neither database is ever
opened with write capability.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from alembic.config import Config
from alembic.script import ScriptDirectory
from pydantic import BaseModel, ConfigDict, Field, JsonValue

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from provenance.verifier_identity import verifier_source_artifact_sha256  # noqa: E402
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite  # noqa: E402
from sqlite_snapshot import SnapshotManifest  # noqa: E402

_SCHEMA_VERSION = "thesis-episode-clone-migration-receipt/v1"
_TARGET_REVISION = "0014_add_thesis_evaluation_episodes"


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class DatabaseEvidence(_FrozenModel):
    requested_path: str
    resolved_path: str
    byte_size: int = Field(ge=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    alembic_revision: str = Field(min_length=1)


class SourceSnapshotBinding(_FrozenModel):
    manifest_path: str
    source_database_path: str
    source_database_byte_size: int = Field(ge=0)
    source_database_mtime_ns: int = Field(ge=0)
    source_database_revision: str = Field(min_length=1)
    snapshot: DatabaseEvidence


class EpisodeEvidence(_FrozenModel):
    episode_id: str = Field(min_length=1)
    fingerprint_policy_version: str = Field(min_length=1)
    provenance_completeness: str = Field(min_length=1)
    occurrence_count: int = Field(ge=1)


class CloneMigrationReceipt(_FrozenModel):
    schema_version: Literal["thesis-episode-clone-migration-receipt/v1"] = _SCHEMA_VERSION
    observed_at: datetime
    evidence_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    ticker: str = Field(min_length=1, max_length=16)
    source: SourceSnapshotBinding
    migrated_clone: DatabaseEvidence
    target_revision: str = _TARGET_REVISION
    source_is_target_ancestor: bool
    source_raw_row_count: int = Field(ge=0)
    clone_raw_row_count: int = Field(ge=0)
    source_raw_rows_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    clone_raw_rows_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    raw_rows_unchanged: bool
    legacy_episode_count: int = Field(ge=0)
    legacy_episodes: tuple[EpisodeEvidence, ...]
    membership_count: int = Field(ge=0)
    distinct_membership_count: int = Field(ge=0)
    every_source_row_mapped_once: bool
    verifier_source_artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    verified: bool
    blocking_reasons: tuple[str, ...]
    point_in_time: Literal[True] = True
    authorizes_live_database_change: Literal[False] = False


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _revision(connection: sqlite3.Connection) -> str:
    rows = connection.execute(
        "SELECT version_num FROM alembic_version ORDER BY version_num"
    ).fetchall()
    if len(rows) != 1 or not isinstance(rows[0][0], str) or not rows[0][0]:
        raise ValueError("database must contain exactly one non-empty alembic revision")
    return str(rows[0][0])


def _database_evidence(requested: Path, connection: sqlite3.Connection) -> DatabaseEvidence:
    resolved = requested.expanduser().resolve()
    stat = resolved.stat()
    return DatabaseEvidence(
        requested_path=os.fspath(requested),
        resolved_path=os.fspath(resolved),
        byte_size=stat.st_size,
        sha256=_sha256_file(resolved),
        alembic_revision=_revision(connection),
    )


def _sqlite_value(value: object) -> JsonValue:
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("raw thesis history contains a non-finite float")
        return value
    if isinstance(value, bytes):
        return {
            "blob_sha256": hashlib.sha256(value).hexdigest(),
            "byte_size": len(value),
        }
    raise ValueError(f"unsupported SQLite value type: {type(value).__name__}")


def _raw_rows(connection: sqlite3.Connection, ticker: str) -> tuple[tuple[int, str], ...]:
    columns = tuple(
        str(row[1]) for row in connection.execute("PRAGMA table_info(thesis_evaluations)")
    )
    if "id" not in columns or "ticker" not in columns:
        raise ValueError("thesis_evaluations does not expose required id/ticker columns")
    quoted = ",".join(f'"{column}"' for column in columns)
    rows = connection.execute(
        f"SELECT {quoted} FROM thesis_evaluations WHERE UPPER(ticker)=? ORDER BY id",
        (ticker,),
    ).fetchall()
    id_index = columns.index("id")
    evidence: list[tuple[int, str]] = []
    for row in rows:
        row_id = row[id_index]
        if not isinstance(row_id, int):
            raise ValueError("thesis_evaluations.id must be an integer")
        payload: dict[str, JsonValue] = {
            "columns": list(columns),
            "values": [_sqlite_value(value) for value in row],
        }
        canonical = json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        evidence.append((row_id, hashlib.sha256(canonical.encode("utf-8")).hexdigest()))
    return tuple(evidence)


def _row_set_sha256(rows: tuple[tuple[int, str], ...]) -> str:
    canonical = json.dumps(rows, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _source_is_ancestor(source_revision: str) -> bool:
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    script = ScriptDirectory.from_config(config)
    return source_revision in {
        str(item.revision) for item in script.iterate_revisions(_TARGET_REVISION, "base")
    }


def _episode_evidence(connection: sqlite3.Connection, ticker: str) -> tuple[EpisodeEvidence, ...]:
    rows = connection.execute(
        "SELECT episode_id,fingerprint_policy_version,provenance_completeness,"
        "duplicate_run_count+1 AS occurrence_count "
        "FROM thesis_evaluation_episodes "
        "WHERE UPPER(ticker)=? AND fingerprint_policy_version='legacy_v0' "
        "ORDER BY episode_id",
        (ticker,),
    ).fetchall()
    return tuple(
        EpisodeEvidence(
            episode_id=str(row[0]),
            fingerprint_policy_version=str(row[1]),
            provenance_completeness=str(row[2]),
            occurrence_count=int(row[3]),
        )
        for row in rows
    )


def _receipt_id(payload: object) -> str:
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def audit_clone_migration(
    *,
    source_snapshot_manifest: Path,
    migrated_clone_db: Path,
    ticker: str = "WIX",
    expected_raw_rows: int = 34,
    expected_legacy_episodes: int = 2,
) -> CloneMigrationReceipt:
    """Verify one migrated clone against its immutable source snapshot."""

    manifest_path = source_snapshot_manifest.expanduser().resolve()
    manifest = SnapshotManifest.model_validate_json(manifest_path.read_text(encoding="utf-8"))
    source_snapshot_path = Path(manifest.snapshot.path).expanduser().resolve()
    clone_path = migrated_clone_db.expanduser().resolve()
    if not source_snapshot_path.is_file() or not clone_path.is_file():
        raise FileNotFoundError("source snapshot and migrated clone must both exist")
    if os.path.samefile(source_snapshot_path, clone_path):
        raise ValueError("migrated clone must be a distinct file from the source snapshot")
    original_source_path = Path(manifest.source.path).expanduser().resolve()
    if clone_path == original_source_path:
        raise ValueError("migrated clone must not be the live source database")
    if _sha256_file(source_snapshot_path) != manifest.snapshot.sha256:
        raise ValueError("source snapshot checksum no longer matches its manifest")
    if source_snapshot_path.stat().st_size != manifest.snapshot.byte_size:
        raise ValueError("source snapshot size no longer matches its manifest")

    source_connection = connect_sqlite(
        source_snapshot_path, role=SQLiteConnectionRole.QUIESCED_IMMUTABLE_READ_ONLY
    )
    clone_connection = connect_sqlite(clone_path, role=SQLiteConnectionRole.READ_ONLY)
    try:
        source = SourceSnapshotBinding(
            manifest_path=os.fspath(manifest_path),
            source_database_path=manifest.source.path,
            source_database_byte_size=manifest.source.byte_size,
            source_database_mtime_ns=manifest.source.mtime_ns,
            source_database_revision=manifest.source.alembic_revision,
            snapshot=_database_evidence(source_snapshot_path, source_connection),
        )
        clone = _database_evidence(migrated_clone_db, clone_connection)
        source_rows = _raw_rows(source_connection, ticker.upper())
        clone_rows = _raw_rows(clone_connection, ticker.upper())
        episodes = _episode_evidence(clone_connection, ticker.upper())
        membership_rows = clone_connection.execute(
            "SELECT member.evaluation_id "
            "FROM thesis_evaluation_episode_members AS member "
            "JOIN thesis_evaluation_episodes AS episode "
            "ON episode.episode_id=member.episode_id "
            "WHERE UPPER(episode.ticker)=? ORDER BY member.evaluation_id",
            (ticker.upper(),),
        ).fetchall()
    finally:
        source_connection.close()
        clone_connection.close()

    member_ids = tuple(int(row[0]) for row in membership_rows)
    source_ids = tuple(row_id for row_id, _digest in source_rows)
    raw_rows_unchanged = source_rows == clone_rows
    mapped_once = (
        len(member_ids) == len(source_ids)
        and len(set(member_ids)) == len(member_ids)
        and tuple(sorted(member_ids)) == source_ids
    )
    source_is_ancestor = _source_is_ancestor(source.snapshot.alembic_revision)
    reasons: list[str] = []
    if source.snapshot.alembic_revision != manifest.source.alembic_revision:
        reasons.append("snapshot revision does not match its source manifest")
    if not source_is_ancestor:
        reasons.append("source revision is not an ancestor of the target migration")
    if clone.alembic_revision != _TARGET_REVISION:
        reasons.append("migrated clone is not at the target revision")
    if len(source_rows) != expected_raw_rows:
        reasons.append("source raw row count does not match expectation")
    if len(clone_rows) != expected_raw_rows:
        reasons.append("clone raw row count does not match expectation")
    if not raw_rows_unchanged:
        reasons.append("raw thesis evaluation rows changed during clone migration")
    if len(episodes) != expected_legacy_episodes:
        reasons.append("legacy episode count does not match expectation")
    if any(episode.provenance_completeness != "partial" for episode in episodes):
        reasons.append("legacy episode provenance is not explicitly partial")
    if sum(episode.occurrence_count for episode in episodes) != expected_raw_rows:
        reasons.append("legacy episode occurrence counts do not cover all source rows")
    if not mapped_once:
        reasons.append("source rows are not mapped to exactly one legacy episode")

    verifier_sha = verifier_source_artifact_sha256(
        {
            "execution/audit_thesis_episode_clone.py": Path(__file__),
            "alembic/versions/0014_add_thesis_evaluation_episodes.py": (
                PROJECT_ROOT / "alembic/versions/0014_add_thesis_evaluation_episodes.py"
            ),
        }
    )
    payload: dict[str, object] = {
        "schema_version": _SCHEMA_VERSION,
        "observed_at": datetime.now(UTC).isoformat(),
        "ticker": ticker.upper(),
        "source": source.model_dump(mode="json"),
        "migrated_clone": clone.model_dump(mode="json"),
        "target_revision": _TARGET_REVISION,
        "source_is_target_ancestor": source_is_ancestor,
        "source_raw_row_count": len(source_rows),
        "clone_raw_row_count": len(clone_rows),
        "source_raw_rows_sha256": _row_set_sha256(source_rows),
        "clone_raw_rows_sha256": _row_set_sha256(clone_rows),
        "raw_rows_unchanged": raw_rows_unchanged,
        "legacy_episode_count": len(episodes),
        "legacy_episodes": [episode.model_dump(mode="json") for episode in episodes],
        "membership_count": len(member_ids),
        "distinct_membership_count": len(set(member_ids)),
        "every_source_row_mapped_once": mapped_once,
        "verifier_source_artifact_sha256": verifier_sha,
        "verified": not reasons,
        "blocking_reasons": reasons,
        "point_in_time": True,
        "authorizes_live_database_change": False,
    }
    return CloneMigrationReceipt.model_validate({**payload, "evidence_id": _receipt_id(payload)})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read-only source-bound audit of the thesis episode clone migration"
    )
    parser.add_argument("--source-snapshot-manifest", type=Path, required=True)
    parser.add_argument("--migrated-clone-db", type=Path, required=True)
    parser.add_argument("--ticker", default="WIX")
    parser.add_argument("--expected-raw-rows", type=int, default=34)
    parser.add_argument("--expected-legacy-episodes", type=int, default=2)
    args = parser.parse_args(argv)
    print(
        json.dumps(
            {
                "event": "thesis_episode_clone_audit_started",
                "source_snapshot_manifest": os.fspath(args.source_snapshot_manifest),
                "migrated_clone_db": os.fspath(args.migrated_clone_db),
                "ticker": args.ticker.upper(),
            },
            sort_keys=True,
        ),
        file=sys.stderr,
    )
    receipt = audit_clone_migration(
        source_snapshot_manifest=args.source_snapshot_manifest,
        migrated_clone_db=args.migrated_clone_db,
        ticker=args.ticker,
        expected_raw_rows=args.expected_raw_rows,
        expected_legacy_episodes=args.expected_legacy_episodes,
    )
    print(receipt.model_dump_json())
    print(
        json.dumps(
            {
                "event": "thesis_episode_clone_audit_finished",
                "evidence_id": receipt.evidence_id,
                "verified": receipt.verified,
                "blocking_reason_count": len(receipt.blocking_reasons),
            },
            sort_keys=True,
        ),
        file=sys.stderr,
    )
    return 0 if receipt.verified else 1


if __name__ == "__main__":
    raise SystemExit(main())
