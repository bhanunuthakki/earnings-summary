"""Exact WIX/AVDV retrospective checkpoint construction."""

from __future__ import annotations

import sqlite3
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from execution import build_wix_avdv_retrospective_checkpoint as builder
from execution import land_session_notes
from research.owner_decision_checkpoint import payload_sha256


def _seed(database: Path) -> None:
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO decisions"
            "(id,ticker,recommendation_kind,decided_by,scope,made_at,created_at) "
            "VALUES (53,'WIX','initiate','owner','ticker','2026-02-15','2026-07-02')"
        )
        connection.execute(
            "INSERT INTO decisions"
            "(id,ticker,recommendation_kind,decided_by,scope,size_pct,made_at,created_at) "
            "VALUES (135,'WIX','sell','owner','ticker',2.5444,'2026-08-14T08:46:19','2026-08-14')"
        )
        connection.execute(
            "INSERT INTO decisions"
            "(id,ticker,recommendation_kind,decided_by,scope,size_pct,made_at,created_at) "
            "VALUES (136,'AVDV','add','owner','ticker',2.5444,'2026-08-14T08:46:25','2026-08-14')"
        )
        connection.execute(
            "INSERT INTO position_sizing_intent"
            "(id,user_id,ticker,intent_kind,intent_value,narrative,created_at,updated_at) "
            "VALUES (7,'bhanu','AVDV','target_weight_pct',4.75,'target','2026-08-14','2026-08-14')"
        )
        connection.execute(
            "INSERT INTO thesis_state"
            "(ticker,thesis,raw_json,last_updated,ingested_at,breach_status) "
            "VALUES ('WIX','Canonical WIX thesis','{}','2026-08-13','2026-08-13','warn')"
        )
        connection.commit()


def test_builder_freezes_truthful_missingness_and_no_fabricated_thesis_change(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    database = migrated_db(tmp_path / "repair.db")
    _seed(database)
    payload = builder.build_payload(database)

    assert payload.retrospective is True
    assert payload.holdings_basis.source_content_sha256 is None
    assert payload.holdings_basis.position("WIX").weight_pct == 2.5444
    assert payload.holdings_basis.position("AVDV").availability == "missing_from_snapshot"
    wix, avdv = payload.legs
    assert wix.existing_decision_id == 135
    assert wix.price_level == 85
    assert wix.prior_owner_decision_id == 53
    assert wix.thesis_state == "intact"
    assert wix.thesis_changed is False
    assert avdv.existing_decision_id == 136
    assert avdv.conviction == "not_provided"
    assert avdv.falsifier == "not_provided"
    assert avdv.target_band is not None
    assert avdv.target_band.minimum_pct == 4.5
    assert avdv.target_band.maximum_pct == 5.0
    assert avdv.target_verification == "target_unverified"
    assert "could not be reconciled" in str(avdv.target_delta_mismatch)
    assert payload.sizing_intents[0].existing_sizing_intent_id == 7
    assert payload.ledger_entries == ()

    output = tmp_path / "checkpoint.json"
    assert builder.main(["--db", str(database), "--output", str(output)]) == 0
    first = output.read_text(encoding="utf-8")
    assert builder.main(["--db", str(database), "--output", str(output)]) == 0
    assert output.read_text(encoding="utf-8") == first
    assert payload_sha256(payload) == payload_sha256(type(payload).model_validate_json(first))


def test_session_decision_fails_closed_without_checkpoint_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        ["land_session_notes.py", "decision", "--repo-root", str(tmp_path)],
    )
    assert land_session_notes.main() == 2
