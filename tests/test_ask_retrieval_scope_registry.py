# pyright: reportPrivateUsage=false
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from ask.audit_store import canonical_json
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
    assert [item.scope_key for item in scopes] == ["scope-1"]
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
