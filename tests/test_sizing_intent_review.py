"""Regression coverage for the read-only sizing-intent review adapter."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from advisor.sizing_intent_review import load_sizing_intent_review
from research.owner_decision_checkpoint import (
    DecisionLeg,
    HoldingBasisPosition,
    HoldingsBasis,
    OwnerDecisionCheckpointPayload,
    SizingIntentSpec,
    TargetBand,
    canonical_payload_json,
    payload_sha256,
    thesis_content_sha256,
)


def _database(tmp_path: Path, *, checkpoints: bool = True) -> Path:
    database = tmp_path / "review.db"
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            CREATE TABLE position_sizing_intent (
                id INTEGER PRIMARY KEY,
                user_id TEXT NOT NULL,
                ticker TEXT NOT NULL,
                intent_kind TEXT NOT NULL,
                intent_value REAL,
                narrative TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        if checkpoints:
            connection.execute(
                """
                CREATE TABLE owner_decision_checkpoints (
                    id INTEGER PRIMARY KEY,
                    checkpoint_schema_version TEXT NOT NULL,
                    payload_sha256 TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    confirmed_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE owner_decision_checkpoint_sizing_intents (
                    checkpoint_id INTEGER NOT NULL,
                    leg_id TEXT NOT NULL,
                    sizing_intent_id INTEGER NOT NULL
                )
                """
            )
    return database


def _insert_intent(database: Path, *, intent_id: int, value: float, created_at: str) -> None:
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            INSERT INTO position_sizing_intent(
                id,user_id,ticker,intent_kind,intent_value,narrative,created_at,updated_at
            ) VALUES (?,?, 'WIX', 'target_weight_pct', ?, 'owner-recorded', ?, ?)
            """,
            (intent_id, "bhanu", value, created_at, created_at),
        )


def _checkpoint_payload() -> OwnerDecisionCheckpointPayload:
    thesis = "The operating thesis remains intact."
    band = TargetBand(minimum_pct=4.5, maximum_pct=5.0)
    return OwnerDecisionCheckpointPayload(
        source_channel="claude_session",
        source_event_id="turn-85",
        holdings_basis=HoldingsBasis(
            source="materialized_holdings_snapshot",
            as_of="2026-08-23T12:00:00",
            source_content_sha256="a" * 64,
            embedded_positions=(
                HoldingBasisPosition(ticker="WIX", availability="observed", weight_pct=4.75),
            ),
        ),
        legs=(
            DecisionLeg(
                leg_id="wix-target",
                ticker="WIX",
                action="add",
                proposed_delta_pct=0.25,
                target_band=band,
                price_level=85.0,
                account="tax_deferred_ira",
                instrument="equity",
                horizon="not_provided",
                thesis_state="intact",
                thesis_content_sha256=thesis_content_sha256(thesis),
                thesis_excerpt=thesis,
                changed_since_prior="Target remains recorded",
                why_now="Owner confirmed the checkpoint",
                conviction="medium",
                falsifier="not_provided",
                portfolio_role="prosumer software",
                qualitative_stress_implication="duration exposure",
                alternative_use_of_capital="not_provided",
                target_verification="verified",
            ),
        ),
        sizing_intents=(
            SizingIntentSpec(
                leg_id="wix-target",
                ticker="WIX",
                intent_kind="target_weight_pct",
                intent_value=4.75,
                narrative="Owner-recorded target",
                target_band=band,
            ),
        ),
    )


def test_no_rows_is_available_empty_evidence(tmp_path: Path) -> None:
    review = load_sizing_intent_review(_database(tmp_path))

    assert review.sizing_intent_source_available is True
    assert review.checkpoint_link_source_available is True
    assert review.entries == ()
    assert review.governed_allocation_recommendation is None


def test_checkpoint_linked_target_band_preserves_checkpoint_evidence(tmp_path: Path) -> None:
    database = _database(tmp_path)
    _insert_intent(database, intent_id=7, value=4.75, created_at="2026-08-23T12:01:00")
    payload = _checkpoint_payload()
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            INSERT INTO owner_decision_checkpoints(
                id,checkpoint_schema_version,payload_sha256,payload_json,confirmed_at
            ) VALUES (17, ?, ?, ?, '2026-08-23T12:02:00')
            """,
            (payload.schema_version, payload_sha256(payload), canonical_payload_json(payload)),
        )
        connection.execute(
            """
            INSERT INTO owner_decision_checkpoint_sizing_intents(
                checkpoint_id,leg_id,sizing_intent_id
            ) VALUES (17, 'wix-target', 7)
            """
        )

    entry = load_sizing_intent_review(database).entries[0]

    assert entry.checkpoint_linked is True
    assert entry.checkpoint_evidence_available is True
    assert entry.checkpoint_id == 17
    assert entry.checkpoint_schema_version == "owner-decision-checkpoint/v1"
    assert entry.checkpoint_payload_sha256 == payload_sha256(payload)
    assert entry.holdings_as_of == "2026-08-23T12:00:00"
    assert entry.holding_availability == "observed"
    assert entry.target_verification == "verified"
    assert entry.target_band == TargetBand(minimum_pct=4.5, maximum_pct=5.0)
    assert entry.intent.updated_at.isoformat() == "2026-08-23T12:01:00"


def test_unlinked_direct_append_has_no_checkpoint_evidence(tmp_path: Path) -> None:
    database = _database(tmp_path)
    _insert_intent(database, intent_id=1, value=2.0, created_at="2026-08-23T12:00:00")
    _insert_intent(database, intent_id=8, value=3.0, created_at="2026-08-23T12:01:00")

    entries = load_sizing_intent_review(database).entries

    assert len(entries) == 1
    entry = entries[0]
    assert entry.checkpoint_linked is False
    assert entry.checkpoint_evidence_available is False
    assert entry.checkpoint_id is None
    assert entry.target_band is None
    assert entry.intent.intent_value == 3.0


def test_partial_checkpoint_evidence_keeps_the_link_without_inventing_a_band(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    _insert_intent(database, intent_id=9, value=4.75, created_at="2026-08-23T12:01:00")
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            INSERT INTO owner_decision_checkpoints(
                id,checkpoint_schema_version,payload_sha256,payload_json,confirmed_at
            ) VALUES (18, 'owner-decision-checkpoint/v1', ?, ?, '2026-08-23T12:02:00')
            """,
            ("c" * 64, json.dumps({"not": "a checkpoint payload"})),
        )
        connection.execute(
            """
            INSERT INTO owner_decision_checkpoint_sizing_intents(
                checkpoint_id,leg_id,sizing_intent_id
            ) VALUES (18, 'wix-target', 9)
            """
        )

    entry = load_sizing_intent_review(database).entries[0]

    assert entry.checkpoint_linked is True
    assert entry.checkpoint_evidence_available is False
    assert entry.checkpoint_id == 18
    assert entry.checkpoint_payload_sha256 == "c" * 64
    assert entry.target_band is None
    assert entry.price_level is None


