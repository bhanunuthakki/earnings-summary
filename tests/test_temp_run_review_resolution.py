"""Reviewed lifecycle closure preserves failures and excludes unrelated state."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal
from uuid import uuid4

import pytest

from src.operations import temp_run_retention as runs

NOW = datetime(2026, 10, 18, tzinfo=UTC)
PURPOSE = "synthetic reviewed recovery regression"


class LeaseLedger:
    """Keep lease state visible without allocating shared host reservations."""

    def __init__(self) -> None:
        self.held: dict[str, tuple[Path, int]] = {}
        self.released: list[str] = []

    def reserve(self, root: Path, expected_bytes: int, **_options: int) -> str:
        token = uuid4().hex
        self.held[token] = (root, expected_bytes)
        return token

    def release(self, root: Path, token: str) -> None:
        assert self.held[token][0] == root
        self.held.pop(token)
        self.released.append(token)


@pytest.fixture
def leases(monkeypatch: pytest.MonkeyPatch) -> LeaseLedger:
    ledger = LeaseLedger()
    monkeypatch.setattr(runs, "reserve_peak", ledger.reserve)
    monkeypatch.setattr(runs, "release_peak", ledger.release)
    return ledger


def begin(
    repo: Path,
    name: str,
    *,
    purpose: str = PURPOSE,
    disposable_class: Literal["declared_files", "isolated_test_fixture"] = "isolated_test_fixture",
) -> tuple[Path, str]:
    root = repo / ".tmp" / name
    manifest = runs.begin_temp_run(
        root,
        repo_root=repo,
        now=NOW,
        disposable_class=disposable_class,
        purpose=purpose,
        expected_bytes=100,
        byte_budget=100,
        min_free_bytes=0,
    )
    assert manifest.reservation_token is not None
    (root / "payload.bin").write_bytes(b"synthetic payload")
    return root, manifest.reservation_token


def finish(repo: Path, root: Path, *, success: bool) -> None:
    runs.finish_temp_run(
        root,
        repo_root=repo,
        success=success,
        disposable_paths=(root / "payload.bin",),
        now=NOW + timedelta(minutes=2 if success else 1),
    )


def test_failed_lease_stays_reserved_until_reviewed_successful_rerun(
    tmp_path: Path, leases: LeaseLedger, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"

    def search_roots(_repo: Path, _code: Path | None) -> list[tuple[Path, bool]]:
        return [(repo / ".tmp", False)]

    monkeypatch.setattr(runs, "_search_roots", search_roots)
    failed, failed_token = begin(repo, "failed")
    finish(repo, failed, success=False)
    original = (failed / runs.MANIFEST_NAME).read_bytes()
    assert failed_token in leases.held
    assert failed_token not in leases.released
    successful, successful_token = begin(repo, "successful")
    with pytest.raises(ValueError, match="unverified_failure_resolution"):
        runs.resolve_failed_temp_run(
            failed,
            successful_root=successful,
            repo_root=repo,
            disposable_paths=(failed / "payload.bin",),
            reason="unfinished rerun is insufficient",
            reviewed_by="synthetic reviewer",
            now=NOW + timedelta(minutes=3),
        )
    assert failed_token in leases.held
    assert not (failed / runs.RESOLUTION_NAME).exists()
    finish(repo, successful, success=True)
    assert successful_token not in leases.held
    resolution = runs.resolve_failed_temp_run(
        failed,
        successful_root=successful,
        repo_root=repo,
        disposable_paths=(failed / "payload.bin",),
        reason="reviewed successful rerun",
        reviewed_by="synthetic reviewer",
        now=NOW + timedelta(minutes=3),
    )
    assert failed_token not in leases.held
    assert leases.released.count(failed_token) == 1
    assert (failed / runs.MANIFEST_NAME).read_bytes() == original
    assert resolution.failed_manifest_sha256 == hashlib.sha256(original).hexdigest()
    assert resolution.successful_manifest.path == successful / runs.MANIFEST_NAME
    assert (failed / "payload.bin").read_bytes() == b"synthetic payload"
    discovery = runs.discover_temp_runs(repo)
    report = next(item for item in discovery.reports if item.root == failed)
    assert report.resolution_status == "resolved"
    assert not report.problems


@pytest.mark.parametrize("mismatch", ["purpose", "class", "failed_pin", "successful_pin"])
def test_mismatched_or_pinned_rerun_cannot_release_failed_lease(
    tmp_path: Path, leases: LeaseLedger, mismatch: str
) -> None:
    repo = tmp_path / "repo"
    failed, token = begin(repo, "failed")
    finish(repo, failed, success=False)
    successful, _ = begin(
        repo,
        "successful",
        purpose="different purpose" if mismatch == "purpose" else PURPOSE,
        disposable_class="declared_files" if mismatch == "class" else "isolated_test_fixture",
    )
    finish(repo, successful, success=True)
    if mismatch.endswith("_pin"):
        pinned = failed if mismatch == "failed_pin" else successful
        path = pinned / runs.MANIFEST_NAME
        manifest = json.loads(path.read_bytes())
        manifest["pins"] = ["owner-review"]
        path.write_text(json.dumps(manifest), encoding="utf-8")
    original = (failed / runs.MANIFEST_NAME).read_bytes()
    with pytest.raises(ValueError, match="unverified_failure_resolution"):
        runs.resolve_failed_temp_run(
            failed,
            successful_root=successful,
            repo_root=repo,
            disposable_paths=(failed / "payload.bin",),
            reason="synthetic attempted resolution",
            reviewed_by="synthetic reviewer",
            now=NOW + timedelta(minutes=3),
        )
    assert token in leases.held
    assert token not in leases.released
    assert (failed / runs.MANIFEST_NAME).read_bytes() == original
    assert not (failed / runs.RESOLUTION_NAME).exists()
    assert (failed / "payload.bin").read_bytes() == b"synthetic payload"


def reviewed_file(repo: Path, root: Path, relative: Path, *, isolated: bool = False) -> Path:
    payload = (root / relative).read_bytes()
    review = runs.TempRunReview(
        run_root=root,
        completed_at=NOW,
        purpose=PURPOSE,
        reviewed_by="synthetic reviewer",
        reason="exact synthetic file inventory",
        disposable_class="isolated_test_fixture" if isolated else "declared_files",
        fixture_origin="synthetic_generated" if isolated else None,
        files=[
            runs.ReviewedTestFile(
                relative_path=relative,
                sha256=hashlib.sha256(payload).hexdigest(),
                size=len(payload),
            )
        ],
    )
    path = repo / "review.json"
    path.write_text(review.model_dump_json(), encoding="utf-8")
    return path


def test_exact_review_adopts_only_declared_bytes_with_bound_review_proof(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    root = repo / ".tmp" / "reviewed"
    root.mkdir(parents=True)
    payload, unknown = root / "payload.bin", root / "unknown.bin"
    payload.write_bytes(b"synthetic declared")
    unknown.write_bytes(b"unclassified owner data")
    review = reviewed_file(repo, root, Path("payload.bin"))
    manifest = runs.register_reviewed_test_run(review, repo_root=repo)
    assert [item.relative_path for item in manifest.files] == [Path("payload.bin")]
    assert manifest.retirement_proofs[0].sha256 == hashlib.sha256(review.read_bytes()).hexdigest()
    assert unknown.read_bytes() == b"unclassified owner data"
    assert payload.read_bytes() == b"synthetic declared"


def test_review_cannot_adopt_source_owned_file(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    root = repo / ".tmp" / "source-owned"
    source = root / "src" / "unique.py"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"unique owner source")
    review = reviewed_file(repo, root, Path("src/unique.py"))
    with pytest.raises(ValueError, match="protected_source"):
        runs.register_reviewed_test_run(review, repo_root=repo)
    assert source.read_bytes() == b"unique owner source"
    assert not (root / runs.MANIFEST_NAME).exists()


def test_review_cannot_adopt_fixture_with_external_git_storage(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    root = repo / ".tmp" / "git-fixture"
    root.mkdir(parents=True)
    external = tmp_path / "external-git"
    external.mkdir()
    (external / "sentinel").write_bytes(b"external owner evidence")
    gitfile = root / ".git"
    gitfile.write_text(f"gitdir: {external}\n", encoding="utf-8")
    payload = root / "payload.bin"
    payload.write_bytes(b"synthetic declared")
    review = reviewed_file(repo, root, Path("payload.bin"), isolated=True)
    original_gitfile = gitfile.read_bytes()
    with pytest.raises(ValueError, match="reviewed_fixture_storage_unsafe"):
        runs.register_reviewed_test_run(review, repo_root=repo)
    assert payload.read_bytes() == b"synthetic declared"
    assert gitfile.read_bytes() == original_gitfile
    assert (external / "sentinel").read_bytes() == b"external owner evidence"
    assert not (root / runs.MANIFEST_NAME).exists()


def test_review_cannot_adopt_linked_payload_directory(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    root = repo / ".tmp" / "linked-fixture"
    root.mkdir(parents=True)
    external = tmp_path / "external"
    external.mkdir()
    (external / "sentinel.bin").write_bytes(b"external owner evidence")
    alias = root / "alias"
    if os.name == "nt":
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(alias), str(external)],
            check=True,
            capture_output=True,
            timeout=5,
        )
    else:
        alias.symlink_to(external, target_is_directory=True)
    try:
        review = reviewed_file(repo, root, Path("alias/sentinel.bin"), isolated=True)
        with pytest.raises(ValueError, match="reviewed_fixture_storage_unsafe"):
            runs.register_reviewed_test_run(review, repo_root=repo)
        assert (external / "sentinel.bin").read_bytes() == b"external owner evidence"
        assert not (root / runs.MANIFEST_NAME).exists()
    finally:
        if os.name == "nt":
            alias.rmdir()
        else:
            alias.unlink()
