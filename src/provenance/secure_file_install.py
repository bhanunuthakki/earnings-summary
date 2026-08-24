"""Small handle-pinned byte installer for attempt-private staging directories.

The public surface deliberately accepts a root plus one relative filename.  It
never follows a destination link, never replaces an existing file, and removes
only the inode created by this invocation when a write cannot be verified.
"""

from __future__ import annotations

import ctypes
import errno
import hashlib
import os
import secrets
import stat
import sys
import time
from dataclasses import dataclass
from pathlib import Path, PurePath
from typing import Protocol, cast

from provenance.immutable_artifact import (
    ImmutableArtifactConflictError,
    read_stable_artifact,
    require_no_reparse_points,
)


class SecureFileInstallError(RuntimeError):
    """The target cannot be installed without an unsafe filesystem transition."""

    def __init__(
        self,
        code: str,
        *,
        ownership: SecureFileOwnershipToken | None = None,
        residue_paths: tuple[Path, ...] = (),
    ) -> None:
        self.code = code
        self.ownership = ownership
        self.residue_paths = residue_paths
        super().__init__(code)


class _WindowsTempAdoptionError(OSError):
    """Named NT temporary existed but could not be safely adopted."""

    def __init__(self, name: str, cause: BaseException) -> None:
        self.name = name
        super().__init__(str(cause))


@dataclass(frozen=True, slots=True)
class SecureFileOwnershipToken:
    """Exact identity of a target created by one no-clobber transaction."""

    path: Path
    device: int
    inode: int

    # A token is authority, not an assertion made by a caller.  Only this
    # module's successful create transaction has the private issuer.
    def __init__(self, path: Path, device: int, inode: int, *, _issuer: object) -> None:
        if _issuer is not _TOKEN_ISSUER:
            raise TypeError("ownership tokens are issued only by a successful install")
        object.__setattr__(self, "path", path)
        object.__setattr__(self, "device", device)
        object.__setattr__(self, "inode", inode)


_TOKEN_ISSUER = object()


def _issue_ownership_token(path: Path, identity: tuple[int, int]) -> SecureFileOwnershipToken:
    return SecureFileOwnershipToken(path, *identity, _issuer=_TOKEN_ISSUER)


@dataclass(frozen=True, slots=True)
class SecureFileRoot:
    """A borrowed, already-pinned POSIX root descriptor for one transaction."""

    path: Path
    descriptor: int
    device: int
    inode: int


@dataclass(frozen=True, slots=True)
class SecureFileInstallResult:
    path: Path
    created: bool
    ownership: SecureFileOwnershipToken | None = None
    residue_paths: tuple[Path, ...] = ()


@dataclass(frozen=True, slots=True)
class SecureFileCleanupResult:
    path: Path
    removed: bool
    remaining: bool


def cleanup_owned_file(token: SecureFileOwnershipToken) -> SecureFileCleanupResult:
    """Retain a created target when no handle-owned delete authority is held."""
    # A device/inode comparison followed by pathname unlink is still a TOCTOU
    # delete.  This token deliberately does not retain a platform delete handle,
    # so every platform retains the residue for a typed caller recovery path.
    return SecureFileCleanupResult(token.path, removed=False, remaining=True)


def _verify_no_clobber_install(
    root: Path,
    relative_name: str,
    payload: bytes,
) -> None:
    """Validate bytes only; creation authority is minted by the transaction."""
    target = root / relative_name
    try:
        snapshot, observed = read_stable_artifact(target)
    except (OSError, ImmutableArtifactConflictError) as exc:
        raise SecureFileInstallError("installed_target_unsafe") from exc
    try:
        metadata = target.stat()
    except OSError as exc:
        raise SecureFileInstallError("installed_target_unsafe") from exc
    if (
        snapshot.file_sha256 != hashlib.sha256(payload).hexdigest()
        or snapshot.size_bytes != len(payload)
        or observed != payload
        or int(metadata.st_nlink) != 1
    ):
        raise SecureFileInstallError("installed_target_conflict")


