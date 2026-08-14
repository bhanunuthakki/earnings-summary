"""Atomic/idempotent owner-decision checkpoint contracts."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest
from pydantic import ValidationError

from research.owner_decision_checkpoint import (
    CheckpointConflictError,
    CheckpointInvariantError,
    DecisionLeg,
    HoldingBasisPosition,
    HoldingsBasis,
    OwnerDecisionCheckpointPayload,
    SizingIntentSpec,
    TargetBand,
    confirm_owner_decision_checkpoint,
    thesis_content_sha256,
)


def _payload(
    *, event: str = "turn-1", why: str = "Reallocate attention and capital"
) -> OwnerDecisionCheckpointPayload:
    thesis = "The operating thesis remains intact."
    return OwnerDecisionCheckpointPayload(
        source_channel="claude_session",
        source_event_id=event,
        holdings_basis=HoldingsBasis(
            source="materialized_holdings_snapshot",
            as_of="2026-08-14T12:00:00",
            embedded_positions=(
                HoldingBasisPosition(ticker="WIX", availability="observed", weight_pct=2.5),
            ),
        ),
        legs=(
            DecisionLeg(
                leg_id="wix",
                ticker="WIX",
                action="sell",
                proposed_delta_pct=2.5,
                target_band=TargetBand(minimum_pct=0, maximum_pct=0),
                price_level=85,
                account="tax_deferred_ira",
                instrument="equity",
                horizon="not_provided",
                thesis_state="intact",
                thesis_content_sha256=thesis_content_sha256(thesis),
                thesis_excerpt=thesis,
                changed_since_prior="Conviction declined",
                why_now=why,
                conviction="low",
                falsifier="Revisit on stronger evidence",
                portfolio_role="prosumer software",
                qualitative_stress_implication="duration and prosumer-cycle exposure",
                alternative_use_of_capital="international small value",
                target_verification="target_unverified",
            ),
        ),
        sizing_intents=(
            SizingIntentSpec(
                leg_id="wix",
                ticker="WIX",
                intent_kind="target_weight_pct",
                intent_value=0,
                narrative="Full exit target",
                target_band=TargetBand(minimum_pct=0, maximum_pct=0),
            ),
        ),
    )


def test_same_event_and_payload_returns_existing_without_duplicate(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    database = migrated_db(tmp_path / "checkpoint.db")
    first = confirm_owner_decision_checkpoint(_payload(), db_path=database)
    second = confirm_owner_decision_checkpoint(_payload(), db_path=database)

    assert first.created is True
    assert second.created is False
    assert second.checkpoint_id == first.checkpoint_id
    assert second.decision_ids == first.decision_ids
    assert second.sizing_intent_ids == first.sizing_intent_ids
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM owner_decision_checkpoints").fetchone() == (
            1,
        )
        assert connection.execute(
            "SELECT COUNT(*) FROM decisions WHERE decided_by='owner' AND ticker='WIX'"
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT COUNT(*) FROM position_sizing_intent WHERE ticker='WIX'"
        ).fetchone() == (1,)


def test_same_event_with_changed_payload_conflicts_and_rolls_back(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    database = migrated_db(tmp_path / "conflict.db")
    confirm_owner_decision_checkpoint(_payload(), db_path=database)

    with pytest.raises(CheckpointConflictError, match="amendment"):
        confirm_owner_decision_checkpoint(
            _payload(why="A materially different rationale"), db_path=database
        )

    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM owner_decision_checkpoints").fetchone() == (
            1,
        )
        assert connection.execute(
            "SELECT COUNT(*) FROM owner_decision_checkpoint_decisions"
        ).fetchone() == (1,)


def test_checkpoint_rejects_backdating_invalid_basis_and_false_verification() -> None:
    base = _payload()
    leg = base.legs[0]

    with pytest.raises(ValidationError, match="made_at"):
        DecisionLeg.model_validate(
            {
                **leg.model_dump(mode="python"),
                "made_at": "2020-01-01T00:00:00",
            }
        )

    with pytest.raises(ValidationError, match="ISO-8601"):
        HoldingsBasis(
            source="materialized_holdings_snapshot",
            as_of="not-a-timestamp",
            embedded_positions=(
                HoldingBasisPosition(ticker="AVDV", availability="missing_from_snapshot"),
            ),
        )

    with pytest.raises(ValidationError, match="cannot verify"):
        OwnerDecisionCheckpointPayload.model_validate(
            {
                **base.model_dump(mode="python"),
                "holdings_basis": HoldingsBasis(
                    source="materialized_holdings_snapshot",
                    as_of="2026-08-14T12:00:00",
                    embedded_positions=(
                        HoldingBasisPosition(ticker="WIX", availability="missing_from_snapshot"),
                    ),
                ),
                "legs": (leg.model_copy(update={"target_verification": "verified"}),),
            }
        )


def test_checkpoint_requires_one_typed_ledger_entry_per_changed_ticker() -> None:
    base = _payload()
    changed_leg = base.legs[0].model_copy(update={"thesis_changed": True})
    entry = {
        "ticker": "WIX",
        "entry_kind": "thesis_update",
        "body": "Accepted owner thesis update",
    }

    with pytest.raises(ValidationError, match="exactly one"):
        OwnerDecisionCheckpointPayload.model_validate(
            {
                **base.model_dump(mode="python"),
                "legs": (changed_leg,),
                "ledger_entries": (entry, entry),
            }
        )

    with pytest.raises(ValidationError, match="entry_kind"):
        OwnerDecisionCheckpointPayload.model_validate(
            {
                **base.model_dump(mode="python"),
                "legs": (changed_leg,),
                "ledger_entries": ({**entry, "entry_kind": "observation"},),
            }
        )

    with pytest.raises(ValidationError, match="body"):
        OwnerDecisionCheckpointPayload.model_validate(
            {
                **base.model_dump(mode="python"),
                "legs": (changed_leg,),
                "ledger_entries": ({**entry, "body": "   "},),
            }
        )


def test_sizing_intent_must_match_its_decision_leg() -> None:
    base = _payload()
    intent = base.sizing_intents[0]

    with pytest.raises(ValidationError, match="ticker must match"):
        OwnerDecisionCheckpointPayload.model_validate(
            {
                **base.model_dump(mode="python"),
                "sizing_intents": (intent.model_copy(update={"ticker": "AVDV"}),),
            }
        )

    with pytest.raises(ValidationError, match="target band must match"):
        OwnerDecisionCheckpointPayload.model_validate(
            {
                **base.model_dump(mode="python"),
                "sizing_intents": (
                    intent.model_copy(
                        update={"target_band": TargetBand(minimum_pct=4.5, maximum_pct=5)}
                    ),
                ),
            }
        )

    with pytest.raises(ValidationError, match="must repeat"):
        OwnerDecisionCheckpointPayload.model_validate(
            {
                **base.model_dump(mode="python"),
                "sizing_intents": (
                    intent.model_copy(update={"target_band": None, "intent_value": 99}),
                ),
            }
        )

    with pytest.raises(ValidationError, match="own alternative"):
        OwnerDecisionCheckpointPayload.model_validate(
            {
                **base.model_dump(mode="python"),
                "legs": (base.legs[0].model_copy(update={"alternative_leg_id": "wix"}),),
            }
        )


def test_late_intent_mismatch_rolls_back_checkpoint_and_decision(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    database = migrated_db(tmp_path / "rollback.db")
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO position_sizing_intent"
            "(id,user_id,ticker,intent_kind,intent_value,narrative,created_at,updated_at) "
            "VALUES (7,'bhanu','AVDV','target_weight_pct',4.75,'target','2026-08-14','2026-08-14')"
        )
        connection.commit()
    base = _payload(event="rollback-event")
    broken = base.model_copy(
        update={
            "sizing_intents": (
                SizingIntentSpec(
                    leg_id="wix",
                    ticker="WIX",
                    intent_kind="target_weight_pct",
                    intent_value=0,
                    narrative="mismatch",
                    existing_sizing_intent_id=7,
                    target_band=base.legs[0].target_band,
                ),
            )
        }
    )

    with pytest.raises(CheckpointInvariantError, match="does not match"):
        confirm_owner_decision_checkpoint(broken, db_path=database)

    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM owner_decision_checkpoints").fetchone() == (
            0,
        )
        assert connection.execute(
            "SELECT COUNT(*) FROM decisions WHERE decided_by='owner'"
        ).fetchone() == (0,)


def _seed_retrospective_rows(database: Path) -> None:
    with sqlite3.connect(database) as connection:
        for decision_id, ticker, kind in ((135, "WIX", "sell"), (136, "AVDV", "add")):
            connection.execute(
                "INSERT INTO decisions"
                "(id,ticker,recommendation_kind,decided_by,scope,size_pct,made_at,created_at) "
                "VALUES (?,?,?,'owner','ticker',2.5444,?,?)",
                (decision_id, ticker, kind, f"2026-08-14T08:46:{decision_id}", "2026-08-14"),
            )
        connection.execute(
            "INSERT INTO position_sizing_intent"
            "(id,user_id,ticker,intent_kind,intent_value,narrative,created_at,updated_at) "
            "VALUES (7,'bhanu','AVDV','target_weight_pct',4.75,'target','2026-08-14','2026-08-14')"
        )
        connection.commit()


def test_retrospective_pair_links_existing_rows_without_backdating_or_ledger(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    database = migrated_db(tmp_path / "retrospective.db")
    _seed_retrospective_rows(database)
    thesis = "WIX thesis remains substantially intact."
    payload = OwnerDecisionCheckpointPayload(
        source_channel="claude_session",
        source_event_id="wix-avdv-repair-v1",
        retrospective=True,
        holdings_basis=HoldingsBasis(
            source="materialized_holdings_snapshot",
            as_of="2026-08-13T11:01:38.636546",
            embedded_positions=(
                HoldingBasisPosition(ticker="WIX", availability="observed", weight_pct=2.5444),
                HoldingBasisPosition(ticker="AVDV", availability="missing_from_snapshot"),
            ),
        ),
        legs=(
            DecisionLeg(
                leg_id="wix",
                ticker="WIX",
                action="sell",
                existing_decision_id=135,
                proposed_delta_pct=2.5444,
                account="tax_deferred_ira",
                instrument="equity",
                horizon="not_provided",
                thesis_state="intact",
                thesis_content_sha256=thesis_content_sha256(thesis),
                thesis_excerpt=thesis,
                changed_since_prior="conviction declined",
                why_now="capital and attention reallocation",
                conviction="low",
                falsifier="revisit on stronger evidence",
                portfolio_role="prosumer software",
                qualitative_stress_implication="duration exposure",
                alternative_use_of_capital="AVDV",
                alternative_leg_id="avdv",
                target_verification="target_unverified",
            ),
            DecisionLeg(
                leg_id="avdv",
                ticker="AVDV",
                action="add",
                existing_decision_id=136,
                proposed_delta_pct=2.5444,
                target_band=TargetBand(minimum_pct=4.5, maximum_pct=5),
                account="tax_deferred_ira",
                instrument="etf",
                horizon="not_provided",
                thesis_state="not_the_reason",
                changed_since_prior="new allocation",
                why_now="funded by WIX proceeds",
                conviction="not_provided",
                falsifier="not_provided",
                portfolio_role="international small value",
                qualitative_stress_implication="currency and international exposure",
                alternative_use_of_capital="WIX proceeds",
                alternative_leg_id="wix",
                target_verification="target_unverified",
            ),
        ),
        sizing_intents=(
            SizingIntentSpec(
                leg_id="avdv",
                ticker="AVDV",
                intent_kind="target_weight_pct",
                intent_value=4.75,
                narrative="target 4.5%-5.0%",
                existing_sizing_intent_id=7,
                target_band=TargetBand(minimum_pct=4.5, maximum_pct=5),
            ),
        ),
    )
    receipt = confirm_owner_decision_checkpoint(payload, db_path=database)
    retried = confirm_owner_decision_checkpoint(payload, db_path=database)
    assert receipt.decision_ids == (135, 136)
    assert retried.decision_ids == receipt.decision_ids
    assert receipt.sizing_intent_ids == (7,)
    assert retried.sizing_intent_ids == receipt.sizing_intent_ids
    assert receipt.ledger_entry_ids == ()

    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            "SELECT id,made_at,basis_kind,basis_ref_id,basis_meta_json "
            "FROM decisions WHERE id IN (135,136) ORDER BY id"
        ).fetchall()
        assert [str(row["made_at"]) for row in rows] == [
            "2026-08-14T08:46:135",
            "2026-08-14T08:46:136",
        ]
        assert all(row["basis_kind"] == "owner_checkpoint" for row in rows)
        assert all(row["basis_ref_id"] == receipt.checkpoint_id for row in rows)
        wix_meta = json.loads(str(rows[0]["basis_meta_json"]))["owner_decision_checkpoint"]
        avdv_meta = json.loads(str(rows[1]["basis_meta_json"]))["owner_decision_checkpoint"]
        assert wix_meta["holding_availability"] == "observed"
        assert avdv_meta["holding_availability"] == "missing_from_snapshot"
        assert avdv_meta["target_band"] == {"minimum_pct": 4.5, "maximum_pct": 5.0}
        assert connection.execute("SELECT COUNT(*) FROM thesis_ledger_entries").fetchone()[0] == 0
