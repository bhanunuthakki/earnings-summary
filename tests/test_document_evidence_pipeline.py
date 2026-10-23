"""Continuation uses exact byte/extractor proof, never download counters or LLMs."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from provenance import document_evidence_pipeline as pipeline
from provenance.document_evidence_pipeline import DocumentEvidenceRequest, process_document_evidence


@pytest.fixture
def connection(tmp_path: Path, migrated_db: Callable[..., Path]) -> Iterator[sqlite3.Connection]:
    db = migrated_db(tmp_path / "fixture.db")
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    yield conn
    conn.close()


def _seed(
    conn: sqlite3.Connection,
    root: Path,
    *,
    ticker: str = "ACME",
    role: str = "portfolio",
    instrument: str = "equity",
    suffix: str = ".txt",
) -> int:
    content = f"{ticker} management reported revenue grew twenty percent.".encode()
    path = root / f"{ticker}{suffix}"
    path.write_bytes(content)
    conn.execute(
        "INSERT INTO tracked_companies (ticker, name, list_type, instrument_type) "
        "VALUES (?, ?, ?, ?)",
        (ticker, ticker, role, instrument),
    )
    row = conn.execute(
        "INSERT INTO documents (ticker, source_type, doc_type, file_path, sha256, "
        "fetched_at, fetch_status, raw_bytes_size) VALUES (?, 'ir_doc', "
        "'earnings_release', ?, ?, '2026-09-18T12:00:00Z', 'ok', ?) RETURNING id",
        (ticker, path.name, hashlib.sha256(content).hexdigest(), len(content)),
    ).fetchone()
    assert row is not None
    conn.commit()
    return int(row[0])


def test_plan_is_read_only_and_does_not_claim_extraction(
    connection: sqlite3.Connection, tmp_path: Path
) -> None:
    _seed(connection, tmp_path)
    before = connection.total_changes
    result = process_document_evidence(connection, DocumentEvidenceRequest(repo_root=tmp_path))
    assert connection.total_changes == before
    assert result.items[0].capture_planned
    assert result.items[0].status == "not-attempted"
    assert result.captured == result.extracted == 0
    assert result.inventory_uninitialized == 1
    assert result.degraded
    assert not (tmp_path / ".tmp").exists()


@pytest.mark.parametrize("role", ["portfolio", "watchlist", "evaluation"])
def test_apply_and_replay_use_exact_evidence_without_claiming_inventory(
    connection: sqlite3.Connection, tmp_path: Path, role: str
) -> None:
    document_id = _seed(connection, tmp_path, role=role)
    request = DocumentEvidenceRequest(repo_root=tmp_path, apply=True, document_id=document_id)
    first = process_document_evidence(connection, request)
    assert first.items[0].status == "extracted"
    assert first.captured == first.extracted == 1
    assert first.inventory_uninitialized == 1
    assert first.degraded
    before = connection.total_changes
    second = process_document_evidence(connection, request)
    assert second.items[0].status == "already-covered"
    assert second.captured == second.extracted == 0
    assert connection.total_changes == before
    assert not (tmp_path / ".tmp").exists()


def test_byte_mutation_is_quarantined_even_after_extraction(
    connection: sqlite3.Connection, tmp_path: Path
) -> None:
    _seed(connection, tmp_path)
    request = DocumentEvidenceRequest(repo_root=tmp_path, apply=True)
    process_document_evidence(connection, request)
    (tmp_path / "ACME.txt").write_bytes(b"changed source")
    result = process_document_evidence(connection, request)
    assert result.items[0].status == "quarantined"
    assert "sha256_mismatch" in result.items[0].findings
    assert result.extracted == 0


def test_failure_keeps_capture_and_resumes_extraction(
    connection: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(connection, tmp_path)
    owner = pipeline.backfill_fulltext_evidence

    def fail(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("parser unavailable")

    monkeypatch.setattr(pipeline, "backfill_fulltext_evidence", fail)
    request = DocumentEvidenceRequest(repo_root=tmp_path, apply=True)
    first = process_document_evidence(connection, request)
    assert first.items[0].status == "failed"
    assert first.captured == 1
    assert not connection.in_transaction
    monkeypatch.setattr(pipeline, "backfill_fulltext_evidence", owner)
    resumed = process_document_evidence(connection, request)
    assert resumed.captured == 0
    assert resumed.extracted == 1


def test_quarantine_does_not_hide_later_page(
    connection: sqlite3.Connection, tmp_path: Path
) -> None:
    _seed(connection, tmp_path)
    second_id = _seed(connection, tmp_path, ticker="BETA", role="evaluation")
    (tmp_path / "BETA.txt").unlink()
    first = process_document_evidence(
        connection, DocumentEvidenceRequest(repo_root=tmp_path, apply=True, batch_size=1)
    )
    assert first.items[0].status == "quarantined"
    assert first.has_more
    second = process_document_evidence(
        connection,
        DocumentEvidenceRequest(
            repo_root=tmp_path,
            apply=True,
            batch_size=1,
            before_document_id=first.next_before_document_id,
        ),
    )
    assert first.items[0].document_id == second_id
    assert second.items[0].ticker == "ACME"
    assert second.extracted == 1
    assert not second.has_more


def test_unsupported_format_and_invalid_instrument_are_explicit(
    connection: sqlite3.Connection, tmp_path: Path
) -> None:
    _seed(connection, tmp_path, suffix=".mp3")
    _seed(connection, tmp_path, ticker="ETF", instrument="etf")
    result = process_document_evidence(
        connection, DocumentEvidenceRequest(repo_root=tmp_path, apply=True)
    )
    assert result.status_counts == {"unsupported": 1, "not-attempted": 1}
    assert "stored_identity_denied:stored_instrument_not_applicable" in result.items[0].findings
    assert result.degraded


def test_mutating_legacy_hash_cannot_reuse_immutable_extraction_proof(
    connection: sqlite3.Connection, tmp_path: Path
) -> None:
    document_id = _seed(connection, tmp_path)
    request = DocumentEvidenceRequest(repo_root=tmp_path, apply=True)
    process_document_evidence(connection, request)
    replacement = b"Different bytes and a different compatibility hash."
    (tmp_path / "ACME.txt").write_bytes(replacement)
    connection.execute(
        "UPDATE documents SET sha256 = ?, raw_bytes_size = ? WHERE id = ?",
        (hashlib.sha256(replacement).hexdigest(), len(replacement), document_id),
    )
    connection.commit()
    result = process_document_evidence(connection, request)
    assert result.items[0].status == "quarantined"
    assert "immutable_document_identity_mismatch" in result.items[0].findings


def test_capture_failure_rolls_back_its_entire_transaction(
    connection: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    document_id = _seed(connection, tmp_path)
    owner = pipeline.ensure_legacy_document_evidence

    def fail(conn: sqlite3.Connection, *, repo_root: Path, document_id: int) -> None:
        owner(conn, repo_root=repo_root, document_id=document_id)
        raise RuntimeError("failure after capture before commit")

    monkeypatch.setattr(pipeline, "ensure_legacy_document_evidence", fail)
    result = process_document_evidence(
        connection, DocumentEvidenceRequest(repo_root=tmp_path, apply=True)
    )
    assert result.items[0].status == "failed"
    assert result.captured == 0
    assert (
        connection.execute(
            "SELECT count(*) FROM evidence_document_versions WHERE legacy_document_id = ?",
            (document_id,),
        ).fetchone()[0]
        == 0
    )
    assert not connection.in_transaction


def test_missing_schema_is_explicitly_unsupported(tmp_path: Path) -> None:
    with sqlite3.connect(":memory:") as conn:
        result = process_document_evidence(conn, DocumentEvidenceRequest(repo_root=tmp_path))
    assert result.degraded
    assert "unsupported_schema:evidence_document_versions" in result.findings


def test_resume_receipt_drains_backlog_and_new_arrivals_without_reparse(
    connection: sqlite3.Connection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from execution import process_document_evidence as cli

    monkeypatch.setattr(cli, "PROJECT_ROOT", tmp_path)
    old_id = _seed(connection, tmp_path, ticker="OLD")
    _seed(connection, tmp_path, ticker="MID")
    newest_id = _seed(connection, tmp_path, ticker="NEW")
    database = Path(connection.execute("PRAGMA database_list").fetchone()[2])
    argv = [
        "--db",
        str(database),
        "--repo-root",
        str(tmp_path),
        "--apply",
        "--resume",
        "--batch-size",
        "2",
    ]
    assert cli.main(argv) == 2  # Inventory absence is still degraded.
    receipt_path = tmp_path / ".tmp" / "operations" / "runtime" / "document-evidence.latest.json"
    first = cli.DocumentEvidenceResumeReceipt.model_validate_json(receipt_path.read_text())
    assert first.newest_document_id_seen == newest_id
    assert first.next_before_document_id > old_id
    incoming_id = _seed(connection, tmp_path, ticker="INCOMING", role="watchlist")
    assert cli.main(argv) == 2
    second = cli.DocumentEvidenceResumeReceipt.model_validate_json(receipt_path.read_text())
    assert {item.document_id for item in second.result.items} == {incoming_id, old_id}
    assert second.newest_document_id_seen == incoming_id
    assert second.next_before_document_id == 0
    assert second.result.extracted == 2
    assert cli.main(argv) == 2
    third = cli.DocumentEvidenceResumeReceipt.model_validate_json(receipt_path.read_text())
    assert third.result.extracted == third.result.captured == 0
    assert all(item.status == "already-covered" for item in third.result.items)
    assert third.pending_document_ids  # Source inventories were never invented.
    capsys.readouterr()


@pytest.mark.parametrize("resource", ["sqlite", "receipt"])
def test_cli_rejects_overlapping_writer_before_opening_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, resource: str
) -> None:
    from execution import process_document_evidence as cli
    from runtime.job_runtime import JobLock

    monkeypatch.setattr(cli, "PROJECT_ROOT", tmp_path)
    database = tmp_path / "absent.db"
    receipt = tmp_path / ".tmp" / "operations" / "runtime" / "document-evidence.latest.json"
    write_set = {
        "sqlite": f"sqlite:{database.resolve()}",
        "portfolio": "portfolio-db",
        "receipt": f"artifact:{receipt}",
    }[resource]
    with JobLock(tmp_path, "other-owner", [write_set], wait_s=0):
        assert cli.main(["--db", str(database), "--repo-root", str(tmp_path), "--apply"]) == 75
    assert not database.exists()


def test_explicit_database_lock_contends_across_code_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from execution import process_document_evidence as cli
    from run_lock import hold_run_lock

    monkeypatch.setattr(cli, "PROJECT_ROOT", tmp_path / "other-code-checkout")
    monkeypatch.setenv("EARNINGS_SUMMARY_DB_PATH", str(tmp_path / "different-configured.db"))
    database = tmp_path / "actual-target.db"
    with hold_run_lock(database, owner="other-checkout", timeout_s=0):
        assert cli.main(["--db", str(database), "--repo-root", str(tmp_path), "--apply"]) == 75
    assert not database.exists()


def test_exact_inherited_database_lock_is_not_reacquired(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from execution import process_document_evidence as cli
    from runtime.job_runtime import JobLock

    monkeypatch.setattr(cli, "PROJECT_ROOT", tmp_path)
    database = tmp_path / "actual-target.db"
    monkeypatch.setenv("EARNINGS_SUMMARY_DB_PATH", str(database))
    calls: list[Path] = []

    def run(
        db_path: Path, _request: DocumentEvidenceRequest, *, receipt_path: Path | None = None
    ) -> int:
        del receipt_path
        calls.append(db_path)
        return 0

    monkeypatch.setattr(cli, "_run", run)
    with JobLock(tmp_path, "parent", ["portfolio-db"], wait_s=0) as parent:
        monkeypatch.setenv("EARNINGS_SUMMARY_JOB_LOCK_PROOF", parent.inheritance_proof())
        assert cli.main(["--db", str(database), "--repo-root", str(tmp_path), "--apply"]) == 0
    assert calls == [database]


@pytest.mark.parametrize("resume", [False, True])
@pytest.mark.parametrize(
    ("coverage_statuses", "degraded"),
    [
        ((), True),
        (("available",), True),
        (("not_published",), True),
        (("not_discovered",), True),
        (("fetch_failed",), True),
        (("quarantined",), True),
        (("captured",), True),
        (("unsupported",), True),
        (("authority_unavailable",), True),
        (("extracted", "quarantined"), True),
        (("extracted",), False),
        (("indexed",), False),
        (("extracted", "indexed"), False),
    ],
)
def test_cli_status_and_resume_pending_require_satisfied_coverage(
    connection: sqlite3.Connection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    resume: bool,
    coverage_statuses: tuple[str, ...],
    degraded: bool,
) -> None:
    from execution import process_document_evidence as cli
    from provenance.source_coverage_refresh import CoverageRefreshRequest, CoverageRefreshResult

    document_id = _seed(connection, tmp_path)
    database = Path(connection.execute("PRAGMA database_list").fetchone()[2])

    def inventory_context(_conn: sqlite3.Connection, item: pipeline.DocumentEvidenceItem) -> None:
        item.inventory_state = "linked"
        item.inventory_keys = ("synthetic-inventory",)
        item.coverage_statuses = coverage_statuses

    def coverage_refresh(
        _conn: sqlite3.Connection, request: CoverageRefreshRequest
    ) -> CoverageRefreshResult:
        # The coverage owner can retain a prior degraded disposition even after
        # fulltext extraction succeeds; the coordinator must report that state.
        return CoverageRefreshResult(
            mode="apply",
            dry_run=False,
            inventory_keys=request.inventory_keys,
            assessments_considered=0,
            assessments_planned=0,
            assessments_created=0,
            assessments_replayed=0,
            target_status_counts={},
            has_more=False,
        )

    monkeypatch.setattr(pipeline, "_inventory_context", inventory_context)
    monkeypatch.setattr(pipeline, "refresh_source_coverage", coverage_refresh)
    monkeypatch.setattr(cli, "PROJECT_ROOT", tmp_path)
    argv = ["--db", str(database), "--repo-root", str(tmp_path), "--apply", "--batch-size", "2"]
    if resume:
        argv.append("--resume")
    assert cli.main(argv) == (2 if degraded else 0)
    result = pipeline.DocumentEvidenceResult.model_validate_json(capsys.readouterr().out)
    assert result.degraded is degraded
    assert result.items[0].status == "extracted"
    if resume:
        receipt_path = (
            tmp_path / ".tmp" / "operations" / "runtime" / "document-evidence.latest.json"
        )
        receipt = cli.DocumentEvidenceResumeReceipt.model_validate_json(receipt_path.read_text())
        assert receipt.pending_document_ids == ((document_id,) if degraded else ())


@pytest.mark.parametrize(
    ("before", "newer", "selected", "ticker", "expected"),
    [
        (0, None, None, None, (3, 2, 1)),
        (3, None, None, None, (2, 1)),
        (0, 1, None, None, (2, 3)),
        (4, 0, None, None, (1, 2, 3)),
        (0, None, 2, None, (2,)),
        (2, None, 2, None, ()),
        (3, 2, 2, None, ()),
        (3, 1, 2, " mid ", (2,)),
        (2, 2, None, None, ()),
        (3, 1, 2, "OTHER", ()),
        (0, None, None, "MID' OR 1=1 --", ()),
    ],
)
def test_pagination_combined_bounds_preserve_all_filters(
    connection: sqlite3.Connection,
    tmp_path: Path,
    before: int,
    newer: int | None,
    selected: int | None,
    ticker: str | None,
    expected: tuple[int, ...],
) -> None:
    ids = [_seed(connection, tmp_path, ticker=name) for name in ("OLD", "MID", "NEW")]
    first = ids[0]
    result = process_document_evidence(
        connection,
        DocumentEvidenceRequest(
            repo_root=tmp_path,
            before_document_id=first + before - 1 if before else 0,
            newer_than_document_id=first + newer - 1 if newer else newer,
            document_id=first + selected - 1 if selected else None,
            ticker=ticker,
        ),
    )
    assert [item.document_id for item in result.items] == [first + index - 1 for index in expected]


@pytest.mark.parametrize("selection", ["descending_page", "exact_document", "ascending_page"])
def test_pagination_seeks_both_primary_key_bounds(
    connection: sqlite3.Connection, tmp_path: Path, selection: str
) -> None:
    ids = [_seed(connection, tmp_path, ticker=name) for name in ("OLD", "MID", "NEW")]
    statements: list[str] = []

    def capture(statement: str) -> None:
        if "SELECT document.id, document.ticker FROM documents AS document" in statement:
            statements.append(statement)

    connection.set_trace_callback(capture)
    request = DocumentEvidenceRequest(
        repo_root=tmp_path,
        before_document_id=ids[-1] if selection != "exact_document" else 0,
        newer_than_document_id=ids[0] if selection == "ascending_page" else None,
        document_id=ids[0] if selection == "exact_document" else None,
    )
    try:
        process_document_evidence(connection, request)
    finally:
        connection.set_trace_callback(None)
    assert len(statements) == 1
    # Inspect the actual executed query, not a duplicate of production SQL.
    plan = connection.execute("EXPLAIN QUERY PLAN " + statements[0]).fetchall()
    document_search = [str(row[3]) for row in plan if "SEARCH document " in str(row[3])]
    assert len(document_search) == 1
    assert "INTEGER PRIMARY KEY" in document_search[0]
    assert "rowid>?" in document_search[0] and "rowid<?" in document_search[0]


@pytest.mark.parametrize("resume", [False, True])
def test_retained_pdf_coverage_skips_backfill_and_scheduled_resume(
    connection: sqlite3.Connection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    resume: bool,
) -> None:
    from datetime import UTC, datetime

    from execution import process_document_evidence as cli
    from provenance.evidence_backfill import ensure_legacy_document_evidence
    from provenance.evidence_ledger import (
        EvidenceLedger,
        EvidenceLocator,
        EvidenceNode,
        ExtractionRun,
    )
    from provenance.fulltext_backfill import FullTextBackfillRequest, backfill_fulltext_evidence
    from provenance.fulltext_extractor_identity import RETAINED_PDF_FULLTEXT_EXTRACTOR_6_19_0

    document_id = _seed(connection, tmp_path, suffix=".pdf")
    ensure_legacy_document_evidence(connection, repo_root=tmp_path, document_id=document_id)
    version = connection.execute(
        "SELECT document_version_id,blob_sha256 FROM evidence_document_versions WHERE legacy_document_id=?",
        (document_id,),
    ).fetchone()
    identity = RETAINED_PDF_FULLTEXT_EXTRACTOR_6_19_0
    stamp = datetime(2026, 9, 18, 12, tzinfo=UTC)
    ledger = EvidenceLedger(connection)
    ledger.persist(
        ExtractionRun(
            extraction_run_id="retained-pdf",
            idempotency_key="retained-pdf",
            document_version_id=str(version[0]),
            input_sha256=str(version[1]),
            extractor_name=identity.name,
            extractor_code_version=identity.code_version,
            extractor_config_sha256=identity.config_sha256,
            output_sha256="c" * 64,
            started_at=stamp,
            completed_at=stamp,
            outcome="succeeded",
        )
    )
    ledger.persist(
        EvidenceNode(
            node_id="retained-page",
            evidence_key="retained-page",
            revision=1,
            extraction_run_id="retained-pdf",
            node_kind="pdf_page",
            text="Reported revenue.",
            locator=EvidenceLocator(source_ref="ACME.pdf", page_number=1),
            recorded_at=stamp,
        )
    )
    connection.commit()
    before_runs = tuple(connection.execute("SELECT * FROM evidence_extraction_runs"))
    before_nodes = tuple(connection.execute("SELECT * FROM evidence_nodes"))
    if resume:
        monkeypatch.setattr(cli, "PROJECT_ROOT", tmp_path)
        database = str(connection.execute("PRAGMA database_list").fetchone()[2])
        argv = [
            "--db",
            database,
            "--repo-root",
            str(tmp_path),
            "--apply",
            "--resume",
            "--batch-size",
            "2",
        ]
        for _ in range(2):
            assert cli.main(argv) == 2  # Missing source inventories stay degraded.
            receipt = tmp_path / ".tmp/operations/runtime/document-evidence.latest.json"
            result = cli.DocumentEvidenceResumeReceipt.model_validate_json(
                receipt.read_text()
            ).result
            assert result.extracted == 0
            assert result.items[0].status == "already-covered"
    else:
        backfill = backfill_fulltext_evidence(
            connection, FullTextBackfillRequest(repo_root=tmp_path, apply=True)
        )
        assert backfill.documents_skipped_covered == 1
        assert backfill.documents_extracted == backfill.records_created == 0
    assert tuple(connection.execute("SELECT * FROM evidence_extraction_runs")) == before_runs
    assert tuple(connection.execute("SELECT * FROM evidence_nodes")) == before_nodes


@pytest.mark.parametrize("ascending", [False, True])
@pytest.mark.parametrize("filtered", [False, True])
def test_source_filter_preserves_both_pagination_directions(
    connection: sqlite3.Connection, tmp_path: Path, ascending: bool, filtered: bool
) -> None:
    from models.documents import SourceType

    ir_id = _seed(connection, tmp_path)
    row = connection.execute(
        "INSERT INTO documents (ticker, source_type, doc_type, file_path, sha256, "
        "fetched_at, fetch_status, raw_bytes_size) VALUES "
        "('ACME', 'fmp', 'earnings_release', 'fmp.txt', ?, '2026-09-18T12:00:00Z', 'ok', 1) "
        "RETURNING id",
        (hashlib.sha256(b"fmp").hexdigest(),),
    ).fetchone()
    assert row is not None
    fmp_id = int(row[0])
    connection.commit()
    result = process_document_evidence(
        connection,
        DocumentEvidenceRequest(
            repo_root=tmp_path,
            ticker="ACME",
            source_type=SourceType.IR_DOC if filtered else None,
            newer_than_document_id=0 if ascending else None,
        ),
    )
    expected = [ir_id] if filtered else ([ir_id, fmp_id] if ascending else [fmp_id, ir_id])
    assert [item.document_id for item in result.items] == expected


def test_resume_receipt_binds_source_filter_and_accepts_old_unfiltered_scope(
    connection: sqlite3.Connection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from execution import process_document_evidence as cli
    from models.documents import SourceType

    monkeypatch.setattr(cli, "PROJECT_ROOT", tmp_path)
    _seed(connection, tmp_path)
    database = Path(connection.execute("PRAGMA database_list").fetchone()[2])
    argv = ["--db", str(database), "--repo-root", str(tmp_path), "--apply", "--resume"]
    assert cli.main([*argv, "--source-type", "ir_doc"]) == 2
    receipt_path = tmp_path / ".tmp" / "operations" / "runtime" / "document-evidence.latest.json"
    receipt = cli.DocumentEvidenceResumeReceipt.model_validate_json(receipt_path.read_text())
    assert receipt.source_type is SourceType.IR_DOC
    with pytest.raises(ValueError, match="scope mismatch"):
        cli.main(argv)
    with pytest.raises(ValueError, match="scope mismatch"):
        cli.main([*argv, "--source-type", "fmp"])
    # Receipts written before this optional field retain unfiltered semantics.
    old_receipt = receipt.model_dump(mode="json", exclude={"source_type"})
    receipt_path.write_text(json.dumps(old_receipt))
    assert cli.main(argv) == 2
    capsys.readouterr()