def _existing_target_result(target: Path, payload: bytes, digest: str) -> SecureFileInstallResult:
    """Read an exact target, briefly allowing another installer's temp link to drain."""
    for attempt in range(32):
        try:
            snapshot, existing = read_stable_artifact(target)
            metadata = target.stat()
        except (OSError, ImmutableArtifactConflictError) as exc:
            raise SecureFileInstallError("existing_target_unsafe") from exc
        if int(metadata.st_nlink) == 1:
            if snapshot.file_sha256 != digest or existing != payload:
                raise SecureFileInstallError("existing_target_conflict") from None
            return SecureFileInstallResult(target, created=False)
        # A link count of two can only be observed during the other installer's
        # source-temp -> final-link transition.  It is not accepted as replay;
        # wait for the owner to drop its temporary name before deciding.
        if int(metadata.st_nlink) != 2 or attempt == 31:
            raise SecureFileInstallError("existing_target_unsafe")
        time.sleep(0.001)
    raise AssertionError("unreachable")


class _WindowsFunction(Protocol):
    argtypes: object
    restype: object

    def __call__(self, *args: object) -> int | None: ...


class _WindowsLibrary(Protocol):
    CreateFileW: _WindowsFunction
    CloseHandle: _WindowsFunction
    GetFileInformationByHandleEx: _WindowsFunction
    NtCreateFile: _WindowsFunction
    NtSetInformationFile: _WindowsFunction
    RtlNtStatusToDosError: _WindowsFunction
    SetFileInformationByHandle: _WindowsFunction


class _WindowsFFI(Protocol):
    def load_library(self, name: str, *, use_last_error: bool = False) -> _WindowsLibrary: ...

    def get_last_error(self) -> int: ...

    def open_osfhandle(self, handle: int, flags: int) -> int: ...

    def get_osfhandle(self, descriptor: int) -> int: ...


if sys.platform == "win32":
    import msvcrt as _native_msvcrt

    class _NativeWindowsFFI:
        @staticmethod
        def load_library(name: str, *, use_last_error: bool = False) -> _WindowsLibrary:
            return cast(_WindowsLibrary, ctypes.WinDLL(name, use_last_error=use_last_error))

        @staticmethod
        def get_last_error() -> int:
            return int(ctypes.get_last_error())

        @staticmethod
        def open_osfhandle(handle: int, flags: int) -> int:
            return int(_native_msvcrt.open_osfhandle(handle, flags))

        @staticmethod
        def get_osfhandle(descriptor: int) -> int:
            return int(_native_msvcrt.get_osfhandle(descriptor))

    _WINDOWS_FFI: _WindowsFFI = _NativeWindowsFFI()
else:

    class _UnavailableWindowsFFI:
        @staticmethod
        def _unavailable() -> RuntimeError:
            return RuntimeError("Windows filesystem APIs are unavailable")

        def load_library(self, name: str, *, use_last_error: bool = False) -> _WindowsLibrary:
            del name, use_last_error
            raise self._unavailable()

        def get_last_error(self) -> int:
            raise self._unavailable()

        def open_osfhandle(self, handle: int, flags: int) -> int:
            del handle, flags
            raise self._unavailable()

        def get_osfhandle(self, descriptor: int) -> int:
            del descriptor
            raise self._unavailable()

    _WINDOWS_FFI = _UnavailableWindowsFFI()


def _is_windows() -> bool:
    """Small platform seam so Windows handle behavior is testable off-host."""
    return os.name == "nt"


def _windows_error_from_status(status: int) -> int:
    ntdll = _WINDOWS_FFI.load_library("ntdll")
    convert = ntdll.RtlNtStatusToDosError
    convert.argtypes = (ctypes.c_long,)
    convert.restype = ctypes.c_uint32
    result = convert(status)
    if result is None:
        raise OSError("RtlNtStatusToDosError returned no status")
    return int(result)


def _windows_open_root(root: Path) -> int:
    kernel32 = _WINDOWS_FFI.load_library("kernel32", use_last_error=True)
    create = kernel32.CreateFileW
    create.argtypes = (
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
    )
    create.restype = ctypes.c_void_p
    raw = create(str(root), 0x80000000, 0x00000003, None, 3, 0x02200000, None)
    if raw in (None, ctypes.c_void_p(-1).value):
        error = _WINDOWS_FFI.get_last_error()
        raise OSError(error, os.strerror(error), str(root))
    try:
        return _WINDOWS_FFI.open_osfhandle(int(raw), os.O_RDONLY | getattr(os, "O_BINARY", 0))
    except Exception:
        kernel32.CloseHandle(ctypes.c_void_p(int(raw)))
        raise


