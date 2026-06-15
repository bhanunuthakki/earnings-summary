"""fact_overrides resolver — the single consult point for company-doc precedence.

See ``directives/provenance_override_2026_06.md`` and the alembic ``0111`` docstring.

A ``fact_overrides`` row records that an authoritative company document (SEC 8-K /
10-Q / 10-K, earnings press release, IR deck / supplement) supersedes FMP's value
for one logical fact, keyed by ``(ticker, period_end, fiscal_period_type,
fact_kind, fact_key)``. The override is durable: it lives in the DB, OUTSIDE the
FMP cache files, so a re-fetch cannot clobber it. Every read path consults this
module so the company-published figure wins at resolve time.

``fact_key`` is the canonical within-period key per kind:

* ``financial_fact`` — the ``line_item`` string (``"revenue"``).
* ``kpi`` — the KPI definition NAME (``"Google Cloud revenue growth"``); portable
  across the per-ticker ``kpi_definitions.id``.
* ``segment`` record — the ``dim_type`` (``"product"`` / ``"geography"``); a
  ``replace`` carries the whole ``data`` dict in ``value_json`` so a re-fetched
  bad record (different segment names, spurious buckets) is fully reconstructed.
* ``segment`` cell — ``"<dim_type>|<dim_name>|<metric>"``
  (``"product|Google Cloud|revenue"``); a scalar ``replace`` or a ``drop``.

Actions: ``replace`` (override value is authoritative), ``drop`` (the FMP
record/cell is spurious — omit it), ``qualify`` (keep FMP's value but attach a
cross-source disagreement annotation; no value change).
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import cast

log = logging.getLogger(__name__)

# fact_kind values (mirror the alembic 0111 CHECK constraint).
FINANCIAL_FACT = "financial_fact"
SEGMENT = "segment"
KPI = "kpi"
FACT_KINDS: tuple[str, ...] = (FINANCIAL_FACT, SEGMENT, KPI)

STATUS_ACTIVE = "active"
STATUS_RETIRED = "retired"

DEFAULT_USER_ID = "bhanu"


class OverrideAction(StrEnum):
    """What the override does to FMP's value for the logical fact."""

    REPLACE = "replace"
    DROP = "drop"
    QUALIFY = "qualify"


@dataclass(frozen=True, slots=True)
class FactOverride:
    """One ``fact_overrides`` row, decoded (``value_json`` parsed)."""

    id: int
    user_id: str
    ticker: str
    period_end: str  # 'YYYY-MM-DD'
    fiscal_period_type: str
    fact_kind: str
    fact_key: str
    action: str
    value: float | None
    unit: str | None
    value_json: dict[str, object] | None
    source_doc_type: str
    source_accession: str | None
    source_exhibit: str | None
    source_url: str | None
    source_excerpt: str | None
    source_doc_id: int | None
    confidence: float | None
    rationale: str | None
    created_by: str
    created_at: str
    status: str


# ---------------------------------------------------------------------------
# fact_key helpers — keep every producer/consumer on the same canonical key
# ---------------------------------------------------------------------------


def segment_record_key(dim_type: str) -> str:
    """Canonical ``fact_key`` for a whole-record segment override."""
    return dim_type.lower()


def segment_cell_key(dim_type: str, dim_name: str, metric: str = "revenue") -> str:
    """Canonical ``fact_key`` for a single segment cell override."""
    return f"{dim_type.lower()}|{dim_name}|{metric}"


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    """Naive-UTC ISO stamp — the repo storage convention."""
    return datetime.now(UTC).replace(tzinfo=None).isoformat(sep=" ", timespec="seconds")


def _has_table(conn: sqlite3.Connection, table: str = "fact_overrides") -> bool:
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
    except sqlite3.Error:
        return False
    return row is not None


def _parse_value_json(raw: object) -> dict[str, object] | None:
    if raw is None:
        return None
    try:
        parsed = json.loads(str(raw))
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    if not isinstance(parsed, dict):
        return None
    return cast("dict[str, object]", parsed)


