"""Regular-file bounds and pinned identities; no native Windows qualification."""

from __future__ import annotations

import ctypes
import errno
import json
import os
import stat
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from compute import evidence_snapshot
from provenance import immutable_artifact
from provenance.immutable_artifact import ImmutableArtifactConflictError, read_stable_artifact


def test_bounded_regular_read_and_oversized_file(tmp_path: Path) -> None:
    source = tmp_path / "source.bin"
    source.write_bytes(b"exact")
    snapshot, payload = read_stable_artifact(source, max_bytes=5, allowed_root=tmp_path)
    assert payload == b"exact" and snapshot.size_bytes == 5
    with pytest.raises(ImmutableArtifactConflictError, match="byte limit"):
        read_stable_artifact(source, max_bytes=4, allowed_root=tmp_path)
    with pytest.raises(ValueError, match="positive integer"):
        read_stable_artifact(source, max_bytes=True)
    with pytest.raises(ImmutableArtifactConflictError, match="source root"):
        read_stable_artifact(source, max_bytes=5, allowed_root=tmp_path / "other")


@pytest.mark.skipif(os.name == "nt", reason="POSIX FIFO primitive")
def test_regular_to_fifo_swap_is_nonblocking_and_closes_handles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.bin"
    source.write_bytes(b"exact")
    original_open = os.open
    opened: list[int] = []

    def replace_before_open(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        if str(path) == source.name:
            assert flags & os.O_NONBLOCK
            source.unlink()
            os.mkfifo(source)
        descriptor = original_open(path, flags, mode, dir_fd=dir_fd)
        opened.append(descriptor)
        return descriptor

    monkeypatch.setattr(os, "open", replace_before_open)
    with pytest.raises(ImmutableArtifactConflictError, match="regular file"):
        read_stable_artifact(source, max_bytes=5, allowed_root=tmp_path)
    assert opened
    for descriptor in opened:
        with pytest.raises(OSError) as refused:
            os.fstat(descriptor)
        assert refused.value.errno == errno.EBADF


@pytest.mark.parametrize("ancestor", [False, True])
def test_symlink_and_nonregular_refusal(tmp_path: Path, ancestor: bool) -> None:
    folder = tmp_path / "real"
    folder.mkdir()
    (folder / "source.bin").write_bytes(b"exact")
    link = tmp_path / "link"
    link.symlink_to(folder if ancestor else folder / "source.bin", target_is_directory=ancestor)
    with pytest.raises(ImmutableArtifactConflictError):
        read_stable_artifact(
            link / "source.bin" if ancestor else link, max_bytes=5, allowed_root=tmp_path
        )
    with pytest.raises(ImmutableArtifactConflictError, match="regular file"):
        read_stable_artifact(folder, max_bytes=5, allowed_root=tmp_path)


def test_growth_is_bounded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "source.bin"
    source.write_bytes(b"exact")
    original_read = os.read
    sizes: list[int] = []

    def grow(descriptor: int, count: int) -> bytes:
        sizes.append(count)
        source.write_bytes(b"exact-and-extra")
        return original_read(descriptor, count)

    monkeypatch.setattr(os, "read", grow)
    with pytest.raises(ImmutableArtifactConflictError, match="byte limit"):
        read_stable_artifact(source, max_bytes=5, allowed_root=tmp_path)
    assert sizes == [5]


def test_full_final_path_stat_is_checked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "source.bin"
    source.write_bytes(b"exact")
    original_fstat = os.fstat
    regular_checks = 0

    def change_after_handle_check(descriptor: int) -> os.stat_result:
        nonlocal regular_checks
        result = original_fstat(descriptor)
        if stat.S_ISREG(result.st_mode):
            regular_checks += 1
            if regular_checks == 2:
                os.utime(source, ns=(result.st_atime_ns, result.st_mtime_ns + 1_000_000_000))
        return result

    monkeypatch.setattr(os, "fstat", change_after_handle_check)
    with pytest.raises(ImmutableArtifactConflictError, match="path changed"):
        read_stable_artifact(source, max_bytes=5, allowed_root=tmp_path)


def test_parent_replacement_refuses_pinned_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = tmp_path / "folder"
    folder.mkdir()
    source = folder / "source.bin"
    source.write_bytes(b"exact")
    original_read = os.read
    replaced = False

    def swap_parent(descriptor: int, count: int) -> bytes:
        nonlocal replaced
        if not replaced:
            replaced = True
            folder.rename(tmp_path / "moved")
            folder.mkdir()
            source.write_bytes(b"exact")
        return original_read(descriptor, count)

    monkeypatch.setattr(os, "read", swap_parent)
    with pytest.raises(ImmutableArtifactConflictError, match=r"path changed|directory changed"):
        read_stable_artifact(source, max_bytes=5, allowed_root=tmp_path)


class FakeWinFunction:
    def __init__(self, result: int) -> None:
        self.result = result
        self.argtypes: list[object] = []
        self.restype: object = None
        self.calls: list[tuple[object, ...]] = []

    def __call__(self, *args: object) -> int:
        self.calls.append(args)
        return self.result


class FakeWinLibrary:
    def __init__(self, close_result: int) -> None:
        self.CreateFileW = FakeWinFunction(123)
        self.GetFileInformationByHandleEx = FakeWinFunction(1)
        self.CloseHandle = FakeWinFunction(close_result)


class FailedDescriptorConversion:
    def open_osfhandle(self, handle: int, flags: int) -> int:
        raise RuntimeError("synthetic descriptor allocation failed")

    def get_osfhandle(self, descriptor: int) -> int:
        return descriptor


@pytest.mark.parametrize("close_result", [0, 1])
def test_win_handle_constructor_failure_closes_or_reports_cleanup_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, close_result: int
) -> None:
    """Pure Win32 boundary doubles; no Windows/native path qualification."""
    library = FakeWinLibrary(close_result)
    monkeypatch.setattr(
        evidence_snapshot,
        "_windows_runtime",
        lambda: (library, FailedDescriptorConversion(), lambda: 0),
    )
    with (
        pytest.raises((RuntimeError, OSError), match=r"allocation failed|close refused"),
        evidence_snapshot.open_windows_evidence_handle(tmp_path / "source.bin"),
    ):
        pytest.fail("a failed descriptor conversion cannot yield a handle")
    assert library.CloseHandle.calls == [(123,)]
    assert library.CloseHandle.argtypes == [ctypes.c_void_p]