def _windows_create_temp(root_fd: int, digest: str) -> tuple[int, str, tuple[int, int]]:
    class _UnicodeString(ctypes.Structure):
        _fields_ = (
            ("Length", ctypes.c_ushort),
            ("MaximumLength", ctypes.c_ushort),
            ("Buffer", ctypes.c_wchar_p),
        )

    class _ObjectAttributes(ctypes.Structure):
        _fields_ = (
            ("Length", ctypes.c_uint32),
            ("RootDirectory", ctypes.c_void_p),
            ("ObjectName", ctypes.POINTER(_UnicodeString)),
            ("Attributes", ctypes.c_uint32),
            ("SecurityDescriptor", ctypes.c_void_p),
            ("SecurityQualityOfService", ctypes.c_void_p),
        )

    class _IoStatusBlock(ctypes.Structure):
        _fields_ = (("Status", ctypes.c_void_p), ("Information", ctypes.c_size_t))

    create = _WINDOWS_FFI.load_library("ntdll").NtCreateFile
    create.restype = ctypes.c_long
    for _ in range(32):
        name = f".{digest}.{secrets.token_hex(16)}.tmp"
        buffer = ctypes.create_unicode_buffer(name)
        unicode_name = _UnicodeString(
            len(name) * ctypes.sizeof(ctypes.c_wchar),
            (len(name) + 1) * ctypes.sizeof(ctypes.c_wchar),
            ctypes.cast(buffer, ctypes.c_wchar_p),
        )
        attributes = _ObjectAttributes(
            ctypes.sizeof(_ObjectAttributes),
            _WINDOWS_FFI.get_osfhandle(root_fd),
            ctypes.pointer(unicode_name),
            0x40,
            None,
            None,
        )
        raw = ctypes.c_void_p()
        status_block = _IoStatusBlock()
        status = create(
            ctypes.byref(raw),
            0x0013019F,
            ctypes.byref(attributes),
            ctypes.byref(status_block),
            None,
            0x80,
            0x01,
            2,
            0x00200060,
            None,
            0,
        )
        if status is None:
            raise OSError("NtCreateFile returned no status")
        if status < 0:
            error = _windows_error_from_status(int(status))
            if error in (80, 183):
                continue
            raise OSError(error, os.strerror(error), name)
        if raw.value is None:
            raise OSError("NtCreateFile returned an empty handle")
        try:
            descriptor = _WINDOWS_FFI.open_osfhandle(
                int(raw.value), os.O_RDWR | getattr(os, "O_BINARY", 0)
            )
        except Exception:
            _WINDOWS_FFI.load_library("kernel32").CloseHandle(ctypes.c_void_p(int(raw.value)))
            raise _WindowsTempAdoptionError(name, OSError("cannot adopt owned temporary")) from None
        try:
            metadata = os.fstat(descriptor)
        except OSError as exc:
            os.close(descriptor)
            raise _WindowsTempAdoptionError(name, exc) from exc
        if not stat.S_ISREG(metadata.st_mode) or int(metadata.st_nlink) != 1:
            os.close(descriptor)
            raise _WindowsTempAdoptionError(
                name, OSError("owned temporary is not a regular single-link file")
            )
        return descriptor, name, (int(metadata.st_dev), int(metadata.st_ino))
    raise OSError("owned temporary name allocation was exhausted")