def _row_to_override(row: sqlite3.Row) -> FactOverride:
    value = row["value"]
    conf = row["confidence"]
    src_doc = row["source_doc_id"]
    return FactOverride(
        id=int(row["id"]),
        user_id=str(row["user_id"]),
        ticker=str(row["ticker"]),
        period_end=str(row["period_end"])[:10],
        fiscal_period_type=str(row["fiscal_period_type"]),
        fact_kind=str(row["fact_kind"]),
        fact_key=str(row["fact_key"]),
        action=str(row["action"]),
        value=None if value is None else float(value),
        unit=None if row["unit"] is None else str(row["unit"]),
        value_json=_parse_value_json(row["value_json"]),
        source_doc_type=str(row["source_doc_type"]),
        source_accession=None if row["source_accession"] is None else str(row["source_accession"]),
        source_exhibit=None if row["source_exhibit"] is None else str(row["source_exhibit"]),
        source_url=None if row["source_url"] is None else str(row["source_url"]),
        source_excerpt=None if row["source_excerpt"] is None else str(row["source_excerpt"]),
        source_doc_id=None if src_doc is None else int(src_doc),
        confidence=None if conf is None else float(conf),
        rationale=None if row["rationale"] is None else str(row["rationale"]),
        created_by=str(row["created_by"]),
        created_at=str(row["created_at"]),
        status=str(row["status"]),
    )


_SELECT_COLS = (
    "id, user_id, ticker, period_end, fiscal_period_type, fact_kind, fact_key, action, "
    "value, unit, value_json, source_doc_type, source_accession, source_exhibit, source_url, "
    "source_excerpt, source_doc_id, status, confidence, rationale, created_by, created_at"
)


# ---------------------------------------------------------------------------
# Write
# ---------------------------------------------------------------------------


def record_override(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    period_end: str,
    fiscal_period_type: str,
    fact_kind: str,
    fact_key: str,
    action: str | OverrideAction,
    source_doc_type: str,
    created_by: str,
    value: float | int | None = None,
    unit: str | None = None,
    value_json: dict[str, object] | None = None,
    source_accession: str | None = None,
    source_exhibit: str | None = None,
    source_url: str | None = None,
    source_excerpt: str | None = None,
    source_doc_id: int | None = None,
    confidence: float | None = 1.0,
    rationale: str | None = None,
    user_id: str = DEFAULT_USER_ID,
    observed_at: str | None = None,
) -> int:
    """Record an active override, retiring any prior active one for the same key.

    Returns the new row id. Validates ``fact_kind`` / ``action`` and the payload
    (``replace`` requires a ``value`` or ``value_json``). Retire-then-insert keeps
    the single-active partial-unique invariant intact within one transaction.
    Does NOT commit — the caller owns the transaction (so a CLI can ``--dry-run``).
    """
    action_str = str(action)
    if fact_kind not in FACT_KINDS:
        raise ValueError(f"fact_kind must be one of {FACT_KINDS}, got {fact_kind!r}")
    if action_str not in (a.value for a in OverrideAction):
        raise ValueError(
            f"action must be one of {[a.value for a in OverrideAction]}, got {action_str!r}"
        )
    if action_str == OverrideAction.REPLACE.value and value is None and value_json is None:
        raise ValueError("a 'replace' override needs a value or value_json")

    period = period_end[:10]
    stamp = observed_at or _now_iso()
    conn.execute(
        "UPDATE fact_overrides SET status = ?, retired_at = ? "
        "WHERE user_id = ? AND ticker = ? AND period_end = ? AND fiscal_period_type = ? "
        "AND fact_kind = ? AND fact_key = ? AND status = ?",
        (
            STATUS_RETIRED,
            stamp,
            user_id,
            ticker.upper(),
            period,
            fiscal_period_type,
            fact_kind,
            fact_key,
            STATUS_ACTIVE,
        ),
    )
    cur = conn.execute(
        "INSERT INTO fact_overrides "
        "(user_id, ticker, period_end, fiscal_period_type, fact_kind, fact_key, action, "
        " value, unit, value_json, source_doc_type, source_accession, source_exhibit, "
        " source_url, source_excerpt, source_doc_id, status, confidence, rationale, "
        " created_by, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            user_id,
            ticker.upper(),
            period,
            fiscal_period_type,
            fact_kind,
            fact_key,
            action_str,
            None if value is None else float(value),
            unit,
            None if value_json is None else json.dumps(value_json),
            source_doc_type,
            source_accession,
            source_exhibit,
            source_url,
            source_excerpt,
            source_doc_id,
            STATUS_ACTIVE,
            confidence,
            rationale,
            created_by,
            stamp,
        ),
    )
    return int(cur.lastrowid) if cur.lastrowid is not None else -1


