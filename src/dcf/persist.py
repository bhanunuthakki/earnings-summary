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
import re
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal, cast
from zoneinfo import ZoneInfo

from dcf import valuation
from dcf.artifact_promotion import ArtifactPromotion
from dcf.provenance import DcfInputProvenance
from model_provenance.versioning import mark_superseded_by, supersede_current
from schema_compat import require_current_for_write


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
    provenance: DcfInputProvenance | None = None


PromotionStatus = Literal["verified", "unverified", "not_applicable", "missing"]


@dataclass(frozen=True)
class DcfPromotionDecision:
    """Typed, deterministic decision about replacing the current DCF run."""

    allowed: bool
    reason: str | None
    candidate_bridge_status: PromotionStatus
    current_bridge_status: PromotionStatus
    candidate_sanity_flag: str | None
    candidate_evidence: dict[str, object]

    def as_dict(self) -> dict[str, object]:
        return {
            "allowed": self.allowed,
            "reason": self.reason,
            "candidate_bridge_status": self.candidate_bridge_status,
            "current_bridge_status": self.current_bridge_status,
            "candidate_sanity_flag": self.candidate_sanity_flag,
            "candidate_evidence": dict(self.candidate_evidence),
        }


class DcfPromotionBlockedError(RuntimeError):
    """A candidate was retained as evidence but denied current promotion."""

    def __init__(self, decision: DcfPromotionDecision) -> None:
        self.decision = decision
        super().__init__(decision.reason or "DCF promotion blocked")


DcfPromotionBlocked = DcfPromotionBlockedError


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
_PACIFIC = ZoneInfo("America/Los_Angeles")


def _validate_input_cutoff(row: DcfRunRow) -> None:
    """Prevent source observations from looking ahead of the valuation date.

    ``valuation_date`` is the Pacific information-date. ``inputs_as_of`` is an
    absolute observation cutoff and therefore must be timezone-aware before it
    can be compared at the Pacific midnight boundary.
    """
    if row.provenance is None:
        return
    inputs_as_of = row.provenance.inputs_as_of
    if inputs_as_of.tzinfo is None or inputs_as_of.utcoffset() is None:
        raise ValueError("DCF inputs_as_of must be a timezone-aware datetime")
    if inputs_as_of.astimezone(_PACIFIC).date() > row.valuation_date:
        raise ValueError("DCF inputs_as_of is later than the Pacific valuation date")


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


def _has_provenance_columns(conn: sqlite3.Connection) -> bool:
    cols = {str(r[1]) for r in conn.execute("PRAGMA table_info(dcf_runs)")}
    return {
        "input_sha256",
        "workbook_sha256",
        "engine_version",
        "inputs_as_of",
        "provenance_json",
    }.issubset(cols)