class _MetadataOs:
    """Simulate only the documented ctime difference; use real POSIX file reads."""

    def __init__(self, name: str, *, change_during_read: bool = False) -> None:
        self.name = name
        self.change_during_read = change_during_read
        self.handle_reads = 0

    def __getattr__(self, name: str) -> object:
        return getattr(os, name)

    def fstat(self, descriptor: int) -> os.stat_result:
        value = os.fstat(descriptor)
        self.handle_reads += 1
        offset = 3600_000_000_000
        if self.change_during_read and self.handle_reads > 1:
            offset *= 2
        fields = {
            "st_atime": value.st_atime,
            "st_mtime": value.st_mtime,
            "st_ctime": value.st_ctime + offset / 1_000_000_000,
            "st_atime_ns": value.st_atime_ns,
            "st_mtime_ns": value.st_mtime_ns,
            "st_ctime_ns": value.st_ctime_ns + offset,
        }
        return os.stat_result(tuple(value), fields)


@pytest.mark.skipif(os.name == "nt", reason="Portable metadata simulation uses POSIX opens")
@pytest.mark.parametrize(
    ("platform", "change_during_read", "expected_refusal"),
    [
        ("nt", False, None),
        ("nt", True, "changed while it was read"),
        ("posix", False, "changed before its handle was pinned"),
    ],
)
def test_path_and_handle_clocks_keep_same_route_change_checks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    platform: str,
    change_during_read: bool,
    expected_refusal: str | None,
) -> None:
    source = tmp_path / "edited.bin"
    source.write_bytes(b"exact")
    monkeypatch.setattr(
        immutable_artifact, "os", _MetadataOs(platform, change_during_read=change_during_read)
    )
    if expected_refusal is not None:
        with pytest.raises(ImmutableArtifactConflictError, match=expected_refusal):
            read_stable_artifact(source, max_bytes=5)
    else:
        snapshot, payload = read_stable_artifact(source, max_bytes=5)
        assert payload == b"exact"
        immutable_artifact.assert_artifact_unchanged(snapshot)


@pytest.mark.skipif(os.name != "nt", reason="Requires actual Windows stat and file handle")
def test_edited_windows_file_remains_readable(
    tmp_path: Path, record_property: Callable[[str, object], None]
) -> None:
    source = tmp_path / "edited.bin"
    source.write_bytes(b"created")
    original_bytes = source.read_bytes()
    original_stat = source.lstat()
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        time.sleep(0.01)
        if time.monotonic() >= deadline:
            pytest.fail(
                "Fixture did not produce an actual modification-time change within one second"
            )
        source.write_bytes(b"exact")
        if source.lstat().st_mtime_ns != original_stat.st_mtime_ns and time.monotonic() < deadline:
            break
    else:
        pytest.fail("Fixture did not produce an actual modification-time change within one second")
    assert source.lstat().st_mtime_ns != original_stat.st_mtime_ns
    assert time.monotonic() < deadline, "Fixture edit exceeded its original one-second deadline"
    assert original_bytes == b"created" and source.read_bytes() == b"exact"
    with evidence_snapshot.open_windows_evidence_handle(source) as descriptor:
        path_stat, handle_stat = source.lstat(), os.fstat(descriptor)
    fields = (
        "st_dev",
        "st_ino",
        "st_mode",
        "st_size",
        "st_mtime_ns",
        "st_ctime_ns",
        "st_birthtime_ns",
        "st_nlink",
    )
    record_property(
        "edited_windows_file_metadata",
        json.dumps(
            {
                name: {field: getattr(value, field, None) for field in fields}
                for name, value in (
                    ("before_edit", original_stat),
                    ("path_after_edit", path_stat),
                    ("handle_after_edit", handle_stat),
                )
            },
            sort_keys=True,
        ),
    )
    snapshot, payload = read_stable_artifact(source, max_bytes=5, allowed_root=tmp_path)
    assert payload == b"exact" and snapshot.size_bytes == 5
    immutable_artifact.assert_artifact_unchanged(snapshot)


