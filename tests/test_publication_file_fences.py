"""Retained native evidence-fence tests.

The Windows tests spawn a *separate* process that holds the fence and then probe
the real filesystem from this process. They carry a genuine skip on non-Windows
hosts with an explicit reason; a skip is not native proof. The portable tests
exercise platform-independent operand validation and the resource-cleanup
boundary using fault injection only — never as a substitute for a native fence.
"""

from __future__ import annotations

import ctypes
import errno
import hashlib
import importlib
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import Any, cast

import pytest

from compute import evidence_snapshot
from compute.evidence_snapshot import (
    EvidenceBudgetExceededError,
    EvidenceFenceUnavailableError,
    UnsafeEvidencePathError,
)

_REQUIRES_WINDOWS = pytest.mark.skipif(
    os.name != "nt",
    reason="Requires actual Windows retained-handle sharing semantics",
)

_CHILD_HOLD = r"""
import sys
from pathlib import Path
from compute import evidence_snapshot

path = Path(sys.argv[1])
root = Path(sys.argv[2])
budget = int(sys.argv[3])
with evidence_snapshot.hold_windows_evidence_snapshot(path, root, max_bytes=budget) as snap:
    sys.stdout.write("READY " + snap.sha256 + "\n")
    sys.stdout.flush()
    sys.stdin.readline()
    sys.stdout.write("DONE " + snap.sha256 + "\n")
    sys.stdout.flush()
"""


def _child_env() -> dict[str, str]:
    return dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"))


def _kernel32() -> Any:
    return cast(Any, ctypes).WinDLL("kernel32", use_last_error=True)


def _create_file_fd(path: Path, access: int, share: int) -> int:
    msvcrt = importlib.import_module("msvcrt")
    create_file = _kernel32().CreateFileW
    create_file.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
    ]
    create_file.restype = ctypes.c_void_p
    handle = create_file(str(path), access, share, None, 3, 0x80, None)
    invalid = ctypes.c_void_p(-1).value
    assert handle not in (None, invalid), cast(Any, ctypes).get_last_error()
    return int(msvcrt.open_osfhandle(int(handle), os.O_RDWR))


def _open_denying_read(path: Path) -> int:
    """Generic-write handle whose share mode refuses other readers."""
    return _create_file_fd(path, 0x40000000, 0x2)


def _open_writer_allowing_read(path: Path) -> int:
    """Generic read/write handle sharing read/write/delete, so only writes conflict."""
    return _create_file_fd(path, 0xC0000000, 0x7)


def _map_writable(path: Path) -> int:
    """Open and close a writable mapping, returning the surviving section handle."""
    kernel32 = _kernel32()
    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
    ]
    create_file.restype = ctypes.c_void_p
    handle = create_file(str(path), 0xC0000000, 0x7, None, 3, 0x80, None)
    invalid = ctypes.c_void_p(-1).value
    assert handle not in (None, invalid), cast(Any, ctypes).get_last_error()
    create_mapping = kernel32.CreateFileMappingW
    create_mapping.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_wchar_p,
    ]
    create_mapping.restype = ctypes.c_void_p
    mapping = create_mapping(ctypes.c_void_p(int(handle)), None, 0x04, 0, 0, None)
    kernel32.CloseHandle(ctypes.c_void_p(int(handle)))
    assert mapping not in (None, invalid), cast(Any, ctypes).get_last_error()
    return int(mapping)


def _close_handle(handle: int) -> None:
    _kernel32().CloseHandle(ctypes.c_void_p(handle))


def _readline_with_timeout(proc: subprocess.Popen[str], timeout: float) -> str:
    assert proc.stdout is not None
    box: list[str] = []

    def read_line() -> None:
        stdout = proc.stdout
        box.append(stdout.readline() if stdout is not None else "")

    thread = threading.Thread(target=read_line, daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive() or not box or not box[0]:
        proc.kill()
        with suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=10)
        assert proc.stderr is not None
        pytest.fail(f"fence child did not respond within {timeout}s: {proc.stderr.read()}")
    return box[0]


