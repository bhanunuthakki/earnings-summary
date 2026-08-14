"""Emit a fail-closed Phase-0 checkout/runtime/database readiness receipt.

Run through ``execution/sqlite_bootstrap.py`` so database probes use the
verified SQLite runtime. The receipt is point-in-time evidence only: it never
opens a database writer and cannot authorize a downstream mutation. A caller
must re-run it while holding the shared ``portfolio-db`` lock immediately
before any migration or write-path activation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"
EXECUTION = PROJECT_ROOT / "execution"
for import_root in (SRC, EXECUTION):
    if str(import_root) not in sys.path:
        sys.path.insert(0, str(import_root))

from backup_restore_readiness_receipt import (  # noqa: E402
    BackupRestoreReadinessReceipt,
    validate_receipt_for_source,
)
from upgrade_database import ACTIVE_HEAD  # noqa: E402

from runtime.job_runtime import portfolio_db_path  # noqa: E402
from schema_compat import describe_drift, expected_head  # noqa: E402
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite  # noqa: E402

_GIT_SHA = re.compile(r"[0-9a-f]{40,64}")
_RELEVANT_GIT_PATHS = (
    "alembic",
    "alembic.ini",
    "execution",
    "src",
    "pyproject.toml",
    "requirements.txt",
    "uv.lock",
)
GitShaResolver = Callable[[Path], str]
GitStatusResolver = Callable[[Path], tuple[str, ...]]
GitAncestryResolver = Callable[[Path, str, str], bool]
DriftState = Literal[
    "clear",
    "unavailable",
    "db_behind_code",
    "checkout_behind_db",
    "checkout_forked",
    "db_unreadable",
]
ReadinessMode = Literal["migration", "operational"]


class OriginMainObservation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    sha: str = Field(pattern=r"^[0-9a-f]{40,64}$")
    fetched_at: datetime


OriginResolver = Callable[[Path], OriginMainObservation]


class PortfolioReadinessReceipt(BaseModel):
    """Typed point-in-time evidence; never an authorization token."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["portfolio-readiness/v2"] = "portfolio-readiness/v2"
    evidence_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    observed_at: datetime
    checkout_root: str
    checkout_sha: str | None
    checkout_relevant_changes: tuple[str, ...]
    runtime_root: str
    runtime_sha: str | None
    runtime_relevant_changes: tuple[str, ...]
    origin_main_sha: str | None
    origin_main_fetched_at: datetime | None
    checkout_is_ancestor_of_origin_main: bool | None
    runtime_is_ancestor_of_origin_main: bool | None
    db_path_requested: str | None
    db_path_resolved: str
    expected_active_alembic_head: str
    checkout_alembic_head: str | None
    runtime_alembic_head: str | None
    db_revision: str | None
    drift_state: DriftState
    backup_restore_receipt_path: str | None
    backup_restore_evidence_id: str | None
    requested_mode: ReadinessMode
    migration_preconditions_met: bool
    operationally_aligned: bool
    migration_blocking_reasons: tuple[str, ...]
    operational_blocking_reasons: tuple[str, ...]
    ready: bool
    blocking_reasons: tuple[str, ...]
    point_in_time_only: Literal[True] = True
    authorizes_downstream_write: Literal[False] = False
    downstream_locked_revalidation_required: Literal[True] = True


def _git_sha(root: Path) -> str:
    completed = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--verify", "HEAD"],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    sha = completed.stdout.strip().lower()
    if completed.returncode != 0 or _GIT_SHA.fullmatch(sha) is None:
        raise ValueError("Git HEAD is unavailable")
    return sha


