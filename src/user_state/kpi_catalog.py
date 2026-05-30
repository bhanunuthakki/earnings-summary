"""Fireable-KPI-name catalog (auto-seed correctness core).

This is correctness mechanism #1 for the auto KPI seeder (see
``scratch/plans/auto_kpi_seeding_plan.md`` §4). The KPI-inflection trigger
loads each registered KPI's series with ``load_kpi_series(ticker, kpi_name)``,
whose SQL joins ``kpi_definitions kd ON ... WHERE kd.name = ?`` — an exact,
case-sensitive (SQLite ``BINARY`` collation) match. If the registry's
``kpi_name`` is not byte-identical to a ``kpi_definitions.name`` row for that
ticker, the series comes back empty, ``_candidate_from_series`` bails at
``len(series) < _MIN_SERIES_LEN``, and there is **no candidate, no alert, no
error** — a silent no-fire. ``"NIM"`` != ``"Net Interest Margin"`` != ``"nim"``.

:func:`allowed_kpi_names` returns the closed set of names the auto-seeder is
allowed to write. A name is in the set only when it has enough quarterly facts
to actually produce a fireable series, so name-validity and fire-ability are
the *same* gate.

Pure read-side helper — best-effort like ``src/timeseries/loaders.py``: a
missing DB / missing tables yields an empty set rather than raising (a fresh
repo simply offers no candidates). It deliberately does NOT use the loud
``user_state._db.open_conn`` path, which is reserved for primary writes.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import cast

log = logging.getLogger(__name__)

# Mirrors ``triggers.kpi_inflection._MIN_SERIES_LEN`` (kpi_inflection.py:81):
# detect_inflection needs >= 8 observations, so a name with fewer quarterly
# kpi_facts rows would pass a naive "exists?" check yet never fire. Keeping the
# floors equal makes name-validity and fire-ability the same gate (plan §4).
_MIN_SERIES_LEN = 8

# Quarterly period types — the FY rows are aggregates that the trigger's
# loader excludes by default (loaders.py DEFAULT_PERIOD_TYPES), so they must
# not count toward the fireable-fact floor here either.
_QUARTERLY_PERIOD_TYPES: tuple[str, ...] = ("Q1", "Q2", "Q3", "Q4")


def _open(db_path: Path) -> sqlite3.Connection | None:
    """Open the DB or return ``None`` when missing — never raises.

    Mirrors ``timeseries.loaders._open``: this is a best-effort read helper,
    so an absent file or an open error degrades to "no catalog" rather than
    surfacing to the caller.
    """
    if not db_path.exists():
        log.debug({"event": "kpi_catalog_db_missing", "path": str(db_path)})
        return None
    try:
        conn = sqlite3.connect(str(db_path), timeout=5.0)
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.Error as exc:
        log.warning({"event": "kpi_catalog_open_failed", "error": str(exc)})
        return None


def allowed_kpi_names(
    ticker: str,
    db_path: Path | str | None = None,
    *,
    min_facts: int = _MIN_SERIES_LEN,
) -> set[str]:
    """Exact ``kpi_definitions.name`` strings the auto-seeder may write for ``ticker``.

    Returns the set of names with at least ``min_facts`` quarterly
    (``fiscal_period_type IN ('Q1','Q2','Q3','Q4')``) ``kpi_facts`` rows for
    ``ticker``. The default ``min_facts=8`` mirrors
    ``triggers.kpi_inflection._MIN_SERIES_LEN`` so every returned name can
    actually produce a fireable series (plan §4).

    Names are returned as exact, case-sensitive strings because that is what
    ``timeseries.loaders.load_kpi_series`` joins on (``kd.name = ?`` at
    loaders.py:505/539). Callers must enumerate these verbatim into the LLM
    prompt and drop any proposal whose ``kpi_name`` is not in this set.

    Best-effort: a missing DB, an unopenable DB, or missing
    ``kpi_facts`` / ``kpi_definitions`` tables yield an empty set rather than
    raising — mirroring ``src/timeseries/loaders.py``.
    """
    if db_path is None:
        return set()
    resolved = Path(db_path)
    conn = _open(resolved)
    if conn is None:
        return set()
    placeholders = ",".join("?" * len(_QUARTERLY_PERIOD_TYPES))
    try:
        rows = conn.execute(
            f"""
            SELECT kd.name AS name, COUNT(*) AS n
            FROM kpi_facts kf
            JOIN kpi_definitions kd ON kd.id = kf.kpi_definition_id
            WHERE kf.ticker = ?
              AND kf.fiscal_period_type IN ({placeholders})
            GROUP BY kd.name
            HAVING COUNT(*) >= ?
            """,
            (ticker.upper(), *_QUARTERLY_PERIOD_TYPES, min_facts),
        ).fetchall()
    except sqlite3.Error as exc:
        # Missing kpi_facts / kpi_definitions table, or a schema mismatch —
        # all best-effort, like the timeseries loaders.
        log.debug(
            {
                "event": "kpi_catalog_query_failed",
                "ticker": ticker,
                "error": f"{type(exc).__name__}: {exc}",
            }
        )
        return set()
    finally:
        conn.close()
    return {cast("str", r["name"]) for r in rows if r["name"] is not None}
