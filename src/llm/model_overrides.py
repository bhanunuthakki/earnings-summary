"""DB-backed model-pin override lookup for the model-eval auto-switch loop.

``_model_for`` in ``src/llm/cli.py`` calls ``active_override(purpose)`` on
every LLM call.  When an active row exists in ``model_pin_overrides``, that
model wins over the hardcoded ``LLM_MODELS`` pin — the loop can route a
purpose to a cheaper model WITHOUT a code change.

The flip side: ``write_pin_override`` and ``deactivate_override`` are called
by ``execution/apply_model_switches.py`` after the eval sweep accumulates
enough evidence.

All helpers degrade gracefully — a DB read failure never blocks an LLM call.
The ``db_path`` param mirrors ``llm_budget._resolve_db_path``:
  1. An explicit override wins (useful in tests and CLIs that pass --repo-root).
  2. Otherwise ``db.DB_PATH`` is imported at call-time so the per-process
     ``set_db_path`` override (set by any CLI that passes --db-path) is
     respected without passing the path through every layer.
  3. If neither is available (no DB context yet), the function returns None /
     is a no-op — identical to "no override row".
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

log = logging.getLogger(__name__)

SET_BY_AUTO = "auto:model_eval_loop"


def _resolve_db_path(override: Path | str | None) -> Path | None:
    if override is not None:
        return Path(override)
    try:
        from db import DB_PATH

        return Path(DB_PATH)
    except ImportError:
        return None


def active_override(purpose: str, *, db_path: Path | str | None = None) -> str | None:
    """Return the active pinned model for ``purpose``, or None if no active
    override row exists (or if the table is missing / DB unavailable).

    Reads the most recently set active override so that a manual correction
    always wins over an older auto-switch row.
    """
    path = _resolve_db_path(db_path)
    if path is None or not Path(path).exists():
        return None
    try:
        conn = sqlite3.connect(str(path), timeout=5.0)
        try:
            row = conn.execute(
                """
                SELECT model FROM model_pin_overrides
                WHERE purpose = ? AND active = 1
                ORDER BY set_at DESC
                LIMIT 1
                """,
                (purpose,),
            ).fetchone()
        finally:
            conn.close()
        return row[0] if row else None
    except sqlite3.Error as exc:
        log.debug(
            {
                "event": "model_override_read_failed",
                "purpose": purpose,
                "error": f"{type(exc).__name__}: {exc}",
            }
        )
        return None


def write_pin_override(
    purpose: str,
    model: str,
    *,
    set_by: str = SET_BY_AUTO,
    reason: dict[str, object] | None = None,
    db_path: Path | str | None = None,
) -> None:
    """Insert an active override row for ``purpose`` → ``model``.

    Deactivates any previously active overrides for the same purpose first so
    only one active row exists at a time. The deactivated rows remain as an
    audit trail.

    ``reason`` is an arbitrary JSON-serialisable dict (run_id, parity_rate,
    verdict_count, …) persisted as ``reason_json``.
    """
    path = _resolve_db_path(db_path)
    if path is None:
        raise RuntimeError("write_pin_override: no DB path available")
    now_iso = datetime.now(UTC).replace(tzinfo=None).isoformat()
    reason_json = json.dumps(reason or {}, ensure_ascii=False)
    conn = sqlite3.connect(str(path), timeout=10.0)
    try:
        conn.execute(
            "UPDATE model_pin_overrides SET active = 0 WHERE purpose = ? AND active = 1",
            (purpose,),
        )
        conn.execute(
            """
            INSERT INTO model_pin_overrides (purpose, model, set_by, set_at, reason_json, active)
            VALUES (?, ?, ?, ?, ?, 1)
            """,
            (purpose, model, set_by, now_iso, reason_json),
        )
        conn.commit()
    finally:
        conn.close()
    log.info(
        {
            "event": "model_pin_override_written",
            "purpose": purpose,
            "model": model,
            "set_by": set_by,
        }
    )


def deactivate_override(
    purpose: str,
    *,
    db_path: Path | str | None = None,
) -> bool:
    """Deactivate any active override for ``purpose``.

    Returns True if at least one row was deactivated, False if no active row
    existed (idempotent).  The rows are kept (active=0) as history.
    """
    path = _resolve_db_path(db_path)
    if path is None:
        raise RuntimeError("deactivate_override: no DB path available")
    conn = sqlite3.connect(str(path), timeout=10.0)
    try:
        cur = conn.execute(
            "UPDATE model_pin_overrides SET active = 0 WHERE purpose = ? AND active = 1",
            (purpose,),
        )
        conn.commit()
        deactivated = cur.rowcount > 0
    finally:
        conn.close()
    if deactivated:
        log.info({"event": "model_pin_override_deactivated", "purpose": purpose})
    return deactivated


# NOTE: verdict WRITES live in execution/run_model_eval_sweep.py
# (``_insert_verdict``, the PR2 sweep). The PR3 draft of this module carried a
# duplicate ``record_verdict`` writer against a divergent column set (the #441
# vs #443 mid-air schema fork); it had no callers and was removed when the
# schemas were reconciled onto the sweep's table shape.
