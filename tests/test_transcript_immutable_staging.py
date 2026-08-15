from __future__ import annotations

import hashlib
import os
import stat
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from pydantic import ValidationError

import transcripts.immutable_staging as staging
from transcripts.immutable_staging import (
    StagedTranscriptArtifact,
    TranscriptStagingError,
    read_staged_transcript,
    stage_transcript_artifact,
)


def _digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _call_untyped(
    function: Callable[..., object],
    *args: object,
    **kwargs: object,
) -> object:
    return function(*args, **kwargs)


def _stage(
    source: Path,
    private_root: Path,
    *,
    payload: bytes | None = None,
    max_bytes: int = 1024,
) -> StagedTranscriptArtifact:
    expected = source.read_bytes() if payload is None else payload
    return stage_transcript_artifact(
        source,
        private_root,
        expected_sha256=_digest(expected),
        expected_size_bytes=len(expected),
        max_bytes=max_bytes,
    )


def _read(artifact: StagedTranscriptArtifact) -> bytes:
    return read_staged_transcript(
        artifact,
        trusted_staging_root=artifact.staging_root,
        trusted_staging_root_device=artifact.staging_root_device,
        trusted_staging_root_inode=artifact.staging_root_inode,
        expected_source_path=artifact.source_path,
        expected_source_device=artifact.source_device,
        expected_source_inode=artifact.source_inode,
        expected_sha256=artifact.sha256,
        expected_size_bytes=artifact.size_bytes,
    )


def test_consumer_uses_only_snapshot_after_source_changes(tmp_path: Path) -> None:
    source = tmp_path / "source.txt"
    private_root = tmp_path / "private"
    private_root.mkdir()
    payload = b"operator: prepared remarks\n"
    source.write_bytes(payload)
    artifact = stage_transcript_artifact(
        source,
        private_root,
        expected_sha256=_digest(payload),
        expected_size_bytes=len(payload),
        max_bytes=1024,
    )
    source.write_bytes(b"substituted after staging")
    source.unlink()

    assert _read(artifact) == payload


def test_stage_is_content_addressed_and_replay_is_exact(tmp_path: Path) -> None:
    source = tmp_path / "source.txt"
    private_root = tmp_path / "private"
    private_root.mkdir()
    payload = b"same transcript bytes"
    source.write_bytes(payload)

    first = _stage(source, private_root)
    second = _stage(source, private_root)

    assert first == second
    assert first.sha256 == _digest(payload)
    assert first.size_bytes == len(payload)
    assert first.staged_path == private_root.resolve() / f"{first.sha256}.transcript"
    assert tuple(private_root.iterdir()) == (first.staged_path,)


def test_concurrent_replay_commits_one_complete_snapshot(tmp_path: Path) -> None:
    source = tmp_path / "source.txt"
    private_root = tmp_path / "private"
    private_root.mkdir()
    payload = b"concurrent immutable transcript"
    source.write_bytes(payload)

    def stage_one(_: int) -> StagedTranscriptArtifact:
        return _stage(source, private_root, payload=payload)

    with ThreadPoolExecutor(max_workers=8) as executor:
        artifacts = tuple(executor.map(stage_one, range(16)))

    assert len(set(artifacts)) == 1
    assert _read(artifacts[0]) == payload
    assert tuple(private_root.iterdir()) == (artifacts[0].staged_path,)


def test_expected_digest_mismatch_leaves_private_root_empty(tmp_path: Path) -> None:
    source = tmp_path / "source.txt"
    private_root = tmp_path / "private"
    private_root.mkdir()
    source.write_bytes(b"unexpected")

    with pytest.raises(TranscriptStagingError, match="expected SHA-256"):
        stage_transcript_artifact(
            source,
            private_root,
            expected_sha256="0" * 64,
            expected_size_bytes=len(b"unexpected"),
            max_bytes=1024,
        )

    assert list(private_root.iterdir()) == []


def test_existing_content_address_collision_fails_closed(tmp_path: Path) -> None:
    source = tmp_path / "source.txt"
    private_root = tmp_path / "private"
    private_root.mkdir()
    payload = b"authorized"
    source.write_bytes(payload)
    target = private_root / f"{_digest(payload)}.transcript"
    target.write_bytes(b"forged collision")

    with pytest.raises(TranscriptStagingError, match="collision"):
        _stage(source, private_root)

    assert target.read_bytes() == b"forged collision"


