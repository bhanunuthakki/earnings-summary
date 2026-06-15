"""Read FMP segment caches with active ``fact_overrides`` applied.

The single seam every consumer of the raw FMP segment caches routes through so a
company-doc override (e.g. the GOOG Q4'25 8-K figures) wins regardless of the
cache's contents. Without this, a ``save_fmp_data`` / ``fetch_fmp_historical_data``
re-fetch that re-introduces FMP's contaminated segmentation would silently reach
the direct-JSON DCF readers (``execution/build_redesigned_dcf.py``,
``dcf_opus_assumptions.py``, ``start_diligence.py``) and the DB-ingest gate.

Consumers:
  * ``compute.segments.extract_segment_facts`` — the DB-ingest path (passes its conn).
  * ``pipeline.segment_cache_audit.audit_ticker_cache`` — the offline / refresh-gate
    audit (an overridden record reconciles, so it is no longer flagged).
  * the three direct-JSON DCF readers (best-effort default-DB open).

See ``directives/provenance_override_2026_06.md``.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterable
from typing import cast

from provenance.overrides import apply_segment_overrides

# FMP segment-cache file suffix -> canonical override ``dim_type``. Mirrors
# ``pipeline.segment_cache_audit.SEGMENT_SUFFIXES`` plus the annual rollups.
SUFFIX_TO_DIM_TYPE: dict[str, str] = {
    "product_segments_quarterly": "product",
    "product_segments_annual": "product",
    "geo_segments_quarterly": "geography",
    "geo_segments_annual": "geography",
}


def dim_type_for_suffix(suffix: str) -> str | None:
    """Canonical override ``dim_type`` for an FMP segment-cache file suffix, or None."""
    return SUFFIX_TO_DIM_TYPE.get(suffix)


def _default_db_path() -> str:
    """``<repo_root>/data/portfolio.db`` — mirrors ``db.DB_PATH`` without importing
    ``db`` (whose module import runs ``init_db()`` as a side effect)."""
    repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return os.path.join(repo_root, "data", "portfolio.db")


def apply_overrides(
    records: Iterable[object],
    *,
    ticker: str,
    dim_type: str,
    conn: sqlite3.Connection | None = None,
    db_path: str | None = None,
) -> list[dict[str, object]]:
    """Return segment-cache records with active ``fact_overrides`` applied (best-effort).

    Uses the provided ``conn``; otherwise opens the default portfolio DB. If no DB /
    no ``fact_overrides`` table / no matching override exists, returns the records
    unchanged (shallow copies) — so a caller running without a DB (or from a worktree
    whose ``data/`` lives in the main repo) degrades to raw FMP data exactly as today.
    Never raises on a DB problem.
    """
    recs: list[dict[str, object]] = [
        cast("dict[str, object]", r) for r in records if isinstance(r, dict)
    ]
    if conn is not None:
        return apply_segment_overrides(conn, ticker=ticker, dim_type=dim_type, records=recs)
    path = db_path or _default_db_path()
    if not os.path.exists(path):
        return recs
    try:
        own = sqlite3.connect(path)
        own.row_factory = sqlite3.Row
    except sqlite3.Error:
        return recs
    try:
        return apply_segment_overrides(own, ticker=ticker, dim_type=dim_type, records=recs)
    except sqlite3.Error:
        return recs
    finally:
        own.close()
