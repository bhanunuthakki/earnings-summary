"""VAL-SEARCH-001: sealed Ask fails closed truthfully when the substrate is absent.

Everything here is hermetic and synthetic: fixture-driven boot/config
diagnostics plus a fixture-driven sealed turn against pytest-local fixture
databases. No live Ask or Claude turn, no SEC or source fetch, no portfolio
or bench database, and no sealed-retrieval reachability claim — these tests
pin that a sealed selection with the required registry/vector substrate
absent is reported truthfully at boot, fails closed per turn, never silently
falls back to another retrieval lane, and never emits a successful-looking
answer. The production default stays ``grounded`` (VAL-SEARCH-010).

Setting ``SEARCH_VALIDATION_EVIDENCE_DIR`` records the contract's evidence
artifacts under that directory during the same pytest run; ordinary runs
write nothing outside pytest tmp.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

import ask.engine as engine
from ask.audit_store import canonical_json
from ask.context import ContextPack
from ask.engine import AskTurn
from ask.retrieval_boot_state import (
    assess_ask_retrieval_boot_state,
    log_ask_retrieval_boot_state,
)
from ask.sealed_retrieval import derive_production_scope_registry
from ask.store import ensure_session, load_turns
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite

_ASK_MODE_ENV = "ASK_RETRIEVAL_MODE"
_VECTOR_ENVS = (
    "EVIDENCE_VECTOR_RUNTIME_ENABLED",
    "EVIDENCE_VECTOR_INDEX_ROOT",
    "EVIDENCE_VECTOR_RUNTIME_ROOT",
)
_EVIDENCE_DIR_ENV = "SEARCH_VALIDATION_EVIDENCE_DIR"

# Markers a truthful unready diagnostic or fail-closed response must never
# carry — the negative half of the contract's "no readiness or success claim".
_READINESS_CLAIM_MARKERS = (
    "registry_status=valid",
    "vector_runtime_status=enabled",
    "vector_runtime_roots_present=True",
)
_SUCCESS_EVENT_TYPES = ("delta", "final", "citations", "stage")
_SEALED_TURN_ERROR = "sealed Ask could not produce a fully audited answer"

# The sealed plane's data substrate tables (mirrors the deployment blocker
# census in .tmp/search-validation/sealed-plane-reachability-blockers.json).
# A fresh migrated fixture DB carries zero rows in each — that is the
# "absent substrate" fixture definition.
_SUBSTRATE_TABLES = (
    "ask_retrieval_scope_promotions",
    "population_run_headers",
    "population_cutover_receipts",
    "research_snapshot_headers",
    "research_snapshot_admission_receipts",
    "canonical_fact_projection_generations",
    "search_corpus_manifests",
    "search_lexical_chunks",
    "search_embedding_artifacts",
    "search_embedding_runtime_registrations",
    "search_embedding_model_promotions",
)

_SCOPE_NOTE = (
    "Hermetic fixture evidence only: no live Ask/Claude turn, no SEC or source "
    "request, no portfolio or bench database access, and no claim that sealed "
    "retrieval is reachable or ready in this or any deployment."
)

_FALLBACK_LANES = (
    ("grounded narrative", "_grounded_narrative_events"),
    ("legacy narrative", "_narrative_events"),
    ("data", "_data_events"),
)


# The minimal live-cohort fixture ``derive_production_scope_registry`` reads
# (mirrors tests/test_ask_retrieval_boot_state.py's registry fixture; defined
# locally so this module owns its own hermetic fixture helpers).
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

_REGISTRY_COHORT_ROWS = """
INSERT INTO v_issuer_reporting_scope_current
  VALUES ('scope-revision-1','scope-1','issuer-1','core');
INSERT INTO issuer_entities VALUES ('issuer-1','operating_company');
INSERT INTO reporting_entities
  VALUES ('reporting-1','issuer-1','legal_registrant');
INSERT INTO v_security_listings_canonical
  VALUES ('issuer-1','ACME','listed');
