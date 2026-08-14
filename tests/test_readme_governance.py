from __future__ import annotations

import hashlib
import json
from pathlib import Path

from operations.readme_governance import collect_readme_governance_status
from readme_updater import (
    GENERATOR_PURPOSE,
    JUDGE_PURPOSE,
    ReadmeDraft,
    ReadmeJudge,
    ReadmeUpdateAttempt,
    ReadmeUpdateResult,
    RepositoryEvidence,
    build_generator_prompt,
    build_judge_prompt,
    evidence_sha256,
    prompt_identities,
)


def _evidence() -> RepositoryEvidence:
    return RepositoryEvidence(
        project_name="earnings-summary",
        tracked_paths=(),
        source_packages=(),
        execution_entrypoints=(),
        test_file_count=0,
        cron_tasks=(),
        sources=(),
    )


_EVIDENCE_SHA = evidence_sha256(_evidence())


def _status(root: Path):
    return collect_readme_governance_status(
        root,
        current_evidence_sha256=_EVIDENCE_SHA,
        candidate_validator=lambda _candidate: (),
    )


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _write_receipt(
    root: Path,
    *,
    run_id: str,
    current: str,
    candidate: str,
    release_approved: bool = True,
) -> None:
    run_dir = root / ".tmp" / "readme_updater" / run_id
    run_dir.mkdir(parents=True)
    evidence = _evidence()
    draft = ReadmeDraft(
        markdown_lines=tuple(candidate.rstrip().splitlines()),
        change_summary=("Updated.",),
        evidence_gaps=(),
    )
    judgment = ReadmeJudge(
        verdict="pass",
        accuracy=5,
        usefulness=5,
        project_specificity=5,
        maintainability=5,
        safety=5,
        issues=(),
        rationale="Evidence-backed.",
    )
    generator_raw = draft.model_dump_json()
    judge_raw = judgment.model_dump_json()
    generator_prompt = build_generator_prompt(evidence, current)
    judge_prompt = build_judge_prompt(evidence, draft.markdown)
    result = ReadmeUpdateResult(
        approved=True,
        attempts=(
            ReadmeUpdateAttempt(
                attempt=1,
                draft=draft,
                deterministic_violations=(),
                judgment=judgment,
                approved=True,
                generator_prompt_sha256=_sha(generator_prompt),
                generator_response_sha256=_sha(generator_raw),
                judge_prompt_sha256=_sha(judge_prompt),
                judge_response_sha256=_sha(judge_raw),
                generator_raw_response=generator_raw,
                judge_raw_response=judge_raw,
            ),
        ),
    )
    actual_evidence_sha = evidence_sha256(evidence)
    (run_dir / "evidence.json").write_text(evidence.model_dump_json(), encoding="utf-8")
    (run_dir / "candidate.md").write_text(candidate, encoding="utf-8", newline="\n")
    identities = prompt_identities()
    attempt = result.attempts[0]
    calls = []
    for index, (purpose, prompt_sha, response_sha) in enumerate(
        (
            (GENERATOR_PURPOSE, attempt.generator_prompt_sha256, attempt.generator_response_sha256),
            (JUDGE_PURPOSE, attempt.judge_prompt_sha256, attempt.judge_response_sha256),
        ),
        start=1,
    ):
        calls.append(
            {
                "id": index,
                "purpose": purpose,
                "template_id": identities[purpose][0],
                "template_version": identities[purpose][1],
                "prompt_sha256": prompt_sha,
                "response_sha256": response_sha,
                "model": "test-model",
            }
        )
    (run_dir / "receipt.json").write_text(
        json.dumps(
            {
                "schema_version": "2",
                "run_id": run_id,
                "starting_readme_sha256": _sha(current),
                "starting_readme": current,
                "evidence_sha256": actual_evidence_sha,
                "release_approved": release_approved,
                "link_violations": [],
                "llm_calls": calls,
                "result": result.model_dump(mode="json"),
            }
        ),
        encoding="utf-8",
    )


def test_status_exposes_exact_approved_preview_that_can_be_applied(tmp_path: Path) -> None:
    current = "# Current\n"
    candidate = "# Candidate\n"
    run_id = "a" * 32
    (tmp_path / "README.md").write_text(current, encoding="utf-8", newline="\n")
    _write_receipt(tmp_path, run_id=run_id, current=current, candidate=candidate)

    status = _status(tmp_path)

    assert status.state == "approved_preview"
    assert status.run_id == run_id
    assert status.can_apply is True
    assert status.current_sha256 == _sha(current)
    assert status.candidate_sha256 == _sha(candidate)


