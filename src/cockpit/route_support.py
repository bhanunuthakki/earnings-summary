"""Shared infrastructure contracts for local comments-server route modules.

Only cross-module mechanics belong here. Route-specific state and decisions
stay in each route family's context and implementation.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol


class ActivationCounter(Protocol):
    """Increment a named panel or route activation receipt."""

    def __call__(self, panel_id: str) -> None: ...


class BackgroundTaskStarter(Protocol):
    """Start one background task under the caller's naming convention."""

    def __call__(self, task: Callable[[], None], name: str) -> None: ...


class RedactedFailureLogger(Protocol):
    """Log a failure message without exposing secret-bearing exception text."""

    def __call__(self, message: str, exc: object, *, level: str = "error") -> None: ...


class InternalFailureResponder(Protocol):
    """Return the server's shaped internal-failure response."""

    def __call__(self, message: str, exc: object, *, status: int) -> tuple[dict[str, str], int]: ...


__all__ = [
    "ActivationCounter",
    "BackgroundTaskStarter",
    "InternalFailureResponder",
    "RedactedFailureLogger",
]
