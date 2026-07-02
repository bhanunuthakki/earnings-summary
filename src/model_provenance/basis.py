"""Basis resolvers — given a consumer (a decision on an entity), what current
model-version does it rest on?

A ``Basis`` is the snapshot a decision records at write time: which kind of model,
which version (ref_id), the key number (value), and as-of. Storing the snapshot —
not just a foreign key — means a decision still remembers what it stood on even
after the model row is superseded.

One resolver per basis kind. Only ``dcf_basis`` today; ``kpi_basis`` /
``thesis_basis`` / ``allocation_basis`` land with Phase 2 and register the same
shape so the write path and backfill stay kind-agnostic.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Basis:
    """The model-version a decision rests on, snapshotted at write time."""

    kind: str  # one of model_provenance.BASIS_KINDS
    ref_id: int | None  # id of the model-version row (None when unknown, e.g. backfill)
    value: float | None  # the key number (e.g. DCF fair value per share)
    as_of: str | None  # the model-version's as-of date (TEXT)
    meta_json: str | None = None  # kind-specific extras, e.g. {"over_under_pct": ...}


def dcf_basis(conn: sqlite3.Connection, ticker: str) -> Basis | None:
    """The current DCF model-version a decision on ``ticker`` rests on — the
    unsegmented latest run. Returns None when there is no usable run (no row, or a
    null fair value). Degrades to None on a pre-DCF schema rather than raising.

    ``COALESCE(is_latest, 1) = 1`` reads the current row on the versioned schema
    (0137+) and every row on a pre-0137 schema (where there is one per ticker), so
    the resolver works either way.
    """
    try:
        cur = conn.execute(
            "SELECT id, npv_per_share, valuation_date, over_under_pct FROM dcf_runs "
            "WHERE ticker = ? AND COALESCE(is_latest, 1) = 1 AND COALESCE(segment_name, '') = '' "
            "ORDER BY created_at DESC, id DESC LIMIT 1",
            (ticker.upper(),),
        )
    except sqlite3.OperationalError:
        return None
    row = cur.fetchone()
    if row is None or row[1] is None:
        return None
    over_under = row[3]
    meta = None if over_under is None else json.dumps({"over_under_pct": float(over_under)})
    return Basis(
        kind="dcf",
        ref_id=int(row[0]),
        value=float(row[1]),
        as_of=(str(row[2]) if row[2] else None),
        meta_json=meta,
    )