def retire_override(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    period_end: str,
    fiscal_period_type: str,
    fact_kind: str,
    fact_key: str,
    user_id: str = DEFAULT_USER_ID,
    observed_at: str | None = None,
) -> bool:
    """Retire the active override for a logical key. Returns True if one was retired."""
    if not _has_table(conn):
        return False
    cur = conn.execute(
        "UPDATE fact_overrides SET status = ?, retired_at = ? "
        "WHERE user_id = ? AND ticker = ? AND period_end = ? AND fiscal_period_type = ? "
        "AND fact_kind = ? AND fact_key = ? AND status = ?",
        (
            STATUS_RETIRED,
            observed_at or _now_iso(),
            user_id,
            ticker.upper(),
            period_end[:10],
            fiscal_period_type,
            fact_kind,
            fact_key,
            STATUS_ACTIVE,
        ),
    )
    return cur.rowcount > 0


# ---------------------------------------------------------------------------
# Read / resolve
# ---------------------------------------------------------------------------


def get_active_overrides(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    fact_kind: str | None = None,
    period_end: str | None = None,
    user_id: str = DEFAULT_USER_ID,
) -> list[FactOverride]:
    """All active overrides for a ticker, optionally filtered by kind / period."""
    if not _has_table(conn):
        return []
    sql = (
        f"SELECT {_SELECT_COLS} FROM fact_overrides WHERE user_id = ? AND ticker = ? AND status = ?"
    )
    params: list[object] = [user_id, ticker.upper(), STATUS_ACTIVE]
    if fact_kind is not None:
        sql += " AND fact_kind = ?"
        params.append(fact_kind)
    if period_end is not None:
        sql += " AND period_end = ?"
        params.append(period_end[:10])
    try:
        rows = conn.execute(sql, params).fetchall()
    except sqlite3.Error:
        return []
    return [_row_to_override(r) for r in rows]


def resolve_scalar(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    period_end: str,
    fiscal_period_type: str,
    fact_kind: str,
    fact_key: str,
    user_id: str = DEFAULT_USER_ID,
) -> FactOverride | None:
    """Return the active override for one logical scalar fact, or None.

    For ``financial_fact`` and ``kpi`` kinds. The caller applies it: ``replace`` →
    substitute ``value`` / ``unit``; ``drop`` → omit the fact; ``qualify`` →
    annotate (no value change).
    """
    if not _has_table(conn):
        return None
    try:
        row = conn.execute(
            f"SELECT {_SELECT_COLS} FROM fact_overrides "
            "WHERE user_id = ? AND ticker = ? AND period_end = ? AND fiscal_period_type = ? "
            "AND fact_kind = ? AND fact_key = ? AND status = ? LIMIT 1",
            (
                user_id,
                ticker.upper(),
                period_end[:10],
                fiscal_period_type,
                fact_kind,
                fact_key,
                STATUS_ACTIVE,
            ),
        ).fetchone()
    except sqlite3.Error:
        return None
    return None if row is None else _row_to_override(row)


def overridden_segment_periods(
    conn: sqlite3.Connection, *, ticker: str, dim_type: str, user_id: str = DEFAULT_USER_ID
) -> set[str]:
    """Period_end set covered by an active segment override for ``dim_type``.

    Used by the segment-cache audit / refresh gate to treat a flagged-but-overridden
    quarter as resolved rather than a contamination FAIL.
    """
    dim = dim_type.lower()
    prefix = f"{dim}|"
    periods: set[str] = set()
    for ov in get_active_overrides(conn, ticker=ticker, fact_kind=SEGMENT, user_id=user_id):
        if ov.fact_key == dim or ov.fact_key.startswith(prefix):
            periods.add(ov.period_end)
    return periods