def _windows_rename_no_replace(descriptor: int, root_fd: int, target_name: str) -> None:
    class _FileRenameInfo(ctypes.Structure):
        _fields_ = (
            ("ReplaceIfExists", ctypes.c_ubyte),
            ("RootDirectory", ctypes.c_void_p),
            ("FileNameLength", ctypes.c_uint32),
            ("FileName", ctypes.c_wchar * (len(target_name) + 1)),
        )

    class _IoStatusBlock(ctypes.Structure):
        _fields_ = (("Status", ctypes.c_void_p), ("Information", ctypes.c_size_t))

    info = _FileRenameInfo()
    info.ReplaceIfExists = 0
    info.RootDirectory = _WINDOWS_FFI.get_osfhandle(root_fd)
    info.FileNameLength = len(target_name) * ctypes.sizeof(ctypes.c_wchar)
    info.FileName = target_name
    status_block = _IoStatusBlock()
    set_info = _WINDOWS_FFI.load_library("ntdll").NtSetInformationFile
    set_info.argtypes = (
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_int,
    )
    set_info.restype = ctypes.c_long
    status = set_info(
        ctypes.c_void_p(_WINDOWS_FFI.get_osfhandle(descriptor)),
        ctypes.byref(status_block),
        ctypes.byref(info),
        ctypes.sizeof(info),
        10,
    )
    if status is None:
        raise OSError("NtSetInformationFile returned no status")
    if status >= 0:
        return
    error = _windows_error_from_status(int(status))
    if error in (80, 183):
        raise FileExistsError(error, os.strerror(error), target_name)
    raise OSError(error, os.strerror(error), target_name)


def _windows_set_read_only(descriptor: int) -> None:
    class _FileBasicInfo(ctypes.Structure):
        _fields_ = (
            ("CreationTime", ctypes.c_longlong),
            ("LastAccessTime", ctypes.c_longlong),
            ("LastWriteTime", ctypes.c_longlong),
            ("ChangeTime", ctypes.c_longlong),
            ("FileAttributes", ctypes.c_uint32),
        )

    kernel32 = _WINDOWS_FFI.load_library("kernel32", use_last_error=True)
    get_info = kernel32.GetFileInformationByHandleEx
    set_info = kernel32.SetFileInformationByHandle
    handle = ctypes.c_void_p(_WINDOWS_FFI.get_osfhandle(descriptor))
    basic = _FileBasicInfo()
    if not get_info(handle, 0, ctypes.byref(basic), ctypes.sizeof(basic)):
        raise OSError(_WINDOWS_FFI.get_last_error(), "cannot read owned temporary attributes")
    basic.FileAttributes = (basic.FileAttributes & ~0x80) | 0x01
    if not set_info(handle, 0, ctypes.byref(basic), ctypes.sizeof(basic)):
        raise OSError(_WINDOWS_FFI.get_last_error(), "cannot seal owned temporary")


def _windows_delete_owned(descriptor: int) -> None:
    kernel32 = _WINDOWS_FFI.load_library("kernel32", use_last_error=True)
    set_info = kernel32.SetFileInformationByHandle
    handle = ctypes.c_void_p(_WINDOWS_FFI.get_osfhandle(descriptor))
    flags = ctypes.c_uint32(0x13)
    if set_info(handle, 21, ctypes.byref(flags), ctypes.sizeof(flags)):
        return
    raise OSError(_WINDOWS_FFI.get_last_error(), "cannot delete owned temporary")


def _write_all(descriptor: int, payload: bytes) -> None:
    remaining = memoryview(payload)
    while remaining:
        written = os.write(descriptor, remaining)
        if written <= 0:
            raise OSError("owned temporary write made no progress")
        remaining = remaining[written:]
    os.fsync(descriptor)


def _assert_windows_root_stable(root: Path, root_fd: int, expected: tuple[int, int]) -> None:
    direct = root.lstat()
    opened = os.fstat(root_fd)
    if (
        not stat.S_ISDIR(direct.st_mode)
        or not stat.S_ISDIR(opened.st_mode)
        or (int(direct.st_dev), int(direct.st_ino)) != expected
        or (int(opened.st_dev), int(opened.st_ino)) != expected
    ):
        raise OSError("root handle identity changed")


