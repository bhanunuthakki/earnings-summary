"""Upsert a DCF run into the `dcf_runs` table.

The table predates the new DCF subsystem (migration 0013) so we coexist
with the legacy columns: only the fields that map to the new flow are
populated. Legacy columns (base_revenue, revenue_growths_json, fcf_margin,
breakdown_json, segment_name) stay NULL on a Phase 3 write.

Versioning (migration 0137): dcf_runs no longer overwrites. A new run supersedes
the prior current run for the same ticker (+segment) — the old row is kept with
is_latest=0 / superseded_at, and the new row lands as is_latest=1 — so valuation
history survives (a superseded model can be diffed, and any decision resting on it
flagged stale). On a pre-0137 schema this falls back to the legacy INSERT OR REPLACE
keyed on the old UNIQUE(ticker) index (migration 0018).

Audit columns from migration 0024 (live_price, live_price_at,
over_under_pct, mos_bar_used, assumption_snapshot_json) are populated;
they're the whole point of this write.

over_under_pct is NOT a caller-supplied field: it is derived here, at the
single write chokepoint, from live_price + npv_per_share (see
derive_over_under). Four bespoke builders once hand-rolled it as percent
UPSIDE — wrong sign and scale, fixed in #368 — so writers no longer get to
supply their own value, and migration 0076 adds a DB CHECK enforcing the
same self-consistency on any raw write.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime

from dcf import valuation
from model_provenance.versioning import mark_superseded_by, supersede_current


@dataclass(frozen=True)
class DcfRunRow:
    """Fields the Phase 3 refresh writes to dcf_runs.

    over_under_pct is deliberately absent — upsert() derives it from
    live_price + npv_per_share so no caller can persist a value that
    violates the documented ratio convention.

    assumptions_sync_status / assumptions_synced_at (migration 0091) record
    the workbook→assumptions-JSON sync outcome ('synced' / 'created' /
    'failed: <detail>', naive-UTC stamp). Only the redesign refresh sets them;
    the bespoke archetype builders (bank/holdco/fintech/platform) leave them
    None, which persists as NULL = "no sync ran".
    """

    ticker: str
    valuation_date: date
    horizon_years: int
    wacc: float
    npv: float  # enterprise value, USD millions
    npv_per_share: float  # USD
    shares_outstanding: float  # absolute count, not millions
    currency: str
    live_price: float | None
    live_price_at: datetime | None
    mos_bar_used: float | None
    assumption_snapshot_json: str
    notes: str | None = None
    run_id: str | None = None
    assumptions_sync_status: str | None = None
    assumptions_synced_at: datetime | None = None


def derive_over_under(live_price: float | None, npv_per_share: float) -> float | None:
    """(live - fair) / fair as a decimal ratio (the migration-0024 convention),
    or None when undefined: no live price, or a non-positive fair value (the
    #291 guard). The only producer of dcf_runs.over_under_pct values."""
    if live_price is None or live_price <= 0 or npv_per_share <= 0:
        return None
    return valuation.over_under_pct(live_price, npv_per_share)


# A fair value more than 60% away from the live price is more likely a broken model
# (stale assumptions, unit/FX defect) than a real mispricing — the 2026-07-19 review
# found 24 such rows feeding dashboards and LLM lenses unflagged. Past this limit the
# row is stamped 'outlier' (migration 0182): surfaces badge it "unreviewed model",
# lenses withhold the fair value. The row still persists — flagged, never dropped.
SANITY_OVER_UNDER_LIMIT = 0.6


def derive_sanity_flag(over_under_pct: float | None) -> str | None:
    """'outlier' when |over_under| exceeds SANITY_OVER_UNDER_LIMIT, else None.

    Derived at the same single write chokepoint as over_under_pct itself, so no
    writer can persist an extreme valuation unflagged.
    """
    if over_under_pct is not None and abs(over_under_pct) > SANITY_OVER_UNDER_LIMIT:
        return "outlier"
    return None


def _has_sync_columns(conn: sqlite3.Connection) -> bool:
    cols = {str(r[1]) for r in conn.execute("PRAGMA table_info(dcf_runs)")}
    return "assumptions_sync_status" in cols and "assumptions_synced_at" in cols


def _has_versioning_columns(conn: sqlite3.Connection) -> bool:
    cols = {str(r[1]) for r in conn.execute("PRAGMA table_info(dcf_runs)")}
    return "is_latest" in cols


def _has_sanity_column(conn: sqlite3.Connection) -> bool:
    cols = {str(r[1]) for r in conn.execute("PRAGMA table_info(dcf_runs)")}
    return "sanity_flag" in cols


def upsert(conn: sqlite3.Connection, row: DcfRunRow) -> None:
    """Persist a new dcf_runs version for ``row.ticker``.

    On the versioned schema (migration 0137+) the prior current run for the ticker
    (unsegmented) is superseded — flipped to is_latest=0 with a superseded_at stamp
    and a back-link to the new row — and the new run is inserted as is_latest=1, so
    valuation history is preserved. On a pre-0137 schema it falls back to the legacy
    INSERT-OR-REPLACE keyed on ticker.

    A row carrying sync fields against a pre-0091 schema raises (loud, with
    the fix) rather than dropping them — a silently-unpersisted sync status
    would defeat the whole point of recording it. Rows WITHOUT sync fields
    (the bespoke archetype builders) keep working on either schema.
    """
    has_sync = _has_sync_columns(conn)
    if (row.assumptions_sync_status or row.assumptions_synced_at) and not has_sync:
        raise sqlite3.OperationalError(
            "dcf_runs is missing assumptions_sync_status/assumptions_synced_at — "
            "run `alembic upgrade head` (migration 0091) before refreshing"
        )
    sync_cols = ", assumptions_sync_status, assumptions_synced_at" if has_sync else ""
    sync_vals = ", :assumptions_sync_status, :assumptions_synced_at" if has_sync else ""
    has_sanity = _has_sanity_column(conn)
    sanity_cols = ", sanity_flag" if has_sanity else ""
    sanity_vals = ", :sanity_flag" if has_sanity else ""
    params: dict[str, object] = {
        "ticker": row.ticker.upper(),
        "valuation_date": row.valuation_date.isoformat(),
        "horizon_years": row.horizon_years,
        "wacc": row.wacc,
        "npv": row.npv,
        "npv_per_share": row.npv_per_share,
        "shares_outstanding": row.shares_outstanding,
        "currency": row.currency,
        "notes": row.notes,
        "run_id": row.run_id,
        "live_price": row.live_price,
        "live_price_at": row.live_price_at.isoformat() if row.live_price_at else None,
        "over_under_pct": derive_over_under(row.live_price, row.npv_per_share),
        "mos_bar_used": row.mos_bar_used,
        "assumption_snapshot_json": row.assumption_snapshot_json,
    }
    if has_sanity:
        params["sanity_flag"] = derive_sanity_flag(
            derive_over_under(row.live_price, row.npv_per_share)
        )
    if has_sync:
        params["assumptions_sync_status"] = row.assumptions_sync_status
        params["assumptions_synced_at"] = (
            row.assumptions_synced_at.isoformat() if row.assumptions_synced_at else None
        )

    base_cols = (
        "ticker, valuation_date, horizon_years, wacc, terminal_growth, "
        "npv, npv_per_share, shares_outstanding, currency, notes, run_id, "
        "live_price, live_price_at, over_under_pct, mos_bar_used, "
        "assumption_snapshot_json, revenue_growths_json, fcf_margin"
    )
    base_vals = (
        ":ticker, :valuation_date, :horizon_years, :wacc, 0, "
        ":npv, :npv_per_share, :shares_outstanding, :currency, :notes, :run_id, "
        ":live_price, :live_price_at, :over_under_pct, :mos_bar_used, "
        ":assumption_snapshot_json, '[]', 0"
    )

    if _has_versioning_columns(conn):
        # Supersede the prior current run for this ticker (unsegmented — the Phase 3
        # write leaves segment_name NULL), then insert the new run as is_latest=1.
        superseded = supersede_current(
            conn,
            table="dcf_runs",
            entity_where="ticker = :ticker AND COALESCE(segment_name, '') = ''",
            entity_params={"ticker": params["ticker"]},
        )
        cur = conn.execute(
            f"INSERT INTO dcf_runs ({base_cols}, is_latest{sync_cols}{sanity_cols}) "
            f"VALUES ({base_vals}, 1{sync_vals}{sanity_vals})",
            params,
        )
        mark_superseded_by(
            conn, table="dcf_runs", superseded_ids=superseded, new_id=int(cur.lastrowid or 0)
        )
    else:
        conn.execute(
            f"INSERT OR REPLACE INTO dcf_runs ({base_cols}{sync_cols}{sanity_cols}) "
            f"VALUES ({base_vals}{sync_vals}{sanity_vals})",
            params,
        )
    conn.commit()


def build_assumption_snapshot(
    fcf_stream: list[float],
    forecast_years: list[int],
    wacc: float,
    terminal_multiple: float,
    diluted_shares_M: float,
    workbook_path: str,
    pv_fcf_stream: float,
    pv_terminal: float,
) -> str:
    """Serialize the inputs that fed the PV calc into a JSON string.

    Stored verbatim in dcf_runs.assumption_snapshot_json so successive
    refreshes can be diffed to see what changed between quarters.
    """
    payload = {
        "workbook_path": workbook_path,
        "wacc": wacc,
        "terminal_multiple": terminal_multiple,
        "diluted_shares_M": diluted_shares_M,
        "forecast_years": forecast_years,
        "fcf_stream_M": fcf_stream,
        "pv_fcf_stream_M": pv_fcf_stream,
        "pv_terminal_M": pv_terminal,
    }
    return json.dumps(payload, indent=2)
