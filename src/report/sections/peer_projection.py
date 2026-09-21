"""Project one governed peer; source-shaped inputs remain in source adapters."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import date
from pathlib import Path

from pydantic import JsonValue, TypeAdapter

from compute.comparable_set_reader import FrozenComparableSet
from provenance.immutable_artifact import read_stable_artifact
from report.sections.p3_data import PeerCompRow
from sources.discovery_market import read_market_context
from sources.peer_financials import peer_numerical_shadow, read_peer_financials


def read_peer_owner_context(root: Path, ticker: str) -> tuple[dict[str, object], dict[str, object]]:
    path = root / "micro_thesis" / "holdings" / f"{ticker.upper()}.json"
    if not path.exists():
        return {}, {
            "status": "absent",
            "path": str(path.relative_to(root)),
            "temporal_scope": "current_only_not_historical_as_known",
        }
    snapshot, body = read_stable_artifact(path)
    payload = TypeAdapter(dict[str, JsonValue]).validate_json(body)
    selected = {
        key: value
        for key, value in payload.items()
        if key in {"competitive_watchlist", "peer_exclude", "peers_section_override"}
    }
    return dict(selected), {
        "status": "captured",
        "temporal_scope": "current_only_not_historical_as_known",
        "path": str(path.relative_to(root)),
        "sha256": snapshot.file_sha256,
        "selected_context": selected,
    }


def project_peer(
    conn: sqlite3.Connection,
    source_dir: Path,
    peer: str,
    reason: str,
    selection: FrozenComparableSet,
    *,
    as_of: date,
    named: bool,
) -> PeerCompRow:
    financials = read_peer_financials(conn, peer, as_of=as_of)
    market = read_market_context(conn, source_dir, peer, as_of=as_of)
    revenue = financials.revenue_ttm
    margin = financials.net_margin_ttm
    market_usd = market.status == "available" and market.currency == "USD"
    revenue_usd = (
        revenue.status == "available"
        and revenue.currency == "USD"
        and revenue.unit in ("USD", "actual")
    )
    notes = ["ROIC unavailable: definition pending"]
    if not market_usd:
        notes.append(
            "Market cap unavailable: "
            + ", ".join(market.reason_codes or ("USD currency unverified",))
        )
    if not revenue_usd:
        notes.append(
            "Revenue unavailable: "
            + ", ".join(revenue.reasons or ("USD currency or unit unverified",))
        )
    if margin.status != "available":
        notes.append("Net margin unavailable: " + ", ".join(margin.reasons))
    membership: dict[str, object] = {
        "source_id": selection.source_id,
        "resolved_at": selection.resolved_at,
        "as_of": as_of.isoformat(),
        "members": selection.members,
    }
    membership["snapshot_sha256"] = hashlib.sha256(
        json.dumps(membership, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    membership["selected_member"] = peer
    membership["selection_reason"] = reason
    reasons = (reason.replace("_", " "),)
    if named and "named rival" not in reasons:
        reasons += ("named rival",)
    return PeerCompRow(
        peer_ticker=peer,
        peer_name=market.name,
        market_cap_usd=float(market.market_cap)
        if market_usd and market.market_cap is not None
        else None,
        revenue_ttm_usd=float(revenue.value) if revenue_usd and revenue.value is not None else None,
        net_margin_ttm=float(margin.value)
        if margin.status == "available" and margin.value is not None
        else None,
        roic_ttm=None,
        match_reasons=reasons,
        coverage_notes=tuple(notes),
        source_evidence={
            "schema_version": "canonical-peer-comparison/v1",
            "membership": membership,
            "financials": financials.model_dump(mode="json"),
            "market": market.model_dump(mode="json"),
            "dual_read_parity": peer_numerical_shadow(source_dir, peer, financials),
            "acquisition_completeness": "unverified",
            "decision_grade": False,
        },
    )
