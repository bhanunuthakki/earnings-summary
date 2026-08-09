from __future__ import annotations

import shutil
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory

from alembic import command

ROOT = Path(__file__).resolve().parents[1]
REVISION = "0238_evidence_first_fact_plane"
PREDECESSOR = "0237_companyfacts_match_gated_capture"
BASE_REVISION = "0213_decision_draft_provider_id"
T0 = "2026-07-27T12:00:00"
H = "a" * 64
H2 = "b" * 64


def _config(path: Path) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("version_locations", str(ROOT / "alembic" / "versions_archived"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    return config


@pytest.fixture(scope="module")
def upgraded_seed(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("fact-plane-v2") / "seed.db"
    config = _config(path)
    legacy = sqlite3.connect(path)
    try:
        legacy.executescript(
            """
            CREATE TABLE financial_facts (
                id INTEGER PRIMARY KEY,
                source_doc_id INTEGER NOT NULL
            );
            CREATE TABLE kpi_facts (
                id INTEGER PRIMARY KEY,
                source_doc_id INTEGER NOT NULL
            );
            """
        )
        legacy.commit()
    finally:
        legacy.close()
    command.stamp(config, BASE_REVISION)
    command.upgrade(config, REVISION)

    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        for issuer in ("issuer-1", "issuer-2"):
            conn.execute(
                "INSERT INTO issuer_entities VALUES (?, ?, 'operating_company', ?)",
                (issuer, f"issuer:{issuer}", T0),
            )
        for entity, issuer in (
            ("entity-1", "issuer-1"),
            ("entity-2", "issuer-2"),
        ):
            conn.execute(
                "INSERT INTO reporting_entities VALUES (?, ?, ?, 'legal_registrant', ?, ?)",
                (entity, f"entity:{entity}", issuer, entity, T0),
            )
        for security, issuer in (
            ("security-1", "issuer-1"),
            ("security-2", "issuer-2"),
            ("security-unrelated", "issuer-1"),
        ):
            conn.execute(
                "INSERT INTO securities VALUES (?, ?, ?, 'common_stock', NULL, ?)",
                (security, f"security:{security}", issuer, T0),
            )
        conn.execute(
            "INSERT INTO security_reporting_entity_revisions "
            "(relationship_revision_id, idempotency_key, relationship_key, "
            "revision, security_id, reporting_entity_id, relationship_kind, "
            "decision_kind, reason_code, reason_details_json, effective_at, "
            "knowledge_at, recorded_at, supersedes_relationship_revision_id) "
            "VALUES ('rel-1', 'rel-1', 'security-1:entity-1', 1, "
            "'security-1', 'entity-1', 'reports_through', 'deterministic', "
            "'seed', '{}', ?, ?, ?, NULL)",
            (T0, T0, T0),
        )
        conn.execute(
            "INSERT INTO recorded_subject_binding_revisions "
            "(binding_revision_id,idempotency_key,recorded_issuer_id,revision,"
            "issuer_id,reporting_entity_id,security_id,outcome,decision_kind,"
            "reason_code,reason_details_json,material_dissent,effective_at,"
            "knowledge_at,recorded_at,supersedes_binding_revision_id) "
            "VALUES ('subject-1','subject-1','issuer-1',1,'issuer-1','entity-1',"
            "NULL,'selected','deterministic','seed','{}',0,?,?,?,NULL)",
            (T0, T0, T0),
        )
        conn.execute(
            "INSERT INTO evidence_content_blobs "
            "(sha256, byte_size, media_type, storage_uri, recorded_at) "
            "VALUES (?, 2, 'application/json', 'file:///evidence.json', ?)",
            (H, T0),
        )
        conn.execute(
            "INSERT INTO evidence_source_observations "
            "(observation_id, idempotency_key, source_kind, source_url, "
            "blob_sha256, source_published_at, filing_at, accepted_at, observed_at, "
            "retrieved_at, retrieval_config_sha256, collector_code_version) "
            "VALUES ('source-1', 'source-1', 'sec_companyfacts', "
            "'https://data.sec.gov/example.json', ?, ?, ?, ?, ?, ?, ?, 'test')",
            (H, T0, T0, T0, T0, T0, H),
        )
        conn.execute(
            "INSERT INTO evidence_source_observations "
            "(observation_id, idempotency_key, source_kind, source_url, "
            "blob_sha256, source_published_at, filing_at, accepted_at, observed_at, "
            "retrieved_at, retrieval_config_sha256, collector_code_version) "
            "VALUES ('source-2', 'source-2', 'sec_companyfacts', "
            "'https://data.sec.gov/unbound.json', ?, ?, ?, ?, ?, ?, ?, 'test')",
            (H, T0, T0, T0, T0, T0, H),
        )
        conn.execute(
            "INSERT INTO evidence_document_versions "
            "(document_version_id, document_key, version_sequence, observation_id, "
            "blob_sha256, issuer_id, ticker, document_type, form_type, "
            "accession_number, exhibit_id, period_start, period_end, as_of_at, "
            "language, replaces_document_version_id, legacy_document_id, recorded_at) "
            "VALUES ('doc-1', 'doc-key-1', 1, 'source-1', ?, 'issuer-1', "
            "NULL, 'sec_companyfacts', 'COMPANYFACTS', NULL, NULL, NULL, ?, ?, "
            "'en', NULL, NULL, ?)",
            (H, T0, T0, T0),
        )
        conn.execute(
            "INSERT INTO evidence_document_versions "
            "(document_version_id, document_key, version_sequence, observation_id, "
            "blob_sha256, issuer_id, ticker, document_type, form_type, "
            "accession_number, exhibit_id, period_start, period_end, as_of_at, "
            "language, replaces_document_version_id, legacy_document_id, recorded_at) "
            "VALUES ('doc-2', 'doc-key-2', 1, 'source-2', ?, 'issuer-2', "
            "NULL, 'sec_companyfacts', 'COMPANYFACTS', NULL, NULL, NULL, ?, ?, "
            "'en', NULL, NULL, ?)",
            (H, T0, T0, T0),
        )
        conn.execute(
            "INSERT INTO evidence_extraction_runs "
            "(extraction_run_id, idempotency_key, document_version_id, input_sha256, "
            "extractor_name, extractor_config_sha256, extractor_code_version, "
            "output_sha256, started_at, completed_at, outcome) "
            "VALUES ('run-1', 'run-1', 'doc-1', ?, 'test', ?, 'v1', ?, ?, ?, "
            "'succeeded')",
            (H, H, H2, T0, T0),
        )
        conn.execute(
            "INSERT INTO evidence_extraction_runs "
            "(extraction_run_id, idempotency_key, document_version_id, input_sha256, "
            "extractor_name, extractor_config_sha256, extractor_code_version, "
            "output_sha256, started_at, completed_at, outcome) "
            "VALUES ('run-2', 'run-2', 'doc-2', ?, 'test', ?, 'v1', ?, ?, ?, "
            "'succeeded')",
            (H, H, H2, T0, T0),
        )
        conn.execute(
            "INSERT INTO evidence_nodes "
            "(node_id, evidence_key, revision, extraction_run_id, parent_node_id, "
            "supersedes_node_id, node_kind, text, locator_json, locator_sha256, "
            "recorded_at) VALUES ('node-1', 'node-key-1', 1, 'run-1', NULL, "
            "NULL, 'document', '{}', '{}', ?, ?)",
            (H, T0),
        )
        conn.execute(
            "INSERT INTO evidence_nodes "
            "(node_id, evidence_key, revision, extraction_run_id, parent_node_id, "
            "supersedes_node_id, node_kind, text, locator_json, locator_sha256, "
            "recorded_at) VALUES ('node-2', 'node-key-2', 1, 'run-2', NULL, "
            "NULL, 'document', '{}', '{}', ?, ?)",
            (H, T0),
        )
        conn.commit()
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        conn.close()
    return path


@pytest.fixture()
def conn(upgraded_seed: Path, tmp_path: Path) -> Iterator[sqlite3.Connection]:
    path = tmp_path / "test.db"
    shutil.copy2(upgraded_seed, path)
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        yield connection
    finally:
        connection.close()


def _insert_cell(
    conn: sqlite3.Connection,
    cell_id: str,
    *,
    entity: str = "entity-1",
    security: str | None = None,
    semantic_hash: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO fact_cells_v2 "
        "(fact_cell_id, idempotency_key, reporting_entity_id, scope_security_id, "
        "semantic_key_version, semantic_key_sha256, concept_namespace, concept_name, "
        "taxonomy_name, taxonomy_version, accounting_basis, consolidation_scope, "
        "period_kind, period_start, period_end, fiscal_year, fiscal_period, "
        "canonical_dimensions_json, canonical_dimensions_sha256, unit_key, currency, "
        "effective_at, knowledge_at, recorded_at) "
        "VALUES (?, ?, ?, ?, 'fact_cell_semantic_key.v2', ?, 'us-gaap', "
        "'Revenue', 'US GAAP', '2026', 'us_gaap', 'consolidated', 'duration', "
        "'2026-01-01', '2026-03-31', 2026, 'Q1', '[]', ?, 'USD', 'USD', ?, ?, ?)",
        (
            cell_id,
            f"cell:{cell_id}",
            entity,
            security,
            semantic_hash or ("c" * 63 + str(len(cell_id) % 10)),
            H,
            T0,
            T0,
            T0,
        ),
    )


def _insert_reported(
    conn: sqlite3.Connection,
    observation_id: str,
    cell_id: str,
    *,
    revision_kind: str = "initial",
    supersedes: str | None = None,
    document_id: str = "doc-1",
    node_id: str = "node-1",
) -> None:
    conn.execute(
        "INSERT INTO fact_observations_v2 "
        "(observation_id, idempotency_key, fact_cell_id, observation_kind, "
        "value_kind, numeric_value, text_value, is_nil, raw_lexical_value, "
        "document_version_id, evidence_node_id, source_locator_json, "
        "source_locator_sha256, source_entry_sha256, source_context_id, "
        "source_unit_id, decimals, precision, legacy_match_revision_id, "
        "formula_id, formula_version, method_name, method_version, "
        "method_config_sha256, revision_kind, supersedes_observation_id, "
        "effective_at, knowledge_at, recorded_at) "
        "VALUES (?, ?, ?, 'reported', 'numeric', '100.00', NULL, 0, '100.00', "
        "?, ?, '{\"path\":\"facts[0]\"}', ?, ?, 'ctx-1', "
        "'unit-1', '-2', NULL, NULL, NULL, NULL, "
        "'sec-xbrl', 'v1', ?, ?, ?, ?, ?, ?)",
        (
            observation_id,
            f"observation:{observation_id}",
            cell_id,
            document_id,
            node_id,
            H,
            H2,
            H,
            revision_kind,
            supersedes,
            T0,
            T0,
            T0,
        ),
    )


def _insert_derived(conn: sqlite3.Connection, observation_id: str, cell_id: str) -> None:
    conn.execute(
        "INSERT INTO fact_observations_v2 "
        "(observation_id, idempotency_key, fact_cell_id, observation_kind, "
        "value_kind, numeric_value, text_value, is_nil, raw_lexical_value, "
        "document_version_id, evidence_node_id, source_locator_json, "
        "source_locator_sha256, source_entry_sha256, legacy_match_revision_id, "
        "formula_id, formula_version, method_name, method_version, "
        "method_config_sha256, revision_kind, supersedes_observation_id, "
        "effective_at, knowledge_at, recorded_at) "
        "VALUES (?, ?, ?, 'derived', 'numeric', '0.25', NULL, 0, '0.25', "
        "NULL, NULL, NULL, NULL, NULL, NULL, 'margin', 'v1', "
        "'deterministic-formula', 'v1', ?, 'initial', NULL, ?, ?, ?)",
        (observation_id, f"observation:{observation_id}", cell_id, H, T0, T0, T0),
    )


def _insert_candidate(
    conn: sqlite3.Connection,
    candidate_id: str,
    candidate_set_id: str,
    cell_id: str,
    observation_id: str,
    *,
    ordinal: int = 0,
    eligibility: str = "eligible",
) -> None:
    conn.execute(
        "INSERT INTO fact_resolution_candidates_v2 "
        "(candidate_id, idempotency_key, candidate_set_id, fact_cell_id, "
        "observation_id, candidate_ordinal, eligibility, reason_code, "
        "reason_details_json, candidate_payload_sha256, recorded_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 'eligible_by_policy', '{}', ?, ?)",
        (
            candidate_id,
            f"candidate:{candidate_id}",
            candidate_set_id,
            cell_id,
            observation_id,
            ordinal,
            eligibility,
            H,
            T0,
        ),
    )


def _insert_resolution(
    conn: sqlite3.Connection,
    resolution_id: str,
    cell_id: str,
    candidate_set_id: str,
    *,
    revision: int = 1,
    status: str = "resolved",
    selected: str | None,
    count: int = 1,
    supersedes: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO fact_resolution_revisions_v2 "
        "(resolution_revision_id, idempotency_key, fact_cell_id, revision, status, "
        "selected_observation_id, candidate_set_id, candidate_count, "
        "candidate_set_digest_sha256, policy_name, policy_version, "
        "policy_config_sha256, reason_code, reason_details_json, effective_at, "
        "knowledge_at, recorded_at, supersedes_resolution_revision_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'source-priority', 'v1', ?, "
        "'policy_result', '{}', ?, ?, ?, ?)",
        (
            resolution_id,
            f"resolution:{resolution_id}",
            cell_id,
            revision,
            status,
            selected,
            candidate_set_id,
            count,
            H2,
            H,
            T0,
            T0,
            T0,
            supersedes,
        ),
    )


def test_migration_is_additive_reversible_head(tmp_path: Path) -> None:
    script = ScriptDirectory.from_config(_config(tmp_path / "unused.db"))
    assert len(script.get_heads()) == 1
    assert script.get_revision(REVISION) is not None

    path = tmp_path / "chain.db"
    config = _config(path)
    legacy = sqlite3.connect(path)
    legacy.executescript(
        "CREATE TABLE financial_facts (id INTEGER PRIMARY KEY, "
        "source_doc_id INTEGER NOT NULL);"
        "CREATE TABLE kpi_facts (id INTEGER PRIMARY KEY, "
        "source_doc_id INTEGER NOT NULL);"
    )
    legacy.close()
    command.stamp(config, BASE_REVISION)
    command.upgrade(config, REVISION)
    db = sqlite3.connect(path)
    try:
        columns = {row[1] for row in db.execute("PRAGMA table_info(fact_cells_v2)")}
        assert {"reporting_entity_id", "semantic_key_sha256", "unit_key"} <= columns
        assert {"ticker", "value", "source_document_id"}.isdisjoint(columns)
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        db.close()
    command.downgrade(config, PREDECESSOR)
    db = sqlite3.connect(path)
    try:
        assert db.execute("SELECT version_num FROM alembic_version").fetchone() == (PREDECESSOR,)
        assert (
            db.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'fact_cells_v2'"
            ).fetchone()
            is None
        )
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        db.close()


def test_cell_identity_and_security_ownership_are_fail_closed(
    conn: sqlite3.Connection,
) -> None:
    _insert_cell(conn, "cell-valid", security="security-1")
    with pytest.raises(sqlite3.IntegrityError, match=r"belong|relationship"):
        _insert_cell(conn, "cell-wrong-owner", security="security-2")
    with pytest.raises(sqlite3.IntegrityError, match="relationship"):
        _insert_cell(conn, "cell-no-relationship", security="security-unrelated")
    with pytest.raises(sqlite3.IntegrityError):
        _insert_cell(conn, "cell-bad-hash", semantic_hash="A" * 64)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "UPDATE fact_cells_v2 SET concept_name = 'Changed' WHERE fact_cell_id = 'cell-valid'"
        )


def test_reported_and_derived_contracts_and_append_only(
    conn: sqlite3.Connection,
) -> None:
    _insert_cell(conn, "cell-observations")
    _insert_reported(conn, "reported-1", "cell-observations")
    _insert_derived(conn, "derived-1", "cell-observations")
    as_reported_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(v_fact_observations_as_reported_v2)")
    }
    assert {"source_context_id", "source_unit_id", "decimals", "precision"} <= (as_reported_columns)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO fact_observations_v2 "
            "(observation_id,idempotency_key,fact_cell_id,observation_kind,"
            "value_kind,numeric_value,text_value,is_nil,raw_lexical_value,"
            "method_name,method_version,method_config_sha256,revision_kind,"
            "effective_at,knowledge_at,recorded_at) VALUES "
            "('bad-reported','bad-reported','cell-observations','reported',"
            "'numeric','1',NULL,0,'1','test','v1',?,'initial',?,?,?)",
            (H, T0, T0, T0),
        )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "UPDATE fact_observations_v2 SET numeric_value = '2' "
            "WHERE observation_id = 'reported-1'"
        )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM fact_observations_v2 WHERE observation_id = 'derived-1'")


