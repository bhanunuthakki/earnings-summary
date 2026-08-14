"""Generate and independently judge a project-specific README update.

Preview (always generates and judges; never writes README.md):
    python execution/sqlite_bootstrap.py execution/update_readme.py

Apply only a judge-approved candidate, with an atomic concurrent-edit guard:
    python execution/sqlite_bootstrap.py execution/update_readme.py --apply

Apply an exact previously judged preview:
    python execution/sqlite_bootstrap.py execution/update_readme.py --apply-run <run_id>

Every candidate and judgment is retained under ``.tmp/readme_updater/<run_id>``.
The collector reads only an explicit allowlist of repository documentation and
source metadata; it never reads .env, credentials, data, outputs, or transcripts.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import stat
import sys
import tomllib
from pathlib import Path
from urllib.parse import unquote
from uuid import uuid4

from pydantic import BaseModel, ConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from log_redact import redact  # noqa: E402
from operations.readme_governance import receipt_violations  # noqa: E402
from readme_receipt import LlmCallAttestation, StoredReadmeReceipt  # noqa: E402
from readme_updater import (  # noqa: E402
    MAX_REVISIONS_PER_RUN,
    CliContract,
    EvidenceSource,
    RepositoryEvidence,
    candidate_violations,
    evidence_sha256,
    run_update_cycle,
)
from run_lock import hold_run_lock  # noqa: E402
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite  # noqa: E402

_SOURCE_ALLOWLIST: tuple[tuple[str, int], ...] = (
    ("AGENTS.md", 14_000),
    ("DEFINITIONS.md", 10_000),
    ("pyproject.toml", 9_000),
    ("Makefile", 5_000),
    ("requirements.txt", 4_000),
    ("start_comments_server.bat", 4_000),
    ("execution/upgrade_database.py", 10_000),
    ("execution/update_readme.py", 18_000),
    ("tests/test_readme_updater.py", 12_000),
    ("directives/llm_calls.md", 12_000),
    ("cron/SETUP_WINDOWS_SCHEDULER.md", 12_000),
    ("directives/README.md", 10_000),
    ("src/operations/registry.py", 10_000),
    ("HOW_TO_USE_REPORTS.md", 14_000),
    ("directives/data_pipeline_dag.md", 12_000),
    ("execution/comments_server.py", 6_000),
    ("execution/run_morning_pipeline.py", 8_000),
)
_MAX_EVIDENCE_CHARS = 60_000
_MAX_EVIDENCE_PACKET_BYTES = 110_000
_PATH_ROOTS = ("src", "execution", "directives", "cron", "docs", "alembic/versions", "tests")
_PATH_SUFFIXES = frozenset({".py", ".md", ".json", ".bat", ".toml", ".txt", ".yml", ".yaml"})
_MARKDOWN_LINK = re.compile(r"\[[^\]]+\]\(([^)]+)\)")
_ARGPARSE_OPTION = re.compile(r"add_argument\(\s*[\"'](--[a-z0-9][a-z0-9-]*)[\"']")
_RUN_ID = re.compile(r"[0-9a-f]{32}\Z")
_MAX_STORED_RECEIPT_BYTES = 1_000_000
_MAX_STORED_CANDIDATE_BYTES = 250_000
_CLI_CONTRACT_PATHS = (
    "execution/comments_server.py",
    "execution/generate_cron_artifacts.py",
    "execution/update_readme.py",
    "execution/upgrade_database.py",
    "execution/verify_cron_registration.py",
)


class _CronSchedule(BaseModel):
    model_config = ConfigDict(extra="ignore")

    trigger: str
    start_boundary: str | None = None


class _CronTask(BaseModel):
    model_config = ConfigDict(extra="ignore")

    task_name: str
    wrapper: str
    schedule: _CronSchedule


class _CronManifest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    tasks: tuple[_CronTask, ...]


class _ProjectMetadata(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str | None = None


class _Pyproject(BaseModel):
    model_config = ConfigDict(extra="ignore")

    project: _ProjectMetadata | None = None


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


_REPARSE_POINT = 0x400


def _read_regular_file(path: Path, *, limit: int, reject_oversize: bool = True) -> bytes:
    """Bounded, handle-verified read that rejects symlinks, hardlinks, and junctions."""

    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise ValueError(f"refusing linked or non-regular evidence file: {path}")
    if int(getattr(before, "st_file_attributes", 0)) & _REPARSE_POINT:
        raise ValueError(f"refusing reparse-point evidence file: {path}")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        opened = os.fstat(fd)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
        ):
            raise ValueError(f"evidence file changed while opening: {path}")
        payload = os.read(fd, limit + 1)
        if reject_oversize and len(payload) > limit:
            raise ValueError(f"file exceeds bounded read limit: {path}")
        return payload if not reject_oversize else payload[:limit]
    finally:
        os.close(fd)


def _read_source(repo_root: Path, relative_path: str, limit: int) -> EvidenceSource | None:
    path = repo_root / relative_path
    if not path.is_file():
        return None
    raw = _read_regular_file(path, limit=limit + 1, reject_oversize=False)
    text = raw.decode("utf-8", errors="replace")
    return EvidenceSource(
        path=relative_path,
        sha256=_sha256_bytes(raw),
        text=text[:limit],
        truncated=len(raw) > limit or len(text) > limit,
    )


def _tracked_paths(repo_root: Path) -> tuple[str, ...]:
    paths: set[str] = set()
    for relative, _limit in _SOURCE_ALLOWLIST:
        if (repo_root / relative).exists():
            paths.add(relative)
    for root_name in _PATH_ROOTS:
        root = repo_root / root_name
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if path.is_file() and path.suffix.casefold() in _PATH_SUFFIXES:
                paths.add(path.relative_to(repo_root).as_posix())
    if (repo_root / "README.md").is_file():
        paths.add("README.md")
    return tuple(sorted(paths)[:600])


def _source_packages(repo_root: Path) -> tuple[str, ...]:
    src = repo_root / "src"
    if not src.exists():
        return ()
    names = {path.name for path in src.iterdir() if path.is_dir() and not path.name.startswith("_")}
    names.update(path.stem for path in src.glob("*.py") if not path.name.startswith("_"))
    return tuple(sorted(names)[:200])


def _cron_tasks(repo_root: Path) -> tuple[dict[str, str | None], ...]:
    path = repo_root / "cron" / "task_manifest.json"
    if not path.is_file():
        return ()
    manifest = _CronManifest.model_validate_json(_read_regular_file(path, limit=250_000))
    tasks: list[dict[str, str | None]] = []
    for row in manifest.tasks:
        tasks.append(
            {
                "task_name": row.task_name,
                "wrapper": row.wrapper,
                "trigger": row.schedule.trigger,
                "start_boundary": row.schedule.start_boundary,
            }
        )
    return tuple(tasks[:100])


def _cli_contracts(repo_root: Path) -> tuple[CliContract, ...]:
    contracts: list[CliContract] = []
    for relative_path in _CLI_CONTRACT_PATHS:
        path = repo_root / relative_path
        if not path.is_file():
            continue
        source = _read_regular_file(path, limit=250_000).decode("utf-8")
        contracts.append(
            CliContract(
                path=relative_path,
                options=tuple(sorted(set(_ARGPARSE_OPTION.findall(source)))),
            )
        )
    return tuple(contracts[:32])


def collect_repository_evidence(repo_root: Path) -> RepositoryEvidence:
    """Collect bounded public repository evidence from a fixed allowlist."""

    root = repo_root.resolve()
    sources: list[EvidenceSource] = []
    remaining = _MAX_EVIDENCE_CHARS
    for relative, per_file_limit in _SOURCE_ALLOWLIST:
        if remaining <= 0:
            break
        source = _read_source(root, relative, min(per_file_limit, remaining))
        if source is None:
            continue
        sources.append(source)
        remaining -= len(source.text)

    project_name = root.name
    pyproject = root / "pyproject.toml"
    if pyproject.is_file():
        payload = _Pyproject.model_validate(
            tomllib.loads(_read_regular_file(pyproject, limit=250_000).decode("utf-8"))
        )
        if payload.project is not None and payload.project.name is not None:
            project_name = payload.project.name

    execution_root = root / "execution"
    execution_entrypoints = (
        tuple(sorted(path.name for path in execution_root.glob("*.py"))[:400])
        if execution_root.exists()
        else ()
    )
    tests_root = root / "tests"
    test_count = len(tuple(tests_root.glob("test_*.py"))) if tests_root.exists() else 0
    evidence = RepositoryEvidence(
        project_name=project_name,
        tracked_paths=_tracked_paths(root),
        source_packages=_source_packages(root),
        execution_entrypoints=execution_entrypoints,
        test_file_count=test_count,
        cron_tasks=_cron_tasks(root),
        sources=tuple(sources),
        cli_contracts=_cli_contracts(root),
    )
    serialize_evidence(evidence)
    return evidence


def serialize_evidence(evidence: RepositoryEvidence) -> bytes:
    """Return the exact compact bytes persisted and accepted by every reader."""

    payload = (evidence.model_dump_json() + "\n").encode("utf-8")
    if len(payload) > _MAX_EVIDENCE_PACKET_BYTES:
        raise ValueError("bounded README evidence packet exceeds its final serialized limit")
    return payload


def candidate_link_violations(markdown: str, repo_root: Path) -> tuple[str, ...]:
    """Reject broken, escaping, or absolute repository-local Markdown links."""

    root = repo_root.resolve()
    violations: list[str] = []
    for raw_target in _MARKDOWN_LINK.findall(markdown):
        target = raw_target.strip().strip("<>").split(maxsplit=1)[0]
        if not target or target.startswith(("#", "http://", "https://", "mailto:")):
            continue
        target = unquote(target).split("#", 1)[0]
        if not target or "<" in target or ">" in target:
            continue
        if Path(target).is_absolute() or re.match(r"^[A-Za-z]:", target):
            violations.append(f"absolute local link is not allowed: {raw_target}")
            continue
        resolved = (root / target).resolve()
        try:
            resolved.relative_to(root)
        except ValueError:
            violations.append(f"local link escapes the repository: {raw_target}")
            continue
        if not resolved.exists():
            violations.append(f"local link target does not exist: {raw_target}")
    return tuple(violations)


def _write_new_file(path: Path, payload: bytes) -> None:
    """Create one new regular file without following a pre-planted path."""

    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    fd = os.open(path, flags, 0o600)
    try:
        written = 0
        while written < len(payload):
            written += os.write(fd, payload[written:])
        os.fsync(fd)
    finally:
        os.close(fd)


def current_candidate_violations(markdown: str, repo_root: Path) -> tuple[str, ...]:
    return (
        *candidate_violations(markdown, _cli_contracts(repo_root)),
        *candidate_link_violations(markdown, repo_root),
    )


def _collect_llm_attestations(db_path: Path, run_id: str) -> tuple[LlmCallAttestation, ...]:
    conn = connect_sqlite(
        db_path,
        role=SQLiteConnectionRole.READ_ONLY,
        schema_preflight=False,
    )
    try:
        rows = conn.execute(
            "SELECT id,purpose,template_id,template_version,prompt_sha256,"
            "response_sha256,model,provider,transport FROM llm_calls "
            "WHERE run_id=? AND purpose IN (?,?) AND error IS NULL "
            "AND response_sha256 IS NOT NULL ORDER BY id",
            (run_id, "readme_update", "readme_update_judge"),
        ).fetchall()
    finally:
        conn.close()
    return tuple(
        LlmCallAttestation.model_validate(
            {
                "id": int(row[0]),
                "purpose": str(row[1]),
                "template_id": str(row[2] or ""),
                "template_version": str(row[3] or ""),
                "prompt_sha256": str(row[4]),
                "response_sha256": str(row[5]),
                "model": str(row[6]),
                "provider": None if row[7] is None else str(row[7]),
                "transport": None if row[8] is None else str(row[8]),
            }
        )
        for row in rows
    )


def _verify_llm_attestations(
    db_path: Path, run_id: str, expected: tuple[LlmCallAttestation, ...]
) -> None:
    current = _collect_llm_attestations(db_path, run_id)
    by_id = {row.id: row for row in current}
    if not expected or current != expected or any(by_id.get(row.id) != row for row in expected):
        raise ValueError("README approval ledger attestations are missing or changed")


def apply_approved_readme(
    *,
    readme_path: Path,
    expected_sha256: str,
    markdown: str,
    staging_directory: Path,
) -> bool:
    """Exclusively stage, then lock every compare/decision/replace operation."""

    payload = markdown.encode("utf-8")
    staging_directory.mkdir(parents=True, exist_ok=True)
    staging_path = staging_directory / f"README.{uuid4().hex}.pending"
    _write_new_file(staging_path, payload)
    try:
        with hold_run_lock(readme_path, owner="readme_updater", timeout_s=30.0):
            current_sha = _sha256_bytes(
                _read_regular_file(readme_path, limit=_MAX_STORED_CANDIDATE_BYTES)
            )
            candidate_sha = _sha256_bytes(payload)
            if current_sha == candidate_sha:
                return False
            if current_sha != expected_sha256:
                raise RuntimeError(
                    "README.md changed after evidence collection; refusing to overwrite"
                )
            os.replace(staging_path, readme_path)
            if (
                _sha256_bytes(_read_regular_file(readme_path, limit=_MAX_STORED_CANDIDATE_BYTES))
                != candidate_sha
            ):
                raise RuntimeError("README.md atomic write failed round-trip verification")
            return True
    finally:
        with contextlib.suppress(FileNotFoundError):
            staging_path.unlink()


def apply_stored_candidate(*, repo_root: Path, run_id: str, db_path: Path | None = None) -> bool:
    """Apply one exact approved preview; return False when it is already current."""

    if _RUN_ID.fullmatch(run_id) is None:
        raise ValueError("README updater run id must be 32 lowercase hexadecimal characters")
    root = repo_root.resolve()
    runs_root = root / ".tmp" / "readme_updater"
    run_dir = runs_root / run_id
    receipt_path = run_dir / "receipt.json"
    candidate_path = run_dir / "candidate.md"
    evidence_path = run_dir / "evidence.json"
    try:
        resolved_run_dir = run_dir.resolve(strict=True)
        if run_dir.is_symlink() or resolved_run_dir.parent != runs_root.resolve(strict=True):
            raise ValueError("README updater run directory must stay under its artifact root")
    except OSError as exc:
        raise ValueError("README updater run directory does not exist") from exc
    if not receipt_path.is_file() or not candidate_path.is_file() or not evidence_path.is_file():
        raise ValueError("README updater run does not contain its receipt, candidate, and evidence")
    if (
        receipt_path.is_symlink()
        or candidate_path.is_symlink()
        or evidence_path.is_symlink()
        or receipt_path.resolve(strict=True).parent != resolved_run_dir
        or candidate_path.resolve(strict=True).parent != resolved_run_dir
        or evidence_path.resolve(strict=True).parent != resolved_run_dir
    ):
        raise ValueError("README updater artifacts must be regular files inside the run directory")
    receipt = StoredReadmeReceipt.model_validate_json(
        _read_regular_file(receipt_path, limit=_MAX_STORED_RECEIPT_BYTES)
    )
    if receipt.run_id != run_id:
        raise ValueError("README updater receipt run id does not match its directory")
    candidate_bytes = _read_regular_file(candidate_path, limit=_MAX_STORED_CANDIDATE_BYTES)
    candidate = candidate_bytes.decode("utf-8")
    stored_evidence = RepositoryEvidence.model_validate_json(
        _read_regular_file(evidence_path, limit=_MAX_EVIDENCE_PACKET_BYTES)
    )
    if evidence_sha256(stored_evidence) != receipt.evidence_sha256:
        raise ValueError("stored README evidence does not match its receipt")
    if candidate != receipt.result.markdown:
        raise ValueError("stored README candidate does not match its judged receipt")
    current_evidence = collect_repository_evidence(root)
    violations = receipt_violations(
        receipt,
        candidate,
        stored_evidence=stored_evidence,
        current_evidence_sha256=evidence_sha256(current_evidence),
        candidate_validator=lambda markdown: current_candidate_violations(markdown, root),
    )
    if violations:
        raise ValueError(
            f"stored README candidate failed current deterministic checks: {violations}"
        )
    _verify_llm_attestations(db_path or root / "data" / "portfolio.db", run_id, receipt.llm_calls)
    return apply_approved_readme(
        readme_path=root / "README.md",
        expected_sha256=receipt.starting_readme_sha256,
        markdown=candidate,
        staging_directory=run_dir,
    )


def _event(name: str, **details: object) -> None:
    print(json.dumps({"event": name, **details}, sort_keys=True), file=sys.stderr)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    apply_group = parser.add_mutually_exclusive_group()
    apply_group.add_argument("--apply", action="store_true", help="Write an approved candidate")
    apply_group.add_argument(
        "--apply-run",
        metavar="RUN_ID",
        help="Apply the exact candidate from a previously approved preview",
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=PROJECT_ROOT,
        help="earnings-summary repository root",
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=PROJECT_ROOT / "data" / "portfolio.db",
        help="SQLite database used by the governed LLM ledger and budget checks",
    )
    parser.add_argument(
        "--max-revisions",
        type=int,
        choices=range(MAX_REVISIONS_PER_RUN + 1),
        default=MAX_REVISIONS_PER_RUN,
        help="Maximum judge-feedback revision rounds (run budget: 0 or 1)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    repo_root = args.repo_root.resolve()
    readme_path = repo_root / "README.md"
    if not readme_path.is_file():
        print(json.dumps({"error": f"README.md not found under {repo_root}"}))
        return 2

    if args.apply_run is not None:
        try:
            applied = apply_stored_candidate(
                repo_root=repo_root, run_id=args.apply_run, db_path=args.db
            )
            _event("readme_update_stored_candidate_applied", run_id=args.apply_run, applied=applied)
            print(json.dumps({"approved": True, "applied": applied, "run_id": args.apply_run}))
            return 0
        except Exception as exc:
            _event(
                "readme_update_stored_candidate_failed",
                run_id=args.apply_run,
                error_type=type(exc).__name__,
                error=redact(exc),
            )
            print(
                json.dumps(
                    {
                        "approved": False,
                        "applied": False,
                        "run_id": args.apply_run,
                        "error": f"{type(exc).__name__}: {redact(exc)}",
                    }
                )
            )
            return 1

    current_bytes = _read_regular_file(readme_path, limit=_MAX_STORED_CANDIDATE_BYTES)
    current_sha = _sha256_bytes(current_bytes)
    current_readme = current_bytes.decode("utf-8")
    run_id = uuid4().hex
    run_dir = repo_root / ".tmp" / "readme_updater" / run_id
    run_dir.mkdir(parents=True, exist_ok=False)

    try:
        evidence = collect_repository_evidence(repo_root)
        _event("readme_update_started", run_id=run_id, apply=bool(args.apply))
        result = run_update_cycle(
            evidence=evidence,
            current_readme=current_readme,
            max_revisions=args.max_revisions,
            db_path=str(args.db),
            run_id=run_id,
        )
        link_violations = candidate_link_violations(result.markdown, repo_root)
        attestations = _collect_llm_attestations(args.db, run_id)
        receipt = StoredReadmeReceipt(
            schema_version="2",
            run_id=run_id,
            starting_readme_sha256=current_sha,
            starting_readme=current_readme,
            evidence_sha256=evidence_sha256(evidence),
            release_approved=result.approved and not link_violations,
            link_violations=link_violations,
            llm_calls=attestations,
            result=result,
        )
        release_violations = receipt_violations(
            receipt,
            result.markdown,
            stored_evidence=evidence,
            current_evidence_sha256=evidence_sha256(evidence),
            candidate_validator=lambda markdown: current_candidate_violations(markdown, repo_root),
        )
        release_approved = receipt.release_approved and not release_violations
        if not release_approved:
            receipt = receipt.model_copy(update={"release_approved": False})
        _write_new_file(run_dir / "candidate.md", result.markdown.encode("utf-8"))
        _write_new_file(
            run_dir / "evidence.json",
            serialize_evidence(evidence),
        )
        _write_new_file(
            run_dir / "receipt.json",
            (receipt.model_dump_json(indent=2) + "\n").encode("utf-8"),
        )

        if not release_approved:
            _event(
                "readme_update_rejected",
                run_id=run_id,
                judge_verdict=result.final_judgment.verdict,
                link_violations=link_violations,
                release_violations=release_violations,
            )
            print(
                json.dumps(
                    {
                        "approved": False,
                        "applied": False,
                        "run_id": run_id,
                        "artifacts": str(run_dir),
                        "link_violations": link_violations,
                        "release_violations": release_violations,
                    },
                    indent=2,
                )
            )
            return 3

        applied = False
        if args.apply:
            applied = apply_approved_readme(
                readme_path=readme_path,
                expected_sha256=current_sha,
                markdown=result.markdown,
                staging_directory=run_dir,
            )
        _event("readme_update_approved", run_id=run_id, applied=applied)
        print(
            json.dumps(
                {
                    "approved": True,
                    "applied": applied,
                    "run_id": run_id,
                    "attempts": len(result.attempts),
                    "artifacts": str(run_dir),
                },
                indent=2,
            )
        )
        return 0
    except Exception as exc:
        _event(
            "readme_update_failed",
            run_id=run_id,
            error_type=type(exc).__name__,
            error=redact(exc),
        )
        print(
            json.dumps(
                {
                    "approved": False,
                    "applied": False,
                    "run_id": run_id,
                    "artifacts": str(run_dir),
                    "error": f"{type(exc).__name__}: {redact(exc)}",
                },
                indent=2,
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
