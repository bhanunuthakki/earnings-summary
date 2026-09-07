"""Typed models for the operational lifecycle inventory."""

from __future__ import annotations

import hashlib
from datetime import date
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator

SCHEMA_VERSION = "operational-lifecycle-inventory/v1"
POLICY_SCHEMA = "operational-lifecycle-dormant-policy/v1"


Disposition = Literal[
    "scheduled",
    "service",
    "ui-reachable",
    "manual-supported",
    "internal-delegate",
    "dormant-until",
    "one-shot-completed",
    "compatibility-tombstone",
    "retire",
]
Surface = Literal[
    "python_module",
    "flask_route",
    "scheduled_task",
    "wrapper",
    "service",
    "reconstruction",
    "registry",
]


class LifecycleError(RuntimeError):
    pass


def require_current_iso_date(value: str, *, field: str, today: date | None = None) -> None:
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO date") from exc
    current = today if today is not None else date.today()
    if parsed < current:
        raise ValueError(f"{field} is expired")


def read_text(path: Path) -> str:
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "utf-16", "utf-16-le", "utf-16-be"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def resolve_repo_file(root: Path, rel: str, *, label: str) -> Path:
    """Resolve a regular repository file without following symlink components."""
    candidate = Path(rel)
    if rel == "" or candidate.is_absolute() or ".." in candidate.parts:
        raise LifecycleError(f"{label} is not a repository file: {rel}")
    root_resolved = root.resolve()
    intended = root_resolved / candidate
    current = root_resolved
    for part in candidate.parts:
        if part in ("", ".", ".."):
            raise LifecycleError(f"{label} is not a repository file: {rel}")
        current = current / part
        try:
            if current.is_symlink():
                raise LifecycleError(f"{label} is not a repository file: {rel}")
        except OSError as exc:
            raise LifecycleError(f"{label} is not a repository file: {rel}") from exc
    try:
        resolved = intended.resolve()
    except OSError as exc:
        raise LifecycleError(f"{label} is not a repository file: {rel}") from exc
    if resolved != intended or not resolved.is_relative_to(root_resolved):
        raise LifecycleError(f"{label} is not a repository file: {rel}")
    try:
        if resolved.is_symlink() or not resolved.is_file():
            raise LifecycleError(f"{label} is not a repository file: {rel}")
    except OSError as exc:
        raise LifecycleError(f"{label} is not a repository file: {rel}") from exc
    return resolved


def source_line(root: Path, path: str, line: int) -> str:
    lines = read_text(root / path).splitlines()
    if line < 1 or line > len(lines):
        raise LifecycleError(f"invalid evidence line {path}:{line}")
    return lines[line - 1].strip()


def fingerprint(path: str, line: int, evidence: str) -> str:
    return hashlib.sha256(f"{path}:{line}:{evidence.strip()}".encode()).hexdigest()


class LifecycleEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str
    line: int
    kind: Surface
    identifier: str
    evidence: str
    fingerprint: str
    disposition: Disposition
    classification_basis: str
    rationale: str
    targets: tuple[str, ...] = ()
    methods: tuple[str, ...] = ()
    endpoint: str | None = None
    owner_evidence: str | None = None
    invocation_evidence: str | None = None
    incoming_edge: str | None = None
    sealed_completion_evidence: str | None = None
    tombstone_consumer: str | None = None
    tombstone_expiry: str | None = None
    dormant_owner: str | None = None
    dormant_activation: str | None = None
    dormant_review: str | None = None
    dormant_policy_evidence: str | None = None
    retirement_evidence: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _check(self) -> LifecycleEntry:
        if self.disposition == "manual-supported" and (
            not self.owner_evidence
            or not self.owner_evidence.startswith(("canonical:", "runbook:"))
            or not self.invocation_evidence
        ):
            raise ValueError(
                "manual-supported requires canonical/runbook owner and invocation evidence"
            )
        if self.disposition == "internal-delegate" and not self.incoming_edge:
            raise ValueError("internal-delegate requires incoming typed edge evidence")
        if self.disposition == "one-shot-completed" and (
            not self.sealed_completion_evidence
            or not self.sealed_completion_evidence.startswith("sealed:")
        ):
            raise ValueError("one-shot-completed requires sealed completion evidence")
        if self.disposition == "compatibility-tombstone":
            if not self.tombstone_consumer or not self.tombstone_expiry:
                raise ValueError("compatibility-tombstone requires consumer and expiry")
            if self.tombstone_consumer.lower() in {"none", "unknown", "n/a"}:
                raise ValueError("compatibility-tombstone requires a named consumer")
            require_current_iso_date(self.tombstone_expiry, field="tombstone_expiry")
        if self.disposition == "dormant-until":
            if not all(
                (
                    self.dormant_owner,
                    self.dormant_activation,
                    self.dormant_review,
                    self.dormant_policy_evidence,
                )
            ):
                raise ValueError(
                    "dormant-until requires owner, activation, review, and policy evidence"
                )
            assert self.dormant_review is not None
            require_current_iso_date(self.dormant_review, field="dormant_review")
        if self.disposition == "retire":
            need = {
                "no-incoming-runtime-edges",
                "no-route-or-ui-surface",
                "no-scheduler-or-service-owner",
                "no-registry-or-reconstruction-contract",
                "behavioral-suite-pass",
            }
            if not need.issubset({i.split(":", 1)[0] for i in self.retirement_evidence}):
                raise ValueError("retire requires all five deletion-proof evidence classes")
        return self


class LifecycleInventory(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: str = SCHEMA_VERSION
    status: Literal["PASS", "HOLD"]
    entries: tuple[LifecycleEntry, ...]
    counts: dict[str, int]
    surface_counts: dict[str, int]
    tracked_tree_hash: str
    revision: str
    worktree_dirty: bool
    reachability_graph_hash: str
    graph_parser: dict[str, str]
    coverage: dict[str, int]
    omissions: tuple[str, ...]
    extras: tuple[str, ...]
    violations: tuple[str, ...]


class DormantPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["operational-lifecycle-dormant-policy/v1"]
    owner_evidence: str
    authorization_evidence: str
    activation_evidence: str
    review_on: str
    path_prefixes: tuple[str, ...]
    exact_paths: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _check(self) -> DormantPolicy:
        if self.owner_evidence != "linear:BHA-142":
            raise ValueError("dormant policy owner must be linear:BHA-142")
        if not self.authorization_evidence.strip() or not self.activation_evidence.strip():
            raise ValueError("dormant policy requires authorization and activation evidence")
        # ISO syntax is fail-closed here; expiry admission is a HOLD violation
        # recorded by build_inventory so stale policy is semantic evidence.
        try:
            date.fromisoformat(self.review_on)
        except ValueError as exc:
            raise ValueError("review_on must be an ISO date") from exc
        if not self.path_prefixes and not self.exact_paths:
            raise ValueError("dormant policy scope is empty")
        return self

    def covers(self, path: str) -> bool:
        return path in self.exact_paths or path.startswith(self.path_prefixes)


def reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    seen: dict[str, object] = {}
    for key, value in pairs:
        if key in seen:
            raise ValueError(f"duplicate object key: {key}")
        seen[key] = value
    return seen