def _fresh_origin_main(root: Path) -> OriginMainObservation:
    completed = subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "fetch",
            "--quiet",
            "origin",
            "+refs/heads/main:refs/remotes/origin/main",
        ],
        check=False,
        capture_output=True,
        timeout=60,
    )
    if completed.returncode != 0:
        raise ValueError("origin/main fetch failed")
    observed_at = datetime.now(UTC)
    resolved = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--verify", "refs/remotes/origin/main"],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    sha = resolved.stdout.strip().lower()
    if resolved.returncode != 0 or _GIT_SHA.fullmatch(sha) is None:
        raise ValueError("fresh origin/main identity is unavailable")
    return OriginMainObservation(sha=sha, fetched_at=observed_at)


def _relevant_changes(root: Path) -> tuple[str, ...]:
    completed = subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
            "-z",
            "--",
            *_RELEVANT_GIT_PATHS,
        ],
        check=False,
        capture_output=True,
        timeout=20,
    )
    if completed.returncode != 0:
        raise ValueError("relevant Git status is unavailable")
    return tuple(
        entry.decode("utf-8", errors="replace") for entry in completed.stdout.split(b"\0") if entry
    )


def _is_ancestor(root: Path, ancestor: str, descendant: str) -> bool:
    completed = subprocess.run(
        ["git", "-C", str(root), "merge-base", "--is-ancestor", ancestor, descendant],
        check=False,
        capture_output=True,
        timeout=10,
    )
    if completed.returncode not in (0, 1):
        raise ValueError("Git ancestry is unavailable")
    return completed.returncode == 0


def _read_db_revision(db_path: Path) -> tuple[str | None, str | None]:
    """Return one revision or a stable blocking reason, without writing."""

    if not db_path.is_file():
        return None, "database_missing"
    try:
        conn = connect_sqlite(
            db_path,
            role=SQLiteConnectionRole.READ_ONLY,
            schema_preflight=False,
        )
        try:
            has_version = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='alembic_version'"
            ).fetchone()
            if has_version is None:
                return None, "database_revision_table_missing"
            revisions = tuple(
                sorted(
                    str(row[0]) for row in conn.execute("SELECT version_num FROM alembic_version")
                )
            )
        finally:
            conn.close()
    except Exception:
        return None, "database_unreadable"
    if len(revisions) != 1:
        return None, "database_revision_not_single"
    return revisions[0], None


