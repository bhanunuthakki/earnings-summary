from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from typing import Literal, cast

import pytest

from readme_updater import (
    GENERATOR_PURPOSE,
    JUDGE_PURPOSE,
    CliContract,
    EvidenceSource,
    ReadmeDraft,
    ReadmeJudge,
    ReadmeJudgeIssue,
    RepositoryEvidence,
    build_judge_prompt,
    candidate_violations,
    run_update_cycle,
)


def _evidence() -> RepositoryEvidence:
    return RepositoryEvidence(
        project_name="earnings-summary",
        tracked_paths=(
            "AGENTS.md",
            "HOW_TO_USE_REPORTS.md",
            "README.md",
            "cron/task_manifest.json",
            "execution/comments_server.py",
            "src/llm/cli.py",
        ),
        source_packages=("ask", "compute", "llm", "pipeline", "report", "ui"),
        execution_entrypoints=("comments_server.py", "run_morning_pipeline.py"),
        test_file_count=42,
        cron_tasks=(
            {
                "task_name": "morning_pipeline",
                "wrapper": "run_morning_pipeline.bat",
                "trigger": "CalendarTrigger",
                "start_boundary": "2026-08-13T04:00:00",
            },
        ),
        sources=(
            EvidenceSource(
                path="AGENTS.md",
                sha256="a" * 64,
                text="Solo-built localhost equity research platform.",
                truncated=False,
            ),
        ),
    )


def _markdown(label: str = "approved") -> str:
    padding = "\n".join(f"Repository-grounded detail {index}." for index in range(160))
    return f"""# Earnings Summary

## Overview

{label}: localhost equity research workspace.

## Quick start

Run the managed Python command.

## How it works

The deterministic pipeline persists evidence before synthesis.

## Operations

Scheduled and interactive writers share the same state boundaries.

## Development

Run focused tests before the full check.

## Security

Keep credentials out of the repository.

{padding}
"""


def _draft(label: str) -> ReadmeDraft:
    return ReadmeDraft(
        markdown_lines=tuple(_markdown(label).splitlines()),
        change_summary=(f"Produced {label} candidate",),
        evidence_gaps=(),
    )


def _judge(verdict: Literal["pass", "revise"], *, issue: bool = False) -> ReadmeJudge:
    issues = (
        (
            ReadmeJudgeIssue(
                severity="major",
                claim="The startup command is unsupported.",
                evidence_path="start_comments_server.bat",
                recommendation="Use the managed launcher.",
            ),
        )
        if issue
        else ()
    )
    score = 5 if verdict == "pass" else 3
    return ReadmeJudge(
        verdict=verdict,
        accuracy=score,
        usefulness=score,
        project_specificity=score,
        maintainability=score,
        safety=5,
        issues=issues,
        rationale="Grounded in the supplied evidence.",
    )


def test_update_cycle_judges_every_candidate_and_revises_once() -> None:
    calls: list[tuple[str, str]] = []

    def caller(prompt: str, **kwargs: object) -> object:
        purpose = cast("str", kwargs["purpose"])
        calls.append((purpose, prompt))
        if purpose == GENERATOR_PURPOSE:
            return _draft("first" if len(calls) == 1 else "revised")
        if len([name for name, _ in calls if name == JUDGE_PURPOSE]) == 1:
            return _judge("revise", issue=True)
        return _judge("pass")

    result = run_update_cycle(
        evidence=_evidence(),
        current_readme=_markdown("current"),
        caller=caller,
        max_revisions=1,
    )

    assert [purpose for purpose, _ in calls] == [
        GENERATOR_PURPOSE,
        JUDGE_PURPOSE,
        GENERATOR_PURPOSE,
        JUDGE_PURPOSE,
    ]
    assert result.approved is True
    assert "revised" in result.markdown
    assert len(result.attempts) == 2
    assert "startup command is unsupported" in calls[2][1]


def test_update_cycle_fails_closed_when_judge_never_passes() -> None:
    judged = 0

    def caller(_prompt: str, **kwargs: object) -> object:
        nonlocal judged
        if kwargs["purpose"] == GENERATOR_PURPOSE:
            return _draft("candidate")
        judged += 1
        return _judge("revise", issue=True)

    result = run_update_cycle(
        evidence=_evidence(),
        current_readme=_markdown("current"),
        caller=caller,
        max_revisions=1,
    )

    assert result.approved is False
    assert judged == 2
    assert len(result.attempts) == 2


def test_judge_prompt_is_independent_and_spotlights_candidate() -> None:
    prompt = build_judge_prompt(_evidence(), _markdown("candidate"))

    assert "candidate" in prompt
    assert "UNTRUSTED CONTENT" in prompt
    assert "change_summary" not in prompt
    assert "readme_update_judge" not in prompt


@pytest.mark.parametrize(
    ("markdown", "expected"),
    [
        ("# Wrong\n", "title"),
        (_markdown().replace("## Security", "## Safeguards"), "required section"),
        (_markdown() + "\nC:\\Users\\someone\\secret.txt\n", "absolute Windows path"),
    ],
)
def test_candidate_contract_rejects_unsafe_or_incomplete_markdown(
    markdown: str,
    expected: str,
) -> None:
    assert any(expected in violation for violation in candidate_violations(markdown))


