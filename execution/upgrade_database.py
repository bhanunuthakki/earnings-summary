"""Safely upgrade fresh, active, or pre-squash earnings-summary databases.

The active Alembic graph is intentionally small (0001 -> 0002 -> 0003), while
pre-squash databases carry a revision from ``alembic/versions_archived``.  A
plain ``alembic upgrade head`` cannot resolve those archived revision IDs.
This command provides the explicit bridge: acquire the shared write lock,
create and verify an online SQLite backup, finish the archived graph, validate
the legacy schema, re-anchor its version metadata at the squashed baseline,
then run the active cleanup/recovery migrations.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from urllib.parse import quote

from alembic.config import Config
from alembic.script import ScriptDirectory
from alembic.util.exc import CommandError
from pydantic import BaseModel, ConfigDict

from alembic import command
from run_lock import hold_run_lock

ACTIVE_BASE = "0001_initial_schema"
ACTIVE_HEAD = "0003_restore_baseline_defaults"

_LEGACY_SCHEMA_REQUIREMENTS: dict[str, frozenset[str]] = {
    "tracked_companies": frozenset({"ticker", "processing_tier"}),
    "documents": frozenset({"id", "ticker"}),
    "llm_calls": frozenset({"purpose", "called_at"}),
    "llm_budgets": frozenset({"purpose", "on_exceed"}),
}


class UpgradeDatabaseError(RuntimeError):
    """The database cannot be upgraded without operator intervention."""


class UpgradeReceipt(BaseModel):
    """Machine-readable result emitted on stdout."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["created", "upgraded", "bridged", "already_current"]
    db_path: str
    from_revision: str | None
    to_revision: str
    backup_path: str | None
    completed_at: str


def _config(repo_root: Path, db_path: Path, *, archived: bool) -> Config:
    config = Config(str(repo_root / "alembic.ini"))
    config.set_main_option("script_location", str(repo_root / "alembic"))
    versions = "versions_archived" if archived else "versions"
    config.set_main_option("version_locations", str(repo_root / "alembic" / versions))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{db_path.as_posix()}")
    return config


def _read_revisions(db_path: Path) -> tuple[str, ...]:
    if not db_path.exists():
        return ()
    uri = f"file:{quote(str(db_path.resolve()).replace(os.sep, '/'), safe='/:')}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        has_version = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='alembic_version'"
        ).fetchone()
        if has_version is None:
            return ()
        return tuple(
            sorted(str(row[0]) for row in conn.execute("SELECT version_num FROM alembic_version"))
        )
    finally:
        conn.close()


def _user_tables(db_path: Path) -> set[str]:
    if not db_path.exists():
        return set()
    conn = sqlite3.connect(str(db_path))
    try:
        return {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%' AND name<>'alembic_version'"
            )
        }
    finally:
        conn.close()


def _integrity_check(db_path: Path) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        result = conn.execute("PRAGMA integrity_check").fetchone()
    finally:
        conn.close()
    if result is None or str(result[0]).lower() != "ok":
        raise UpgradeDatabaseError(f"SQLite integrity_check failed for {db_path}: {result!r}")


def _backup_database(db_path: Path, backup_path: Path) -> None:
    if backup_path.exists():
        raise UpgradeDatabaseError(f"refusing to overwrite backup: {backup_path}")
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = backup_path.with_name(f".{backup_path.name}.{os.getpid()}.tmp")
    source = sqlite3.connect(str(db_path))
    destination = sqlite3.connect(str(temp_path))
    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()
    try:
        _integrity_check(temp_path)
        temp_path.replace(backup_path)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise


def _default_backup_path(db_path: Path) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return db_path.with_name(f"{db_path.stem}.pre-upgrade-{stamp}{db_path.suffix}")


