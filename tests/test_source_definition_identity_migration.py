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
PARENT = "0258_fact_anchor_run_lookup_index"
REVISION = "0259_source_definition_identity"
AT = "2026-07-30T00:00:00+00:00"


def _config(path: Path) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    return config


def _sha(value: object) -> str:
    return hashlib.sha256(str(value).encode()).hexdigest()


def _database_at_parent(path: Path) -> Config:
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
    config = _config(path)
    command.stamp(config, BASE_REVISION)
    command.upgrade(config, PARENT)
    return config


def _component_values(
    *,
    component_id: str,
    qualifier: str,
) -> tuple[object, ...]:
    payload = json.dumps(
        {
            "component_kind": "concept",
            "definition_qualifier_sha256": qualifier,
            "local_name": "Revenue",
            "taxonomy_version": "2026",
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return (
        component_id,
        component_id,
        "concept",
        "urn:test",
        "Revenue",
        "issuer-taxonomy",
        "2026",
        qualifier,
        None,
        "__global__",
        0,
        "numeric",
        "duration",
        None,
        0,
        "Revenue",
        "Exact source definition.",
        "[]",
        "{}",
        payload,
        _sha(payload),
        AT,
        AT,
        AT,
    )


def _legacy_component_values(*, evidence_locator_json: str) -> tuple[object, ...]:
    payload = json.dumps(
        {
            "component_kind": "concept",
            "local_name": "Revenue [synthetic]",
            "taxonomy_version": "legacy [synthetic]",
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return (
        "component:legacy",
        "component:legacy",
        "concept",
        "urn:test",
        "Revenue [synthetic]",
        "earnings-summary-legacy",
        "legacy [synthetic]",
        None,
        "__global__",
        0,
        "numeric",
        "duration",
        None,
        0,
        "Revenue",
        "Legacy source definition.",
        "[]",
        evidence_locator_json,
        payload,
        _sha(payload),
        AT,
        AT,
        AT,
    )


def test_0259_fails_closed_when_existing_concept_qualifier_is_not_reconstructible(
    tmp_path: Path,
) -> None:
    path = tmp_path / "unqualified-legacy-concept.db"
    config = _database_at_parent(path)
    conn = sqlite3.connect(path)
    conn.create_function("fact_sha256", 1, _sha)
    values = _legacy_component_values(evidence_locator_json="{}")
    conn.execute(
        f"INSERT INTO source_taxonomy_components VALUES ({','.join('?' for _ in values)})",
        values,
    )
    conn.commit()
    conn.close()

    with pytest.raises(
        RuntimeError,
        match="cannot reconstruct an existing concept definition qualifier",
    ):
        command.upgrade(config, REVISION)

    conn = sqlite3.connect(path)
    try:
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone() == (PARENT,)
        assert conn.execute("SELECT COUNT(*) FROM source_taxonomy_components").fetchone() == (1,)
        assert "definition_qualifier_sha256" not in {
            str(row[1]) for row in conn.execute("PRAGMA table_info(source_taxonomy_components)")
        }
    finally:
        conn.close()


def test_0259_preserves_exact_coordinate_and_qualifies_definition_variants(
    tmp_path: Path,
) -> None:
    path = tmp_path / "definition-variants.db"
    config = _database_at_parent(path)
    command.upgrade(config, REVISION)

    conn = sqlite3.connect(path)
    conn.create_function("fact_sha256", 1, _sha)
    try:
        columns = {
            str(row[1]) for row in conn.execute("PRAGMA table_info(source_taxonomy_components)")
        }
        assert "definition_qualifier_sha256" in columns
        first = _component_values(component_id="component:first", qualifier="a" * 64)
        second = _component_values(component_id="component:second", qualifier="b" * 64)
        conn.execute(
            f"INSERT INTO source_taxonomy_components VALUES ({','.join('?' for _ in first)})",
            first,
        )
        conn.execute(
            f"INSERT INTO source_taxonomy_components VALUES ({','.join('?' for _ in second)})",
            second,
        )
        duplicate = list(second)
        duplicate[0] = "component:duplicate"
        duplicate[1] = "component:duplicate"
        with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
            conn.execute(
                "INSERT INTO source_taxonomy_components VALUES "
                f"({','.join('?' for _ in duplicate)})",
                duplicate,
            )
        coordinates = conn.execute(
            "SELECT DISTINCT taxonomy_namespace,local_name,taxonomy_name,"
            "taxonomy_version FROM source_taxonomy_components"
        ).fetchall()
        assert coordinates == [("urn:test", "Revenue", "issuer-taxonomy", "2026")]
        trigger_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' "
            "AND name='trg_binding_exact_coordinate'"
        ).fetchone()
        assert trigger_sql is not None
        assert "source.definition_qualifier_sha256=fact_sha256(json_object(" in str(trigger_sql[0])
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(
        RuntimeError,
        match="would collapse distinct source definition qualifiers",
    ):
        command.downgrade(config, PARENT)


def test_0259_upgrade_downgrade_preserves_rows_triggers_and_integrity(
    tmp_path: Path,
) -> None:
    path = tmp_path / "definition-roundtrip.db"
    config = _database_at_parent(path)
    command.upgrade(config, REVISION)
    conn = sqlite3.connect(path)
    conn.create_function("fact_sha256", 1, _sha)
    values = _component_values(component_id="component:only", qualifier="c" * 64)
    conn.execute(
        f"INSERT INTO source_taxonomy_components VALUES ({','.join('?' for _ in values)})",
        values,
    )
    conn.commit()
    conn.close()

    command.downgrade(config, PARENT)

    conn = sqlite3.connect(path)
    try:
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone() == (PARENT,)
        columns = {
            str(row[1]) for row in conn.execute("PRAGMA table_info(source_taxonomy_components)")
        }
        assert "definition_qualifier_sha256" not in columns
        assert conn.execute("SELECT COUNT(*) FROM source_taxonomy_components").fetchone() == (1,)
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        trigger_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' "
            "AND name='trg_binding_exact_coordinate'"
        ).fetchone()
        assert trigger_sql is not None
        assert "source.local_name=source_cell.concept_name" in str(trigger_sql[0])
        assert "source.taxonomy_version=anchor.source_taxonomy_version" in str(trigger_sql[0])
    finally:
        conn.close()