def _spawn_fence(
    path: Path, root: Path, *, max_bytes: int = evidence_snapshot.DEFAULT_EVIDENCE_MAX_BYTES
) -> tuple[subprocess.Popen[str], str]:
    proc = subprocess.Popen(
        [sys.executable, "-c", _CHILD_HOLD, str(path), str(root), str(max_bytes)],
        env=_child_env(),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    line = _readline_with_timeout(proc, 10.0)
    if not line.startswith("READY "):
        pytest.fail(f"fence child never became ready: {line!r}")
    return proc, line.strip().split(" ", 1)[1]


def _release(proc: subprocess.Popen[str]) -> None:
    assert proc.stdin is not None
    proc.stdin.write("\n")
    proc.stdin.flush()
    assert _readline_with_timeout(proc, 10.0).startswith("DONE ")
    proc.stdin.close()
    assert proc.wait(timeout=10) == 0


def _make_junction(link: Path, target: Path) -> None:
    subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        check=True,
        capture_output=True,
        timeout=10,
    )


class _RecordingOs:
    """Forward real OS calls while recording descriptor closes."""

    def __init__(self) -> None:
        self.closed: list[int] = []

    def __getattr__(self, name: str) -> object:
        return getattr(os, name)

    def close(self, descriptor: int) -> None:
        self.closed.append(descriptor)
        os.close(descriptor)


class _FailingCloseOs(_RecordingOs):
    def close(self, descriptor: int) -> None:
        self.closed.append(descriptor)
        raise OSError(errno.EIO, "close failed")


def test_fence_requires_win32_runtime(tmp_path: Path) -> None:
    if os.name == "nt":
        pytest.skip("the Win32 runtime provides the fence on this host")
    source = tmp_path / "source.bin"
    source.write_bytes(b"exact")
    with (
        pytest.raises(EvidenceFenceUnavailableError, match="Win32"),
        evidence_snapshot.hold_windows_evidence_snapshot(source, tmp_path),
    ):
        pytest.fail("a POSIX host must not pretend to hold the Windows fence")


def test_fence_rejects_relative_operands(tmp_path: Path) -> None:
    with (
        pytest.raises(UnsafeEvidencePathError, match="absolute"),
        evidence_snapshot.hold_windows_evidence_snapshot(Path("source.bin"), tmp_path),
    ):
        pytest.fail("a relative evidence path must be refused")


def test_fence_rejects_operands_outside_allowed_root(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"exact")
    with (
        pytest.raises(UnsafeEvidencePathError, match="under the approved allowed root"),
        evidence_snapshot.hold_windows_evidence_snapshot(outside, root),
    ):
        pytest.fail("an escape from the allowed root must be refused")


def test_fence_rejects_dotdot_in_either_operand(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "source.bin").write_bytes(b"exact")
    traversal_path = root / ".." / "root" / "source.bin"
    traversal_root = tmp_path / "elsewhere" / ".." / "root"
    with (
        pytest.raises(UnsafeEvidencePathError, match="contain"),
        evidence_snapshot.hold_windows_evidence_snapshot(traversal_path, root),
    ):
        pytest.fail("a '..' in the evidence path must be refused")
    with (
        pytest.raises(UnsafeEvidencePathError, match="contain"),
        evidence_snapshot.hold_windows_evidence_snapshot(root / "source.bin", traversal_root),
    ):
        pytest.fail("a '..' in the allowed root must be refused")


def test_fence_rejects_root_itself_as_file(tmp_path: Path) -> None:
    with (
        pytest.raises(UnsafeEvidencePathError, match="plain file"),
        evidence_snapshot.hold_windows_evidence_snapshot(tmp_path, tmp_path),
    ):
        pytest.fail("the allowed root is a directory, not a fenceable file")


def test_fence_rejects_nonpositive_budget(tmp_path: Path) -> None:
    source = tmp_path / "source.bin"
    source.write_bytes(b"exact")
    with (
        pytest.raises(ValueError, match="positive"),
        evidence_snapshot.hold_windows_evidence_snapshot(source, tmp_path, max_bytes=0),
    ):
        pytest.fail("a nonpositive budget must be refused")


def test_capture_closes_partial_acquisition_on_open_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    proxy = _RecordingOs()
    monkeypatch.setattr(evidence_snapshot, "os", proxy)
    with pytest.raises(FileNotFoundError):
        evidence_snapshot.capture_snapshot(root / "missing.bin", root)
    assert len(proxy.closed) == 1