def test_reported_anchor_requires_selected_reporting_entity_binding(
    conn: sqlite3.Connection,
) -> None:
    _insert_cell(conn, "cell-unbound-document", entity="entity-2")
    with pytest.raises(sqlite3.IntegrityError, match="same-entity"):
        _insert_reported(
            conn,
            "reported-unbound",
            "cell-unbound-document",
            document_id="doc-2",
            node_id="node-2",
        )


def test_revision_parent_and_all_relation_kinds(conn: sqlite3.Connection) -> None:
    _insert_cell(conn, "cell-relations")
    _insert_reported(conn, "reported-old", "cell-relations")
    _insert_reported(
        conn,
        "reported-new",
        "cell-relations",
        revision_kind="amendment",
        supersedes="reported-old",
    )
    for index, kind in enumerate(
        (
            "exact_duplicate_of",
            "amends",
            "reissues",
            "presentation_recast_of",
            "conflicts_with",
            "supersedes_for_as_known",
        )
    ):
        conn.execute(
            "INSERT INTO fact_observation_relations_v2 "
            "(relation_id,idempotency_key,subject_observation_id,"
            "object_observation_id,relation_kind,reason_code,reason_details_json,"
            "policy_name,policy_version,policy_config_sha256,effective_at,"
            "knowledge_at,recorded_at) VALUES (?,?,?,?,?,'test','{}',"
            "'test-policy','v1',?,?,?,?)",
            (
                f"relation-{index}",
                f"relation-{index}",
                "reported-new",
                "reported-old",
                kind,
                H,
                T0,
                T0,
                T0,
            ),
        )
    assert conn.execute("SELECT COUNT(*) FROM fact_observation_relations_v2").fetchone() == (6,)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE fact_observation_relations_v2 SET reason_code = 'changed'")