def _verify_windows_owned_install(
    descriptor: int,
    identity: tuple[int, int],
    payload: bytes,
) -> None:
    """Prove the renamed file through the still-owned Windows descriptor.

    The target name is deliberately not reopened until this handle is closed:
    a pathname read before then would introduce a replacement race between the
    successful no-replace rename and verification.
    """
    metadata = os.fstat(descriptor)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or int(metadata.st_nlink) != 1
        or (int(metadata.st_dev), int(metadata.st_ino)) != identity
        or int(metadata.st_size) != len(payload)
    ):
        raise OSError("installed target descriptor changed")
    os.lseek(descriptor, 0, os.SEEK_SET)
    observed = bytearray()
    while len(observed) < len(payload):
        chunk = os.read(descriptor, len(payload) - len(observed))
        if not chunk:
            raise OSError("installed target descriptor ended before expected payload")
        observed.extend(chunk)
    if bytes(observed) != payload:
        raise OSError("installed target descriptor bytes differ from expected payload")


def _assert_posix_root_stable(root: Path, root_fd: int, expected: tuple[int, int]) -> None:
    try:
        direct = root.lstat()
        opened = os.fstat(root_fd)
    except OSError as exc:
        raise SecureFileInstallError("root_identity_changed") from exc
    if (
        not stat.S_ISDIR(direct.st_mode)
        or not stat.S_ISDIR(opened.st_mode)
        or (int(direct.st_dev), int(direct.st_ino)) != expected
        or (int(opened.st_dev), int(opened.st_ino)) != expected
    ):
        raise SecureFileInstallError("root_identity_changed")


def _rename_no_replace(root_fd: int, source_name: str, target_name: str) -> None:
    """Atomically move an owned temp below a pinned root without replacement."""
    libc = ctypes.CDLL(None, use_errno=True)
    source = ctypes.c_char_p(os.fsencode(source_name))
    target = ctypes.c_char_p(os.fsencode(target_name))
    if sys.platform.startswith("linux"):
        function = getattr(libc, "renameat2", None)
        if function is None:
            raise SecureFileInstallError("atomic_no_replace_unavailable")
        function.argtypes = (
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        )
        function.restype = ctypes.c_int
        outcome = function(root_fd, source, root_fd, target, 1)  # RENAME_NOREPLACE
    elif sys.platform == "darwin":
        function = getattr(libc, "renameatx_np", None)
        if function is None:
            raise SecureFileInstallError("atomic_no_replace_unavailable")
        function.argtypes = (
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        )
        function.restype = ctypes.c_int
        outcome = function(root_fd, source, root_fd, target, 0x00000004)  # RENAME_EXCL
    else:
        raise SecureFileInstallError("atomic_no_replace_unavailable")
    if outcome == 0:
        return
    error = ctypes.get_errno()
    if error == errno.EEXIST:
        raise FileExistsError(error, os.strerror(error), target_name)
    raise OSError(error, os.strerror(error), target_name)


