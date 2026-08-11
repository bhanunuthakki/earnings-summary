"""Phase-1 Wave-3 DCF dry-run artifact (MUTATING, behind the higher-bar gate).

A DCF fair value is fixed between fundamentals updates, so a "what-if" assumption
tweak is recomputed UPSTREAM by the DCF engine (that recompute IS the oracle) and
the resulting ``DcfRunRow`` is held as an inert ``kind='dcf'`` research_proposal --
NEVER upserted into the live ``dcf_runs`` survivor row until approve. On approve,
behind the W3-1 gate, the proposed row is reconstructed and upserted (INSERT OR
REPLACE on ticker, the intended live mutation).

``oracle_ok`` is set at draft time iff the proposed ``npv_per_share`` is a valid
positive fair value (the #291 no-fair-value guard); the gate additionally requires
the evidence doorway + adversarial survival. The apply re-checks the fair value
before writing, so a corrupted payload can never upsert a non-positive fair value.

Dependency-injected (``create_fn`` / ``get_fn`` / ``persist_fn``) so the primitives
are unit-testable without the DCF engine or a DB.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, cast

from research.apply import register_mutating_applier
from research.proposals import create_proposal, get_proposal
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite

# The DcfRunRow fields we round-trip through artifact_json to rebuild it on approve.
_ROW_KEYS: tuple[str, ...] = (
    "ticker",
    "valuation_date",
    "horizon_years",
    "wacc",
    "npv",
    "npv_per_share",
    "shares_outstanding",
    "currency",
    "live_price",
    "live_price_at",
    "mos_bar_used",
    "assumption_snapshot_json",
    "notes",
    "run_id",
    "provenance",
)


def _as_float(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) and value.strip():
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _jsonable(value: object) -> object:
    """Coerce date/datetime to ISO strings so the proposed row survives json.dumps."""
    if is_dataclass(value) and not isinstance(value, type):
        return _jsonable(cast(object, asdict(value)))
    if isinstance(value, dict):
        mapping = cast("dict[object, object]", value)
        return {str(key): _jsonable(item) for key, item in mapping.items()}
    if isinstance(value, (list, tuple)):
        items = cast("list[object] | tuple[object, ...]", value)
        return [_jsonable(item) for item in items]
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


def draft_dcf_proposal(
    *,
    ticker: str,
    proposed_row: dict[str, object],
    old_npv_per_share: float | None = None,
    title: str | None = None,
    evidence_json: str = "[]",
    adversarial_verdict: str | None = None,
    note_id: int | None = None,
    task_id: int | None = None,
    db_path: Path | str | None = None,
    repo_root: Path | None = None,
    create_fn: Callable[..., int] = create_proposal,
) -> int | None:
    """Persist an inert ``kind='dcf'`` proposal carrying a pre-recomputed DcfRunRow.

    ``proposed_row`` is the DcfRunRow (as a JSON-able dict) the DCF engine already
    recomputed under the tweaked assumptions -- this module never runs the engine.
    Returns None if the recompute produced no valid (positive) fair value.
    """
    ticker = (ticker or "").strip().upper()
    proposed_npv = _as_float(proposed_row.get("npv_per_share"))
    if not ticker or proposed_npv is None or proposed_npv <= 0:
        return None  # #291: no valid fair value -> nothing to propose
    from dcf.provenance import provenance_from_payload, verify_effective_provenance

    proposed_ticker = str(proposed_row.get("ticker") or "").strip().upper()
    if proposed_ticker != ticker:
        raise ValueError("DCF proposed row ticker does not match proposal ticker")
    provenance = provenance_from_payload(proposed_row.get("provenance"))
    verify_effective_provenance(
        provenance,
        ticker=ticker,
        repo_root=(repo_root or Path(__file__).resolve().parents[2]),
        assumption_snapshot_json=str(proposed_row.get("assumption_snapshot_json") or "{}"),
    )
    old = _as_float(old_npv_per_share)
    tail = ""
    if old and old > 0:
        tail = f" (was ${old:,.2f}, {(proposed_npv - old) / old * 100.0:+.1f}%)"
    body = (
        f"Proposed DCF: fair value ${proposed_npv:,.2f}/sh{tail}. "
        "Recomputed in Python from the tweaked assumptions; approve to make it live."
    )
    artifact = {
        "proposed_row": {
            **{k: _jsonable(proposed_row.get(k)) for k in _ROW_KEYS},
            "provenance": _jsonable(provenance),
        },
        "old_npv_per_share": old,
        "oracle_ok": True,
    }
    return int(
        create_fn(
            task_id=task_id,
            kind="dcf",
            ticker=ticker,
            title=(title or f"DCF revision: {ticker}")[:200],
            body_md=body,
            evidence_json=evidence_json,
            source_note_ids=json.dumps([note_id] if note_id is not None else []),
            artifact_json=json.dumps(artifact),
            adversarial_verdict=adversarial_verdict,
            provenance="derived",
            db_path=db_path,
        )
    )


def _reconstruct_row(row: dict[str, object]) -> Any:
    """Rebuild a ``dcf.persist.DcfRunRow`` from its round-tripped dict form."""
    from dcf.persist import DcfRunRow
    from dcf.provenance import provenance_from_payload

    def _opt_date(v: object) -> date | None:
        return date.fromisoformat(str(v)) if v else None

    def _opt_dt(v: object) -> datetime | None:
        return datetime.fromisoformat(str(v)) if v else None

    return DcfRunRow(
        ticker=str(row.get("ticker") or "").upper(),
        valuation_date=_opt_date(row.get("valuation_date")) or date.today(),
        horizon_years=int(_as_float(row.get("horizon_years")) or 0),
        wacc=_as_float(row.get("wacc")) or 0.0,
        npv=_as_float(row.get("npv")) or 0.0,
        npv_per_share=_as_float(row.get("npv_per_share")) or 0.0,
        shares_outstanding=_as_float(row.get("shares_outstanding")) or 0.0,
        currency=str(row.get("currency") or "USD"),
        live_price=_as_float(row.get("live_price")),
        live_price_at=_opt_dt(row.get("live_price_at")),
        mos_bar_used=_as_float(row.get("mos_bar_used")),
        assumption_snapshot_json=str(row.get("assumption_snapshot_json") or "{}"),
        notes=(str(row["notes"]) if row.get("notes") else None),
        run_id=(str(row["run_id"]) if row.get("run_id") else None),
        provenance=provenance_from_payload(row.get("provenance")),
    )


def _default_persist(
    row: dict[str, object],
    *,
    db_path: Path | str | None = None,
    repo_root: Path | None = None,
) -> None:
    from db_paths import resolve_db_path
    from dcf.persist import upsert

    dcf_row = _reconstruct_row(row)
    path = resolve_db_path(db_path)
    if path is None:
        raise RuntimeError("no DB path available for the DCF live write")
    conn = connect_sqlite(path, role=SQLiteConnectionRole.WRITER, schema_preflight=True)
    try:
        upsert(conn, dcf_row, repo_root=repo_root)
        conn.commit()
    finally:
        conn.close()


def apply_dcf_proposal(
    proposal_id: int,
    *,
    db_path: Path | str | None = None,
    repo_root: Path | None = None,
    get_fn: Callable[..., Any] = get_proposal,
    persist_fn: Callable[..., None] | None = None,
) -> str:
    """The GATED write: reconstruct the proposed DcfRunRow and upsert it live.

    Reached only after the higher-bar gate clears. Re-checks the fair value (oracle)
    before writing so a corrupted payload can never upsert a non-positive fair value.
    """
    prop = get_fn(proposal_id, db_path=db_path)
    if prop is None or getattr(prop, "kind", None) != "dcf":
        raise ValueError(f"proposal {proposal_id} is not a dcf proposal")
    artifact = getattr(prop, "artifact_json", None)
    if not artifact:
        raise ValueError(
            f"dcf proposal {proposal_id} carries no proposed row (artifact_json empty)"
        )
    data: object = json.loads(artifact)
    row = cast("dict[str, object]", data).get("proposed_row") if isinstance(data, dict) else None
    if not isinstance(row, dict):
        raise ValueError(f"dcf proposal {proposal_id} carries no proposed_row")
    row_dict = cast("dict[str, object]", row)
    npv = _as_float(row_dict.get("npv_per_share"))
    if npv is None or npv <= 0:
        raise ValueError(
            f"dcf proposal {proposal_id} has no valid fair value (oracle re-check failed)"
        )
    proposed_ticker = str(row_dict.get("ticker") or "").strip().upper()
    proposal_ticker = str(getattr(prop, "ticker", "") or "").strip().upper()
    if not proposed_ticker or proposed_ticker != proposal_ticker:
        raise ValueError(f"dcf proposal {proposal_id} ticker does not match proposed row")
    reconstructed = _reconstruct_row(row_dict)
    from dcf.provenance import verify_effective_provenance

    verify_effective_provenance(
        reconstructed.provenance,
        ticker=proposed_ticker,
        repo_root=(repo_root or Path(__file__).resolve().parents[2]),
        assumption_snapshot_json=reconstructed.assumption_snapshot_json,
    )
    saver = persist_fn or _default_persist
    saver(row_dict, db_path=db_path, repo_root=repo_root)
    return f"DCF for {row_dict.get('ticker')} updated -> ${npv:,.2f}/sh (live)"


register_mutating_applier("dcf", apply_dcf_proposal)