def test_status_identifies_applied_candidate_and_disables_apply(tmp_path: Path) -> None:
    current = "# Current\n"
    candidate = "# Candidate\n"
    run_id = "b" * 32
    (tmp_path / "README.md").write_text(candidate, encoding="utf-8", newline="\n")
    _write_receipt(tmp_path, run_id=run_id, current=current, candidate=candidate)

    status = _status(tmp_path)

    assert status.state == "applied"
    assert status.can_apply is False


def test_status_does_not_promote_release_rejected_receipt(tmp_path: Path) -> None:
    current = "# Current\n"
    candidate = "# Candidate\n"
    run_id = "9" * 32
    (tmp_path / "README.md").write_text(current, encoding="utf-8", newline="\n")
    _write_receipt(
        tmp_path,
        run_id=run_id,
        current=current,
        candidate=candidate,
        release_approved=False,
    )

    status = _status(tmp_path)

    assert status.state == "rejected"
    assert status.can_apply is False


def test_status_fails_closed_on_invalid_or_oversized_receipt(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("# Current\n", encoding="utf-8", newline="\n")
    run_dir = tmp_path / ".tmp" / "readme_updater" / ("c" * 32)
    run_dir.mkdir(parents=True)
    (run_dir / "receipt.json").write_bytes(b"{" + b"x" * 1_100_000)

    status = _status(tmp_path)

    assert status.state == "invalid"
    assert status.can_apply is False


def test_status_rechecks_current_deterministic_candidate_contract(tmp_path: Path) -> None:
    current = "# Current\n"
    candidate = "# Candidate\n"
    run_id = "d" * 32
    (tmp_path / "README.md").write_text(current, encoding="utf-8", newline="\n")
    _write_receipt(tmp_path, run_id=run_id, current=current, candidate=candidate)

    status = collect_readme_governance_status(
        tmp_path,
        current_evidence_sha256=_EVIDENCE_SHA,
        candidate_validator=lambda _candidate: ("linked target disappeared",),
    )

    assert status.state == "rejected"
    assert status.can_apply is False


def test_status_expires_when_repository_evidence_changes(tmp_path: Path) -> None:
    current = "# Current\n"
    candidate = "# Candidate\n"
    run_id = "e" * 32
    (tmp_path / "README.md").write_text(current, encoding="utf-8", newline="\n")
    _write_receipt(tmp_path, run_id=run_id, current=current, candidate=candidate)

    status = collect_readme_governance_status(
        tmp_path,
        current_evidence_sha256="f" * 64,
        candidate_validator=lambda _candidate: (),
    )

    assert status.state == "rejected"
    assert status.can_apply is False


def test_status_rejects_unrelated_llm_ledger_attestations(tmp_path: Path) -> None:
    current = "# Current\n"
    candidate = "# Candidate\n"
    run_id = "8" * 32
    (tmp_path / "README.md").write_text(current, encoding="utf-8", newline="\n")
    _write_receipt(tmp_path, run_id=run_id, current=current, candidate=candidate)
    receipt_path = tmp_path / ".tmp" / "readme_updater" / run_id / "receipt.json"
    payload = json.loads(receipt_path.read_text(encoding="utf-8"))
    payload["llm_calls"][0]["response_sha256"] = "9" * 64
    receipt_path.write_text(json.dumps(payload), encoding="utf-8")

    status = _status(tmp_path)

    assert status.state == "rejected"
    assert status.can_apply is False


def test_readme_eval_corpus_joins_candidate_to_exact_evidence(tmp_path: Path) -> None:
    from evals.corpora import load_readme_update_corpus

    current = "# Current\n"
    candidate = "# Candidate\n"
    run_id = "f" * 32
    (tmp_path / "README.md").write_text(current, encoding="utf-8", newline="\n")
    _write_receipt(tmp_path, run_id=run_id, current=current, candidate=candidate)

    items = load_readme_update_corpus(tmp_path)

    assert len(items) == 1
    payload = json.loads(items[0].content)
    assert payload["readme_candidate"] == candidate
    assert payload["repository_evidence"] == _evidence().model_dump(mode="json")


def test_readme_eval_corpus_rejects_linked_run_directory(tmp_path: Path) -> None:
    from evals.corpora import load_readme_update_corpus

    run_id = "7" * 32
    external = tmp_path / "external"
    external.mkdir()
    _write_receipt(
        external,
        run_id=run_id,
        current="# Current\n",
        candidate="# Private candidate\n",
    )
    runs_root = tmp_path / "repo" / ".tmp" / "readme_updater"
    runs_root.mkdir(parents=True)
    try:
        (runs_root / run_id).symlink_to(
            external / ".tmp" / "readme_updater" / run_id,
            target_is_directory=True,
        )
    except OSError:
        import pytest

        pytest.skip("directory symlinks are unavailable on this Windows configuration")

    assert load_readme_update_corpus(tmp_path / "repo") == []