def test_consumer_rejects_staged_byte_tampering(tmp_path: Path) -> None:
    source = tmp_path / "source.txt"
    private_root = tmp_path / "private"
    private_root.mkdir()
    source.write_bytes(b"authorized")
    artifact = _stage(source, private_root)
    artifact.staged_path.chmod(0o600)
    artifact.staged_path.write_bytes(b"substitute")
    artifact.staged_path.chmod(stat.S_IREAD)

    with pytest.raises(TranscriptStagingError, match="staged SHA-256"):
        _read(artifact)


def test_consumer_rejects_writable_snapshot_even_when_bytes_match(tmp_path: Path) -> None:
    source = tmp_path / "source.txt"
    private_root = tmp_path / "private"
    private_root.mkdir()
    source.write_bytes(b"authorized")
    artifact = _stage(source, private_root)
    artifact.staged_path.chmod(stat.S_IREAD | stat.S_IWRITE)

    with pytest.raises(TranscriptStagingError, match="must remain read-only"):
        _read(artifact)


def test_consumer_revalidates_forged_model_construct_path(tmp_path: Path) -> None:
    source = tmp_path / "source.txt"
    private_root = tmp_path / "private"
    private_root.mkdir()
    source.write_bytes(b"authorized")
    artifact = _stage(source, private_root)
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"authorized")
    forged = StagedTranscriptArtifact.model_construct(
        source_path=artifact.source_path,
        source_device=artifact.source_device,
        source_inode=artifact.source_inode,
        staging_root=artifact.staging_root,
        staging_root_device=artifact.staging_root_device,
        staging_root_inode=artifact.staging_root_inode,
        staged_path=outside,
        sha256=artifact.sha256,
        size_bytes=artifact.size_bytes,
    )

    with pytest.raises(TranscriptStagingError, match="canonical staged path"):
        read_staged_transcript(
            forged,
            trusted_staging_root=artifact.staging_root,
            trusted_staging_root_device=artifact.staging_root_device,
            trusted_staging_root_inode=artifact.staging_root_inode,
            expected_source_path=artifact.source_path,
            expected_source_device=artifact.source_device,
            expected_source_inode=artifact.source_inode,
            expected_sha256=artifact.sha256,
            expected_size_bytes=artifact.size_bytes,
        )


def test_stage_rejects_source_symlink(tmp_path: Path) -> None:
    source = tmp_path / "source.txt"
    source.write_bytes(b"authorized")
    link = tmp_path / "source-link.txt"
    try:
        link.symlink_to(source)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")
    private_root = tmp_path / "private"
    private_root.mkdir()

    with pytest.raises(TranscriptStagingError, match="symlink or reparse"):
        stage_transcript_artifact(
            link,
            private_root,
            expected_sha256=_digest(b"authorized"),
            expected_size_bytes=len(b"authorized"),
            max_bytes=1024,
        )


