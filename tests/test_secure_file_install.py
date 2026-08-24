# pyright: reportPrivateUsage=false
"""Handle-pinned staging installer regressions independent of network fetches."""

from __future__ import annotations

import ctypes
import hashlib
import os
import stat
from collections.abc import Callable
from pathlib import Path
from threading import Event, Thread
from types import SimpleNamespace
from typing import Any, cast

import pytest

import provenance.secure_file_install as install


class _FakeWindowsFunction:
    argtypes: object = None
    restype: object = None

    def __init__(self, callback: Callable[..., int | None]) -> None:
        self._callback = callback

    def __call__(self, *args: object) -> int | None:
        return self._callback(*args)


class _TempCreationFFI:
    """Minimal NT wrapper that executes the real temporary-adoption code off-host."""

    def __init__(
        self,
        root: Path,
        digest: str,
        adopt: Callable[[Path], int],
    ) -> None:
        self.residue = root / f".{digest}.{'a' * 32}.tmp"
        self._adopt = adopt
        self.closed_raw_handles: list[int] = []
        self.NtCreateFile = _FakeWindowsFunction(self._create)
        self.CloseHandle = _FakeWindowsFunction(self._close)

    def _create(self, raw: object, *_args: object) -> int:
        self.residue.write_bytes(b"")
        ctypes.cast(cast("Any", raw), ctypes.POINTER(ctypes.c_void_p))[0] = ctypes.c_void_p(9876)
        return 0

    def _close(self, raw: object) -> int:
        value = getattr(raw, "value", raw)
        self.closed_raw_handles.append(int(cast("Any", value)))
        return 1

    def load_library(self, _name: str, *, use_last_error: bool = False) -> object:
        del use_last_error
        return SimpleNamespace(NtCreateFile=self.NtCreateFile, CloseHandle=self.CloseHandle)

    @staticmethod
    def get_last_error() -> int:
        return 5

    def open_osfhandle(self, _handle: int, _flags: int) -> int:
        return self._adopt(self.residue)

    @staticmethod
    def get_osfhandle(descriptor: int) -> int:
        return descriptor


def _fixed_token_hex(_byte_count: int) -> str:
    return "a" * 32


def _return_root_descriptor(descriptor: int) -> Callable[[Path], int]:
    def open_root(_root: Path) -> int:
        return descriptor

    return open_root


def test_installer_exact_replay_and_conflict(tmp_path: Path) -> None:
    root = tmp_path / "attempt" / "objects"
    payload = b"issuer bytes"
    first = install.install_bytes_no_clobber(
        root, "source.pdf", payload, expected_sha256=hashlib.sha256(payload).hexdigest()
    )
    assert first.created and first.path.read_bytes() == payload
    assert not install.install_bytes_no_clobber(root, "source.pdf", payload).created
    with pytest.raises(install.SecureFileInstallError, match="existing_target_conflict"):
        install.install_bytes_no_clobber(root, "source.pdf", b"different")


