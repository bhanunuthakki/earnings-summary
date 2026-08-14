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
import ctypes
import os
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Protocol, cast

from _lib import log_event
from alembic.config import Config
from alembic.script import ScriptDirectory
from alembic.util.exc import CommandError
from pydantic import BaseModel, ConfigDict

from alembic import command
from run_lock import hold_run_lock
from runtime.job_runtime import portfolio_db_path
from sqlite_runtime import (
    SQLiteConnectionRole,
    connect_sqlite,
    require_safe_sqlite_writer_runtime,
)

ACTIVE_BASE = "0001_initial_schema"
ACTIVE_HEAD = "0017_add_owner_decision_checkpoints"
OPERATION_EVENTS_CONTRACT_REVISION = "0012_close_operation_event_detail_reason"
_MANAGED_RUNTIME_REPOSITORY = "earnings-summary"
_WINDOWS_CSIDL_PROFILE = 0x0028

_LEGACY_SCHEMA_REQUIREMENTS: dict[str, frozenset[str]] = {
    "tracked_companies": frozenset({"ticker", "processing_tier"}),
    "documents": frozenset({"id", "ticker"}),
    "llm_calls": frozenset({"purpose", "called_at"}),
    "llm_budgets": frozenset({"purpose", "on_exceed"}),
}

_ContractRows = tuple[tuple[object, ...], ...]
_ContractQuery = Callable[[str, tuple[object, ...]], _ContractRows]


class _OperationEventsContractValidator(Protocol):
    def __call__(self, query: _ContractQuery, *, closed: bool) -> None: ...


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


def trusted_account_home() -> Path:
    """Resolve the OS account home without consulting process environment."""

    if os.name == "nt":
        # ``Path.home()`` trusts USERPROFILE/HOME, so it cannot anchor a guard
        # whose callers may control their environment. SHGetFolderPathW asks
        # the Windows shell for the current account's registered profile.
        shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        profile = ctypes.create_unicode_buffer(32_768)
        result = shell32.SHGetFolderPathW(
            None,
            _WINDOWS_CSIDL_PROFILE,
            None,
            0,
            profile,
        )
        if result != 0 or not profile.value:
            raise UpgradeDatabaseError(
                f"Windows account profile lookup failed with HRESULT 0x{result & 0xFFFFFFFF:08x}"
            )
        return Path(profile.value).resolve()

    import pwd

    return Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()


def authoritative_managed_runtime_root() -> Path:
    """Return the closed laptop runtime identity, independent of caller input."""

    return (
        trusted_account_home() / ".gemini" / "antigravity" / "runtime" / _MANAGED_RUNTIME_REPOSITORY
    ).resolve()


def _same_database_path(left: Path, right: Path) -> bool:
    """Compare canonical paths and existing hard-link identities fail-closed."""

    if left == right:
        return True
    try:
        return left.samefile(right)
    except OSError:
        return False


def authoritative_managed_database_paths(runtime_root: Path) -> tuple[Path, ...]:
    """Return every DB identity that must retain live-cutover protections.

    The default managed-runtime path is always protected. A configured DB is
    protected in addition, never instead, so EARNINGS_SUMMARY_DB_PATH cannot
    redirect the guard away from the real managed database.
    """

    default_database = (runtime_root / "data" / "portfolio.db").resolve()
    configured_database = portfolio_db_path(runtime_root).resolve()
    if _same_database_path(default_database, configured_database):
        return (default_database,)
    return (default_database, configured_database)


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
    conn = connect_sqlite(db_path, role=SQLiteConnectionRole.READ_ONLY)
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
    conn = connect_sqlite(db_path, role=SQLiteConnectionRole.READ_ONLY)
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
    conn = connect_sqlite(db_path, role=SQLiteConnectionRole.READ_ONLY)
    try:
        result = conn.execute("PRAGMA integrity_check").fetchone()
        foreign_key_violations = conn.execute("PRAGMA foreign_key_check").fetchall()
    finally:
        conn.close()
    if result is None or str(result[0]).lower() != "ok":
        raise UpgradeDatabaseError(f"SQLite integrity_check failed for {db_path}: {result!r}")
    if foreign_key_violations:
        raise UpgradeDatabaseError(
            f"SQLite foreign_key_check failed for {db_path}: {foreign_key_violations[:10]!r}"
        )