"""


def _registry_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(_REGISTRY_SCHEMA)
    conn.executescript(_REGISTRY_COHORT_ROWS)
    return conn


def _write_registry(path: Path, conn: sqlite3.Connection) -> None:
    registry = derive_production_scope_registry(conn)
    path.write_text(canonical_json(registry) + "\n", encoding="utf-8")


def _reset_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(_ASK_MODE_ENV, raising=False)
    for name in _VECTOR_ENVS:
        monkeypatch.delenv(name, raising=False)


def _record_artifact(name: str, payload: dict[str, object]) -> None:
    """Record one VAL-SEARCH-001 evidence artifact when a validation run pins
    the evidence directory; ordinary test runs write nothing outside tmp."""
    root = os.environ.get(_EVIDENCE_DIR_ENV, "").strip()
    if not root:
        return
    path = Path(root) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _substrate_counts(db_path: Path) -> dict[str, int]:
    conn = connect_sqlite(db_path, role=SQLiteConnectionRole.READ_ONLY)
    try:
        return {
            table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in _SUBSTRATE_TABLES
        }
    finally:
        conn.close()


def _substrate_absent_fixture_configuration(registry_path: Path) -> dict[str, object]:
    return {
        "ask_retrieval_mode_env": _ASK_MODE_ENV,
        "production_scope_registry": {
            "path": str(registry_path),
            "exists": registry_path.exists(),
        },
        "vector_runtime_env": {name: os.environ.get(name) for name in _VECTOR_ENVS},
        "llm_boundary": "guarded — any LLM call records and raises; zero live calls",
        "network": "none — hermetic pytest fixtures only",
    }


def test_fixture_db_carries_no_sealed_plane_substrate(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fixture definition: a hermetic migrated DB with zero substrate rows."""

    db_path = migrated_db(tmp_path / "sealed-plane.db", target="head")
    _reset_env(monkeypatch)
    registry_path = tmp_path / "absent-registry.json"
    counts = _substrate_counts(db_path)
    assert counts == {table: 0 for table in _SUBSTRATE_TABLES}
    _record_artifact(
        "sealed-plane-val-search-001-fixture-definition.json",
        {
            "assertion": "VAL-SEARCH-001",
            "feature": "sealed-plane-reachability",
            "recorded_at": datetime.now(UTC).isoformat(),
            "scope_note": _SCOPE_NOTE,
            "fixture": {
                **_substrate_absent_fixture_configuration(registry_path),
                "ask_store_db": "hermetic migrated fixture DB (alembic head, pytest tmp)",
                "absent_substrate_components": counts,
            },
        },
    )


