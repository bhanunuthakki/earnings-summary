"""Canonical clock for the repo's naive-UTC TEXT-timestamp convention.

Every TEXT timestamp column in this codebase stores a *naive*-UTC ISO 8601
string (no ``+00:00`` offset). The reason is comparison safety: a tz-aware
stamp read back as a ``datetime`` raises ``TypeError`` the moment any consumer
compares it against the naive ``datetime`` values every other store already
holds (``aware < naive`` is illegal). See ``project_naive_utc_datetime_convention``.

This module is the single source of truth so the ~dozen ``now_iso`` /
``_now_iso`` helpers scattered across the stores can't drift back to aware.
It is deliberately side-effect-free — it imports only the stdlib, so any store
module can depend on it without dragging in db / LLM / Flask. It mirrors
``evals.harness.now_naive_utc``, the original instance of this helper.
"""

from __future__ import annotations

from datetime import UTC, datetime


def now_naive_utc() -> datetime:
    """Current UTC instant as a *naive* ``datetime`` (``tzinfo`` stripped)."""
    return datetime.now(UTC).replace(tzinfo=None)


def now_iso() -> str:
    """Current UTC instant as a naive-UTC ISO 8601 string — the canonical
    format for every TEXT timestamp column in this repo."""
    return now_naive_utc().isoformat()


def to_naive_utc(dt: datetime) -> datetime:
    """Normalize a possibly-aware ``datetime`` to naive-UTC.

    Aware inputs are converted to UTC and then stripped of ``tzinfo``; naive
    inputs (already assumed UTC by convention) pass through unchanged. Use this
    on read paths so rows written before the naive-UTC migration (which still
    carry a ``+00:00`` offset) and rows written after it compare without raising.
    """
    if dt.tzinfo is not None:
        return dt.astimezone(UTC).replace(tzinfo=None)
    return dt
