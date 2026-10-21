"""Operations package.

Exports load on first access, so importing a light submodule such as
``operations.journal`` does not pull in the registry, evals and domain code.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from operations.models import ObservationState, OperationsRegistry, OperationsSnapshot
    from operations.registry import build_operations_registry
    from operations.snapshot import collect_operations_snapshot

__all__ = [
    "ObservationState",
    "OperationsRegistry",
    "OperationsSnapshot",
    "build_operations_registry",
    "collect_operations_snapshot",
]


def __getattr__(name: str) -> object:
    if name == "ObservationState":
        from operations.models import ObservationState

        return ObservationState
    if name == "OperationsRegistry":
        from operations.models import OperationsRegistry

        return OperationsRegistry
    if name == "OperationsSnapshot":
        from operations.models import OperationsSnapshot

        return OperationsSnapshot
    if name == "build_operations_registry":
        from operations.registry import build_operations_registry

        return build_operations_registry
    if name == "collect_operations_snapshot":
        from operations.snapshot import collect_operations_snapshot

        return collect_operations_snapshot
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
