"""Boot-time reachability state for the sealed Ask retrieval plane.

The production scope registry, the retrieval mode, and the vector-runtime
gate must be deliberate: missing or invalid config fails closed per turn and
is reported at boot. These tests pin the assessment contract — no exception
escapes, every degraded state is typed, and the default Ask mode stays
``grounded``.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path

import pytest

from ask.audit_store import canonical_json
from ask.retrieval_boot_state import (
    AskRetrievalBootState,
    assess_ask_retrieval_boot_state,
    assess_ask_retrieval_boot_state_from_path,
    log_ask_retrieval_boot_state,
)
from ask.sealed_retrieval import derive_production_scope_registry

_ASK_MODE_ENV = "ASK_RETRIEVAL_MODE"
_VECTOR_ENVS = (
    "EVIDENCE_VECTOR_RUNTIME_ENABLED",
    "EVIDENCE_VECTOR_INDEX_ROOT",
    "EVIDENCE_VECTOR_RUNTIME_ROOT",
)

_REGISTRY_SCHEMA = """
CREATE TABLE v_issuer_reporting_scope_current (
    scope_revision_id TEXT,
    scope_key TEXT,
    issuer_id TEXT,
    inclusion_state TEXT
);
CREATE TABLE issuer_entities (
    issuer_id TEXT,
    entity_kind TEXT
);
CREATE TABLE reporting_entities (
    reporting_entity_id TEXT,
    issuer_id TEXT,
    reporting_entity_kind TEXT
);
CREATE TABLE v_security_listings_canonical (
    issuer_id TEXT,
    normalized_ticker TEXT,
    status TEXT
);
"""

_CORE_ROWS = {
    "seed": """
INSERT INTO v_issuer_reporting_scope_current
  VALUES ('scope-revision-1','scope-1','issuer-1','core');
INSERT INTO issuer_entities VALUES ('issuer-1','operating_company');
INSERT INTO reporting_entities
  VALUES ('reporting-1','issuer-1','legal_registrant');
INSERT INTO v_security_listings_canonical
  VALUES ('issuer-1','ACME','listed');
"""
}


def _registry_db(*, with_listings: bool = True) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(_REGISTRY_SCHEMA)
    conn.executescript(_CORE_ROWS["seed"])
    if not with_listings:
        conn.execute("DELETE FROM v_security_listings_canonical")
    return conn


def _reset_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(_ASK_MODE_ENV, raising=False)
    for name in _VECTOR_ENVS:
        monkeypatch.delenv(name, raising=False)


def _enable_runtime(monkeypatch: pytest.MonkeyPatch, index_root: Path, runtime_root: Path) -> None:
    monkeypatch.setenv("EVIDENCE_VECTOR_RUNTIME_ENABLED", "1")
    monkeypatch.setenv("EVIDENCE_VECTOR_INDEX_ROOT", str(index_root))
    monkeypatch.setenv("EVIDENCE_VECTOR_RUNTIME_ROOT", str(runtime_root))


def _write_registry(path: Path, conn: sqlite3.Connection) -> None:
    registry = derive_production_scope_registry(conn)
    path.write_text(canonical_json(registry) + "\n", encoding="utf-8")


def test_valid_registry_and_enabled_runtime_report_a_ready_plane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _reset_env(monkeypatch)
    registry_path = tmp_path / "ask_retrieval_production_scopes.json"
    _write_registry(registry_path, _registry_db())
    index_root = tmp_path / "index"
    runtime_root = tmp_path / "runtime"
    index_root.mkdir()
    runtime_root.mkdir()
    monkeypatch.setenv(_ASK_MODE_ENV, "sealed")
    _enable_runtime(monkeypatch, index_root, runtime_root)

    state = assess_ask_retrieval_boot_state(_registry_db(), registry_path=registry_path)

    assert state.mode_valid is True
    assert state.retrieval_mode == "sealed"
    assert state.registry_status == "valid"
    assert state.registry_scope_count == 1
    assert state.vector_runtime_status == "enabled"
    assert state.vector_runtime_roots_present is True
    assert state.sealed_plane_ready is True


def test_missing_registry_file_fails_closed_as_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _reset_env(monkeypatch)
    state = assess_ask_retrieval_boot_state(_registry_db(), registry_path=tmp_path / "absent.json")
    assert state.registry_status == "missing"
    assert state.registry_scope_count is None
    assert state.sealed_plane_ready is False


def test_tampered_registry_commitment_fails_closed_as_invalid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _reset_env(monkeypatch)
    conn = _registry_db()
    registry_path = tmp_path / "ask_retrieval_production_scopes.json"
    _write_registry(registry_path, conn)
    tampered = derive_production_scope_registry(conn)
    tampered["source_scope_revision_ids"] = ["scope-revision-other"]
    registry_path.write_text(canonical_json(tampered) + "\n", encoding="utf-8")

    state = assess_ask_retrieval_boot_state(conn, registry_path=registry_path)

    assert state.registry_status == "invalid"
    assert "commitment mismatch" in state.registry_detail
    assert state.sealed_plane_ready is False


def test_registry_from_a_different_cohort_is_cohort_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _reset_env(monkeypatch)
    registry_path = tmp_path / "ask_retrieval_production_scopes.json"
    _write_registry(registry_path, _registry_db())

    other = sqlite3.connect(":memory:")
    other.executescript(_REGISTRY_SCHEMA)
    other.executescript(
        """