def test_capture_closes_every_descriptor_when_close_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    source = root / "source.bin"
    source.write_bytes(b"exact")
    proxy = _FailingCloseOs()
    monkeypatch.setattr(evidence_snapshot, "os", proxy)
    try:
        with pytest.raises(OSError, match="close failed"):
            evidence_snapshot.capture_snapshot(source, root)
        assert len(proxy.closed) == 2
    finally:
        for descriptor in proxy.closed:
            os.close(descriptor)


@_REQUIRES_WINDOWS
def test_windows_budget_overflow_fails_closed(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    source = root / "source.bin"
    source.write_bytes(b"x" * 64)
    with (
        pytest.raises(EvidenceBudgetExceededError, match="budget"),
        evidence_snapshot.hold_windows_evidence_snapshot(source, root, max_bytes=8),
    ):
        pytest.fail("an oversized payload must fail closed")


@_REQUIRES_WINDOWS
def test_windows_fence_denies_write_replace_delete_and_rename(tmp_path: Path) -> None:
    base = tmp_path / "base"
    root = base / "root"
    folder = root / "folder"
    folder.mkdir(parents=True)
    source = folder / "source.bin"
    source.write_bytes(b"exact")
    replacement = tmp_path / "replacement.bin"
    replacement.write_bytes(b"replacement")
    moved_root = tmp_path / "moved-root"
    moved_folder = root / "moved-folder"
    moved_base = tmp_path / "moved-base"

    proc, sha = _spawn_fence(source, root)
    try:
        assert sha == hashlib.sha256(b"exact").hexdigest()

        with pytest.raises(PermissionError) as write_denied:
            source.write_bytes(b"changed")
        assert getattr(write_denied.value, "winerror", None) in {
            5,
            32,
        } or write_denied.value.errno in {errno.EACCES, errno.EPERM}

        with pytest.raises(PermissionError):
            os.replace(replacement, source)

        with pytest.raises(PermissionError):
            source.unlink()

        with pytest.raises(PermissionError):
            folder.rename(moved_folder)

        with pytest.raises(PermissionError):
            root.rename(moved_root)

        with pytest.raises(PermissionError):
            base.rename(moved_base)

        assert source.read_bytes() == b"exact"
    finally:
        _release(proc)

    base.rename(moved_base)
    released = moved_base / "root" / "folder" / "source.bin"
    released.write_bytes(b"released")
    assert released.read_bytes() == b"released"
    released.unlink()
    assert not released.exists()


@_REQUIRES_WINDOWS
def test_windows_writer_denying_read_prevents_acquisition(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    source = root / "source.bin"
    source.write_bytes(b"exact")

    exclusive = _open_denying_read(source)
    try:
        with (
            pytest.raises(evidence_snapshot.EvidenceSourceChangedError),
            evidence_snapshot.hold_windows_evidence_snapshot(source, root),
        ):
            pytest.fail("a reader-denying writer must block fence acquisition")
    finally:
        os.close(exclusive)


@_REQUIRES_WINDOWS
def test_windows_read_compatible_writer_prevents_acquisition(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    source = root / "source.bin"
    source.write_bytes(b"exact")

    writer = _open_writer_allowing_read(source)
    try:
        with (
            pytest.raises(evidence_snapshot.EvidenceSourceChangedError),
            evidence_snapshot.hold_windows_evidence_snapshot(source, root),
        ):
            pytest.fail("a writer sharing reads must still block fence acquisition")
    finally:
        os.close(writer)


@_REQUIRES_WINDOWS
def test_windows_writable_mapping_contention(
    tmp_path: Path, record_property: Callable[[str, object], None]
) -> None:
    """A writable mapping must not be silently treated as excluded.

    If a real Windows host lets this fence acquire while a writable mapping is
    live, the read-share fence cannot claim to exclude a memory-mapped writer and
    this test fails closed rather than recording a false exclusion.
    """
    root = tmp_path / "root"
    root.mkdir()
    source = root / "source.bin"
    source.write_bytes(b"exact")

    mapping = _map_writable(source)
    blocked = False
    try:
        try:
            with evidence_snapshot.hold_windows_evidence_snapshot(source, root):
                pass
        except evidence_snapshot.EvidenceSourceChangedError:
            blocked = True
    finally:
        _close_handle(mapping)
    record_property("writable_mapping_blocks_acquisition", blocked)
    if not blocked:
        pytest.fail(
            "a live writable mapping did not block acquisition; this primitive "
            "cannot claim exclusion of a memory-mapped writer without an owner fence"
        )


@_REQUIRES_WINDOWS
def test_windows_pre_acquisition_replacement_is_reflected(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    source = root / "source.bin"
    source.write_bytes(b"old")
    candidate = evidence_snapshot.capture_snapshot(source, root)

    source.write_bytes(b"new")
    with evidence_snapshot.hold_windows_evidence_snapshot(source, root) as current:
        assert current.payload == b"new"
        assert current.sha256 != candidate.sha256


@_REQUIRES_WINDOWS
def test_windows_fence_rejects_junction_in_ancestry(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    (target / "source.bin").write_bytes(b"exact")
    link = tmp_path / "source-parent"
    _make_junction(link, target)
    try:
        with (
            pytest.raises(UnsafeEvidencePathError, match="reparse"),
            evidence_snapshot.hold_windows_evidence_snapshot(link / "source.bin", tmp_path),
        ):
            pytest.fail("a junction in the ancestry must be refused")
    finally:
        link.rmdir()


@_REQUIRES_WINDOWS
def test_windows_fence_rejects_junction_above_allowed_root(tmp_path: Path) -> None:
    target = tmp_path / "target"
    (target / "root").mkdir(parents=True)
    (target / "root" / "source.bin").write_bytes(b"exact")
    link = tmp_path / "linked-parent"
    _make_junction(link, target)
    try:
        with (
            pytest.raises(UnsafeEvidencePathError, match="reparse"),
            evidence_snapshot.hold_windows_evidence_snapshot(
                link / "root" / "source.bin", link / "root"
            ),
        ):
            pytest.fail("a junction above the allowed root must be refused")
    finally:
        link.rmdir()


@_REQUIRES_WINDOWS
def test_windows_fence_rejects_redirected_allowed_root(tmp_path: Path) -> None:
    real_root = tmp_path / "real-root"
    real_root.mkdir()
    (real_root / "source.bin").write_bytes(b"exact")
    alias_root = tmp_path / "alias-root"
    _make_junction(alias_root, real_root)
    try:
        with (
            pytest.raises(UnsafeEvidencePathError, match="reparse"),
            evidence_snapshot.hold_windows_evidence_snapshot(alias_root / "source.bin", alias_root),
        ):
            pytest.fail("a redirected allowed root must not become the authority")
    finally:
        alias_root.rmdir()


@_REQUIRES_WINDOWS
def test_windows_fence_releases_descriptors_on_error(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    source = root / "source.bin"
    source.write_bytes(b"exact")
    with (
        pytest.raises(RuntimeError, match="boom"),
        evidence_snapshot.hold_windows_evidence_snapshot(source, root) as snap,
    ):
        assert snap.payload == b"exact"
        raise RuntimeError("boom")
    source.write_bytes(b"after-error")
    assert source.read_bytes() == b"after-error"


@_REQUIRES_WINDOWS
def test_windows_fence_releases_descriptors_on_process_termination(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    source = root / "source.bin"
    source.write_bytes(b"exact")
    proc, _sha = _spawn_fence(source, root)
    proc.kill()
    proc.wait(timeout=10)

    deadline = time.monotonic() + 5.0
    last_error: OSError | None = None
    while time.monotonic() < deadline:
        try:
            source.write_bytes(b"after-kill")
            last_error = None
            break
        except OSError as exc:
            if getattr(exc, "winerror", None) not in {5, 32} and exc.errno not in {
                errno.EACCES,
                errno.EPERM,
            }:
                raise
            last_error = exc
            time.sleep(0.05)
    assert last_error is None, f"handle outlived its process: {last_error}"
    assert source.read_bytes() == b"after-kill"
    source.unlink()
    assert not source.exists()
