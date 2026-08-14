"""Fail-closed README updater status for the Operations governance surface."""

from __future__ import annotations

import hashlib
import os
import re
import stat
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from readme_receipt import StoredReadmeReceipt
from readme_updater import (
    GENERATOR_PURPOSE,
    JUDGE_PURPOSE,
    RepositoryEvidence,
    build_generator_prompt,
    build_judge_prompt,
    evidence_sha256,
    judgment_passes,
    parse_readme_draft_response,
    parse_readme_judge_response,
    prior_feedback_for_attempt,
    prompt_identities,
)

ReadmeGovernanceState = Literal[
    "not_run", "approved_preview", "applied", "rejected", "stale", "invalid"
]
ReadmeGovernanceTone = Literal["ok", "warn", "bad"]

_RUN_ID = re.compile(r"[0-9a-f]{32}\Z")
_MAX_RECEIPT_BYTES = 1_000_000
_MAX_CANDIDATE_BYTES = 250_000
_MAX_EVIDENCE_BYTES = 110_000
_REPARSE_POINT = 0x400


class ReadmeGovernanceStatus(BaseModel):
    """Safe status projection; candidate prose and filesystem paths stay private."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    state: ReadmeGovernanceState
    tone: ReadmeGovernanceTone
    run_id: str | None
    verdict: Literal["pass", "revise"] | None
    attempts: int
    current_sha256: str | None
    candidate_sha256: str | None
    can_apply: bool
    recorded_at: datetime | None


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _read_regular_file(path: Path, *, limit: int) -> bytes:
    """Read at most ``limit`` bytes without accepting links or reparse files."""

    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise ValueError("README updater artifacts must be single-link regular files")
    if int(getattr(before, "st_file_attributes", 0)) & _REPARSE_POINT:
        raise ValueError("README updater artifacts may not be reparse points")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        opened = os.fstat(fd)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
        ):
            raise ValueError("README updater artifact changed while opening")
        payload = os.read(fd, limit + 1)
        if len(payload) > limit:
            raise ValueError("README updater artifact exceeds the validation limit")
        return payload
    finally:
        os.close(fd)


def receipt_violations(
    receipt: StoredReadmeReceipt,
    candidate: str,
    *,
    stored_evidence: RepositoryEvidence,
    current_evidence_sha256: str,
    candidate_validator: Callable[[str], tuple[str, ...]],
) -> tuple[str, ...]:
    """Shared release checks used by both status and apply."""

    violations: list[str] = []
    if receipt.release_approved is not True:
        violations.append("release gate did not approve the stored candidate")
    if receipt.evidence_sha256 != current_evidence_sha256:
        violations.append("repository evidence changed after approval")
    if receipt.evidence_sha256 != evidence_sha256(stored_evidence):
        violations.append("stored repository evidence does not match the receipt")
    if _sha256(receipt.starting_readme.encode("utf-8")) != receipt.starting_readme_sha256:
        violations.append("stored starting README does not match its receipt hash")
    if receipt.link_violations:
        violations.extend(receipt.link_violations)
    if (
        not receipt.result.approved
        or not receipt.result.attempts[-1].approved
        or not judgment_passes(receipt.result.final_judgment)
    ):
        violations.append("stored candidate was not approved by the judge")
    identities = prompt_identities()
    for purpose in (GENERATOR_PURPOSE, JUDGE_PURPOSE):
        expected_id, expected_version = identities[purpose]
        matching = [row for row in receipt.llm_calls if row.purpose == purpose]
        if len(matching) < len(receipt.result.attempts):
            violations.append(f"missing successful {purpose} ledger attestations")
        if any(
            row.template_id != expected_id or row.template_version != expected_version
            for row in matching
        ):
            violations.append(f"{purpose} prompt identity changed after approval")
    prior_feedback: str | None = None
    for attempt in receipt.result.attempts:
        try:
            parsed_draft = parse_readme_draft_response(attempt.generator_raw_response)
            parsed_judgment = parse_readme_judge_response(attempt.judge_raw_response)
        except (ValueError, ValidationError):
            violations.append(f"attempt {attempt.attempt} raw LLM response is invalid")
            continue
        if parsed_draft != attempt.draft or parsed_judgment != attempt.judgment:
            violations.append(f"attempt {attempt.attempt} typed output differs from raw response")
        generator_prompt = build_generator_prompt(
            stored_evidence, receipt.starting_readme, prior_feedback
        )
        judge_prompt = build_judge_prompt(stored_evidence, attempt.draft.markdown)
        if _sha256(generator_prompt.encode("utf-8")) != attempt.generator_prompt_sha256:
            violations.append(f"attempt {attempt.attempt} generator prompt cannot be reconstructed")
        if _sha256(judge_prompt.encode("utf-8")) != attempt.judge_prompt_sha256:
            violations.append(f"attempt {attempt.attempt} judge prompt cannot be reconstructed")
        if (
            _sha256(attempt.generator_raw_response.encode("utf-8"))
            != attempt.generator_response_sha256
        ):
            violations.append(f"attempt {attempt.attempt} generator response hash is invalid")
        if _sha256(attempt.judge_raw_response.encode("utf-8")) != attempt.judge_response_sha256:
            violations.append(f"attempt {attempt.attempt} judge response hash is invalid")
        expected_exchanges = (
            (
                GENERATOR_PURPOSE,
                attempt.generator_prompt_sha256,
                attempt.generator_response_sha256,
            ),
            (JUDGE_PURPOSE, attempt.judge_prompt_sha256, attempt.judge_response_sha256),
        )
        for purpose, prompt_sha, response_sha in expected_exchanges:
            matches = [
                row
                for row in receipt.llm_calls
                if row.purpose == purpose
                and row.prompt_sha256 == prompt_sha
                and row.response_sha256 == response_sha
            ]
            if len(matches) != 1:
                violations.append(
                    f"attempt {attempt.attempt} is not bound to one exact {purpose} ledger exchange"
                )
        prior_feedback = prior_feedback_for_attempt(attempt)
    violations.extend(candidate_validator(candidate))
    return tuple(violations)


def _status(
    *,
    state: ReadmeGovernanceState,
    tone: ReadmeGovernanceTone,
    current_sha256: str | None,
    run_id: str | None = None,
    verdict: Literal["pass", "revise"] | None = None,
    attempts: int = 0,
    candidate_sha256: str | None = None,
    can_apply: bool = False,
    recorded_at: datetime | None = None,
) -> ReadmeGovernanceStatus:
    return ReadmeGovernanceStatus(
        state=state,
        tone=tone,
        run_id=run_id,
        verdict=verdict,
        attempts=attempts,
        current_sha256=current_sha256,
        candidate_sha256=candidate_sha256,
        can_apply=can_apply,
        recorded_at=recorded_at,
    )


def collect_readme_governance_status(
    repo_root: Path,
    *,
    current_evidence_sha256: str,
    candidate_validator: Callable[[str], tuple[str, ...]],
) -> ReadmeGovernanceStatus:
    """Inspect README and the newest receipt using the current release checks."""

    root = repo_root.resolve()
    readme = root / "README.md"
    try:
        current_sha = _sha256(_read_regular_file(readme, limit=_MAX_CANDIDATE_BYTES))
    except (OSError, ValueError):
        return _status(state="invalid", tone="bad", current_sha256=None)
    runs_root = root / ".tmp" / "readme_updater"
    if not runs_root.is_dir():
        return _status(state="not_run", tone="warn", current_sha256=current_sha)
    run_dirs = tuple(
        path for path in runs_root.iterdir() if path.is_dir() and _RUN_ID.fullmatch(path.name)
    )
    if not run_dirs:
        return _status(state="not_run", tone="warn", current_sha256=current_sha)
    latest = max(run_dirs, key=lambda path: path.stat().st_mtime_ns)
    recorded_at = datetime.fromtimestamp(latest.stat().st_mtime, tz=UTC)
    receipt_path = latest / "receipt.json"
    candidate_path = latest / "candidate.md"
    evidence_path = latest / "evidence.json"
    try:
        resolved_runs_root = runs_root.resolve(strict=True)
        resolved_latest = latest.resolve(strict=True)
        if latest.is_symlink() or resolved_latest.parent != resolved_runs_root:
            raise ValueError("README updater run directory escaped its artifact root")
        receipt = StoredReadmeReceipt.model_validate_json(
            _read_regular_file(receipt_path, limit=_MAX_RECEIPT_BYTES)
        )
        if receipt.run_id != latest.name:
            raise ValueError("receipt run id does not match its directory")
        candidate_bytes = _read_regular_file(candidate_path, limit=_MAX_CANDIDATE_BYTES)
        candidate = candidate_bytes.decode("utf-8")
        stored_evidence = RepositoryEvidence.model_validate_json(
            _read_regular_file(evidence_path, limit=_MAX_EVIDENCE_BYTES)
        )
        if evidence_sha256(stored_evidence) != receipt.evidence_sha256:
            raise ValueError("stored evidence does not match its receipt")
        if candidate != receipt.result.markdown:
            raise ValueError("candidate does not match the judged receipt")
    except (OSError, UnicodeDecodeError, ValueError, ValidationError):
        return _status(
            state="invalid",
            tone="bad",
            current_sha256=current_sha,
            run_id=latest.name,
            recorded_at=recorded_at,
        )

    candidate_sha = _sha256(candidate_bytes)
    verdict = receipt.result.final_judgment.verdict
    attempts = len(receipt.result.attempts)

    def receipt_status(
        state: ReadmeGovernanceState,
        tone: ReadmeGovernanceTone,
        *,
        can_apply: bool = False,
    ) -> ReadmeGovernanceStatus:
        return _status(
            state=state,
            tone=tone,
            current_sha256=current_sha,
            run_id=receipt.run_id,
            verdict=verdict,
            attempts=attempts,
            candidate_sha256=candidate_sha,
            can_apply=can_apply,
            recorded_at=recorded_at,
        )

    if receipt_violations(
        receipt,
        candidate,
        stored_evidence=stored_evidence,
        current_evidence_sha256=current_evidence_sha256,
        candidate_validator=candidate_validator,
    ):
        return receipt_status("rejected", "warn")
    if current_sha == candidate_sha:
        return receipt_status("applied", "ok")
    if current_sha == receipt.starting_readme_sha256:
        return receipt_status("approved_preview", "ok", can_apply=True)
    return receipt_status("stale", "warn")


__all__ = [
    "ReadmeGovernanceStatus",
    "collect_readme_governance_status",
    "receipt_violations",
]
