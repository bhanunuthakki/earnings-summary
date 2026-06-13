"""Read/edit API for ``discovery_sources`` — the weighted source registry.

The roster the weighted scorer reads (``scoring.py``) and the panel edits: one
row per signal SOURCE (each factor screen, each adjacency channel, each
rostered 13F investor) with an editable ``base_weight``, a ``cik`` (investors),
the ``style_tags`` the new-vs-add asymmetry reads, and an ``active`` flag.

The roster is GLOBAL, not tenant-scoped — in a single-user tool the source
weights aren't per-user (the candidates and signals they produce are). Reads
degrade to empty on a missing DB / pre-0096 schema so a caller on an old DB
falls back to default weights rather than crashing.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from user_state._db import now_iso, open_conn

#: The weight used when a source isn't in the roster (pre-seed / new screen).
DEFAULT_WEIGHT: float = 1.0


@dataclass(slots=True)
class SourceRow:
    """One ``discovery_sources`` row, decoded (``style_tags`` split to a tuple,
    ``active`` to a bool)."""

    source_key: str
    signal_class: str
    display_name: str
    base_weight: float
    tier: str
    style_tags: tuple[str, ...]
    cik: str | None
    active: bool
    last_calibrated_at: str | None
    created_at: str
    updated_at: str


def _split_tags(raw: object) -> tuple[str, ...]:
    if not isinstance(raw, str) or not raw.strip():
        return ()
    return tuple(t.strip() for t in raw.split(",") if t.strip())


def _row_to_dc(row: sqlite3.Row) -> SourceRow:
    return SourceRow(
        source_key=str(row["source_key"]),
        signal_class=str(row["signal_class"]),
        display_name=str(row["display_name"]),
        base_weight=float(row["base_weight"]),
        tier=str(row["tier"]),
        style_tags=_split_tags(row["style_tags"]),
        cik=None if row["cik"] is None else str(row["cik"]),
        active=bool(row["active"]),
        last_calibrated_at=(
            None if row["last_calibrated_at"] is None else str(row["last_calibrated_at"])
        ),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def list_sources(
    *,
    signal_class: str | None = None,
    active_only: bool = False,
    db_path: Path | str | None = None,
) -> list[SourceRow]:
    """The roster, ordered by class then weight (DESC). Degrades to ``[]`` on a
    missing DB / pre-0096 schema."""
    clauses: list[str] = []
    params: list[object] = []
    if signal_class is not None:
        clauses.append("signal_class = ?")
        params.append(signal_class)
    if active_only:
        clauses.append("active = 1")
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    try:
        conn = open_conn(db_path)
    except (sqlite3.Error, FileNotFoundError, RuntimeError):
        return []
    try:
        rows = conn.execute(
            "SELECT * FROM discovery_sources"
            + where
            + " ORDER BY signal_class, base_weight DESC, source_key",
            params,
        ).fetchall()
        return [_row_to_dc(r) for r in rows]
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def load_source_map(*, db_path: Path | str | None = None) -> dict[str, SourceRow]:
    """``{source_key: SourceRow}`` for weight resolution. Empty on missing
    schema (callers fall back to ``DEFAULT_WEIGHT``)."""
    return {s.source_key: s for s in list_sources(db_path=db_path)}


def weight_for(source_map: dict[str, SourceRow], source_key: str) -> float:
    """The resolved ``base_weight`` for a source, or ``DEFAULT_WEIGHT`` when the
    roster doesn't know it (a name still scores, at unit weight)."""
    row = source_map.get(source_key)
    return row.base_weight if row is not None else DEFAULT_WEIGHT


def get_source(source_key: str, *, db_path: Path | str | None = None) -> SourceRow | None:
    try:
        conn = open_conn(db_path)
    except (sqlite3.Error, FileNotFoundError, RuntimeError):
        return None
    try:
        row = conn.execute(
            "SELECT * FROM discovery_sources WHERE source_key = ?", (source_key,)
        ).fetchone()
        return None if row is None else _row_to_dc(row)
    except sqlite3.Error:
        return None
    finally:
        conn.close()


def _update(
    source_key: str, assignments: str, params: list[object], *, db_path: Path | str | None
) -> SourceRow | None:
    """Apply a SET clause to one row + bump ``updated_at``; return the fresh row
    or None when the key is unknown."""
    conn = open_conn(db_path)
    try:
        cur = conn.execute(
            f"UPDATE discovery_sources SET {assignments}, updated_at = ? WHERE source_key = ?",
            [*params, now_iso(), source_key],
        )
        if cur.rowcount == 0:
            return None
        conn.commit()
        row = conn.execute(
            "SELECT * FROM discovery_sources WHERE source_key = ?", (source_key,)
        ).fetchone()
        return None if row is None else _row_to_dc(row)
    finally:
        conn.close()


def set_source_weight(
    source_key: str, base_weight: float, *, db_path: Path | str | None = None
) -> SourceRow | None:
    """Edit a source's weight (the panel's weight-edit surface + quarterly
    recalibration). Clamps negatives to 0 (the CHECK forbids them anyway)."""
    weight = max(float(base_weight), 0.0)
    return _update(
        source_key,
        "base_weight = ?, last_calibrated_at = ?",
        [weight, now_iso()],
        db_path=db_path,
    )


def set_source_active(
    source_key: str, active: bool, *, db_path: Path | str | None = None
) -> SourceRow | None:
    """Toggle a source on/off in the scorer + miner."""
    return _update(source_key, "active = ?", [1 if active else 0], db_path=db_path)


def set_source_cik(
    source_key: str, cik: str | None, *, db_path: Path | str | None = None
) -> SourceRow | None:
    """Set/clear a manager's CIK (the 13F miner resolves these; the owner can
    paste one). Normalizes to 10-digit zero-padded, or None to clear."""
    normalized: str | None = None
    if cik is not None and str(cik).strip():
        digits = "".join(ch for ch in str(cik) if ch.isdigit())
        normalized = f"{int(digits):010d}" if digits else None
    return _update(source_key, "cik = ?", [normalized], db_path=db_path)