def test_derivation_edges_seal_exact_inputs_and_freeze_set(
    conn: sqlite3.Connection,
) -> None:
    _insert_cell(conn, "cell-derivation")
    _insert_reported(conn, "input-1", "cell-derivation")
    _insert_reported(conn, "input-2", "cell-derivation")
    _insert_derived(conn, "output-1", "cell-derivation")
    conn.execute(
        "INSERT INTO fact_derivation_input_edges_v2 "
        "VALUES ('edge-1','edge-1','output-1','input-1',NULL,'numerator',0,?)",
        (T0,),
    )
    with pytest.raises(sqlite3.IntegrityError, match="complete"):
        conn.execute(
            "INSERT INTO fact_derivation_seals_v2 VALUES "
            "('seal-bad','seal-bad','output-1',2,?,?,"
            "'canonical-json','v1',?,?,?)",
            (H, H2, T0, T0, T0),
        )
    conn.execute(
        "INSERT INTO fact_derivation_seals_v2 VALUES "
        "('seal-1','seal-1','output-1',1,?,?,"
        "'canonical-json','v1',?,?,?)",
        (H, H2, T0, T0, T0),
    )
    with pytest.raises(sqlite3.IntegrityError, match="sealed"):
        conn.execute(
            "INSERT INTO fact_derivation_input_edges_v2 "
            "VALUES ('edge-2','edge-2','output-1','input-2',NULL,"
            "'denominator',1,?)",
            (T0,),
        )


