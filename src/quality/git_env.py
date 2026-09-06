"""Isolate local Git subprocesses from an outer repository or hook."""

from __future__ import annotations

import os
from collections.abc import Mapping


def clean_local_git_env(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """Return an environment without Git's inherited repository context."""
    source = os.environ if environ is None else environ
    return {key: value for key, value in source.items() if not key.upper().startswith("GIT_")}


def is_git_executable(command: str) -> bool:
    """Recognize Git executables portably, including Windows wrappers."""
    name = command.replace("\\", "/").rsplit("/", 1)[-1].casefold()
    return name in {"git", "git.exe", "git.cmd"}
