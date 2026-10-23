"""Start the sealed filing parser through the shared Windows AppContainer controls.

Copied into the sealed runtime as earnings_summary_xbrl_launcher.py. Run only with
that runtime's Python -I -S -B; no project or site-package imports are required.
"""

from __future__ import annotations

import ctypes
import importlib.util
import json
import os
import re
import sys
from collections.abc import Sequence
from contextlib import suppress
from ctypes import wintypes
from pathlib import Path

_MAXIMUM_STDIN_BYTES = 64 * 1024 * 1024


def _invocation(
    arguments: Sequence[str],
) -> tuple[Path, Path, tuple[str, ...], int, int, int, str, str]:
    if (
        len(arguments) < 19
        or tuple(arguments[:4])
        != ("--contract", "earnings-xbrl-os-sandbox.v1", "--deny-network", "--read-tree")
        or arguments[5] != "--read-input-tree"
        or tuple(arguments[7:17:2])
        != (
            "--timeout-seconds",
            "--cancel-handle",
            "--cleanup-handle",
            "--invocation-id",
            "--installation-id",
        )
        or arguments[17] != "--"
    ):
        raise ValueError("invalid launcher contract")
    runtime_root = Path(arguments[4])
    input_root = Path(arguments[6])
    if not runtime_root.is_absolute() or not input_root.is_absolute():
        raise ValueError("launcher roots must be absolute")
    if runtime_root != Path(__file__).absolute().parent:
        raise ValueError("launcher runtime identity mismatch")
    if any(re.fullmatch(r"[1-9][0-9]{0,19}", arguments[index]) is None for index in (8, 10, 12)):
        raise ValueError("launcher control values must be decimal")
    timeout, cancel, cleanup = (int(arguments[index]) for index in (8, 10, 12))
    invocation, installation = arguments[14], arguments[16]
    if (
        not 1 <= timeout <= 3600
        or not 0 < cancel < 2**64
        or not 0 < cleanup < 2**64
        or cancel == cleanup
    ):
        raise ValueError("launcher control bounds invalid")
    if (
        re.fullmatch(r"[0-9a-f]{32}", invocation) is None
        or re.fullmatch(r"[0-9a-f]{64}", installation) is None
    ):
        raise ValueError("launcher control identity invalid")
    command = tuple(arguments[18:])
    if not command or not Path(command[0]).is_absolute():
        raise ValueError("invalid child command")
    return runtime_root, input_root, command, timeout, cancel, cleanup, invocation, installation


def _load_support(runtime_root: Path):
    support = runtime_root / "earnings_summary_windows_appcontainer.py"
    spec = importlib.util.spec_from_file_location("earnings_summary_windows_appcontainer", support)
    if spec is None or spec.loader is None:
        raise ValueError("sandbox support unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _acknowledge(handle: int, invocation: str, installation: str, proved: bool) -> None:
    if sys.platform != "win32":
        raise ValueError("Windows cleanup acknowledgement unavailable")
    body = json.dumps(
        {"invocation_id": invocation, "installation_id": installation, "cleanup_proved": proved},
        sort_keys=True,
    ).encode()
    native = ctypes.WinDLL("kernel32", use_last_error=True)
    native.WriteFile.argtypes = [
        wintypes.HANDLE,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.c_void_p,
    ]
    native.WriteFile.restype = wintypes.BOOL
    written = wintypes.DWORD()
    buffer = ctypes.create_string_buffer(body)
    if not native.WriteFile(
        handle, buffer, len(body), ctypes.byref(written), None
    ) or written.value != len(body):
        raise ValueError("cleanup acknowledgement failed")


def main(argv: Sequence[str] | None = None) -> int:
    control: tuple[int, str, str] | None = None
    try:
        runtime_root, input_root, command, timeout, cancel, cleanup, invocation, installation = (
            _invocation(sys.argv[1:] if argv is None else argv)
        )
        control = (cleanup, invocation, installation)
        payload = sys.stdin.buffer.read(_MAXIMUM_STDIN_BYTES + 1)
        if len(payload) > _MAXIMUM_STDIN_BYTES:
            raise ValueError("launcher input limit exceeded")
        if os.name != "nt":
            raise ValueError("Windows sandbox unavailable")
        support = _load_support(runtime_root)
        result = support.run_filing_appcontainer(
            command,
            runtime_root=runtime_root,
            input_root=input_root,
            payload=payload,
            timeout_seconds=timeout,
            cancel_handle=cancel,
        )
        _acknowledge(cleanup, invocation, installation, True)
        return result
    except Exception as exc:
        if control is not None and getattr(exc, "cleanup_proved", False) is True:
            with suppress(Exception):
                _acknowledge(*control, True)
        # Only bounded identifiers cross this boundary. Exception text can contain
        # private paths, source bytes, command arguments, or credentials.
        stage = getattr(exc, "stage", "source_launcher")
        if (
            not isinstance(stage, str)
            or not stage.isascii()
            or re.fullmatch(r"[a-z][a-z0-9_]{0,63}", stage) is None
        ):
            stage = "source_launcher"
        error = getattr(exc, "winerror", 0)
        if type(error) is not int or not 0 <= error <= 0xFFFFFFFF:
            error = 0
        sys.stderr.write(
            json.dumps(
                {
                    "event": "filing_xbrl_sandbox_refused",
                    "error_type": "SandboxRefused",
                    "native_error_code": error,
                    "stage": stage,
                },
                sort_keys=True,
            )
            + "\n"
        )
        return 125


if __name__ == "__main__":
    raise SystemExit(main())
