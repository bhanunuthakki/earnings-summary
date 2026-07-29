from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest
from alembic.config import Config

from alembic import command

ROOT = Path(__file__).resolve().parents[1]
BASE_REVISION = "0213_decision_draft_provider_id"
REVISION = "0243_metric_ontology"
PARENT = "0242_filing_xbrl_extraction_dispositions"
AT = "2026-07-27T00:00:00+00:00"
FUTURE = "2026-07-28T00:00:00+00:00"


def _config(path: Path) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    return config


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _sql_sha(value: object) -> str:
    return hashlib.sha256(str(value).encode()).hexdigest()


def test_0243_upgrade_constraints_final_seal_and_exact_downgrade(
    tmp_path: Path,
) -> None:
    path = tmp_path / "ontology-migration.db"
    config = _config(path)
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE financial_facts (
            id INTEGER PRIMARY KEY, source_doc_id INTEGER NOT NULL
        );
        CREATE TABLE kpi_facts (
            id INTEGER PRIMARY KEY, source_doc_id INTEGER NOT NULL
        );
        """
    )
    conn.close()
    command.stamp(config, BASE_REVISION)
    command.upgrade(config, REVISION)

    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys=ON")
    conn.create_function("fact_sha256", 1, _sql_sha)
    try:
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone() == (REVISION,)
        tables = {
            str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert {
            "canonical_metrics",
            "canonical_axes",
            "canonical_members",
            "source_taxonomy_components",
            "source_observation_taxonomy_assertions",
            "canonical_metric_definition_revisions",
            "source_dimension_mapping_revisions",
            "metric_mapping_revisions",
            "canonical_metric_cells",
            "canonical_metric_cell_dimensions",
            "canonical_metric_cell_seals",
            "fact_cell_canonical_binding_revisions",
            "ontology_snapshot_headers",
            "ontology_snapshot_members",
            "ontology_snapshot_seals",
        } <= tables
        source_component_fks = {
            (str(row[2]), str(row[3]), str(row[4]))
            for row in conn.execute("PRAGMA foreign_key_list(source_taxonomy_components)")
        }
        assert (
            "reporting_entities",
            "reporting_entity_id",
            "reporting_entity_id",
        ) in source_component_fks
        canonical_cell_fks = {
            (str(row[2]), str(row[3]), str(row[4]))
            for row in conn.execute("PRAGMA foreign_key_list(canonical_metric_cells)")
        }
        assert (
            "reporting_entities",
            "reporting_entity_id",
            "reporting_entity_id",
        ) in canonical_cell_fks
        assert ("securities", "scope_security_id", "security_id") in canonical_cell_fks
        taxonomy_trigger = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' "
            "AND name='trg_source_taxonomy_assertion_exact'"
        ).fetchone()
        assert taxonomy_trigger is not None
        taxonomy_trigger_sql = str(taxonomy_trigger[0])
        for required_proof in (
            "fact_cell_semantic_key_sha256",
            "anchor_payload_sha256",
            "observation_payload_sha256",
            "extraction_output_sha256",
            "raw_entry_sha256",
            "observation_set_sha256",
            "run.extraction_run_id=NEW.extraction_run_id",
            "datetime(completeness.recorded_at)",
            "datetime(NEW.knowledge_at)",
        ):
            assert required_proof in taxonomy_trigger_sql

        component_payload = json.dumps(
            {
                "component_kind": "concept",
                "local_name": "Revenue",
                "taxonomy_version": "2025",
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        component_values = (
            "concept:revenue",
            "concept:revenue",
            "concept",
            "urn:test",
            "Revenue",
            "test-taxonomy",
            "2025",
            None,
            "__global__",
            0,
            None,
            "duration",
            None,
            0,
            None,
            None,
            "[]",
            "{}",
            component_payload,
            _sha(component_payload),
            AT,
            AT,
            AT,
        )
        conn.execute(
            "INSERT INTO source_taxonomy_components VALUES "
            f"({','.join('?' for _ in component_values)})",
            component_values,
        )
        duplicate = list(component_values)
        duplicate[0] = "concept:duplicate"
        duplicate[1] = "concept:duplicate"
        with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
            conn.execute(
                "INSERT INTO source_taxonomy_components VALUES "
                f"({','.join('?' for _ in duplicate)})",
                duplicate,
            )
        bad_hash = list(component_values)
        bad_hash[0] = "concept:bad-hash"
        bad_hash[1] = "concept:bad-hash"
        bad_hash[4] = "OtherConcept"
        bad_hash[18] = '{"component_kind":"concept","local_name":"OtherConcept"}'
        bad_hash[19] = "0" * 64
        with pytest.raises(sqlite3.IntegrityError, match="commitment mismatch"):
            conn.execute(
                "INSERT INTO source_taxonomy_components VALUES "
                f"({','.join('?' for _ in bad_hash)})",
                bad_hash,
            )
        global_extension: list[object] = list(component_values)
        global_extension[0] = "concept:global-extension"
        global_extension[1] = "concept:global-extension"
        global_extension[4] = "IssuerRevenue"
        global_extension[9] = 1
        global_extension[18] = '{"component_kind":"concept","local_name":"IssuerRevenue"}'
        global_extension[19] = _sha(global_extension[18])
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            conn.execute(
                "INSERT INTO source_taxonomy_components VALUES "
                f"({','.join('?' for _ in global_extension)})",
                global_extension,
            )

        metric_payload = '{"canonical_name":"Revenue","metric_id":"revenue"}'
        conn.execute(
            "INSERT INTO issuer_entities "
            "(issuer_id,idempotency_key,entity_kind,created_at) "
            "VALUES (?,?,?,?)",
            ("issuer", "issuer", "operating_company", AT),
        )
        conn.execute(
            "INSERT INTO reporting_entities "
            "(reporting_entity_id,idempotency_key,issuer_id,"
            "reporting_entity_kind,display_name,created_at) "
            "VALUES (?,?,?,?,?,?)",
            (
                "entity",
                "entity",
                "issuer",
                "legal_registrant",
                "Entity",
                AT,
            ),
        )
        conn.execute(
            "INSERT INTO canonical_metrics VALUES (?,?,?,?,?,?,?,?)",
            (
                "revenue",
                "metric:revenue",
                "Revenue",
                metric_payload,
                _sha(metric_payload),
                AT,
                AT,
                AT,
            ),
        )
        definition_payload = json.dumps(
            {
                "metric_id": "revenue",
                "revision": 2,
                "supersedes_metric_definition_revision_id": "missing",
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO canonical_metric_definition_revisions "
                "(metric_definition_revision_id,idempotency_key,metric_id,"
                "revision,supersedes_metric_definition_revision_id,lifecycle,"
                "definition_text,aliases_json,value_kind,period_kind,"
                "unit_family,accounting_basis,scope_constraints_json,"
                "commitment_json,commitment_sha256,effective_at,knowledge_at,"
                "recorded_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "definition:bad:2",
                    "definition:bad:2",
                    "revenue",
                    2,
                    "missing",
                    "active",
                    "bad parent",
                    "[]",
                    "numeric",
                    "duration",
                    "currency",
                    "us_gaap",
                    "{}",
                    definition_payload,
                    _sha(definition_payload),
                    AT,
                    AT,
                    AT,
                ),
            )
        semantic_identity = json.dumps(
            {
                "accounting_basis": "us_gaap",
                "canonical_dimensions": [],
                "consolidation_scope": "consolidated",
                "metric_id": "revenue",
                "period_end": AT,
                "period_kind": "instant",
                "period_start": None,
                "reporting_entity_id": "entity",
                "scope_security_id": None,
                "unit_family": "currency",
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        conn.execute(
            "INSERT INTO canonical_metric_cells "
            "(canonical_metric_cell_id,idempotency_key,metric_id,"
            "reporting_entity_id,period_kind,period_end,dimension_count,"
            "unit_family,accounting_basis,consolidation_scope,effective_at,"
            "knowledge_at,recorded_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "cell:backdated-seal",
                "cell:backdated-seal",
                "revenue",
                "entity",
                "instant",
                AT,
                0,
                "currency",
                "us_gaap",
                "consolidated",
                AT,
                AT,
                FUTURE,
            ),
        )
        with pytest.raises(sqlite3.IntegrityError, match="seal mismatch"):
            conn.execute(
                "INSERT INTO canonical_metric_cell_seals VALUES (?,?,?,?,?,?)",
                (
                    "cell:backdated-seal",
                    "[]",
                    _sha("[]"),
                    semantic_identity,
                    _sha(semantic_identity),
                    AT,
                ),
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO canonical_metric_cells "
                "(canonical_metric_cell_id,idempotency_key,metric_id,"
                "reporting_entity_id,period_kind,period_end,dimension_count,"
                "unit_family,accounting_basis,consolidation_scope,"
                "effective_at,knowledge_at,recorded_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "cell:unknown",
                    "cell:unknown",
                    "unknown",
                    "entity",
                    "instant",
                    AT,
                    0,
                    "currency",
                    "us_gaap",
                    "consolidated",
                    AT,
                    AT,
                    AT,
                ),
            )

        conn.execute(
            "INSERT INTO ontology_snapshot_headers VALUES (?,?,?,?)",
            ("snapshot", "snapshot", AT, AT),
        )
        empty = "[]"
        with pytest.raises(sqlite3.IntegrityError, match="seal mismatch"):
            conn.execute(
                "INSERT INTO ontology_snapshot_seals VALUES (?,?,?,?,?)",
                ("snapshot", 0, empty, _sha(empty), AT),
            )
        expected = list(
            conn.execute(
                "SELECT member_kind,member_id,member_sha256 "
                "FROM v_ontology_snapshot_expected_members "
                "WHERE ontology_snapshot_id='snapshot' "
                "ORDER BY member_kind,member_id"
            )
        )
        for ordinal, row in enumerate(expected):
            conn.execute(
                "INSERT INTO ontology_snapshot_members VALUES (?,?,?,?,?)",
                ("snapshot", ordinal, *row),
            )
        payload = json.dumps(
            [{"id": row[1], "kind": row[0], "sha256": row[2]} for row in expected],
            sort_keys=True,
            separators=(",", ":"),
        )
        conn.execute(
            "INSERT INTO ontology_snapshot_seals VALUES (?,?,?,?,?)",
            ("snapshot", len(expected), payload, _sha(payload), AT),
        )
        with pytest.raises(sqlite3.IntegrityError, match="sealed"):
            conn.execute(
                "INSERT INTO ontology_snapshot_members VALUES (?,?,?,?,?)",
                ("snapshot", 99, "extra", "extra", "a" * 64),
            )
        conn.execute(
            "INSERT INTO ontology_snapshot_headers VALUES (?,?,?,?)",
            ("snapshot:tampered", "snapshot:tampered", AT, AT),
        )
        tampered_expected = list(
            conn.execute(
                "SELECT member_kind,member_id,member_sha256 "
                "FROM v_ontology_snapshot_expected_members "
                "WHERE ontology_snapshot_id='snapshot:tampered' "
                "ORDER BY member_kind,member_id"
            )
        )
        for ordinal, row in enumerate(tampered_expected):
            digest = "b" * 64 if ordinal == 0 else row[2]
            conn.execute(
                "INSERT INTO ontology_snapshot_members VALUES (?,?,?,?,?)",
                ("snapshot:tampered", ordinal, row[0], row[1], digest),
            )
        tampered_payload = json.dumps(
            [
                {
                    "id": row[1],
                    "kind": row[0],
                    "sha256": "b" * 64 if ordinal == 0 else row[2],
                }
                for ordinal, row in enumerate(tampered_expected)
            ],
            sort_keys=True,
            separators=(",", ":"),
        )
        with pytest.raises(sqlite3.IntegrityError, match="seal mismatch"):
            conn.execute(
                "INSERT INTO ontology_snapshot_seals VALUES (?,?,?,?,?)",
                (
                    "snapshot:tampered",
                    len(tampered_expected),
                    tampered_payload,
                    _sha(tampered_payload),
                    AT,
                ),
            )
        conn.execute(
            "INSERT INTO ontology_snapshot_headers VALUES (?,?,?,?)",
            ("snapshot:extra", "snapshot:extra", AT, AT),
        )
        extra_expected = list(
            conn.execute(
                "SELECT member_kind,member_id,member_sha256 "
                "FROM v_ontology_snapshot_expected_members "
                "WHERE ontology_snapshot_id='snapshot:extra' "
                "ORDER BY member_kind,member_id"
            )
        )
        for ordinal, row in enumerate(extra_expected):
            conn.execute(
                "INSERT INTO ontology_snapshot_members VALUES (?,?,?,?,?)",
                ("snapshot:extra", ordinal, *row),
            )
        conn.execute(
            "INSERT INTO ontology_snapshot_members VALUES (?,?,?,?,?)",
            (
                "snapshot:extra",
                len(extra_expected),
                "extra",
                "extra",
                "c" * 64,
            ),
        )
        extra_payload = json.dumps(
            [{"id": row[1], "kind": row[0], "sha256": row[2]} for row in extra_expected]
            + [{"id": "extra", "kind": "extra", "sha256": "c" * 64}],
            sort_keys=True,
            separators=(",", ":"),
        )
        with pytest.raises(sqlite3.IntegrityError, match="seal mismatch"):
            conn.execute(
                "INSERT INTO ontology_snapshot_seals VALUES (?,?,?,?,?)",
                (
                    "snapshot:extra",
                    len(extra_expected) + 1,
                    extra_payload,
                    _sha(extra_payload),
                    AT,
                ),
            )
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        conn.close()

    command.downgrade(config, PARENT)
    conn = sqlite3.connect(path)
    try:
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone() == (PARENT,)
        assert (
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='canonical_metrics'"
            ).fetchone()
            is None
        )
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        conn.close()