def _has_input_ledger(conn: sqlite3.Connection) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='dcf_run_inputs'"
        ).fetchone()
        is not None
    )


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _nonempty_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def _input_ledger_rows(provenance: DcfInputProvenance) -> list[dict[str, object]]:
    """Normalize the source records carried by a DCF provenance envelope."""
    detail = provenance.detail or {}
    rows: list[dict[str, object]] = []
    raw_sources = detail.get("sources")
    if raw_sources is not None and not isinstance(raw_sources, list):
        raise ValueError("DCF provenance sources must be a list")
    sources = cast("list[object]", raw_sources or [])
    for raw_source in sources:
        if not isinstance(raw_source, dict):
            raise ValueError("each DCF provenance source must be an object")
        source = cast("dict[str, object]", raw_source)
        role = _nonempty_text(source.get("role"))
        locator = next(
            (
                value
                for key in ("path", "locator", "url", "source")
                if (value := _nonempty_text(source.get(key))) is not None
            ),
            None,
        )
        if role is None or locator is None:
            raise ValueError("each DCF provenance source needs a non-empty role and locator")
        sha256 = _nonempty_text(source.get("sha256"))
        if sha256 is not None and _SHA256_RE.fullmatch(sha256) is None:
            raise ValueError(f"invalid SHA-256 for DCF input source {role!r}")
        raw_size = source.get("bytes")
        if raw_size is not None and (
            not isinstance(raw_size, int) or isinstance(raw_size, bool) or raw_size < 0
        ):
            raise ValueError(f"invalid byte size for DCF input source {role!r}")
        rows.append(
            {
                "role": role,
                "locator": locator,
                "sha256": sha256,
                "byte_size": raw_size,
                "observed_at": _nonempty_text(source.get("observed_at")),
                "detail_json": json.dumps(
                    source,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            }
        )

    raw_market = detail.get("market_price")
    if raw_market is not None and not isinstance(raw_market, dict):
        raise ValueError("DCF market-price provenance must be an object")
    market = cast("dict[str, object]", raw_market) if isinstance(raw_market, dict) else None
    if market is not None and any(
        market.get(key) is not None for key in ("price", "observed_at", "source")
    ):
        rows.append(
            {
                "role": "market_price",
                "locator": _nonempty_text(market.get("source")) or "live_market_price",
                "sha256": None,
                "byte_size": None,
                "observed_at": _nonempty_text(market.get("observed_at")),
                "detail_json": json.dumps(
                    market,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            }
        )
    return rows


def _persist_input_ledger(
    conn: sqlite3.Connection,
    *,
    dcf_run_id: int,
    rows: list[dict[str, object]],
) -> None:
    if not rows:
        return
    conn.executemany(
        """
        INSERT INTO dcf_run_inputs
            (dcf_run_id, role, locator, sha256, byte_size, observed_at, detail_json)
        VALUES
            (:dcf_run_id, :role, :locator, :sha256, :byte_size, :observed_at, :detail_json)
        """,
        [{"dcf_run_id": dcf_run_id, **source} for source in rows],
    )


def _same_current_version(
    conn: sqlite3.Connection,
    row: DcfRunRow,
    params: dict[str, object],
) -> bool:
    """Whether the current row already represents this exact input set."""
    if row.provenance is None:
        return False
    current = conn.execute(
        """
        SELECT valuation_date, horizon_years, wacc, npv, npv_per_share,
               shares_outstanding, currency, notes, run_id, live_price,
               live_price_at, over_under_pct, mos_bar_used,
               assumption_snapshot_json, input_sha256, workbook_sha256,
               engine_version, inputs_as_of, provenance_json
        FROM dcf_runs
        WHERE ticker = ? AND COALESCE(segment_name, '') = '' AND is_latest = 1
        LIMIT 1
        """,
        (row.ticker.upper(),),
    ).fetchone()
    if current is None:
        return False
    expected = (
        params["valuation_date"],
        params["horizon_years"],
        params["wacc"],
        params["npv"],
        params["npv_per_share"],
        params["shares_outstanding"],
        params["currency"],
        params["notes"],
        params["run_id"],
        params["live_price"],
        params["live_price_at"],
        params["over_under_pct"],
        params["mos_bar_used"],
        params["assumption_snapshot_json"],
        row.provenance.input_sha256,
        row.provenance.workbook_sha256,
        row.provenance.engine_version,
        row.provenance.inputs_as_of_iso(),
        row.provenance.as_json(),
    )
    return tuple(current) == expected


_BRIDGE_STRENGTH: dict[PromotionStatus, int] = {
    "missing": 0,
    "not_applicable": 0,
    "unverified": 1,
    "verified": 2,
}


def _bridge_status(value: object) -> PromotionStatus:
    if value in {"verified", "unverified"}:
        return cast("PromotionStatus", value)
    return "missing"


def _receipt_status(provenance_json: object) -> PromotionStatus:
    if not isinstance(provenance_json, str) or not provenance_json.strip():
        return "missing"
    try:
        raw: object = json.loads(provenance_json)
    except json.JSONDecodeError:
        return "missing"
    if not isinstance(raw, dict):
        return "missing"
    receipt = cast("dict[str, object]", raw).get("equity_bridge_receipt")
    if not isinstance(receipt, dict):
        return "missing"
    return _bridge_status(cast("dict[str, object]", receipt).get("status"))


def _current_promotion_state(
    conn: sqlite3.Connection, ticker: str
) -> tuple[PromotionStatus, str | None] | None:
    columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(dcf_runs)")}
    if "ticker" not in columns:
        return None
    latest = " AND COALESCE(is_latest, 1) = 1" if "is_latest" in columns else ""
    segment = " AND COALESCE(segment_name, '') = ''" if "segment_name" in columns else ""
    sanity = "sanity_flag" if "sanity_flag" in columns else "NULL AS sanity_flag"
    try:
        current = conn.execute(
            f"SELECT provenance_json, {sanity} FROM dcf_runs "
            "WHERE UPPER(ticker) = UPPER(?)" + latest + segment + " ORDER BY id DESC LIMIT 1",
            (ticker.upper(),),
        ).fetchone()
    except sqlite3.Error:
        return None
    if current is None:
        return None
    raw_flag = current[1]
    return _receipt_status(current[0]), (str(raw_flag) if isinstance(raw_flag, str) else None)


def promotion_decision(conn: sqlite3.Connection, row: DcfRunRow) -> DcfPromotionDecision:
    """Evaluate whether ``row`` may become the current run.

    Outliers require an explicit owner-review contract. No such contract is
    inferred here: the existing DCF refresh/apply paths provide none, so the
    candidate is denied and returned to the caller as evidence. A candidate
    with a weaker equity-bridge receipt also cannot replace a stronger current
    receipt. This function is read-only and safe to run before file replacement.
    """
    candidate_status = (
        _receipt_status(row.provenance.as_json()) if row.provenance is not None else "missing"
    )
    candidate_sanity = derive_sanity_flag(derive_over_under(row.live_price, row.npv_per_share))
    current_state = _current_promotion_state(conn, row.ticker)
    current_status = current_state[0] if current_state is not None else "missing"
    evidence: dict[str, object] = {
        "ticker": row.ticker.upper(),
        "engine_version": (row.provenance.engine_version if row.provenance is not None else None),
        "input_sha256": row.provenance.input_sha256 if row.provenance is not None else None,
        "workbook_sha256": (row.provenance.workbook_sha256 if row.provenance is not None else None),
        "inputs_as_of": (row.provenance.inputs_as_of_iso() if row.provenance is not None else None),
        "equity_bridge_status": candidate_status,
        "sanity_flag": candidate_sanity,
    }
    if candidate_sanity == "outlier":
        return DcfPromotionDecision(
            allowed=False,
            reason="outlier_requires_explicit_owner_review",
            candidate_bridge_status=candidate_status,
            current_bridge_status=current_status,
            candidate_sanity_flag=candidate_sanity,
            candidate_evidence=evidence,
        )
    if (
        current_state is not None
        and _BRIDGE_STRENGTH[candidate_status] < _BRIDGE_STRENGTH[current_status]
    ):
        return DcfPromotionDecision(
            allowed=False,
            reason="candidate_equity_bridge_weaker_than_current",
            candidate_bridge_status=candidate_status,
            current_bridge_status=current_status,
            candidate_sanity_flag=candidate_sanity,
            candidate_evidence=evidence,
        )
    return DcfPromotionDecision(
        allowed=True,
        reason=None,
        candidate_bridge_status=candidate_status,
        current_bridge_status=current_status,
        candidate_sanity_flag=candidate_sanity,
        candidate_evidence=evidence,
    )


def check_promotion(conn: sqlite3.Connection, row: DcfRunRow) -> DcfPromotionDecision:
    """Public read-only preflight used before swapping files or mirrors."""
    return promotion_decision(conn, row)


def upsert(
    conn: sqlite3.Connection,
    row: DcfRunRow,
    *,
    artifact_promotion: ArtifactPromotion | None = None,
) -> bool:
    """Persist a new dcf_runs version for ``row.ticker``.

    On the versioned schema (migration 0137+) the prior current run for the ticker
    (unsegmented) is superseded — flipped to is_latest=0 with a superseded_at stamp
    and a back-link to the new row — and the new run is inserted as is_latest=1, so
    valuation history is preserved. On a pre-0137 schema it falls back to the legacy
    INSERT-OR-REPLACE keyed on ticker.

    Returns ``True`` when a version is written and ``False`` when the current
    version already has the exact provenance and calculated values.

    A row carrying sync fields against a pre-0091 schema raises (loud, with
    the fix) rather than dropping them — a silently-unpersisted sync status
    would defeat the whole point of recording it. Rows WITHOUT sync fields
    (the bespoke archetype builders) keep working on either schema.
    """
    _validate_input_cutoff(row)
    has_sync = _has_sync_columns(conn)
    if (row.assumptions_sync_status or row.assumptions_synced_at) and not has_sync:
        raise sqlite3.OperationalError(
            "dcf_runs is missing assumptions_sync_status/assumptions_synced_at — "
            "run `alembic upgrade head` (migration 0091) before refreshing"
        )
    has_provenance = _has_provenance_columns(conn)
    if row.provenance is not None:
        require_current_for_write(conn)
    if row.provenance is not None and not has_provenance:
        raise sqlite3.OperationalError(
            "dcf_runs is missing DCF provenance columns — run `alembic upgrade head` before refreshing"
        )
    input_ledger_rows = (
        _input_ledger_rows(row.provenance)
        if row.provenance is not None and _has_input_ledger(conn)
        else []
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
    if has_provenance and row.provenance is not None:
        params.update(
            {
                "input_sha256": row.provenance.input_sha256,
                "workbook_sha256": row.provenance.workbook_sha256,
                "engine_version": row.provenance.engine_version,
                "inputs_as_of": row.provenance.inputs_as_of_iso(),
                "provenance_json": row.provenance.as_json(),
            }
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
    provenance_cols = (
        ", input_sha256, workbook_sha256, engine_version, inputs_as_of, provenance_json"
        if has_provenance
        else ""
    )
    provenance_vals = (
        ", :input_sha256, :workbook_sha256, :engine_version, :inputs_as_of, :provenance_json"
        if has_provenance
        else ""
    )
    if has_provenance and row.provenance is None:
        params.update(
            {
                "input_sha256": None,
                "workbook_sha256": None,
                "engine_version": None,
                "inputs_as_of": None,
                "provenance_json": None,
            }
        )

    if (
        _has_versioning_columns(conn)
        and has_provenance
        and _same_current_version(conn, row, params)
    ):
        return False

    decision = promotion_decision(conn, row)
    if not decision.allowed:
        raise DcfPromotionBlockedError(decision)

    # A SAVEPOINT opened as the outermost transaction is committed by RELEASE
    # in SQLite. Start an explicit transaction so the workbook swap remains
    # reversible until the following connection commit succeeds.
    if not conn.in_transaction:
        conn.execute("BEGIN")
    conn.execute("SAVEPOINT dcf_run_upsert")
    artifact_applied = False
    try:
        if _has_versioning_columns(conn):
            # Supersede the prior current run for this ticker (unsegmented — the
            # Phase 3 write leaves segment_name NULL), then insert its successor.
            superseded = supersede_current(
                conn,
                table="dcf_runs",
                entity_where="ticker = :ticker AND COALESCE(segment_name, '') = ''",
                entity_params={"ticker": params["ticker"]},
            )
            cur = conn.execute(
                f"INSERT INTO dcf_runs "
                f"({base_cols}, is_latest{sync_cols}{sanity_cols}{provenance_cols}) "
                f"VALUES ({base_vals}, 1{sync_vals}{sanity_vals}{provenance_vals})",
                params,
            )
            new_id = int(cur.lastrowid or 0)
            mark_superseded_by(conn, table="dcf_runs", superseded_ids=superseded, new_id=new_id)
        else:
            cur = conn.execute(
                f"INSERT OR REPLACE INTO dcf_runs "
                f"({base_cols}{sync_cols}{sanity_cols}{provenance_cols}) "
                f"VALUES ({base_vals}{sync_vals}{sanity_vals}{provenance_vals})",
                params,
            )
            new_id = int(cur.lastrowid or 0)
        _persist_input_ledger(conn, dcf_run_id=new_id, rows=input_ledger_rows)
        if artifact_promotion is not None:
            artifact_promotion.apply()
            artifact_applied = True
        conn.execute("RELEASE SAVEPOINT dcf_run_upsert")
        conn.commit()
    except Exception:
        if conn.in_transaction:
            try:
                conn.execute("ROLLBACK TO SAVEPOINT dcf_run_upsert")
                conn.execute("RELEASE SAVEPOINT dcf_run_upsert")
            except sqlite3.Error:
                conn.rollback()
        if artifact_applied and artifact_promotion is not None:
            artifact_promotion.rollback()
        raise
    if artifact_applied and artifact_promotion is not None:
        artifact_promotion.finalize()
    return True


def build_assumption_snapshot(
    fcf_stream: list[float],
    forecast_years: list[int],
    wacc: float,
    terminal_multiple: float,
    diluted_shares_M: float,  # noqa: N803 - serialized schema and keyword API use _M units
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
