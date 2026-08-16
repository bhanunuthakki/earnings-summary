"""Plan or apply a closed-snapshot, production-derived data-backbone rehearsal.

The command never opens or activates a live WAL database. Its source must be a
closed, sidecar-free restored snapshot. Apply mode uses the SQLite backup API,
copies and seals the immutable FMP corpus, upgrades and
rehydrates only the candidate, and exercises replacement plus rollback on
throwaway copies inside a new work directory.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import sqlite3
import subprocess
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pydantic

import alembic

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from log_redact import redact  # noqa: E402
from provenance.data_backbone_rehearsal import (  # noqa: E402
    CodeIdentity,
    CodeManifestEntry,
    DatabaseReadMode,
    RehearsalError,
    RehearsalFailureReceipt,
    RehearsalReceipt,
    RuntimeIdentity,
    SwapRehearsalRolledBackError,
    UpgradeTerminalReceipt,
    build_corpus_manifest,
    build_table_commitments,
    copy_corpus_verified,
    database_revision,
    database_storage_identity,
    exercise_swap_and_rollback,
    online_backup_read_only,
    require_disk_space,
    require_equal_commitments,
    require_sidecar_free_database,
    seal_failure_receipt,
    seal_rehearsal_receipt,
    sha256_file,
    validate_offline_receipt,
    verify_database,
    write_json_atomically,
)
from runtime.python_process import managed_python_prefix  # noqa: E402

_CLEANUP_POLICY = "retain_failure_evidence_and_resume_only_with_a_new_empty_work_directory"
_CODE_PATHS = (
    "execution/rehearse_data_backbone.py",
    "src/provenance/data_backbone_rehearsal.py",
    "execution/upgrade_database.py",
    "execution/refresh_cache.py",
    "src/provenance/atomic_cutover.py",
)


def _event(event: str, **fields: object) -> None:
    safe_fields = {key: redact(value) for key, value in fields.items()}
    print(
        json.dumps({"event": event, **safe_fields}, sort_keys=True, separators=(",", ":")),
        file=sys.stderr,
    )


def _git_commit(repo_root: Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    commit = result.stdout.strip().lower()
    if (
        result.returncode != 0
        or len(commit) != 40
        or any(char not in "0123456789abcdef" for char in commit)
    ):
        raise RehearsalError("unable to bind rehearsal to one Git commit")
    return commit


def code_identity(repo_root: Path) -> CodeIdentity:
    status = subprocess.run(
        ["git", "-C", str(repo_root), "status", "--porcelain=v1", "--untracked-files=all"],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if status.returncode != 0:
        raise RehearsalError("unable to inspect Git worktree identity")
    entries: list[CodeManifestEntry] = []
    for relative_path in _CODE_PATHS:
        path = repo_root / relative_path
        if not path.is_file():
            raise RehearsalError(f"required rehearsal code path is missing: {relative_path}")
        entries.append(
            CodeManifestEntry(
                relative_path=relative_path,
                size_bytes=path.stat().st_size,
                content_sha256=sha256_file(path),
            )
        )
    frozen = tuple(entries)
    canonical = json.dumps(
        [entry.model_dump(mode="json") for entry in frozen],
        sort_keys=True,
        separators=(",", ":"),
    )
    return CodeIdentity(
        worktree_clean=not status.stdout,
        porcelain_sha256=hashlib.sha256(status.stdout.encode("utf-8")).hexdigest(),
        entries=frozen,
        manifest_sha256=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
    )


def _runtime_identity() -> RuntimeIdentity:
    executable = Path(sys.executable).resolve(strict=True)
    return RuntimeIdentity(
        python_version=platform.python_version(),
        python_executable=str(executable),
        python_executable_sha256=sha256_file(executable),
        sqlite_version=sqlite3.sqlite_version,
        pydantic_version=pydantic.__version__,
        alembic_version=alembic.__version__,
        platform=platform.platform(),
    )


def _resolve_inputs(
    *,
    repo_root: Path,
    source_db: Path,
    source_corpus: Path,
    work_dir: Path,
    receipt_path: Path,
) -> tuple[Path, Path, Path, Path, Path]:
    repo_root = repo_root.resolve(strict=True)
    source_db = source_db.resolve(strict=True)
    # Preserve the lexical corpus root until its lstat/reparse safety gate.
    source_corpus = source_corpus.expanduser().absolute()
    work_dir = work_dir.resolve(strict=False)
    receipt_path = receipt_path.resolve(strict=False)
    if not repo_root.is_dir() or not source_db.is_file() or not source_corpus.is_dir():
        raise RehearsalError("repo root, source database, and source corpus must exist")
    if work_dir.exists():
        raise RehearsalError(
            "work directory already exists; retain it as evidence and resume with a new empty path"
        )
    if not work_dir.parent.is_dir():
        raise RehearsalError("work-directory parent must already exist for disk preflight")
    source_aliases = {
        os.path.normcase(str(source_db)),
        *(os.path.normcase(f"{source_db}{suffix}") for suffix in ("-wal", "-shm", "-journal")),
    }
    if os.path.normcase(str(work_dir)) in source_aliases:
        raise RehearsalError("work directory must not alias the source database or sidecars")
    if os.path.normcase(str(receipt_path)) in source_aliases:
        raise RehearsalError("receipt path must not alias the source database or sidecars")
    try:
        receipt_path.relative_to(work_dir)
    except ValueError:
        pass
    else:
        raise RehearsalError("receipt path must be outside the disposable work directory")
    for protected in (source_db, source_corpus):
        try:
            work_dir.relative_to(protected)
        except ValueError:
            continue
        raise RehearsalError("work directory must be outside every source path")
    try:
        receipt_path.relative_to(source_corpus)
    except ValueError:
        pass
    else:
        raise RehearsalError("receipt path must be outside the source corpus")
    return repo_root, source_db, source_corpus, work_dir, receipt_path


def _run_upgrade(repo_root: Path, candidate: Path, backup: Path) -> UpgradeTerminalReceipt:
    command = [
        *managed_python_prefix(repo_root),
        str(repo_root / "execution" / "upgrade_database.py"),
        "--db-path",
        str(candidate),
        "--repo-root",
        str(repo_root),
        "--runtime-root",
        str(candidate.parents[1]),
        "--backup-path",
        str(backup),
        "--allow-isolated-db",
    ]
    result = subprocess.run(
        command,
        cwd=repo_root,
        check=False,
        capture_output=True,
        text=True,
        timeout=60 * 30,
    )
    if result.returncode != 0:
        raise RehearsalError(f"candidate migration failed with terminal code {result.returncode}")
    try:
        return UpgradeTerminalReceipt.model_validate_json(result.stdout)
    except ValueError as exc:
        raise RehearsalError("candidate migration emitted an invalid receipt") from exc


def _offline_runner_source(repo_root: Path, rehearsal_root: Path) -> str:
    execution_path = json.dumps(str(repo_root / "execution"))
    rehearsal_path = json.dumps(str(rehearsal_root))
    return f"""from __future__ import annotations
