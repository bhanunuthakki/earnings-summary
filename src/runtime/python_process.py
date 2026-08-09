"""Canonical argv builder for repository Python child processes.

Every repository script runs behind ``execution/sqlite_bootstrap.py`` so a
writer cannot accidentally inherit the host interpreter's vulnerable SQLite
library. External executables and Python ``-c`` probes are intentionally out of
scope and continue to use ``subprocess`` directly.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Sequence
from pathlib import Path


def managed_python_prefix(
    repo_root: str | os.PathLike[str],
    *,
    unbuffered: bool = False,
    executable: str | os.PathLike[str] | None = None,
) -> list[str]:
    """Return the interpreter/bootstrap prefix for a repository child."""
    root = Path(repo_root).resolve()
    argv = [os.fspath(executable or sys.executable)]
    if unbuffered:
        argv.append("-u")
    argv.append(os.fspath(root / "execution" / "sqlite_bootstrap.py"))
    return argv


def managed_python_argv(
    repo_root: str | os.PathLike[str],
    target: str | os.PathLike[str],
    *arguments: str,
    unbuffered: bool = False,
    executable: str | os.PathLike[str] | None = None,
) -> list[str]:
    """Build a bootstrap-aware argv for one Python file inside ``repo_root``."""
    root = Path(repo_root).resolve()
    script = Path(target)
    if not script.is_absolute():
        script = root / script
    script = script.resolve()
    try:
        script.relative_to(root)
    except ValueError as exc:
        raise ValueError("managed Python target must stay inside the repository") from exc
    if script.suffix.lower() != ".py":
        raise ValueError("managed Python target must be a .py file")
    bootstrap = root / "execution" / "sqlite_bootstrap.py"
    argv = managed_python_prefix(root, unbuffered=unbuffered, executable=executable)
    assert Path(argv[-1]).resolve() == bootstrap
    argv.extend([os.fspath(script), *arguments])
    return argv


def ensure_managed_python_argv(
    repo_root: str | os.PathLike[str], command: Sequence[str]
) -> list[str]:
    """Wrap a raw current-interpreter repository-script command once.

    Commands already using the bootstrap, external interpreters, modules, and
    non-Python executables are returned unchanged.
    """
    argv = list(command)
    if len(argv) < 2 or Path(argv[0]).resolve() != Path(sys.executable).resolve():
        return argv
    index = 1
    unbuffered = False
    if argv[index] == "-u":
        unbuffered = True
        index += 1
    if index >= len(argv) or argv[index] == "-m":
        return argv
    root = Path(repo_root).resolve()
    target = Path(argv[index])
    if not target.is_absolute():
        target = root / target
    target = target.resolve()
    bootstrap = (root / "execution" / "sqlite_bootstrap.py").resolve()
    if target == bootstrap:
        return argv
    try:
        target.relative_to(root)
    except ValueError:
        return argv
    if target.suffix.lower() != ".py":
        return argv
    return managed_python_argv(
        root,
        target,
        *argv[index + 1 :],
        unbuffered=unbuffered,
        executable=argv[0],
    )


__all__ = ["ensure_managed_python_argv", "managed_python_argv", "managed_python_prefix"]
