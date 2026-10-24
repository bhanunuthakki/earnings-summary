"""Calendar-year presentation intake uses real managed publication on both routes."""

from __future__ import annotations

import hashlib
import io
import json
import sqlite3
import subprocess
import sys
from collections.abc import Callable
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import cast

import pytest

from execution import categorize_ir_uploads as categorizer
from execution import discover_ir_documents_all as discovery
from execution import fetch_ir_documents as fetch
from execution import fetch_ir_from_urls as manual
from ir_pipeline.manifest import ManifestEntry, merge_write
from ir_uploads import BoundIRClassification
from models.documents import DocType
from models.ir_uploads import CategorizationFailure, CategorizationResult, Confidence
from pipeline import managed_ir_sources as managed
from pipeline.source_policy import ArtifactKind, CollectionSource, StoredCollectionAuthorization
from provenance.document_evidence_pipeline import DocumentEvidenceItem, DocumentEvidenceResult


def _no_sleep(_seconds: float) -> None:
    return None


def _no_registry(_root: Path) -> list[tuple[str, str, tuple[str, ...]]]:
    return []


def _forbid_narrative(_ticker: str, _root: Path) -> None:
    pytest.fail("anchor refresh must not follow rejected input")


def _classification(
    *, doc_type: DocType = DocType.IR_PRESENTATION, period: date = date(2026, 6, 30)
) -> CategorizationResult:
    return CategorizationResult(
        ticker="MELI",
        doc_type=doc_type,
        period_end=period,
        period_label="Q2 2026",
        confidence=Confidence.HIGH,
        ticker_evidence=["issuer"],
        doc_type_evidence=["slides"],
        period_evidence=["Q2"],
    )


@pytest.fixture
def state(
    tmp_path: Path, migrated_db: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path]:
    root = tmp_path / "state"
    (root / "data").mkdir(parents=True)
    database = migrated_db(root / "data" / "synthetic.sqlite")
    monkeypatch.setenv("EARNINGS_SUMMARY_DB_PATH", str(database))
    with sqlite3.connect(database) as conn:
        conn.execute(
            "INSERT INTO tracked_companies (ticker,name,list_type,fiscal_year_end,instrument_type) "
            "VALUES ('MELI','MercadoLibre','portfolio','12-31','equity')"
        )
    monkeypatch.setattr(fetch.time, "sleep", _no_sleep)
    monkeypatch.setattr("ir_uploads._RUNTIME_REGISTRY", None)
    return root, database


@pytest.fixture
def transport(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []

    def download(url: str) -> tuple[bytes, str, str]:
        calls.append(url)
        return f"Synthetic issuer PDF at {url}".encode(), "", "application/pdf"

    monkeypatch.setattr(fetch, "_fetch_bytes", download)
    return calls


@pytest.fixture
def classifications(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str | None, str | None]]:
    calls: list[tuple[str | None, str | None]] = []

    def classify(
        path: Path,
        *,
        ticker_hint: str | None = None,
        calendar_override: str | None = None,
        source_url: str | None = None,
    ) -> CategorizationResult:
        assert path.read_bytes()
        assert ticker_hint == "MELI"
        calls.append((calendar_override, source_url))
        return _classification()

    monkeypatch.setattr(managed, "classify_ir_file", classify)
    monkeypatch.setattr(categorizer, "classify_ir_file", classify)
    return calls


def _manifest(root: Path, urls: list[str]) -> None:
    merge_write(
        root,
        "MELI",
        [
            ManifestEntry(ticker="MELI", doc_type="document", url=url, year=2026, quarter="Q2")
            for url in urls
        ],
    )


def _evidence_child(argv: list[str], database: Path) -> subprocess.CompletedProcess[str]:
    """Simulate the deterministic child transport using actual stored document IDs."""
    with sqlite3.connect(database) as conn:
        documents = conn.execute(
            "SELECT id,ticker FROM documents WHERE ticker='MELI' AND source_type='ir_doc' ORDER BY id"
        ).fetchall()
    assert documents
    result = DocumentEvidenceResult(
        mode="apply",
        items=[
            DocumentEvidenceItem(
                document_id=document_id,
                ticker=ticker,
                status="already-covered",
                inventory_state="linked",
                coverage_statuses=("extracted",),
            )
            for document_id, ticker in documents
        ],
        status_counts={"already-covered": len(documents)},
    )
    return subprocess.CompletedProcess(argv, 0, result.model_dump_json(), "")


def _auto(root: Path, database: Path) -> dict[str, object]:
    return fetch.process_ticker(
        "MELI", root=root, db_path=database, categorize=True, calendar="calendar"
    )


@pytest.mark.parametrize("existing", [False, True])
def test_transport_skip_keeps_registered_work_and_explicit_zero_ready_truth(
    state: tuple[Path, Path],
    transport: list[str],
    classifications: list[tuple[str | None, str | None]],
    monkeypatch: pytest.MonkeyPatch,
    existing: bool,
) -> None:
    root, database = state
    good, dead = "https://issuer.test/good.pdf", "https://issuer.test/dead.pdf"
    if existing:
        _manifest(root, [good])
        assert _auto(root, database)["ready"] == 1
    _manifest(root, [dead])

    def absent(url: str) -> None:
        transport.append(url)
        return

    monkeypatch.setattr(fetch, "_fetch_bytes", absent)
    before = list(classifications)
    result = _auto(root, database)
    assert (result["status"], result["failed"], result["transport_skipped"]) == (
        "partial" if existing else "failed",
        1,
        1,
    )
    assert result["downloaded"] == 0
    assert result["ready_existing"] == int(existing)
    assert (
        fetch.main(
            ["--ticker", "MELI", "--categorize", "--repo-root", str(root), "--db", str(database)]
        )
        == 1
    )
    assert (
        manual.register_urls(
            "MELI", [dead], repo_root=root, db_path=database, year=2026, quarter=2, process=False
        )
        == 0
    )
    assert classifications == before
    with sqlite3.connect(database) as conn:
        assert conn.execute("SELECT last_status FROM ir_fetch_status").fetchone()[0] == "failed"
        assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == int(existing)


