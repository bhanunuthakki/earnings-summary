"""Contracts for immutable selection decisions over legacy fact rows."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from provenance.fact_selection import FactSelectionDecision, FactSelectionLedger
from tests.evidence_support import seed_evidence_document, seed_issuer

STAMP = datetime(2026, 7, 26, 22, 0, 0)
SHA = "a" * 64


def _seed_kpi_facts(conn: sqlite3.Connection, facts: tuple[tuple[int, str, float], ...]) -> None:
    """Insert an evidence-backed source document, definition and the KPI rows under test."""
    seed_issuer(conn, "issuer-acme")
    seed_evidence_document(
        conn,
        doc_id=1,
        ticker="ACME",
        issuer_id="issuer-acme",
        period_end="2025-12-31",
        file_path="acme.htm",
        sha_char="0",
    )
    conn.execute(
        "INSERT INTO kpi_definitions (id, ticker, name, primary_source, definition_origin) "
        "VALUES (9001, 'ACME', 'Authorized Shares (in shares)', 'sec', 'capture')"
    )
    conn.executemany(
        "INSERT INTO kpi_facts (id, ticker, period_end, fiscal_period_type, "
        "kpi_definition_id, value, unit, source_doc_id) "
        "VALUES (?, 'ACME', ?, 'Q4', 9001, ?, ?, 1)",
        [
            (fact_id, f"{2025 - index}-12-31", value, unit)
            for index, (fact_id, unit, value) in enumerate(facts)
        ],
    )


def _conn(tmp_path: Path, migrated_db: Callable[..., Path]) -> sqlite3.Connection:
    path = tmp_path / "fact-selection.db"
    migrated_db(path)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = ON")
    _seed_kpi_facts(conn, ((7, "actual", 24000000000), (8, "actual", 12)))
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


def test_ledger_validates_target_row_and_makes_exact_replay_a_noop(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    conn = _conn(tmp_path, migrated_db)
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


def test_ledger_requires_a_same_target_revision_chain_and_is_append_only(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    conn = _conn(tmp_path, migrated_db)
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