def _install_windows_handle_relative(
    root: Path, relative_name: str, payload: bytes, digest: str, *, read_only: bool = True
) -> SecureFileInstallResult:
    """Install through an NT root handle; no target path is opened by pathname."""
    target = root / relative_name
    root_fd: int | None = None
    descriptor: int | None = None
    token: SecureFileOwnershipToken | None = None
    temporary_name: str | None = None
    failure: SecureFileInstallError | None = None
    installed = False
    try:
        root_fd = _windows_open_root(root)
        root_metadata = os.fstat(root_fd)
        root_identity = (int(root_metadata.st_dev), int(root_metadata.st_ino))
        _assert_windows_root_stable(root, root_fd, root_identity)
        descriptor, temporary_name, identity = _windows_create_temp(root_fd, digest)
        _write_all(descriptor, payload)
        if read_only:
            _windows_set_read_only(descriptor)
        before = os.fstat(descriptor)
        if (int(before.st_dev), int(before.st_ino)) != identity or int(before.st_nlink) != 1:
            raise OSError("owned temporary identity changed")
        try:
            _windows_rename_no_replace(descriptor, root_fd, relative_name)
        except FileExistsError:
            _windows_delete_owned(descriptor)
            os.close(descriptor)
            descriptor = None
            try:
                snapshot, existing = read_stable_artifact(target)
            except (OSError, ImmutableArtifactConflictError) as exc:
                raise SecureFileInstallError("existing_target_unsafe") from exc
            if snapshot.file_sha256 != digest or existing != payload:
                raise SecureFileInstallError("existing_target_conflict") from None
            _assert_windows_root_stable(root, root_fd, root_identity)
            _verify_no_clobber_install(root, relative_name, payload)
            _assert_windows_root_stable(root, root_fd, root_identity)
            return SecureFileInstallResult(target, created=False)
        _verify_windows_owned_install(descriptor, identity, payload)
        installed = True
        token = _issue_ownership_token(target, identity)
        _assert_windows_root_stable(root, root_fd, root_identity)
        os.close(descriptor)
        descriptor = None
        try:
            _verify_no_clobber_install(root, relative_name, payload)
        except SecureFileInstallError as exc:
            raise SecureFileInstallError(exc.code, ownership=token) from exc
        _assert_windows_root_stable(root, root_fd, root_identity)
        return SecureFileInstallResult(target, created=True, ownership=token)
    except SecureFileInstallError as exc:
        failure = SecureFileInstallError(
            exc.code,
            ownership=token if token is not None and exc.ownership is None else exc.ownership,
            residue_paths=exc.residue_paths,
        )
        raise failure from exc
    except OSError as exc:
        residues = () if not isinstance(exc, _WindowsTempAdoptionError) else (root / exc.name,)
        failure = SecureFileInstallError(
            "windows_handle_install_failed", ownership=token, residue_paths=residues
        )
        raise failure from exc
    finally:
        cleanup_error: OSError | None = None
        if descriptor is not None:
            if not installed:
                try:
                    _windows_delete_owned(descriptor)
                except OSError as exc:
                    cleanup_error = exc
            os.close(descriptor)
        if root_fd is not None:
            os.close(root_fd)
        if cleanup_error is not None and temporary_name is not None:
            residue = (root / temporary_name,)
            if failure is not None:
                raise SecureFileInstallError(
                    failure.code,
                    ownership=failure.ownership,
                    residue_paths=(*failure.residue_paths, *residue),
                ) from failure
            raise SecureFileInstallError(
                "windows_temporary_cleanup_failed", residue_paths=residue
            ) from cleanup_error