def _evidence_id(receipt: PortfolioReadinessReceipt) -> str:
    payload = receipt.model_dump(mode="json", exclude={"evidence_id"})
    canonical = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def collect_readiness(
    *,
    checkout_root: Path,
    runtime_root: Path,
    db_path: Path | None = None,
    backup_restore_receipt_path: Path | None = None,
    mode: ReadinessMode = "operational",
    git_sha_resolver: GitShaResolver = _git_sha,
    git_status_resolver: GitStatusResolver = _relevant_changes,
    origin_resolver: OriginResolver = _fresh_origin_main,
    ancestry_resolver: GitAncestryResolver = _is_ancestor,
) -> PortfolioReadinessReceipt:
    """Collect an immutable readiness verdict from point-in-time evidence."""

    observed_at = datetime.now(UTC)
    checkout = checkout_root.resolve()
    runtime = runtime_root.resolve()
    requested_db = None if db_path is None else str(db_path)
    database = (db_path or portfolio_db_path(runtime)).resolve()
    base_reasons: list[str] = []

    checkout_sha: str | None = None
    runtime_sha: str | None = None
    origin: OriginMainObservation | None = None
    try:
        checkout_sha = git_sha_resolver(checkout)
    except (OSError, subprocess.SubprocessError, ValueError):
        base_reasons.append("checkout_git_head_unavailable")
    try:
        runtime_sha = git_sha_resolver(runtime)
    except (OSError, subprocess.SubprocessError, ValueError):
        base_reasons.append("runtime_git_head_unavailable")
    if checkout_sha is not None and runtime_sha is not None and checkout_sha != runtime_sha:
        base_reasons.append("runtime_checkout_sha_mismatch")

    try:
        checkout_changes = git_status_resolver(checkout)
    except (OSError, subprocess.SubprocessError, ValueError):
        checkout_changes = ()
        base_reasons.append("checkout_relevant_status_unavailable")
    if checkout_changes:
        base_reasons.append("checkout_relevant_changes_present")
    try:
        runtime_changes = git_status_resolver(runtime)
    except (OSError, subprocess.SubprocessError, ValueError):
        runtime_changes = ()
        base_reasons.append("runtime_relevant_status_unavailable")
    if runtime_changes:
        base_reasons.append("runtime_relevant_changes_present")

    try:
        origin = origin_resolver(checkout)
    except (OSError, subprocess.SubprocessError, ValueError):
        base_reasons.append("fresh_origin_main_unavailable")
    checkout_ancestor: bool | None = None
    runtime_ancestor: bool | None = None
    if origin is not None and checkout_sha is not None:
        try:
            checkout_ancestor = ancestry_resolver(checkout, checkout_sha, origin.sha)
        except (OSError, subprocess.SubprocessError, ValueError):
            base_reasons.append("checkout_origin_ancestry_unavailable")
        else:
            if not checkout_ancestor:
                base_reasons.append("checkout_not_ancestor_of_origin_main")
            if checkout_sha != origin.sha:
                base_reasons.append("checkout_not_at_fresh_origin_main")
    if origin is not None and runtime_sha is not None:
        try:
            runtime_ancestor = ancestry_resolver(checkout, runtime_sha, origin.sha)
        except (OSError, subprocess.SubprocessError, ValueError):
            base_reasons.append("runtime_origin_ancestry_unavailable")
        else:
            if not runtime_ancestor:
                base_reasons.append("runtime_not_ancestor_of_origin_main")

    checkout_head: str | None = None
    runtime_head: str | None = None
    try:
        checkout_head = expected_head(checkout)
    except Exception:
        base_reasons.append("checkout_alembic_head_unavailable")
    try:
        runtime_head = expected_head(runtime)
    except Exception:
        base_reasons.append("runtime_alembic_head_unavailable")
    if checkout_head is not None and checkout_head != ACTIVE_HEAD:
        base_reasons.append("active_head_contract_mismatch")
    if checkout_head is not None and runtime_head is not None and runtime_head != checkout_head:
        base_reasons.append("runtime_checkout_alembic_head_mismatch")

    db_revision, db_reason = _read_db_revision(database)
    if db_reason is not None:
        base_reasons.append(db_reason)

    drift_state: DriftState = "unavailable"
    if checkout_head is not None and db_reason is None:
        try:
            drift = describe_drift(database, project_root=checkout)
        except Exception:
            base_reasons.append("schema_drift_probe_failed")
        else:
            if drift is None:
                if db_revision == checkout_head:
                    drift_state = "clear"
                else:
                    base_reasons.append("schema_drift_probe_inconclusive")
            else:
                if drift.reason == "db_behind_code":
                    drift_state = "db_behind_code"
                elif drift.reason == "checkout_behind_db":
                    drift_state = "checkout_behind_db"
                elif drift.reason == "checkout_forked":
                    drift_state = "checkout_forked"
                elif drift.reason == "db_unreadable":
                    drift_state = "db_unreadable"
                else:
                    base_reasons.append("schema_drift_reason_unknown")

    backup_evidence_id: str | None = None
    resolved_backup_receipt: str | None = None
    backup_receipt: BackupRestoreReadinessReceipt | None = None
    if backup_restore_receipt_path is None:
        base_reasons.append("backup_restore_receipt_required")
    else:
        receipt_path = backup_restore_receipt_path.resolve()
        resolved_backup_receipt = str(receipt_path)
        try:
            backup_receipt = BackupRestoreReadinessReceipt.model_validate_json(
                receipt_path.read_text(encoding="utf-8")
            )
        except (OSError, ValueError):
            base_reasons.append("backup_restore_receipt_invalid")
        else:
            backup_evidence_id = backup_receipt.evidence_id
    artifact_reasons: tuple[str, ...] = ()
    current_source_reasons: tuple[str, ...] = ()
    if backup_receipt is not None:
        artifact_reasons = validate_receipt_for_source(
            backup_receipt,
            source_db=database,
            source_revision=db_revision,
            require_current_identity=False,
        )
        current_source_reasons = validate_receipt_for_source(
            backup_receipt,
            source_db=database,
            source_revision=db_revision,
            require_current_identity=True,
        )
        try:
            snapshot_drift = describe_drift(
                Path(backup_receipt.snapshot_resolved_path),
                project_root=checkout,
            )
        except Exception:
            artifact_reasons = (*artifact_reasons, "backup_restore_revision_probe_failed")
        else:
            if snapshot_drift is not None and snapshot_drift.reason != "db_behind_code":
                artifact_reasons = (
                    *artifact_reasons,
                    "backup_restore_revision_not_ancestor_of_target",
                )

    common = list(dict.fromkeys(base_reasons))
    operational_reasons = [*common, *artifact_reasons]
    if drift_state != "clear":
        operational_reasons.append(f"schema_drift:{drift_state}")
    migration_reasons = [*common, *current_source_reasons]
    if drift_state != "db_behind_code":
        migration_reasons.append(f"migration_requires_db_behind_code:{drift_state}")
    migration_blocking_reasons = tuple(dict.fromkeys(migration_reasons))
    operational_blocking_reasons = tuple(dict.fromkeys(operational_reasons))
    migration_preconditions_met = not migration_blocking_reasons
    operationally_aligned = not operational_blocking_reasons
    blocking_reasons = (
        migration_blocking_reasons if mode == "migration" else operational_blocking_reasons
    )
    draft = PortfolioReadinessReceipt(
        evidence_id="0" * 64,
        observed_at=observed_at,
        checkout_root=str(checkout),
        checkout_sha=checkout_sha,
        checkout_relevant_changes=checkout_changes,
        runtime_root=str(runtime),
        runtime_sha=runtime_sha,
        runtime_relevant_changes=runtime_changes,
        origin_main_sha=None if origin is None else origin.sha,
        origin_main_fetched_at=None if origin is None else origin.fetched_at,
        checkout_is_ancestor_of_origin_main=checkout_ancestor,
        runtime_is_ancestor_of_origin_main=runtime_ancestor,
        db_path_requested=requested_db,
        db_path_resolved=str(database),
        expected_active_alembic_head=ACTIVE_HEAD,
        checkout_alembic_head=checkout_head,
        runtime_alembic_head=runtime_head,
        db_revision=db_revision,
        drift_state=drift_state,
        backup_restore_receipt_path=resolved_backup_receipt,
        backup_restore_evidence_id=backup_evidence_id,
        requested_mode=mode,
        migration_preconditions_met=migration_preconditions_met,
        operationally_aligned=operationally_aligned,
        migration_blocking_reasons=migration_blocking_reasons,
        operational_blocking_reasons=operational_blocking_reasons,
        ready=not blocking_reasons,
        blocking_reasons=blocking_reasons,
    )
    return draft.model_copy(update={"evidence_id": _evidence_id(draft)})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkout-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument(
        "--db-path",
        type=Path,
        help="Override the runtime-root database resolved by the canonical DB path helper.",
    )
    parser.add_argument(
        "--mode",
        choices=("migration", "operational"),
        default="operational",
        help="Evaluate locked migration preconditions or post-migration alignment.",
    )
    parser.add_argument(
        "--backup-restore-receipt",
        type=Path,
        required=True,
        help="Typed receipt emitted by backup_restore_readiness_receipt.py.",
    )
    args = parser.parse_args(argv)
    receipt = collect_readiness(
        checkout_root=args.checkout_root,
        runtime_root=args.runtime_root,
        db_path=args.db_path,
        backup_restore_receipt_path=args.backup_restore_receipt,
        mode=args.mode,
    )
    print(receipt.model_dump_json(indent=2))
    return 0 if receipt.ready else 1


if __name__ == "__main__":
    raise SystemExit(main())