def _backup_database(db_path: Path, backup_path: Path) -> None:
    if backup_path.exists():
        raise UpgradeDatabaseError(f"refusing to overwrite backup: {backup_path}")
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = backup_path.with_name(f".{backup_path.name}.{os.getpid()}.tmp")
    source = connect_sqlite(db_path, role=SQLiteConnectionRole.READ_ONLY)
    destination = connect_sqlite(
        temp_path,
        role=SQLiteConnectionRole.SNAPSHOT_DESTINATION,
        schema_preflight=False,
    )
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
    conn = connect_sqlite(db_path, role=SQLiteConnectionRole.READ_ONLY)
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


def _replace_single_revision(db_path: Path, *, expected: str, target: str) -> None:
    revisions = _read_revisions(db_path)
    if revisions != (expected,):
        raise UpgradeDatabaseError(
            f"database did not carry the expected single revision: expected={expected!r} "
            f"actual={list(revisions)!r}"
        )
    conn = connect_sqlite(
        db_path,
        role=SQLiteConnectionRole.WRITER,
        schema_preflight=False,
    )
    try:
        with conn:
            updated = conn.execute(
                "UPDATE alembic_version SET version_num=? WHERE version_num=?",
                (target, expected),
            )
            if updated.rowcount != 1:
                raise UpgradeDatabaseError("failed to re-anchor the single Alembic revision row")
    finally:
        conn.close()


def _require_exact_closed_operation_events_contract(
    db_path: Path,
    *,
    active: Config,
) -> None:
    script = ScriptDirectory.from_config(active)
    revision = script.get_revision(OPERATION_EVENTS_CONTRACT_REVISION)
    candidate = getattr(revision.module, "require_operation_events_contract", None)
    if not callable(candidate):
        raise UpgradeDatabaseError("active operation-events contract validator is unavailable")
    validate = cast(_OperationEventsContractValidator, candidate)
    conn = connect_sqlite(db_path, role=SQLiteConnectionRole.READ_ONLY)

    def query(sql: str, parameters: tuple[object, ...]) -> _ContractRows:
        return tuple(tuple(row) for row in conn.execute(sql, parameters).fetchall())

    try:
        validate(query, closed=True)
    except RuntimeError as exc:
        raise UpgradeDatabaseError(
            f"operation_events exact 0012 contract rejected: {exc}"
        ) from None
    finally:
        conn.close()


def _reanchor_at_active_baseline(
    db_path: Path,
    *,
    active: Config,
    expected_archived_head: str,
) -> None:
    if "operation_events" in _user_tables(db_path):
        # Compatibility for an exact-current schema whose revision metadata
        # was restored/restamped to the archived graph. Migration 0012 owns
        # the only non-idempotent shape delta, so let its exact contract guard
        # normalize operation_events back to 0011 before replaying the graph.
        _require_exact_closed_operation_events_contract(db_path, active=active)
        _replace_single_revision(
            db_path,
            expected=expected_archived_head,
            target=ACTIVE_HEAD,
        )
        command.downgrade(active, "0011_add_operations_journal")
        _replace_single_revision(
            db_path,
            expected="0011_add_operations_journal",
            target=ACTIVE_BASE,
        )
        return
    _replace_single_revision(
        db_path,
        expected=expected_archived_head,
        target=ACTIVE_BASE,
    )