def test_checkpoint_missing_its_linked_holding_degrades_to_partial_evidence(tmp_path: Path) -> None:
    database = _database(tmp_path)
    _insert_intent(database, intent_id=10, value=4.75, created_at="2026-08-23T12:01:00")
    payload = _checkpoint_payload().model_copy(
        update={
            "holdings_basis": HoldingsBasis(
                source="materialized_holdings_snapshot",
                as_of="2026-08-23T12:00:00",
                embedded_positions=(
                    HoldingBasisPosition(ticker="AVDV", availability="observed", weight_pct=4.75),
                ),
            )
        }
    )
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            INSERT INTO owner_decision_checkpoints(
                id,checkpoint_schema_version,payload_sha256,payload_json,confirmed_at
            ) VALUES (19, ?, ?, ?, '2026-08-23T12:02:00')
            """,
            (payload.schema_version, payload_sha256(payload), canonical_payload_json(payload)),
        )
        connection.execute(
            """
            INSERT INTO owner_decision_checkpoint_sizing_intents(
                checkpoint_id,leg_id,sizing_intent_id
            ) VALUES (19, 'wix-target', 10)
            """
        )

    entry = load_sizing_intent_review(database).entries[0]

    assert entry.checkpoint_linked is True
    assert entry.checkpoint_evidence_available is False
    assert entry.checkpoint_id == 19
    assert entry.holdings_as_of is None
    assert entry.target_band is None


def test_checkpoint_with_mismatched_intent_identity_degrades_to_partial_evidence(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    _insert_intent(database, intent_id=11, value=3.0, created_at="2026-08-23T12:01:00")
    payload = _checkpoint_payload()
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            INSERT INTO owner_decision_checkpoints(
                id,checkpoint_schema_version,payload_sha256,payload_json,confirmed_at
            ) VALUES (20, ?, ?, ?, '2026-08-23T12:02:00')
            """,
            (payload.schema_version, payload_sha256(payload), canonical_payload_json(payload)),
        )
        connection.execute(
            """
            INSERT INTO owner_decision_checkpoint_sizing_intents(
                checkpoint_id,leg_id,sizing_intent_id
            ) VALUES (20, 'wix-target', 11)
            """
        )

    entry = load_sizing_intent_review(database).entries[0]

    assert entry.checkpoint_linked is True
    assert entry.checkpoint_evidence_available is False
    assert entry.checkpoint_id == 20
    assert entry.checkpoint_source_channel is None
    assert entry.target_band is None


def test_checkpoint_with_mismatched_payload_digest_degrades_to_partial_evidence(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    _insert_intent(database, intent_id=12, value=4.75, created_at="2026-08-23T12:01:00")
    payload = _checkpoint_payload()
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            INSERT INTO owner_decision_checkpoints(
                id,checkpoint_schema_version,payload_sha256,payload_json,confirmed_at
            ) VALUES (21, ?, ?, ?, '2026-08-23T12:02:00')
            """,
            (payload.schema_version, "d" * 64, canonical_payload_json(payload)),
        )
        connection.execute(
            """
            INSERT INTO owner_decision_checkpoint_sizing_intents(
                checkpoint_id,leg_id,sizing_intent_id
            ) VALUES (21, 'wix-target', 12)
            """
        )

    entry = load_sizing_intent_review(database).entries[0]

    assert entry.checkpoint_linked is True
    assert entry.checkpoint_evidence_available is False
    assert entry.checkpoint_id == 21
    assert entry.target_band is None


def test_unavailable_sizing_intent_source_is_explicit(tmp_path: Path) -> None:
    database = tmp_path / "missing-source.db"
    with sqlite3.connect(database):
        pass

    review = load_sizing_intent_review(database)

    assert review.sizing_intent_source_available is False
    assert review.checkpoint_link_source_available is False
    assert review.entries == ()


def test_missing_checkpoint_source_preserves_available_sizing_intent(tmp_path: Path) -> None:
    database = _database(tmp_path, checkpoints=False)
    _insert_intent(database, intent_id=13, value=4.75, created_at="2026-08-23T12:01:00")

    review = load_sizing_intent_review(database)

    assert review.sizing_intent_source_available is True
    assert review.checkpoint_link_source_available is False
    assert len(review.entries) == 1
    assert review.entries[0].intent.id == 13
    assert review.entries[0].checkpoint_evidence_available is False