def test_stage_rejects_staging_root_symlink(tmp_path: Path) -> None:
    source = tmp_path / "source.txt"
    source.write_bytes(b"authorized")
    real_root = tmp_path / "private"
    real_root.mkdir()
    link_root = tmp_path / "private-link"
    try:
        link_root.symlink_to(real_root, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")

    with pytest.raises(TranscriptStagingError, match="symlink or reparse"):
        _stage(source, link_root)


@pytest.mark.parametrize("reparse_subject", ["source", "root"])
def test_stage_rejects_simulated_windows_reparse_points(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reparse_subject: str,
) -> None:
    source = tmp_path / "source.txt"
    private_root = tmp_path / "private"
    private_root.mkdir()
    source.write_bytes(b"authorized")
    subject = source if reparse_subject == "source" else private_root
    subject_metadata = subject.lstat()
    subject_identity = (int(subject_metadata.st_dev), int(subject_metadata.st_ino))

    def simulate_reparse(metadata: os.stat_result) -> bool:
        observed = (int(metadata.st_dev), int(metadata.st_ino))
        return observed == subject_identity

    monkeypatch.setattr(staging, "_has_reparse_attribute", simulate_reparse)

    with pytest.raises(TranscriptStagingError, match="symlink or reparse"):
        _stage(source, private_root)


def test_read_rejects_simulated_staged_reparse_point(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.txt"
    private_root = tmp_path / "private"
    private_root.mkdir()
    source.write_bytes(b"authorized")
    artifact = _stage(source, private_root)
    target_metadata = artifact.staged_path.lstat()
    target_identity = (int(target_metadata.st_dev), int(target_metadata.st_ino))

    def simulate_reparse(metadata: os.stat_result) -> bool:
        observed = (int(metadata.st_dev), int(metadata.st_ino))
        return observed == target_identity

    monkeypatch.setattr(staging, "_has_reparse_attribute", simulate_reparse)

    with pytest.raises(TranscriptStagingError, match="symlink or reparse"):
        _read(artifact)


def test_consumer_rejects_hardlinked_staged_file(tmp_path: Path) -> None:
    source = tmp_path / "source.txt"
    private_root = tmp_path / "private"
    private_root.mkdir()
    source.write_bytes(b"authorized")
    artifact = _stage(source, private_root)
    alias = tmp_path / "alias.txt"
    try:
        os.link(artifact.staged_path, alias)
    except OSError as exc:
        pytest.skip(f"hard links unavailable: {exc}")

    with pytest.raises(TranscriptStagingError, match="hard link"):
        _read(artifact)


def test_stage_enforces_explicit_size_bound(tmp_path: Path) -> None:
    source = tmp_path / "source.txt"
    private_root = tmp_path / "private"
    private_root.mkdir()
    source.write_bytes(b"12345")

    with pytest.raises(TranscriptStagingError, match="maximum size"):
        _stage(source, private_root, max_bytes=4)

    assert list(private_root.iterdir()) == []


def test_stage_requires_caller_digest_and_exact_length(tmp_path: Path) -> None:
    source = tmp_path / "source.txt"
    private_root = tmp_path / "private"
    private_root.mkdir()
    source.write_bytes(b"authorized")

    with pytest.raises(TypeError):
        _call_untyped(stage_transcript_artifact, source, private_root, max_bytes=1024)
    with pytest.raises(TypeError):
        _call_untyped(
            stage_transcript_artifact,
            source,
            private_root,
            expected_sha256=_digest(b"authorized"),
            max_bytes=1024,
        )


def test_public_boundary_rejects_wrong_runtime_types(tmp_path: Path) -> None:
    source = tmp_path / "source.txt"
    private_root = tmp_path / "private"
    private_root.mkdir()
    payload = b"authorized"
    source.write_bytes(payload)

    with pytest.raises(TranscriptStagingError, match="pathlib Path"):
        _call_untyped(
            stage_transcript_artifact,
            str(source),
            private_root,
            expected_sha256=_digest(payload),
            expected_size_bytes=len(payload),
            max_bytes=1024,
        )
    with pytest.raises(TranscriptStagingError, match="expected byte length"):
        _call_untyped(
            stage_transcript_artifact,
            source,
            private_root,
            expected_sha256=_digest(payload),
            expected_size_bytes=str(len(payload)),
            max_bytes=1024,
        )
    artifact = _stage(source, private_root)
    with pytest.raises(TranscriptStagingError, match="source identity"):
        _call_untyped(
            read_staged_transcript,
            artifact,
            trusted_staging_root=artifact.staging_root,
            trusted_staging_root_device=artifact.staging_root_device,
            trusted_staging_root_inode=artifact.staging_root_inode,
            expected_source_path=artifact.source_path,
            expected_source_device=True,
            expected_source_inode=artifact.source_inode,
            expected_sha256=artifact.sha256,
            expected_size_bytes=artifact.size_bytes,
        )


def test_expected_length_mismatch_leaves_private_root_empty(tmp_path: Path) -> None:
    source = tmp_path / "source.txt"
    private_root = tmp_path / "private"
    private_root.mkdir()
    payload = b"authorized"
    source.write_bytes(payload)

    with pytest.raises(TranscriptStagingError, match="expected byte length"):
        stage_transcript_artifact(
            source,
            private_root,
            expected_sha256=_digest(payload),
            expected_size_bytes=len(payload) + 1,
            max_bytes=1024,
        )

    assert list(private_root.iterdir()) == []


def test_read_requires_independent_root_source_digest_and_length(tmp_path: Path) -> None:
    source = tmp_path / "source.txt"
    private_root = tmp_path / "private"
    private_root.mkdir()
    payload = b"authorized"
    source.write_bytes(payload)
    artifact = _stage(source, private_root)

    with pytest.raises(TypeError):
        _call_untyped(read_staged_transcript, artifact)
    wrong_root = tmp_path / "other-root"
    wrong_root.mkdir()
    with pytest.raises(TranscriptStagingError, match="trusted staging root"):
        read_staged_transcript(
            artifact,
            trusted_staging_root=wrong_root,
            trusted_staging_root_device=artifact.staging_root_device,
            trusted_staging_root_inode=artifact.staging_root_inode,
            expected_source_path=source.resolve(),
            expected_source_device=artifact.source_device,
            expected_source_inode=artifact.source_inode,
            expected_sha256=_digest(payload),
            expected_size_bytes=len(payload),
        )

    forged_root_identity = artifact.model_copy(
        update={"staging_root_inode": artifact.staging_root_inode + 1}
    )
    with pytest.raises(TranscriptStagingError, match="trusted staging root identity"):
        read_staged_transcript(
            forged_root_identity,
            trusted_staging_root=artifact.staging_root,
            trusted_staging_root_device=artifact.staging_root_device,
            trusted_staging_root_inode=artifact.staging_root_inode,
            expected_source_path=artifact.source_path,
            expected_source_device=artifact.source_device,
            expected_source_inode=artifact.source_inode,
            expected_sha256=artifact.sha256,
            expected_size_bytes=artifact.size_bytes,
        )
    forged_source_identity = artifact.model_copy(update={"source_inode": artifact.source_inode + 1})
    with pytest.raises(TranscriptStagingError, match="expected source identity"):
        read_staged_transcript(
            forged_source_identity,
            trusted_staging_root=artifact.staging_root,
            trusted_staging_root_device=artifact.staging_root_device,
            trusted_staging_root_inode=artifact.staging_root_inode,
            expected_source_path=artifact.source_path,
            expected_source_device=artifact.source_device,
            expected_source_inode=artifact.source_inode,
            expected_sha256=artifact.sha256,
            expected_size_bytes=artifact.size_bytes,
        )


def test_coordinated_receipt_root_source_and_digest_forgery_is_rejected(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.txt"
    trusted_root = tmp_path / "trusted"
    attacker_root = tmp_path / "attacker"
    trusted_root.mkdir()
    attacker_root.mkdir()
    payload = b"authorized"
    substituted = b"substitute"
    source.write_bytes(payload)
    artifact = _stage(source, trusted_root)
    attacker_source = tmp_path / "attacker-source.txt"
    attacker_source.write_bytes(substituted)
    attacker_target = attacker_root / f"{_digest(substituted)}.transcript"
    attacker_target.write_bytes(substituted)
    attacker_target.chmod(stat.S_IREAD)
    attacker_source_metadata = attacker_source.stat()
    attacker_root_metadata = attacker_root.stat()
    forged = artifact.model_copy(
        update={
            "source_path": attacker_source.resolve(),
            "source_device": int(attacker_source_metadata.st_dev),
            "source_inode": int(attacker_source_metadata.st_ino),
            "staging_root": attacker_root.resolve(),
            "staging_root_device": int(attacker_root_metadata.st_dev),
            "staging_root_inode": int(attacker_root_metadata.st_ino),
            "staged_path": attacker_target.resolve(),
            "sha256": _digest(substituted),
            "size_bytes": len(substituted),
        }
    )

    with pytest.raises(TranscriptStagingError, match="trusted staging root"):
        read_staged_transcript(
            forged,
            trusted_staging_root=trusted_root.resolve(),
            trusted_staging_root_device=artifact.staging_root_device,
            trusted_staging_root_inode=artifact.staging_root_inode,
            expected_source_path=source.resolve(),
            expected_source_device=artifact.source_device,
            expected_source_inode=artifact.source_inode,
            expected_sha256=_digest(payload),
            expected_size_bytes=len(payload),
        )


def test_model_construct_and_copy_cannot_forge_expected_length(tmp_path: Path) -> None:
    source = tmp_path / "source.txt"
    private_root = tmp_path / "private"
    private_root.mkdir()
    payload = b"authorized"
    source.write_bytes(payload)
    artifact = _stage(source, private_root)
    constructed = StagedTranscriptArtifact.model_construct(
        source_path=artifact.source_path,
        source_device=artifact.source_device,
        source_inode=artifact.source_inode,
        staging_root=artifact.staging_root,
        staging_root_device=artifact.staging_root_device,
        staging_root_inode=artifact.staging_root_inode,
        staged_path=artifact.staged_path,
        sha256=artifact.sha256,
        size_bytes=len(payload) + 1,
    )
    copied = artifact.model_copy(update={"size_bytes": len(payload) + 1})

    for forged in (constructed, copied):
        with pytest.raises(TranscriptStagingError, match="expected byte length"):
            read_staged_transcript(
                forged,
                trusted_staging_root=private_root.resolve(),
                trusted_staging_root_device=artifact.staging_root_device,
                trusted_staging_root_inode=artifact.staging_root_inode,
                expected_source_path=source.resolve(),
                expected_source_device=artifact.source_device,
                expected_source_inode=artifact.source_inode,
                expected_sha256=_digest(payload),
                expected_size_bytes=len(payload),
            )


def test_source_swap_between_lstat_and_open_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.txt"
    replacement = tmp_path / "replacement.txt"
    private_root = tmp_path / "private"
    private_root.mkdir()
    payload = b"same bytes"
    source.write_bytes(payload)
    replacement.write_bytes(payload)
    swapped = False

    def swap_after_lstat(path: Path, *, label: str) -> os.stat_result:
        nonlocal swapped
        metadata = path.lstat()
        if label == "source transcript" and not swapped:
            swapped = True
            source.unlink()
            replacement.rename(source)
        return metadata

    monkeypatch.setattr(staging, "_lstat", swap_after_lstat)

    with pytest.raises(TranscriptStagingError, match="identity changed"):
        _stage(source, private_root, payload=payload)
    assert list(private_root.iterdir()) == []


def test_root_swap_between_lstat_and_open_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.txt"
    private_root = tmp_path / "private"
    replacement_root = tmp_path / "replacement-root"
    moved_root = tmp_path / "moved-root"
    private_root.mkdir()
    replacement_root.mkdir()
    payload = b"authorized"
    source.write_bytes(payload)
    artifact = _stage(source, private_root)
    replacement_target = replacement_root / artifact.staged_path.name
    replacement_target.write_bytes(payload)
    replacement_target.chmod(stat.S_IREAD)
    swapped = False

    def swap_after_lstat(path: Path, *, label: str) -> os.stat_result:
        nonlocal swapped
        metadata = path.lstat()
        if label == "staging root" and not swapped:
            swapped = True
            private_root.rename(moved_root)
            replacement_root.rename(private_root)
        return metadata

    monkeypatch.setattr(staging, "_lstat", swap_after_lstat)

    with pytest.raises(TranscriptStagingError, match="identity changed while opening"):
        _read(artifact)


def test_staged_target_swap_between_lstat_and_open_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.txt"
    private_root = tmp_path / "private"
    private_root.mkdir()
    payload = b"same bytes"
    source.write_bytes(payload)
    artifact = _stage(source, private_root)
    replacement = tmp_path / "replacement.txt"
    replacement.write_bytes(payload)
    replacement.chmod(stat.S_IREAD)
    swapped = False

    def swap_after_lstat(path: Path, *, label: str) -> os.stat_result:
        nonlocal swapped
        metadata = path.lstat()
        if label == "staged transcript" and not swapped:
            swapped = True
            artifact.staged_path.chmod(stat.S_IWRITE)
            artifact.staged_path.unlink()
            replacement.rename(artifact.staged_path)
            artifact.staged_path.chmod(stat.S_IREAD)
        return metadata

    monkeypatch.setattr(staging, "_lstat", swap_after_lstat)

    with pytest.raises(TranscriptStagingError, match="identity changed"):
        _read(artifact)


def test_source_hardlink_is_rejected(tmp_path: Path) -> None:
    source = tmp_path / "source.txt"
    alias = tmp_path / "alias.txt"
    private_root = tmp_path / "private"
    private_root.mkdir()
    payload = b"authorized"
    source.write_bytes(payload)
    try:
        os.link(source, alias)
    except OSError as exc:
        pytest.skip(f"hard links unavailable: {exc}")

    with pytest.raises(TranscriptStagingError, match="hard link"):
        _stage(source, private_root, payload=payload)


def test_atomic_install_failure_cleans_temporary_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.txt"
    private_root = tmp_path / "private"
    private_root.mkdir()
    payload = b"authorized"
    source.write_bytes(payload)

    def fail_install(*_args: object, **_kwargs: object) -> None:
        raise OSError("injected install failure")

    monkeypatch.setattr(staging, "_atomic_install_no_replace", fail_install, raising=False)
    with pytest.raises(TranscriptStagingError, match="could not be committed"):
        _stage(source, private_root, payload=payload)

    assert list(private_root.iterdir()) == []


def test_substituted_temporary_cannot_poison_canonical_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.txt"
    private_root = tmp_path / "private"
    private_root.mkdir()
    payload = b"authorized"
    poison = b"attacker-controlled"
    source.write_bytes(payload)
    replacement = tmp_path / "replacement.tmp"
    replacement.write_bytes(poison)
    candidate: object = vars(staging)["_atomic_install_no_replace"]
    assert callable(candidate)
    substitution_blocked = False

    def attempt_substitution_then_install(*args: object, **kwargs: object) -> object:
        nonlocal substitution_blocked
        named_temporaries = tuple(private_root.glob("*.tmp"))
        if not named_temporaries:
            substitution_blocked = True
        else:
            assert len(named_temporaries) == 1
            temporary = named_temporaries[0]
            try:
                temporary.chmod(stat.S_IWRITE)
                temporary.unlink()
                replacement.rename(temporary)
            except OSError:
                substitution_blocked = True
        return candidate(*args, **kwargs)

    monkeypatch.setattr(
        staging,
        "_atomic_install_no_replace",
        attempt_substitution_then_install,
        raising=False,
    )

    with pytest.raises(TranscriptStagingError, match="remain read-only"):
        _stage(source, private_root, payload=payload)

    assert substitution_blocked
    assert not (private_root / f"{_digest(payload)}.transcript").exists()
    assert replacement.read_bytes() == poison


def test_cleanup_never_deletes_a_replacement_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.txt"
    private_root = tmp_path / "private"
    private_root.mkdir()
    payload = b"authorized"
    source.write_bytes(payload)
    victim = tmp_path / "victim.txt"
    victim.write_bytes(b"must survive")
    substitution_blocked = False

    def attempt_substitution_then_fail(*_args: object, **_kwargs: object) -> None:
        nonlocal substitution_blocked
        named_temporaries = tuple(private_root.glob("*.tmp"))
        if not named_temporaries:
            substitution_blocked = True
        else:
            assert len(named_temporaries) == 1
            temporary = named_temporaries[0]
            try:
                temporary.chmod(stat.S_IWRITE)
                temporary.unlink()
                victim.rename(temporary)
            except OSError:
                substitution_blocked = True
        raise OSError("injected install failure")

    monkeypatch.setattr(
        staging,
        "_atomic_install_no_replace",
        attempt_substitution_then_fail,
        raising=False,
    )

    with pytest.raises(TranscriptStagingError, match="could not be committed"):
        _stage(source, private_root, payload=payload)

    assert substitution_blocked
    assert victim.read_bytes() == b"must survive"
    assert list(private_root.iterdir()) == []


def test_cleanup_never_chmods_a_replacement_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.txt"
    private_root = tmp_path / "private"
    private_root.mkdir()
    payload = b"authorized"
    source.write_bytes(payload)
    victim = tmp_path / "victim.txt"
    victim.write_bytes(b"must stay read-only")
    victim.chmod(stat.S_IREAD)
    substitution_blocked = False

    def attempt_substitution_then_fail(*_args: object, **_kwargs: object) -> None:
        nonlocal substitution_blocked
        named_temporaries = tuple(private_root.glob("*.tmp"))
        if not named_temporaries:
            substitution_blocked = True
        else:
            assert len(named_temporaries) == 1
            temporary = named_temporaries[0]
            try:
                temporary.chmod(stat.S_IWRITE)
                temporary.unlink()
                victim.rename(temporary)
            except OSError:
                substitution_blocked = True
        raise OSError("injected install failure")

    monkeypatch.setattr(
        staging,
        "_atomic_install_no_replace",
        attempt_substitution_then_fail,
        raising=False,
    )

    with pytest.raises(TranscriptStagingError, match="could not be committed"):
        _stage(source, private_root, payload=payload)

    assert substitution_blocked
    assert victim.read_bytes() == b"must stay read-only"
    assert not victim.stat().st_mode & stat.S_IWUSR


def test_cleanup_denial_is_surfaced(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.txt"
    private_root = tmp_path / "private"
    private_root.mkdir()
    payload = b"authorized"
    source.write_bytes(payload)

    def fail_install(*_args: object, **_kwargs: object) -> None:
        raise OSError("injected install failure")

    def deny_owned_cleanup(*_args: object, **_kwargs: object) -> None:
        raise OSError("injected cleanup denial")

    monkeypatch.setattr(staging, "_atomic_install_no_replace", fail_install, raising=False)
    monkeypatch.setattr(staging, "_delete_owned_temporary", deny_owned_cleanup)

    with pytest.raises(TranscriptStagingError, match="cleanup could not be completed"):
        _stage(source, private_root, payload=payload)


def test_failed_installed_target_is_removed_through_owned_handle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.txt"
    private_root = tmp_path / "private"
    private_root.mkdir()
    payload = b"authorized"
    source.write_bytes(payload)
    candidate: object = vars(staging)["_validate_owned_temporary"]
    assert callable(candidate)

    def fail_after_installed_validation(*args: object, **kwargs: object) -> object:
        result = candidate(*args, **kwargs)
        if kwargs.get("installed") is True:
            raise TranscriptStagingError("injected installed-target failure")
        return result

    monkeypatch.setattr(
        staging,
        "_validate_owned_temporary",
        fail_after_installed_validation,
    )

    with pytest.raises(TranscriptStagingError, match="injected installed-target failure"):
        _stage(source, private_root, payload=payload)

    assert list(private_root.iterdir()) == []


@pytest.mark.skipif(os.name != "nt", reason="Windows root-handle contract")
def test_windows_open_root_handle_blocks_commit_redirection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.txt"
    private_root = tmp_path / "private"
    replacement_root = tmp_path / "replacement-root"
    moved_root = tmp_path / "moved-root"
    private_root.mkdir()
    replacement_root.mkdir()
    payload = b"authorized"
    source.write_bytes(payload)
    candidate: object = vars(staging)["_commit_snapshot"]
    assert callable(candidate)
    replacement_blocked = False

    def attempt_root_replace_then_commit(*args: object, **kwargs: object) -> object:
        nonlocal replacement_blocked
        try:
            private_root.rename(moved_root)
            replacement_root.rename(private_root)
        except OSError:
            replacement_blocked = True
        return candidate(*args, **kwargs)

    monkeypatch.setattr(staging, "_commit_snapshot", attempt_root_replace_then_commit)

    artifact = _stage(source, private_root, payload=payload)

    assert replacement_blocked
    assert _read(artifact) == payload


@pytest.mark.skipif(os.name == "nt", reason="POSIX dir_fd contract")
def test_posix_root_replacement_cannot_redirect_commit_side_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.txt"
    private_root = tmp_path / "private"
    replacement_root = tmp_path / "replacement-root"
    moved_root = tmp_path / "moved-root"
    private_root.mkdir()
    replacement_root.mkdir()
    payload = b"authorized"
    source.write_bytes(payload)
    candidate: object = vars(staging)["_commit_snapshot"]
    assert callable(candidate)

    def replace_root_then_commit(*args: object, **kwargs: object) -> object:
        private_root.rename(moved_root)
        replacement_root.rename(private_root)
        return candidate(*args, **kwargs)

    monkeypatch.setattr(staging, "_commit_snapshot", replace_root_then_commit)

    with pytest.raises(TranscriptStagingError, match="staging root identity changed"):
        _stage(source, private_root, payload=payload)

    assert list(private_root.iterdir()) == []


def test_stored_receipt_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        StagedTranscriptArtifact.model_validate(
            {
                "source_path": Path("C:/source.txt"),
                "staging_root": Path("C:/private"),
                "staged_path": Path("C:/private/" + "a" * 64 + ".transcript"),
                "sha256": "a" * 64,
                "size_bytes": 1,
                "network_allowed": True,
            }
        )
