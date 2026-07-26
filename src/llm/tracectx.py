"""Trace/stage context for LLM calls — P1 of the LLM Quality Program
(directives/llm_quality_program_2026_07.md).

The problem this solves, measured: diagnosing the July-2026 quota incident
took ~15 hand-written SQL queries against a flat ``llm_calls`` table, because
a row knows its purpose but not WHICH PIPELINE STAGE produced it. "Stage 0b is
burning 40% of the morning's tokens" was unanswerable without joining on
timestamps by eye.

Design — two propagation mechanisms, deliberately:

1. **In-process**: a ``contextvars.ContextVar`` holding the current
   ``TraceContext``. ``record_llm_call`` reads it implicitly, so no call site
   changes and no signature churn.
2. **Cross-process**: the morning pipeline runs its stages as SUBPROCESSES
   (``execution/run_morning_pipeline.py`` builds ``argv`` lists), so an
   in-process contextvar cannot reach them. ``child_env()`` serializes the
   context into environment variables and a child process seeds itself from
   them on first access. This mirrors how OpenTelemetry propagates context
   across process boundaries (a serialized ``traceparent``), and it is the
   only mechanism that actually works for this platform's architecture.

Naming follows the OTel GenAI semantic conventions where they map cleanly
(owner decision 2026-07-25: bespoke storage, OTel-aligned SEMANTICS, no
Phoenix/Langfuse service). ``trace_id``/``span_id``/``parent_span_id`` carry
their standard meanings; ``stage`` is platform-native (OTel has no stable
attribute for "pipeline stage" yet), so a future OTLP export is a projection
of these columns, never a rewrite.

Everything here is best-effort and non-raising: telemetry must never break an
LLM call. An absent context yields NULL columns — honestly "not traced",
never a fabricated id.
"""

from __future__ import annotations

import contextvars
import os
import uuid
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass, replace

# Environment keys for cross-process propagation (see module docstring).
ENV_TRACE_ID = "ES_TRACE_ID"
ENV_SPAN_ID = "ES_SPAN_ID"
ENV_STAGE = "ES_STAGE"


@dataclass(frozen=True, slots=True)
class TraceContext:
    """One point in the run tree. ``span_id`` identifies the CURRENT stage;
    ``parent_span_id`` its caller (None at the root)."""

    trace_id: str
    span_id: str
    stage: str
    parent_span_id: str | None = None


_CURRENT: contextvars.ContextVar[TraceContext | None] = contextvars.ContextVar(
    "es_llm_trace_context", default=None
)


def _new_id() -> str:
    return uuid.uuid4().hex[:16]


def _from_env() -> TraceContext | None:
    """Seed from the environment — how a subprocess joins its parent's trace.

    Requires BOTH a trace id and a stage: a half-set environment would produce
    a row that looks traced but isn't attributable, which is the
    silent-degradation shape this platform keeps finding. Half-set ⇒ no
    context ⇒ honest NULLs.
    """
    trace_id = (os.environ.get(ENV_TRACE_ID) or "").strip()
    stage = (os.environ.get(ENV_STAGE) or "").strip()
    if not trace_id or not stage:
        return None
    parent = (os.environ.get(ENV_SPAN_ID) or "").strip() or None
    return TraceContext(
        trace_id=trace_id,
        span_id=_new_id(),  # this process is a NEW span under the inherited parent
        stage=stage,
        parent_span_id=parent,
    )


def current() -> TraceContext | None:
    """The active context, seeding once from the environment if a parent
    process handed one down. Never raises."""
    ctx = _CURRENT.get()
    if ctx is not None:
        return ctx
    try:
        seeded = _from_env()
    except Exception:
        return None
    if seeded is not None:
        _CURRENT.set(seeded)
    return seeded


@contextmanager
def stage(name: str, *, trace_id: str | None = None) -> Generator[TraceContext]:
    """Open a stage span. Nests under any active context (inheriting its
    trace) or starts a new trace at the root.

    Usage::

        with tracectx.stage("morning_pipeline.0b.decision_conditions_extract"):
            ...  # every LLM call inside is attributed to this stage
    """
    parent = current()
    if parent is not None:
        ctx = TraceContext(
            trace_id=trace_id or parent.trace_id,
            span_id=_new_id(),
            stage=name,
            parent_span_id=parent.span_id,
        )
    else:
        ctx = TraceContext(
            trace_id=trace_id or _new_id(), span_id=_new_id(), stage=name, parent_span_id=None
        )
    token = _CURRENT.set(ctx)
    try:
        yield ctx
    finally:
        _CURRENT.reset(token)


def child_env(
    base: dict[str, str] | None = None, *, stage_name: str | None = None
) -> dict[str, str]:
    """An environment dict for a SUBPROCESS stage, carrying the current trace.

    ``stage_name`` overrides the stage label for the child (the orchestrator
    knows which stage it is launching; the child inherits the trace id and
    becomes a span under the orchestrator's span). With no active context the
    base env is returned unchanged — subprocesses stay untraced rather than
    inventing a rootless trace.
    """
    env = dict(base if base is not None else os.environ)
    ctx = current()
    if ctx is None and stage_name is None:
        return env
    if ctx is None:
        # The orchestrator itself isn't in a span but is labelling the child:
        # start a trace here so the child's rows are still attributable.
        env[ENV_TRACE_ID] = _new_id()
        env[ENV_SPAN_ID] = _new_id()
        env[ENV_STAGE] = str(stage_name)
        return env
    env[ENV_TRACE_ID] = ctx.trace_id
    env[ENV_SPAN_ID] = ctx.span_id
    env[ENV_STAGE] = stage_name or ctx.stage
    return env


def context_fields() -> tuple[str | None, str | None, str | None, str | None]:
    """(trace_id, span_id, parent_span_id, stage) for the ledger writer.
    All-None when nothing is active — "not traced", never fabricated."""
    ctx = current()
    if ctx is None:
        return None, None, None, None
    return ctx.trace_id, ctx.span_id, ctx.parent_span_id, ctx.stage


def with_stage(ctx: TraceContext, name: str) -> TraceContext:
    """A sibling context relabelled to ``name`` (same trace/span lineage)."""
    return replace(ctx, stage=name)
