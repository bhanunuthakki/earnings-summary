from __future__ import annotations

import hashlib
import os
import stat
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from transcripts.immutable_staging import (
    StagedTranscriptArtifact,
    TranscriptStagingError,
    read_staged_transcript,
    stage_transcript_artifact,
)


def _digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


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
        max_bytes=1024,
    )
    source.write_bytes(b"substituted after staging")
    source.unlink()

    assert read_staged_transcript(artifact) == payload


def test_stage_is_content_addressed_and_replay_is_exact(tmp_path: Path) -> None:
    source = tmp_path / "source.txt"
    private_root = tmp_path / "private"
    private_root.mkdir()
    payload = b"same transcript bytes"
    source.write_bytes(payload)

    first = stage_transcript_artifact(source, private_root, max_bytes=1024)
    second = stage_transcript_artifact(source, private_root, max_bytes=1024)

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
        return stage_transcript_artifact(source, private_root, max_bytes=1024)

    with ThreadPoolExecutor(max_workers=8) as executor:
        artifacts = tuple(executor.map(stage_one, range(16)))

    assert len(set(artifacts)) == 1
    assert read_staged_transcript(artifacts[0]) == payload
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
        stage_transcript_artifact(source, private_root, max_bytes=1024)

    assert target.read_bytes() == b"forged collision"


def test_consumer_rejects_staged_byte_tampering(tmp_path: Path) -> None:
    source = tmp_path / "source.txt"
    private_root = tmp_path / "private"
    private_root.mkdir()
    source.write_bytes(b"authorized")
    artifact = stage_transcript_artifact(source, private_root, max_bytes=1024)
    artifact.staged_path.chmod(0o600)
    artifact.staged_path.write_bytes(b"substitute")
    artifact.staged_path.chmod(stat.S_IREAD)

    with pytest.raises(TranscriptStagingError, match="staged SHA-256"):
        read_staged_transcript(artifact)


def test_consumer_rejects_writable_snapshot_even_when_bytes_match(tmp_path: Path) -> None:
    source = tmp_path / "source.txt"
    private_root = tmp_path / "private"
    private_root.mkdir()
    source.write_bytes(b"authorized")
    artifact = stage_transcript_artifact(source, private_root, max_bytes=1024)
    artifact.staged_path.chmod(stat.S_IREAD | stat.S_IWRITE)

    with pytest.raises(TranscriptStagingError, match="must remain read-only"):
        read_staged_transcript(artifact)


def test_consumer_revalidates_forged_model_construct_path(tmp_path: Path) -> None:
    source = tmp_path / "source.txt"
    private_root = tmp_path / "private"
    private_root.mkdir()
    source.write_bytes(b"authorized")
    artifact = stage_transcript_artifact(source, private_root, max_bytes=1024)
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"authorized")
    forged = StagedTranscriptArtifact.model_construct(
        source_path=artifact.source_path,
        staging_root=artifact.staging_root,
        staged_path=outside,
        sha256=artifact.sha256,
        size_bytes=artifact.size_bytes,
    )

    with pytest.raises(TranscriptStagingError, match="canonical staged path"):
        read_staged_transcript(forged)


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
        stage_transcript_artifact(link, private_root, max_bytes=1024)


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
        stage_transcript_artifact(source, link_root, max_bytes=1024)


def test_consumer_rejects_hardlinked_staged_file(tmp_path: Path) -> None:
    source = tmp_path / "source.txt"
    private_root = tmp_path / "private"
    private_root.mkdir()
    source.write_bytes(b"authorized")
    artifact = stage_transcript_artifact(source, private_root, max_bytes=1024)
    alias = tmp_path / "alias.txt"
    try:
        os.link(artifact.staged_path, alias)
    except OSError as exc:
        pytest.skip(f"hard links unavailable: {exc}")

    with pytest.raises(TranscriptStagingError, match="hard link"):
        read_staged_transcript(artifact)


def test_stage_enforces_explicit_size_bound(tmp_path: Path) -> None:
    source = tmp_path / "source.txt"
    private_root = tmp_path / "private"
    private_root.mkdir()
    source.write_bytes(b"12345")

    with pytest.raises(TranscriptStagingError, match="maximum size"):
        stage_transcript_artifact(source, private_root, max_bytes=4)

    assert list(private_root.iterdir()) == []
