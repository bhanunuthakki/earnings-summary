"""Read-only rollout diagnostics do not certify fact or consumer readiness."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest

from execution.audit_cohort_readiness import main
from provenance.cohort_readiness import audit_cohort_readiness
from provenance.evidence_ledger import ContentBlob, EvidenceLedger, SourceObservation
from provenance.fact_plane_v2 import FactCellV2, FactPlaneV2
from provenance.population_completeness import PopulationTemporalScope
from sqlite_runtime import register_sqlite_integrity_functions

STAMP = datetime(2026, 10, 22, tzinfo=UTC)
SCOPE = PopulationTemporalScope(knowledge_cutoff=STAMP, observed_through=STAMP)


def _identities(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS issuer_entities (issuer_id TEXT PRIMARY KEY,"
        "issuer_key TEXT,entity_kind TEXT,created_at TEXT)"
    )
    conn.executemany(
        "INSERT OR IGNORE INTO issuer_entities VALUES (?,?,?,?)",
        [(name, name, "operating_company", STAMP) for name in ("issuer-a", "issuer-b")],
    )


def test_missing_schema_is_unknown_not_zero() -> None:
    with sqlite3.connect(":memory:") as conn:
        _identities(conn)
        report = audit_cohort_readiness(conn, ("issuer-a",), SCOPE)
    issuer = report.issuers[0]
    assert issuer.acquisition.state == "unknown"
    assert issuer.acquisition.counts["inventories"] is None
    assert issuer.extraction.state == "unknown"
    assert report.has_blockers


def test_empty_current_schema_has_missing_evidence(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    path = migrated_db(tmp_path / "cohort.db")
    with sqlite3.connect(path) as conn:
        _identities(conn)
        report = audit_cohort_readiness(conn, ("issuer-a",), SCOPE)
    issuer = report.issuers[0]
    assert issuer.acquisition.state == "missing"
    assert issuer.acquisition.counts["inventories"] == 0
    assert "source_inventory_missing" in issuer.acquisition.blockers
    assert issuer.extraction.state == "missing"
    assert issuer.semantic_admission.state == "missing"
    assert issuer.consumer_readiness.state == "missing"


def test_inventory_is_issuer_and_clock_scoped_and_does_not_prove_extraction(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    path = migrated_db(tmp_path / "cohort.db")
    with sqlite3.connect(path) as conn:
        _identities(conn)
        for name, issuer, stamp in (
            ("a", "issuer-a", "2026-10-21T00:00:00Z"),
            ("b", "issuer-b", "2026-10-21T00:00:00Z"),
            ("future", "issuer-a", "2026-10-23T00:00:00Z"),
        ):
            conn.execute(
                "INSERT INTO source_inventory_snapshots "
                "(snapshot_id,idempotency_key,inventory_key,revision,issuer_id,source_kind,"
                "source_url,outcome,authoritative,retrieval_config_sha256,collector_code_version,"
                "started_at,completed_at,recorded_at) VALUES (?,?,?,1,?,'ir_crawl',"
                "'https://example.test','failed',1,?,'fixture',?,?,?)",
                (name, name, name, issuer, "a" * 64, stamp, stamp, stamp),
            )
        report = audit_cohort_readiness(conn, ("issuer-a", "issuer-b"), SCOPE)
    assert [row.issuer_id for row in report.issuers] == ["issuer-a", "issuer-b"]
    issuer = report.issuers[0]
    assert issuer.acquisition.counts["inventories"] == 1
    assert issuer.acquisition.state == "observed_unverified"
    assert "source_inventory_incomplete" in issuer.acquisition.blockers
    assert issuer.extraction.counts["processing_obligations"] == 0
    assert issuer.semantic_admission.counts["observations"] == 0
    assert issuer.consumer_readiness.state != "ready"


def test_cli_requires_explicit_scope_and_db() -> None:
    with pytest.raises(SystemExit):
        main([])


def test_nonempty_fact_cells_respect_reporting_identity_and_both_clocks(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    path = migrated_db(tmp_path / "cohort.db")
    with sqlite3.connect(path) as conn:
        _identities(conn)
        register_sqlite_integrity_functions(conn)
        for issuer in ("issuer-a", "issuer-b"):
            conn.execute(
                "INSERT OR IGNORE INTO issuer_entities VALUES (?,?,?,?)",
                (issuer, issuer, "operating_company", STAMP),
            )
            conn.execute(
                "INSERT INTO reporting_entities VALUES (?,?,?,?,?,?)",
                (issuer, issuer, issuer, "legal_registrant", issuer, STAMP),
            )
        for name, issuer, stamp in (
            ("a", "issuer-a", STAMP),
            ("b", "issuer-b", STAMP),
            ("future", "issuer-a", datetime(2026, 10, 23, tzinfo=UTC)),
        ):
            FactPlaneV2(conn).persist_cell(
                FactCellV2(
                    fact_cell_id=name,
                    idempotency_key=name,
                    reporting_entity_id=issuer,
                    concept_namespace="test",
                    concept_name=name,
                    taxonomy_name="fixture",
                    accounting_basis="us_gaap",
                    consolidation_scope="consolidated",
                    period_kind="instant",
                    period_end=STAMP,
                    unit_key="USD",
                    currency="USD",
                    effective_at=STAMP,
                    knowledge_at=stamp,
                    recorded_at=stamp,
                )
            )
        report = audit_cohort_readiness(conn, ("issuer-a",), SCOPE)
    assert report.issuers[0].semantic_admission.counts["fact_cells"] == 1
    assert report.issuers[0].semantic_admission.counts["observations"] == 0
    assert report.readiness_evaluated is False


def test_receipt_clock_count_preserves_fractional_precision() -> None:
    # Minimal schema is intentional: this isolates precision from seal validation.
    with sqlite3.connect(":memory:") as conn:
        _identities(conn)
        conn.execute(
            "CREATE TABLE population_cutover_receipts (knowledge_cutoff TEXT,observed_through TEXT)"
        )
        conn.executemany(
            "INSERT INTO population_cutover_receipts VALUES (?,?)",
            [
                ("2026-10-22T00:00:00.000000Z", "2026-10-22T00:00:00.000000Z"),
                ("2026-10-22T00:00:00.000001Z", "2026-10-22T00:00:00.000000Z"),
            ],
        )
        report = audit_cohort_readiness(conn, ("issuer-a",), SCOPE)
    consumer = report.issuers[0].consumer_readiness
    assert consumer.counts["population_cutover_receipts_at_exact_clocks"] == 1
    assert consumer.state == "observed_unverified"
    assert consumer.evidence_scope == "global"


def test_cli_does_not_mutate_database(
    tmp_path: Path, migrated_db: Callable[..., Path], capsys: pytest.CaptureFixture[str]
) -> None:
    path = migrated_db(tmp_path / "cohort.db")
    with sqlite3.connect(path) as conn:
        _identities(conn)
    before = path.read_bytes()
    assert (
        main(
            [
                "--db-path",
                str(path),
                "--issuer-id",
                "issuer-a",
                "--knowledge-cutoff",
                STAMP.isoformat(),
                "--observed-through",
                STAMP.isoformat(),
            ]
        )
        == 2
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["issuer_scope"] == ["issuer-a"]
    assert payload["mode"] == "read_only"
    assert payload["consumer_audit_executed"] is False
    assert payload["evidence_clock_precision"] == "whole_seconds"
    assert payload["receipt_clock_precision"] == "microseconds"
    assert path.read_bytes() == before


def _query_fixture() -> sqlite3.Connection:
    """Minimal query substrate; these rows deliberately grant no verified receipt.

    The migrated tests above cover the real schema. These focused census tests
    isolate issuer, revision, and clock selection without fabricating valid seals.
    """
    conn = sqlite3.connect(":memory:")
    _identities(conn)
    conn.executescript(
        """
        CREATE TABLE source_obligation_revisions (
            obligation_revision_id TEXT, issuer_id TEXT);
        CREATE TABLE document_processing_obligation_revisions (
            processing_obligation_revision_id TEXT, source_obligation_revision_id TEXT,
            processing_obligation_key TEXT, revision INTEGER, knowledge_at TEXT, recorded_at TEXT);
        CREATE TABLE document_processing_disposition_headers (
            processing_disposition_id TEXT, processing_obligation_revision_id TEXT,
            terminal_status TEXT, knowledge_at TEXT, recorded_at TEXT);
        CREATE TABLE document_processing_disposition_seals (
            processing_disposition_id TEXT, sealed_at TEXT);
        CREATE TABLE reporting_entities (reporting_entity_id TEXT,issuer_id TEXT);
        CREATE TABLE fact_cells_v2 (
            fact_cell_id TEXT, reporting_entity_id TEXT,knowledge_at TEXT,recorded_at TEXT);
        CREATE TABLE fact_observations_v2 (
            observation_id TEXT,fact_cell_id TEXT,knowledge_at TEXT,recorded_at TEXT,numeric_value TEXT);
        CREATE TABLE source_fact_publication_members (
            publication_id TEXT,record_kind TEXT,record_id TEXT,recorded_at TEXT);
        CREATE TABLE source_fact_publication_seals (publication_id TEXT,sealed_at TEXT);
        CREATE TABLE canonical_metric_cells (
            reporting_entity_id TEXT,knowledge_at TEXT,recorded_at TEXT);
        """
    )
    return conn


def test_processing_terminal_census_scopes_issuers_clocks_and_missing_seals() -> None:
    with _query_fixture() as conn:
        for key, issuer, status, clock in (
            ("success", "issuer-a", "succeeded", STAMP.isoformat()),
            ("failure", "issuer-a", "failed", STAMP.isoformat()),
            ("unsealed", "issuer-a", "succeeded", STAMP.isoformat()),
            ("missing", "issuer-a", None, STAMP.isoformat()),
            ("other", "issuer-b", "failed", STAMP.isoformat()),
            ("future", "issuer-a", "failed", "2026-10-23T00:00:00Z"),
        ):
            conn.execute("INSERT INTO source_obligation_revisions VALUES (?,?)", (key, issuer))
            conn.execute(
                "INSERT INTO document_processing_obligation_revisions VALUES (?,?,?,1,?,?)",
                (key, key, key, clock, clock),
            )
            if status:
                conn.execute(
                    "INSERT INTO document_processing_disposition_headers VALUES (?,?,?,?,?)",
                    (key, key, status, clock, clock),
                )
                if key != "unsealed":
                    conn.execute(
                        "INSERT INTO document_processing_disposition_seals VALUES (?,?)",
                        (key, clock),
                    )
        stage = audit_cohort_readiness(conn, ("issuer-a",), SCOPE).issuers[0].extraction
    assert stage.counts["processing_obligations"] == 4
    assert stage.counts["succeeded"] == 1
    assert stage.counts["failed"] == 1
    assert stage.counts["without_sealed_disposition"] == 2
    assert set(stage.blockers) == {"processing_failed", "processing_dispositions_missing"}
    assert stage.state == "observed_unverified"


def test_publication_census_does_not_certify_conflicting_or_unpublished_facts() -> None:
    with _query_fixture() as conn:
        for issuer in ("issuer-a", "issuer-b"):
            conn.execute("INSERT INTO reporting_entities VALUES (?,?)", (issuer, issuer))
            conn.execute(
                "INSERT INTO canonical_metric_cells VALUES (?,?,?)", (issuer, STAMP, STAMP)
            )
        for key, issuer, clock, published in (
            ("first", "issuer-a", STAMP.isoformat(), True),
            ("conflicting", "issuer-a", STAMP.isoformat(), True),
            ("unpublished", "issuer-a", STAMP.isoformat(), False),
            ("other", "issuer-b", STAMP.isoformat(), True),
            ("future", "issuer-a", "2026-10-23T00:00:00Z", True),
        ):
            cell = "first" if key == "conflicting" else key
            if key != "conflicting":
                conn.execute(
                    "INSERT INTO fact_cells_v2 VALUES (?,?,?,?)", (cell, issuer, clock, clock)
                )
            conn.execute(
                "INSERT INTO fact_observations_v2 VALUES (?,?,?,?,?)",
                (key, cell, clock, clock, "200" if key == "conflicting" else "100"),
            )
            if published:
                conn.execute(
                    "INSERT INTO source_fact_publication_members VALUES (?,'fact_observation',?,?)",
                    (key, key, clock),
                )
                conn.execute("INSERT INTO source_fact_publication_seals VALUES (?,?)", (key, clock))
        assert (
            conn.execute(
                "SELECT COUNT(DISTINCT numeric_value) FROM fact_observations_v2 WHERE fact_cell_id='first'"
            ).fetchone()[0]
            == 2
        )
        report = audit_cohort_readiness(conn, ("issuer-a",), SCOPE)
    stage = report.issuers[0].semantic_admission
    assert stage.counts["observations"] == 3
    assert stage.counts["observations_with_publication_seal"] == 2
    assert "source_fact_publication_seals_missing" in stage.blockers
    assert stage.state == "observed_unverified"
    assert stage.verification_required == "semantic_admission_and_publication_not_verified"
    assert report.has_blockers and report.readiness_evaluated is False
    assert report.consumer_audit_executed is False


def test_unknown_or_not_yet_observed_issuer_is_rejected() -> None:
    with sqlite3.connect(":memory:") as conn:
        _identities(conn)
        conn.execute(
            "INSERT INTO issuer_entities VALUES ('future','future','operating_company',?)",
            ("2026-10-23T00:00:00Z",),
        )
        for issuer in ("unknown", "future", " issuer-a "):
            with pytest.raises(ValueError, match="unknown canonical issuer"):
                audit_cohort_readiness(conn, (issuer,), SCOPE)


def test_newer_failed_inventory_never_falls_back_to_older_success(
    tmp_path: Path, migrated_db: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    import provenance.cohort_readiness as census

    selected: list[tuple[str, ...]] = []
    owner = census.reporting_document_scope

    def inspect_scope(
        conn: sqlite3.Connection,
        cutoff: datetime,
        observed: datetime,
        *,
        inventory_snapshot_ids: tuple[str, ...] | None = None,
    ) -> tuple[object, object, int]:
        assert inventory_snapshot_ids is not None
        selected.append(inventory_snapshot_ids)
        return owner(conn, cutoff, observed, inventory_snapshot_ids=inventory_snapshot_ids)

    monkeypatch.setattr(census, "reporting_document_scope", inspect_scope)
    with sqlite3.connect(migrated_db(tmp_path / "latest-inventory.db")) as conn:
        _identities(conn)
        register_sqlite_integrity_functions(conn)
        ledger = EvidenceLedger(conn)
        ledger.persist(
            ContentBlob(
                sha256="a" * 64,
                byte_size=1,
                media_type="text/html",
                storage_uri="file:///synthetic/archive.html",
                recorded_at=STAMP,
            )
        )
        ledger.persist(
            SourceObservation(
                observation_id="archive",
                idempotency_key="archive",
                source_kind="ir_doc",
                source_url="https://example.test",
                blob_sha256="a" * 64,
                source_published_at=None,
                filing_at=None,
                accepted_at=None,
                observed_at=STAMP,
                retrieved_at=STAMP,
                retrieval_config_sha256="a" * 64,
                collector_code_version="fixture",
            )
        )
        for revision, outcome in ((1, "succeeded"), (2, "failed")):
            identity = f"inventory-{revision}"
            conn.execute(
                "INSERT INTO source_inventory_snapshots "
                "(snapshot_id,idempotency_key,inventory_key,revision,issuer_id,source_kind,"
                "source_url,outcome,authoritative,retrieval_config_sha256,collector_code_version,"
                "started_at,completed_at,recorded_at,source_observation_id,supersedes_snapshot_id) "
                "VALUES (?,?,?,?,'issuer-a','ir_crawl','https://example.test',?,1,?,'fixture',?,?,?,?,?)",
                (
                    identity,
                    identity,
                    "same-archive",
                    revision,
                    outcome,
                    "a" * 64,
                    STAMP,
                    STAMP,
                    STAMP,
                    "archive" if revision == 1 else None,
                    "inventory-1" if revision == 2 else None,
                ),
            )
        stage = audit_cohort_readiness(conn, ("issuer-a",), SCOPE).issuers[0].acquisition
    assert selected == [("inventory-2",)]
    assert stage.counts["inventories"] == 1
    assert stage.counts["incomplete_inventories"] == 1
    assert "source_inventory_incomplete" in stage.blockers


@pytest.mark.parametrize("target", ["missing", "checkout", "directory"])
def test_cli_rejects_invalid_database_before_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    target: str,
) -> None:
    from execution import audit_cohort_readiness as cli

    database = {
        "missing": tmp_path / "missing-sensitive-name.db",
        "checkout": Path(cli.__file__).resolve().parents[1] / "data" / "portfolio.db",
        "directory": tmp_path,
    }[target]

    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("An invalid database reached connection creation")

    monkeypatch.setattr(cli, "connect_sqlite", forbidden)
    assert (
        cli.main(
            [
                "--db-path",
                str(database),
                "--issuer-id",
                "issuer-a",
                "--knowledge-cutoff",
                STAMP.isoformat(),
                "--observed-through",
                STAMP.isoformat(),
            ]
        )
        == 2
    )
    assert json.loads(capsys.readouterr().out) == {
        "status": "blocked",
        "reason": "cohort_readiness_input_or_database_error",
    }
    if target == "missing":
        assert not database.exists()


def test_cli_accepts_explicit_configured_external_read_only_database(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import db
    from execution import audit_cohort_readiness as cli
    from sqlite_runtime import SQLiteConnectionRole

    database = migrated_db(tmp_path / "external-authority.sqlite")
    with sqlite3.connect(database) as conn:
        _identities(conn)
    monkeypatch.setattr(db, "DB_PATH", str(database))
    original_connect = cli.connect_sqlite
    opened: list[Path] = []

    def connect(path: Path, *, role: SQLiteConnectionRole) -> sqlite3.Connection:
        assert role is SQLiteConnectionRole.READ_ONLY
        opened.append(path)
        return original_connect(path, role=role)

    monkeypatch.setattr(cli, "connect_sqlite", connect)
    before = database.read_bytes()
    assert (
        cli.main(
            [
                "--db-path",
                str(database),
                "--issuer-id",
                "issuer-a",
                "--knowledge-cutoff",
                STAMP.isoformat(),
                "--observed-through",
                STAMP.isoformat(),
            ]
        )
        == 2
    )  # Valid source, with expected missing cohort evidence.
    assert opened == [database.resolve()]
    assert json.loads(capsys.readouterr().out)["issuer_scope"] == ["issuer-a"]
    assert database.read_bytes() == before
