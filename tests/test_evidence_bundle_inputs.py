"""Hermetic dependency-input tests for evidence collection."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import cast

from quality.evidence_bundle import ALLOWED_SOURCE_PATHS, ArtifactSpec, collect_evidence
from quality.git_env import clean_local_git_env


def _run(repo: Path, *args: str) -> None:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        check=False,
        env=clean_local_git_env(),
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")


def _repo(tmp_path: Path, *, include_claims: bool = False) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    (repo / "docs" / "quality").mkdir(parents=True)
    (repo / "docs" / "quality" / "quality-9plus-roadmap.md").write_text(
        "# roadmap\n", encoding="utf-8"
    )
    if include_claims:
        (repo / "config").mkdir()
        (repo / "config" / "quality_roadmap_claims.json").write_text(
            '{"schema_version":"roadmap-claim-map-v1"}\n', encoding="utf-8"
        )
    (repo / ".tmp").mkdir()
    (repo / ".gitignore").write_text(".tmp/\n", encoding="utf-8")
    (repo / "roadmap.txt").write_text("subject\n", encoding="utf-8")
    _run(repo, "init", "-b", "main")
    _run(repo, "config", "user.email", "test@example.com")
    _run(repo, "config", "user.name", "Test")
    _run(repo, "add", "-A")
    _run(repo, "commit", "-m", "init")
    _run(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    return repo


def _specs() -> tuple[ArtifactSpec, ...]:
    return (
        ArtifactSpec(
            artifact_id="alpha",
            canonical_path=ALLOWED_SOURCE_PATHS[0],
            command=("alpha",),
            native_scope="WORKTREE",
        ),
        ArtifactSpec(
            artifact_id="beta",
            canonical_path=ALLOWED_SOURCE_PATHS[1],
            command=("beta",),
            native_scope="WORKTREE",
            depends_on=("alpha",),
            input_manifest_flag="--input-manifest",
        ),
    )


def test_dependent_receives_fresh_manifest_and_records_actual_command(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    staging = repo / ".tmp" / "quality" / "inputs"

    def run(argv: tuple[str, ...], root: Path) -> subprocess.CompletedProcess[bytes]:
        if argv[0] == "alpha":
            return subprocess.CompletedProcess(argv, 0, b"alpha-fresh\n", b"")
        assert argv[0] == "beta"
        manifest_path = Path(argv[argv.index("--input-manifest") + 1])
        payload = cast(dict[str, object], json.loads(manifest_path.read_text(encoding="utf-8")))
        assert payload == {
            "alpha": {
                "path": "alpha.raw",
                "sha256": hashlib.sha256(b"alpha-fresh\n").hexdigest(),
            }
        }
        assert (staging / "alpha.raw").read_bytes() == b"alpha-fresh\n"
        return subprocess.CompletedProcess(argv, 0, b"beta-fresh\n", b"")

    # The fixture assertion above uses the digest of the actual bytes.
    expected = hashlib.sha256(b"alpha-fresh\n").hexdigest()

    def checked_run(argv: tuple[str, ...], root: Path) -> subprocess.CompletedProcess[bytes]:
        if argv[0] == "beta":
            manifest_path = Path(argv[argv.index("--input-manifest") + 1])
            payload = cast(
                dict[str, dict[str, str]], json.loads(manifest_path.read_text(encoding="utf-8"))
            )
            assert payload["alpha"]["sha256"] == expected
            assert (staging / "alpha.raw").read_bytes() == b"alpha-fresh\n"
        return run(argv, root)

    manifest = collect_evidence(repo, staging, _specs(), runner=checked_run)
    assert manifest.status == "COMPLETE"
    beta = next(record for record in manifest.artifacts if record.artifact_id == "beta")
    assert "--input-manifest" in beta.command
    assert str(staging / "beta.inputs.json") in beta.command
    assert "input_manifest_flag" not in beta.model_dump_json()


def test_dependency_mutation_is_hold_and_not_masked_by_final_write(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    staging = repo / ".tmp" / "quality" / "inputs"

    def run(argv: tuple[str, ...], root: Path) -> subprocess.CompletedProcess[bytes]:
        if argv[0] == "beta":
            (staging / "alpha.raw").write_bytes(b"tampered\n")
        return subprocess.CompletedProcess(argv, 0, b"payload\n", b"")

    manifest = collect_evidence(repo, staging, _specs(), runner=run)
    assert manifest.status == "HOLD"
    assert any("input dependency changed" in item for item in manifest.violations)
    assert (staging / "alpha.raw").read_bytes() == b"tampered\n"


def test_roadmap_context_is_exact_bytes_in_staged_manifest(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    staging = repo / ".tmp" / "quality" / "inputs"
    specs = (
        *_specs()[:1],
        _specs()[1].model_copy(
            update={
                "roadmap_context_path": "docs/quality/quality-9plus-roadmap.md",
            }
        ),
    )

    def run(argv: tuple[str, ...], root: Path) -> subprocess.CompletedProcess[bytes]:
        if argv[0] == "beta":
            manifest_path = Path(argv[argv.index("--input-manifest") + 1])
            payload = cast(dict[str, dict[str, str]], json.loads(manifest_path.read_text()))
            assert payload["roadmap"]["path"] == "roadmap.context.md"
            assert (staging / payload["roadmap"]["path"]).read_bytes() == (
                repo / "docs/quality/quality-9plus-roadmap.md"
            ).read_bytes()
        return subprocess.CompletedProcess(argv, 0, b"ok\n", b"")

    manifest = collect_evidence(repo, staging, specs, runner=run)
    assert manifest.status == "COMPLETE"


def test_present_claim_map_is_subject_blob_and_missing_map_is_legacy_valid(tmp_path: Path) -> None:
    repo = _repo(tmp_path, include_claims=True)
    staging = repo / ".tmp" / "quality" / "inputs"
    specs = (
        *_specs()[:1],
        _specs()[1].model_copy(
            update={"roadmap_context_path": "docs/quality/quality-9plus-roadmap.md"}
        ),
    )

    def run(argv: tuple[str, ...], root: Path) -> subprocess.CompletedProcess[bytes]:
        if argv[0] == "beta":
            manifest_path = Path(argv[argv.index("--input-manifest") + 1])
            payload = cast(dict[str, dict[str, str]], json.loads(manifest_path.read_text()))
            assert payload["roadmap_claims"]["path"] == "roadmap.claims.json"
            assert (staging / "roadmap.claims.json").read_bytes() == (
                repo / "config/quality_roadmap_claims.json"
            ).read_bytes()
        return subprocess.CompletedProcess(argv, 0, b"ok\n", b"")

    manifest = collect_evidence(repo, staging, specs, runner=run)
    assert manifest.status == "COMPLETE"

    legacy = _repo(tmp_path / "legacy")
    legacy_staging = legacy / ".tmp" / "quality" / "inputs"

    def legacy_run(argv: tuple[str, ...], root: Path) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(argv, 0, b"ok\n", b"")

    legacy_manifest = collect_evidence(legacy, legacy_staging, specs, runner=legacy_run)
    assert legacy_manifest.status == "COMPLETE"
    assert "roadmap_claims" not in (legacy_staging / "beta.inputs.json").read_text()


def test_transient_roadmap_change_cannot_be_masked_by_restore(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    staging = repo / ".tmp" / "quality" / "inputs"
    specs = (
        *_specs()[:1],
        _specs()[1].model_copy(
            update={
                "roadmap_context_path": "docs/quality/quality-9plus-roadmap.md",
            }
        ),
    )
    context = repo / "docs/quality/quality-9plus-roadmap.md"
    original = context.read_bytes()

    def run(argv: tuple[str, ...], root: Path) -> subprocess.CompletedProcess[bytes]:
        if argv[0] == "beta":
            context.write_bytes(b"transient\n")
            context.write_bytes(original)
        return subprocess.CompletedProcess(argv, 0, b"ok\n", b"")

    manifest = collect_evidence(repo, staging, specs, runner=run)
    assert manifest.status == "HOLD"
    assert any("roadmap context changed" in item for item in manifest.violations)


def test_transient_claim_map_change_cannot_be_masked_by_restore(tmp_path: Path) -> None:
    repo = _repo(tmp_path, include_claims=True)
    staging = repo / ".tmp" / "quality" / "inputs"
    specs = (
        *_specs()[:1],
        _specs()[1].model_copy(
            update={"roadmap_context_path": "docs/quality/quality-9plus-roadmap.md"}
        ),
    )
    claims = repo / "config/quality_roadmap_claims.json"
    original = claims.read_bytes()

    def run(argv: tuple[str, ...], root: Path) -> subprocess.CompletedProcess[bytes]:
        if argv[0] == "beta":
            claims.write_bytes(b"transient\n")
            claims.write_bytes(original)
        return subprocess.CompletedProcess(argv, 0, b"ok\n", b"")

    manifest = collect_evidence(repo, staging, specs, runner=run)
    assert manifest.status == "HOLD"
    assert any("roadmap context changed" in item for item in manifest.violations)


def test_runtime_fields_do_not_change_collection_v1_record_shape(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    staging = repo / ".tmp" / "quality" / "inputs"

    def run(argv: tuple[str, ...], root: Path) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(argv, 0, b"raw\n", b"")

    manifest = collect_evidence(repo, staging, _specs(), runner=run)
    for record in manifest.artifacts:
        payload = cast(dict[str, object], record.model_dump())
        assert "input_manifest_flag" not in payload
        assert "roadmap_context_path" not in payload
