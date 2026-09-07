"""Public facade for the operational lifecycle producer slice."""

from __future__ import annotations

from .lifecycle_discovery import (
    REGISTRY_AUTHORITIES,
    LifecycleEvidenceFields,
    lifecycle_evidence_fields,
)
from .lifecycle_inventory import (
    build_inventory,
    is_protected_lifecycle_output,
    load_inventory,
    validate_inventory,
)
from .lifecycle_models import (
    SCHEMA_VERSION,
    Disposition,
    DormantPolicy,
    LifecycleEntry,
    LifecycleError,
    LifecycleInventory,
    Surface,
)

MAX_STDOUT_BYTES = 100_000

__all__ = [
    "MAX_STDOUT_BYTES",
    "REGISTRY_AUTHORITIES",
    "SCHEMA_VERSION",
    "Disposition",
    "DormantPolicy",
    "LifecycleEntry",
    "LifecycleError",
    "LifecycleEvidenceFields",
    "LifecycleInventory",
    "Surface",
    "build_inventory",
    "is_protected_lifecycle_output",
    "lifecycle_evidence_fields",
    "load_inventory",
    "validate_inventory",
]