def test_pending_registered_processing_runs_after_new_publication_failure(
    state: tuple[Path, Path],
    transport: list[str],
    classifications: list[tuple[str | None, str | None]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import index_manager

    root, database = state
    _manifest(root, ["https://issuer.test/good.pdf"])
    assert _auto(root, database)["ready"] == 1
    # Limit this fetch to the new input. Previously registered work remains pending.
    (fetch.manifest_dir(root) / "MELI_urls.json").unlink()
    _manifest(root, ["https://issuer.test/new.pdf"])

    def failed_projection(*_args: object, **_kwargs: object) -> bool:
        raise OSError("synthetic new projection failure")

    monkeypatch.setattr(index_manager, "register_ir_document", failed_projection)
    anchors: list[str] = []

    def child(argv: list[str], _timeout: float) -> subprocess.CompletedProcess[str]:
        if any(Path(arg).name == "fetch_ir_documents.py" for arg in argv):
            return _fetch_cli_child(argv)
        if any(Path(arg).name == "process_document_evidence.py" for arg in argv):
            return _evidence_child(argv, database)
        assert any(Path(arg).name == "ir_narrative.py" for arg in argv)
        anchors.append("MELI")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(discovery, "_run_child", child)

    def pending(_root: Path, _ticker: str) -> bool:
        return True

    monkeypatch.setattr(discovery, "has_pending_narrative", pending)
    result = cast(Callable[..., discovery.TickerResult], getattr(discovery, "_finish_discovery"))(
        discovery.DiscoveryResult("MELI", discovery.TickerStatus.OK, 1, 0),
        repo_root=root,
        db_path=database,
        skip_download=False,
        process=True,
        summaries=False,
        download_timeout=30,
        process_timeout=30,
        ticker_deadline=60,
        whole_deadline_at=None,
        owner_requested=False,
    )
    assert result.status is discovery.TickerStatus.FAILED
    assert result.processed is True
    assert anchors == ["MELI"]
    with sqlite3.connect(database) as conn:
        assert conn.execute("SELECT brief_dirty FROM tracked_companies").fetchone()[0] == 1


def test_canonical_legacy_reindex_reads_the_explicit_index(
    state: tuple[Path, Path],
    transport: list[str],
    classifications: list[tuple[str | None, str | None]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import index_manager

    root, database = state
    _manifest(root, ["https://issuer.test/good.pdf"])
    assert _auto(root, database)["ready"] == 1
    index_path = root / ".tmp" / "document_index.json"
    assert index_manager.mark_document_processed(
        "MELI", 2026, "Q2", "presentation", document_index_path=index_path
    )
    original = index_path.read_bytes()
    decoy = root / ".tmp" / "decoy-index.json"
    decoy.write_text("{}")
    monkeypatch.setattr(index_manager, "DOCUMENT_INDEX_PATH", str(decoy))
    canonical = next((root / "ir_documents" / "MELI" / "2026-06-30").glob("*.pdf"))
    with sqlite3.connect(database) as conn:
        conn.row_factory = sqlite3.Row
        record = categorizer.process_ir_document(
            canonical,
            conn,
            False,
            "MELI",
            ir_dir=root / "ir_documents",
            rel_root=root,
            document_index_path=index_path,
        )
    assert record["status"] == "reindexed"
    assert index_path.read_bytes() == original
    assert decoy.read_text() == "{}"


def test_explicit_local_upload_cli_uses_state_paths_without_fetch_sweep(
    state: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import index_manager
    import issuer_registry

    root, database = state
    incoming = root / "ir_documents" / "MELI" / "local.pdf"
    incoming.parent.mkdir(parents=True)
    incoming.write_bytes(b"Synthetic local upload")
    decoy = root / ".tmp" / "decoy-index.json"
    decoy.parent.mkdir(parents=True)
    decoy.write_text("{}")
    monkeypatch.setattr(index_manager, "DOCUMENT_INDEX_PATH", str(decoy))
    roots: list[Path] = []

    def registry(state_root: Path) -> list[tuple[str, str, tuple[str, ...]]]:
        roots.append(state_root)
        return []

    def classify(*_args: object, **_kwargs: object) -> CategorizationResult:
        return _classification(doc_type=DocType.IR_PRESS_RELEASE)

    monkeypatch.setattr(issuer_registry, "effective_entries", registry)
    monkeypatch.setattr(categorizer, "classify_ir_file", classify)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "categorize_ir_uploads.py",
            "--ticker",
            "MELI",
            "--source-dir",
            str(root / "ir_documents"),
            "--rel-root",
            str(root),
            "--db-path",
            str(database),
            "--manifest-dir",
            str(root / ".tmp" / "categorization"),
        ],
    )
    assert categorizer.main() == 0
    assert roots == [root]
    assert (root / ".tmp" / "document_index.json").exists()
    assert decoy.read_text() == "{}"
    with sqlite3.connect(database) as conn:
        assert (
            conn.execute("SELECT source_url FROM documents").fetchone()[0]
            == "manual_upload:local.pdf"
        )


def test_undetermined_family_uses_existing_legacy_rejection(
    state: tuple[Path, Path],
    transport: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, database = state
    url = "https://issuer.test/unknown.pdf"
    _manifest(root, [url])
    paths: list[Path] = []
    calls: list[str] = []
    process = cast(Callable[..., dict[str, object]], categorizer.process_ir_document)
    failure = CategorizationFailure(
        reason="synthetic undetermined period",
        text_sample="synthetic",
        doc_type_guess=DocType.IR_PRESENTATION,
    )

    def unknown(*_args: object, **_kwargs: object) -> CategorizationFailure:
        calls.append("classified")
        return failure

    def legacy(path: Path, *args: object, **kwargs: object) -> dict[str, object]:
        paths.append(path)
        assert url.encode() in path.read_bytes()
        return process(path, *args, **kwargs)

    def no_second_parse(*_args: object, **_kwargs: object) -> CategorizationFailure:
        pytest.fail("legacy handoff parsed exact classified bytes twice")

    monkeypatch.setattr(managed, "classify_ir_file", unknown)
    monkeypatch.setattr(categorizer, "classify_ir_file", no_second_parse)
    monkeypatch.setattr(categorizer, "process_ir_document", legacy)
    summary = _auto(root, database)
    assert summary["failed"] == 1
    assert calls == ["classified"]
    assert paths and paths[0].is_relative_to(root / "ir_documents")
    assert summary["legacy_outcomes"]
    with sqlite3.connect(database) as conn:
        assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0
    assert transport == [url]


@pytest.mark.parametrize("damage", ["bytes", "size", "ticker", "calendar", "url"])
def test_legacy_classification_handoff_rejects_byte_or_context_change(
    state: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    damage: str,
) -> None:
    root, database = state
    incoming = root / "ir_documents" / "MELI" / "incoming.pdf"
    incoming.parent.mkdir(parents=True)
    payload = b"Synthetic bound legacy input"
    incoming.write_bytes(payload)
    url = "https://issuer.test/input.pdf"
    bound = BoundIRClassification(
        _classification(doc_type=DocType.IR_PRESS_RELEASE),
        hashlib.sha256(payload).hexdigest(),
        len(payload),
        "MELI",
        "calendar",
        url,
    )
    if damage == "bytes":
        incoming.write_bytes(b"Different legacy input")
    elif damage == "size":
        bound = replace(bound, byte_size=len(payload) + 1)
    elif damage == "ticker":
        bound = replace(bound, ticker_hint="OTHER")
    elif damage == "calendar":
        bound = replace(bound, calendar_override="jan_31")
    else:
        bound = replace(bound, source_url="https://issuer.test/other.pdf")

    def forbid_parse(*_args: object, **_kwargs: object) -> CategorizationResult:
        pytest.fail("invalid bound classification entered the parser")

    monkeypatch.setattr(categorizer, "classify_ir_file", forbid_parse)
    with sqlite3.connect(database) as conn:
        conn.row_factory = sqlite3.Row
        with pytest.raises(ValueError, match="classification bytes or context changed"):
            categorizer.process_ir_document(
                incoming,
                conn,
                False,
                "MELI",
                ir_dir=root / "ir_documents",
                rel_root=root,
                url_overrides={incoming.name: url},
                calendar_override="calendar",
                classification=bound,
                document_index_path=root / ".tmp" / "document_index.json",
            )
        assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0
    assert incoming.exists()


def test_coherent_cross_checkout_skip_is_not_reverification_and_strict_repair_holds(
    state: tuple[Path, Path],
    transport: list[str],
    classifications: list[tuple[str | None, str | None]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, database = state
    url = "https://issuer.test/q2.pdf"
    _manifest(root, [url])
    assert _auto(root, database)["ready"] == 1
    index_path = root / ".tmp" / "document_index.json"
    original = index_path.read_bytes()
    other_checkout = root / "other-checkout"
    other_checkout.mkdir()

    def other_code_root() -> Path:
        return other_checkout

    monkeypatch.setattr(managed, "_code_root", other_code_root)
    replay = cast(Callable[..., object], getattr(managed, "_replay_result"))
    replays: list[str] = []

    def record_replay(*args: object, **kwargs: object) -> object:
        replays.append("full")
        return replay(*args, **kwargs)

    monkeypatch.setattr(managed, "_replay_result", record_replay)
    before = list(classifications)
    summary = _auto(root, database)
    assert (
        summary["failed"],
        summary["ready_existing"],
        summary["registration_not_reverified"],
    ) == (0, 1, 1)
    assert replays == []
    assert index_path.read_bytes() == original
    index_path.unlink()
    repair = _auto(root, database)
    assert repair["projection_failed"] == 1
    assert repair["ready_existing"] == 0
    assert not index_path.exists()
    assert replays == ["full"]
    assert transport == [url]
    assert classifications == before


@pytest.mark.parametrize("phase", ["inventory_receipt.json", "publication_result.json"])
@pytest.mark.parametrize("route", ["automatic", "manual"])
def test_committed_finalization_failure_resumes_without_collection_or_classification(
    state: tuple[Path, Path],
    transport: list[str],
    classifications: list[tuple[str | None, str | None]],
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
    route: str,
) -> None:
    root, database = state
    url = "https://issuer.test/q2.pdf"
    _manifest(root, [url])
    publish = cast(Callable[[Path, str], None], getattr(managed, "_publish_managed_text"))

    def fail_finalize(path: Path, text: str) -> None:
        if path.name == phase:
            raise OSError("synthetic finalize failure")
        publish(path, text)

    monkeypatch.setattr(managed, "_publish_managed_text", fail_finalize)
    first = _auto(root, database)
    assert (first["registered"], first["ready"], first["failed"]) == (1, 0, 1)
    with sqlite3.connect(database) as conn:
        assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 1
    before = list(classifications)
    monkeypatch.setattr(managed, "_publish_managed_text", publish)

    def forbid_admission(*_args: object) -> None:
        pytest.fail("committed finalization entered current admission")

    def changed_identity() -> str:
        return "0" * 64

    monkeypatch.setattr(managed, "_policy", forbid_admission)
    monkeypatch.setattr(managed, "classifier_code_identity", changed_identity)
    anchors: list[str] = []

    def anchor(ticker: str, _root: Path) -> bool:
        anchors.append(ticker)
        return True

    monkeypatch.setattr(manual, "_run_narrative", anchor)
    if route == "automatic":
        retry = _auto(root, database)
        assert (retry["failed"], retry["ready_existing"]) == (0, 1)
    else:
        assert (
            manual.register_urls(
                "MELI", [url], repo_root=root, db_path=database, year=2026, quarter=2
            )
            == 1
        )
        assert anchors == ["MELI"]
    assert list((root / "data" / "managed_ir_publications").glob("*/publication_result.json"))
    assert (root / ".tmp" / "document_index.json").exists()
    assert transport == [url]
    assert classifications == before


@pytest.mark.parametrize("route", ["automatic", "manual"])
def test_later_reused_publication_finalizes_despite_older_coherent_attempt(
    state: tuple[Path, Path],
    transport: list[str],
    classifications: list[tuple[str | None, str | None]],
    monkeypatch: pytest.MonkeyPatch,
    route: str,
) -> None:
    root, database = state
    url = "https://issuer.test/reused.pdf"
    _manifest(root, [url])
    assert _auto(root, database)["ready"] == 1
    original_result = next(
        (root / "data" / "managed_ir_publications").glob("*/publication_result.json")
    )
    original = managed.PreparedIssuerDocumentPublication.model_validate_json(
        original_result.read_bytes()
    )
    receipt = original.staging_receipt
    request = receipt.request.model_copy(update={"attempt_id": "second-reused-attempt"})
    attempt = root / ".tmp" / "managed_ir_staging" / request.attempt_id
    objects = attempt / "objects"
    objects.mkdir(parents=True)
    for item in receipt.documents:
        (attempt / item.object_path).write_bytes(
            (
                root / ".tmp" / "managed_ir_staging" / receipt.request.attempt_id / item.object_path
            ).read_bytes()
        )
    seal = cast(
        Callable[..., managed.IssuerDocumentStagingReceipt],
        getattr(managed, "_seal_prepared_staging"),
    )
    seal(request, list(receipt.documents), root, database, objects)
    publish = cast(Callable[[Path, str], None], getattr(managed, "_publish_managed_text"))

    def fail_later_result(path: Path, text: str) -> None:
        if path.name == "publication_result.json" and path.parent.name == request.attempt_id:
            raise OSError("synthetic later finalize failure")
        publish(path, text)

    monkeypatch.setattr(managed, "_publish_managed_text", fail_later_result)
    with pytest.raises(managed.PreparedIssuerDocumentPublisherError) as failed:
        managed.publish_prepared_issuer_documents(request, state_root=root, db_path=database)
    assert failed.value.committed
    later_result = original_result.parent.parent / request.attempt_id / "publication_result.json"
    assert not later_result.exists()
    with sqlite3.connect(database) as conn:
        assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM managed_ir_publications").fetchone()[0] == 2
    before = list(classifications)
    monkeypatch.setattr(managed, "_publish_managed_text", publish)
    if route == "automatic":
        result = _auto(root, database)
        assert (result["failed"], result["ready_existing"]) == (0, 1)
    else:
        assert (
            manual.register_urls(
                "MELI", [url], repo_root=root, db_path=database, year=2026, quarter=2, process=False
            )
            == 1
        )
    assert later_result.exists()
    resumed = managed.PreparedIssuerDocumentPublication.model_validate_json(
        later_result.read_bytes()
    )
    assert resumed.inserted_document_ids == ()
    assert resumed.reused_document_ids == original.inserted_document_ids
    assert transport == [url]
    assert classifications == before

    # Once every attempt is complete, repeated manifest entries inspect metadata
    # once per URL and never enter strict replay or read the PDF bytes.
    inspected: list[str] = []
    recorded = cast(
        Callable[..., managed.PreparedIssuerDocumentPublication],
        getattr(managed, "_recorded_publication"),
    )

    def record_inspection(
        root: Path, attempt: str, database: Path
    ) -> managed.PreparedIssuerDocumentPublication:
        inspected.append(attempt)
        return recorded(root, attempt, database)

    def forbid_full_replay(*_args: object, **_kwargs: object) -> None:
        pytest.fail("completed registration entered strict replay or PDF hashing")

    monkeypatch.setattr(managed, "_recorded_publication", record_inspection)
    monkeypatch.setattr(
        managed, "_replay_registered_issuer_document_publications", forbid_full_replay
    )
    monkeypatch.setattr(managed, "_file_sha", forbid_full_replay)
    manifest = fetch.manifest_dir(root) / "MELI_urls.json"
    entries = json.loads(manifest.read_text())
    manifest.write_text(json.dumps([*entries, *entries]))
    completed = _auto(root, database)
    assert completed["registration_not_reverified"] == 2
    assert set(inspected) == {receipt.request.attempt_id, request.attempt_id}
    assert len(inspected) == 2
    assert transport == [url]
    assert classifications == before


def test_coherent_registration_survives_identity_and_historical_policy_drift(
    state: tuple[Path, Path],
    transport: list[str],
    classifications: list[tuple[str | None, str | None]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from runtime.job_runtime import JobLock

    root, database = state
    urls = [f"https://issuer.test/q2-{i}.pdf" for i in range(3)]
    _manifest(root, urls)
    assert _auto(root, database)["ready"] == 3
    index_path = root / ".tmp" / "document_index.json"
    original = index_path.read_bytes()
    before = list(classifications)
    inspected: list[str] = []
    pdf_reads: list[Path] = []
    read_artifact = cast(Callable[..., object], managed.read_stable_artifact)

    def metadata_only(path: Path, *args: object, **kwargs: object) -> object:
        if path.suffix.lower() == ".pdf":
            pdf_reads.append(path)
        return read_artifact(path, *args, **kwargs)

    monkeypatch.setattr(managed, "read_stable_artifact", metadata_only)
    replay = cast(
        Callable[..., managed.PreparedIssuerDocumentPublication | None],
        getattr(managed, "_replay_result"),
    )

    def read_history(
        receipt: managed.IssuerDocumentStagingReceipt, *args: object
    ) -> managed.PreparedIssuerDocumentPublication | None:
        inspected.append(receipt.request.attempt_id)
        return replay(receipt, *args)

    def forbid_writer(_lock: JobLock) -> JobLock:
        pytest.fail("coherent historical inspection acquired a writer lock")

    def changed_identity() -> str:
        pytest.fail("recorded-registration no-op compared current code identity")

    def forbid_policy(*_args: object) -> None:
        pytest.fail("historical completion entered current admission")

    monkeypatch.setattr(managed, "classifier_code_identity", changed_identity)
    monkeypatch.setattr(managed, "verifier_code_identity", changed_identity)
    monkeypatch.setattr(managed, "_policy", forbid_policy)
    monkeypatch.setattr(managed, "_replay_result", read_history)
    monkeypatch.setattr(JobLock, "__enter__", forbid_writer)
    retry = _auto(root, database)
    assert (retry["failed"], retry["ready_existing"]) == (0, 3)
    assert retry["registration_not_reverified"] == 3
    assert inspected == []
    assert (
        manual.register_urls(
            "MELI", urls, repo_root=root, db_path=database, year=2026, quarter=2, process=False
        )
        == 3
    )
    assert (
        fetch.main(
            ["--ticker", "MELI", "--categorize", "--repo-root", str(root), "--db", str(database)]
        )
        == 0
    )

    def fetch_child(argv: list[str], _timeout: float) -> subprocess.CompletedProcess[str]:
        return _fetch_cli_child(argv)

    monkeypatch.setattr(discovery, "_run_child", fetch_child)
    result = cast(Callable[..., discovery.TickerResult], getattr(discovery, "_finish_discovery"))(
        discovery.DiscoveryResult("MELI", discovery.TickerStatus.OK, len(urls), 0),
        repo_root=root,
        db_path=database,
        skip_download=False,
        process=False,
        summaries=False,
        download_timeout=30,
        process_timeout=30,
        ticker_deadline=60,
        whole_deadline_at=None,
        owner_requested=False,
    )
    assert result.status is discovery.TickerStatus.OK
    assert inspected == []
    assert pdf_reads == []
    assert index_path.read_bytes() == original
    assert transport == urls
    assert classifications == before


@pytest.mark.parametrize("source", ["ir", "manual", "user_intake"])
def test_replay_preserves_well_formed_foreign_alias(
    state: tuple[Path, Path],
    transport: list[str],
    classifications: list[tuple[str | None, str | None]],
    monkeypatch: pytest.MonkeyPatch,
    source: str,
) -> None:
    import index_manager

    root, database = state
    url = "https://issuer.test/q2.pdf"
    _manifest(root, [url])
    assert _auto(root, database)["ready"] == 1
    index_path = root / ".tmp" / "document_index.json"
    monkeypatch.setattr(index_manager, "DOCUMENT_INDEX_PATH", str(index_path))
    if source == "ir":
        index_manager.register_ir_document(
            "MELI",
            2026,
            "Q2",
            "presentation",
            "https://other.test/owner-selected.pdf",
            local_path="/owner/selected.pdf",
        )
    elif source == "manual":
        index_manager.register_manual_document(
            "MELI", 2026, "Q2", "presentation", "/owner/selected.pdf"
        )
    else:
        index_manager.register_user_intake_document(
            "MELI", 2026, "Q2", "presentation", "/owner/selected.pdf", "a" * 64, 1.0
        )
    assert index_manager.mark_document_processed("MELI", 2026, "Q2", "presentation")
    original = index_path.read_bytes()
    before = list(classifications)
    retry = _auto(root, database)
    assert retry["failed"] == 0
    assert index_path.read_bytes() == original
    assert retry["alias_held_by_other_document"] == 1
    assert classifications == before
    assert transport == [url]


@pytest.mark.parametrize("failure", ["file", "database"])
def test_legacy_file_failure_reports_partial_after_prior_managed_commit(
    state: tuple[Path, Path],
    transport: list[str],
    classifications: list[tuple[str | None, str | None]],
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    root, database = state
    urls = [
        "https://issuer.test/managed.pdf",
        "https://issuer.test/bad.pdf",
        "https://issuer.test/later.pdf",
    ]
    _manifest(root, urls)

    def classify(path: Path, **_kwargs: object) -> CategorizationResult:
        return _classification(
            doc_type=DocType.IR_PRESENTATION
            if b"managed.pdf" in path.read_bytes()
            else DocType.IR_PRESS_RELEASE
        )

    monkeypatch.setattr(managed, "classify_ir_file", classify)
    monkeypatch.setattr(categorizer, "classify_ir_file", classify)
    process = cast(Callable[..., dict[str, object]], categorizer.process_ir_document)
    calls: list[str] = []

    def fail_file(path: Path, *args: object, **kwargs: object) -> dict[str, object]:
        calls.append(path.name)
        if b"bad.pdf" in path.read_bytes():
            assert isinstance(args[0], sqlite3.Connection)
            args[0].execute("UPDATE tracked_companies SET brief_dirty=1 WHERE ticker='MELI'")
            if failure == "database":
                raise sqlite3.OperationalError("synthetic legacy database failure")
            raise ValueError("synthetic legacy file failure")
        return process(path, *args, **kwargs)

    monkeypatch.setattr(categorizer, "process_ir_document", fail_file)
    summary = _auto(root, database)
    assert summary["status"] == "partial"
    assert summary["failed"] in {1, 2}
    assert summary["managed_registered"] == 1
    assert summary["registered"] == (1 if failure == "database" else 2)
    assert len(calls) == (1 if failure == "database" else 2)
    with sqlite3.connect(database) as conn:
        assert conn.execute("SELECT COUNT(*) FROM managed_ir_publications").fetchone()[0] == 1
        assert conn.execute("SELECT brief_dirty FROM tracked_companies").fetchone()[0] == 0
    assert transport == urls


def test_legacy_classifier_uses_state_registry_and_restores_it_after_failure(
    state: tuple[Path, Path],
    transport: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ir_uploads
    import issuer_registry

    root, database = state
    url = "https://issuer.test/legacy.pdf"
    _manifest(root, [url])
    prior = [("PRIOR", "calendar", ("Prior issuer",))]
    local = [("MELI", "calendar", ("State issuer",))]
    roots: list[Path] = []
    get_registry = cast(
        Callable[[], list[tuple[str, str, tuple[str, ...]]]],
        getattr(ir_uploads, "_effective_registry"),
    )

    def registry(state_root: Path) -> list[tuple[str, str, tuple[str, ...]]]:
        roots.append(state_root)
        return local

    def classify(*_args: object, **_kwargs: object) -> CategorizationResult:
        return _classification(doc_type=DocType.IR_PRESS_RELEASE)

    def fail(*_args: object, **_kwargs: object) -> dict[str, object]:
        assert get_registry() == local
        raise ValueError("synthetic legacy classification failure")

    monkeypatch.setattr(managed, "classify_ir_file", classify)
    monkeypatch.setattr(categorizer, "process_ir_document", fail)
    monkeypatch.setattr(issuer_registry, "effective_entries", registry)
    ir_uploads.set_runtime_registry(prior)
    try:
        summary = _auto(root, database)
        assert summary["failed"] == 1
        assert get_registry() == prior
    finally:
        ir_uploads.set_runtime_registry(None)
    assert roots == [root]
    assert transport == [url]


def test_postcommit_projection_lock_contention_reports_and_recovers(
    state: tuple[Path, Path],
    transport: list[str],
    classifications: list[tuple[str | None, str | None]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from runtime.job_runtime import JobAlreadyRunningError, JobLock

    root, database = state
    url = "https://issuer.test/q2.pdf"
    _manifest(root, [url])
    enter = JobLock.__enter__

    def busy(lock: JobLock) -> JobLock:
        if getattr(lock, "_job_name") == "managed-ir-index":
            raise JobAlreadyRunningError("synthetic busy projection")
        return enter(lock)

    monkeypatch.setattr(JobLock, "__enter__", busy)
    first = _auto(root, database)
    assert (first["registered"], first["ready"], first["projection_failed"]) == (1, 0, 1)
    before = list(classifications)
    monkeypatch.setattr(JobLock, "__enter__", enter)
    assert _auto(root, database)["projection_repaired"] == 1
    assert transport == [url]
    assert classifications == before


def test_invalid_selected_period_returns_failure_before_collection(
    state: tuple[Path, Path],
    transport: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, database = state
    _manifest(root, ["https://issuer.test/q2.pdf"])

    def invalid_period(_entry: dict[str, object]) -> None:
        return None

    def selected(
        entries: list[dict[str, object]], *, max_quarters: int
    ) -> tuple[list[dict[str, object]], int]:
        assert max_quarters > 0
        return entries, 0

    monkeypatch.setattr(fetch, "_reported_quarter", invalid_period)
    monkeypatch.setattr(fetch, "_bounded_manifest_entries", selected)
    summary = _auto(root, database)
    assert (summary["status"], summary["failed"], summary["downloaded"]) == ("failed", 1, 0)
    assert transport == []


@pytest.mark.parametrize("route", ["automatic", "manual"])
def test_actual_routes_publish_once_and_replay_sealed_invocation(
    state: tuple[Path, Path],
    transport: list[str],
    classifications: list[tuple[str | None, str | None]],
    monkeypatch: pytest.MonkeyPatch,
    route: str,
) -> None:
    root, database = state
    url = "https://issuer.test/q2.pdf"
    anchors: list[str] = []

    def anchor(ticker: str, _root: Path) -> bool:
        anchors.append(ticker)
        return True

    monkeypatch.setattr(manual, "_run_narrative", anchor)

    def no_legacy(*_args: object, **_kwargs: object) -> tuple[int, int]:
        pytest.fail("managed presentation entered the legacy categorizer")

    monkeypatch.setattr(fetch, "_run_categorize", no_legacy)
    requested_values: list[bool] = []
    authorize = managed.authorize_stored_collection_target

    def record_policy(
        db_path: Path,
        ticker: str,
        *,
        requested: bool,
        source: CollectionSource,
        artifact_kind: ArtifactKind,
        require_corporate_instrument: bool = False,
    ) -> StoredCollectionAuthorization:
        requested_values.append(requested)
        return authorize(
            db_path,
            ticker,
            requested=requested,
            source=source,
            artifact_kind=artifact_kind,
            require_corporate_instrument=require_corporate_instrument,
        )

    monkeypatch.setattr(managed, "authorize_stored_collection_target", record_policy)
    if route == "manual":
        assert (
            manual.register_urls(
                "MELI", [url], repo_root=root, db_path=database, year=2026, quarter=2
            )
            == 1
        )
        assert anchors == ["MELI"]
    else:
        _manifest(root, [url])
        summary = _auto(root, database)
        assert (summary["registered"], summary["ready"], summary["managed_registered"]) == (1, 1, 1)
    assert transport == [url]
    assert all(calendar == "calendar" and source == url for calendar, source in classifications)
    assert len(classifications) >= 3  # triage, prepared validation and publisher validation
    assert not fetch.incoming_urls_path(root).exists()
    with sqlite3.connect(database) as conn:
        row = conn.execute("SELECT source_url,doc_type,file_path FROM documents").fetchone()
        assert row[:2] == (url, "ir_presentation")
        assert (root / row[2]).exists()
        assert conn.execute("SELECT COUNT(*) FROM evidence_document_versions").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM managed_ir_publications").fetchone()[0] == 1
    result_path = next(
        (root / "data" / "managed_ir_publications").glob("*/publication_result.json")
    )
    publication = managed.PreparedIssuerDocumentPublication.model_validate_json(
        result_path.read_bytes()
    )
    request = publication.staging_receipt.request
    assert request.owner_requested is (route == "manual")
    assert request.calendar_override == "calendar"
    assert requested_values and all(value is (route == "manual") for value in requested_values)
    assert (
        managed.publish_prepared_issuer_documents(request, state_root=root, db_path=database)
        == publication
    )
    assert requested_values[-1] is (route == "manual")
    index_path = root / ".tmp" / "document_index.json"
    index = json.loads(index_path.read_text())
    index["MELI_2026_Q2_presentation"]["processed"] = True
    index_path.write_text(json.dumps(index))
    classified_before = list(classifications)
    summary = _auto(root, database)
    assert (
        summary["downloaded"],
        summary["registered"],
        summary["skipped"],
        summary["projection_repaired"],
    ) == (0, 0, 1, 0)
    assert transport == [url]
    assert classifications == classified_before
    assert json.loads(index_path.read_text())["MELI_2026_Q2_presentation"]["processed"] is True


@pytest.mark.parametrize("process", [False, True])
def test_manual_side_effects_require_completed_registration(
    state: tuple[Path, Path],
    transport: list[str],
    classifications: list[tuple[str | None, str | None]],
    monkeypatch: pytest.MonkeyPatch,
    process: bool,
) -> None:
    root, database = state
    anchors: list[str] = []

    def anchor(ticker: str, _root: Path) -> bool:
        anchors.append(ticker)
        return True

    monkeypatch.setattr(manual, "_run_narrative", anchor)
    assert (
        manual.register_urls(
            "MELI",
            ["https://issuer.test/q2.pdf"],
            repo_root=root,
            db_path=database,
            year=2026,
            quarter=2,
            process=process,
        )
        == 1
    )
    assert anchors == (["MELI"] if process else [])
    with sqlite3.connect(database) as conn:
        assert conn.execute("SELECT brief_dirty FROM tracked_companies").fetchone()[0] == int(
            process
        )
        assert conn.execute("SELECT last_status,downloaded FROM ir_fetch_status").fetchone() == (
            "ok",
            1,
        )


@pytest.mark.parametrize("failure", ["classification", "bytes"])
def test_rejected_download_is_not_registration_success_and_retains_repair_input(
    state: tuple[Path, Path],
    transport: list[str],
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    root, database = state

    def classify(path: Path, **_kwargs: object) -> CategorizationResult | CategorizationFailure:
        if failure == "bytes":
            path.chmod(0o600)
            path.write_bytes(b"changed during classification")
            return _classification()
        return _classification().model_copy(update={"confidence": Confidence.LOW})

    monkeypatch.setattr(managed, "classify_ir_file", classify)
    monkeypatch.setattr(manual, "_run_narrative", _forbid_narrative)
    assert (
        manual.register_urls(
            "MELI",
            ["https://issuer.test/rejected.pdf"],
            repo_root=root,
            db_path=database,
            year=2026,
            quarter=2,
        )
        == 0
    )
    with sqlite3.connect(database) as conn:
        assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0
        assert conn.execute("SELECT brief_dirty FROM tracked_companies").fetchone()[0] == 0
        assert conn.execute("SELECT last_status,downloaded FROM ir_fetch_status").fetchone() == (
            "failed",
            1,
        )
    assert len(list((root / ".tmp" / "managed_ir_staging").glob("*/objects/*.pdf"))) == 1
    assert not fetch.incoming_urls_path(root).exists()


def test_partial_candidate_failure_preserves_committed_first_candidate(
    state: tuple[Path, Path],
    transport: list[str],
    classifications: list[tuple[str | None, str | None]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, database = state
    good = "https://issuer.test/good.pdf"
    bad = "https://issuer.test/bad.pdf"
    classify = managed.classify_ir_file

    def sometimes(
        path: Path,
        *,
        ticker_hint: str | None = None,
        calendar_override: str | None = None,
        source_url: str | None = None,
    ) -> CategorizationResult | CategorizationFailure:
        if source_url == bad:
            return CategorizationFailure(reason="unresolved", text_sample="synthetic")
        return classify(
            path,
            ticker_hint=ticker_hint,
            calendar_override=calendar_override,
            source_url=source_url,
        )

    monkeypatch.setattr(managed, "classify_ir_file", sometimes)
    _manifest(root, [good, bad])
    summary = _auto(root, database)
    assert (summary["status"], summary["downloaded"], summary["registered"], summary["failed"]) == (
        "partial",
        2,
        1,
        1,
    )
    with sqlite3.connect(database) as conn:
        assert conn.execute("SELECT source_url FROM documents").fetchall() == [(good,)]
        assert conn.execute("SELECT COUNT(*) FROM managed_ir_publications").fetchone()[0] == 1


@pytest.mark.parametrize("process", [False, True])
@pytest.mark.parametrize("adapter", ["function", "cli"])
def test_projection_failure_retry_finishes_explicit_adapter_effects(
    state: tuple[Path, Path],
    transport: list[str],
    classifications: list[tuple[str | None, str | None]],
    monkeypatch: pytest.MonkeyPatch,
    process: bool,
    adapter: str,
) -> None:
    import index_manager

    root, database = state
    url = "https://issuer.test/q2.pdf"
    register = index_manager.register_ir_document
    anchors: list[str] = []

    def anchor(ticker: str, _root: Path) -> bool:
        anchors.append(ticker)
        return True

    def fail_projection(*_args: object, **_kwargs: object) -> bool:
        raise OSError("synthetic index failure")

    monkeypatch.setattr(manual, "_run_narrative", anchor)
    monkeypatch.setattr(index_manager, "register_ir_document", fail_projection)
    assert (
        manual.register_urls(
            "MELI", [url], repo_root=root, db_path=database, year=2026, quarter=2, process=process
        )
        == 0
    )
    assert not anchors
    with sqlite3.connect(database) as conn:
        assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 1
        assert conn.execute("SELECT brief_dirty FROM tracked_companies").fetchone()[0] == 0
        assert conn.execute("SELECT last_status,downloaded FROM ir_fetch_status").fetchone() == (
            "failed",
            1,
        )
    assert list((root / "data" / "managed_ir_publications").glob("*/publication_result.json"))
    before = list(classifications)
    monkeypatch.setattr(index_manager, "register_ir_document", register)
    if adapter == "function":
        assert (
            manual.register_urls(
                "MELI",
                [url],
                repo_root=root,
                db_path=database,
                year=2026,
                quarter=2,
                process=process,
            )
            == 1
        )
    else:
        assert (
            manual.main(
                [
                    "--ticker",
                    "MELI",
                    "--url",
                    url,
                    "--repo-root",
                    str(root),
                    "--db",
                    str(database),
                    "--year",
                    "2026",
                    "--quarter",
                    "2",
                    *([] if process else ["--no-process"]),
                ]
            )
            == 0
        )
    assert anchors == (["MELI"] if process else [])
    assert (root / ".tmp" / "document_index.json").exists()
    assert transport == [url]
    assert classifications == before
    with sqlite3.connect(database) as conn:
        assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 1
        assert conn.execute("SELECT brief_dirty FROM tracked_companies").fetchone()[0] == int(
            process
        )
        assert conn.execute("SELECT last_status,downloaded FROM ir_fetch_status").fetchone() == (
            "ok",
            0,
        )
    # Coherent registration replay re-runs only deterministic requested effects.
    assert (
        manual.main(
            [
                "--ticker",
                "MELI",
                "--url",
                url,
                "--repo-root",
                str(root),
                "--db",
                str(database),
                "--year",
                "2026",
                "--quarter",
                "2",
            ]
        )
        == 0
    )
    assert anchors == (["MELI", "MELI"] if process else ["MELI"])
    assert transport == [url]
    assert classifications == before


@pytest.mark.parametrize("family", ["noncalendar", "noncalendar_period", "unsupported", "event"])
def test_supported_legacy_fallback_is_limited_to_the_downloaded_file(
    state: tuple[Path, Path],
    transport: list[str],
    monkeypatch: pytest.MonkeyPatch,
    family: str,
) -> None:
    import issuer_registry

    root, database = state
    if family == "noncalendar":
        with sqlite3.connect(database) as conn:
            conn.execute("UPDATE tracked_companies SET fiscal_year_end='01-31'")
    period = (
        date(2026, 7, 31) if family in {"noncalendar", "noncalendar_period"} else date(2026, 6, 30)
    )
    doc_type = (
        DocType.IR_PRESENTATION
        if family in {"noncalendar", "noncalendar_period"}
        else DocType.IR_EVENT
        if family == "event"
        else DocType.IR_PRESS_RELEASE
    )
    seen: list[tuple[Path, str | None]] = []

    def classify(
        path: Path, *, calendar_override: str | None = None, **_kwargs: object
    ) -> CategorizationResult:
        seen.append((path, calendar_override))
        return _classification(doc_type=doc_type, period=period)

    monkeypatch.setattr(managed, "classify_ir_file", classify)
    monkeypatch.setattr(categorizer, "classify_ir_file", classify)
    monkeypatch.setattr(issuer_registry, "effective_entries", _no_registry)
    url = "https://issuer.test/fallback.pdf"
    _manifest(root, [url])
    unrelated = root / "ir_documents" / "MELI" / "unrelated.pdf"
    unrelated.parent.mkdir(parents=True)
    unrelated.write_bytes(b"Existing unrelated local upload")
    summary = fetch.process_ticker(
        "MELI",
        root=root,
        db_path=database,
        categorize=True,
        calendar="jan_31" if family == "noncalendar" else "calendar",
    )
    assert (summary["downloaded"], summary["registered"], summary["managed_registered"]) == (
        1,
        1,
        0,
    )
    assert unrelated.read_bytes() == b"Existing unrelated local upload"
    assert all(path != unrelated for path, _ in seen)
    assert all(
        calendar == ("jan_31" if family == "noncalendar" else "calendar") for _, calendar in seen
    )
    assert len(seen) == 1
    assert transport == [url]
    with sqlite3.connect(database) as conn:
        row = conn.execute("SELECT doc_type,period_end,source_url FROM documents").fetchone()
        assert row[0] == doc_type.value
        assert row[1].startswith(period.isoformat())
        assert row[2] == url
        assert conn.execute("SELECT COUNT(*) FROM managed_ir_publications").fetchone()[0] == 0
    assert (root / ".tmp" / "document_index.json").exists()


def test_denied_policy_does_not_download_or_stage(
    state: tuple[Path, Path],
    transport: list[str],
    classifications: list[tuple[str | None, str | None]],
) -> None:
    root, database = state
    with sqlite3.connect(database) as conn:
        conn.execute("UPDATE tracked_companies SET list_type='index_member'")
    _manifest(root, ["https://issuer.test/q2.pdf"])
    assert _auto(root, database)["status"] == "policy_denied"
    assert not transport and not classifications
    assert not (root / ".tmp" / "managed_ir_staging").exists()


def test_missing_committed_receipt_proof_blocks_repair_without_source_work(
    state: tuple[Path, Path],
    transport: list[str],
    classifications: list[tuple[str | None, str | None]],
) -> None:
    root, database = state
    _manifest(root, ["https://issuer.test/q2.pdf"])
    assert _auto(root, database)["ready"] == 1
    result_path = next(
        (root / "data" / "managed_ir_publications").glob("*/publication_result.json")
    )
    result_path.chmod(0o600)
    result_path.unlink()
    durable_receipt = result_path.parent / "staging_receipt.json"
    durable_receipt.chmod(0o600)
    durable_receipt.unlink()
    (
        root / ".tmp" / "managed_ir_staging" / result_path.parent.name / "staging_receipt.json"
    ).unlink()
    before = list(classifications)
    summary = _auto(root, database)
    assert (
        summary["status"],
        summary["registered"],
        summary["projection_failed"],
        summary["failed"],
    ) == ("failed", 0, 1, 1)
    assert len(transport) == 1
    assert classifications == before


def test_legacy_request_and_staged_document_serialization_remain_unchanged() -> None:
    from tests.test_managed_ir_sources import request_fixture

    request = request_fixture()
    payload = request.model_dump(mode="json")
    assert "owner_requested" not in payload
    assert "calendar_override" not in payload
    assert managed.IssuerDocumentStagingRequest.model_validate(payload) == request
    automatic = request.model_copy(
        update={"owner_requested": False, "calendar_override": "calendar"}
    )
    sealed = automatic.model_dump(mode="json")
    assert sealed["owner_requested"] is False
    assert sealed["calendar_override"] == "calendar"
    assert managed.IssuerDocumentStagingRequest.model_validate(sealed) == automatic


def test_two_same_quarter_presentations_preserve_selected_processed_alias_on_reverse_replay(
    state: tuple[Path, Path],
    transport: list[str],
    classifications: list[tuple[str | None, str | None]],
) -> None:
    root, database = state
    first = "https://issuer.test/earnings.pdf"
    second = "https://issuer.test/strategy.pdf"
    _manifest(root, [first, second])
    assert _auto(root, database)["registered"] == 2
    with sqlite3.connect(database) as conn:
        assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM managed_ir_publications").fetchone()[0] == 2
    index_path = root / ".tmp" / "document_index.json"
    index = json.loads(index_path.read_text())
    assert len(index) == 1  # This historical index is one compatibility alias per quarter/type.
    selected = next(iter(index.values()))
    selected["processed"] = True
    index_path.write_text(json.dumps(index))
    before_index = index_path.read_bytes()
    before_classifications = list(classifications)
    manifest_path = fetch.manifest_dir(root) / "MELI_urls.json"
    entries = json.loads(manifest_path.read_text())
    manifest_path.write_text(json.dumps(list(reversed(entries))))
    summary = _auto(root, database)
    assert summary["downloaded"] == 0
    assert summary["registered"] == 0
    assert summary["projection_repaired"] == 0
    assert index_path.read_bytes() == before_index
    assert transport == [first, second]
    assert classifications == before_classifications
    # The coherent selected alias may be outside the current manifest subset.
    other_url = first if selected["ir_url"] == second else second
    manifest_path.write_text(json.dumps([entry for entry in entries if entry["url"] == other_url]))
    subset = _auto(root, database)
    assert subset["projection_repaired"] == 0
    assert index_path.read_bytes() == before_index
    assert transport == [first, second]
    assert classifications == before_classifications


@pytest.mark.parametrize("population", [1, 2])
@pytest.mark.parametrize("damage", ["missing", "invalid_entry", "corrupt_json", "wrong_root"])
def test_projection_repair_requires_one_verified_candidate(
    state: tuple[Path, Path],
    transport: list[str],
    classifications: list[tuple[str | None, str | None]],
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
    population: int,
    damage: str,
) -> None:
    root, database = state
    urls = [f"https://issuer.test/presentation-{i}.pdf" for i in range(population)]
    _manifest(root, urls)
    assert _auto(root, database)["registered"] == population
    index_path = root / ".tmp" / "document_index.json"
    if damage == "missing":
        index_path.unlink()
        original = None
    else:
        index_path.write_text(
            '{"MELI_2026_Q2_presentation":{"processed":true},'
            '"OTHER":{"processed":true,"indexed_at":"original","updated_at":"original"}}'
            if damage == "invalid_entry"
            else "[]"
            if damage == "wrong_root"
            else "invalid-json"
        )
        original = index_path.read_bytes()
    before = list(classifications)
    summary = _auto(root, database)
    assert summary["downloaded"] == summary["registered"] == summary["ready"] == 0
    if damage in {"corrupt_json", "wrong_root"}:
        assert summary["projection_repaired"] == 0
        assert summary["projection_failed"] == population
        assert index_path.read_bytes() == original
        assert "compatibility_projection_index_corrupt" in caplog.text
    elif population == 1:
        assert summary["projection_repaired"] == 1
        assert summary["projection_failed"] == 0
        entry = next(iter(json.loads(index_path.read_text()).values()))
        assert entry["ir_url"] == urls[0]
        assert entry["processed"] is False
        if damage == "invalid_entry":
            assert json.loads(index_path.read_text())["OTHER"] == {
                "processed": True,
                "indexed_at": "original",
                "updated_at": "original",
            }
    else:
        assert summary["projection_repaired"] == 0
        assert summary["projection_failed"] == 2
        assert (index_path.read_bytes() if index_path.exists() else None) == original
        assert "compatibility_projection_ambiguous" in caplog.text
    assert transport == urls
    assert classifications == before
    with sqlite3.connect(database) as conn:
        assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == population
        assert (
            conn.execute("SELECT COUNT(*) FROM managed_ir_publications").fetchone()[0] == population
        )
    capsys.readouterr()


def _fetch_cli_child(argv: list[str]) -> subprocess.CompletedProcess[str]:
    output, errors = io.StringIO(), io.StringIO()
    offset = next(
        index for index, arg in enumerate(argv) if Path(arg).name == "fetch_ir_documents.py"
    )
    with redirect_stdout(output), redirect_stderr(errors):
        code = fetch.main(argv[offset + 1 :])
    return subprocess.CompletedProcess(argv, code, output.getvalue(), errors.getvalue())


@pytest.mark.parametrize("damage", ["corrupt_json", "wrong_root", "io_error"])
def test_unreadable_whole_index_preserves_bytes_on_fresh_publication_and_replay(
    state: tuple[Path, Path],
    transport: list[str],
    classifications: list[tuple[str | None, str | None]],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    damage: str,
) -> None:
    root, database = state
    url = "https://issuer.test/q2.pdf"
    index_path = root / ".tmp" / "document_index.json"
    index_path.parent.mkdir(parents=True)
    original = b"invalid-json" if damage == "corrupt_json" else b"[]"
    index_path.write_bytes(original)
    monkeypatch.setattr(manual, "_run_narrative", _forbid_narrative)
    if damage == "io_error":
        read_text = Path.read_text

        def denied(path: Path, encoding: str | None = None, errors: str | None = None) -> str:
            if path == index_path:
                raise PermissionError("synthetic projection read denied")
            return read_text(path, encoding=encoding, errors=errors)

        monkeypatch.setattr(Path, "read_text", denied)
    for attempt in range(2):
        before = list(classifications)
        assert (
            manual.register_urls(
                "MELI", [url], repo_root=root, db_path=database, year=2026, quarter=2
            )
            == 0
        )
        assert index_path.read_bytes() == original
        if attempt:
            assert classifications == before
        with sqlite3.connect(database) as conn:
            assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 1
            assert conn.execute("SELECT brief_dirty FROM tracked_companies").fetchone()[0] == 0
            assert conn.execute("SELECT last_status FROM ir_fetch_status").fetchone()[0] == "failed"
    assert transport == [url]
    expected = (
        "synthetic projection read denied"
        if damage == "io_error"
        else "compatibility_projection_index_corrupt"
    )
    assert expected in caplog.text
    if damage == "io_error":
        assert "compatibility_projection_index_corrupt" not in caplog.text


@pytest.mark.parametrize("failure", ["anchor", "dirty"])
def test_explicit_effect_failure_recovers_on_coherent_replay(
    state: tuple[Path, Path],
    transport: list[str],
    classifications: list[tuple[str | None, str | None]],
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    root, database = state
    url = "https://issuer.test/q2.pdf"
    anchor_results = [1, 0] if failure == "anchor" else [0, 0]
    anchors: list[str] = []

    def child(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        if any(Path(arg).name == "process_document_evidence.py" for arg in argv):
            return _evidence_child(argv, database)
        assert any(Path(arg).name == "ir_narrative.py" for arg in argv)
        anchors.append("MELI")
        return subprocess.CompletedProcess(argv, anchor_results.pop(0), "", "")

    monkeypatch.setattr(manual.subprocess, "run", child)
    if failure == "dirty":
        with sqlite3.connect(database) as conn:
            conn.execute(
                "CREATE TRIGGER reject_dirty BEFORE UPDATE OF brief_dirty ON tracked_companies "
                "WHEN NEW.brief_dirty=1 BEGIN SELECT RAISE(ABORT,'synthetic dirty failure'); END"
            )
    assert (
        manual.register_urls("MELI", [url], repo_root=root, db_path=database, year=2026, quarter=2)
        == 0
    )
    with sqlite3.connect(database) as conn:
        assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 1
        assert conn.execute("SELECT brief_dirty FROM tracked_companies").fetchone()[0] == 0
        assert conn.execute("SELECT last_status FROM ir_fetch_status").fetchone()[0] == "failed"
        if failure == "dirty":
            conn.execute("DROP TRIGGER reject_dirty")
    before = list(classifications)
    assert (
        manual.register_urls("MELI", [url], repo_root=root, db_path=database, year=2026, quarter=2)
        == 1
    )
    assert anchors == ["MELI", "MELI"]
    assert transport == [url]
    assert classifications == before
    with sqlite3.connect(database) as conn:
        assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 1
        assert conn.execute("SELECT brief_dirty FROM tracked_companies").fetchone()[0] == 1
        assert conn.execute("SELECT last_status FROM ir_fetch_status").fetchone()[0] == "ok"


@pytest.mark.parametrize(
    "adapter", ["manual", "ticker_cli", "all_cli", "discovery", "discovery_zero_exit"]
)
@pytest.mark.parametrize("failure", ["classification", "projection", "partial"])
def test_actual_failed_intake_stays_failed_through_cli_and_discovery(
    state: tuple[Path, Path],
    transport: list[str],
    classifications: list[tuple[str | None, str | None]],
    monkeypatch: pytest.MonkeyPatch,
    adapter: str,
    failure: str,
) -> None:
    import index_manager

    root, database = state
    good, bad = "https://issuer.test/good.pdf", "https://issuer.test/bad.pdf"
    urls = [good, bad] if failure == "partial" else [bad if failure == "classification" else good]
    _manifest(root, urls)
    classify = managed.classify_ir_file
    anchors: list[str] = []

    def classified(
        path: Path,
        *,
        ticker_hint: str | None = None,
        calendar_override: str | None = None,
        source_url: str | None = None,
    ) -> CategorizationResult | CategorizationFailure:
        if source_url == bad:
            return CategorizationFailure(reason="synthetic rejection", text_sample="synthetic")
        return classify(
            path,
            ticker_hint=ticker_hint,
            calendar_override=calendar_override,
            source_url=source_url,
        )

    def fail_projection(*_args: object, **_kwargs: object) -> bool:
        raise OSError("synthetic projection failure")

    def anchor(ticker: str, _root: Path) -> bool:
        anchors.append(ticker)
        return True

    monkeypatch.setattr(managed, "classify_ir_file", classified)
    if failure == "projection":
        monkeypatch.setattr(index_manager, "register_ir_document", fail_projection)
    if adapter == "manual":
        monkeypatch.setattr(manual, "_run_narrative", anchor)
        assert (
            manual.register_urls(
                "MELI", urls, repo_root=root, db_path=database, year=2026, quarter=2
            )
            == 0
        )
        with sqlite3.connect(database) as conn:
            assert conn.execute("SELECT last_status FROM ir_fetch_status").fetchone()[0] == "failed"
    elif adapter.startswith("discovery"):

        def child(argv: list[str], _timeout: float) -> subprocess.CompletedProcess[str]:
            if any(Path(arg).name == "fetch_ir_documents.py" for arg in argv):
                child_result = _fetch_cli_child(argv)
                return (
                    subprocess.CompletedProcess(argv, 0, child_result.stdout, child_result.stderr)
                    if adapter == "discovery_zero_exit"
                    else child_result
                )
            if any(Path(arg).name == "process_document_evidence.py" for arg in argv):
                return _evidence_child(argv, database)
            assert any(Path(arg).name == "ir_narrative.py" for arg in argv)
            anchors.append("MELI")
            return subprocess.CompletedProcess(argv, 0, "", "")

        monkeypatch.setattr(discovery, "_run_child", child)

        def pending(_root: Path, _ticker: str) -> bool:
            return False

        monkeypatch.setattr(discovery, "has_pending_narrative", pending)
        result = cast(
            Callable[..., discovery.TickerResult], getattr(discovery, "_finish_discovery")
        )(
            discovery.DiscoveryResult("MELI", discovery.TickerStatus.OK, len(urls), 0),
            repo_root=root,
            db_path=database,
            skip_download=False,
            process=True,
            summaries=False,
            download_timeout=30,
            process_timeout=30,
            ticker_deadline=60,
            whole_deadline_at=None,
            owner_requested=False,
        )
        assert result.status is discovery.TickerStatus.FAILED
        assert result.processed is (failure == "partial")
    else:
        scope = ["--all"] if adapter == "all_cli" else ["--ticker", "MELI"]
        assert (
            fetch.main(
                [
                    *scope,
                    "--categorize",
                    "--calendar",
                    "calendar",
                    "--repo-root",
                    str(root),
                    "--db",
                    str(database),
                ]
            )
            == 1
        )
    process_usable = failure == "partial" and (
        adapter == "manual" or adapter.startswith("discovery")
    )
    assert anchors == (["MELI"] if process_usable else [])
    assert transport == urls
    with sqlite3.connect(database) as conn:
        assert conn.execute("SELECT brief_dirty FROM tracked_companies").fetchone()[0] == int(
            process_usable
        )
        assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == int(
            failure != "classification"
        )


def test_discovery_repair_only_completes_deterministic_effects_and_defers_models(
    state: tuple[Path, Path],
    transport: list[str],
    classifications: list[tuple[str | None, str | None]],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import index_manager

    root, database = state
    url = "https://issuer.test/q2.pdf"
    _manifest(root, [url])
    register = index_manager.register_ir_document

    def fail_projection(*_args: object, **_kwargs: object) -> bool:
        raise OSError("synthetic projection failure")

    monkeypatch.setattr(index_manager, "register_ir_document", fail_projection)
    assert _auto(root, database)["ready"] == 0
    before = list(classifications)
    monkeypatch.setattr(index_manager, "register_ir_document", register)
    anchors: list[str] = []

    def child(argv: list[str], _timeout: float) -> subprocess.CompletedProcess[str]:
        if any(Path(arg).name == "fetch_ir_documents.py" for arg in argv):
            return _fetch_cli_child(argv)
        if any(Path(arg).name == "process_document_evidence.py" for arg in argv):
            return _evidence_child(argv, database)
        assert any(Path(arg).name == "ir_narrative.py" for arg in argv), (
            "repair launched a model summary child"
        )
        anchors.append("MELI")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(discovery, "_run_child", child)

    def pending(_root: Path, _ticker: str) -> bool:
        return False

    monkeypatch.setattr(discovery, "has_pending_narrative", pending)
    result = cast(Callable[..., discovery.TickerResult], getattr(discovery, "_finish_discovery"))(
        discovery.DiscoveryResult("MELI", discovery.TickerStatus.OK, 1, 0),
        repo_root=root,
        db_path=database,
        skip_download=False,
        process=True,
        summaries=True,
        download_timeout=30,
        process_timeout=30,
        ticker_deadline=60,
        whole_deadline_at=None,
        owner_requested=False,
    )
    assert result.status is discovery.TickerStatus.OK and result.processed
    assert result.downloaded == 0
    assert anchors == ["MELI"]
    assert transport == [url] and classifications == before
    with sqlite3.connect(database) as conn:
        assert conn.execute("SELECT brief_dirty FROM tracked_companies").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 1
    assert '"event": "ir_model_summaries_deferred"' in capsys.readouterr().out
