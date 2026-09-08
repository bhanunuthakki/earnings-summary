"""Hermetic tests for the BHA-147 exact-subject evidence bundle."""

from __future__ import annotations

import hashlib
import inspect
import json
import os
import shlex
import stat
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from quality.admission_policy import SLOTS, SOURCE_PATHS, parse_source
from quality.admission_policy import evaluate_slot as _evaluate_slot
from quality.admission_policy import required_paths as _required_paths
from quality.architecture import build_architecture_receipt
from quality.duplicates import build_inventory
from quality.evidence_bundle import (
    ADMISSION_GENERATOR_PATH,
    ALLOWED_SOURCE_PATHS,
    ArtifactSpec,
    CollectionManifest,
    admission_path_for,
    allowed_bundle_paths,
    assemble_bundle,
    collect_evidence,
    load_collection_manifest,
    non_architecture_blocks,
    record_score_evidence,
    validate_bundle_diff,
    verify_staged_bytes,
)
from quality.evidence_bundle_io import Runner, default_runner
from quality.evidence_path_policy import FREEZE_PATH
from quality.git_env import clean_local_git_env
from quality.scoring import HARD_GATES, AdmissionReceipt
from quality.static_quality import RuntimeIdentity, StaticQualityInventory
from quality.test_db_models import TestDbAudit as _TestDbAudit


@pytest.fixture(autouse=True)
def _hermetic_git_env(monkeypatch: pytest.MonkeyPatch) -> None:
    dirty = dict(os.environ)
    clean = clean_local_git_env(dirty)
    for key in dirty:
        if key not in clean:
            monkeypatch.delenv(key, raising=False)


def test_exact_subject_static_and_test_db_roundtrips() -> None:
    subject = "a" * 40
    other = "b" * 40
    inventory = StaticQualityInventory(
        repo_root=".",
        tracked_python_files=0,
        active=[],
        immutable_historical_migration=[],
        generated_declarative_exception=[],
        diagnostics=[],
        current_exclusions={},
        scoped_commit=subject,
        source_hash="c" * 64,
        config_hash="d" * 64,
        receipt_identity="e" * 24,
        runtime=RuntimeIdentity(
            implementation="CPython",
            python_version="3.12.0",
            platform="linux",
            machine="x86_64",
        ),
    )
    raw_static = inventory.model_dump_json().encode()
    assert parse_source("static", raw_static, subject).typed_valid is True
    assert parse_source("static", raw_static, other).typed_valid is False

    audit = _TestDbAudit(
        scoped_commit=subject,
        scanner_sha256="0" * 64,
        source_sha256="1" * 64,
        collection_status="COMPLETE",
        raw_audit_status="PASS",
        tracked_test_files=(),
        database_builders=(),
        counts_by_taxonomy={},
        findings=(),
        violations=(),
        builder_invocations=(),
    )
    raw_db = audit.model_dump_json().encode()
    assert parse_source("test_db", raw_db, subject).typed_valid is True
    with pytest.raises(ValueError):
        parse_source("static", raw_static, subject.upper())
    with pytest.raises(ValueError):
        parse_source("static", raw_static, "abc")