def test_resolution_candidate_guards_and_projections(
    conn: sqlite3.Connection,
) -> None:
    _insert_cell(conn, "cell-resolution")
    _insert_cell(conn, "cell-other")
    _insert_reported(conn, "candidate-obs", "cell-resolution")
    with pytest.raises(sqlite3.IntegrityError, match="belong"):
        _insert_candidate(
            conn,
            "candidate-wrong-cell",
            "set-wrong",
            "cell-other",
            "candidate-obs",
        )
    _insert_candidate(conn, "candidate-1", "set-unresolved", "cell-resolution", "candidate-obs")
    _insert_resolution(
        conn,
        "resolution-1",
        "cell-resolution",
        "set-unresolved",
        status="unresolved",
        selected=None,
    )
    assert conn.execute(
        "SELECT COUNT(*) FROM v_fact_cells_resolved_current_v2 "
        "WHERE fact_cell_id = 'cell-resolution'"
    ).fetchone() == (0,)
    with pytest.raises(sqlite3.IntegrityError, match="finalized"):
        _insert_candidate(
            conn,
            "candidate-too-late",
            "set-unresolved",
            "cell-resolution",
            "candidate-obs",
            ordinal=1,
        )

    _insert_candidate(conn, "candidate-2", "set-resolved", "cell-resolution", "candidate-obs")
    with pytest.raises(sqlite3.IntegrityError, match="complete"):
        _insert_resolution(
            conn,
            "resolution-bad-count",
            "cell-resolution",
            "set-resolved",
            revision=2,
            selected="candidate-obs",
            count=2,
            supersedes="resolution-1",
        )
    _insert_resolution(
        conn,
        "resolution-2",
        "cell-resolution",
        "set-resolved",
        revision=2,
        selected="candidate-obs",
        supersedes="resolution-1",
    )
    assert conn.execute(
        "SELECT selected_observation_id FROM v_fact_cells_resolved_current_v2 "
        "WHERE fact_cell_id = 'cell-resolution'"
    ).fetchone() == ("candidate-obs",)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "UPDATE fact_resolution_revisions_v2 SET status = 'retired' "
            "WHERE resolution_revision_id = 'resolution-2'"
        )


