"""Self-updating freshness — surface decisions whose recorded basis has been
superseded by a newer model-version.

``v_decision_freshness`` (migration 0137) is a VIEW that joins each decision to the
CURRENT latest model-version of its basis_kind+entity, so freshness is always live
— there is no stamping job to run and nothing goes stale-about-staleness. This
module is the typed read layer over that view plus the one policy helper
(``stale_material_decisions``) the standup re-review signal consumes.

``MATERIAL_DRIFT_PCT`` mirrors the 0.10 threshold baked into the view (a view
cannot read a Python constant). Change both together.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

MATERIAL_DRIFT_PCT = 0.10
BASIS_STATUSES: tuple[str, ...] = (
    "unknown",  # decision recorded no structured basis (pre-capture / owner note)
    "fresh",  # basis is the current model-version (or newer)
    "superseded_minor",  # a newer version exists but moved < MATERIAL_DRIFT_PCT
    "superseded_material",  # a newer version moved >= MATERIAL_DRIFT_PCT — re-review
)


@dataclass(frozen=True, slots=True)
class DecisionFreshness:
    """One decision's basis-vs-current comparison, decoded from the view."""

    decision_id: int
    ticker: str | None
    recommendation_kind: str | None
    decided_by: str | None
    outcome_label: str | None
    basis_kind: str | None
    basis_value: float | None
    current_value: float | None
    basis_as_of: str | None
    current_as_of: str | None
    basis_drift_pct: float | None
    valuation_superseded: bool
    basis_status: str


def _decode(row: dict[str, object]) -> DecisionFreshness:
    def _f(key: str) -> float | None:
        v = row.get(key)
        return None if v is None else float(v)  # type: ignore[arg-type]

    def _s(key: str) -> str | None:
        v = row.get(key)
        return None if v is None else str(v)

    return DecisionFreshness(
        decision_id=int(row["decision_id"]),  # type: ignore[arg-type]
        ticker=_s("ticker"),
        recommendation_kind=_s("recommendation_kind"),
        decided_by=_s("decided_by"),
        outcome_label=_s("outcome_label"),
        basis_kind=_s("basis_kind"),
        basis_value=_f("basis_value"),
        current_value=_f("current_value"),
        basis_as_of=_s("basis_as_of"),
        current_as_of=_s("current_as_of"),
        basis_drift_pct=_f("basis_drift_pct"),
        valuation_superseded=bool(row.get("valuation_superseded")),
        basis_status=str(row.get("basis_status") or "unknown"),
    )


def decision_freshness(
    conn: sqlite3.Connection,
    *,
    statuses: tuple[str, ...] | None = None,
    pending_only: bool = False,
    decided_by: str | None = None,
) -> list[DecisionFreshness]:
    """Rows of ``v_decision_freshness``, sorted by absolute drift (largest first).

    Optional filters: ``statuses`` (basis_status IN ...), ``pending_only`` (open
    outcomes only), ``decided_by``. Returns ``[]`` when the view is absent — a data
    dir from before migration 0137 — rather than raising, so callers degrade
    cleanly. Does not mutate the connection's ``row_factory``.
    """
    where: list[str] = []
    params: list[object] = []
    if statuses:
        where.append(f"basis_status IN ({','.join('?' for _ in statuses)})")
        params.extend(statuses)
    if pending_only:
        where.append("(outcome_label IS NULL OR outcome_label = 'pending')")
    if decided_by is not None:
        where.append("decided_by = ?")
        params.append(decided_by)
    sql = "SELECT * FROM v_decision_freshness"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY ABS(COALESCE(basis_drift_pct, 0)) DESC, decision_id"
    try:
        cur = conn.execute(sql, params)
    except sqlite3.OperationalError:
        return []  # view not present (pre-0137 data dir)
    cols = [c[0] for c in cur.description]
    return [_decode(dict(zip(cols, r, strict=True))) for r in cur.fetchall()]


def stale_material_decisions(
    conn: sqlite3.Connection, *, pending_only: bool = True
) -> list[DecisionFreshness]:
    """Decisions resting on a materially-superseded model-version (|drift| >=
    ``MATERIAL_DRIFT_PCT``) — the set that auto-queues a re-review. Defaults to
    still-open decisions."""
    return decision_freshness(conn, statuses=("superseded_material",), pending_only=pending_only)