INSERT INTO v_issuer_reporting_scope_current
  VALUES ('scope-revision-9','scope-1','issuer-9','core');
INSERT INTO issuer_entities VALUES ('issuer-9','operating_company');
INSERT INTO reporting_entities
  VALUES ('reporting-9','issuer-9','legal_registrant');
INSERT INTO v_security_listings_canonical
  VALUES ('issuer-9','BETA','listed');
"""
    )
    state = assess_ask_retrieval_boot_state(other, registry_path=registry_path)

    assert state.registry_status == "cohort_mismatch"
    assert state.sealed_plane_ready is False


def test_underivable_live_cohort_fails_closed_as_cohort_underivable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _reset_env(monkeypatch)
    conn = _registry_db(with_listings=False)
    registry_path = tmp_path / "ask_retrieval_production_scopes.json"
    _write_registry(registry_path, _registry_db())

    state = assess_ask_retrieval_boot_state(conn, registry_path=registry_path)

    assert state.registry_status == "cohort_underivable"
    assert "missing, duplicate, or unsupported" in state.registry_detail
    assert state.sealed_plane_ready is False


def test_vector_runtime_disabled_by_default_is_reported_not_hidden(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _reset_env(monkeypatch)
    state = assess_ask_retrieval_boot_state(_registry_db())
    assert state.vector_runtime_status == "disabled"
    assert "EVIDENCE_VECTOR_RUNTIME_ENABLED" in state.vector_runtime_detail
    assert state.vector_runtime_roots_present is None
    assert state.sealed_plane_ready is False


def test_enabled_runtime_without_roots_is_invalid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _reset_env(monkeypatch)
    monkeypatch.setenv("EVIDENCE_VECTOR_RUNTIME_ENABLED", "1")
    state = assess_ask_retrieval_boot_state(_registry_db())
    assert state.vector_runtime_status == "invalid"
    assert "EVIDENCE_VECTOR_INDEX_ROOT" in state.vector_runtime_detail
    assert "EVIDENCE_VECTOR_RUNTIME_ROOT" in state.vector_runtime_detail


def test_enabled_runtime_with_absent_roots_reports_roots_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _reset_env(monkeypatch)
    _enable_runtime(monkeypatch, tmp_path / "index", tmp_path / "runtime")
    state = assess_ask_retrieval_boot_state(_registry_db())
    assert state.vector_runtime_status == "enabled"
    assert state.vector_runtime_roots_present is False
    assert state.sealed_plane_ready is False


def test_invalid_retrieval_mode_is_reported_not_raised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _reset_env(monkeypatch)
    monkeypatch.setenv(_ASK_MODE_ENV, "speculative")
    state = assess_ask_retrieval_boot_state(_registry_db())
    assert state.retrieval_mode == "speculative"
    assert state.mode_valid is False
    assert "must be grounded, legacy, shadow, or sealed" in state.mode_detail


def test_default_mode_remains_grounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _reset_env(monkeypatch)
    state = assess_ask_retrieval_boot_state(_registry_db())
    assert state.mode_valid is True
    assert state.retrieval_mode == "grounded"


def test_sealed_selected_but_unready_logs_at_error_while_grounded_logs_info(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _reset_env(monkeypatch)
    registry_path = tmp_path / "ask_retrieval_production_scopes.json"
    _write_registry(registry_path, _registry_db())
    grounded = assess_ask_retrieval_boot_state(_registry_db(), registry_path=registry_path)
    with caplog.at_level(logging.INFO, logger="ask.retrieval_boot_state"):
        log_ask_retrieval_boot_state(grounded)
    info_records = [r for r in caplog.records if "ask_retrieval_boot_state" in r.message]
    assert info_records and info_records[0].levelno == logging.INFO

    caplog.clear()
    monkeypatch.setenv(_ASK_MODE_ENV, "sealed")
    sealed_unready = assess_ask_retrieval_boot_state(_registry_db(), registry_path=registry_path)
    with caplog.at_level(logging.INFO, logger="ask.retrieval_boot_state"):
        log_ask_retrieval_boot_state(sealed_unready)
    error_records = [r for r in caplog.records if "ask_retrieval_boot_state" in r.message]
    assert error_records
    assert error_records[0].levelno == logging.ERROR
    message = error_records[0].getMessage()
    assert "mode=sealed" in message
    assert "registry_status=valid" in message
    assert "vector_runtime_status=disabled" in message


def test_state_model_round_trips_through_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _reset_env(monkeypatch)
    state = assess_ask_retrieval_boot_state(_registry_db())
    restored = AskRetrievalBootState.model_validate(json.loads(state.model_dump_json()))
    assert restored == state


def test_assessment_from_db_path_opens_one_read_only_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _reset_env(monkeypatch)
    db_path = tmp_path / "scopes.sqlite"
    conn = sqlite3.connect(db_path)
    conn.executescript(_REGISTRY_SCHEMA)
    conn.executescript(_CORE_ROWS["seed"])
    conn.commit()
    conn.close()
    registry_path = tmp_path / "ask_retrieval_production_scopes.json"
    write_conn = sqlite3.connect(db_path)
    try:
        _write_registry(registry_path, write_conn)
    finally:
        write_conn.close()

    state = assess_ask_retrieval_boot_state_from_path(db_path, registry_path=registry_path)

    assert state.registry_status == "valid"
    assert state.registry_scope_count == 1
