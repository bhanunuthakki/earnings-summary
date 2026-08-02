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

from dcf.latest import latest_dcf_row


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
    unsegmented latest run, read through the canonical shared reader
    (``dcf.latest.latest_dcf_row``, PR: canonical latest_dcf_run reader).
    Returns None when there is no usable run (no row, or a null fair value).
    Degrades to None on a pre-DCF schema rather than raising.
    """
    row = latest_dcf_row(conn, ticker)
    if row is None or row.npv_per_share is None:
        return None
    over_under = row.over_under_pct
    meta = None if over_under is None else json.dumps({"over_under_pct": float(over_under)})
    return Basis(
        kind="dcf",
        ref_id=row.id,
        value=float(row.npv_per_share),
        as_of=row.valuation_date,
        meta_json=meta,
    )
