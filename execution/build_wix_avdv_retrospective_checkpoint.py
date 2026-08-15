"""Build the current-dated WIX/AVDV retrospective checkpoint payload.

This utility does not write the database.  It verifies the already-recorded
decision/intent rows, freezes the decision-time materialized-holdings facts,
and emits the typed payload consumed by record_owner_decision_checkpoint.py.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from research.owner_decision_checkpoint import (  # noqa: E402
    DecisionLeg,
    HoldingBasisPosition,
    HoldingsBasis,
    OwnerDecisionCheckpointPayload,
    SizingIntentSpec,
    TargetBand,
    payload_sha256,
    thesis_content_sha256,
)
from user_state._db import open_read_conn  # noqa: E402

WIX_DECISION_ID = 135
AVDV_DECISION_ID = 136
AVDV_INTENT_ID = 7
DECISION_DELTA_PCT = 2.5444
HOLDINGS_AS_OF = "2026-08-13T11:01:38.636546"
SOURCE_EVENT_ID = "wix-avdv-final-2026-08-14:retrospective-checkpoint-v1"


def _require_decision(conn: sqlite3.Connection, decision_id: int, ticker: str, kind: str) -> None:
    row = conn.execute(
        "SELECT ticker,recommendation_kind,decided_by,size_pct FROM decisions WHERE id=?",
        (decision_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"required decision {decision_id} is missing")
    if (
        str(row["ticker"]).upper() != ticker
        or str(row["recommendation_kind"]) != kind
        or str(row["decided_by"]) != "owner"
        or row["size_pct"] is None
        or abs(float(row["size_pct"]) - DECISION_DELTA_PCT) > 1e-9
    ):
        raise RuntimeError(f"decision {decision_id} no longer matches the repair contract")


def build_payload(db_path: Path) -> OwnerDecisionCheckpointPayload:
    conn = open_read_conn(db_path)
    try:
        _require_decision(conn, WIX_DECISION_ID, "WIX", "sell")
        _require_decision(conn, AVDV_DECISION_ID, "AVDV", "add")
        prior = conn.execute("SELECT ticker,decided_by FROM decisions WHERE id=53").fetchone()
        if prior is None or str(prior["ticker"]).upper() != "WIX" or prior["decided_by"] != "owner":
            raise RuntimeError("prior WIX owner decision 53 is unavailable")
        intent = conn.execute(
            "SELECT user_id,ticker,intent_kind,intent_value FROM position_sizing_intent WHERE id=?",
            (AVDV_INTENT_ID,),
        ).fetchone()
        if intent is None or (
            str(intent["user_id"]) != "bhanu"
            or str(intent["ticker"]).upper() != "AVDV"
            or str(intent["intent_kind"]) != "target_weight_pct"
            or intent["intent_value"] is None
            or abs(float(intent["intent_value"]) - 4.75) > 1e-9
        ):
            raise RuntimeError("AVDV sizing intent 7 no longer matches the repair contract")
        thesis_row = conn.execute("SELECT thesis FROM thesis_state WHERE ticker='WIX'").fetchone()
        if thesis_row is None or not str(thesis_row["thesis"] or "").strip():
            raise RuntimeError("current WIX thesis is unavailable")
        wix_thesis = str(thesis_row["thesis"]).strip()
    finally:
        conn.close()

    target = TargetBand(minimum_pct=4.5, maximum_pct=5.0)
    return OwnerDecisionCheckpointPayload(
        source_channel="claude_session",
        source_event_id=SOURCE_EVENT_ID,
        retrospective=True,
        holdings_basis=HoldingsBasis(
            source="materialized_holdings_snapshot",
            as_of=HOLDINGS_AS_OF,
            source_content_sha256=None,
            embedded_positions=(
                HoldingBasisPosition(
                    ticker="WIX", availability="observed", weight_pct=DECISION_DELTA_PCT
                ),
                HoldingBasisPosition(ticker="AVDV", availability="missing_from_snapshot"),
            ),
            basis_note=(
                "Retrospective embedded basis from the decision-time materialized snapshot. "
                "The mutable full snapshot file was not retained, so no whole-file hash is claimed."
            ),
        ),
        legs=(
            DecisionLeg(
                leg_id="wix_exit",
                ticker="WIX",
                action="sell",
                existing_decision_id=WIX_DECISION_ID,
                proposed_delta_pct=DECISION_DELTA_PCT,
                target_band=TargetBand(minimum_pct=0.0, maximum_pct=0.0),
                price_level=85.0,
                account="tax_deferred_ira",
                instrument="equity",
                horizon="not_provided",
                thesis_state="intact",
                thesis_content_sha256=thesis_content_sha256(wix_thesis),
                thesis_excerpt=wix_thesis[:1200],
                thesis_changed=False,
                changed_since_prior=(
                    "Conviction and willingness to spend monitoring attention declined; the "
                    "company and much of the operating thesis remained intact."
                ),
                why_now=(
                    "Full exit near $85 because Base44 optionality did not overcome low "
                    "conviction, monitoring burden, prosumer-cycle and duration risk, and a "
                    "better portfolio use of the capital."
                ),
                conviction="low",
                falsifier=(
                    "Revisit only if Base44 economics become transparent and core plus FCF "
                    "proof materially raises conviction versus alternatives."
                ),
                portfolio_role="single-company prosumer software and Base44 optionality",
                qualitative_stress_implication=(
                    "Exposed to prosumer demand weakness and long-duration software multiple "
                    "compression; exit reduces those concentrated sensitivities."
                ),
                alternative_use_of_capital="allocate the full proceeds to AVDV",
                prior_owner_decision_id=53,
                alternative_leg_id="avdv_add",
                target_verification="target_unverified",
                target_delta_mismatch=(
                    "A full-exit target is not execution evidence; holdings must later confirm "
                    "that WIX reached zero."
                ),
            ),
            DecisionLeg(
                leg_id="avdv_add",
                ticker="AVDV",
                action="add",
                existing_decision_id=AVDV_DECISION_ID,
                proposed_delta_pct=DECISION_DELTA_PCT,
                target_band=target,
                price_level=None,
                account="tax_deferred_ira",
                instrument="etf",
                horizon="not_provided",
                thesis_state="not_the_reason",
                thesis_changed=False,
                changed_since_prior="new alternative-capital allocation paired with the WIX exit",
                why_now=(
                    "Allocate the full WIX proceeds to international small-cap value, targeting "
                    "4.5%-5.0% of the portfolio."
                ),
                conviction="not_provided",
                falsifier="not_provided",
                portfolio_role="international small-cap value diversification and factor exposure",
                qualitative_stress_implication=(
                    "Adds international, currency and small-value cycle exposure; intended as "
                    "diversification rather than a short-term hedge."
                ),
                alternative_use_of_capital="funded by the full WIX proceeds",
                alternative_leg_id="wix_exit",
                target_verification="target_unverified",
                target_delta_mismatch=(
                    "The 2.5444% proposed delta could not be reconciled to the 4.5%-5.0% target "
                    "because AVDV was missing from the decision-time snapshot."
                ),
            ),
        ),
        sizing_intents=(
            SizingIntentSpec(
                leg_id="avdv_add",
                ticker="AVDV",
                intent_kind="target_weight_pct",
                intent_value=4.75,
                narrative=(
                    "Target band 4.5%-5.0% after allocating the full WIX proceeds; "
                    "target remains unverified pending holdings evidence."
                ),
                existing_sizing_intent_id=AVDV_INTENT_ID,
                target_band=target,
            ),
        ),
        ledger_entries=(),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    payload = build_payload(args.db)
    rendered = payload.model_dump_json(indent=2) + "\n"
    if args.output.exists():
        if args.output.read_text(encoding="utf-8") != rendered:
            raise RuntimeError(f"refusing to overwrite different payload: {args.output}")
        status = "already_done"
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
        status = "created"
    print(
        json.dumps(
            {
                "status": status,
                "output": str(args.output.resolve()),
                "payload_sha256": payload_sha256(payload),
                "source_event_id": payload.source_event_id,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