def _validate_legacy_schema(db_path: Path) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        tables = {
            str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        issues: list[str] = []
        for table, required_columns in _LEGACY_SCHEMA_REQUIREMENTS.items():
            if table not in tables:
                issues.append(f"missing_table:{table}")
                continue
            columns = {
                str(row[0])
                for row in conn.execute("SELECT name FROM pragma_table_info(?)", (table,))
            }
            missing = sorted(required_columns - columns)
            if missing:
                issues.append(f"missing_columns:{table}:{','.join(missing)}")
    finally:
        conn.close()
    if issues:
        raise UpgradeDatabaseError(
            "pre-squash schema is incomplete; restore a complete backup before bridging: "
            + ";".join(issues)
        )


def _reanchor_at_active_baseline(db_path: Path, expected_archived_head: str) -> None:
    revisions = _read_revisions(db_path)
    if revisions != (expected_archived_head,):
        raise UpgradeDatabaseError(
            f"archived upgrade did not reach its single head: expected={expected_archived_head!r} "
            f"actual={list(revisions)!r}"
        )
    conn = sqlite3.connect(str(db_path), timeout=30.0)
    try:
        with conn:
            updated = conn.execute(
                "UPDATE alembic_version SET version_num=? WHERE version_num=?",
                (ACTIVE_BASE, expected_archived_head),
            )
            if updated.rowcount != 1:
                raise UpgradeDatabaseError("failed to re-anchor the single Alembic revision row")
    finally:
        conn.close()


def upgrade_database(
    db_path: Path,
    *,
    repo_root: Path,
    backup_path: Path | None = None,
) -> UpgradeReceipt:
    """Upgrade ``db_path`` and return a validated receipt."""

    db_path = db_path.resolve()
    repo_root = repo_root.resolve()
    existed = db_path.exists()
    initial = _read_revisions(db_path)
    if len(initial) > 1:
        raise UpgradeDatabaseError(f"multiple Alembic heads in database: {list(initial)!r}")
    from_revision = initial[0] if initial else None

    active = _config(repo_root, db_path, archived=False)
    active_script = ScriptDirectory.from_config(active)
    active_revisions = {revision.revision for revision in active_script.walk_revisions()}
    if from_revision == ACTIVE_HEAD:
        _integrity_check(db_path)
        return UpgradeReceipt(
            status="already_current",
            db_path=str(db_path),
            from_revision=from_revision,
            to_revision=ACTIVE_HEAD,
            backup_path=None,
            completed_at=datetime.now(UTC).isoformat(),
        )

    if from_revision is None and _user_tables(db_path):
        raise UpgradeDatabaseError(
            "non-empty unversioned database; refusing to guess a migration baseline"
        )

    chosen_backup: Path | None = None
    if existed and from_revision is not None:
        chosen_backup = (backup_path or _default_backup_path(db_path)).resolve()

    with hold_run_lock(db_path, owner="upgrade_database", timeout_s=30.0):
        if chosen_backup is not None:
            _backup_database(db_path, chosen_backup)

        if from_revision is None or from_revision in active_revisions:
            command.upgrade(active, "head")
            status: Literal["created", "upgraded", "bridged", "already_current"] = (
                "created" if not existed else "upgraded"
            )
        else:
            archived = _config(repo_root, db_path, archived=True)
            archived_script = ScriptDirectory.from_config(archived)
            try:
                archived_script.get_revision(from_revision)
            except CommandError as exc:
                raise UpgradeDatabaseError(f"unknown Alembic revision: {from_revision}") from exc
            archived_heads = archived_script.get_heads()
            if len(archived_heads) != 1:
                raise UpgradeDatabaseError(
                    f"archived graph must have one head, found {archived_heads!r}"
                )
            archived_head = archived_heads[0]
            command.upgrade(archived, "head")
            _validate_legacy_schema(db_path)
            _reanchor_at_active_baseline(db_path, archived_head)
            command.upgrade(active, "head")
            status = "bridged"

        final = _read_revisions(db_path)
        if final != (ACTIVE_HEAD,):
            raise UpgradeDatabaseError(
                f"upgrade did not reach active head: expected={ACTIVE_HEAD!r} actual={list(final)!r}"
            )
        _integrity_check(db_path)

    return UpgradeReceipt(
        status=status,
        db_path=str(db_path),
        from_revision=from_revision,
        to_revision=ACTIVE_HEAD,
        backup_path=str(chosen_backup) if chosen_backup is not None else None,
        completed_at=datetime.now(UTC).isoformat(),
    )


def _log(event: str, **fields: object) -> None:
    print(json.dumps({"event": event, **fields}, sort_keys=True), file=sys.stderr)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", type=Path, required=True)
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--backup-path", type=Path)
    args = parser.parse_args()
    try:
        receipt = upgrade_database(
            args.db_path,
            repo_root=args.repo_root,
            backup_path=args.backup_path,
        )
    except (OSError, sqlite3.Error, CommandError, UpgradeDatabaseError) as exc:
        _log("database_upgrade_failed", error=f"{type(exc).__name__}: {exc}")
        return 1
    print(receipt.model_dump_json())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