def test_derived_candidates_require_a_seal(conn: sqlite3.Connection) -> None:
    _insert_cell(conn, "cell-derived-resolution")
    _insert_reported(conn, "derived-input", "cell-derived-resolution")
    _insert_derived(conn, "derived-candidate", "cell-derived-resolution")
    _insert_candidate(
        conn,
        "derived-candidate-row",
        "derived-set",
        "cell-derived-resolution",
        "derived-candidate",
    )
    with pytest.raises(sqlite3.IntegrityError, match="seals"):
        _insert_resolution(
            conn,
            "derived-resolution",
            "cell-derived-resolution",
            "derived-set",
            selected="derived-candidate",
        )
    conn.execute(
        "INSERT INTO fact_derivation_input_edges_v2 VALUES "
        "('derived-edge','derived-edge','derived-candidate','derived-input',"
        "NULL,'input',0,?)",
        (T0,),
    )
    conn.execute(
        "INSERT INTO fact_derivation_seals_v2 VALUES "
        "('derived-seal','derived-seal','derived-candidate',1,?,?,"
        "'canonical-json','v1',?,?,?)",
        (H, H2, T0, T0, T0),
    )
    _insert_resolution(
        conn,
        "derived-resolution-ok",
        "cell-derived-resolution",
        "derived-set",
        selected="derived-candidate",
    )