def apply_segment_overrides(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    dim_type: str,
    records: Iterable[dict[str, object]],
    user_id: str = DEFAULT_USER_ID,
) -> list[dict[str, object]]:
    """Return ``records`` with active segment overrides applied in-memory.

    ``records`` are raw FMP segment-cache records — each a dict with a ``date``
    (period_end), ``period``, and ``data`` (``{segment_name: value}``). For each
    record matching an override's ``(period_end, fiscal_period_type)``:

    * a record-level ``replace`` (``fact_key == dim_type``) swaps the whole ``data``
      dict for the override's ``value_json`` (the robust path for the GOOG case);
    * cell-level overrides (``fact_key == "dim_type|dim_name|metric"``) ``replace``
      one segment value or ``drop`` one segment from ``data``.

    Records are matched on BOTH ``date`` (period_end) and ``period``
    (fiscal_period_type), so a Q4 override never bleeds into the same-dated FY
    annual rollup (both carry ``date`` 2025-12-31 but ``period`` Q4 vs FY). A record
    with no ``period`` field falls back to a date-only match, applied only when a
    single override unambiguously covers that date.

    Never mutates the input records. ``qualify`` overrides are read-through (no
    value change). Returns a new list of shallow-copied records.
    """
    dim = dim_type.lower()
    overrides = [
        ov
        for ov in get_active_overrides(conn, ticker=ticker, fact_kind=SEGMENT, user_id=user_id)
        if ov.fact_key == dim or ov.fact_key.startswith(f"{dim}|")
    ]
    if not overrides:
        return [dict(rec) for rec in records]

    record_level: dict[tuple[str, str], FactOverride] = {}
    cell_level: dict[tuple[str, str], list[FactOverride]] = {}
    record_by_date: dict[str, list[FactOverride]] = {}
    cell_by_date: dict[str, list[FactOverride]] = {}
    for ov in overrides:
        key = (ov.period_end, ov.fiscal_period_type)
        if ov.fact_key == dim:
            record_level[key] = ov
            record_by_date.setdefault(ov.period_end, []).append(ov)
        else:
            cell_level.setdefault(key, []).append(ov)
            cell_by_date.setdefault(ov.period_end, []).append(ov)

    out: list[dict[str, object]] = []
    for rec in records:
        new_rec = dict(rec)
        date = str(new_rec.get("date") or "")[:10]
        period_val = new_rec.get("period")
        if period_val is not None:
            key = (date, str(period_val))
            rec_ov = record_level.get(key)
            cells = cell_level.get(key)
        else:
            # No period field — match by date only, and only when unambiguous.
            rl = record_by_date.get(date)
            rec_ov = rl[0] if rl is not None and len(rl) == 1 else None
            cells = cell_by_date.get(date)
        if rec_ov is not None and rec_ov.action == OverrideAction.REPLACE.value:
            if rec_ov.value_json is not None:
                new_rec["data"] = dict(rec_ov.value_json)
            out.append(new_rec)
            continue
        if cells:
            raw_data = new_rec.get("data")
            data: dict[str, object] = {}
            if isinstance(raw_data, dict):
                data = dict(cast("dict[str, object]", raw_data))
            for cell in cells:
                parts = cell.fact_key.split("|")
                if len(parts) < 2:
                    continue
                dim_name = parts[1]
                if cell.action == OverrideAction.DROP.value:
                    data.pop(dim_name, None)
                elif cell.action == OverrideAction.REPLACE.value and cell.value is not None:
                    data[dim_name] = cell.value
            new_rec["data"] = data
        out.append(new_rec)
    return out


# ---------------------------------------------------------------------------
# Scalar (financial_fact / kpi) series overlay — consumed by the read paths
# ---------------------------------------------------------------------------