def upgrade_database(
    db_path: Path,
    *,
    repo_root: Path,
    runtime_root: Path,
    backup_path: Path | None = None,
    phase0_backup_restore_receipt: Path | None = None,
    allow_isolated_db: bool = False,
) -> UpgradeReceipt:
    """Upgrade ``db_path`` and return a validated receipt."""

    require_safe_sqlite_writer_runtime()

    db_path = db_path.resolve()
    repo_root = repo_root.resolve()
    runtime_root = runtime_root.resolve()
    authoritative_runtime = authoritative_managed_runtime_root()
    authoritative_live_databases = authoritative_managed_database_paths(authoritative_runtime)
    live_database = any(
        _same_database_path(db_path, protected_db) for protected_db in authoritative_live_databases
    )
    if live_database and runtime_root != authoritative_runtime:
        raise UpgradeDatabaseError(
            "live portfolio DB runtime_root does not match the authoritative managed runtime"
        )
    if live_database and allow_isolated_db:
        raise UpgradeDatabaseError(
            "authoritative live portfolio DB cannot be treated as an isolated database"
        )
    if not live_database and not allow_isolated_db:
        raise UpgradeDatabaseError(
            "target database does not match the runtime database; "
            "isolated databases require explicit allow_isolated_db=True"
        )

    origin_observation = None
    if live_database and phase0_backup_restore_receipt is not None:
        # Fetching is network I/O and must not monopolize the portfolio DB
        # lock. The sealed observation is consumed by the locked recheck.
        # ``sqlite_bootstrap.py execution/upgrade_database.py`` installs the
        # execution directory itself as the import root.  Keep sibling CLI
        # imports top-level so the governed path entrypoint and pytest's
        # execution import root resolve the same module.
        import portfolio_readiness_receipt as readiness_module

        origin_observation = readiness_module.fetch_origin_main(repo_root)

    with hold_run_lock(db_path, owner="upgrade_database", timeout_s=30.0):
        # Revision/schema classification and the mutation it authorizes must be
        # one locked operation. Otherwise a concurrent writer can change the
        # database between inspection and the first Alembic statement.
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

        if existed and live_database:
            if phase0_backup_restore_receipt is None:
                raise UpgradeDatabaseError(
                    "live portfolio DB upgrade requires a Phase-0 backup/restore receipt"
                )
            if origin_observation is None:
                raise UpgradeDatabaseError("live portfolio DB origin evidence is unavailable")

            # Revalidation runs inside the same database lock as the backup and
            # Alembic mutation, closing the point-in-time TOCTOU gap. Origin was
            # fetched before the lock; this resolver performs no network I/O.
            import portfolio_readiness_receipt as readiness_module

            readiness = readiness_module.collect_readiness(
                checkout_root=repo_root,
                runtime_root=runtime_root,
                db_path=db_path,
                backup_restore_receipt_path=phase0_backup_restore_receipt,
                mode="migration",
                origin_resolver=lambda _root: origin_observation,
            )
            if not readiness.ready:
                raise UpgradeDatabaseError(
                    "live portfolio DB migration preconditions failed: "
                    + ",".join(readiness.blocking_reasons)
                )

        if from_revision is None and _user_tables(db_path):
            raise UpgradeDatabaseError(
                "non-empty unversioned database; refusing to guess a migration baseline"
            )

        chosen_backup: Path | None = None
        if existed and from_revision is not None:
            chosen_backup = (backup_path or _default_backup_path(db_path)).resolve()

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
            _reanchor_at_active_baseline(
                db_path,
                active=active,
                expected_archived_head=archived_head,
            )
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", type=Path, required=True)
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument(
        "--runtime-root",
        type=Path,
        required=True,
        help="Actual managed runtime checkout whose canonical portfolio DB is being evaluated.",
    )
    parser.add_argument("--backup-path", type=Path)
    parser.add_argument(
        "--phase0-backup-restore-receipt",
        type=Path,
        help=(
            "Required when upgrading the canonical live portfolio DB; revalidated "
            "inside the shared writer lock."
        ),
    )
    parser.add_argument(
        "--allow-isolated-db",
        action="store_true",
        help="Explicitly permit a non-runtime database for tests or rehearsals.",
    )
    args = parser.parse_args()
    try:
        receipt = upgrade_database(
            args.db_path,
            repo_root=args.repo_root,
            runtime_root=args.runtime_root,
            backup_path=args.backup_path,
            phase0_backup_restore_receipt=args.phase0_backup_restore_receipt,
            allow_isolated_db=args.allow_isolated_db,
        )
    except (OSError, RuntimeError, sqlite3.Error, CommandError) as exc:
        log_event("database_upgrade_failed", error=f"{type(exc).__name__}: {exc}")
        return 1
    print(receipt.model_dump_json())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
