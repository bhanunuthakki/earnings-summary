"""Tests for src/entity_store.py and the migration 0036 spine."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

import entity_store
from entity_store import (
    decide_proposal,
    get_entity,
    pending_proposals,
    propose_mapping,
    record_alias,
    record_concept_alias,
    record_concept_definition,
    record_extraction,
    record_mention,
    resolve_concept,
    resolve_entity,
    upsert_concept,
    upsert_entity,
    upsert_relationship,
)


def _schema(conn: sqlite3.Connection) -> None:
    """Mirror migration 0036 inline for hermetic tests."""
    conn.executescript(
        """
        CREATE TABLE entities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind VARCHAR(32) NOT NULL,
            canonical_name VARCHAR(255) NOT NULL,
            display_name VARCHAR(255),
            external_ids TEXT,
            parent_entity_id INTEGER REFERENCES entities(id),
            meta_json TEXT,
            effective_from DATETIME, effective_to DATETIME,
            created_at DATETIME NOT NULL,
            last_observed_at DATETIME,
            UNIQUE(kind, canonical_name)
        );
        CREATE TABLE entity_aliases (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            entity_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
            alias_text VARCHAR(255) NOT NULL,
            alias_kind VARCHAR(32) NOT NULL,
            first_observed_at DATETIME, last_observed_at DATETIME,
            observation_count INTEGER NOT NULL DEFAULT 1,
            confidence FLOAT NOT NULL DEFAULT 1.0,
            exemplar_source_doc_id INTEGER, exemplar_excerpt TEXT,
            UNIQUE(entity_id, alias_text)
        );
        CREATE TABLE entity_relationships (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            from_entity_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
            relationship_kind VARCHAR(48) NOT NULL,
            to_entity_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
            effective_from DATETIME, effective_to DATETIME,
            evidence_doc_id INTEGER, evidence_excerpt TEXT,
            confidence FLOAT NOT NULL DEFAULT 1.0,
            meta_json TEXT
        );
        CREATE TABLE entity_mentions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_doc_id INTEGER NOT NULL,
            entity_id INTEGER REFERENCES entities(id),
            char_offset_start INTEGER, char_offset_end INTEGER,
            mention_text VARCHAR(255) NOT NULL,
            surrounding_context TEXT,
            extractor_id VARCHAR(64) NOT NULL,
            extractor_version VARCHAR(16) NOT NULL,
            confidence FLOAT NOT NULL DEFAULT 1.0,
            unresolved_alias_text VARCHAR(255)
        );
        CREATE TABLE concepts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind VARCHAR(32) NOT NULL,
            canonical_name VARCHAR(128) NOT NULL,
            unit_kind VARCHAR(32),
            taxonomy_xbrl_tag VARCHAR(128),
            generic_definition_md TEXT,
            computation_kind VARCHAR(32),
            computation_formula_md TEXT,
            created_at DATETIME NOT NULL,
            UNIQUE(canonical_name)
        );
        CREATE TABLE concept_aliases (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            concept_id INTEGER NOT NULL REFERENCES concepts(id) ON DELETE CASCADE,
            ticker VARCHAR(16),
            alias_text VARCHAR(255) NOT NULL,
            alias_kind VARCHAR(32) NOT NULL,
            first_observed_at DATETIME, last_observed_at DATETIME,
            confidence FLOAT NOT NULL DEFAULT 1.0,
            UNIQUE(concept_id, ticker, alias_text)
        );
        CREATE TABLE concept_definitions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            concept_id INTEGER NOT NULL REFERENCES concepts(id),
            ticker VARCHAR(16) NOT NULL,
            effective_from DATETIME NOT NULL,
            effective_to DATETIME,
            definition_md TEXT NOT NULL,
            evidence_doc_id INTEGER, evidence_excerpt TEXT,
            computation_change_md TEXT,
            superseded_by_id INTEGER REFERENCES concept_definitions(id)
        );
        CREATE TABLE extractions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_doc_id INTEGER NOT NULL,
            char_offset_start INTEGER, char_offset_end INTEGER,
            extraction_kind VARCHAR(48) NOT NULL,
            extractor_id VARCHAR(64) NOT NULL,
            extractor_version VARCHAR(16) NOT NULL,
            payload_json TEXT NOT NULL,
            confidence FLOAT NOT NULL DEFAULT 1.0,
            extracted_at DATETIME NOT NULL,
            superseded_by_extraction_id INTEGER REFERENCES extractions(id),
            links_to_json TEXT
        );
        CREATE TABLE mapping_proposals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind VARCHAR(48) NOT NULL,
            ticker VARCHAR(16),
            proposed_by VARCHAR(64) NOT NULL,
            payload_json TEXT NOT NULL,
            confidence FLOAT NOT NULL,
            source_doc_id INTEGER, source_excerpt TEXT,
            status VARCHAR(24) NOT NULL DEFAULT 'pending_review',
            applied_at DATETIME, applied_to_entity_id INTEGER, applied_to_concept_id INTEGER,
            decided_at DATETIME, decided_by VARCHAR(64), decision_notes TEXT,
            created_at DATETIME NOT NULL
        );
        """
    )
    conn.commit()


@pytest.fixture()
def db(tmp_path: Path) -> Path:
    p = tmp_path / "spine.db"
    conn = sqlite3.connect(str(p))
    try:
        _schema(conn)
    finally:
        conn.close()
    return p


# ---------------------------------------------------------------------------
# Entities
# ---------------------------------------------------------------------------


def test_upsert_entity_creates_with_self_alias(db: Path) -> None:
    eid = upsert_entity(kind="company", canonical_name="Alphabet Inc.", db_path=db)
    assert eid is not None and eid > 0

    # resolve by canonical name should work immediately (self-alias)
    assert resolve_entity("Alphabet Inc.", db_path=db) == eid
    assert resolve_entity("alphabet inc.", db_path=db) == eid  # case-insensitive


def test_upsert_entity_dedups_same_kind_name(db: Path) -> None:
    e1 = upsert_entity(kind="company", canonical_name="Alphabet Inc.", db_path=db)
    e2 = upsert_entity(kind="company", canonical_name="Alphabet Inc.", db_path=db)
    assert e1 == e2


def test_upsert_entity_kind_namespace_independent(db: Path) -> None:
    # Same name in two kinds → two distinct entities
    eid_co = upsert_entity(kind="company", canonical_name="Cloud", db_path=db)
    eid_seg = upsert_entity(kind="segment", canonical_name="Cloud", db_path=db)
    assert eid_co != eid_seg


class _TrackingConnection(sqlite3.Connection):
    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        self.commit_calls = 0
        self.close_calls = 0

    def commit(self) -> None:
        self.commit_calls += 1
        super().commit()

    def close(self) -> None:
        self.close_calls += 1
        super().close()


def test_borrowed_entity_writers_leave_transaction_ownership_to_caller(db: Path) -> None:
    conn = sqlite3.connect(str(db), factory=_TrackingConnection)
    company_id = upsert_entity(kind="company", canonical_name="Alphabet", conn=conn)
    segment_id = upsert_entity(
        kind="segment",
        canonical_name="Alphabet:Cloud",
        parent_entity_id=company_id,
        conn=conn,
    )
    alias_id = record_alias(entity_id=segment_id, alias_text="Cloud", conn=conn)
    relationship_id = upsert_relationship(
        from_entity_id=segment_id,
        relationship_kind="segment_of",
        to_entity_id=company_id,
        conn=conn,
    )

    assert company_id and segment_id and alias_id and relationship_id
    assert conn.commit_calls == 0
    assert conn.close_calls == 0
    assert conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0] == 2
    conn.rollback()
    conn.close()


def test_default_entity_writer_owns_commit_and_close(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = _TrackingConnection(":memory:")
    _schema(conn)
    conn.commit_calls = 0
    monkeypatch.setattr(entity_store, "_open", lambda _db_path: conn)

    assert upsert_entity(kind="company", canonical_name="Alphabet") is not None
    assert conn.commit_calls == 1
    assert conn.close_calls == 1


def test_get_entity_round_trips_metadata(db: Path) -> None:
    eid = upsert_entity(
        kind="company",
        canonical_name="Alphabet",
        display_name="Alphabet Inc.",
        external_ids={"cik": "0001652044", "ticker": "GOOG"},
        meta={"sector": "Communication Services"},
        db_path=db,
    )
    e = get_entity(eid, db_path=db)
    assert e is not None
    assert e.canonical_name == "Alphabet"
    assert e.external_ids == {"cik": "0001652044", "ticker": "GOOG"}
    assert e.meta == {"sector": "Communication Services"}


# ---------------------------------------------------------------------------
# Aliases — observation counting
# ---------------------------------------------------------------------------


def test_record_alias_increments_count_on_duplicate(db: Path) -> None:
    eid = upsert_entity(kind="company", canonical_name="Alphabet", db_path=db)
    aid1 = record_alias(entity_id=eid, alias_text="Google", db_path=db)
    aid2 = record_alias(entity_id=eid, alias_text="Google", db_path=db)
    assert aid1 == aid2  # same row

    conn = sqlite3.connect(str(db))
    try:
        row = conn.execute(
            "SELECT observation_count FROM entity_aliases WHERE id = ?", (aid1,)
        ).fetchone()
        assert row[0] == 2  # incremented from initial 1
    finally:
        conn.close()


def test_resolve_entity_picks_highest_confidence(db: Path) -> None:
    eid = upsert_entity(kind="company", canonical_name="Brookfield Corp", db_path=db)
    record_alias(entity_id=eid, alias_text="BN", confidence=0.95, db_path=db)
    # A lower-confidence alias for the same text on a different entity
    other = upsert_entity(kind="ticker", canonical_name="BN-ticker", db_path=db)
    record_alias(entity_id=other, alias_text="BN", confidence=0.30, db_path=db)

    resolved = resolve_entity("BN", db_path=db)
    assert resolved == eid


# ---------------------------------------------------------------------------
# Mentions
# ---------------------------------------------------------------------------


def test_record_mention_stores_provenance(db: Path) -> None:
    eid = upsert_entity(kind="company", canonical_name="NVIDIA", db_path=db)
    mid = record_mention(
        source_doc_id=42,
        entity_id=eid,
        mention_text="NVIDIA",
        extractor_id="llm_entity_resolver",
        extractor_version="v1",
        char_offset_start=120,
        char_offset_end=126,
        surrounding_context="...dependence on NVIDIA capacity for cloud expansion...",
        db_path=db,
    )
    assert mid is not None and mid > 0


def test_record_mention_handles_unresolved(db: Path) -> None:
    mid = record_mention(
        source_doc_id=42,
        entity_id=None,
        mention_text="some custom name",
        extractor_id="llm_entity_resolver",
        extractor_version="v1",
        unresolved_alias_text="some custom name",
        confidence=0.3,
        db_path=db,
    )
    assert mid is not None
    conn = sqlite3.connect(str(db))
    try:
        row = conn.execute(
            "SELECT entity_id, unresolved_alias_text FROM entity_mentions WHERE id = ?", (mid,)
        ).fetchone()
        assert row[0] is None
        assert row[1] == "some custom name"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Relationships
# ---------------------------------------------------------------------------


def test_upsert_relationship_dedups_same_edge(db: Path) -> None:
    co = upsert_entity(kind="company", canonical_name="Alphabet", db_path=db)
    seg = upsert_entity(kind="segment", canonical_name="Google Cloud", db_path=db)
    r1 = upsert_relationship(
        from_entity_id=seg,
        relationship_kind="segment_of",
        to_entity_id=co,
        effective_from=datetime(2015, 1, 1, tzinfo=UTC),
        db_path=db,
    )
    r2 = upsert_relationship(
        from_entity_id=seg,
        relationship_kind="segment_of",
        to_entity_id=co,
        effective_from=datetime(2015, 1, 1, tzinfo=UTC),
        db_path=db,
    )
    assert r1 == r2


# ---------------------------------------------------------------------------
# Concepts
# ---------------------------------------------------------------------------


def test_upsert_concept_creates_with_self_alias(db: Path) -> None:
    cid = upsert_concept(canonical_name="Active Customers", kind="kpi", db_path=db)
    assert cid is not None
    assert resolve_concept("Active Customers", db_path=db) == cid


def test_resolve_concept_ticker_specific_wins(db: Path) -> None:
    cid_universal = upsert_concept(canonical_name="Customers", db_path=db)
    cid_nu = upsert_concept(canonical_name="NU Active Customers", db_path=db)
    record_concept_alias(concept_id=cid_nu, ticker="NU", alias_text="customers", db_path=db)
    record_concept_alias(concept_id=cid_universal, ticker=None, alias_text="customers", db_path=db)

    # Without ticker → universal wins
    assert resolve_concept("customers", db_path=db) == cid_universal
    # With ticker='NU' → ticker-specific wins
    assert resolve_concept("customers", ticker="NU", db_path=db) == cid_nu


def test_concept_definition_supersedes_prior(db: Path) -> None:
    cid = upsert_concept(canonical_name="Active Customers", db_path=db)
    d1 = record_concept_definition(
        concept_id=cid,
        ticker="NU",
        effective_from=datetime(2022, 1, 1, tzinfo=UTC),
        definition_md="customers with >$1 transactions in last 30 days",
        db_path=db,
    )
    d2 = record_concept_definition(
        concept_id=cid,
        ticker="NU",
        effective_from=datetime(2024, 9, 1, tzinfo=UTC),
        definition_md="customers with >$2 transactions in last 90 days",
        computation_change_md="raised activity floor from $1/30d to $2/90d",
        db_path=db,
    )
    assert d1 != d2

    conn = sqlite3.connect(str(db))
    try:
        rows = conn.execute(
            "SELECT id, superseded_by_id, effective_to FROM concept_definitions ORDER BY id"
        ).fetchall()
        # First row should be superseded by second
        assert rows[0][1] == d2
        assert rows[0][2] is not None
        # Second row is current (superseded_by_id IS NULL)
        assert rows[1][1] is None
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Extractions
# ---------------------------------------------------------------------------


def test_record_extraction_stores_payload(db: Path) -> None:
    eid = record_extraction(
        source_doc_id=42,
        extraction_kind="forward_statement",
        extractor_id="forward_statement_extractor",
        extractor_version="v1",
        payload={
            "sentence": "We expect cloud revenue to grow 25% in 2026",
            "kpi_name": "cloud revenue",
            "target_value": 0.25,
            "target_period": "2026-12-31",
        },
        confidence=0.92,
        char_offset_start=1023,
        char_offset_end=1075,
        db_path=db,
    )
    assert eid is not None

    conn = sqlite3.connect(str(db))
    try:
        row = conn.execute(
            "SELECT payload_json, char_offset_start FROM extractions WHERE id = ?", (eid,)
        ).fetchone()
        import json

        payload = json.loads(row[0])
        assert payload["kpi_name"] == "cloud revenue"
        assert row[1] == 1023
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Mapping proposals — auto-apply, pending, reject
# ---------------------------------------------------------------------------


def test_propose_high_confidence_auto_applies_new_entity(db: Path) -> None:
    pid, status = propose_mapping(
        kind="new_entity",
        payload={"kind": "competitor", "canonical_name": "DeepSeek"},
        confidence=0.91,
        db_path=db,
    )
    assert pid is not None
    assert status == "auto_applied"
    # The entity should exist
    assert resolve_entity("DeepSeek", db_path=db) is not None


def test_propose_high_confidence_auto_applies_new_alias(db: Path) -> None:
    eid = upsert_entity(kind="competitor", canonical_name="OpenAI", db_path=db)
    _pid, status = propose_mapping(
        kind="new_alias",
        payload={"entity_id": eid, "alias_text": "ChatGPT maker"},
        confidence=0.90,
        db_path=db,
    )
    assert status == "auto_applied"
    # Alias should resolve
    assert resolve_entity("ChatGPT maker", db_path=db) == eid


def test_propose_medium_confidence_pending_review(db: Path) -> None:
    _pid, status = propose_mapping(
        kind="new_entity",
        payload={"kind": "competitor", "canonical_name": "AmbiguousName"},
        confidence=0.70,
        db_path=db,
    )
    assert status == "pending_review"
    # Entity should NOT be applied yet
    assert resolve_entity("AmbiguousName", db_path=db) is None


def test_propose_low_confidence_rejected(db: Path) -> None:
    _pid, status = propose_mapping(
        kind="new_entity",
        payload={"kind": "competitor", "canonical_name": "RandomNoise"},
        confidence=0.20,
        db_path=db,
    )
    assert status == "rejected"


def test_pending_proposals_lists_only_pending(db: Path) -> None:
    propose_mapping(
        kind="new_entity",
        payload={"kind": "competitor", "canonical_name": "AutoApplied"},
        confidence=0.95,
        db_path=db,
    )
    propose_mapping(
        kind="new_entity",
        payload={"kind": "competitor", "canonical_name": "NeedsReview"},
        confidence=0.65,
        db_path=db,
    )
    pending = pending_proposals(db_path=db)
    names = [p.payload.get("canonical_name") for p in pending if isinstance(p.payload, dict)]
    assert "NeedsReview" in names
    assert "AutoApplied" not in names


def test_decide_proposal_apply_executes_payload(db: Path) -> None:
    pid, status = propose_mapping(
        kind="new_entity",
        payload={"kind": "competitor", "canonical_name": "NeedsReview"},
        confidence=0.65,
        db_path=db,
    )
    assert status == "pending_review"

    ok = decide_proposal(proposal_id=pid or 0, decision="apply", decided_by="user", db_path=db)
    assert ok is True
    assert resolve_entity("NeedsReview", db_path=db) is not None


def test_decide_proposal_reject_no_schema_effect(db: Path) -> None:
    pid, _ = propose_mapping(
        kind="new_entity",
        payload={"kind": "competitor", "canonical_name": "RejectedName"},
        confidence=0.65,
        db_path=db,
    )
    decide_proposal(proposal_id=pid or 0, decision="reject", decided_by="user", db_path=db)
    assert resolve_entity("RejectedName", db_path=db) is None


# ---------------------------------------------------------------------------
# Resilience
# ---------------------------------------------------------------------------


def test_upsert_entity_returns_none_when_db_missing(tmp_path: Path) -> None:
    missing = tmp_path / "no.db"
    assert upsert_entity(kind="company", canonical_name="x", db_path=missing) is None


def test_resolve_returns_none_when_table_missing(tmp_path: Path) -> None:
    db = tmp_path / "empty.db"
    sqlite3.connect(str(db)).close()
    assert resolve_entity("anything", db_path=db) is None
