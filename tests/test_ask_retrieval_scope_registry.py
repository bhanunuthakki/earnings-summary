# pyright: reportPrivateUsage=false
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import cast

import pytest

from ask.audit_store import canonical_json
from ask.sealed_retrieval import derive_retrieval_scope_id
from execution import audit_ask_retrieval_cutover as cutover
from execution.generate_ask_retrieval_production_scopes import _derive_registry


def _registry_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
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
        INSERT INTO v_issuer_reporting_scope_current
          VALUES ('scope-revision-1','scope-1','issuer-1','core');
        INSERT INTO issuer_entities VALUES ('issuer-1','operating_company');
        INSERT INTO reporting_entities
          VALUES ('reporting-1','issuer-1','legal_registrant');
        INSERT INTO v_security_listings_canonical
          VALUES ('issuer-1','ACME','listed');
        """
    )
    return conn


def test_generator_and_cutover_share_one_exact_scope_commitment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _derive_registry(_registry_db())
    path = tmp_path / "ask_retrieval_production_scopes.json"
    path.write_text(canonical_json(registry) + "\n", encoding="utf-8")
    monkeypatch.setattr(cutover, "PRODUCTION_SCOPE_REGISTRY", path)
    scopes = cutover._load_authoritative_scopes(
        registry_path=path,
        expected_sha256=str(registry["scope_set_sha256"]),
    )
    assert [item.source_scope_key for item in scopes] == ["scope-1"]
    assert scopes[0].source_scope_revision_id == "scope-revision-1"
    assert scopes[0].reporting_entity_id == "reporting-1"


def test_cutover_rejects_operator_or_registry_digest_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _derive_registry(_registry_db())
    path = tmp_path / "ask_retrieval_production_scopes.json"
    path.write_text(canonical_json(registry) + "\n", encoding="utf-8")
    monkeypatch.setattr(cutover, "PRODUCTION_SCOPE_REGISTRY", path)
    with pytest.raises(SystemExit, match="scope-set commitment mismatch"):
        cutover._load_authoritative_scopes(
            registry_path=path,
            expected_sha256="0" * 64,
        )
    tampered = dict(registry)
    tampered["source_scope_revision_ids"] = ["scope-revision-other"]
    path.write_text(canonical_json(tampered) + "\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="registry commitment mismatch"):
        cutover._load_authoritative_scopes(
            registry_path=path,
            expected_sha256=str(registry["scope_set_sha256"]),
        )


@pytest.mark.parametrize(
    "delete_sql",
    [
        "DELETE FROM issuer_entities",
        "DELETE FROM reporting_entities",
        "DELETE FROM v_security_listings_canonical",
    ],
)
def test_every_core_scope_fails_closed_when_identity_plane_is_missing(
    delete_sql: str,
) -> None:
    conn = _registry_db()
    conn.execute(delete_sql)
    with pytest.raises(ValueError, match="missing, duplicate, or unsupported"):
        _derive_registry(conn)


def test_duplicate_core_identity_is_not_collapsed() -> None:
    conn = _registry_db()
    conn.execute(
        "INSERT INTO reporting_entities VALUES "
        "('reporting-duplicate','issuer-1','legal_registrant')"
    )
    with pytest.raises(ValueError, match="missing, duplicate, or unsupported"):
        _derive_registry(conn)


def test_shared_raw_scope_keys_derive_distinct_stable_retrieval_ids() -> None:
    conn = _registry_db()
    conn.executescript(
        """
        INSERT INTO v_issuer_reporting_scope_current
          VALUES ('scope-revision-2','scope-1','issuer-2','core');
        INSERT INTO issuer_entities VALUES ('issuer-2','operating_company');
        INSERT INTO reporting_entities
          VALUES ('reporting-2','issuer-2','legal_registrant');
        INSERT INTO v_security_listings_canonical
          VALUES ('issuer-2','BETA','listed');
        """
    )

    registry = _derive_registry(conn)
    raw_scopes = registry["scopes"]
    assert isinstance(raw_scopes, list)
    scopes = cast(list[dict[str, str]], raw_scopes)
    assert len(scopes) == 2
    assert [item["source_scope_key"] for item in scopes] == ["scope-1", "scope-1"]
    assert {item["issuer_id"]: item["source_scope_revision_id"] for item in scopes} == {
        "issuer-1": "scope-revision-1",
        "issuer-2": "scope-revision-2",
    }
    scope_ids = [item["scope_id"] for item in scopes]
    assert scope_ids == sorted(scope_ids)
    assert len(set(scope_ids)) == 2
    assert all(value.startswith("ask-scope:v1:") and len(value) == 77 for value in scope_ids)
    assert scope_ids == [
        derive_retrieval_scope_id(
            source_scope_key=item["source_scope_key"], issuer_id=item["issuer_id"]
        )
        for item in scopes
    ]


def test_retrieval_scope_id_is_canonical_and_rejects_empty_composite_members() -> None:
    expected = derive_retrieval_scope_id(
        source_scope_key="investor-research",
        issuer_id="issuer-1",
    )
    assert expected == derive_retrieval_scope_id(
        source_scope_key="investor-research",
        issuer_id="issuer-1",
    )
    assert len(expected.encode("utf-8")) <= 256
    with pytest.raises(ValueError, match="source scope key"):
        derive_retrieval_scope_id(source_scope_key=" ", issuer_id="issuer-1")
    with pytest.raises(ValueError, match="issuer ID"):
        derive_retrieval_scope_id(source_scope_key="investor-research", issuer_id=" ")
    with pytest.raises(ValueError, match="surrounding whitespace"):
        derive_retrieval_scope_id(
            source_scope_key=" investor-research",
            issuer_id="issuer-1",
        )


def test_cutover_holds_one_snapshot_and_rechecks_registry_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _registry_db()
    registry = _derive_registry(conn)
    path = tmp_path / "ask_retrieval_production_scopes.json"
    path.write_text(canonical_json(registry) + "\n", encoding="utf-8")
    monkeypatch.setattr(cutover, "PRODUCTION_SCOPE_REGISTRY", path)

    def connect(database_path: object, *, role: object) -> sqlite3.Connection:
        del database_path, role
        return conn

    def verify_budget(connection: sqlite3.Connection) -> None:
        del connection

    class Integrity:
        ready = True

    def audit_integrity(connection: sqlite3.Connection) -> Integrity:
        del connection
        return Integrity()

    monkeypatch.setattr(cutover, "connect_sqlite", connect)
    monkeypatch.setattr(cutover, "_verify_claim_audit_budget", verify_budget)
    monkeypatch.setattr(cutover, "audit_answer_audit_integrity", audit_integrity)

    class Ready:
        outcome = "ready"

        def model_dump_json(self) -> str:
            return '{"outcome":"ready"}'

    def assess(
        connection: sqlite3.Connection,
        scopes: object,
        *,
        runtime: object,
    ) -> Ready:
        del scopes, runtime
        assert connection.in_transaction
        path.write_text(path.read_text(encoding="utf-8") + " ", encoding="utf-8")
        return Ready()

    monkeypatch.setattr(cutover, "assess_retrieval_readiness", assess)
    with pytest.raises(SystemExit, match="registry changed during cutover audit"):
        cutover.main(
            [
                "--db",
                str(tmp_path / "unused.db"),
                "--scope-set-sha256",
                str(registry["scope_set_sha256"]),
            ]
        )
