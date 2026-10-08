"""Deliberate boot-time state for the production Ask retrieval plane.

Sealed Ask is opt-in and fails closed per turn: a missing or invalid
production scope registry, an underivable live cohort, or a disabled local
vector runtime each refuse retrieval rather than degrade an answer. This
module makes that state visible at boot so an operator sees exactly why the
sealed plane is (or is not) reachable in this deployment — the vector-runtime
gate is explicit and logged, never silently off.

The assessment never raises and never changes what any Ask mode answers or
cites; per-turn readiness remains the fail-closed gate that serves the turn.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from pathlib import Path
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict

from ask.audit_store import canonical_json, digest_text
from ask.engine import ask_retrieval_mode
from ask.sealed_retrieval import (
    PRODUCTION_SCOPE_REGISTRY_PATH,
    derive_production_scope_registry,
    load_production_scopes,
)
from search.embedding_promotion import LocalVectorRuntimeConfig
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite

log = logging.getLogger(__name__)

RegistryStatus = Literal[
    "valid",
    "missing",
    "invalid",
    "cohort_mismatch",
    "cohort_underivable",
]
VectorRuntimeStatus = Literal["disabled", "enabled", "invalid"]


class AskRetrievalBootState(BaseModel):
    """One deployment's config-level Ask retrieval reachability snapshot."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    retrieval_mode: str
    mode_valid: bool
    mode_detail: str
    registry_status: RegistryStatus
    registry_detail: str
    registry_scope_count: int | None
    vector_runtime_status: VectorRuntimeStatus
    vector_runtime_detail: str
    vector_runtime_index_root: str | None
    vector_runtime_runtime_root: str | None
    vector_runtime_roots_present: bool | None

    @property
    def sealed_plane_ready(self) -> bool:
        """Config-level prerequisites for sealed turns (promotions stay per-turn)."""
        return (
            self.registry_status == "valid"
            and self.vector_runtime_status == "enabled"
            and bool(self.vector_runtime_roots_present)
        )


def _assess_mode() -> tuple[str, bool, str]:
    raw = os.environ.get("ASK_RETRIEVAL_MODE", "grounded").strip().lower()
    try:
        return ask_retrieval_mode(), True, ""
    except ValueError as exc:
        return raw, False, str(exc)


def _registry_commitment_intact(committed: dict[str, object]) -> bool:
    """Mirror the loader's own registry commitment check for classification."""

    core = {key: value for key, value in committed.items() if key != "registry_sha256"}
    stored = committed.get("registry_sha256")
    return isinstance(stored, str) and digest_text(canonical_json(core)) == stored


def _assess_registry(
    conn: sqlite3.Connection,
    registry_path: Path,
) -> tuple[RegistryStatus, str, int | None]:
    if not registry_path.exists():
        return (
            "missing",
            f"production scope registry {registry_path} does not exist",
            None,
        )
    try:
        derive_production_scope_registry(conn)
    except (ValueError, sqlite3.Error) as exc:
        return (
            "cohort_underivable",
            f"live cohort derivation failed: {exc}",
            None,
        )
    try:
        decoded: object = json.loads(registry_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return "invalid", f"production scope registry is unreadable: {exc}", None
    if not isinstance(decoded, dict):
        return "invalid", "production scope registry must be a JSON object", None
    committed = cast(dict[str, object], decoded)
    try:
        scopes = load_production_scopes(conn, registry_path)
    except (OSError, ValueError) as exc:
        if _registry_commitment_intact(committed) and canonical_json(committed) != canonical_json(
            derive_production_scope_registry(conn)
        ):
            return (
                "cohort_mismatch",
                "committed production scope registry differs from the live frozen "
                f"cohort/source revisions: {exc}",
                None,
            )
        return "invalid", str(exc), None
    return (
        "valid",
        "committed production scope registry equals the live frozen cohort",
        len(scopes),
    )


def _assess_vector_runtime() -> tuple[VectorRuntimeStatus, str, LocalVectorRuntimeConfig | None]:
    try:
        runtime = LocalVectorRuntimeConfig.from_environment()
    except ValueError as exc:
        return "invalid", str(exc), None
    if runtime is None:
        return (
            "disabled",
            "EVIDENCE_VECTOR_RUNTIME_ENABLED is unset or 0; sealed semantic "
            "lanes are unavailable and sealed turns fail closed",
            None,
        )
    return (
        "enabled",
        "EVIDENCE_VECTOR_RUNTIME_ENABLED=1 with explicit index/runtime roots",
        runtime,
    )


def assess_ask_retrieval_boot_state(
    conn: sqlite3.Connection,
    *,
    registry_path: Path = PRODUCTION_SCOPE_REGISTRY_PATH,
) -> AskRetrievalBootState:
    """Assess sealed-plane reachability without mutating anything."""

    mode, mode_valid, mode_detail = _assess_mode()
    registry_status, registry_detail, scope_count = _assess_registry(conn, registry_path)
    runtime_status, runtime_detail, runtime = _assess_vector_runtime()
    return AskRetrievalBootState(
        retrieval_mode=mode,
        mode_valid=mode_valid,
        mode_detail=mode_detail,
        registry_status=registry_status,
        registry_detail=registry_detail,
        registry_scope_count=scope_count,
        vector_runtime_status=runtime_status,
        vector_runtime_detail=runtime_detail,
        vector_runtime_index_root=None if runtime is None else str(runtime.index_root),
        vector_runtime_runtime_root=None if runtime is None else str(runtime.runtime_root),
        vector_runtime_roots_present=(
            None
            if runtime is None
            else runtime.index_root.exists() and runtime.runtime_root.exists()
        ),
    )


def assess_ask_retrieval_boot_state_from_path(
    db_path: Path,
    *,
    registry_path: Path = PRODUCTION_SCOPE_REGISTRY_PATH,
) -> AskRetrievalBootState:
    """Open one read-only connection, assess, and close it."""

    conn = connect_sqlite(db_path, role=SQLiteConnectionRole.READ_ONLY)
    try:
        return assess_ask_retrieval_boot_state(conn, registry_path=registry_path)
    finally:
        conn.close()


def log_ask_retrieval_boot_state(state: AskRetrievalBootState) -> None:
    """Emit one structured boot line; sealed-selected-but-unready is an error."""

    sealed_selected = state.retrieval_mode == "sealed"
    unready = not state.mode_valid or (sealed_selected and not state.sealed_plane_ready)
    log.log(
        logging.ERROR if unready else logging.INFO,
        "ask_retrieval_boot_state mode=%s mode_valid=%s registry_status=%s "
        "registry_scope_count=%s vector_runtime_status=%s "
        "vector_runtime_roots_present=%s registry_detail=%r vector_runtime_detail=%r",
        state.retrieval_mode,
        state.mode_valid,
        state.registry_status,
        state.registry_scope_count,
        state.vector_runtime_status,
        state.vector_runtime_roots_present,
        state.registry_detail,
        state.vector_runtime_detail,
    )