def test_candidate_contract_rejects_unsupported_project_cli_flags() -> None:
    contracts = (
        CliContract(
            path="execution/upgrade_database.py",
            options=("--backup-path", "--db-path", "--repo-root"),
        ),
    )
    markdown = _markdown() + (
        "\n```powershell\npython execution/sqlite_bootstrap.py "
        "execution/upgrade_database.py --db data/portfolio.db --repo-root .\n```\n"
    )

    violations = candidate_violations(markdown, contracts)

    assert any("unsupported option --db" in violation for violation in violations)


def test_apply_requires_the_readme_hash_to_remain_unchanged(tmp_path: Path) -> None:
    from execution.update_readme import apply_approved_readme

    readme = tmp_path / "README.md"
    readme.write_text(_markdown("original"), encoding="utf-8")
    expected_sha = hashlib.sha256(readme.read_bytes()).hexdigest()
    readme.write_text(_markdown("concurrent edit"), encoding="utf-8")

    with pytest.raises(RuntimeError, match="changed after evidence collection"):
        apply_approved_readme(
            readme_path=readme,
            expected_sha256=expected_sha,
            markdown=_markdown("candidate"),
            staging_path=tmp_path / "candidate.pending",
        )


def test_concurrent_applies_serialize_and_one_refuses_stale_bytes(tmp_path: Path) -> None:
    from execution.update_readme import apply_approved_readme

    readme = tmp_path / "README.md"
    readme.write_text(_markdown("original"), encoding="utf-8")
    expected_sha = hashlib.sha256(readme.read_bytes()).hexdigest()
    barrier = Barrier(2)

    def apply(label: str) -> str:
        barrier.wait(timeout=5)
        try:
            apply_approved_readme(
                readme_path=readme,
                expected_sha256=expected_sha,
                markdown=_markdown(label),
                staging_path=tmp_path / f"{label}.pending",
            )
        except RuntimeError as exc:
            return str(exc)
        return "applied"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = sorted(pool.map(apply, ("candidate-a", "candidate-b")))

    assert results == [
        "README.md changed after evidence collection; refusing to overwrite",
        "applied",
    ]
    final = readme.read_text(encoding="utf-8")
    assert ("candidate-a" in final) ^ ("candidate-b" in final)
    assert not readme.with_name("README.md.write.lock").exists()


def test_evidence_collection_is_bounded_and_excludes_secret_files(tmp_path: Path) -> None:
    from execution.update_readme import collect_repository_evidence

    (tmp_path / "src").mkdir()
    (tmp_path / "execution").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "cron").mkdir()
    (tmp_path / "directives").mkdir()
    (tmp_path / "AGENTS.md").write_text("project rules", encoding="utf-8")
    (tmp_path / "HOW_TO_USE_REPORTS.md").write_text("daily workflow", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text('[project]\nname="x"\n', encoding="utf-8")
    (tmp_path / "start_comments_server.bat").write_text("python server", encoding="utf-8")
    (tmp_path / "directives" / "README.md").write_text("directive index", encoding="utf-8")
    (tmp_path / "cron" / "task_manifest.json").write_text(
        '{"tasks": [{"task_name": "x", "wrapper": "x.bat", '
        '"schedule": {"trigger": "CalendarTrigger", "start_boundary": null}}]}',
        encoding="utf-8",
    )
    (tmp_path / ".env").write_text("SECRET=do-not-read", encoding="utf-8")
    (tmp_path / "src" / "alpha.py").write_text("", encoding="utf-8")
    (tmp_path / "execution" / "run_alpha.py").write_text("", encoding="utf-8")
    (tmp_path / "tests" / "test_alpha.py").write_text("", encoding="utf-8")

    evidence = collect_repository_evidence(tmp_path)
    payload = evidence.model_dump_json()

    assert "do-not-read" not in payload
    assert evidence.cron_tasks[0]["task_name"] == "x"
    assert evidence.execution_entrypoints == ("run_alpha.py",)
    assert evidence.test_file_count == 1


def test_llm_purposes_are_versioned_isolated_and_model_pinned() -> None:
    from evals.coverage import META_PURPOSES
    from llm.capture import CAPTURE_DENYLIST
    from llm.cli import DEFAULT_MODEL, LLM_MODELS
    from llm.prompt_versions import registered_purposes

    assert LLM_MODELS[GENERATOR_PURPOSE] == DEFAULT_MODEL
    assert LLM_MODELS[JUDGE_PURPOSE] == "claude-opus-4-8"
    assert {GENERATOR_PURPOSE, JUDGE_PURPOSE} <= registered_purposes()
    assert {GENERATOR_PURPOSE, JUDGE_PURPOSE} <= META_PURPOSES
    assert {GENERATOR_PURPOSE, JUDGE_PURPOSE} <= CAPTURE_DENYLIST
