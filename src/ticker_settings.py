"""Per-ticker dashboard-owned settings (migration 0067).

One knob today: ``bypass_budget`` — the persistent "always ignore LLM budget caps
for this ticker" flag, set from the dashboard's per-ticker Refresh panel and read
by ``build_artifacts`` when resolving ``force_budget_bypass``. Best-effort: every
helper degrades to a safe default (``False`` / no-op) when the DB or table is
missing so a fresh repo / pre-0067 DB never breaks the build or the server.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from db_paths import resolve_db_path

log = logging.getLogger(__name__)


def get_bypass_budget(ticker: str, *, db_path: Path | str | None = None) -> bool:
    """True when `ticker` is set to always ignore LLM budget caps. Returns False
    on a missing DB / table / row or any read error (fail safe: no bypass)."""
    path = resolve_db_path(db_path)
    if path is None or not Path(path).exists():
        return False
    try:
        conn = sqlite3.connect(str(path), timeout=5.0)
        try:
            row = conn.execute(
                "SELECT bypass_budget FROM ticker_settings WHERE ticker = ?",
                (ticker.upper(),),
            ).fetchone()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        log.debug(
            {
                "event": "get_bypass_budget_failed",
                "ticker": ticker,
                "error": f"{type(exc).__name__}: {exc}",
            }
        )
        return False
    return bool(row[0]) if row is not None else False


def set_bypass_budget(
    ticker: str,
    value: bool,
    *,
    db_path: Path | str | None = None,
    now: datetime | None = None,
) -> bool:
    """Upsert the per-ticker `bypass_budget` flag. Returns True on write, False
    when the DB / table is unavailable."""
    path = resolve_db_path(db_path)
    if path is None or not Path(path).exists():
        return False
    ts = (now or datetime.now(UTC)).isoformat()
    try:
        conn = sqlite3.connect(str(path), timeout=5.0)
        try:
            conn.execute(
                """
                INSERT INTO ticker_settings (ticker, bypass_budget, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(ticker) DO UPDATE SET
                    bypass_budget = excluded.bypass_budget,
                    updated_at = excluded.updated_at
                """,
                (ticker.upper(), 1 if value else 0, ts, ts),
            )
            conn.commit()
            return True
        finally:
            conn.close()
    except sqlite3.Error as exc:
        log.warning(
            {
                "event": "set_bypass_budget_failed",
                "ticker": ticker,
                "error": f"{type(exc).__name__}: {exc}",
            }
        )
        return False
