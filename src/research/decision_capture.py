"""Phase-2 multi-channel decision capture (plan §4.2).

Logs a decision ONLY on a position change above a size threshold. Assembles the
structured shape ``{ticker, direction, size_pct, conviction, falsifier}`` from
explicit fields and persists via the decisions writer (injected; ``conviction`` /
``falsifier`` are WRITE-ONCE at the writer), and links the decision back to the
originating musing.

Dependency-injected (``persist_fn`` / ``link_fn``) so the core is testable
without an LLM or a DB.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any


def _as_float(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) and value.strip():
        try:
            return float(value)
        except ValueError:
            return None
    return None


def capture_decision(
    *,
    ticker: str | None = None,
    direction: str | None = None,
    size_pct: float | None = None,
    conviction: str | None = None,
    falsifier: str | None = None,
    size_threshold: float = 1.0,
    note_id: int | None = None,
    db_path: Path | str | None = None,
    persist_fn: Callable[..., int] | None = None,
    link_fn: Callable[..., Any] | None = None,
) -> int | None:
    """Capture a decision iff the position change clears the size threshold.

    Returns the new decision id, or None (below threshold / missing a required
    field / no persist wired).
    """
    fields: dict[str, object | None] = {
        "ticker": ticker.strip().upper() if ticker else None,
        "direction": direction.strip().lower() if direction else None,
        "size_pct": size_pct,
        "conviction": conviction,
        "falsifier": falsifier,
    }

    # Size threshold: a trim/add below the threshold is NOT a logged decision.
    size = _as_float(fields.get("size_pct"))
    if size is not None and size < size_threshold:
        return None
    if not fields["ticker"] or not fields["direction"]:
        return None
    if persist_fn is None:
        return None

    decision_id = persist_fn(note_id=note_id, db_path=db_path, **fields)
    if link_fn is not None and note_id is not None and decision_id:
        link_fn(note_id=note_id, decision_id=int(decision_id), db_path=db_path)
    return int(decision_id) if decision_id else None