def install_bytes_no_clobber(
    root: Path,
    relative_name: str,
    payload: bytes,
    *,
    expected_sha256: str | None = None,
    expected_size: int | None = None,
    read_only: bool = True,
    root_context: SecureFileRoot | None = None,
) -> SecureFileInstallResult:
    """Install exact bytes with create-only semantics below a pinned directory.

    The directory descriptor is the POSIX authority for the leaf operation;
    Windows routes through an NT root-handle-relative create/rename transaction.
    """
    relative = PurePath(relative_name)
    if (
        not relative_name
        or relative.is_absolute()
        or len(relative.parts) != 1
        or relative.parts[0] in {"", ".", ".."}
    ):
        raise SecureFileInstallError("unsafe_relative_name")
    digest = hashlib.sha256(payload).hexdigest()
    if (expected_sha256 is not None and digest != expected_sha256) or (
        expected_size is not None and len(payload) != expected_size
    ):
        raise SecureFileInstallError("payload_identity_mismatch")
    root = Path(os.path.abspath(os.fspath(root)))
    if root_context is None:
        try:
            require_no_reparse_points(root)
            root.mkdir(parents=True, exist_ok=True)
            require_no_reparse_points(root)
        except (OSError, ImmutableArtifactConflictError) as exc:
            raise SecureFileInstallError("staging_root_unsafe") from exc
    elif root_context.path != root:
        raise SecureFileInstallError("root_context_mismatch")
    target = root / relative_name
    if _is_windows():
        if read_only:
            return _install_windows_handle_relative(root, relative_name, payload, digest)
        return _install_windows_handle_relative(
            root, relative_name, payload, digest, read_only=False
        )
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    root_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_BINARY", 0)
    root_fd: int | None = None
    borrowed_root = root_context is not None
    descriptor: int | None = None
    temporary_name: str | None = None
    temporary_identity: tuple[int, int] | None = None
    token: SecureFileOwnershipToken | None = None
    installed = False
    try:
        if root_context is None:
            root_fd = os.open(root, root_flags | nofollow)
            root_metadata = os.fstat(root_fd)
            root_identity = (int(root_metadata.st_dev), int(root_metadata.st_ino))
        else:
            root_fd = root_context.descriptor
            root_identity = (root_context.device, root_context.inode)
        _assert_posix_root_stable(root, root_fd, root_identity)
        try:
            os.stat(relative_name, dir_fd=root_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            _assert_posix_root_stable(root, root_fd, root_identity)
            result = _existing_target_result(target, payload, digest)
            _assert_posix_root_stable(root, root_fd, root_identity)
            return result
        for _ in range(32):
            candidate = f".{relative_name}.{secrets.token_hex(16)}.tmp"
            try:
                descriptor = os.open(
                    candidate,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | nofollow,
                    0o600,
                    dir_fd=root_fd,
                )
            except FileExistsError:
                continue
            temporary_name = candidate
            break
        if descriptor is None or temporary_name is None:
            raise SecureFileInstallError("temporary_name_exhausted")
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or int(metadata.st_nlink) != 1:
            raise SecureFileInstallError("created_temporary_unsafe")
        temporary_identity = (int(metadata.st_dev), int(metadata.st_ino))
        _write_all(descriptor, payload)
        if read_only:
            os.fchmod(descriptor, stat.S_IREAD)
        after = os.fstat(descriptor)
        if (
            (int(after.st_dev), int(after.st_ino)) != temporary_identity
            or int(after.st_nlink) != 1
            or int(after.st_size) != len(payload)
        ):
            raise SecureFileInstallError("created_temporary_changed")
        named_temporary = os.stat(temporary_name, dir_fd=root_fd, follow_symlinks=False)
        if (int(named_temporary.st_dev), int(named_temporary.st_ino)) != temporary_identity:
            raise SecureFileInstallError("created_temporary_changed")
        _assert_posix_root_stable(root, root_fd, root_identity)
        try:
            _rename_no_replace(root_fd, temporary_name, relative_name)
        except FileExistsError:
            _assert_posix_root_stable(root, root_fd, root_identity)
            # The unlinked temporary is retained: a check-then-unlink cleanup
            # could delete a replacement.  The exact target remains replayable.
            result = _existing_target_result(target, payload, digest)
            _assert_posix_root_stable(root, root_fd, root_identity)
            return SecureFileInstallResult(
                result.path, created=False, residue_paths=(root / temporary_name,)
            )
        temporary_name = None
        # A successful dirfd-relative rename is the create transaction's proof
        # that the final name names our still-open temporary inode.
        token = _issue_ownership_token(target, temporary_identity)
        target_metadata = os.stat(relative_name, dir_fd=root_fd, follow_symlinks=False)
        if (
            not stat.S_ISREG(target_metadata.st_mode)
            or int(target_metadata.st_nlink) != 1
            or (int(target_metadata.st_dev), int(target_metadata.st_ino)) != temporary_identity
        ):
            raise SecureFileInstallError("installed_target_changed")
        installed = True
        _assert_posix_root_stable(root, root_fd, root_identity)
        try:
            _verify_no_clobber_install(root, relative_name, payload)
        except SecureFileInstallError as exc:
            raise SecureFileInstallError(exc.code, ownership=token) from exc
        _assert_posix_root_stable(root, root_fd, root_identity)
        return SecureFileInstallResult(target, created=True, ownership=token)
    except SecureFileInstallError as exc:
        residue = () if temporary_name is None else (root / temporary_name,)
        ownership = token if token is not None and exc.ownership is None else exc.ownership
        if ownership is not exc.ownership or residue:
            raise SecureFileInstallError(
                exc.code,
                ownership=ownership,
                residue_paths=(*exc.residue_paths, *residue),
            ) from exc
        raise
    except OSError as exc:
        residue = () if temporary_name is None else (root / temporary_name,)
        if token is not None:
            raise SecureFileInstallError(
                "secure_install_failed", ownership=token, residue_paths=residue
            ) from exc
        raise SecureFileInstallError("secure_install_failed", residue_paths=residue) from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        # A named temporary is deliberately retained on every pre-commit error.
        # No portable path-based unlink can prove ownership at deletion time.
        if token is not None and not installed:
            cleanup_owned_file(token)
        if root_fd is not None and not borrowed_root:
            os.close(root_fd)
