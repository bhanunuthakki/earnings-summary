"""Summary processing completion is distinct from metric presence or admission."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable, Iterator, Mapping
from datetime import datetime
from pathlib import Path
from typing import Literal

import pytest

from compute import kpi_extract_summaries as summaries
from models.runs import StageName, StageStatus
from pipeline.run_accounting import JsonValue, PipelineRunSuppressedError
from provenance.evidence_ledger import (
    ContentBlob,
    DocumentVersion,
    EvidenceLedger,
    EvidenceNode,
    ExtractionRun,
    SourceObservation,
)
from provenance.legacy_document_evidence import (
    LegacyDocumentEvidenceBindingLedger,
    LegacyDocumentEvidenceBindingRevision,
    LegacyDocumentScopeLocator,
)
from provenance.llm_extracted_parent import resolve_parent


@pytest.fixture
def workspace(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> Iterator[tuple[Path, sqlite3.Connection]]:
    root = tmp_path.resolve()
    (root / ".tmp").mkdir()
    holdings = root / "micro_thesis" / "holdings"
    holdings.mkdir(parents=True)
    (holdings / "NU.json").write_text(
        json.dumps({"tier_1_kpis": [{"name": "Net interest margin"}]}), encoding="utf-8"
    )
    conn = sqlite3.connect(migrated_db(root / "test.sqlite"))
    conn.row_factory = sqlite3.Row
    try:
        yield root, conn
    finally:
        conn.close()


def _extractor(calls: list[str]) -> Callable[..., dict[str, dict[str, object]]]:
    def extract(
        ticker: str, period_label: str, names: list[str], text: str, **kwargs: object
    ) -> dict[str, dict[str, object]]:
        calls.append(text)
        assert names == ["Net interest margin"]
        return {
            names[0]: {
                "value": 17.8,
                "unit": "percent",
                "confidence": 0.9,
                "source_excerpt": text,
                "reported_period_end": "2025-03-31",
                "period_role": "current",
                "accounting_basis": "management",
                "consolidation_scope": "consolidated",
                "source_unit_scale": "none",
            }
        }

    return extract


def _register_summary(
    conn: sqlite3.Connection,
    source: Path,
    text: str,
    *,
    version_ticker: str = "NU",
    extraction_outcome: Literal["succeeded", "failed"] = "succeeded",
    extraction_input_sha: str | None = None,
) -> int:
    """Preserve real synthetic bytes and their extraction lineage, without admission."""
    source.write_text(text, encoding="utf-8")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    preserved = source.parent / f"synthetic-evidence-{digest}.txt"
    preserved.write_bytes(source.read_bytes())
    stamp = datetime(2025, 5, 1)
    quarter = int(source.name.split("_Q")[1][0])
    month, day = {1: (3, 31), 2: (6, 30), 3: (9, 30), 4: (12, 31)}[quarter]
    period = datetime(2025, month, day)
    match = resolve_parent(
        conn, ticker="NU", doc_type="llm_summary", file_path=str(source), period_end=period
    )
    conn.execute(
        "INSERT INTO documents(ticker,source_type,doc_type,period_end,file_path,sha256,"
        "fetched_at,fetch_status,raw_bytes_size,source_quality_tier,parent_document_id) "
        "VALUES ('NU','llm_extracted','llm_summary',?,?,?,?, 'ok',?,4,?) ON CONFLICT(sha256) DO NOTHING",
        (
            period.isoformat(sep=" "),
            str(source),
            digest,
            stamp.isoformat(),
            len(source.read_bytes()),
            match.parent_document_id if match else None,
        ),
    )
    document_id = int(
        conn.execute("SELECT id FROM documents WHERE sha256=?", (digest,)).fetchone()[0]
    )
    identity = f"synthetic-summary-{document_id}"
    ledger = EvidenceLedger(conn)
    ledger.persist(
        ContentBlob(
            sha256=digest,
            byte_size=len(source.read_bytes()),
            media_type="text/plain",
            storage_uri=preserved.as_uri(),
            recorded_at=stamp,
        )
    )
    ledger.persist(
        SourceObservation(
            observation_id=identity,
            idempotency_key=identity,
            source_kind="test_fixture",
            source_url=f"https://example.test/NU/{identity}",
            blob_sha256=digest,
            source_published_at=stamp,
            filing_at=None,
            accepted_at=None,
            observed_at=stamp,
            retrieved_at=stamp,
            retrieval_config_sha256=digest,
            collector_code_version="test-v1",
        )
    )
    ledger.persist(
        DocumentVersion(
            document_version_id=identity,
            document_key=identity,
            version_sequence=1,
            observation_id=identity,
            blob_sha256=digest,
            issuer_id="fixture:NU",
            ticker=version_ticker,
            document_type="llm_summary",
            form_type="summary",
            language="en",
            period_end=period,
            legacy_document_id=document_id,
            recorded_at=stamp,
        )
    )
    ledger.persist(
        ExtractionRun(
            extraction_run_id=identity,
            idempotency_key=identity,
            document_version_id=identity,
            input_sha256=extraction_input_sha or digest,
            extractor_name="synthetic-summary",
            extractor_config_sha256=digest,
            extractor_code_version="test-v1",
            output_sha256=digest,
            started_at=stamp,
            completed_at=stamp,
            outcome=extraction_outcome,
        )
    )
    if extraction_outcome == "failed":
        conn.commit()
        return document_id
    ledger.persist(
        EvidenceNode(
            node_id=identity,
            evidence_key=identity,
            revision=1,
            extraction_run_id=identity,
            node_kind="document",
            text=text,
            recorded_at=stamp,
        )
    )
    conn.commit()
    return document_id


def test_changed_summary_is_processed_despite_existing_period_metric(
    workspace: tuple[Path, sqlite3.Connection], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, conn = workspace
    source = root / ".tmp" / "NU_Q1_2025_summary.txt"
    _register_summary(conn, source, "Net interest margin was 17.8%.")
    calls: list[str] = []
    monkeypatch.setattr(summaries, "_llm_extract", _extractor(calls))
    first = summaries.extract_for_ticker("NU", root, conn)
    assert first.error is None
    assert first.kpis_inserted_total == 1
    _register_summary(conn, source, "Revised wording: net interest margin was 17.8%.")
    second = summaries.extract_for_ticker("NU", root, conn)
    assert second.error is None
    assert len(calls) == 2
    assert conn.execute("SELECT COUNT(DISTINCT source_doc_id) FROM kpi_facts").fetchone()[0] == 2
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM kpi_fact_semantic_contexts WHERE status='admitted'"
        ).fetchone()[0]
        == 0
    )


def test_unchanged_empty_attempt_does_not_repeat_model_work(
    workspace: tuple[Path, sqlite3.Connection], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, conn = workspace
    _register_summary(conn, root / ".tmp" / "NU_Q1_2025_summary.txt", "No metric here.")
    calls: list[str] = []

    def empty(*args: object, **kwargs: object) -> dict[str, dict[str, object]]:
        calls.append("called")
        return {}

    monkeypatch.setattr(summaries, "_llm_extract", empty)
    for _ in range(2):
        assert summaries.extract_for_ticker("NU", root, conn).error is None
    assert calls == ["called"]


def _empty_source(root: Path, conn: sqlite3.Connection, quarter: int = 1) -> Path:
    source = root / ".tmp" / f"NU_Q{quarter}_2025_summary.txt"
    _register_summary(conn, source, f"Synthetic quarter {quarter}.")
    return source


def _empty_extractor(calls: list[list[str]]) -> Callable[..., dict[str, dict[str, object]]]:
    def extract(
        ticker: str, period: str, names: list[str], text: str, **kwargs: object
    ) -> dict[str, dict[str, object]]:
        calls.append(list(names))
        return {}

    return extract


def test_completed_document_does_not_stop_later_documents(
    workspace: tuple[Path, sqlite3.Connection],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, conn = workspace
    _empty_source(root, conn)
    calls: list[list[str]] = []
    monkeypatch.setattr(summaries, "_llm_extract", _empty_extractor(calls))
    assert summaries.extract_for_ticker("NU", root, conn).error is None
    _empty_source(root, conn, 2)
    result = summaries.extract_for_ticker("NU", root, conn)
    assert result.error is None
    assert len(calls) == 2
    assert len(result.quarters_skipped_no_missing) == 1
    assert len(result.documents_completed) == 1


def _route_resolver(route: list[str]) -> Callable[..., tuple[str, str]]:
    def resolve(*args: object, **kwargs: object) -> tuple[str, str]:
        return route[0], route[1]

    return resolve


@pytest.mark.parametrize(
    "change", ["source", "holdings", "request", "units", "contract", "model", "backend"]
)
def test_material_input_changes_invalidate_completion(
    workspace: tuple[Path, sqlite3.Connection],
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    root, conn = workspace
    source = _empty_source(root, conn)
    calls: list[list[str]] = []
    route = ["claude-haiku-4-5", "claude"]
    monkeypatch.setattr(summaries, "resolve_model_and_backend", _route_resolver(route))
    monkeypatch.setattr(summaries, "_llm_extract", _empty_extractor(calls))
    assert summaries.extract_for_ticker("NU", root, conn).error is None
    holdings_path = root / "micro_thesis" / "holdings" / "NU.json"
    holdings = json.loads(holdings_path.read_text())
    if change == "source":
        _register_summary(conn, source, "Different source bytes.")
    elif change == "holdings":
        holdings["notes"] = "Changed context"
    elif change == "request":
        holdings["tier_1_kpis"].append({"name": "Customers"})
    elif change == "units":
        holdings["break_rules"] = [{"kpi_name": "Net interest margin", "unit": "percent"}]
    elif change == "contract":
        monkeypatch.setattr(summaries, "_ALLOWLIST_EXTRACTOR_CONTRACT", "test-new-contract")
    elif change == "model":
        route[0] = "claude-sonnet-4-6"
    elif change == "backend":
        route[1] = "codex"
    if change in {"holdings", "request", "units"}:
        holdings_path.write_text(json.dumps(holdings), encoding="utf-8")
    assert summaries.extract_for_ticker("NU", root, conn).error is None
    assert len(calls) == 2
    assert (
        conn.execute("SELECT COUNT(DISTINCT pipeline_key) FROM pipeline_attempts").fetchone()[0]
        == 2
    )


def test_empty_disposition_is_durable_and_unreturned_is_unknown(
    workspace: tuple[Path, sqlite3.Connection],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, conn = workspace
    _empty_source(root, conn)
    calls: list[list[str]] = []
    monkeypatch.setattr(summaries, "_llm_extract", _empty_extractor(calls))
    result = summaries.extract_for_ticker("NU", root, conn)
    assert result.error is None
    disposition = result.documents_completed[0]
    assert disposition["returned_kpis"] == []
    assert disposition["unreturned_kpis"] == ["Net interest margin"]
    assert disposition["unreturned_disposition"] == "unknown"
    assert disposition["completion_scope"] == "bounded_processing"
    stored = conn.execute(
        "SELECT error_msg FROM pipeline_stage_transitions WHERE stage='parse'"
    ).fetchone()[0]
    assert json.loads(stored) == disposition
    assert conn.execute("SELECT COUNT(*) FROM kpi_facts").fetchone()[0] == 0


def test_partial_output_completes_and_preserves_unknown_names(
    workspace: tuple[Path, sqlite3.Connection],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, conn = workspace
    source = root / ".tmp" / "NU_Q1_2025_summary.txt"
    _register_summary(conn, source, "Net interest margin was 17.8%.")
    holdings = root / "micro_thesis" / "holdings" / "NU.json"
    holdings.write_text(
        json.dumps({"tier_1_kpis": [{"name": "Net interest margin"}, {"name": "Customers"}]})
    )
    calls: list[str] = []
    original = _extractor(calls)

    def partial(
        ticker: str, period: str, names: list[str], text: str, **kwargs: object
    ) -> dict[str, dict[str, object]]:
        assert names == ["Net interest margin", "Customers"]
        return original(ticker, period, names[:1], text, **kwargs)

    monkeypatch.setattr(summaries, "_llm_extract", partial)
    first = summaries.extract_for_ticker("NU", root, conn)
    assert first.error is None
    assert first.documents_completed[0]["unreturned_kpis"] == ["Customers"]
    assert first.kpis_inserted_total == 1
    assert summaries.extract_for_ticker("NU", root, conn).error is None
    assert len(calls) == 1


def test_model_failure_is_retryable_and_route_is_bound_to_attempt(
    workspace: tuple[Path, sqlite3.Connection],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, conn = workspace
    _empty_source(root, conn)
    observed: list[dict[str, object]] = []
    monkeypatch.setattr(
        summaries,
        "resolve_model_and_backend",
        _route_resolver(["claude-haiku-4-5", "claude"]),
    )

    def extract(*args: object, **kwargs: object) -> dict[str, dict[str, object]]:
        observed.append(kwargs)
        run = conn.execute(
            "SELECT status FROM pipeline_attempts WHERE attempt_id=?", (kwargs["run_id"],)
        ).fetchone()
        assert run[0] == "in_progress"
        if len(observed) == 1:
            raise RuntimeError("synthetic model failure")
        return {}

    monkeypatch.setattr(summaries, "_llm_extract", extract)
    first = summaries.extract_for_ticker("NU", root, conn)
    assert first.error and "synthetic model failure" in first.error
    assert summaries.extract_for_ticker("NU", root, conn).error is None
    assert len(observed) == 2
    assert observed[0]["model"] == "claude-haiku-4-5"
    assert observed[0]["backend"] == "claude"
    assert observed[0]["db_path"] == root / "test.sqlite"
    assert [r[0] for r in conn.execute("SELECT status FROM pipeline_attempts ORDER BY rowid")] == [
        "failed",
        "ok",
    ]


@pytest.mark.parametrize("target", ["summary", "holdings", "document", "route"])
def test_drift_during_model_work_fails_before_persistence(
    workspace: tuple[Path, sqlite3.Connection],
    monkeypatch: pytest.MonkeyPatch,
    target: str,
) -> None:
    root, conn = workspace
    source = _empty_source(root, conn)
    route = ["claude-haiku-4-5", "claude"]
    monkeypatch.setattr(summaries, "resolve_model_and_backend", _route_resolver(route))

    def drift(*args: object, **kwargs: object) -> dict[str, dict[str, object]]:
        if target == "summary":
            source.write_text("Changed during model work.")
        elif target == "holdings":
            path = root / "micro_thesis" / "holdings" / "NU.json"
            path.write_bytes(path.read_bytes() + b" ")
        elif target == "document":
            conn.execute("UPDATE documents SET sha256='changed' WHERE doc_type='llm_summary'")
        else:
            route[0] = "claude-sonnet-4-6"
        return {}

    monkeypatch.setattr(summaries, "_llm_extract", drift)
    assert summaries.extract_for_ticker("NU", root, conn).error is not None
    assert conn.execute("SELECT status FROM pipeline_attempts").fetchone()[0] == "failed"
    assert conn.execute("SELECT COUNT(*) FROM pipeline_stage_transitions").fetchone()[0] == 0
    monkeypatch.setattr(summaries, "_llm_extract", _empty_extractor([]))
    if target == "summary":
        _register_summary(conn, source, source.read_text())
    assert summaries.extract_for_ticker("NU", root, conn).error is None


def test_pending_fact_and_stage_writes_roll_back_before_failed_accounting(
    workspace: tuple[Path, sqlite3.Connection],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, conn = workspace
    source = root / ".tmp" / "NU_Q1_2025_summary.txt"
    _register_summary(conn, source, "Net interest margin was 17.8%.")
    calls: list[str] = []
    monkeypatch.setattr(summaries, "_llm_extract", _extractor(calls))
    original = summaries.record_stage

    def fail_stage(
        connection: sqlite3.Connection,
        run_id: str,
        ticker: str,
        stage: StageName,
        status: StageStatus,
        *,
        period_end: datetime | None = None,
        error_msg: str | None = None,
        commit: bool = True,
    ) -> None:
        original(
            connection,
            run_id,
            ticker,
            stage,
            status,
            period_end=period_end,
            error_msg=error_msg,
            commit=commit,
        )
        assert conn.execute("SELECT COUNT(*) FROM kpi_facts").fetchone()[0] == 1
        raise RuntimeError("synthetic stage failure")

    monkeypatch.setattr(summaries, "record_stage", fail_stage)
    result = summaries.extract_for_ticker("NU", root, conn)
    assert result.error and "synthetic stage failure" in result.error
    assert conn.execute("SELECT COUNT(*) FROM kpi_facts").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM reported_observations").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM pipeline_stage_transitions").fetchone()[0] == 0
    assert conn.execute("SELECT status FROM pipeline_attempts").fetchone()[0] == "failed"
    monkeypatch.setattr(summaries, "record_stage", original)
    assert summaries.extract_for_ticker("NU", root, conn).kpis_inserted_total == 1
    assert len(calls) == 2


def test_missing_evidence_blocks_model_and_repaired_evidence_is_retryable(
    workspace: tuple[Path, sqlite3.Connection],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, conn = workspace
    source = root / ".tmp" / "NU_Q1_2025_summary.txt"
    source.write_text("Net interest margin was 17.8%.", encoding="utf-8")
    calls: list[str] = []
    monkeypatch.setattr(summaries, "_llm_extract", _extractor(calls))
    for _ in range(2):
        result = summaries.extract_for_ticker("NU", root, conn)
        assert result.error and "evidence version is missing" in result.error
    assert calls == []
    assert conn.execute("SELECT COUNT(*) FROM kpi_facts").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM pipeline_attempts").fetchone()[0] == 0
    # Preserve the existing legacy source and add its missing evidence lineage.
    _register_summary(conn, source, "Net interest margin was 17.8%.")
    result = summaries.extract_for_ticker("NU", root, conn)
    assert result.error is None
    assert len(calls) == 1
    assert result.kpis_inserted_total == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("ticker", "OTHER"),
        ("doc_type", "other"),
        ("source_type", "test"),
        ("period_end", "2024-12-31"),
    ],
)
def test_same_hash_wrong_document_identity_fails_before_model(
    workspace: tuple[Path, sqlite3.Connection],
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: str,
) -> None:
    root, conn = workspace
    source = _empty_source(root, conn)
    doc_id = _register_summary(conn, source, "Synthetic summary bytes.")
    conn.execute(f"UPDATE documents SET {field}=? WHERE id=?", (value, doc_id))
    conn.commit()
    calls: list[list[str]] = []
    monkeypatch.setattr(summaries, "_llm_extract", _empty_extractor(calls))
    result = summaries.extract_for_ticker("NU", root, conn)
    assert result.error and "identity or parent binding" in result.error
    assert calls == []
    assert conn.execute("SELECT COUNT(*) FROM pipeline_attempts").fetchone()[0] == 0


def test_refresh_repeats_completed_work_but_preserves_active_writer(
    workspace: tuple[Path, sqlite3.Connection],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, conn = workspace
    _empty_source(root, conn)
    calls: list[list[str]] = []
    monkeypatch.setattr(summaries, "_llm_extract", _empty_extractor(calls))
    assert summaries.extract_for_ticker("NU", root, conn).error is None
    assert summaries.extract_for_ticker("NU", root, conn, refresh=True).error is None
    assert len(calls) == 2
    original = summaries.start_run
    active_id: list[str] = []

    def reserve(
        connection: sqlite3.Connection,
        directive: str,
        ticker_scope: list[str],
        *,
        invocation_inputs: Mapping[str, JsonValue] | None = None,
        force: bool = False,
        deduplicate_completed: bool = False,
    ) -> str:
        run_id = original(
            connection,
            directive,
            ticker_scope,
            invocation_inputs=invocation_inputs,
            force=force,
            deduplicate_completed=deduplicate_completed,
        )
        active_id.append(run_id)
        raise PipelineRunSuppressedError("synthetic", run_id, StageStatus.IN_PROGRESS)

    monkeypatch.setattr(summaries, "start_run", reserve)
    with pytest.raises(PipelineRunSuppressedError):
        summaries.extract_for_ticker("NU", root, conn, refresh=True)
    monkeypatch.setattr(summaries, "start_run", original)
    with pytest.raises(PipelineRunSuppressedError) as caught:
        summaries.extract_for_ticker("NU", root, conn, refresh=True)
    assert caught.value.attempt_id == active_id[0]
    assert (
        conn.execute(
            "SELECT status FROM pipeline_attempts WHERE attempt_id=?", (active_id[0],)
        ).fetchone()[0]
        == "in_progress"
    )
    assert len(calls) == 2


def _parent(conn: sqlite3.Connection, root: Path) -> int:
    path = root / "NU_Q1_2025.txt"
    payload = b"Synthetic parent transcript bytes."
    path.write_bytes(payload)
    cur = conn.execute(
        "INSERT INTO documents(ticker,source_type,doc_type,period_end,file_path,sha256,"
        "fetch_status,fetched_at,raw_bytes_size) "
        "VALUES ('NU','transcript_audio','earnings_call_transcript','2025-03-31 00:00:00',"
        "?,?,'ok','2025-05-01',?)",
        (str(path), hashlib.sha256(payload).hexdigest(), len(payload)),
    )
    assert cur.lastrowid is not None
    conn.commit()
    return cur.lastrowid


def test_parent_hash_change_invalidates_completion_and_parent_drift_fails(
    workspace: tuple[Path, sqlite3.Connection],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, conn = workspace
    parent_id = _parent(conn, root)
    _empty_source(root, conn)
    calls: list[list[str]] = []
    monkeypatch.setattr(summaries, "_llm_extract", _empty_extractor(calls))
    assert summaries.extract_for_ticker("NU", root, conn).error is None
    conn.execute("UPDATE documents SET sha256='parent-new-version' WHERE id=?", (parent_id,))
    conn.commit()
    assert summaries.extract_for_ticker("NU", root, conn).error is None
    assert len(calls) == 2

    def drift(*args: object, **kwargs: object) -> dict[str, dict[str, object]]:
        conn.execute("UPDATE documents SET sha256='during-extraction' WHERE id=?", (parent_id,))
        return {}

    monkeypatch.setattr(summaries, "_llm_extract", drift)
    result = summaries.extract_for_ticker("NU", root, conn, refresh=True)
    assert result.error and "database inputs changed" in result.error
    assert (
        conn.execute("SELECT sha256 FROM documents WHERE id=?", (parent_id,)).fetchone()[0]
        == "parent-new-version"
    )


def test_new_parent_does_not_silently_repair_existing_unlinked_summary(
    workspace: tuple[Path, sqlite3.Connection],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, conn = workspace
    _empty_source(root, conn)
    calls: list[list[str]] = []
    monkeypatch.setattr(summaries, "_llm_extract", _empty_extractor(calls))
    assert summaries.extract_for_ticker("NU", root, conn).error is None
    _parent(conn, root)
    result = summaries.extract_for_ticker("NU", root, conn)
    assert result.error and "identity or parent binding" in result.error
    assert (
        conn.execute(
            "SELECT parent_document_id FROM documents WHERE doc_type='llm_summary'"
        ).fetchone()[0]
        is None
    )
    assert len(calls) == 1


def test_resolved_unit_mapping_alone_invalidates_completion(
    workspace: tuple[Path, sqlite3.Connection],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from models.facts import Unit

    root, conn = workspace
    _empty_source(root, conn)
    calls: list[list[str]] = []
    units = {"Net interest margin": Unit.PERCENT}

    def resolve_units(*args: object, **kwargs: object) -> dict[str, Unit]:
        return dict(units)

    monkeypatch.setattr(summaries, "_canonical_units_from_holdings", resolve_units)
    monkeypatch.setattr(summaries, "_llm_extract", _empty_extractor(calls))
    assert summaries.extract_for_ticker("NU", root, conn).error is None
    units.clear()
    assert summaries.extract_for_ticker("NU", root, conn).error is None
    assert len(calls) == 2


def test_original_failure_survives_failed_accounting(
    workspace: tuple[Path, sqlite3.Connection],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, conn = workspace
    _empty_source(root, conn)

    def model_failure(*args: object, **kwargs: object) -> dict[str, dict[str, object]]:
        raise ValueError("original synthetic error")

    def accounting_failure(*args: object, **kwargs: object) -> None:
        raise RuntimeError("secondary accounting error")

    monkeypatch.setattr(summaries, "_llm_extract", model_failure)
    monkeypatch.setattr(summaries, "end_run", accounting_failure)
    result = summaries.extract_for_ticker("NU", root, conn)
    assert result.error and "original synthetic error" in result.error
    assert "secondary accounting error" not in result.error
    assert not conn.in_transaction


@pytest.mark.parametrize("mismatch", ["issuer", "extractor_failure"])
def test_existing_evidence_mismatch_blocks_all_model_work(
    workspace: tuple[Path, sqlite3.Connection],
    monkeypatch: pytest.MonkeyPatch,
    mismatch: str,
) -> None:
    root, conn = workspace
    source = root / ".tmp" / "NU_Q1_2025_summary.txt"
    _register_summary(
        conn,
        source,
        "Net interest margin was 17.8%.",
        version_ticker="OTHER" if mismatch == "issuer" else "NU",
        extraction_outcome="failed" if mismatch == "extractor_failure" else "succeeded",
    )
    calls: list[str] = []
    monkeypatch.setattr(summaries, "_llm_extract", _extractor(calls))
    before = conn.total_changes
    result = summaries.extract_for_ticker("NU", root, conn)
    assert result.error and "evidence" in result.error
    assert calls == []
    assert conn.total_changes == before
    assert conn.execute("SELECT COUNT(*) FROM kpi_facts").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM pipeline_attempts").fetchone()[0] == 0


def test_invalid_explicit_binding_never_falls_back_to_valid_direct_version(
    workspace: tuple[Path, sqlite3.Connection],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, conn = workspace
    source = root / ".tmp" / "NU_Q1_2025_summary.txt"
    document_id = _register_summary(conn, source, "Net interest margin was 17.8%.")
    identity = f"synthetic-summary-{document_id}"
    stamp = datetime(2025, 5, 1)
    LegacyDocumentEvidenceBindingLedger(conn).persist(
        LegacyDocumentEvidenceBindingRevision(
            binding_revision_id="invalid-source-hash",
            idempotency_key="invalid-source-hash",
            legacy_document_id=document_id,
            revision=1,
            document_version_id=identity,
            evidence_node_id=identity,
            scope_locator=LegacyDocumentScopeLocator(source_ref=str(source)),
            scope_content_sha256="0" * 64,
            effective_at=stamp,
            knowledge_at=stamp,
            recorded_at=stamp,
        )
    )
    conn.commit()
    calls: list[str] = []
    monkeypatch.setattr(summaries, "_llm_extract", _extractor(calls))
    before = conn.total_changes
    result = summaries.extract_for_ticker("NU", root, conn)
    assert result.error and "binding source hash does not match" in result.error
    assert calls == []
    assert conn.total_changes == before
    assert conn.execute("SELECT COUNT(*) FROM pipeline_attempts").fetchone()[0] == 0


def test_existing_database_rejects_mismatched_extractor_input_before_model(
    workspace: tuple[Path, sqlite3.Connection],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, conn = workspace
    source = root / ".tmp" / "NU_Q1_2025_summary.txt"
    with pytest.raises(sqlite3.IntegrityError, match="input hash must match"):
        _register_summary(
            conn, source, "Net interest margin was 17.8%.", extraction_input_sha="0" * 64
        )
    conn.commit()
    calls: list[str] = []
    monkeypatch.setattr(summaries, "_llm_extract", _extractor(calls))
    result = summaries.extract_for_ticker("NU", root, conn)
    assert result.error and "evidence node or successful extractor" in result.error
    assert calls == []
