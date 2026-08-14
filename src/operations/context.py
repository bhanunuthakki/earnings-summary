"""Validated operation identity propagated across process boundaries.

Only identifiers and a stage label cross the environment boundary. Commands,
arguments, process environments, outputs, prompts, responses, URLs, and payloads
are deliberately outside this contract.
"""

from __future__ import annotations

import contextvars
import os
import re
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass

ENV_OPERATION_ID = "ES_OPERATION_ID"
ENV_TRACE_ID = "ES_TRACE_ID"
ENV_STAGE = "ES_STAGE"

_OPERATION_ID = re.compile(r"operation:[0-9a-f]{64}\Z")
_TRACE_ID = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")
_STAGE = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")
_UNSAFE_LABEL_FRAGMENTS = (
    "argv",
    "env",
    "prompt",
    "response",
    "stdout",
    "stderr",
    "payload",
    "secret",
    "token",
    "credential",
    "apikey",
    "api_key",
)


def _safe_label(value: str, pattern: re.Pattern[str], name: str) -> None:
    if pattern.fullmatch(value) is None:
        raise ValueError(f"{name} contains unsupported characters")
    lowered = value.casefold()
    if any(fragment in lowered for fragment in _UNSAFE_LABEL_FRAGMENTS):
        raise ValueError(f"{name} contains an unsafe label fragment")


@dataclass(frozen=True, slots=True)
class OperationContext:
    operation_id: str
    trace_id: str
    stage: str

    def __post_init__(self) -> None:
        if _OPERATION_ID.fullmatch(self.operation_id) is None:
            raise ValueError("operation_id must be a canonical operation identifier")
        _safe_label(self.trace_id, _TRACE_ID, "trace_id")
        _safe_label(self.stage, _STAGE, "stage")


_CURRENT: contextvars.ContextVar[OperationContext | None] = contextvars.ContextVar(
    "es_operation_context", default=None
)


def _from_env() -> OperationContext | None:
    values = (
        (os.environ.get(ENV_OPERATION_ID) or "").strip(),
        (os.environ.get(ENV_TRACE_ID) or "").strip(),
        (os.environ.get(ENV_STAGE) or "").strip(),
    )
    if not all(values):
        return None
    try:
        return OperationContext(operation_id=values[0], trace_id=values[1], stage=values[2])
    except ValueError:
        return None


def current() -> OperationContext | None:
    """Return the active validated context, or an honest ``None``."""

    return _CURRENT.get() or _from_env()


@contextmanager
def activate(*, operation_id: str, trace_id: str, stage: str) -> Generator[OperationContext]:
    """Activate one in-process operation context for nested telemetry writers."""

    active = OperationContext(operation_id=operation_id, trace_id=trace_id, stage=stage)
    token = _CURRENT.set(active)
    try:
        yield active
    finally:
        _CURRENT.reset(token)


def child_env(
    base: Mapping[str, str] | None = None,
    *,
    stage: str | None = None,
) -> dict[str, str]:
    """Copy *base* and add only the validated operation correlation fields."""

    env = dict(base if base is not None else os.environ)
    active = current()
    if active is None:
        return env
    child = OperationContext(
        operation_id=active.operation_id,
        trace_id=active.trace_id,
        stage=stage or active.stage,
    )
    env[ENV_OPERATION_ID] = child.operation_id
    env[ENV_TRACE_ID] = child.trace_id
    env[ENV_STAGE] = child.stage
    return env


__all__ = [
    "ENV_OPERATION_ID",
    "ENV_STAGE",
    "ENV_TRACE_ID",
    "OperationContext",
    "activate",
    "child_env",
    "current",
]
