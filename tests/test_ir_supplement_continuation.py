"""Hermetic continuation, replay, and artifact transaction contract tests."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import cast

import pytest

import compute.ir_supplement_capture as continuation
from compute.ir_supplement_capture import (
    SupplementCapture,
    SupplementExchange,
    SupplementSaturatedError,
)
from llm_artifact_store import UpsertRequest, read_current, upsert
from provenance.fulltext_backfill import project_pdf_page_texts


@pytest.fixture
def conn(tmp_path: Path, migrated_db: Callable[..., Path]) -> Iterator[sqlite3.Connection]:
    db = migrated_db(tmp_path / "continuation.db")
    connection = sqlite3.connect(db)
    connection.row_factory = sqlite3.Row
    yield connection
    connection.close()


def _synthetic_exchange(
    conn: sqlite3.Connection,
    prompt: str,
    rows: list[dict[str, object]],
    *,
    model: str = "qualified-model",
    effective_prompt: str | None = None,
) -> SupplementExchange:
    raw = json.dumps(rows)
    effective = effective_prompt if effective_prompt is not None else prompt
    prompt_sha = hashlib.sha256(effective.encode()).hexdigest()
    response_sha = hashlib.sha256(raw.encode()).hexdigest()
    run_id = "fixture:" + uuid.uuid4().hex
    cursor = conn.execute(
        "INSERT INTO llm_calls (called_at,model,prompt_sha256,prompt_chars,elapsed_ms,"
        "purpose,response_sha256,response_chars,provider,transport,outcome,run_id) "
        "VALUES ('2026-10-23',?,?, ?,0,'kpi_summary_enumerate',?,?,'fixture','fixture','success',?)",
        (model, prompt_sha, len(effective), response_sha, len(raw), run_id),
    )
    conn.commit()
    return SupplementExchange(
        raw_response=raw,
        submitted_prompt=prompt,
        model=model,
        provider="fixture",
        transport="fixture",
        prompt_sha256=prompt_sha,
        response_sha256=response_sha,
        llm_call_id=int(cursor.lastrowid or 0),
        run_id=run_id,
    )


def _capture(
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
    pages: tuple[str, ...],
    extract: Callable[[str], list[dict[str, object]]],
    **limits: object,
) -> SupplementCapture:
    def project_pages(raw: bytes) -> tuple[str, ...]:
        return pages

    monkeypatch.setattr(continuation, "project_pdf_page_texts", project_pages)
    defaults: dict[str, object] = {
        "max_input_chars": 80_000,
        "max_facts": 200,
        "max_chunks": 256,
        "max_structured_calls": 256,
    }
    defaults.update(limits)

    def governed_extract(
        text: str, _body: str, retain_raw: Callable[[SupplementExchange], None]
    ) -> SupplementExchange:
        try:
            rows = extract(text)
        except SupplementSaturatedError:
            saturated_row: dict[str, object] = {"source_excerpt": text[:80]}
            rows = [saturated_row] * capture.max_facts
        prompt = capture.prompt(text)
        exchange = _synthetic_exchange(
            conn,
            prompt,
            rows,
            model=capture.model,
            effective_prompt=capture.effective_prompt(prompt),
        )
        retain_raw(exchange)
        return exchange

    capture = SupplementCapture(
        conn=conn,
        ticker="TEST",
        document_id=1,
        source_sha256=hashlib.sha256(b"original source").hexdigest(),
        model="qualified-model",
        backend="qualified-backend",
        period_label="2026-06-30",
        max_input_chars=int(cast(int, defaults["max_input_chars"])),
        max_facts=int(cast(int, defaults["max_facts"])),
        max_chunks=int(cast(int, defaults["max_chunks"])),
        max_structured_calls=int(cast(int, defaults["max_structured_calls"])),
        prompt=lambda text: "EXACT PROMPT " + text,
        extract=governed_extract,
        effective_prompt=lambda prompt: prompt,
        override_identity=lambda: "no-override",
        validate_rows=lambda raw: cast(list[dict[str, object]], raw),
    )
    return capture


def test_more_than_80k_and_200_facts_last_page_and_replay(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    pages = tuple(
        "\n".join(f"Metric {page}:{item} 10% " + "x" * 250 for item in range(120))
        for page in range(3)
    )
    calls: list[str] = []

    def extract(text: str) -> list[dict[str, object]]:
        calls.append(text)
        return [
            {"label": line.split(" 10%")[0], "source_excerpt": line[:80]}
            for line in text.splitlines()
        ]

    job = _capture(conn, monkeypatch, pages, extract)
    output = job.capture(b"original source")
    assert sum(len(rows) for _, rows in output) == 360
    assert output[-1][1][-1]["label"] == "Metric 2:119"
    assert len(calls) == 3
    assert _capture(conn, monkeypatch, pages, extract).capture(b"original source") == output
    assert len(calls) == 3


def test_failed_later_page_replays_earlier_page(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    failed = True

    def extract(text: str) -> list[dict[str, object]]:
        nonlocal failed
        calls.append(text)
        if text == "last 30%" and failed:
            failed = False
            raise ValueError("provider failed")
        return [{"label": text, "source_excerpt": text}]

    pages = ("first 20%", "last 30%")
    with pytest.raises(ValueError, match="provider failed"):
        _capture(conn, monkeypatch, pages, extract).capture(b"original source")
    output = _capture(conn, monkeypatch, pages, extract).capture(b"original source")
    assert len(output) == 2
    assert calls == ["first 20%", "last 30%", "last 30%"]


def test_changed_bytes_prompt_and_model_invalidate_cache(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    def extract(text: str) -> list[dict[str, object]]:
        calls.append(text)
        return [{"source_excerpt": text}]

    job = _capture(conn, monkeypatch, ("Margin 10%",), extract)
    job.capture(b"original source")
    changed = _capture(conn, monkeypatch, ("Margin 10%",), extract)
    changed.source_sha256 = hashlib.sha256(b"changed bytes").hexdigest()
    changed.capture(b"changed bytes")
    other_model = _capture(conn, monkeypatch, ("Margin 10%",), extract)
    other_model.source_sha256 = changed.source_sha256
    other_model.model = "other-qualified-model"
    other_model.capture(b"changed bytes")
    other_prompt = _capture(conn, monkeypatch, ("Margin 10%",), extract)
    other_prompt.source_sha256 = changed.source_sha256
    other_prompt.model = other_model.model
    other_prompt.prompt = lambda text: "NEW PROMPT " + text
    other_prompt.capture(b"changed bytes")
    assert len(calls) == 4


@pytest.mark.parametrize("limits", [{"max_chunks": 2}, {"max_structured_calls": 4}])
def test_global_budget_preflight_makes_no_calls(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, limits: dict[str, int]
) -> None:
    def extract(text: str) -> list[dict[str, object]]:
        pytest.fail("preflight must occur before model work")

    with pytest.raises(ValueError, match="before extraction"):
        _capture(conn, monkeypatch, ("A", "B", "C"), extract, **limits).capture(b"original source")


@pytest.mark.parametrize("pages", [("one", ""), ("",), ()])
def test_empty_image_pages_unresolved_before_calls(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, pages: tuple[str, ...]
) -> None:
    def extract(text: str) -> list[dict[str, object]]:
        pytest.fail("unresolved native pages must block model work")

    with pytest.raises(ValueError, match="unresolved"):
        _capture(conn, monkeypatch, pages, extract).capture(b"original source")


def test_saturation_splits_with_source_context_and_replays(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    page = "Period 2026-06-30 USD millions\nNorth margin 10%\nSouth margin 20%\n"
    calls: list[str] = []

    def extract(text: str) -> list[dict[str, object]]:
        calls.append(text)
        if text == page:
            raise SupplementSaturatedError("split")
        body = text.split("EXTRACT ONLY FROM THIS SOURCE SECTION:\n")[-1]
        return [
            {"source_excerpt": line, "label": "Margin", "scope": line.split()[0]}
            for line in body.splitlines()
            if "margin" in line
        ]

    output = _capture(conn, monkeypatch, (page,), extract).capture(b"original source")
    assert [row["scope"] for _, rows in output for row in rows] == ["North", "South"]
    assert all("Period 2026-06-30 USD millions" in text for text in calls)
    assert len(calls) > 1
    prior_calls = len(calls)
    assert _capture(conn, monkeypatch, (page,), extract).capture(b"original source") == output
    assert len(calls) == prior_calls


def test_saturated_single_source_line_fails_without_truncation(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    def extract(text: str) -> list[dict[str, object]]:
        raise SupplementSaturatedError("full")

    with pytest.raises(ValueError, match="source line unresolved"):
        _capture(conn, monkeypatch, ("one dense unsplittable line",), extract).capture(
            b"original source"
        )


def test_quote_not_in_section_retained_as_rejected_not_processed(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    job = _capture(
        conn, monkeypatch, ("Margin 10%",), lambda text: [{"source_excerpt": "Invented 99%"}]
    )
    with pytest.raises(ValueError, match="quote does not occur"):
        job.capture(b"original source")
    versions = [
        json.loads(row[0])
        for row in conn.execute("SELECT content_json FROM llm_artifacts ORDER BY id")
    ]
    assert [version["status"] for version in versions] == ["pending", "rejected"]
    assert versions[-1]["rows"] == []
    assert "Invented 99%" in versions[-1]["exchange"]["raw_response"]
    with pytest.raises(ValueError, match="explicit refresh"):
        job.capture(b"original source")


def test_cache_output_tamper_cannot_replay(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    def extract(text: str) -> list[dict[str, object]]:
        calls.append(text)
        return [{"source_excerpt": text}]

    _capture(conn, monkeypatch, ("Margin 10%",), extract).capture(b"original source")
    conn.execute("UPDATE llm_artifacts SET content_json='{}'")
    conn.commit()
    _capture(conn, monkeypatch, ("Margin 10%",), extract).capture(b"original source")
    assert len(calls) == 2


def test_native_page_projection_preserves_blank_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    import io

    import pypdf

    # Test parser mechanics; production still rejects the unapproved local runtime.
    from provenance.fulltext_extractor_identity import PDF_FULLTEXT_PYPDF_VERSION

    monkeypatch.setattr(pypdf, "__version__", PDF_FULLTEXT_PYPDF_VERSION)
    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=100, height=100)
    writer.add_blank_page(width=100, height=100)
    buffer = io.BytesIO()
    writer.write(buffer)
    assert project_pdf_page_texts(buffer.getvalue()) == ("", "")


def test_supplied_artifact_connection_preserves_outer_rollback(conn: sqlite3.Connection) -> None:
    conn.execute("CREATE TABLE owner_work (value TEXT)")
    conn.commit()
    conn.execute("INSERT INTO owner_work VALUES ('uncommitted')")
    artifact_id, hit = upsert(
        UpsertRequest(ticker="TEST", purpose="kpi_summary_enumerate", content_json={"rows": []}),
        conn=conn,
    )
    assert artifact_id is not None and not hit
    assert conn.in_transaction
    conn.rollback()
    assert conn.execute("SELECT COUNT(*) FROM owner_work").fetchone()[0] == 0
    assert read_current(ticker="TEST", purpose="kpi_summary_enumerate", conn=conn) is None


def test_supplied_artifact_connection_commit_and_failed_insert_preserve_outer_work(
    conn: sqlite3.Connection,
) -> None:
    conn.execute("CREATE TABLE owner_work (value TEXT)")
    conn.commit()
    conn.execute("INSERT INTO owner_work VALUES ('keep')")
    conn.execute(
        "CREATE TEMP TRIGGER fail_artifact BEFORE INSERT ON llm_artifacts "
        "BEGIN SELECT RAISE(ABORT, 'synthetic failure'); END"
    )
    artifact_id, _ = upsert(
        UpsertRequest(ticker="TEST", purpose="kpi_summary_enumerate"), conn=conn
    )
    assert artifact_id is None
    assert conn.in_transaction
    assert conn.execute("SELECT value FROM owner_work").fetchone()[0] == "keep"
    conn.execute("DROP TRIGGER fail_artifact")
    artifact_id, _ = upsert(
        UpsertRequest(ticker="TEST", purpose="kpi_summary_enumerate", content_json=[]), conn=conn
    )
    assert artifact_id is not None
    conn.commit()
    assert read_current(ticker="TEST", purpose="kpi_summary_enumerate", conn=conn) is not None


def test_native_projection_rejects_unapproved_parser(monkeypatch: pytest.MonkeyPatch) -> None:
    import pypdf

    monkeypatch.setattr(pypdf, "__version__", "unapproved")
    with pytest.raises(Exception, match="pdf_parser_runtime_not_approved"):
        project_pdf_page_texts(b"not parsed")


def _retained_source(conn: sqlite3.Connection, tmp_path: Path) -> tuple[Path, str]:
    source = tmp_path / "original.pdf"
    source.write_bytes(b"original source")
    from provenance.evidence_backfill import ensure_legacy_document_evidence

    conn.execute(
        "INSERT INTO documents (id,ticker,source_type,doc_type,period_end,file_path,sha256,fetched_at,fetch_status,raw_bytes_size) "
        "VALUES (1,'TEST','ir_doc','ir_supplement','2026-06-30 00:00:00',?,?, '2026-07-01','ok',15)",
        (str(source), hashlib.sha256(source.read_bytes()).hexdigest()),
    )
    ensure_legacy_document_evidence(conn, repo_root=tmp_path, document_id=1)
    conn.commit()

    version = str(
        conn.execute(
            "SELECT document_version_id FROM evidence_document_versions WHERE legacy_document_id=1"
        ).fetchone()[0]
    )
    return source, version


@pytest.mark.parametrize("existing_aliases", [False, True])
@pytest.mark.parametrize("distinct", ["scope", "period"])
@pytest.mark.parametrize(
    "labels",
    [
        ("Margin", "Margin"),
        ("Margin", "margin"),
        ("Margin", "Margin (%)"),
        ("Margin percent", "Margin"),
    ],
)
def test_direct_capture_retains_distinct_candidates_without_legacy_projection(
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    distinct: str,
    labels: tuple[str, str],
    existing_aliases: bool,
) -> None:
    from datetime import datetime

    import compute.kpi_extract_summaries as summaries

    source, _ = _retained_source(conn, tmp_path)
    if existing_aliases:
        conn.executemany(
            "INSERT INTO kpi_definitions (ticker,name,unit,primary_source,definition_origin) "
            "VALUES ('TEST',?,'percent','ir_doc','capture')",
            [("Margin",), ("margin",)],
        )
        conn.commit()

    def list_sources(*args: object) -> list[tuple[int, Path, datetime]]:
        return [(1, source, datetime(2026, 6, 30))]

    def route(*args: object, **kwargs: object) -> tuple[str, str]:
        return ("fixture", "fixture")

    def page_texts(raw: bytes) -> tuple[str, ...]:
        return ("North margin 10%", "South margin 20%")

    def locator_factory(path: Path, *, raw_bytes: bytes | None = None) -> Callable[..., None]:
        def locate(quote: str | None, *, page_number: int | None = None) -> None:
            return None

        return locate

    monkeypatch.setattr(summaries, "_list_ir_capture_pdf_docs", list_sources)
    monkeypatch.setattr(summaries, "resolve_model_and_backend", route)
    monkeypatch.setattr(continuation, "project_pdf_page_texts", page_texts)
    monkeypatch.setattr("pipeline.locators.PdfQuoteLocator", locator_factory)

    def extract(ticker: str, period: str, text: str, **kwargs: object) -> list[dict[str, object]]:
        north = text.startswith("North")
        rows: list[dict[str, object]] = [
            {
                "label": labels[0 if north else 1],
                "value": 10 if north else 20,
                "unit": "percent",
                "source_excerpt": text,
                "source_value_text": "10%" if north else "20%",
                "reported_period_end": "2026-06-30"
                if north or distinct == "scope"
                else "2025-06-30",
                "period_role": "current"
                if north or distinct == "scope"
                else "prior_year_comparator",
                "accounting_basis": "management",
                "consolidation_scope": "geography",
                "dimensions": {"geography": "North" if north or distinct == "period" else "South"},
                "source_unit_scale": "none",
            }
        ]

        from datetime import UTC

        from llm.ledger import record_llm_call
        from llm.structured import StructuredCallResult

        prompt = getattr(summaries, "_enumerate_prompt")(
            ticker, period, text, 200, source_scope="direct_pdf_rich"
        )
        raw = json.dumps(rows)
        record_llm_call(
            started_at=datetime.now(UTC),
            elapsed_ms=0,
            model="fixture-actual",
            prompt_sha=hashlib.sha256(prompt.encode()).hexdigest(),
            prompt_chars=len(prompt),
            purpose="kpi_summary_enumerate",
            ticker=ticker,
            scope=None,
            run_id=str(kwargs["run_id"]),
            response_text=raw,
            provider="fixture",
            transport="fixture",
            outcome="success",
        )
        observer = cast(
            Callable[[StructuredCallResult[list[object]]], None], kwargs["exchange_observer"]
        )
        observer(StructuredCallResult(value=[], raw_response=raw, prompt=prompt))
        return rows

    monkeypatch.setattr(summaries, "_llm_extract_enumerate", extract)
    result = summaries.capture_for_ir_pdf_docs("TEST", tmp_path, conn)
    assert result.error is None
    assert result.quarters_extracted == []
    assert conn.execute("SELECT COUNT(*) FROM kpi_facts").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM kpi_definitions WHERE ticker='TEST'").fetchone()[
        0
    ] == (2 if existing_aliases else 0)
    assert conn.execute("SELECT COUNT(*) FROM llm_artifacts").fetchone()[0] == 4
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM llm_artifacts WHERE superseded_by_id IS NULL"
        ).fetchone()[0]
        == 2
    )
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM evidence_nodes WHERE node_id LIKE 'ir-pdf-candidate:%'"
        ).fetchone()[0]
        == 2
    )
    assert conn.execute("SELECT status FROM pipeline_attempts").fetchone()[0] == "ok"


def test_one_long_page_sections_retain_final_source_line(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    text = "Header USD millions\n" + "\n".join(
        f"line {item}: 10% " + "x" * 200 for item in range(420)
    )
    calls: list[str] = []

    def extract(section: str) -> list[dict[str, object]]:
        calls.append(section)
        body = section.split("EXTRACT ONLY FROM THIS SOURCE SECTION:\n")[-1]
        return [
            {"source_excerpt": line[:80]} for line in body.splitlines() if line.startswith("line ")
        ]

    output = _capture(conn, monkeypatch, (text,), extract, max_facts=300).capture(
        b"original source"
    )
    assert sum(len(rows) for _, rows in output) == 420
    assert str(output[-1][1][-1]["source_excerpt"]).startswith("line 419:")
    assert all(len(section) <= 80_000 for section in calls)
    assert all("Header USD millions" in section for section in calls)


def test_empty_processed_response_is_cached_without_fact_recall_claim(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    def extract(section: str) -> list[dict[str, object]]:
        calls.append(section)
        return []

    for _ in range(2):
        assert _capture(conn, monkeypatch, ("No reported values",), extract).capture(
            b"original source"
        ) == [(1, [])]
    assert len(calls) == 1
    payload = conn.execute(
        "SELECT content_json FROM llm_artifacts WHERE superseded_by_id IS NULL"
    ).fetchone()[0]
    assert "processed" in payload and "complete" not in payload


@pytest.mark.parametrize("case", ["ocr_required", "text_mismatch", "source_mismatch"])
def test_retained_native_page_assessment_blocks_known_unresolved_pages(
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    case: str,
) -> None:
    from provenance.ocr_extraction import normalize_extracted_text

    source, version = _retained_source(conn, tmp_path)
    native_text = "Report footer"
    source_sha = hashlib.sha256(source.read_bytes()).hexdigest()
    stored_sha = "b" * 64 if case == "source_mismatch" else source_sha
    outcome = "ocr_required" if case == "ocr_required" else "native_sufficient"
    text_sha = hashlib.sha256(normalize_extracted_text(native_text).encode()).hexdigest()
    if case == "text_mismatch":
        text_sha = "c" * 64
    assessment_sql = (
        "INSERT INTO ocr_document_assessments "
        "(assessment_id,idempotency_key,document_version_id,input_sha256,detector_name,detector_config_sha256,detector_code_version,native_output_sha256,page_count,outcome,reason_code,assessed_at) "
        "VALUES ('assessment','assessment',?,?,'fixture',?,'fixture',?,1,?,NULL,'2026-10-22')"
    )
    assessment_inputs = (version, stored_sha, "a" * 64, "d" * 64, outcome)
    if case == "source_mismatch":
        # The canonical schema rejects this mismatch before the adapter can read it.
        with pytest.raises(sqlite3.IntegrityError, match="input must match document bytes"):
            conn.execute(assessment_sql, assessment_inputs)
        conn.rollback()
        assert conn.execute("SELECT COUNT(*) FROM llm_artifacts").fetchone()[0] == 0
        return
    conn.execute(assessment_sql, assessment_inputs)
    conn.execute(
        "INSERT INTO ocr_preflight_pages "
        "(assessment_id,page_number,native_character_count,native_text_sha256,requires_ocr) "
        "VALUES ('assessment',1,12,?,?)",
        (text_sha, int(case == "ocr_required")),
    )
    conn.commit()

    def reject_call(text: str) -> list[dict[str, object]]:
        pytest.fail("retained unresolved assessment must block model work")

    capture = _capture(conn, monkeypatch, (native_text,), reject_call)
    capture.document_version_id = version
    with pytest.raises(ValueError, match="existing native-page"):
        capture.capture(source.read_bytes())
    assert conn.execute("SELECT COUNT(*) FROM llm_artifacts").fetchone()[0] == 0


def test_financial_manifest_late_failure_rolls_back_partial_write(
    conn: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import datetime

    import compute.kpi_extract_summaries as summaries
    from models.documents import SourceType
    from models.facts import FiscalPeriodType
    from pipeline.kpi_persistence import persist_manifest as real_persist

    _retained_source(conn, tmp_path)
    build = getattr(summaries, "_build_capture_manifest")
    manifest = build(
        "TEST",
        datetime(2026, 6, 30),
        FiscalPeriodType.Q2,
        1,
        [{"label": "Margin", "value": 10, "unit": "percent", "source_excerpt": "10%"}],
        primary_source=SourceType.IR_DOC,
    )

    def fail_after_write(*args: object, **kwargs: object) -> object:
        real_persist(conn, run_id=str(kwargs["run_id"]), manifest=manifest, commit=False)
        assert conn.execute("SELECT COUNT(*) FROM kpi_facts").fetchone()[0] == 1
        raise ValueError("late persistence failure")

    monkeypatch.setattr(summaries, "persist_manifest", fail_after_write)
    persist = getattr(summaries, "_persist_accounted")
    with pytest.raises(ValueError, match="late persistence failure"):
        persist(
            conn,
            directive="test_atomic_manifest",
            ticker="TEST",
            invocation_inputs={},
            manifest=manifest,
        )
    assert conn.execute("SELECT COUNT(*) FROM kpi_facts").fetchone()[0] == 0
    assert (
        conn.execute("SELECT COUNT(*) FROM kpi_definitions WHERE ticker='TEST'").fetchone()[0] == 0
    )
    assert conn.execute("SELECT status FROM pipeline_attempts").fetchone()[0] == "failed"


def test_financial_persistence_rejects_unrelated_open_transaction(
    conn: sqlite3.Connection,
) -> None:
    from datetime import datetime

    import compute.kpi_extract_summaries as summaries
    from models.facts import FiscalPeriodType

    build = getattr(summaries, "_build_capture_manifest")
    manifest = build("TEST", datetime(2026, 6, 30), FiscalPeriodType.Q2, 1, [])
    conn.execute("CREATE TABLE owner_work (value TEXT)")
    conn.commit()
    conn.execute("INSERT INTO owner_work VALUES ('uncommitted')")
    with pytest.raises(ValueError, match="owned transaction boundary"):
        getattr(summaries, "_persist_accounted")(
            conn,
            directive="test_atomic_manifest",
            ticker="TEST",
            invocation_inputs={},
            manifest=manifest,
        )
    assert conn.in_transaction
    assert conn.execute("SELECT value FROM owner_work").fetchone()[0] == "uncommitted"
    assert conn.execute("SELECT COUNT(*) FROM pipeline_attempts").fetchone()[0] == 0
    conn.rollback()
    assert conn.execute("SELECT COUNT(*) FROM owner_work").fetchone()[0] == 0


def test_pinned_byte_locator_ignores_replacement_pdf(tmp_path: Path) -> None:
    import fitz

    from pipeline.locators import PdfQuoteLocator

    source = tmp_path / "source.pdf"
    original = fitz.open()
    original.new_page().insert_text((72, 72), "Margin 10%")
    original.save(source)
    original.close()
    raw = source.read_bytes()
    locator = PdfQuoteLocator(source, raw_bytes=raw)
    replacement = fitz.open()
    replacement.new_page().insert_text((72, 72), "Replacement introduction")
    replacement.new_page().insert_text((72, 72), "Margin 10%")
    source.write_bytes(replacement.tobytes())
    replacement.close()
    located = locator("Margin 10%")
    assert located is not None and located.pdf_page == 1
    assert located.pdf_bbox is not None


@pytest.mark.parametrize("replace_after_capture", [False, True])
def test_live_prompt_override_repair_and_fallback_metadata_bind_cache(
    conn: sqlite3.Connection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    replace_after_capture: bool,
) -> None:
    from datetime import UTC, datetime

    import compute.kpi_extract_summaries as summaries
    import llm.structured as structured
    from llm.ledger import record_llm_call
    from llm.prompt_ab import PromptEdit, apply_prompt_override, write_prompt_override

    source, _ = _retained_source(conn, tmp_path)
    original_raw = source.read_bytes()

    def sources(*args: object) -> list[tuple[int, Path, datetime]]:
        return [(1, source, datetime(2026, 6, 30))]

    def route(*args: object, **kwargs: object) -> tuple[str, str]:
        return ("requested-primary", "requested-backend")

    def pages(raw: bytes) -> tuple[str, ...]:
        return ("Margin 10%",)

    monkeypatch.setattr(summaries, "_list_ir_capture_pdf_docs", sources)
    monkeypatch.setattr(summaries, "resolve_model_and_backend", route)
    monkeypatch.setattr(continuation, "project_pdf_page_texts", pages)
    if replace_after_capture:
        original_capture = continuation.SupplementCapture.capture

        def capture_then_replace(
            self: continuation.SupplementCapture, raw_bytes: bytes
        ) -> list[tuple[int, list[dict[str, object]]]]:
            assert raw_bytes == original_raw
            result = original_capture(self, raw_bytes)
            source.write_bytes(b"replacement after extraction")
            return result

        monkeypatch.setattr(continuation.SupplementCapture, "capture", capture_then_replace)
    rows = [
        {
            "label": "Margin",
            "value": 10,
            "unit": "percent",
            "source_excerpt": "Margin 10%",
            "source_value_text": "10%",
            "reported_period_end": "2026-06-30",
            "period_role": "current",
            "accounting_basis": "gaap",
            "consolidation_scope": "consolidated",
            "source_unit_scale": "none",
        }
    ]
    calls: list[tuple[str, str]] = []

    def governed_call(prompt: str, **kwargs: object) -> str:
        # This fixture replaces only provider dispatch. Keep the governed hook
        # and canonical ledger to prove cache identity and final repair lineage.
        assert "EXPERIMENT_A" not in prompt and "EXPERIMENT_B" not in prompt
        effective = apply_prompt_override("kpi_summary_enumerate", None, prompt)
        assert effective.count("EXPERIMENT_") == 1
        repair = prompt.startswith("IMPORTANT:")
        raw = json.dumps(rows) if repair else "not valid JSON"
        actual_model = "actual-fallback" if repair else "actual-primary"
        calls.append((prompt, effective))
        record_llm_call(
            started_at=datetime.now(UTC),
            elapsed_ms=0,
            model=actual_model,
            prompt_sha=hashlib.sha256(effective.encode()).hexdigest(),
            prompt_chars=len(effective),
            purpose="kpi_summary_enumerate",
            ticker="TEST",
            scope=None,
            run_id=str(kwargs["run_id"]),
            response_text=raw,
            provider="fallback-provider" if repair else "primary-provider",
            transport="fallback-transport" if repair else "primary-transport",
            outcome="success",
        )
        return raw

    monkeypatch.setattr(structured, "call_llm", governed_call)
    db = Path(conn.execute("PRAGMA database_list").fetchone()[2])

    def override(marker: str) -> None:
        write_prompt_override(
            "kpi_summary_enumerate",
            (PromptEdit(find="There is NO list", replace=marker + " There is NO list"),),
            experiment_id="fixture-" + marker,
            db_path=db,
        )

    override("EXPERIMENT_A")
    first = summaries.capture_for_ir_pdf_docs("TEST", tmp_path, conn)
    if replace_after_capture:
        assert first.error and "changed" in first.error
        assert first.kpis_inserted_total == 0
        assert conn.execute("SELECT COUNT(*) FROM kpi_facts").fetchone()[0] == 0
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM evidence_nodes WHERE node_id LIKE 'ir-pdf-candidate:%'"
            ).fetchone()[0]
            == 0
        )
        assert conn.execute("SELECT COUNT(*) FROM llm_artifacts").fetchone()[0] == 3
        return
    assert first.error is None and first.kpis_inserted_total == 0
    assert conn.execute("SELECT COUNT(*) FROM kpi_facts").fetchone()[0] == 0
    assert len(calls) == 2
    second = summaries.capture_for_ir_pdf_docs("TEST", tmp_path, conn)
    assert second.error is None and len(calls) == 2
    override("EXPERIMENT_B")
    third = summaries.capture_for_ir_pdf_docs("TEST", tmp_path, conn)
    assert third.error is None and len(calls) == 4
    artifact = conn.execute(
        "SELECT model,llm_call_id,content_json FROM llm_artifacts WHERE superseded_by_id IS NULL"
    ).fetchone()
    payload = json.loads(artifact["content_json"])
    exchange = payload["exchange"]
    assert artifact["model"] == exchange["model"] == "actual-fallback"
    assert (
        exchange["provider"] == "fallback-provider"
        and exchange["transport"] == "fallback-transport"
    )
    assert exchange["submitted_prompt"] == continuation.repair_supplement_prompt(calls[2][0])
    assert exchange["prompt_sha256"] == hashlib.sha256(calls[3][1].encode()).hexdigest()
    assert artifact["llm_call_id"] == exchange["llm_call_id"]
    assert (
        conn.execute(
            "SELECT prompt_sha256 FROM llm_calls WHERE id=?", (artifact["llm_call_id"],)
        ).fetchone()[0]
        == exchange["prompt_sha256"]
    )
    conn.execute("UPDATE llm_artifacts SET model='misattributed' WHERE superseded_by_id IS NULL")
    conn.commit()
    rejected = summaries.capture_for_ir_pdf_docs("TEST", tmp_path, conn)
    assert rejected.error and "attribution mismatch" in rejected.error
    assert len(calls) == 4


@pytest.mark.parametrize("capture_name", ["capture_for_ticker", "capture_for_ir_pdf_docs"])
def test_capture_rejects_caller_transaction_before_source_preparation(
    conn: sqlite3.Connection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capture_name: str,
) -> None:
    import compute.kpi_extract_summaries as summaries

    def no_sources(*args: object, **kwargs: object) -> object:
        pytest.fail("caller transaction must be rejected before source preparation")

    monkeypatch.setattr(summaries, "_list_sources", no_sources)
    monkeypatch.setattr(summaries, "_list_ir_capture_pdf_docs", no_sources)
    conn.execute("CREATE TABLE owner_work (value TEXT)")
    conn.commit()
    conn.execute("INSERT INTO owner_work VALUES ('uncommitted')")
    result = getattr(summaries, capture_name)("TEST", tmp_path, conn)
    assert result.error and "before source preparation" in result.error
    assert conn.in_transaction
    assert conn.execute("SELECT value FROM owner_work").fetchone()[0] == "uncommitted"
    assert conn.execute("SELECT COUNT(*) FROM pipeline_attempts").fetchone()[0] == 0
    conn.rollback()
    assert conn.execute("SELECT COUNT(*) FROM owner_work").fetchone()[0] == 0