def active_scalar_override_map(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    fact_kind: str,
    fact_key: str,
    user_id: str = DEFAULT_USER_ID,
) -> dict[tuple[str, str], FactOverride]:
    """``(period_end, fiscal_period_type) -> active replace/drop override`` for one
    logical scalar fact (a ``financial_fact`` line_item or a ``kpi`` definition name).

    ``qualify`` overrides are excluded — they annotate, they do not change a value.
    Empty when there is no override / no ``fact_overrides`` table. Series readers that
    carry a per-row ``fiscal_period_type`` look up ``(period_end, type)`` directly;
    readers that carry only ``period_end`` use :func:`date_override_map`.
    """
    out: dict[tuple[str, str], FactOverride] = {}
    for ov in get_active_overrides(conn, ticker=ticker, fact_kind=fact_kind, user_id=user_id):
        if ov.fact_key != fact_key or ov.action == OverrideAction.QUALIFY.value:
            continue
        out[(ov.period_end, ov.fiscal_period_type)] = ov
    return out


def date_override_map(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    fact_kind: str,
    fact_key: str,
    period_types: Iterable[str],
    user_id: str = DEFAULT_USER_ID,
) -> dict[str, FactOverride]:
    """``period_end -> active replace/drop override``, restricted to overrides whose
    ``fiscal_period_type`` is in ``period_types``.

    For series readers that carry only ``period_end`` (the query already scoped the
    period types). If a date carries overrides for two in-scope period types (rare:
    e.g. both Q4 and FY in scope) the last wins — readers needing exact type matching
    use :func:`active_scalar_override_map`.
    """
    allowed = set(period_types)
    out: dict[str, FactOverride] = {}
    for (period_end, ftype), ov in active_scalar_override_map(
        conn, ticker=ticker, fact_kind=fact_kind, fact_key=fact_key, user_id=user_id
    ).items():
        if ftype in allowed:
            out[period_end] = ov
    return out


# ---------------------------------------------------------------------------
# Provenance-chip surfacing (P6) — make the displayed source honest
# ---------------------------------------------------------------------------


def chip_override_map(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    fact_kind: str,
    fact_key: str,
    period_types: Iterable[str],
    user_id: str = DEFAULT_USER_ID,
) -> dict[str, FactOverride]:
    """``period_end -> active replace/qualify override`` for the provenance chip.

    Unlike :func:`date_override_map` this KEEPS ``qualify`` (the chip annotates it
    with a ⚠ note) and excludes ``drop`` (a dropped period shows no cell). Restricted
    to ``period_types``.
    """
    allowed = set(period_types)
    out: dict[str, FactOverride] = {}
    for ov in get_active_overrides(conn, ticker=ticker, fact_kind=fact_kind, user_id=user_id):
        if ov.fact_key != fact_key or ov.action == OverrideAction.DROP.value:
            continue
        if ov.fiscal_period_type in allowed:
            out[ov.period_end] = ov
    return out


def override_chip_label(ov: FactOverride) -> str:
    """Compact 'overridden by' label, e.g. ``sec_8k · 0001652044-26-000012 · ex991.htm``."""
    parts = [ov.source_doc_type]
    if ov.source_accession:
        parts.append(ov.source_accession)
    if ov.source_exhibit:
        parts.append(ov.source_exhibit)
    return " · ".join(parts)


def override_provenance(ov: FactOverride) -> dict[str, object]:
    """Cell-provenance fields describing a ``replace`` override's company-doc source.

    Overlay these onto a cell-provenance dict / ``CellSource`` so the chip shows the
    figure came from the filing (doc type, accession, URL, the asserted confidence
    and ``created_by`` audit tag), and an ``override`` label row — not the FMP row
    whose value was superseded. FMP-derivation lineage / fetched_at are cleared since
    they no longer describe the displayed value.
    """
    return {
        "source": ov.source_doc_type,
        "doc_type": ov.source_doc_type,
        "accession_number": ov.source_accession,
        "source_url": ov.source_url,
        "filing_date": None,
        "locator": ov.source_exhibit,
        "confidence": ov.confidence,
        "extracted_by": ov.created_by,
        "computed_from": None,
        "fetched_at": None,
        "source_doc_id": ov.source_doc_id,
        "override": override_chip_label(ov),
    }


def qualify_note(ov: FactOverride) -> str:
    """⚠ chip note for a ``qualify`` override (keeps FMP's value, flags it)."""
    note = f"⚠ qualified by {ov.source_doc_type}"
    if ov.source_accession:
        note += f" {ov.source_accession}"
    if ov.source_excerpt:
        note += f": {ov.source_excerpt}"
    return note