def test_lifecycle_handoff_order_and_preservation(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    reach_bytes = _pass_payload("s-reach-v1")
    life_bytes = _pass_payload("s-life-v1")
    order: list[str] = []
    specs = (
        ArtifactSpec(
            artifact_id="lifecycle",
            canonical_path=ALLOWED_SOURCE_PATHS[1],
            command=("echo", "lifecycle"),
            native_scope="WORKTREE",
            output_flag="--output",
            depends_on=("reachability",),
        ),
        ArtifactSpec(
            artifact_id="reachability",
            canonical_path=ALLOWED_SOURCE_PATHS[0],
            command=("echo", "reachability"),
            native_scope="WORKTREE",
            output_flag="--output",
            handoff_path=".tmp/quality/reachability-check.json",
        ),
    )

    def run(argv: tuple[str, ...], root: Path) -> subprocess.CompletedProcess[bytes]:
        name = Path(argv[-1]).name
        if name == "reachability.out":
            order.append("reachability")
            Path(argv[-1]).write_bytes(reach_bytes)
        else:
            assert name == "lifecycle.out"
            assert (root / ".tmp/quality/reachability-check.json").read_bytes() == reach_bytes
            order.append("lifecycle")
            Path(argv[-1]).write_bytes(life_bytes)
        return subprocess.CompletedProcess(args=list(argv), returncode=0, stdout=b"", stderr=b"")

    staging = repo / ".tmp" / "quality" / "handoff-order"
    manifest = collect_evidence(repo, staging, specs, runner=run)
    assert manifest.status == "COMPLETE"
    assert order == ["reachability", "lifecycle"]
    by_id = {record.artifact_id: record for record in manifest.artifacts}
    assert by_id["lifecycle"].depends_on == ("reachability",)
    assert by_id["reachability"].handoff_path == ".tmp/quality/reachability-check.json"
    assert (staging / by_id["reachability"].staging_file).read_bytes() == reach_bytes
    assert (staging / by_id["lifecycle"].staging_file).read_bytes() == life_bytes


def test_invalid_dependencies_rejected_without_running(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    called: list[tuple[str, ...]] = []

    def run(argv: tuple[str, ...], root: Path) -> subprocess.CompletedProcess[bytes]:
        called.append(argv)
        return subprocess.CompletedProcess(args=list(argv), returncode=0, stdout=b"{}", stderr=b"")

    unknown = ArtifactSpec(
        artifact_id="alpha",
        canonical_path=ALLOWED_SOURCE_PATHS[0],
        command=("echo", "alpha"),
        native_scope="WORKTREE",
        depends_on=("missing",),
    )
    with pytest.raises(ValueError, match="unknown dependency"):
        collect_evidence(repo, repo / ".tmp/quality/bad-dep", (unknown,), runner=run)
    cycle = (
        unknown.model_copy(update={"depends_on": ("beta",)}),
        ArtifactSpec(
            artifact_id="beta",
            canonical_path=ALLOWED_SOURCE_PATHS[1],
            command=("echo", "beta"),
            native_scope="WORKTREE",
            depends_on=("alpha",),
        ),
    )
    with pytest.raises(ValueError, match="cycle"):
        collect_evidence(repo, repo / ".tmp/quality/cycle", cycle, runner=run)
    assert called == []


def test_default_runner_help_smoke_without_pythonpath(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checkout = Path(__file__).resolve().parents[1]
    monkeypatch.delenv("PYTHONPATH", raising=False)
    result = default_runner((sys.executable, "src/quality/reachability.py", "--help"), checkout)
    assert result.returncode == 0
    assert b"ModuleNotFoundError" not in result.stdout
    assert b"ModuleNotFoundError" not in result.stderr


def _run(repo: Path, *args: str) -> None:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        check=False,
        env=clean_local_git_env(),
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")


def _make_repo(tmp: Path) -> Path:
    repo = tmp / "repo"
    repo.mkdir(parents=True)
    _run(repo, "init", "-b", "main")
    _run(repo, "config", "user.email", "test@example.com")
    _run(repo, "config", "user.name", "Test")
    (repo / "src" / "quality").mkdir(parents=True)
    gen = repo / ADMISSION_GENERATOR_PATH
    gen.parent.mkdir(parents=True, exist_ok=True)
    import pathlib as _pathlib

    _policy = _pathlib.Path(__file__).resolve().parents[1] / ADMISSION_GENERATOR_PATH
    gen.write_bytes(_policy.read_bytes())
    (repo / "docs" / "quality").mkdir(parents=True)
    (repo / ".tmp" / "quality").mkdir(parents=True)
    (repo / ".gitignore").write_text(".tmp/\n", encoding="utf-8")
    (repo / "a.txt").write_text("hello\n", encoding="utf-8")
    _run(repo, "add", "-A")
    _run(repo, "commit", "-m", "init")
    _run(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    return repo


def _head(repo: Path) -> str:
    out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, check=True)
    return out.stdout.decode("ascii").strip().lower()


def _pass_payload(schema: str, extra: dict[str, str] | None = None) -> bytes:
    payload: dict[str, str] = {"schema_version": schema, "status": "PASS"}
    if extra:
        payload.update(extra)
    return (json.dumps(payload, sort_keys=True) + "\n").encode("utf-8")


def _hold_payload(schema: str) -> bytes:
    return (json.dumps({"schema_version": schema, "status": "HOLD"}) + "\n").encode("utf-8")


def _specs(ids: tuple[str, ...] = ("alpha", "beta")) -> tuple[ArtifactSpec, ...]:
    paths = (ALLOWED_SOURCE_PATHS[0], ALLOWED_SOURCE_PATHS[1])
    return tuple(
        ArtifactSpec(
            artifact_id=artifact_id,
            canonical_path=path,
            generator_path=None,
            generator_version="v1",
            command=("echo", artifact_id),
            native_scope="WORKTREE",
        )
        for artifact_id, path in zip(ids, paths, strict=True)
    )


def _runner_for(mapping: dict[str, bytes], exit_code: int = 0) -> Runner:
    def run(argv: tuple[str, ...], repo_root: Path) -> subprocess.CompletedProcess[bytes]:
        key = argv[-1] if argv else ""
        data = mapping.get(key, b"{}")
        return subprocess.CompletedProcess(
            args=list(argv), returncode=exit_code, stdout=data, stderr=b""
        )

    return run


def _payloads_ok(repo: Path) -> dict[str, bytes]:
    arch = build_architecture_receipt(repo, "WORKTREE")
    dup = build_inventory(repo)
    return {
        "alpha": arch.model_dump_json().encode("utf-8"),
        "beta": dup.model_dump_json().encode("utf-8"),
    }


def _collect_ok(
    repo: Path, sub: str, payloads: dict[str, bytes]
) -> tuple[CollectionManifest, Path]:
    staging = repo / ".tmp" / "quality" / sub
    manifest = collect_evidence(repo, staging, _specs(), runner=_runner_for(payloads))
    assert manifest.status == "COMPLETE"
    return manifest, staging


def _bundle_now(repo: Path) -> str:
    return _commit_bundle(repo)


def test_clean_success_and_byte_preservation(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    subject = _head(repo)
    payloads = _payloads_ok(repo)
    staging = repo / ".tmp" / "quality" / "evidence-bundle"
    manifest = collect_evidence(repo, staging, _specs(), runner=_runner_for(payloads))
    assert manifest.status == "COMPLETE"
    assert manifest.subject_commit == subject
    assert manifest.clean_before and manifest.clean_after
    assert (
        "bundle" not in manifest.model_dump_json().lower()
        or "bundle_commit" not in manifest.model_dump_json()
    )
    reloaded = load_collection_manifest(staging / "manifest.json")
    assert reloaded.manifest_hash == manifest.manifest_hash
    assert verify_staged_bytes(reloaded, staging) == ()
    for record in reloaded.artifacts:
        assert record.native_scope == "WORKTREE"
        assert (staging / record.staging_file).read_bytes() == payloads[record.artifact_id]
    result = assemble_bundle(repo, reloaded, staging, repo / "docs" / "quality")
    assert result.status == "COMPLETE"
    for rel in result.written:
        assert rel in allowed_bundle_paths()
    for record in reloaded.artifacts:
        preserved = (repo / record.canonical_path).read_bytes()
        assert preserved == payloads[record.artifact_id]
        assert hashlib.sha256(preserved).hexdigest() == record.sha256


def test_dirty_worktree_hold(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    (repo / "dirty.txt").write_text("dirty", encoding="utf-8")
    manifest = collect_evidence(
        repo,
        repo / ".tmp" / "quality" / "evidence-bundle",
        _specs(),
        runner=_runner_for({"alpha": b"{}", "beta": b"{}"}),
    )
    assert manifest.status == "HOLD"
    assert any("dirty" in v for v in manifest.violations)


def test_head_race_hold(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)

    def racing(argv: tuple[str, ...], root: Path) -> subprocess.CompletedProcess[bytes]:
        if argv[-1] == "alpha":
            (root / "race.txt").write_text("race", encoding="utf-8")
            subprocess.run(["git", "add", "-A"], cwd=root, check=True, capture_output=True)
            subprocess.run(
                ["git", "commit", "-m", "race"], cwd=root, check=True, capture_output=True
            )
        return subprocess.CompletedProcess(args=list(argv), returncode=0, stdout=b"{}", stderr=b"")

    manifest = collect_evidence(
        repo, repo / ".tmp" / "quality" / "evidence-bundle", _specs(), runner=racing
    )
    assert manifest.status == "HOLD"
    assert any("HEAD changed" in v or "tree changed" in v for v in manifest.violations)


def test_tree_race_hold(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)

    def tree_race(argv: tuple[str, ...], root: Path) -> subprocess.CompletedProcess[bytes]:
        if argv[-1] == "beta":
            (root / "a.txt").write_text("changed\n", encoding="utf-8")
            subprocess.run(["git", "add", "-A"], cwd=root, check=True, capture_output=True)
            subprocess.run(
                ["git", "commit", "-m", "tree-race"], cwd=root, check=True, capture_output=True
            )
        return subprocess.CompletedProcess(args=list(argv), returncode=0, stdout=b"{}", stderr=b"")

    manifest = collect_evidence(
        repo, repo / ".tmp" / "quality" / "evidence-bundle", _specs(), runner=tree_race
    )
    assert manifest.status == "HOLD"


def test_missing_tampered_duplicate_artifacts(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    payloads = {"alpha": _pass_payload("s-a-v1"), "beta": _pass_payload("s-b-v1")}
    staging = repo / ".tmp" / "quality" / "evidence-bundle"
    manifest = collect_evidence(repo, staging, _specs(), runner=_runner_for(payloads))
    assert manifest.status == "COMPLETE"
    (staging / "alpha.raw").unlink()
    assert any("missing" in v for v in verify_staged_bytes(manifest, staging))
    (staging / "alpha.raw").write_bytes(b"tampered")
    assert any("tampered" in v for v in verify_staged_bytes(manifest, staging))
    (staging / "alpha.raw").write_bytes(payloads["alpha"])
    tampered = manifest.model_copy(update={"manifest_hash": "0" * 64})
    (staging / "manifest.json").write_text(tampered.model_dump_json(indent=2), encoding="utf-8")
    try:
        load_collection_manifest(staging / "manifest.json")
        assert False, "expected integrity failure"
    except ValueError:
        pass


def test_duplicate_spec_rejected(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    dup = (*_specs(), _specs()[0])
    try:
        collect_evidence(
            repo, repo / ".tmp" / "quality" / "evidence-bundle", dup, runner=_runner_for({})
        )
        assert False, "expected duplicate rejection"
    except ValueError:
        pass


def test_wrong_generator_hold(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    payloads = {"alpha": _pass_payload("s-a-v1"), "beta": _pass_payload("s-b-v1")}
    staging = repo / ".tmp" / "quality" / "evidence-bundle"
    manifest = collect_evidence(repo, staging, _specs(), runner=_runner_for(payloads))
    result = assemble_bundle(
        repo, manifest, staging, repo / "docs" / "quality", generator_path="src/quality/scoring.py"
    )
    assert result.status == "HOLD"
    assert any("registered oracle" in v for v in result.violations)


def test_partial_registry_hold(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    assert len(non_architecture_blocks()) == 14
    assert len(HARD_GATES) == 10
    payloads = _payloads_ok(repo)
    staging = repo / ".tmp" / "quality" / "evidence-bundle"
    manifest = collect_evidence(repo, staging, _specs(), runner=_runner_for(payloads))
    result = assemble_bundle(repo, manifest, staging, repo / "docs" / "quality")
    assert result.status == "COMPLETE"
    block_paths = [p for p in result.written if "admission-block-" in p]
    gate_paths = [p for p in result.written if "admission-gate-" in p]
    assert len(block_paths) == 14
    assert len(gate_paths) == 10


def test_generic_wrong_schema_hold(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    payloads = {"alpha": _hold_payload("s-a-v1"), "beta": _pass_payload("s-b-v1")}
    staging = repo / ".tmp" / "quality" / "evidence-bundle"
    manifest = collect_evidence(repo, staging, _specs(), runner=_runner_for(payloads))
    result = assemble_bundle(repo, manifest, staging, repo / "docs" / "quality")
    assert result.status == "HOLD"


def test_valid_raw_semantic_nonadmitting_complete(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    payloads = _payloads_ok(repo)
    manifest, staging = _collect_ok(repo, "evidence-bundle", payloads)
    result = assemble_bundle(repo, manifest, staging, repo / "docs" / "quality")
    assert result.status == "COMPLETE"
    for record in manifest.artifacts:
        assert (repo / record.canonical_path).read_bytes() == payloads[record.artifact_id]
    assert len(SLOTS) == 24
    for key in SLOTS:
        assert _evaluate_slot(key, {}) == "fail"
    collected = {record.canonical_path for record in manifest.artifacts}
    receipts = [
        json.loads((repo / p).read_text(encoding="utf-8"))
        for p in result.written
        if "admission-" in p
    ]
    assert len(receipts) == 24
    for raw in receipts:
        receipt = AdmissionReceipt.model_validate(raw)
        assert receipt.state == "fail"
        expected = sorted(set(_required_paths(receipt.key)) & collected)
        assert sorted(s.path for s in receipt.sources) == expected


def test_self_reference_rejected(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    colliding = admission_path_for("block", non_architecture_blocks()[0])
    specs = (
        ArtifactSpec(
            artifact_id="alpha",
            canonical_path=colliding,
            generator_version="v1",
            command=("echo", "alpha"),
            native_scope="WORKTREE",
        ),
        ArtifactSpec(
            artifact_id="beta",
            canonical_path=ALLOWED_SOURCE_PATHS[1],
            generator_version="v1",
            command=("echo", "beta"),
            native_scope="WORKTREE",
        ),
    )
    payloads = {"alpha": _pass_payload("s-x-v1"), "beta": _pass_payload("s-y-v1")}
    staging = repo / ".tmp" / "quality" / "evidence-bundle"
    try:
        manifest = collect_evidence(repo, staging, specs, runner=_runner_for(payloads))
    except ValueError:
        return
    result = assemble_bundle(repo, manifest, staging, repo / "docs" / "quality")
    assert result.status == "HOLD"
    assert any(
        "collision" in v or "allowlist" in v or "self-reference" in v for v in result.violations
    )


def test_output_collision_and_alias(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    payloads = _payloads_ok(repo)
    staging = repo / ".tmp" / "quality" / "evidence-bundle"
    manifest = collect_evidence(repo, staging, _specs(), runner=_runner_for(payloads))
    target = repo / ALLOWED_SOURCE_PATHS[0]
    result = assemble_bundle(repo, manifest, staging, repo / "docs" / "quality")
    assert result.status == "COMPLETE"
    assert target.read_bytes() == payloads["alpha"]
    link = repo / "docs" / "quality" / "link.json"
    try:
        link.symlink_to(target)
        assert link.is_symlink()
    except OSError:
        pass
    assert (repo / ALLOWED_SOURCE_PATHS[0]).read_bytes() == payloads["alpha"]


def _commit_bundle(repo: Path) -> str:
    _run(repo, "add", "--", "docs/quality")
    _run(repo, "commit", "-m", "bundle")
    _run(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    return _head(repo)


def test_bundle_diff_and_ancestry(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    subject = _head(repo)
    payloads = _payloads_ok(repo)
    staging = repo / ".tmp" / "quality" / "evidence-bundle"
    manifest = collect_evidence(repo, staging, _specs(), runner=_runner_for(payloads))
    result = assemble_bundle(repo, manifest, staging, repo / "docs" / "quality")
    assert result.status == "COMPLETE"
    bundle = _commit_bundle(repo)
    assert validate_bundle_diff(repo, subject, bundle) == ()
    (repo / "docs" / "quality" / "evil.txt").write_text("evil", encoding="utf-8")
    _run(repo, "add", "-A")
    _run(repo, "commit", "-m", "evil")
    _run(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    evil = _head(repo)
    assert validate_bundle_diff(repo, subject, evil) != ()


def test_non_json_bundle_diff(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    subject = _head(repo)
    payloads = _payloads_ok(repo)
    staging = repo / ".tmp" / "quality" / "evidence-bundle"
    manifest = collect_evidence(repo, staging, _specs(), runner=_runner_for(payloads))
    assert assemble_bundle(repo, manifest, staging, repo / "docs" / "quality").status == "COMPLETE"
    (repo / ALLOWED_SOURCE_PATHS[0]).write_bytes(b"not json{{{")
    bundle = _commit_bundle(repo)
    assert any(
        "JSON" in v or "non-evidence" in v for v in validate_bundle_diff(repo, subject, bundle)
    )


def test_origin_main_and_discarded_sha(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    subject = _head(repo)
    payloads = _payloads_ok(repo)
    staging = repo / ".tmp" / "quality" / "evidence-bundle"
    manifest = collect_evidence(repo, staging, _specs(), runner=_runner_for(payloads))
    assert assemble_bundle(repo, manifest, staging, repo / "docs" / "quality").status == "COMPLETE"
    bundle = _commit_bundle(repo)
    subprocess.run(
        ["git", "update-ref", "-d", "refs/remotes/origin/main"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    assert any("origin" in v for v in validate_bundle_diff(repo, subject, bundle))
    _run(repo, "update-ref", "refs/remotes/origin/main", bundle)
    _run(repo, "reset", "--hard", subject)
    assert any(
        "ancestor" in v or "resolve" in v for v in validate_bundle_diff(repo, bundle, subject)
    )


def test_post_commit_external_manifest(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    subject = _head(repo)
    payloads = _payloads_ok(repo)
    staging = repo / ".tmp" / "quality" / "evidence-bundle"
    manifest = collect_evidence(repo, staging, _specs(), runner=_runner_for(payloads))
    result = assemble_bundle(repo, manifest, staging, repo / "docs" / "quality")
    assert result.status == "COMPLETE"
    bundle = _commit_bundle(repo)
    out = repo / ".tmp" / "quality" / "score-evidence.json"
    evidence = record_score_evidence(repo, bundle, manifest, staging, out)
    assert evidence.scoped_commit == subject
    assert all(entry.bundle_commit == bundle for entry in evidence.blocks.values())
    assert all(entry.bundle_commit == bundle for entry in evidence.hard_gates.values())
    assert out.is_file()
    assert out.relative_to(repo).as_posix().startswith(".tmp/")
    raw = out.read_bytes()
    assert bundle not in (repo / ALLOWED_SOURCE_PATHS[0]).read_text(encoding="utf-8")
    assert json.loads(raw.decode("utf-8"))["blocks"]
    try:
        record_score_evidence(
            repo,
            bundle,
            manifest,
            staging,
            repo / "docs" / "quality" / "bad.json",
        )
        assert False, "expected ignored-path enforcement"
    except ValueError:
        pass
    assert "bundle_commit" not in (staging / "manifest.json").read_text(encoding="utf-8")


def test_no_self_containing_claim(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    payloads = _payloads_ok(repo)
    staging = repo / ".tmp" / "quality" / "evidence-bundle"
    manifest = collect_evidence(repo, staging, _specs(), runner=_runner_for(payloads))
    text = (staging / "manifest.json").read_text(encoding="utf-8")
    assert "bundle_commit" not in text
    assert manifest.subject_commit not in [a.sha256 for a in manifest.artifacts]
    assert os.path.realpath(staging).startswith(os.path.realpath(repo))


def test_manifest_mutation_and_staging_hygiene(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    payloads = _payloads_ok(repo)
    manifest, staging = _collect_ok(repo, "evidence-bundle", payloads)
    rec = manifest.artifacts[0]
    forged = rec.model_copy(update={"sha256": "0" * 64})
    mutated = manifest.model_copy(update={"artifacts": (forged, *manifest.artifacts[1:])})
    res = assemble_bundle(repo, mutated, staging, repo / "docs" / "quality")
    assert res.status == "HOLD"
    assert any("integrity" in v for v in res.violations)
    assert res.written == ()
    (repo / ".gitignore").write_text("", encoding="utf-8")
    res2 = assemble_bundle(repo, manifest, staging, repo / "docs" / "quality")
    assert res2.status == "HOLD"
    assert any("ignored" in v for v in res2.violations)
    (repo / ".gitignore").write_text(".tmp/\n", encoding="utf-8")
    rel_man = (staging.relative_to(repo).as_posix()) + "/manifest.json"
    _run(repo, "add", "-f", "--", rel_man)
    res3 = assemble_bundle(repo, manifest, staging, repo / "docs" / "quality")
    assert res3.status == "HOLD"
    assert any("tracked" in v for v in res3.violations)
    _run(repo, "rm", "--cached", "-q", "--", rel_man)


def test_staged_symlink_hardlink_nonregular(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    payloads = _payloads_ok(repo)
    manifest, staging = _collect_ok(repo, "evidence-bundle", payloads)
    (staging / "alpha.raw").unlink()
    (staging / "alpha.raw").symlink_to(staging / "beta.raw")
    assert any("symlink" in v for v in verify_staged_bytes(manifest, staging))
    (staging / "alpha.raw").unlink()
    (staging / "alpha.raw").write_bytes(payloads["alpha"])
    (staging / "beta.raw").unlink()
    os.link(staging / "alpha.raw", staging / "beta.raw")
    probs = verify_staged_bytes(manifest, staging)
    assert any("hard-link" in v or "duplicate" in v for v in probs)
    (staging / "beta.raw").unlink()
    (staging / "beta.raw").write_bytes(payloads["beta"])
    (staging / "alpha.raw").unlink()
    (staging / "alpha.raw").mkdir()
    assert any(
        "non-regular" in v or "escape" in v or "missing" in v
        for v in verify_staged_bytes(manifest, staging)
    )
    (staging / "alpha.raw").rmdir()
    (staging / "alpha.raw").write_bytes(payloads["alpha"])
    assert verify_staged_bytes(manifest, staging) == ()


def test_staged_reread_instability(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _make_repo(tmp_path)
    payloads = _payloads_ok(repo)
    manifest, staging = _collect_ok(repo, "evidence-bundle", payloads)
    orig_open = os.open
    opened: list[int] = []

    def tracking_open(path: str, flags: int) -> int:
        fd = orig_open(path, flags)
        opened.append(fd)
        return fd

    monkeypatch.setattr(os, "open", tracking_open)
    orig_read = os.read

    def flipping_read(fd: int, n: int) -> bytes:
        chunk = orig_read(fd, n)
        if len(opened) >= 2 and fd == opened[1] and len(chunk) > 0:
            return b"X" + chunk[1:] if len(chunk) > 1 else b"X"
        return chunk

    monkeypatch.setattr(os, "read", flipping_read)
    assert any(
        "changed during read" in v or "tampered" in v
        for v in verify_staged_bytes(manifest, staging)
    )


def test_subject_dirty_stale_moved(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    payloads = _payloads_ok(repo)
    manifest, staging = _collect_ok(repo, "evidence-bundle", payloads)
    (repo / "dirty2.txt").write_text("x", encoding="utf-8")
    res = assemble_bundle(repo, manifest, staging, repo / "docs" / "quality")
    assert res.status == "HOLD"
    assert any("dirty" in v for v in res.violations)
    assert res.written == ()
    (repo / "dirty2.txt").unlink()
    (repo / "new.txt").write_text("new", encoding="utf-8")
    _run(repo, "add", "-A")
    _run(repo, "commit", "-m", "new")
    res2 = assemble_bundle(repo, manifest, staging, repo / "docs" / "quality")
    assert res2.status == "HOLD"
    assert any("differs" in v or "moved" in v or "subject" in v for v in res2.violations)
    assert res2.written == ()


def test_output_symlink_hardlink(tmp_path: Path) -> None:
    sym_parent = tmp_path / "sym"
    sym_parent.mkdir()
    repo = _make_repo(sym_parent)
    payloads = _payloads_ok(repo)
    first = ALLOWED_SOURCE_PATHS[0]
    second = ALLOWED_SOURCE_PATHS[1]
    (repo / first).parent.mkdir(parents=True, exist_ok=True)
    (repo / first).write_text("prior", encoding="utf-8")
    (repo / second).write_text("prior2", encoding="utf-8")
    _run(repo, "add", "-A")
    _run(repo, "commit", "-m", "priors")
    _run(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    payloads = _payloads_ok(repo)
    manifest, staging = _collect_ok(repo, "evidence-bundle", payloads)
    (repo / second).unlink()
    (repo / second).symlink_to(repo / first)
    res = assemble_bundle(repo, manifest, staging, repo / "docs" / "quality")
    assert res.status == "HOLD"
    assert any("symlink" in v or "unsafe" in v or "alias" in v for v in res.violations)
    assert res.written == ()
    hard_parent = tmp_path / "hard"
    hard_parent.mkdir()
    repo2 = _make_repo(hard_parent)
    (repo2 / first).parent.mkdir(parents=True, exist_ok=True)
    (repo2 / first).write_text("prior", encoding="utf-8")
    os.link(repo2 / first, repo2 / second)
    _run(repo2, "add", "-A")
    _run(repo2, "commit", "-m", "priors")
    _run(repo2, "update-ref", "refs/remotes/origin/main", "HEAD")
    manifest2, staging2 = _collect_ok(repo2, "evidence-bundle", _payloads_ok(repo2))
    res2 = assemble_bundle(repo2, manifest2, staging2, repo2 / "docs" / "quality")
    assert res2.status == "HOLD"
    assert any("hard-link" in v or "alias" in v or "unsafe" in v for v in res2.violations)
    assert res2.written == ()


def test_write_failure_restores_byte_for_byte(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _make_repo(tmp_path)
    first = ALLOWED_SOURCE_PATHS[0]
    second = ALLOWED_SOURCE_PATHS[1]
    (repo / first).parent.mkdir(parents=True, exist_ok=True)
    (repo / first).write_bytes(b"prior-first")
    os.chmod(repo / first, 0o640)
    (repo / second).write_bytes(b"prior-second")
    os.chmod(repo / second, 0o640)
    _run(repo, "add", "-A")
    _run(repo, "commit", "-m", "priors")
    _run(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    payloads = _payloads_ok(repo)
    manifest, staging = _collect_ok(repo, "evidence-bundle", payloads)
    prior_first = (repo / first).read_bytes()
    prior_second = (repo / second).read_bytes()
    allowed = set(allowed_bundle_paths())
    pre_existing = {r: (repo / r).read_bytes() for r in allowed if (repo / r).is_file()}
    orig_replace = os.replace

    def failing_replace(src: str, dst: str) -> None:
        if str(dst).endswith(second):
            orig_replace(src, dst)
            raise OSError("injected later")
        orig_replace(src, dst)

    monkeypatch.setattr(os, "replace", failing_replace)
    res = assemble_bundle(repo, manifest, staging, repo / "docs" / "quality")
    assert res.status == "HOLD"
    assert res.written == ()
    assert any("unable to write" in v for v in res.violations)
    assert (repo / first).read_bytes() == prior_first
    assert (repo / second).read_bytes() == prior_second
    for rel in allowed:
        cur = repo / rel
        if rel in pre_existing:
            assert cur.read_bytes() == pre_existing[rel]
        elif rel not in (first, second):
            assert not cur.exists()


def test_committed_source_variants(tmp_path: Path) -> None:
    r1 = _make_repo(tmp_path / "r1")
    p1 = _payloads_ok(r1)
    m1, s1 = _collect_ok(r1, "evidence-bundle", p1)
    subj1 = m1.subject_commit
    assert assemble_bundle(r1, m1, s1, r1 / "docs" / "quality").status == "COMPLETE"
    (r1 / m1.artifacts[0].canonical_path).unlink()
    b1 = _bundle_now(r1)
    assert any("missing" in v or "absent" in v for v in validate_bundle_diff(r1, subj1, b1, m1))
    r2 = _make_repo(tmp_path / "r2")
    p2 = _payloads_ok(r2)
    pre_bytes = p2["alpha"]
    (r2 / ALLOWED_SOURCE_PATHS[0]).parent.mkdir(parents=True, exist_ok=True)
    (r2 / ALLOWED_SOURCE_PATHS[0]).write_bytes(pre_bytes)
    _run(r2, "add", "-A")
    _run(r2, "commit", "-m", "pre")
    p2 = _payloads_ok(r2)
    assert p2["alpha"] != pre_bytes
    m2, s2 = _collect_ok(r2, "evidence-bundle", p2)
    assert assemble_bundle(r2, m2, s2, r2 / "docs" / "quality").status == "COMPLETE"
    b2 = _bundle_now(r2)
    assert validate_bundle_diff(r2, m2.subject_commit, b2, m2) == ()
    r3 = _make_repo(tmp_path / "r3")
    p3 = _payloads_ok(r3)
    m3, s3 = _collect_ok(r3, "evidence-bundle", p3)
    assert assemble_bundle(r3, m3, s3, r3 / "docs" / "quality").status == "COMPLETE"
    (r3 / m3.artifacts[0].canonical_path).write_bytes(b"altered")
    b3 = _bundle_now(r3)
    assert any(
        "altered" in v or "mismatch" in v
        for v in validate_bundle_diff(r3, m3.subject_commit, b3, m3)
    )


def test_forged_admissions_and_incomplete(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    payloads = _payloads_ok(repo)
    manifest, staging = _collect_ok(repo, "evidence-bundle", payloads)
    assert assemble_bundle(repo, manifest, staging, repo / "docs" / "quality").status == "COMPLETE"
    subject = manifest.subject_commit
    valid = _bundle_now(repo)
    assert validate_bundle_diff(repo, subject, valid, manifest) == ()
    block_rel = admission_path_for("block", "maintainability.enforced_ratchets")

    def _forge(kind: str) -> tuple[str, ...]:
        _run(repo, "reset", "--hard", valid)
        raw = json.loads((repo / block_rel).read_text(encoding="utf-8"))
        if kind == "hash":
            raw["sources"][0]["sha256"] = "0" * 64
        elif kind == "subject":
            raw["subject_commit"] = "f" * 40
        elif kind == "generator":
            raw["generator_sha256"] = "1" * 64
        elif kind == "state":
            raw["state"] = "pass" if raw.get("state") == "fail" else "bogus"
            if raw.get("state") == "pass":
                raw["state"] = "bogus"
        elif kind == "key":
            raw["key"] = HARD_GATES[0]
        (repo / block_rel).write_text(
            json.dumps(raw, sort_keys=True, indent=2) + "\n", encoding="utf-8"
        )
        if kind == "incomplete":
            (repo / block_rel).unlink()
        _run(repo, "add", "-A")
        _run(repo, "commit", "-m", kind)
        _run(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
        forged_bundle = _head(repo)
        return validate_bundle_diff(repo, subject, forged_bundle, manifest)

    for kind in ("hash", "subject", "generator", "state", "key", "incomplete"):
        probs = _forge(kind)
        assert probs != ()
    _run(repo, "reset", "--hard", valid)
    _run(repo, "update-ref", "refs/remotes/origin/main", valid)


def test_record_tamper_and_external(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    payloads = _payloads_ok(repo)
    manifest, staging = _collect_ok(repo, "evidence-bundle", payloads)
    assert assemble_bundle(repo, manifest, staging, repo / "docs" / "quality").status == "COMPLETE"
    bundle = _bundle_now(repo)
    out = repo / ".tmp" / "quality" / "score-evidence.json"
    (staging / "alpha.raw").write_bytes(b"bad")
    try:
        record_score_evidence(repo, bundle, manifest, staging, out)
        assert False
    except ValueError:
        pass
    (staging / "alpha.raw").write_bytes(payloads["alpha"])
    bad = manifest.model_copy(update={"subject_commit": "0" * 40})
    try:
        record_score_evidence(repo, bundle, bad, staging, out)
        assert False
    except ValueError:
        pass
    if out.is_file() or out.is_symlink():
        out.unlink()
    out.symlink_to(staging / "manifest.json")
    try:
        record_score_evidence(repo, bundle, manifest, staging, out)
        assert False
    except ValueError:
        pass
    out.unlink()
    out.write_bytes(b"{}")
    buddy = repo / ".tmp" / "quality" / "buddy.json"
    os.link(out, buddy)
    try:
        record_score_evidence(repo, bundle, manifest, staging, out)
        assert False
    except ValueError:
        pass
    buddy.unlink()
    out.unlink()
    out.write_bytes(b"{}")
    _run(repo, "add", "-f", "--", out.relative_to(repo).as_posix())
    try:
        record_score_evidence(repo, bundle, manifest, staging, out)
        assert False
    except ValueError:
        pass
    _run(repo, "rm", "--cached", "-q", "--", out.relative_to(repo).as_posix())
    out.unlink()
    try:
        record_score_evidence(
            repo,
            bundle,
            manifest,
            staging,
            repo / "docs" / "quality" / "bad.json",
        )
        assert False
    except ValueError:
        pass
    evidence = record_score_evidence(repo, bundle, manifest, staging, out)
    assert evidence.scoped_commit == manifest.subject_commit
    assert out.is_file()
    assert stat.S_IMODE(os.lstat(out).st_mode) != 0


def test_record_rejects_non_complete_manifest(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    payloads = _payloads_ok(repo)
    manifest, staging = _collect_ok(repo, "evidence-bundle", payloads)
    assert assemble_bundle(repo, manifest, staging, repo / "docs" / "quality").status == "COMPLETE"
    bundle = _bundle_now(repo)
    from quality.evidence_bundle_io import manifest_integrity

    out = repo / ".tmp" / "quality" / "score-evidence.json"
    hold = manifest.model_copy(update={"status": "HOLD"})
    hold = hold.model_copy(update={"manifest_hash": manifest_integrity(hold)})
    with pytest.raises(ValueError):
        record_score_evidence(repo, bundle, hold, staging, out)
    dirty = manifest.model_copy(update={"clean_before": False})
    dirty = dirty.model_copy(update={"manifest_hash": manifest_integrity(dirty)})
    with pytest.raises(ValueError):
        record_score_evidence(repo, bundle, dirty, staging, out)
    assert verify_staged_bytes(manifest, staging) == ()
    assert validate_bundle_diff(repo, manifest.subject_commit, bundle, manifest) == ()


def test_complete_truthfulness_fail_closed(tmp_path: Path) -> None:
    from pydantic import ValidationError

    from quality.evidence_bundle_io import manifest_integrity

    repo = _make_repo(tmp_path)
    manifest, _staging = _collect_ok(repo, "evidence-bundle", _payloads_ok(repo))
    assert manifest.status == "COMPLETE"

    def _assert_rehashed_rejected(mutated: CollectionManifest) -> None:
        payload = mutated.model_dump()
        payload["manifest_hash"] = manifest_integrity(mutated)
        with pytest.raises(ValidationError):
            CollectionManifest.model_validate(payload)

    forged_violations = manifest.model_copy(update={"violations": ("boom",)})
    _assert_rehashed_rejected(forged_violations)
    with pytest.raises(ValidationError):
        CollectionManifest.model_validate({**manifest.model_dump(), "violations": ("boom",)})

    for bad_status in ("failed", "hold"):
        bad = manifest.artifacts[0].model_copy(update={"collection_status": bad_status})
        forged = manifest.model_copy(update={"artifacts": (bad, *manifest.artifacts[1:])})
        _assert_rehashed_rejected(forged)
        with pytest.raises(ValidationError):
            CollectionManifest.model_validate(
                {
                    **manifest.model_dump(),
                    "artifacts": [
                        bad.model_dump(),
                        *[a.model_dump() for a in manifest.artifacts[1:]],
                    ],
                }
            )

    _assert_rehashed_rejected(manifest.model_copy(update={"clean_before": False}))
    _assert_rehashed_rejected(manifest.model_copy(update={"clean_after": False}))
    _assert_rehashed_rejected(manifest.model_copy(update={"head_before": "f" * 40}))
    _assert_rehashed_rejected(manifest.model_copy(update={"tree_before": "e" * 40}))

    hold_first = manifest.artifacts[0].model_copy(update={"collection_status": "hold"})
    hold_bypass = manifest.model_copy(
        update={
            "status": "HOLD",
            "violations": ("diagnostic hold",),
            "clean_before": False,
            "head_before": "f" * 40,
            "tree_before": "e" * 40,
            "artifacts": (hold_first, *manifest.artifacts[1:]),
        }
    )
    hold_payload = hold_bypass.model_dump()
    hold_payload["manifest_hash"] = manifest_integrity(hold_bypass)
    hold_manifest = CollectionManifest.model_validate(hold_payload)
    assert hold_manifest.status == "HOLD"
    assert hold_manifest.violations != ()
    assert hold_manifest.artifacts[0].collection_status == "hold"


def test_cli_validate_enforces_manifest(tmp_path: Path) -> None:
    import sys

    repo = _make_repo(tmp_path)
    payloads = _payloads_ok(repo)
    manifest, staging = _collect_ok(repo, "evidence-bundle", payloads)
    assert assemble_bundle(repo, manifest, staging, repo / "docs" / "quality").status == "COMPLETE"
    bundle = _bundle_now(repo)
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "execution"))
    from collect_evidence_bundle import main as cli_main

    assert (
        cli_main(
            [
                "--mode",
                "validate",
                "--repo-root",
                str(repo),
                "--staging-dir",
                str(staging),
                "--subject",
                manifest.subject_commit,
                "--bundle",
                bundle,
            ]
        )
        == 0
    )
    (repo / manifest.artifacts[0].canonical_path).write_bytes(
        _pass_payload("s-a-v1", {"extra": "x"})
    )
    _run(repo, "add", "--", "docs/quality")
    _run(repo, "commit", "-m", "tamper")
    _run(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    tampered = _head(repo)
    probs = validate_bundle_diff(repo, manifest.subject_commit, tampered)
    assert any(
        "typed-invalid" in v or "schema" in v.lower() or "source" in v.lower() for v in probs
    )
    assert (
        cli_main(
            [
                "--mode",
                "validate",
                "--repo-root",
                str(repo),
                "--staging-dir",
                str(staging),
                "--subject",
                manifest.subject_commit,
                "--bundle",
                tampered,
            ]
        )
        != 0
    )


def test_all_24_receipts_fail(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    manifest, staging = _collect_ok(repo, "evidence-bundle", _payloads_ok(repo))
    result = assemble_bundle(repo, manifest, staging, repo / "docs" / "quality")
    assert result.status == "COMPLETE"
    assert len(SLOTS) == 24
    for key in SLOTS:
        text = (repo / admission_path_for(SLOTS[key].kind, key)).read_text(encoding="utf-8")
        assert AdmissionReceipt.model_validate(json.loads(text)).state == "fail"


def test_cross_slot_source_forgery_rejected(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    manifest, staging = _collect_ok(repo, "evidence-bundle", _payloads_ok(repo))
    assert assemble_bundle(repo, manifest, staging, repo / "docs" / "quality").status == "COMPLETE"
    subject = manifest.subject_commit
    bundle = _bundle_now(repo)
    assert validate_bundle_diff(repo, subject, bundle, manifest) == ()
    target = admission_path_for("block", "maintainability.enforced_ratchets")
    raw = json.loads((repo / target).read_text(encoding="utf-8"))
    donor = json.loads(
        (repo / admission_path_for("block", "maintainability.static_quality")).read_text(
            encoding="utf-8"
        )
    )
    raw["sources"] = donor["sources"]
    (repo / target).write_text(json.dumps(raw, indent=2) + "\n", encoding="utf-8")
    _run(repo, "add", "-A")
    _run(repo, "commit", "-m", "forge")
    _run(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    assert validate_bundle_diff(repo, subject, _head(repo), manifest) != ()


def test_forged_pass_rejected(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    manifest, staging = _collect_ok(repo, "evidence-bundle", _payloads_ok(repo))
    assert assemble_bundle(repo, manifest, staging, repo / "docs" / "quality").status == "COMPLETE"
    subject = manifest.subject_commit
    target = admission_path_for("block", "maintainability.enforced_ratchets")
    raw = json.loads((repo / target).read_text(encoding="utf-8"))
    raw["state"] = "pass"
    (repo / target).write_text(json.dumps(raw, indent=2) + "\n", encoding="utf-8")
    forged = _commit_bundle(repo)
    assert any("forged" in v for v in validate_bundle_diff(repo, subject, forged, manifest))


def test_duplicate_key_typed_source_rejected(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    dup = b'{"schema_version": "architecture-measurement-v1", "schema_version": "architecture-measurement-v1"}'
    payloads = {"alpha": dup, "beta": _payloads_ok(repo)["beta"]}
    manifest, staging = _collect_ok(repo, "evidence-bundle", payloads)
    result = assemble_bundle(repo, manifest, staging, repo / "docs" / "quality")
    assert result.status == "HOLD"
    assert any("typed-invalid" in v for v in result.violations)


def test_empty_sources_only_for_fail() -> None:
    base: dict[str, str | list[object]] = {
        "schema_version": "quality-score-admission/v1",
        "subject_commit": "a" * 40,
        "generator_path": ADMISSION_GENERATOR_PATH,
        "generator_sha256": "b" * 64,
        "sources": [],
    }
    fail = AdmissionReceipt.model_validate(
        {**base, "kind": "block", "key": non_architecture_blocks()[0], "state": "fail"}
    )
    assert fail.sources == ()
    with pytest.raises(ValueError):
        AdmissionReceipt.model_validate(
            {**base, "kind": "block", "key": non_architecture_blocks()[0], "state": "pass"}
        )


def test_default_specs_select_existing_producers_and_output_flags() -> None:
    assert tuple(SOURCE_PATHS.values()) == ALLOWED_SOURCE_PATHS
    from quality.evidence_bundle_collect import default_artifact_specs

    checkout = Path(__file__).resolve().parents[1]
    specs = default_artifact_specs()
    assert len(specs) == 9
    by_id = {s.artifact_id: s for s in specs}
    assert set(by_id) == {
        "architecture",
        "duplicates",
        "lifecycle",
        "performance",
        "reachability",
        "reconciliation",
        "roadmap_freeze",
        "static",
        "test_db",
    }
    for spec in specs:
        assert spec.output_flag is not None
        assert spec.output_flag.startswith("--")
        assert spec.generator_path is not None
        assert spec.command[1] == spec.generator_path
        assert spec.output_flag not in spec.command
        assert spec.accepted_exit_codes == (0, 2)
    assert by_id["architecture"].generator_path == "src/quality/architecture.py"
    assert by_id["architecture"].output_flag == "--output"
    assert by_id["duplicates"].generator_path == "src/quality/duplicates.py"
    assert by_id["duplicates"].output_flag == "--out"
    assert by_id["lifecycle"].canonical_path == "docs/quality/lifecycle-inventory.json"
    assert by_id["lifecycle"].generator_path == ("execution/classify_operational_lifecycle.py")
    assert by_id["lifecycle"].output_flag == "--output"
    assert by_id["performance"].output_flag == "--output"
    assert by_id["performance"].generator_path == "execution/capture_performance_baseline.py"
    perf_command = by_id["performance"].command
    perf_argv = shlex.split(perf_command[perf_command.index("--command") + 1])
    assert perf_argv[0] == sys.executable
    assert perf_argv[1:] == ["-c", "print('ok')"]
    assert by_id["reachability"].generator_path == "src/quality/reachability.py"
    assert by_id["reachability"].output_flag == "--output"
    assert by_id["reconciliation"].output_flag == "--output"
    assert by_id["reconciliation"].generator_path == "execution/reconcile_quality_baseline.py"
    assert by_id["reconciliation"].command[1] == "execution/reconcile_quality_baseline.py"
    assert by_id["static"].generator_path == "src/quality/static_quality.py"
    assert by_id["static"].output_flag == "--output"
    assert by_id["test_db"].canonical_path == "docs/quality/test-db-patterns-baseline.json"
    assert by_id["test_db"].generator_path == "execution/audit_test_db_patterns.py"
    assert by_id["test_db"].output_flag == "--output"
    assert by_id["lifecycle"].command[1] == ("execution/classify_operational_lifecycle.py")
    assert by_id["test_db"].command[1] == "execution/audit_test_db_patterns.py"
    assert by_id["lifecycle"].depends_on == ("reachability",)
    assert by_id["reachability"].handoff_path == ".tmp/quality/reachability-check.json"
    for artifact_id, spec in by_id.items():
        if artifact_id not in (
            "lifecycle",
            "reachability",
            "reconciliation",
            "roadmap_freeze",
        ):
            assert spec.depends_on == ()
            assert spec.handoff_path is None
    assert by_id["reconciliation"].depends_on == (
        "architecture",
        "duplicates",
        "reachability",
        "static",
        "test_db",
    )
    assert by_id["reconciliation"].input_manifest_flag == "--staged-manifest"
    assert by_id["reconciliation"].roadmap_context_path == "docs/quality/quality-9plus-roadmap.md"
    assert by_id["roadmap_freeze"].canonical_path == "docs/quality/roadmap-freeze.json"
    assert by_id["roadmap_freeze"].generator_version == "roadmap-freeze-index/v1"
    assert by_id["roadmap_freeze"].input_manifest_flag == "--input-manifest"
    assert by_id["roadmap_freeze"].depends_on == (
        "architecture",
        "duplicates",
        "lifecycle",
        "performance",
        "reachability",
        "reconciliation",
        "static",
        "test_db",
    )
    for spec in specs:
        assert spec.generator_path is not None
        assert (checkout / spec.generator_path).is_file()


def test_assembler_has_no_freeze_override_surface() -> None:
    assert "freeze_index" not in inspect.signature(assemble_bundle).parameters


def test_invalid_collected_freeze_holds_without_writing(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    spec = ArtifactSpec(
        artifact_id="roadmap_freeze",
        canonical_path=FREEZE_PATH,
        command=("freeze",),
        native_scope="WORKTREE",
        output_flag="--output",
        accepted_exit_codes=(0, 2),
    )
    staging = repo / ".tmp" / "quality" / "freeze"

    def run(argv: tuple[str, ...], root: Path) -> subprocess.CompletedProcess[bytes]:
        Path(argv[-1]).write_bytes(b"{}\n")
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    manifest = collect_evidence(repo, staging, (spec,), runner=run)
    result = assemble_bundle(repo, manifest, staging, repo / "docs" / "quality")
    assert result.status == "HOLD"
    assert FREEZE_PATH not in result.written
    assert not (repo / FREEZE_PATH).exists()


def test_staging_file_must_match_artifact_id() -> None:
    from pydantic import ValidationError

    from quality.evidence_bundle_models import ArtifactRecord

    base: dict[str, object] = {
        "artifact_id": "alpha",
        "canonical_path": ALLOWED_SOURCE_PATHS[0],
        "command": ("echo", "alpha"),
        "native_scope": "WORKTREE",
        "sha256": "a" * 64,
        "byte_length": 0,
        "collection_status": "collected",
        "staging_file": "alpha.raw",
    }
    ArtifactRecord.model_validate(base)
    for bad in ("beta.raw", "../alpha.raw", "a/b.raw", "alpha.txt", "alpha.raw/"):
        with pytest.raises(ValidationError):
            ArtifactRecord.model_validate({**base, "staging_file": bad})


def test_accepted_exit_code_two_output_file(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    payloads = _payloads_ok(repo)
    file_bytes = payloads["alpha"]
    receipt = build_architecture_receipt(repo, "WORKTREE")
    assert file_bytes == receipt.model_dump_json().encode("utf-8")

    def _file_runner(payload: bytes) -> Runner:
        def run(argv: tuple[str, ...], root: Path) -> subprocess.CompletedProcess[bytes]:
            Path(argv[-1]).write_bytes(payload)
            return subprocess.CompletedProcess(
                args=list(argv), returncode=2, stdout=b"", stderr=b""
            )

        return run

    def _spec(codes: tuple[int, ...]) -> ArtifactSpec:
        return ArtifactSpec(
            artifact_id="alpha",
            canonical_path=ALLOWED_SOURCE_PATHS[0],
            generator_path=None,
            generator_version="v1",
            command=("echo", "alpha"),
            native_scope="WORKTREE",
            output_flag="--output",
            accepted_exit_codes=codes,
        )

    ok = collect_evidence(
        repo,
        repo / ".tmp" / "quality" / "ok",
        (_spec((0, 2)),),
        runner=_file_runner(file_bytes),
    )
    assert ok.status == "COMPLETE"
    assert ok.artifacts[0].collection_status == "collected"
    assert ok.artifacts[0].return_code == 2
    assert ok.artifacts[0].accepted_exit_codes == (0, 2)
    assert (
        assemble_bundle(
            repo,
            ok,
            repo / ".tmp" / "quality" / "ok",
            repo / "docs" / "quality",
        ).status
        == "COMPLETE"
    )
    hold = collect_evidence(
        repo, repo / ".tmp" / "quality" / "hold", (_spec((0,)),), runner=_file_runner(file_bytes)
    )
    assert hold.status == "HOLD"
    assert hold.artifacts[0].collection_status == "hold"
    assert hold.artifacts[0].return_code == 2


def test_output_flag_file_bytes_win_over_stdout(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    file_bytes = _pass_payload("s-file-v1")
    stdout_bytes = _pass_payload("s-stdout-v1")
    assert file_bytes != stdout_bytes
    spec = ArtifactSpec(
        artifact_id="alpha",
        canonical_path=ALLOWED_SOURCE_PATHS[0],
        generator_path=None,
        generator_version="v1",
        command=("echo", "alpha"),
        native_scope="WORKTREE",
        output_flag="--output",
    )

    def file_runner(argv: tuple[str, ...], root: Path) -> subprocess.CompletedProcess[bytes]:
        assert argv[-2] == "--output"
        out = Path(argv[-1])
        out.write_bytes(file_bytes)
        return subprocess.CompletedProcess(
            args=list(argv), returncode=0, stdout=stdout_bytes, stderr=b""
        )

    staging = repo / ".tmp" / "quality" / "file-wins"
    assert not staging.exists()
    manifest = collect_evidence(repo, staging, (spec,), runner=file_runner)
    assert manifest.status == "COMPLETE"
    rec = manifest.artifacts[0]
    assert rec.sha256 == hashlib.sha256(file_bytes).hexdigest()
    assert rec.byte_length == len(file_bytes)
    assert (staging / rec.staging_file).read_bytes() == file_bytes
    assert verify_staged_bytes(manifest, staging) == ()


def test_output_flag_missing_fail_closed(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    spec = ArtifactSpec(
        artifact_id="alpha",
        canonical_path=ALLOWED_SOURCE_PATHS[0],
        generator_path=None,
        generator_version="v1",
        command=("echo", "alpha"),
        native_scope="WORKTREE",
        output_flag="--output",
    )

    def missing_runner(argv: tuple[str, ...], root: Path) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(args=list(argv), returncode=0, stdout=b"{}", stderr=b"")

    staging = repo / ".tmp" / "quality" / "missing-out"
    assert not staging.exists()
    manifest = collect_evidence(repo, staging, (spec,), runner=missing_runner)
    assert manifest.status == "HOLD"
    assert any("missing producer output" in v for v in manifest.violations)
    assert manifest.artifacts[0].collection_status == "hold"


def test_output_flag_symlink_dir_hardlink_fail_closed(tmp_path: Path) -> None:
    def _one(sub: str, setup: Callable[[Path], None]) -> CollectionManifest:
        repo = _make_repo(tmp_path / sub)
        spec = ArtifactSpec(
            artifact_id="alpha",
            canonical_path=ALLOWED_SOURCE_PATHS[0],
            generator_path=None,
            generator_version="v1",
            command=("echo", "alpha"),
            native_scope="WORKTREE",
            output_flag="--output",
        )
        staging = repo / ".tmp" / "quality" / "out"
        assert not staging.exists()

        def runner(argv: tuple[str, ...], root: Path) -> subprocess.CompletedProcess[bytes]:
            setup(Path(argv[-1]))
            return subprocess.CompletedProcess(
                args=list(argv), returncode=0, stdout=b"{}", stderr=b""
            )

        return collect_evidence(repo, staging, (spec,), runner=runner)

    def _mk_symlink(p: Path) -> None:
        p.parent.mkdir(parents=True, exist_ok=True)
        target = p.parent / "real.json"
        target.write_bytes(b"{}")
        p.symlink_to(target)

    def _mk_dir(p: Path) -> None:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.mkdir(parents=True, exist_ok=True)

    def _mk_hardlink(p: Path) -> None:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"{}")
        buddy = p.parent / "buddy.json"
        os.link(p, buddy)

    m_sym = _one("r-sym", _mk_symlink)
    assert m_sym.status == "HOLD"
    assert any("symlink" in v for v in m_sym.violations)
    m_dir = _one("r-dir", _mk_dir)
    assert m_dir.status == "HOLD"
    assert any("directory" in v for v in m_dir.violations)
    m_hard = _one("r-hard", _mk_hardlink)
    assert m_hard.status == "HOLD"
    assert any("hard-linked" in v for v in m_hard.violations)


def test_output_flag_stale_safe_output_replaced(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    fresh = _pass_payload("s-fresh-v1")
    spec = ArtifactSpec(
        artifact_id="alpha",
        canonical_path=ALLOWED_SOURCE_PATHS[0],
        generator_path=None,
        generator_version="v1",
        command=("echo", "alpha"),
        native_scope="WORKTREE",
        output_flag="--output",
    )
    staging = repo / ".tmp" / "quality" / "stale-out"
    assert not staging.exists()

    def runner(argv: tuple[str, ...], root: Path) -> subprocess.CompletedProcess[bytes]:
        out = Path(argv[-1])
        assert not out.exists() and not out.is_symlink()
        out.write_bytes(fresh)
        return subprocess.CompletedProcess(args=list(argv), returncode=0, stdout=b"{}", stderr=b"")

    staging.mkdir(parents=True, exist_ok=True)
    (staging / "alpha.out").write_bytes(b"stale")
    (staging / "alpha.out").chmod(0o640)
    manifest = collect_evidence(repo, staging, (spec,), runner=runner)
    assert manifest.status == "COMPLETE"
    assert (staging / manifest.artifacts[0].staging_file).read_bytes() == fresh


def test_collector_rejects_tracked_output_without_mutation(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    spec = ArtifactSpec(
        artifact_id="alpha",
        canonical_path=ALLOWED_SOURCE_PATHS[0],
        generator_path=None,
        generator_version="v1",
        command=("echo", "alpha"),
        native_scope="WORKTREE",
        output_flag="--output",
    )
    staging = repo / ".tmp" / "quality" / "evidence-bundle"
    staging.mkdir(parents=True, exist_ok=True)
    out = staging / "alpha.out"
    out.write_bytes(b"tracked-output")
    _run(repo, "add", "-f", "--", out.relative_to(repo).as_posix())
    _run(repo, "commit", "-m", "track output")
    _run(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    before = out.read_bytes()
    called: list[tuple[tuple[str, ...], Path]] = []

    def runner(argv: tuple[str, ...], root: Path) -> subprocess.CompletedProcess[bytes]:
        called.append((argv, root))
        return subprocess.CompletedProcess(args=list(argv), returncode=0, stdout=b"{}", stderr=b"")

    with pytest.raises(ValueError, match="tracked"):
        collect_evidence(repo, staging, (spec,), runner=runner)
    assert called == []
    assert out.read_bytes() == before


def test_runtime_policy_drift_hold(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _make_repo(tmp_path)
    payloads = _payloads_ok(repo)
    manifest, staging = _collect_ok(repo, "evidence-bundle", payloads)
    assert assemble_bundle(repo, manifest, staging, repo / "docs" / "quality").status == "COMPLETE"
    bundle = _bundle_now(repo)
    assert validate_bundle_diff(repo, manifest.subject_commit, bundle, manifest) == ()
    monkeypatch.setattr("quality.evidence_bundle_validate._RUNTIME_POLICY_SHA256", "0" * 64)
    probs = validate_bundle_diff(repo, manifest.subject_commit, bundle, manifest)
    assert probs != ()
    assert any("runtime admission policy" in v for v in probs)