def test_installer_rejects_symlinked_root_without_following_it(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "attempt"
    root.symlink_to(outside, target_is_directory=True)
    with pytest.raises(install.SecureFileInstallError, match="staging_root_unsafe"):
        install.install_bytes_no_clobber(root, "source.pdf", b"issuer bytes")
    assert not (outside / "source.pdf").exists()


def test_windows_branch_uses_handle_relative_seam_not_path_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "attempt"
    called: list[tuple[Path, str, bytes, str]] = []

    def handle_install(
        path: Path, name: str, payload: bytes, digest: str
    ) -> install.SecureFileInstallResult:
        called.append((path, name, payload, digest))
        target = path / name
        target.write_bytes(payload)
        return install.SecureFileInstallResult(target, created=True)

    def path_open_forbidden(*_args: object, **_kwargs: object) -> int:
        raise AssertionError("Windows branch must not path-open the target")

    monkeypatch.setattr(install, "_install_windows_handle_relative", handle_install)
    monkeypatch.setattr(install, "_is_windows", lambda: True)
    monkeypatch.setattr(install.os, "open", path_open_forbidden)
    result = install.install_bytes_no_clobber(root, "source.pdf", b"issuer bytes")
    assert result.created and called and result.path.read_bytes() == b"issuer bytes"


def test_windows_handle_seam_reports_exact_replay_as_not_created(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "attempt"
    target = root / "source.pdf"
    root.mkdir()
    target.write_bytes(b"issuer bytes")

    def replay(
        path: Path, name: str, payload: bytes, digest: str
    ) -> install.SecureFileInstallResult:
        del digest
        assert path / name == target and payload == b"issuer bytes"
        return install.SecureFileInstallResult(target, created=False)

    monkeypatch.setattr(install, "_install_windows_handle_relative", replay)
    monkeypatch.setattr(install, "_is_windows", lambda: True)
    assert not install.install_bytes_no_clobber(root, "source.pdf", b"issuer bytes").created


def test_failed_verification_never_deletes_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "attempt" / "objects"
    original = install.read_stable_artifact
    calls = 0

    def replace_before_verify(path: Path) -> tuple[object, bytes]:
        nonlocal calls
        calls += 1
        if calls == 1:
            path.unlink()
            path.write_bytes(b"replacement")
        return original(path)

    monkeypatch.setattr(install, "read_stable_artifact", replace_before_verify)
    with pytest.raises(install.SecureFileInstallError, match="installed_target_conflict"):
        install.install_bytes_no_clobber(root, "source.pdf", b"issuer bytes")
    assert (root / "source.pdf").read_bytes() == b"replacement"


def test_installer_completes_short_writes_before_publishing_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "attempt"
    target = root / "source.pdf"
    original_write = install.os.write
    entered = Event()
    release = Event()

    def short_blocked_write(descriptor: int, data: bytes | memoryview) -> int:
        entered.set()
        assert release.wait(timeout=2)
        return original_write(descriptor, data[:1])

    monkeypatch.setattr(install.os, "write", short_blocked_write)
    outcome: list[install.SecureFileInstallResult] = []

    def install_in_thread() -> None:
        outcome.append(install.install_bytes_no_clobber(root, "source.pdf", b"complete bytes"))

    thread = Thread(target=install_in_thread)
    thread.start()
    assert entered.wait(timeout=2)
    assert not target.exists()
    release.set()
    thread.join(timeout=2)
    assert not thread.is_alive()
    assert outcome[0].created and target.read_bytes() == b"complete bytes"


def test_created_token_is_replacement_safe_and_reused_has_no_token(tmp_path: Path) -> None:
    root = tmp_path / "attempt"
    first = install.install_bytes_no_clobber(root, "source.pdf", b"issuer bytes")
    assert first.created and first.ownership is not None
    first.path.unlink()
    first.path.write_bytes(b"replacement")
    cleanup = install.cleanup_owned_file(first.ownership)
    assert cleanup.remaining and not cleanup.removed
    assert first.path.read_bytes() == b"replacement"
    replay = install.install_bytes_no_clobber(root, "source.pdf", b"replacement")
    assert not replay.created and replay.ownership is None


def test_atomic_rename_collision_replays_without_replacing_existing_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "attempt"
    root.mkdir()
    target = root / "source.pdf"

    def collide(*_args: object) -> None:
        target.write_bytes(b"issuer bytes")
        raise FileExistsError

    monkeypatch.setattr(install, "_rename_no_replace", collide)
    result = install.install_bytes_no_clobber(root, target.name, b"issuer bytes")
    assert not result.created
    assert result.path == target and target.read_bytes() == b"issuer bytes"
    assert len(result.residue_paths) == 1 and result.residue_paths[0].exists()


def test_precommit_root_failure_reports_retained_temporary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "attempt"
    root.mkdir()
    original = install._assert_posix_root_stable
    calls = 0

    def fail_after_temp(path: Path, descriptor: int, expected: tuple[int, int]) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise install.SecureFileInstallError("root_identity_changed")
        original(path, descriptor, expected)

    monkeypatch.setattr(install, "_assert_posix_root_stable", fail_after_temp)
    with pytest.raises(install.SecureFileInstallError, match="root_identity_changed") as raised:
        install.install_bytes_no_clobber(root, "source.pdf", b"issuer bytes")
    assert len(raised.value.residue_paths) == 1
    assert raised.value.residue_paths[0].exists()


def test_ownership_token_cannot_be_minted_from_a_caller_created_flag(tmp_path: Path) -> None:
    with pytest.raises(TypeError, match="_issuer"):
        getattr(install, "SecureFileOwnershipToken")(tmp_path / "source.pdf", 1, 2)


def test_failed_final_verification_retains_transaction_issued_ownership(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "attempt"

    def reject(*_args: object, **_kwargs: object) -> None:
        raise install.SecureFileInstallError("installed_target_unsafe")

    monkeypatch.setattr(install, "_verify_no_clobber_install", reject)
    with pytest.raises(install.SecureFileInstallError, match="installed_target_unsafe") as raised:
        install.install_bytes_no_clobber(root, "source.pdf", b"issuer bytes")
    assert raised.value.ownership is not None
    assert (root / "source.pdf").read_bytes() == b"issuer bytes"
    cleanup = install.cleanup_owned_file(raised.value.ownership)
    assert cleanup.remaining and not cleanup.removed


def test_posix_root_replacement_is_rejected_before_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "attempt"
    replacement = tmp_path / "replacement"
    moved = tmp_path / "moved"
    root.mkdir()
    replacement.mkdir()
    original_write_all = install._write_all

    def replace_root(descriptor: int, payload: bytes) -> None:
        original_write_all(descriptor, payload)
        root.rename(moved)
        replacement.rename(root)

    monkeypatch.setattr(install, "_write_all", replace_root)
    with pytest.raises(install.SecureFileInstallError, match="root_identity_changed"):
        install.install_bytes_no_clobber(root, "source.pdf", b"issuer bytes")
    assert not (root / "source.pdf").exists()
    assert not (moved / "source.pdf").exists()


def test_posix_temp_replacement_survives_identity_bound_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "attempt"
    root.mkdir()
    victim = tmp_path / "victim.txt"
    victim.write_bytes(b"replacement")
    original_write_all = install._write_all

    def replace_temp(descriptor: int, payload: bytes) -> None:
        original_write_all(descriptor, payload)
        temporary = next(root.glob(".source.pdf.*.tmp"))
        temporary.unlink()
        victim.rename(temporary)

    monkeypatch.setattr(install, "_write_all", replace_temp)
    with pytest.raises(install.SecureFileInstallError, match="created_temporary_changed"):
        install.install_bytes_no_clobber(root, "source.pdf", b"issuer bytes")
    replacement = next(root.glob(".source.pdf.*.tmp"))
    assert replacement.read_bytes() == b"replacement"
    assert not (root / "source.pdf").exists()


def test_windows_collision_closes_the_owned_descriptor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "attempt"
    root.mkdir()
    target = root / "source.pdf"
    target.write_bytes(b"issuer bytes")
    root_fd = os.open(root, os.O_RDONLY)
    temporary = root / ".owned.tmp"
    temporary.write_bytes(b"issuer bytes")
    descriptor = os.open(temporary, os.O_RDWR)
    metadata = os.fstat(descriptor)
    closed: list[int] = []
    original_close = install.os.close

    def record_close(value: int) -> None:
        closed.append(value)
        original_close(value)

    def open_root(_root: Path) -> int:
        return root_fd

    def create_temp(_root_fd: int, _digest: str) -> tuple[int, str, tuple[int, int]]:
        return descriptor, temporary.name, (metadata.st_dev, metadata.st_ino)

    def seal_temp(_descriptor: int) -> None:
        return None

    def collision(_descriptor: int, _root_fd: int, _target_name: str) -> None:
        raise FileExistsError

    def delete_temp(_descriptor: int) -> None:
        return None

    monkeypatch.setattr(install, "_windows_open_root", open_root)
    monkeypatch.setattr(install, "_windows_create_temp", create_temp)
    monkeypatch.setattr(install, "_windows_set_read_only", seal_temp)
    monkeypatch.setattr(install, "_windows_rename_no_replace", collision)
    monkeypatch.setattr(install, "_windows_delete_owned", delete_temp)
    monkeypatch.setattr(install.os, "close", record_close)
    result = install._install_windows_handle_relative(
        root, target.name, b"issuer bytes", hashlib.sha256(b"issuer bytes").hexdigest()
    )
    assert not result.created and descriptor in closed


def test_windows_success_closes_owned_descriptor_before_stable_target_reopen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "attempt"
    root.mkdir()
    target = root / "source.pdf"
    payload = b"issuer bytes"
    root_fd = os.open(root, os.O_RDONLY)
    temporary = root / ".owned.tmp"
    descriptor = os.open(temporary, os.O_RDWR | os.O_CREAT, 0o600)
    metadata = os.fstat(descriptor)
    closed: list[int] = []
    original_close = install.os.close
    original_read_stable = install.read_stable_artifact

    def record_close(value: int) -> None:
        closed.append(value)
        original_close(value)

    def create_temp(_root_fd: int, _digest: str) -> tuple[int, str, tuple[int, int]]:
        return descriptor, temporary.name, (metadata.st_dev, metadata.st_ino)

    def rename(_descriptor: int, _root_fd: int, _target_name: str) -> None:
        temporary.rename(target)

    def seal_temp(_descriptor: int) -> None:
        return None

    def read_target_only_after_close(path: Path) -> tuple[object, bytes]:
        assert descriptor in closed
        return original_read_stable(path)

    monkeypatch.setattr(install, "_windows_open_root", _return_root_descriptor(root_fd))
    monkeypatch.setattr(install, "_windows_create_temp", create_temp)
    monkeypatch.setattr(install, "_windows_set_read_only", seal_temp)
    monkeypatch.setattr(install, "_windows_rename_no_replace", rename)
    monkeypatch.setattr(install, "read_stable_artifact", read_target_only_after_close)
    monkeypatch.setattr(install.os, "close", record_close)

    result = install._install_windows_handle_relative(
        root, target.name, payload, hashlib.sha256(payload).hexdigest()
    )

    assert result.created and result.ownership is not None
    assert target.read_bytes() == payload
    assert descriptor in closed


def test_windows_owned_descriptor_byte_mismatch_prevents_target_reopen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "attempt"
    root.mkdir()
    target = root / "source.pdf"
    payload = b"issuer bytes"
    root_fd = os.open(root, os.O_RDONLY)
    temporary = root / ".owned.tmp"
    descriptor = os.open(temporary, os.O_RDWR | os.O_CREAT, 0o600)
    metadata = os.fstat(descriptor)
    original_read = install.os.read

    def create_temp(_root_fd: int, _digest: str) -> tuple[int, str, tuple[int, int]]:
        return descriptor, temporary.name, (metadata.st_dev, metadata.st_ino)

    def rename(_descriptor: int, _root_fd: int, _target_name: str) -> None:
        temporary.rename(target)

    def seal_temp(_descriptor: int) -> None:
        return None

    def tampered_descriptor_read(value: int, count: int) -> bytes:
        if value == descriptor:
            return b"x" * count
        return original_read(value, count)

    def target_reopen_forbidden(_path: Path) -> tuple[object, bytes]:
        raise AssertionError("target path must not reopen after owned-byte mismatch")

    def delete_owned(_descriptor: int) -> None:
        target.unlink()

    monkeypatch.setattr(install, "_windows_open_root", _return_root_descriptor(root_fd))
    monkeypatch.setattr(install, "_windows_create_temp", create_temp)
    monkeypatch.setattr(install, "_windows_set_read_only", seal_temp)
    monkeypatch.setattr(install, "_windows_rename_no_replace", rename)
    monkeypatch.setattr(install, "_windows_delete_owned", delete_owned)
    monkeypatch.setattr(install.os, "read", tampered_descriptor_read)
    monkeypatch.setattr(install, "read_stable_artifact", target_reopen_forbidden)

    with pytest.raises(install.SecureFileInstallError, match="windows_handle_install_failed"):
        install._install_windows_handle_relative(
            root, target.name, payload, hashlib.sha256(payload).hexdigest()
        )
    assert not target.exists()


def test_windows_failed_owned_delete_reports_named_residue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "attempt"
    root.mkdir()
    target = root / "source.pdf"
    target.write_bytes(b"issuer bytes")
    root_fd = os.open(root, os.O_RDONLY)
    temporary = root / ".owned.tmp"
    temporary.write_bytes(b"issuer bytes")
    descriptor = os.open(temporary, os.O_RDWR)
    metadata = os.fstat(descriptor)

    def open_root(_root: Path) -> int:
        return root_fd

    def create_temp(_root_fd: int, _digest: str) -> tuple[int, str, tuple[int, int]]:
        return descriptor, temporary.name, (metadata.st_dev, metadata.st_ino)

    def set_read_only(_descriptor: int) -> None:
        return None

    def collide(_descriptor: int, _root_fd: int, _target_name: str) -> None:
        raise FileExistsError

    def deny_delete(_descriptor: int) -> None:
        raise OSError("deny")

    monkeypatch.setattr(install, "_windows_open_root", open_root)
    monkeypatch.setattr(
        install,
        "_windows_create_temp",
        create_temp,
    )
    monkeypatch.setattr(install, "_windows_set_read_only", set_read_only)
    monkeypatch.setattr(install, "_windows_rename_no_replace", collide)
    monkeypatch.setattr(install, "_windows_delete_owned", deny_delete)
    with pytest.raises(
        install.SecureFileInstallError, match="windows_handle_install_failed"
    ) as raised:
        install._install_windows_handle_relative(
            root, target.name, b"issuer bytes", hashlib.sha256(b"issuer bytes").hexdigest()
        )
    assert raised.value.residue_paths == (temporary,)


def test_windows_raw_handle_adoption_failure_reports_named_residue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "attempt"
    root.mkdir()
    digest = hashlib.sha256(b"issuer bytes").hexdigest()
    root_fd = os.open(root, os.O_RDONLY)

    def deny_adoption(_path: Path) -> int:
        raise OSError("injected adoption failure")

    ffi = _TempCreationFFI(root, digest, deny_adoption)
    monkeypatch.setattr(install, "_WINDOWS_FFI", ffi)
    monkeypatch.setattr(install.secrets, "token_hex", _fixed_token_hex)
    monkeypatch.setattr(install, "_windows_open_root", _return_root_descriptor(root_fd))

    with pytest.raises(
        install.SecureFileInstallError, match="windows_handle_install_failed"
    ) as raised:
        install._install_windows_handle_relative(root, "source.pdf", b"issuer bytes", digest)

    assert raised.value.residue_paths == (ffi.residue,)
    assert ffi.residue.exists()
    assert ffi.closed_raw_handles == [9876]


def test_windows_adopted_descriptor_fstat_failure_reports_named_residue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "attempt"
    root.mkdir()
    digest = hashlib.sha256(b"issuer bytes").hexdigest()
    root_fd = os.open(root, os.O_RDONLY)
    owned: list[int] = []

    def adopt(path: Path) -> int:
        descriptor = os.open(path, os.O_RDWR)
        owned.append(descriptor)
        return descriptor

    ffi = _TempCreationFFI(root, digest, adopt)
    original_fstat = install.os.fstat
    original_close = install.os.close
    closed: list[int] = []

    def fail_owned_fstat(descriptor: int) -> os.stat_result:
        if descriptor in owned:
            raise OSError("injected fstat failure")
        return original_fstat(descriptor)

    def record_close(descriptor: int) -> None:
        closed.append(descriptor)
        original_close(descriptor)

    monkeypatch.setattr(install, "_WINDOWS_FFI", ffi)
    monkeypatch.setattr(install.secrets, "token_hex", _fixed_token_hex)
    monkeypatch.setattr(install, "_windows_open_root", _return_root_descriptor(root_fd))
    monkeypatch.setattr(install.os, "fstat", fail_owned_fstat)
    monkeypatch.setattr(install.os, "close", record_close)

    with pytest.raises(
        install.SecureFileInstallError, match="windows_handle_install_failed"
    ) as raised:
        install._install_windows_handle_relative(root, "source.pdf", b"issuer bytes", digest)

    assert raised.value.residue_paths == (ffi.residue,)
    assert ffi.residue.exists()
    assert len(owned) == 1 and closed.count(owned[0]) == 1
    assert ffi.closed_raw_handles == []


def test_windows_unsafe_adopted_metadata_reports_named_residue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "attempt"
    root.mkdir()
    digest = hashlib.sha256(b"issuer bytes").hexdigest()
    root_fd = os.open(root, os.O_RDONLY)
    owned: list[int] = []

    def adopt(path: Path) -> int:
        descriptor = os.open(path, os.O_RDWR)
        owned.append(descriptor)
        return descriptor

    ffi = _TempCreationFFI(root, digest, adopt)
    original_fstat = install.os.fstat
    original_close = install.os.close
    closed: list[int] = []

    def unsafe_owned_metadata(descriptor: int) -> object:
        if descriptor in owned:
            return SimpleNamespace(st_mode=stat.S_IFDIR | 0o700, st_nlink=1)
        return original_fstat(descriptor)

    def record_close(descriptor: int) -> None:
        closed.append(descriptor)
        original_close(descriptor)

    monkeypatch.setattr(install, "_WINDOWS_FFI", ffi)
    monkeypatch.setattr(install.secrets, "token_hex", _fixed_token_hex)
    monkeypatch.setattr(install, "_windows_open_root", _return_root_descriptor(root_fd))
    monkeypatch.setattr(install.os, "fstat", unsafe_owned_metadata)
    monkeypatch.setattr(install.os, "close", record_close)

    with pytest.raises(
        install.SecureFileInstallError, match="windows_handle_install_failed"
    ) as raised:
        install._install_windows_handle_relative(root, "source.pdf", b"issuer bytes", digest)

    assert raised.value.residue_paths == (ffi.residue,)
    assert ffi.residue.exists()
    assert len(owned) == 1 and closed.count(owned[0]) == 1
    assert ffi.closed_raw_handles == []
