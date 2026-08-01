from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

import provenance.immutable_artifact as immutable
from provenance.immutable_artifact import (
    ImmutableArtifactConflictError,
    assert_artifact_unchanged,
    publish_text_no_clobber,
    read_stable_artifact,
)


def test_stable_artifact_snapshot_detects_later_replacement(tmp_path: Path) -> None:
    path = tmp_path / "receipt.json"
    path.write_bytes(b'{"value":1}\n')

    snapshot, payload = read_stable_artifact(path)

    assert payload == b'{"value":1}\n'
    assert len(snapshot.file_sha256) == 64
    assert_artifact_unchanged(snapshot)
    path.write_bytes(b'{"value":2}\n')
    with pytest.raises(ImmutableArtifactConflictError, match="changed"):
        assert_artifact_unchanged(snapshot)


def test_stable_artifact_rejects_transient_swap_before_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "receipt.json"
    replacement = tmp_path / "replacement.json"
    path.write_bytes(b"original\n")
    replacement.write_bytes(b"replacement\n")
    real_open = immutable.os.open
    swapped = False

    def open_after_swap(
        target: str | os.PathLike[str],
        flags: int,
        mode: int = 0,
    ) -> int:
        nonlocal swapped
        if not swapped and Path(str(target)) == path:
            swapped = True
            replacement.replace(path)
        return real_open(target, flags, mode)

    monkeypatch.setattr(immutable.os, "open", open_after_swap)
    with pytest.raises(ImmutableArtifactConflictError, match="pinned"):
        read_stable_artifact(path)


def test_stable_artifact_rejects_symlink_component(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    (target / "receipt.json").write_bytes(b"{}\n")
    link = tmp_path / "link"
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation is unavailable")

    with pytest.raises(ImmutableArtifactConflictError, match="reparse"):
        read_stable_artifact(link / "receipt.json")


def test_publication_removes_owned_link_when_parent_identity_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "receipt.json"
    real_stat = immutable.os.stat
    parent_swap_reported = False

    def stat_with_parent_swap(
        path: str | os.PathLike[str],
        *,
        dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> os.stat_result:
        nonlocal parent_swap_reported
        result = real_stat(path, dir_fd=dir_fd, follow_symlinks=follow_symlinks)
        try:
            real_stat(destination)
            destination_exists = True
        except FileNotFoundError:
            destination_exists = False
        if (
            Path(path) == tmp_path
            and not follow_symlinks
            and destination_exists
            and not parent_swap_reported
        ):
            parent_swap_reported = True
            return cast(
                os.stat_result,
                SimpleNamespace(
                    st_dev=result.st_dev,
                    st_ino=int(result.st_ino) + 1,
                    st_size=result.st_size,
                    st_mtime_ns=result.st_mtime_ns,
                    st_ctime_ns=result.st_ctime_ns,
                ),
            )
        return result

    monkeypatch.setattr(immutable.os, "stat", stat_with_parent_swap)
    with pytest.raises(ImmutableArtifactConflictError, match="parent changed"):
        publish_text_no_clobber(destination, "{}")
    assert not destination.exists()