@pytest.mark.skipif(os.name != "nt", reason="Requires actual Windows sharing protection")
def test_windows_pinned_reader_denies_write_and_parent_rename(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    record_property: Callable[[str, object], None],
) -> None:
    deadline = time.monotonic() + 5.0

    def check_deadline() -> None:
        assert time.monotonic() < deadline, (
            "Windows protection fixture exceeded its original deadline"
        )

    check_deadline()
    folder = tmp_path / "folder"
    folder.mkdir()
    source = folder / "source.bin"
    check_deadline()
    source.write_bytes(b"exact")
    moved = tmp_path / "moved"
    observations: list[dict[str, object]] = []

    class PinnedReaderOs:
        """Forward real OS calls; observe only the reader's actual pinned read."""

        def __getattr__(self, name: str) -> object:
            return getattr(os, name)

        def read(self, descriptor: int, count: int) -> bytes:
            check_deadline()
            assert not observations and count == 5
            assert evidence_snapshot.windows_evidence_handle_path(descriptor) == source
            assert os.fstat(descriptor).st_size == 5
            check_deadline()
            with pytest.raises(PermissionError) as write_denied:
                source.write_bytes(b"changed")
            assert write_denied.value.errno == errno.EACCES
            check_deadline()
            with pytest.raises(PermissionError) as delete_denied:
                source.unlink()
            assert delete_denied.value.errno == errno.EACCES
            assert getattr(delete_denied.value, "winerror", None) in {5, 32}
            check_deadline()
            with pytest.raises(PermissionError) as rename_denied:
                folder.rename(moved)
            assert rename_denied.value.errno == errno.EACCES
            assert getattr(rename_denied.value, "winerror", None) == 5
            observations.append(
                {
                    "write_errno": write_denied.value.errno,
                    "write_winerror": getattr(write_denied.value, "winerror", None),
                    "delete_errno": delete_denied.value.errno,
                    "delete_winerror": getattr(delete_denied.value, "winerror", None),
                    "rename_errno": rename_denied.value.errno,
                    "rename_winerror": getattr(rename_denied.value, "winerror", None),
                    "actual_pinned_path": str(source),
                }
            )
            check_deadline()
            return os.read(descriptor, count)

    check_deadline()
    with monkeypatch.context() as patch:
        patch.setattr(immutable_artifact, "os", PinnedReaderOs())
        snapshot, payload = read_stable_artifact(source, max_bytes=5, allowed_root=tmp_path)
    check_deadline()
    assert payload == b"exact" and snapshot.size_bytes == 5 and len(observations) == 1
    immutable_artifact.assert_artifact_unchanged(snapshot)
    check_deadline()
    source.write_bytes(b"after-close")
    check_deadline()
    folder.rename(moved)
    check_deadline()
    assert (moved / source.name).read_bytes() == b"after-close"
    assert not folder.exists()
    check_deadline()
    (moved / source.name).unlink()
    check_deadline()
    assert not (moved / source.name).exists()
    record_property("windows_pinned_reader_protection", json.dumps(observations, sort_keys=True))
    check_deadline()


@pytest.mark.skipif(os.name != "nt", reason="Requires an actual Windows directory junction")
def test_windows_bounded_reader_rejects_source_parent_junction(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    (target / "source.bin").write_bytes(b"exact")
    link = tmp_path / "source-parent"
    subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        check=True,
        capture_output=True,
        timeout=5,
    )
    try:
        junction_check = getattr(link, "is_junction", None)
        assert callable(junction_check) and junction_check()
        with pytest.raises(ImmutableArtifactConflictError, match="reparse"):
            read_stable_artifact(link / "source.bin", max_bytes=5, allowed_root=tmp_path)
    finally:
        link.rmdir()
    assert not link.exists() and (target / "source.bin").read_bytes() == b"exact"
