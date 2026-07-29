"""Contracts for immutable selection decisions over legacy fact rows."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

import pytest
from alembic.config import Config
from pydantic import ValidationError

from alembic import command
from provenance.fact_selection import FactSelectionDecision, FactSelectionLedger

ROOT = Path(__file__).resolve().parents[1]
PRIOR = "0216_search_corpus_foundation"
HEAD = "0217_fact_selection_ledger"
STAMP = datetime(2026, 7, 26, 22, 0, 0)
SHA = "a" * 64


def _config(path: Path) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    return config


def _conn(tmp_path: Path) -> sqlite3.Connection:
    path = tmp_path / "fact-selection.db"
    config = _config(path)
    command.stamp(config, PRIOR)
    command.upgrade(config, HEAD)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = ON")
    # This focused migration test stamps the predecessor rather than replaying
    # the full historical graph; create the two pre-existing FK parents that a
    # real 0216 database already has.
    conn.execute("CREATE TABLE evidence_nodes (node_id TEXT PRIMARY KEY)")
    conn.execute("CREATE TABLE validation_issues (id INTEGER PRIMARY KEY)")
    conn.execute("CREATE TABLE kpi_facts (id INTEGER PRIMARY KEY, value TEXT NOT NULL)")
    conn.execute("INSERT INTO kpi_facts (id, value) VALUES (7, '24000000000')")
    conn.execute("INSERT INTO kpi_facts (id, value) VALUES (8, '12')")
    return conn


def _decision(
    *,
    decision_id: str = "exclude-kpi-7-r1",
    idempotency_key: str = "exclude:kpi_facts:7:policy-v1",
    revision: int = 1,
    supersedes_decision_id: str | None = None,
    reason_code: str = "mis_scaled_count_unit",
) -> FactSelectionDecision:
    return FactSelectionDecision(
        decision_id=decision_id,
        idempotency_key=idempotency_key,
        target_table="kpi_facts",
        target_row_id=7,
        revision=revision,
        selection_state="excluded",
        reason_code=reason_code,
        reason_details=(("definition_name", "Authorized Shares (in shares)"),),
        decision_kind="deterministic",
        policy_name="capture_count_unit_scale_guard",
        policy_version="1",
        policy_config_sha256=SHA,
        evidence_node_id=None,
        validation_issue_id=None,
        effective_at=STAMP,
        knowledge_at=STAMP,
        recorded_at=STAMP,
        supersedes_decision_id=supersedes_decision_id,
        material_dissent=False,
    )


def test_selection_decision_requires_a_closed_target_and_ordered_clocks() -> None:
    with pytest.raises(ValidationError, match="allowlisted"):
        FactSelectionDecision.model_validate(
            _decision().model_dump() | {"target_table": "documents"}
        )
    with pytest.raises(ValidationError, match="knowledge_at"):
        FactSelectionDecision.model_validate(
            _decision().model_dump() | {"knowledge_at": STAMP.replace(hour=21)}
        )


def test_ledger_validates_target_row_and_makes_exact_replay_a_noop(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    try:
        ledger = FactSelectionLedger(conn)
        first = ledger.persist(_decision())
        assert first.created is True
        assert ledger.persist(_decision()).created is False
        assert conn.execute(
            "SELECT selection_state, reason_code FROM v_fact_selection_current"
        ).fetchone() == ("excluded", "mis_scaled_count_unit")
        with pytest.raises(ValueError, match="does not exist"):
            ledger.persist(
                FactSelectionDecision.model_validate(
                    _decision().model_dump()
                    | {
                        "decision_id": "missing",
                        "idempotency_key": "missing",
                        "target_row_id": 99,
                    }
                )
            )
        with pytest.raises(ValueError, match="evidence node"):
            ledger.persist(
                FactSelectionDecision.model_validate(
                    _decision().model_dump()
                    | {
                        "decision_id": "missing-evidence",
                        "idempotency_key": "missing-evidence",
                        "evidence_node_id": "unknown-node",
                    }
                )
            )
    finally:
        conn.close()


def test_ledger_requires_a_same_target_revision_chain_and_is_append_only(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    try:
        ledger = FactSelectionLedger(conn)
        ledger.persist(_decision())
        ledger.persist(
            _decision(
                decision_id="exclude-kpi-7-r2",
                idempotency_key="exclude:kpi_facts:7:policy-v2",
                revision=2,
                supersedes_decision_id="exclude-kpi-7-r1",
                reason_code="corrected_policy_version",
            )
        )
        assert conn.execute(
            "SELECT decision_id, revision FROM v_fact_selection_current"
        ).fetchone() == ("exclude-kpi-7-r2", 2)
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute("UPDATE fact_selection_decisions SET reason_code = 'changed'")
        with pytest.raises(ValueError, match="same target"):
            ledger.persist(
                FactSelectionDecision.model_validate(
                    _decision(
                        decision_id="bad-chain",
                        idempotency_key="bad-chain",
                        revision=2,
                        supersedes_decision_id="exclude-kpi-7-r1",
                    ).model_dump()
                    | {"target_row_id": 8}
                )
            )
        with pytest.raises(ValueError, match="immediately prior"):
            ledger.persist(
                _decision(
                    decision_id="bad-revision",
                    idempotency_key="bad-revision",
                    revision=3,
                    supersedes_decision_id="exclude-kpi-7-r1",
                )
            )
    finally:
        conn.close()


def test_migration_round_trip_removes_only_the_selection_ledger(tmp_path: Path) -> None:
    path = tmp_path / "fact-selection-round-trip.db"
    config = _config(path)
    command.stamp(config, PRIOR)
    command.upgrade(config, HEAD)
    command.downgrade(config, PRIOR)
    conn = sqlite3.connect(path)
    try:
        assert (
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'fact_selection_decisions'"
            ).fetchone()
            is None
        )
    finally:
        conn.close()
