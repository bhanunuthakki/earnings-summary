"""A corpus uses exactly the primary documents of its verified analysis scope."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import pytest

from provenance.analysis_scope import (
    AnalysisEvidenceScope,
    build_analysis_scope,
    resolve_analysis_coverage,
)
from provenance.evidence_ledger import EvidenceLedger, EvidenceNode, ExtractionRun
from provenance.fulltext_extractor_identity import STRUCTURED_WEB_ARCHIVE_FULLTEXT_EXTRACTOR
from provenance.immutable_artifact import ImmutableArtifactConflictError
from runtime.job_runtime import JobLock
from search.corpus_builder import (
    CorpusBuildRequest,
    CorpusBuildResult,
    ExpectedDocument,
    build_grounded_search_corpus,
    load_analysis_expected_document_inventory,
)
from sqlite_runtime import register_sqlite_integrity_functions
from tests.test_analysis_scope import K, add_expected, scope_db, scope_request

STAMP = datetime(2026, 7, 27, 2)


def test_unscoped_inventory_cannot_claim_analysis_namespace(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    expected = ExpectedDocument(
        expected_document_key="missing-document",
        membership_status="missing",
        reason="not yet captured",
    )
    with pytest.raises(ValueError, match="namespace requires"):
        CorpusBuildRequest(
            corpus_key="analysis-scope:" + "a" * 64,
            revision=1,
            selector_code_version="test@1",
            recorded_at=STAMP,
            expected_documents=(expected,),
        )
    request = CorpusBuildRequest(
        corpus_key="ordinary-corpus",
        revision=1,
        selector_code_version="test@1",
        recorded_at=STAMP,
        expected_documents=(expected,),
    ).model_copy(update={"corpus_key": "analysis-scope:" + "a" * 64})
    conn = _conn(tmp_path, migrated_db)
    try:
        with pytest.raises(ValueError, match="namespace requires"):
            build_grounded_search_corpus(conn, request)
    finally:
        conn.close()


def _scoped_request(conn: sqlite3.Connection, scope: AnalysisEvidenceScope) -> CorpusBuildRequest:
    inventory, snapshots = load_analysis_expected_document_inventory(
        conn, scope, cutoff_at=K, observed_through=K
    )
    return CorpusBuildRequest(
        corpus_key=scope.scope_id,
        revision=1,
        selector_code_version="analysis-test@1",
        recorded_at=K,
        knowledge_cutoff=K,
        expected_documents=inventory.expected_documents,
        source_inventory_snapshot_ids=snapshots,
        analysis_scope=scope,
    )


def _seed_primary_extraction(conn: sqlite3.Connection) -> None:
    identity = STRUCTURED_WEB_ARCHIVE_FULLTEXT_EXTRACTOR
    ledger = EvidenceLedger(conn)
    digest = str(
        conn.execute(
            "SELECT blob_sha256 FROM evidence_document_versions WHERE document_version_id='document:expected-10k'"
        ).fetchone()[0]
    )
    ledger.persist(
        ExtractionRun(
            extraction_run_id="scope-fulltext",
            idempotency_key="scope-fulltext",
            document_version_id="document:expected-10k",
            input_sha256=digest,
            extractor_name=identity.name,
            extractor_config_sha256=identity.config_sha256,
            extractor_code_version=identity.code_version,
            output_sha256="a" * 64,
            started_at=K,
            completed_at=K,
            outcome="succeeded",
        )
    )
    ledger.persist(
        EvidenceNode(
            node_id="scope-passage",
            evidence_key="scope-passage",
            revision=1,
            extraction_run_id="scope-fulltext",
            node_kind="passage",
            text="Revenue increased.",
            recorded_at=K,
        )
    )
    conn.commit()


def test_scoped_corpus_indexes_primary_and_preserves_missing_archive(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    conn, scope = scope_db(tmp_path, migrated_db)
    try:
        register_sqlite_integrity_functions(conn)
        _seed_primary_extraction(conn)
        result = build_grounded_search_corpus(
            conn, _scoped_request(conn, scope).model_copy(update={"apply": True})
        )
        assert result.completion_status == "complete"
        assert result.expected_document_count == result.included_document_count == 1
        assert conn.execute(
            "SELECT document_version_id FROM search_corpus_document_memberships WHERE manifest_id=?",
            (result.manifest_id,),
        ).fetchall() == [("document:expected-10k",)]
        outside = next(
            item
            for item in resolve_analysis_coverage(conn, scope, K, K)
            if item.expected_document_id == "old-missing"
        )
        assert outside.role == "outside_scope"
        assert outside.coverage_status == "unassessed"
    finally:
        conn.close()


def test_scoped_corpus_requires_package_dependencies(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    conn, _scope = scope_db(tmp_path, migrated_db)
    try:
        add_expected(
            conn, "required-missing", "0000000001-26-000001", document_type="sec_financial_report"
        )
        scope = build_analysis_scope(conn, scope_request())
        with pytest.raises(ValueError, match="capture is incomplete"):
            _scoped_request(conn, scope)
        assert conn.execute("SELECT COUNT(*) FROM search_corpus_manifests").fetchone()[0] == 0
    finally:
        conn.close()


@pytest.mark.parametrize(
    "change",
    ["scope_key", "scope_receipt", "document_key", "document_id", "snapshot", "extra_dependency"],
)
def test_scoped_corpus_rejects_unbound_identities(
    tmp_path: Path, migrated_db: Callable[..., Path], change: str
) -> None:
    conn, scope = scope_db(tmp_path, migrated_db)
    try:
        request = _scoped_request(conn, scope)
        if change == "scope_key":
            request = request.model_copy(update={"corpus_key": "other-analysis"})
        elif change == "scope_receipt":
            request = request.model_copy(
                update={"analysis_scope": scope.model_copy(update={"scope_sha256": "0" * 64})}
            )
        elif change == "snapshot":
            request = request.model_copy(
                update={"source_inventory_snapshot_ids": ("other-inventory",)}
            )
        elif change == "extra_dependency":
            request = request.model_copy(
                update={
                    "expected_documents": (
                        *request.expected_documents,
                        ExpectedDocument(
                            expected_document_key="generated-report",
                            document_version_id="document:generated-report",
                            membership_status="included",
                            reason="extra",
                        ),
                    )
                }
            )
        else:
            changed = request.expected_documents[0].model_copy(
                update={
                    "expected_document_key"
                    if change == "document_key"
                    else "document_version_id": "wrong",
                }
            )
            request = request.model_copy(update={"expected_documents": (changed,)})
        with pytest.raises(ValueError):
            build_grounded_search_corpus(conn, request)
        assert conn.execute("SELECT COUNT(*) FROM search_corpus_manifests").fetchone()[0] == 0
    finally:
        conn.close()


def test_scoped_corpus_rejects_stale_receipt(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    conn, scope = scope_db(tmp_path, migrated_db)
    try:
        request = _scoped_request(conn, scope)
        add_expected(
            conn,
            "new-dependency",
            "0000000001-26-000001",
            document_type="sec_financial_report",
        )
        with pytest.raises(ValueError, match="stale"):
            build_grounded_search_corpus(conn, request)
    finally:
        conn.close()


def test_scoped_corpus_rechecks_scope_before_atomic_publication(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    conn, scope = scope_db(tmp_path, migrated_db)
    try:
        register_sqlite_integrity_functions(conn)
        _seed_primary_extraction(conn)
        request = _scoped_request(conn, scope).model_copy(
            update={"apply": True, "persist_batch_size": 1}
        )

        def change_inventory(_count: int) -> None:
            add_expected(
                conn,
                "new-dependency",
                "0000000001-26-000001",
                document_type="sec_financial_report",
            )
            conn.commit()

        with pytest.raises(ValueError, match="stale"):
            build_grounded_search_corpus(conn, request, change_inventory)
        assert conn.execute("SELECT COUNT(*) FROM search_corpus_manifest_seals").fetchone()[0] == 0
    finally:
        conn.close()


def test_cli_builds_scoped_primary_inventory_without_unsafe_opt_in(
    tmp_path: Path, migrated_db: Callable[..., Path], capsys: pytest.CaptureFixture[str]
) -> None:
    from execution.build_grounded_search_corpus import main

    conn, scope = scope_db(tmp_path, migrated_db)
    database = Path(str(conn.execute("PRAGMA database_list").fetchone()[2]))
    try:
        _seed_primary_extraction(conn)
    finally:
        conn.close()
    receipt = tmp_path / "scope.json"
    receipt.write_text(scope.model_dump_json() + "\n", encoding="utf-8")
    assert (
        main(
            [
                "--db",
                str(database),
                "--analysis-scope",
                str(receipt),
                "--corpus-key",
                scope.scope_id,
                "--revision",
                "1",
                "--selector-code-version",
                "test@1",
                "--recorded-at",
                K.isoformat(),
                "--knowledge-cutoff",
                K.isoformat(),
            ]
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out)
    assert result["expected_document_count"] == 1
    assert result["completion_status"] == "complete"
    assert result["records_created"] == 0


def _scope_cli_args(database: Path, receipt: Path, scope: AnalysisEvidenceScope) -> list[str]:
    return [
        "--db",
        str(database),
        "--analysis-scope",
        str(receipt),
        "--corpus-key",
        scope.scope_id,
        "--revision",
        "1",
        "--selector-code-version",
        "test@1",
        "--recorded-at",
        K.isoformat(),
        "--knowledge-cutoff",
        K.isoformat(),
    ]


def test_cli_scope_requires_canonical_serialization(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    from execution.build_grounded_search_corpus import main

    conn, scope = scope_db(tmp_path, migrated_db)
    database = Path(str(conn.execute("PRAGMA database_list").fetchone()[2]))
    conn.commit()
    conn.close()
    receipt = tmp_path / "alternate-scope.json"
    receipt.write_text(json.dumps(scope.model_dump(mode="json"), indent=2) + "\n", encoding="utf-8")
    with pytest.raises(ImmutableArtifactConflictError, match="canonically serialized"):
        main(_scope_cli_args(database, receipt, scope))
    with sqlite3.connect(database) as inspect:
        assert inspect.execute("SELECT COUNT(*) FROM search_corpus_manifests").fetchone()[0] == 0


@pytest.mark.parametrize("mutation_at", ["before_seals", "after_seals", "dry_run"])
def test_cli_scope_mutation_prevents_corpus_publication(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    mutation_at: str,
) -> None:
    from execution import build_grounded_search_corpus as cli

    conn, scope = scope_db(tmp_path, migrated_db)
    database = Path(str(conn.execute("PRAGMA database_list").fetchone()[2]))
    try:
        _seed_primary_extraction(conn)
    finally:
        conn.close()
    receipt = tmp_path / "scope.json"
    receipt.write_text(scope.model_dump_json() + "\n", encoding="utf-8")
    monkeypatch.setattr(cli, "PROJECT_ROOT", tmp_path)
    real_builder = cli.build_grounded_search_corpus
    real_lock = cli.JobLock
    lock_resources: list[str] = []

    def locked(root: Path, job_name: str, resources: list[str]) -> JobLock:
        lock_resources.extend(resources)
        return real_lock(root, job_name, resources)

    def mutate(_count: int) -> None:
        receipt.write_text(scope.model_dump_json() + "\n\n", encoding="utf-8")

    def building(
        database_conn: sqlite3.Connection,
        request: CorpusBuildRequest,
        *,
        before_publish: Callable[[], None] | None = None,
    ) -> CorpusBuildResult:
        if mutation_at == "dry_run":
            result = real_builder(database_conn, request, before_publish=before_publish)
            mutate(0)
            return result
        checks = 0

        def guard() -> None:
            nonlocal checks
            checks += 1
            assert database_conn.in_transaction
            if mutation_at == "after_seals" and checks == 2:
                assert (
                    database_conn.execute(
                        "SELECT COUNT(*) FROM search_corpus_manifest_seals"
                    ).fetchone()[0]
                    == 1
                )
                mutate(0)
            assert before_publish is not None
            before_publish()

        return real_builder(
            database_conn,
            request,
            on_chunk_batch_complete=mutate if mutation_at == "before_seals" else None,
            before_publish=guard,
        )

    monkeypatch.setattr(cli, "JobLock", locked)
    monkeypatch.setattr(cli, "build_grounded_search_corpus", building)
    with pytest.raises(ImmutableArtifactConflictError, match="changed after admission"):
        cli.main(
            [
                *_scope_cli_args(database, receipt, scope),
                *([] if mutation_at == "dry_run" else ["--apply"]),
            ]
        )
    if mutation_at != "dry_run":
        assert f"artifact:{receipt.resolve()}" in lock_resources
    with sqlite3.connect(database) as inspect:
        # Batch staging remains available for recovery. Nothing becomes queryable.
        assert inspect.execute("SELECT COUNT(*) FROM search_corpus_manifests").fetchone()[0] == (
            0 if mutation_at == "dry_run" else 1
        )
        assert (
            inspect.execute("SELECT COUNT(*) FROM search_corpus_manifest_seals").fetchone()[0] == 0
        )
        assert inspect.execute("SELECT COUNT(*) FROM search_index_runs").fetchone()[0] == 0
        assert inspect.execute("SELECT COUNT(*) FROM search_projection_seals").fetchone()[0] == 0


@pytest.mark.parametrize(
    "extra",
    [
        ["--allow-unsealed-inventory"],
        ["--inventory", "other.json"],
        ["--coverage-inventory-key", "other"],
    ],
)
def test_cli_scope_does_not_mix_other_inventory_modes(tmp_path: Path, extra: list[str]) -> None:
    from execution.build_grounded_search_corpus import main

    with pytest.raises(SystemExit) as error:
        main(
            [
                "--db",
                str(tmp_path / "absent.db"),
                "--analysis-scope",
                "scope.json",
                "--corpus-key",
                "scope",
                "--revision",
                "1",
                "--selector-code-version",
                "test@1",
                "--recorded-at",
                K.isoformat(),
                *extra,
            ]
        )
    assert error.value.code == 2


def _conn(tmp_path: Path, migrated_db: Callable[..., Path]) -> sqlite3.Connection:
    conn = sqlite3.connect(migrated_db(tmp_path / "corpus.db", target="head"))
    conn.execute("PRAGMA foreign_keys = ON")
    register_sqlite_integrity_functions(conn)
    return conn


def test_unscoped_manifest_hash_and_request_serialization_remain_unchanged(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    conn = _conn(tmp_path, migrated_db)
    try:
        request = CorpusBuildRequest(
            corpus_key="unscoped:legacy",
            revision=1,
            selector_code_version="test@1",
            recorded_at=STAMP,
            expected_documents=(
                ExpectedDocument(
                    expected_document_key="missing-document",
                    membership_status="missing",
                    reason="not yet captured",
                ),
            ),
        )
        assert "analysis_scope" not in request.model_dump(mode="json")
        # Fixed from the pre-scope metadata builder for these exact inputs.
        assert (
            build_grounded_search_corpus(conn, request).manifest_config_sha256
            == (
                "e59da7cbda57fb72e24a3d53238f8eb0d40ddca2e12a227cac6dc0d9db16812a"  # pragma: allowlist secret -- fixed regression corpus digest
            )
        )
    finally:
        conn.close()


def test_cli_closes_database_when_inventory_read_fails(
    tmp_path: Path, migrated_db: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    from execution import build_grounded_search_corpus as cli

    conn = _conn(tmp_path, migrated_db)

    def fake_connect(*_args: object, **_kwargs: object) -> sqlite3.Connection:
        return conn

    monkeypatch.setattr(cli, "connect_sqlite", fake_connect)
    with pytest.raises(FileNotFoundError):
        cli.main(
            [
                "--db",
                str(tmp_path / "corpus.db"),
                "--inventory",
                str(tmp_path / "absent.json"),
                "--allow-unsealed-inventory",
                "--corpus-key",
                "test",
                "--revision",
                "1",
                "--selector-code-version",
                "test@1",
                "--recorded-at",
                STAMP.isoformat(),
            ]
        )
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        conn.execute("SELECT 1")