def test_substrate_absent_boot_state_names_the_missing_substrate_and_claims_no_readiness(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Boot diagnostics for the substrate-absent fixture state stay typed and
    truthful, and the sealed-selected line logs at ERROR, never at INFO."""

    _reset_env(monkeypatch)
    registry_path = tmp_path / "absent-registry.json"
    fixture_conn = _registry_db()

    grounded = assess_ask_retrieval_boot_state(fixture_conn, registry_path=registry_path)
    assert grounded.retrieval_mode == "grounded"
    assert grounded.mode_valid is True
    assert grounded.registry_status == "missing"
    assert str(registry_path) in grounded.registry_detail
    assert "does not exist" in grounded.registry_detail
    assert grounded.vector_runtime_status == "disabled"
    assert "EVIDENCE_VECTOR_RUNTIME_ENABLED" in grounded.vector_runtime_detail
    assert grounded.sealed_plane_ready is False

    with caplog.at_level(logging.INFO, logger="ask.retrieval_boot_state"):
        log_ask_retrieval_boot_state(grounded)
    grounded_records = [
        record for record in caplog.records if "ask_retrieval_boot_state" in record.getMessage()
    ]
    assert grounded_records and grounded_records[-1].levelno == logging.INFO
    grounded_line = grounded_records[-1].getMessage()

    caplog.clear()
    monkeypatch.setenv(_ASK_MODE_ENV, "sealed")
    sealed = assess_ask_retrieval_boot_state(fixture_conn, registry_path=registry_path)
    assert sealed.retrieval_mode == "sealed"
    assert sealed.registry_status == "missing"
    assert sealed.vector_runtime_status == "disabled"
    assert sealed.sealed_plane_ready is False

    with caplog.at_level(logging.INFO, logger="ask.retrieval_boot_state"):
        log_ask_retrieval_boot_state(sealed)
    sealed_records = [
        record for record in caplog.records if "ask_retrieval_boot_state" in record.getMessage()
    ]
    assert sealed_records and sealed_records[-1].levelno == logging.ERROR
    sealed_line = sealed_records[-1].getMessage()

    claim_markers_found: list[str] = []
    for line in (grounded_line, sealed_line):
        assert "registry_status=missing" in line
        assert "vector_runtime_status=disabled" in line
        claim_markers_found.extend(marker for marker in _READINESS_CLAIM_MARKERS if marker in line)
    assert claim_markers_found == []

    _record_artifact(
        "sealed-plane-val-search-001-boot-state.json",
        {
            "assertion": "VAL-SEARCH-001",
            "feature": "sealed-plane-reachability",
            "recorded_at": datetime.now(UTC).isoformat(),
            "scope_note": _SCOPE_NOTE,
            "fixture_state": "substrate absent: registry file missing, vector runtime env unset",
            "fixture": _substrate_absent_fixture_configuration(registry_path),
            "grounded_default_boot": {
                "state": grounded.model_dump(mode="json"),
                "level": "INFO",
                "message": grounded_line,
            },
            "sealed_selected_boot": {
                "state": sealed.model_dump(mode="json"),
                "level": "ERROR",
                "message": sealed_line,
            },
            "negative_check": {
                "readiness_claim_markers_searched": list(_READINESS_CLAIM_MARKERS),
                "readiness_claim_markers_found": claim_markers_found,
                "note": (
                    "both boot lines name the missing substrate and carry no "
                    "readiness claim; the sealed-selected line logs at ERROR"
                ),
            },
        },
    )


def _armed_lanes(monkeypatch: pytest.MonkeyPatch) -> tuple[list[str], list[str]]:
    """Guard every other retrieval lane plus the LLM seam; each records the
    attempt (the no-fallback evidence) and raises (a fallback can never
    succeed silently)."""

    lanes_invoked: list[str] = []

    def _guard(lane: str) -> Callable[..., object]:
        def forbidden(*_args: object, **_kwargs: object) -> object:
            lanes_invoked.append(lane)
            raise AssertionError(f"sealed Ask must not fall back to the {lane} lane")

        return forbidden

    for lane, attr in _FALLBACK_LANES:
        monkeypatch.setattr(engine, attr, _guard(lane))

    llm_calls: list[str] = []

    def forbidden_llm(*_args: object, **_kwargs: object) -> str:
        llm_calls.append("call_llm")
        raise AssertionError("sealed Ask must not call an LLM when the substrate is absent")

    monkeypatch.setattr(engine, "call_llm", forbidden_llm)
    return lanes_invoked, llm_calls


def _drive_sealed_turn(
    db_path: Path,
    session_id: str,
    *,
    repo_root: Path,
) -> tuple[list[dict[str, object]], str]:
    """Mirror the cockpit's exact sealed call: resolve the mode from the env,
    then run one fixture-driven narrative turn."""

    question = "Why did margins fall at ACME?"
    turn = AskTurn(text=question, session_id=session_id)
    pack = ContextPack(scope="portfolio", default_tickers=["ACME"], system_context="context")
    events = list(
        engine.respond_turn(
            turn,
            pack,
            db_path=db_path,
            repo_root=repo_root,
            retrieval_mode=engine.ask_retrieval_mode(),
        )
    )
    return events, question


def test_sealed_turn_fails_closed_when_the_registry_substrate_is_absent(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A sealed turn against the substrate-absent fixture fails closed: one
    typed error event, the failure logged at ERROR naming the missing
    registry, no other lane, no LLM call, and no answer persisted."""

    db_path = migrated_db(tmp_path / "sealed-plane.db", target="head")
    session = ensure_session(None, scope="portfolio", db_path=db_path)
    registry_path = tmp_path / "absent-registry.json"
    _reset_env(monkeypatch)
    monkeypatch.setenv(_ASK_MODE_ENV, "sealed")
    monkeypatch.setattr(engine, "_PRODUCTION_SCOPE_REGISTRY", registry_path)
    lanes_invoked, llm_calls = _armed_lanes(monkeypatch)

    with caplog.at_level(logging.ERROR, logger="ask.engine"):
        events, question = _drive_sealed_turn(db_path, session.id, repo_root=tmp_path)

    assert events == [{"type": "error", "error": _SEALED_TURN_ERROR}]
    assert lanes_invoked == []
    assert llm_calls == []
    emitted_types = [str(event.get("type")) for event in events]
    found_success_types = [kind for kind in _SUCCESS_EVENT_TYPES if kind in emitted_types]
    assert found_success_types == []

    failure_records = [
        record for record in caplog.records if "ask_sealed_answer_failed" in record.getMessage()
    ]
    assert failure_records and failure_records[-1].levelno == logging.ERROR
    failure_message = failure_records[-1].getMessage()
    assert "production scope registry is unavailable" in failure_message
    assert str(registry_path) in failure_message
    claim_markers_found = [
        marker for marker in _READINESS_CLAIM_MARKERS if marker in failure_message
    ]
    assert claim_markers_found == []

    rows = load_turns(session.id, db_path=db_path)
    assert [row.role for row in rows] == ["user"]
    assert rows[0].text == question

    _record_artifact(
        "sealed-plane-val-search-001-turn-outcome.json",
        {
            "assertion": "VAL-SEARCH-001",
            "feature": "sealed-plane-reachability",
            "recorded_at": datetime.now(UTC).isoformat(),
            "scope_note": _SCOPE_NOTE,
            "case": "production scope registry absent",
            "fixture": _substrate_absent_fixture_configuration(registry_path),
            "request": {
                "mode": "sealed",
                "question": question,
                "tickers": ["ACME"],
                "session_scope": "portfolio",
            },
            "outcome": {
                "failed_closed": True,
                "events": events,
                "failure_log": {
                    "event": "ask_sealed_answer_failed",
                    "level": "ERROR",
                    "message": failure_message,
                },
                "fallback_lanes_invoked": lanes_invoked,
                "llm_calls": llm_calls,
                "assistant_turns_persisted": 0,
                "store_tail_roles": [row.role for row in rows],
            },
            "negative_check": {
                "readiness_claim_markers_searched": list(_READINESS_CLAIM_MARKERS),
                "readiness_claim_markers_found": claim_markers_found,
                "success_event_types_searched": list(_SUCCESS_EVENT_TYPES),
                "success_event_types_found": found_success_types,
                "note": (
                    "no fallback lane was used, no LLM call ran, and no readiness "
                    "or success claim appears in the failure log or the response"
                ),
            },
        },
    )


def test_sealed_turn_fails_closed_when_the_committed_registry_cannot_derive_the_live_cohort(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The deployment's real blocker shape: a committed registry exists, but
    the live cohort cannot derive it (empty scope cohort) — the sealed turn
    still fails closed instead of degrading to another lane."""

    db_path = migrated_db(tmp_path / "sealed-plane.db", target="head")
    session = ensure_session(None, scope="portfolio", db_path=db_path)
    registry_path = tmp_path / "ask_retrieval_production_scopes.json"
    _write_registry(registry_path, _registry_db())
    _reset_env(monkeypatch)
    monkeypatch.setenv(_ASK_MODE_ENV, "sealed")
    monkeypatch.setattr(engine, "_PRODUCTION_SCOPE_REGISTRY", registry_path)
    lanes_invoked, llm_calls = _armed_lanes(monkeypatch)

    with caplog.at_level(logging.ERROR, logger="ask.engine"):
        events, question = _drive_sealed_turn(db_path, session.id, repo_root=tmp_path)

    assert events == [{"type": "error", "error": _SEALED_TURN_ERROR}]
    assert lanes_invoked == []
    assert llm_calls == []

    failure_records = [
        record for record in caplog.records if "ask_sealed_answer_failed" in record.getMessage()
    ]
    assert failure_records and failure_records[-1].levelno == logging.ERROR
    failure_message = failure_records[-1].getMessage()
    assert "canonical registry has no enabled core Ask scopes" in failure_message

    rows = load_turns(session.id, db_path=db_path)
    assert [row.role for row in rows] == ["user"]
    assert rows[0].text == question

    _record_artifact(
        "sealed-plane-val-search-001-turn-outcome-underivable-cohort.json",
        {
            "assertion": "VAL-SEARCH-001",
            "feature": "sealed-plane-reachability",
            "recorded_at": datetime.now(UTC).isoformat(),
            "scope_note": _SCOPE_NOTE,
            "case": "committed registry present but the live cohort is underivable",
            "fixture": {
                "production_scope_registry": {
                    "path": str(registry_path),
                    "exists": registry_path.exists(),
                    "committed_from": "seeded synthetic cohort fixture (one issuer)",
                },
                "live_cohort": "hermetic migrated fixture DB with an empty scope cohort",
                "vector_runtime_env": {name: os.environ.get(name) for name in _VECTOR_ENVS},
            },
            "request": {
                "mode": "sealed",
                "question": question,
                "tickers": ["ACME"],
                "session_scope": "portfolio",
            },
            "outcome": {
                "failed_closed": True,
                "events": events,
                "failure_log": {
                    "event": "ask_sealed_answer_failed",
                    "level": "ERROR",
                    "message": failure_message,
                },
                "fallback_lanes_invoked": lanes_invoked,
                "llm_calls": llm_calls,
                "assistant_turns_persisted": 0,
                "store_tail_roles": [row.role for row in rows],
            },
            "negative_check": {
                "readiness_claim_markers_searched": list(_READINESS_CLAIM_MARKERS),
                "readiness_claim_markers_found": [
                    marker for marker in _READINESS_CLAIM_MARKERS if marker in failure_message
                ],
                "success_event_types_searched": list(_SUCCESS_EVENT_TYPES),
                "success_event_types_found": [
                    kind
                    for kind in _SUCCESS_EVENT_TYPES
                    if kind in [str(event.get("type")) for event in events]
                ],
                "note": "same fail-closed contract as the registry-absent case",
            },
        },
    )


def test_mode_unset_selects_grounded_and_the_grounded_lane_serves_the_turn(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """VAL-SEARCH-010's invariant on the same substrate-absent fixture: with
    ASK_RETRIEVAL_MODE unset the turn selects grounded and the grounded
    narrative lane (not sealed) serves it."""

    db_path = migrated_db(tmp_path / "sealed-plane.db", target="head")
    monkeypatch.delenv(_ASK_MODE_ENV, raising=False)
    for name in _VECTOR_ENVS:
        monkeypatch.delenv(name, raising=False)
    assert engine.ask_retrieval_mode() == "grounded"

    sentinel_events: list[dict[str, object]] = [
        {"type": "final", "text": "grounded-sentinel", "route": "narrative"}
    ]

    def fake_grounded_narrative(*_args: object, **_kwargs: object) -> Iterator[dict[str, object]]:
        yield from sentinel_events

    def forbidden_sealed(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("grounded Ask must not run the sealed/shadow path")

    def forbidden_llm(*_args: object, **_kwargs: object) -> str:
        raise AssertionError("the sentinel grounded lane must not call an LLM")

    def empty_catalog(*_args: object, **_kwargs: object) -> dict[str, list[dict[str, object]]]:
        return {}

    def empty_tickers(*_args: object, **_kwargs: object) -> set[str]:
        return set()

    monkeypatch.setattr(engine, "_grounded_narrative_events", fake_grounded_narrative)
    monkeypatch.setattr(engine, "_sealed_or_shadow_narrative_events", forbidden_sealed)
    monkeypatch.setattr(engine, "call_llm", forbidden_llm)
    monkeypatch.setattr(engine, "metric_catalog", empty_catalog)
    monkeypatch.setattr(engine, "tracked_tickers", empty_tickers)

    turn = AskTurn(text="Why did margins fall at ACME?")
    pack = ContextPack(scope="portfolio", default_tickers=["ACME"], system_context="context")
    events = list(
        engine.respond_turn(
            turn,
            pack,
            db_path=db_path,
            repo_root=tmp_path,
            retrieval_mode=engine.ask_retrieval_mode(),
        )
    )

    assert events == sentinel_events
