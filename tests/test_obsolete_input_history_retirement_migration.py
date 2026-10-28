"""Retirement of optional LLM-call history and unconfirmed profile inputs."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest

from provenance.evidence_backfill import ensure_legacy_document_evidence

PREDECESSOR = "0068_retire_personal_coaching"


def _call(
    connection: sqlite3.Connection,
    call_id: int,
    purpose: str,
    *,
    artifact_id: int | None = None,
) -> None:
    connection.execute(
        "INSERT INTO llm_calls "
        "(id,called_at,purpose,model,prompt_sha256,prompt_chars,elapsed_ms,artifact_id) "
        "VALUES (?, '2026-01-01', ?, 'synthetic', ?, 1, 1, ?)",
        (call_id, purpose, "a" * 64, artifact_id),
    )


def _profile_fact(
    connection: sqlite3.Connection,
    fact_id: int,
    category: str,
    provenance: str,
    status: str,
    *,
    successor: int | None = None,
) -> None:
    connection.execute(
        "INSERT INTO owner_profile_facts "
        '(id,category,"key",value_json,narrative,provenance,status,created_at,'
        "is_latest,superseded_by_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            fact_id,
            category,
            f"synthetic-{fact_id}",
            "{}",
            "synthetic",
            provenance,
            status,
            "2026-01-01",
            1 if successor is None else 0,
            successor,
        ),
    )


def _before_counts(connection: sqlite3.Connection) -> dict[str, int]:
    return {
        table: int(connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
        for table in (
            "llm_calls",
            "owner_profile_facts",
            "financial_facts",
            "source_calls",
            "decisions",
        )
    }


def test_retirement_deletes_only_unlinked_calls_and_unconfirmed_profile_inputs(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    database = migrated_db(tmp_path / "optional-history.db", target=PREDECESSOR)
    source_bytes = b'{"revenue":123.0}'
    (tmp_path / "synthetic.json").write_bytes(source_bytes)
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        _call(connection, 91001, "behavior_distill")  # Eligible.
        _call(connection, 91002, "theme_synthesis")  # One known JSON reference protects this row.
        _call(
            connection, 91003, "saydo_pair", artifact_id=777
        )  # Legacy artifact link protects this row.
        connection.execute(
            "INSERT INTO llm_artifacts (id,purpose,input_sha256,generated_at) "
            "VALUES (777,'company_description',?,'2026-01-01')",
            ("c" * 64,),
        )
        _call(connection, 91006, "saydo_pair", artifact_id=778)  # Dangling scalar is eligible.
        _call(connection, 91004, "transcript_summary")  # Retained source-grounded purpose.
        _call(connection, 91005, "capture_triage")  # Exact FK reference protects this row.
        connection.execute(
            "INSERT INTO llm_artifacts "
            "(id,purpose,input_sha256,generated_at,llm_call_id) "
            "VALUES (91050,'company_description',?,'2026-01-01',91005)",
            ("b" * 64,),
        )
        connection.execute(
            "INSERT INTO analyst_notes "
            "(id,kind,body,source,created_at,updated_at,context_json) "
            "VALUES (91060,'observation','explicit note','manual','2026-01-01','2026-01-01',?)",
            ('{"evidence":{"call_id":91002}}',),
        )
        _profile_fact(connection, 92001, "capacity", "wealthplan_import", "affirmed")
        _profile_fact(connection, 92002, "appetite", "derived", "rejected")
        _profile_fact(connection, 92003, "appetite", "owner", "affirmed")
        _profile_fact(connection, 92004, "behavioral", "derived", "affirmed")
        _profile_fact(connection, 92005, "behavioral", "derived", "proposed")
        _profile_fact(connection, 92006, "behavioral", "owner", "proposed")
        connection.execute(
            "INSERT INTO decisions "
            "(ticker,recommendation_kind,decided_by,falsifier,made_at,created_at) "
            "VALUES ('SYNTH','hold','owner','synthetic falsifier','2026-01-01','2026-01-01')"
        )
        connection.execute(
            "INSERT INTO documents "
            "(id,ticker,source_type,doc_type,file_path,sha256,fetched_at,fetch_status,raw_bytes_size) "
            "VALUES (93001,'SYNTH','fmp','sec_10q','synthetic.json',?,'2026-01-01','ok',?)",
            (hashlib.sha256(source_bytes).hexdigest(), len(source_bytes)),
        )
        connection.row_factory = sqlite3.Row
        ensure_legacy_document_evidence(connection, repo_root=tmp_path, document_id=93001)
        connection.row_factory = None
        connection.execute(
            "INSERT INTO financial_facts "
            "(ticker,period_end,fiscal_period_type,line_item,value,unit,currency,"
            "source_doc_id,extracted_by,locator) "
            "VALUES ('SYNTH','2025-12-31','FY','revenue',123.0,'actual','USD',93001,'fmp',?)",
            ('{"json_path":"$.revenue"}',),
        )
        connection.execute(
            "INSERT INTO source_calls (id,source_name,kind,status) "
            "VALUES (91070,'synthetic','archive_read','ok')"
        )
        before_counts = _before_counts(connection)
        before_decisions = connection.execute("SELECT * FROM decisions ORDER BY id").fetchall()
        before_financial = connection.execute(
            "SELECT * FROM financial_facts ORDER BY id"
        ).fetchall()
        before_sources = connection.execute("SELECT * FROM source_calls ORDER BY id").fetchall()
        before_notes = connection.execute("SELECT * FROM analyst_notes ORDER BY id").fetchall()
        before_artifacts = connection.execute("SELECT * FROM llm_artifacts ORDER BY id").fetchall()

    migrated_db(database, upgrade_existing=True, target="0069_retire_obsolete_input_history")

    with sqlite3.connect(database) as connection:
        calls = connection.execute(
            "SELECT id,purpose,artifact_id FROM llm_calls WHERE id BETWEEN 91001 AND 91005 ORDER BY id"
        ).fetchall()
        assert calls == [
            (91002, "theme_synthesis", None),
            (91003, "saydo_pair", 777),
            (91004, "transcript_summary", None),
            (91005, "capture_triage", None),
        ]
        profile = connection.execute(
            "SELECT id,category,provenance,status FROM owner_profile_facts "
            "WHERE id BETWEEN 92001 AND 92006 ORDER BY id"
        ).fetchall()
        assert profile == [
            (92003, "appetite", "owner", "affirmed"),
            (92004, "behavioral", "derived", "affirmed"),
            (92006, "behavioral", "owner", "proposed"),
        ]
        assert (
            connection.execute("SELECT * FROM decisions ORDER BY id").fetchall() == before_decisions
        )
        assert (
            connection.execute("SELECT * FROM financial_facts ORDER BY id").fetchall()
            == before_financial
        )
        assert (
            connection.execute("SELECT * FROM source_calls ORDER BY id").fetchall()
            == before_sources
        )
        assert (
            connection.execute("SELECT * FROM analyst_notes ORDER BY id").fetchall() == before_notes
        )
        assert (
            connection.execute("SELECT * FROM llm_artifacts ORDER BY id").fetchall()
            == before_artifacts
        )
        after_counts = _before_counts(connection)
        assert after_counts == {
            **before_counts,
            "llm_calls": before_counts["llm_calls"] - 2,
            "owner_profile_facts": before_counts["owner_profile_facts"] - 3,
        }
        assert connection.execute("SELECT id FROM llm_calls WHERE id=91006").fetchone() is None
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.parametrize("reference", [91001, "91001", "091001", 91001.0, "9.1001e4"])
def test_numeric_json_reference_preserves_exact_call(
    tmp_path: Path, migrated_db: Callable[..., Path], reference: int | float | str
) -> None:
    database = migrated_db(tmp_path / "numeric-reference.db", target=PREDECESSOR)
    with sqlite3.connect(database) as connection:
        _call(connection, 91001, "theme_synthesis")
        connection.execute(
            "INSERT INTO analyst_notes "
            "(kind,body,source,created_at,updated_at,context_json) "
            "VALUES ('observation','explicit note','manual','2026-01-01','2026-01-01',?)",
            (json.dumps({"evidence": {"llm_call_id": reference}}),),
        )

    migrated_db(database, upgrade_existing=True, target="0069_retire_obsolete_input_history")

    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT id FROM llm_calls WHERE id=91001").fetchone() == (91001,)


@pytest.mark.parametrize("blocker", ["unknown_fk", "unknown_trigger", "invalid_json"])
def test_unknown_call_dependency_fails_before_any_retirement_write(
    tmp_path: Path, migrated_db: Callable[..., Path], blocker: str
) -> None:
    database = migrated_db(tmp_path / f"optional-history-{blocker}.db", target=PREDECESSOR)
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        _call(connection, 93001, "behavior_distill")
        _profile_fact(connection, 93002, "capacity", "owner", "affirmed")
        if blocker == "unknown_fk":
            connection.execute(
                "CREATE TABLE custom_call_links (call_id INTEGER REFERENCES llm_calls(id))"
            )
            connection.execute("INSERT INTO custom_call_links VALUES (93001)")
        elif blocker == "unknown_trigger":
            connection.execute(
                "CREATE TRIGGER custom_call_delete BEFORE DELETE ON llm_calls "
                "BEGIN SELECT RAISE(ABORT,'blocked'); END"
            )
        else:
            connection.execute(
                "INSERT INTO analyst_notes "
                "(id,kind,body,source,created_at,updated_at,context_json) "
                "VALUES (93003,'observation','synthetic','manual','2026-01-01','2026-01-01','{')"
            )
        before = _before_counts(connection)

    with pytest.raises(RuntimeError):
        migrated_db(database, upgrade_existing=True, target="0069_retire_obsolete_input_history")

    with sqlite3.connect(database) as connection:
        assert _before_counts(connection) == before
        assert connection.execute("SELECT purpose FROM llm_calls WHERE id=93001").fetchone() == (
            "behavior_distill",
        )
        assert connection.execute(
            "SELECT category FROM owner_profile_facts WHERE id=93002"
        ).fetchone() == ("capacity",)


def test_retained_profile_successor_into_retirement_set_fails_before_writes(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    database = migrated_db(tmp_path / "profile-successor.db", target=PREDECESSOR)
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        _call(connection, 94001, "behavior_distill")
        _profile_fact(connection, 94002, "capacity", "wealthplan_import", "affirmed")
        _profile_fact(connection, 94003, "appetite", "owner", "affirmed", successor=94002)
        before = _before_counts(connection)

    with pytest.raises(RuntimeError, match="retained owner fact"):
        migrated_db(database, upgrade_existing=True, target="0069_retire_obsolete_input_history")

    with sqlite3.connect(database) as connection:
        assert _before_counts(connection) == before
        assert connection.execute(
            "SELECT superseded_by_id FROM owner_profile_facts WHERE id=94003"
        ).fetchone() == (94002,)
        assert connection.execute("SELECT purpose FROM llm_calls WHERE id=94001").fetchone() == (
            "behavior_distill",
        )
