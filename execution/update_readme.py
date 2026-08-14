"""Generate and independently judge a project-specific README update.

Preview (always generates and judges; never writes README.md):
    python execution/sqlite_bootstrap.py execution/update_readme.py

Apply only a judge-approved candidate, with an atomic concurrent-edit guard:
    python execution/sqlite_bootstrap.py execution/update_readme.py --apply

Every candidate and judgment is retained under ``.tmp/readme_updater/<run_id>``.
The collector reads only an explicit allowlist of repository documentation and
source metadata; it never reads .env, credentials, data, outputs, or transcripts.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tomllib
from pathlib import Path
from urllib.parse import unquote
from uuid import uuid4

from pydantic import BaseModel, ConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from log_redact import redact  # noqa: E402
from readme_updater import (  # noqa: E402
    MAX_REVISIONS_PER_RUN,
    CliContract,
    EvidenceSource,
    RepositoryEvidence,
    run_update_cycle,
)
from run_lock import hold_run_lock  # noqa: E402

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
_MAX_EVIDENCE_CHARS = 130_000
_PATH_ROOTS = ("src", "execution", "directives", "cron", "docs", "alembic/versions", "tests")
_PATH_SUFFIXES = frozenset({".py", ".md", ".json", ".bat", ".toml", ".txt", ".yml", ".yaml"})
_MARKDOWN_LINK = re.compile(r"\[[^\]]+\]\(([^)]+)\)")
_ARGPARSE_OPTION = re.compile(r"add_argument\(\s*[\"'](--[a-z0-9][a-z0-9-]*)[\"']")
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


def _read_source(repo_root: Path, relative_path: str, limit: int) -> EvidenceSource | None:
    path = repo_root / relative_path
    if not path.is_file():
        return None
    raw = path.read_bytes()
    text = raw.decode("utf-8", errors="replace")
    return EvidenceSource(
        path=relative_path,
        sha256=_sha256_bytes(raw),
        text=text[:limit],
        truncated=len(text) > limit,
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
    return tuple(sorted(paths))


def _source_packages(repo_root: Path) -> tuple[str, ...]:
    src = repo_root / "src"
    if not src.exists():
        return ()
    names = {path.name for path in src.iterdir() if path.is_dir() and not path.name.startswith("_")}
    names.update(path.stem for path in src.glob("*.py") if not path.name.startswith("_"))
    return tuple(sorted(names))


def _cron_tasks(repo_root: Path) -> tuple[dict[str, str | None], ...]:
    path = repo_root / "cron" / "task_manifest.json"
    if not path.is_file():
        return ()
    manifest = _CronManifest.model_validate_json(path.read_text(encoding="utf-8"))
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
    return tuple(tasks)


def _cli_contracts(repo_root: Path) -> tuple[CliContract, ...]:
    contracts: list[CliContract] = []
    for relative_path in _CLI_CONTRACT_PATHS:
        path = repo_root / relative_path
        if not path.is_file():
            continue
        source = path.read_text(encoding="utf-8")
        contracts.append(
            CliContract(
                path=relative_path,
                options=tuple(sorted(set(_ARGPARSE_OPTION.findall(source)))),
            )
        )
    return tuple(contracts)


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
        payload = _Pyproject.model_validate(tomllib.loads(pyproject.read_text(encoding="utf-8")))
        if payload.project is not None and payload.project.name is not None:
            project_name = payload.project.name

    execution_root = root / "execution"
    execution_entrypoints = (
        tuple(sorted(path.name for path in execution_root.glob("*.py")))
        if execution_root.exists()
        else ()
    )
    tests_root = root / "tests"
    test_count = len(tuple(tests_root.glob("test_*.py"))) if tests_root.exists() else 0
    return RepositoryEvidence(
        project_name=project_name,
        tracked_paths=_tracked_paths(root),
        source_packages=_source_packages(root),
        execution_entrypoints=execution_entrypoints,
        test_file_count=test_count,
        cron_tasks=_cron_tasks(root),
        sources=tuple(sources),
        cli_contracts=_cli_contracts(root),
    )


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


def apply_approved_readme(
    *,
    readme_path: Path,
    expected_sha256: str,
    markdown: str,
    staging_path: Path,
) -> None:
    """Lock, compare, and atomically apply without losing another updater's edit."""

    staging_path.parent.mkdir(parents=True, exist_ok=True)
    staging_path.write_text(markdown, encoding="utf-8", newline="\n")
    with hold_run_lock(readme_path, owner="readme_updater", timeout_s=30.0):
        current_sha = _sha256_bytes(readme_path.read_bytes())
        if current_sha != expected_sha256:
            raise RuntimeError("README.md changed after evidence collection; refusing to overwrite")
        os.replace(staging_path, readme_path)
        expected_applied_sha = _sha256_bytes(markdown.encode("utf-8"))
        if _sha256_bytes(readme_path.read_bytes()) != expected_applied_sha:
            raise RuntimeError("README.md atomic write failed round-trip verification")


def _event(name: str, **details: object) -> None:
    print(json.dumps({"event": name, **details}, sort_keys=True), file=sys.stderr)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Write an approved candidate")
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

    current_bytes = readme_path.read_bytes()
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
        (run_dir / "candidate.md").write_text(result.markdown, encoding="utf-8", newline="\n")
        (run_dir / "receipt.json").write_text(
            json.dumps(
                {
                    "run_id": run_id,
                    "starting_readme_sha256": current_sha,
                    "result": result.model_dump(mode="json"),
                },
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
            newline="\n",
        )

        link_violations = candidate_link_violations(result.markdown, repo_root)
        if not result.approved or link_violations:
            _event(
                "readme_update_rejected",
                run_id=run_id,
                judge_verdict=result.final_judgment.verdict,
                link_violations=link_violations,
            )
            print(
                json.dumps(
                    {
                        "approved": False,
                        "applied": False,
                        "run_id": run_id,
                        "artifacts": str(run_dir),
                        "link_violations": link_violations,
                    },
                    indent=2,
                )
            )
            return 3

        applied = False
        if args.apply:
            apply_approved_readme(
                readme_path=readme_path,
                expected_sha256=current_sha,
                markdown=result.markdown,
                staging_path=run_dir / "README.md.pending",
            )
            applied = True
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