import sys
from pathlib import Path
sys.path.insert(0, {execution_path})
import refresh_cache
root = Path({rehearsal_path})
cache = root / ".tmp" / "cacher"
cache.mkdir(parents=True, exist_ok=True)
refresh_cache.PROJECT_ROOT = root
refresh_cache.DB_PATH = root / "data" / "portfolio.db"
refresh_cache.ENV_FILE = root / ".env"
refresh_cache.FMP_DIR = root / "data" / "historical" / "fmp"
refresh_cache.CACHE_DIR = cache
refresh_cache.LOCK_PATH = cache / ".lock"
refresh_cache.OFFLINE_LOCK_PATH = cache / ".offline-corpus.lock"
refresh_cache.QUEUE_PATH = cache / "queue.json"
refresh_cache.HINTS_PATH = cache / "forced_stale.json"
sys.argv = ["refresh_cache.py", "run", "--offline-corpus-only", "--db", str(refresh_cache.DB_PATH)]
raise SystemExit(refresh_cache.main())
"""


def _run_offline_replay(
    repo_root: Path,
    rehearsal_root: Path,
    *,
    copied_manifest_sha: str,
) -> object:
    runner = rehearsal_root / "run_offline_replay.py"
    runner.write_text(_offline_runner_source(repo_root, rehearsal_root), encoding="utf-8")
    environment = dict(os.environ)
    for key in tuple(environment):
        if key.upper() in {"FMP_API_KEY", "FMP_TOKEN", "FMP_KEY"}:
            del environment[key]
    result = subprocess.run(
        [*managed_python_prefix(repo_root), str(runner)],
        cwd=rehearsal_root,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=60 * 60,
    )
    return validate_offline_receipt(
        result.stdout,
        return_code=result.returncode,
        copied_manifest_sha=copied_manifest_sha,
    )


def _copy_database_exact(source: Path, destination: Path) -> None:
    if destination.exists():
        raise RehearsalError(f"throwaway database already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)
    if sha256_file(destination) != sha256_file(source):
        raise RehearsalError("throwaway database copy differs from its source")


def failure_destination(args: argparse.Namespace) -> Path:
    requested = args.receipt_path.expanduser().resolve(strict=False)
    source_db = args.source_db.expanduser().resolve(strict=False)
    source_corpus = args.source_corpus.expanduser().resolve(strict=False)
    work_dir = args.work_dir.expanduser().resolve(strict=False)
    aliases = {
        os.path.normcase(str(source_db)),
        *(os.path.normcase(f"{source_db}{suffix}") for suffix in ("-wal", "-shm", "-journal")),
    }
    unsafe = requested.exists() or os.path.normcase(str(requested)) in aliases
    for protected in (source_corpus, work_dir):
        try:
            requested.relative_to(protected)
        except ValueError:
            continue
        unsafe = True
    if not unsafe:
        return requested
    failure_root = PROJECT_ROOT.resolve() / ".tmp" / "data-backbone-rehearsal-failures"
    failure_root.mkdir(parents=True, exist_ok=True)
    root_stat = failure_root.lstat()
    attributes = int(getattr(root_stat, "st_file_attributes", 0))
    if failure_root.is_symlink() or attributes & 0x400:
        raise RehearsalError("durable failure-receipt directory is a reparse point")
    return failure_root / f"failure-{uuid.uuid4()}.json"


def _record_failure(args: argparse.Namespace, exc: Exception) -> RehearsalFailureReceipt:
    destination = failure_destination(args)
    failure = seal_failure_receipt(
        requested_receipt_path=args.receipt_path.expanduser().resolve(strict=False),
        durable_receipt_path=destination,
        failure_type=type(exc).__name__,
        failure_detail=redact(exc),
        cleanup_resume_policy=_CLEANUP_POLICY,
    )
    write_json_atomically(destination, failure)
    return failure


def _receipt_common(
    *,
    mode: str,
    status: str,
    commit: str,
    code_identity: CodeIdentity,
    runtime: RuntimeIdentity,
    repo_root: Path,
    source_db: Path,
    source_corpus: Path,
    work_dir: Path,
    receipt_path: Path,
    source_revision: str,
    expected_head: str,
    source_verification: object,
    source_storage: object,
    source_manifest: object,
    preservation_before: object,
    required_disk: int,
    free_disk: int,
    started_at: datetime,
    **terminal: object,
) -> RehearsalReceipt:
    return seal_rehearsal_receipt(
        schema_version="data-backbone-rehearsal/v1",
        mode=mode,
        status=status,
        main_commit=commit,
        code_identity=code_identity,
        runtime=runtime,
        repo_root=str(repo_root),
        source_database=str(source_db),
        source_corpus=str(source_corpus),
        work_directory=str(work_dir),
        receipt_path=str(receipt_path),
        source_schema_before=source_revision,
        expected_schema_after=expected_head,
        source_database_before=source_verification,
        source_storage_before=source_storage,
        source_corpus_before=source_manifest,
        preservation_before=preservation_before,
        required_disk_bytes=required_disk,
        free_disk_bytes_at_preflight=free_disk,
        cleanup_resume_policy=_CLEANUP_POLICY,
        started_at=started_at,
        completed_at=datetime.now(UTC),
        **terminal,
    )


def run(args: argparse.Namespace) -> RehearsalReceipt:
    started_at = datetime.now(UTC)
    repo_root, source_db, source_corpus, work_dir, receipt_path = _resolve_inputs(
        repo_root=args.repo_root,
        source_db=args.source_db,
        source_corpus=args.source_corpus,
        work_dir=args.work_dir,
        receipt_path=args.receipt_path,
    )
    if receipt_path.exists():
        raise RehearsalError(f"receipt path already exists: {receipt_path}")
    commit = _git_commit(repo_root)
    code_identity_before = code_identity(repo_root)
    runtime = _runtime_identity()
    require_sidecar_free_database(source_db)
    source_revision = database_revision(
        source_db,
        read_mode=DatabaseReadMode.CLOSED_IMMUTABLE_SOURCE,
    )
    from upgrade_database import ACTIVE_HEAD

    source_verification = verify_database(
        source_db,
        expected_head=source_revision,
        read_mode=DatabaseReadMode.CLOSED_IMMUTABLE_SOURCE,
    )
    source_storage = database_storage_identity(source_db)
    source_manifest = build_corpus_manifest(source_corpus)
    preservation_before = build_table_commitments(
        source_db,
        read_mode=DatabaseReadMode.CLOSED_IMMUTABLE_SOURCE,
    )
    required_disk = require_disk_space(
        work_dir,
        database_bytes=source_verification.size_bytes,
        corpus_bytes=source_manifest.total_bytes,
    )
    free_disk = shutil.disk_usage(work_dir.parent).free

    if not args.apply_rehearsal:
        return _receipt_common(
            mode="plan",
            status="planned",
            commit=commit,
            code_identity=code_identity_before,
            runtime=runtime,
            repo_root=repo_root,
            source_db=source_db,
            source_corpus=source_corpus,
            work_dir=work_dir,
            receipt_path=receipt_path,
            source_revision=source_revision,
            expected_head=ACTIVE_HEAD,
            source_verification=source_verification,
            source_storage=source_storage,
            source_manifest=source_manifest,
            preservation_before=preservation_before,
            required_disk=required_disk,
            free_disk=free_disk,
            started_at=started_at,
        )

    if not code_identity_before.worktree_clean:
        raise RehearsalError("apply rehearsal requires a clean tracked and untracked worktree")

    rehearsal_root = work_dir / "rehearsal-repo"
    candidate = rehearsal_root / "data" / "portfolio.db"
    copied_corpus = rehearsal_root / "data" / "historical" / "fmp"
    work_dir.mkdir(parents=True)
    _event("data_backbone_rehearsal_started", work_directory=work_dir)
    online_backup_read_only(source_db, candidate)
    source_snapshot = work_dir / "source-snapshot.db"
    _copy_database_exact(candidate, source_snapshot)
    snapshot_commitments = build_table_commitments(candidate)
    require_equal_commitments(preservation_before, snapshot_commitments)
    _, copied_manifest = copy_corpus_verified(
        source_corpus,
        copied_corpus,
        expected_source=source_manifest,
    )
    upgrade = _run_upgrade(repo_root, candidate, work_dir / "candidate-pre-upgrade.db")
    if upgrade.from_revision != source_revision or upgrade.to_revision != ACTIVE_HEAD:
        raise RehearsalError("upgrade receipt is not bound to source and expected revisions")
    offline = _run_offline_replay(
        repo_root,
        rehearsal_root,
        copied_manifest_sha=copied_manifest.manifest_sha256,
    )
    source_manifest_after = build_corpus_manifest(source_corpus)
    copied_manifest_after = build_corpus_manifest(copied_corpus)
    if source_manifest_after != source_manifest:
        raise RehearsalError("source corpus changed during rehearsal")
    if copied_manifest_after != copied_manifest:
        raise RehearsalError("copied corpus changed outside governed offline replay")
    source_storage_after = database_storage_identity(source_db)
    source_sha_after = sha256_file(source_db)
    if source_storage_after != source_storage:
        raise RehearsalError("source database storage bytes changed during rehearsal")
    preservation_after = build_table_commitments(candidate)
    require_equal_commitments(preservation_before, preservation_after)
    candidate_verification = verify_database(candidate, expected_head=ACTIVE_HEAD)

    swap_dir = work_dir / "swap"
    swap_live = swap_dir / "rehearsal-live.db"
    swap_candidate = swap_dir / "rehearsal-candidate.db"
    _copy_database_exact(source_snapshot, swap_live)
    _copy_database_exact(candidate, swap_candidate)
    swap = exercise_swap_and_rollback(swap_dir, swap_live, swap_candidate)
    forced_dir = work_dir / "forced-swap"
    forced_live = forced_dir / "rehearsal-live.db"
    forced_candidate = forced_dir / "rehearsal-candidate.db"
    _copy_database_exact(source_snapshot, forced_live)
    _copy_database_exact(candidate, forced_candidate)
    try:
        exercise_swap_and_rollback(
            forced_dir,
            forced_live,
            forced_candidate,
            force_post_swap_failure=True,
        )
    except SwapRehearsalRolledBackError as exc:
        forced_swap = exc.evidence
    else:
        raise RehearsalError("forced post-swap fault did not exercise rollback")
    code_identity_after = code_identity(repo_root)
    if code_identity_after != code_identity_before:
        raise RehearsalError("repository code identity changed during rehearsal")
    return _receipt_common(
        mode="apply_rehearsal",
        status="passed",
        commit=commit,
        code_identity=code_identity_before,
        runtime=runtime,
        repo_root=repo_root,
        source_db=source_db,
        source_corpus=source_corpus,
        work_dir=work_dir,
        receipt_path=receipt_path,
        source_revision=source_revision,
        expected_head=ACTIVE_HEAD,
        source_verification=source_verification,
        source_storage=source_storage,
        source_manifest=source_manifest,
        preservation_before=preservation_before,
        required_disk=required_disk,
        free_disk=free_disk,
        started_at=started_at,
        source_database_after_sha256=source_sha_after,
        source_storage_after=source_storage_after,
        candidate_database_after=candidate_verification,
        source_corpus_after=source_manifest_after,
        copied_corpus_after=copied_manifest_after,
        preservation_after=preservation_after,
        upgrade=upgrade,
        offline_replay=offline,
        swap_rollback=swap,
        forced_failure_rollback=forced_swap,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--source-db", type=Path, required=True)
    parser.add_argument("--source-corpus", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--receipt-path", type=Path, required=True)
    parser.add_argument(
        "--apply-rehearsal",
        action="store_true",
        help="Create and mutate only isolated candidate and throwaway paths",
    )
    args = parser.parse_args(argv)
    try:
        receipt = run(args)
        write_json_atomically(args.receipt_path.resolve(), receipt)
    except Exception as exc:
        failure = _record_failure(args, exc)
        print(failure.model_dump_json(), end="\n")
        _event(
            "data_backbone_rehearsal_failed",
            durable_receipt_path=failure.durable_receipt_path,
            receipt_sha256=failure.receipt_sha256,
            status=failure.status,
        )
        return 1
    print(receipt.model_dump_json(), end="\n")
    _event(
        "data_backbone_rehearsal_complete",
        mode=receipt.mode,
        receipt_sha256=receipt.receipt_sha256,
        status=receipt.status,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
