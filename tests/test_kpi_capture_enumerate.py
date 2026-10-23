"""Tests for the Stage-B capture-every-number enumerate mode in
compute.kpi_extract_summaries — the no-allowlist path that recovers the numbers
the tier_1 allowlist drops.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from functools import partial
from pathlib import Path
from typing import cast

import pytest
from pydantic import TypeAdapter, ValidationError

import compute.kpi_extract_summaries as kes
from compute.kpi_extract_summaries import (
    capture_for_ir_pdf_docs,
    capture_for_ticker,
)
from models.facts import FiscalPeriodType, Unit
from models.kpis import DefinitionOrigin
from models.runs import StageStatus
from pipeline.kpi_persistence import KpiExtractionManifest
from pipeline.run_accounting import PipelineRunSuppressedError

_build_capture_manifest = cast(
    Callable[..., KpiExtractionManifest], getattr(kes, "_build_capture_manifest")
)
_fiscal_period_type_for = cast(
    Callable[[str, datetime], FiscalPeriodType], getattr(kes, "_fiscal_period_type_for")
)
_llm_extract_enumerate = cast(
    Callable[..., list[dict[str, object]]], getattr(kes, "_llm_extract_enumerate")
)


def _const_caller(response: str) -> Callable[..., object]:
    """A typed stand-in for the governed structured call."""

    def _call(prompt: str, **_: object) -> object:
        del prompt
        cleaned = response.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("\n", 1)[1].rsplit("```", 1)[0]
        return json.loads(cleaned)

    return _call


# ---------------------------------------------------------------------------
# _llm_extract_enumerate — the no-allowlist prompt + parser
# ---------------------------------------------------------------------------

_ENUMERATE_JSON = (
    "[\n"
    '  {"label": "Revenue", "value": 1200000000, "unit": "actual", "currency": "USD", "source_excerpt": "Revenue was $1.2 billion", "source_value_text": "1.2", "reported_period_end": "2025-03-31", "period_role": "current", "accounting_basis": "gaap", "consolidation_scope": "consolidated", "source_unit_scale": "none"},\n'
    '  {"label": "Net interest margin", "value": 17.8, "unit": "percent", "source_excerpt": "NIM of 17.8%", "source_value_text": "17.8%", "reported_period_end": "2025-03-31", "period_role": "current", "accounting_basis": "management", "consolidation_scope": "consolidated", "source_unit_scale": "none"},\n'
    '  {"label": "Customers", "value": 118000000, "unit": "count", "source_excerpt": "118 million customers", "source_value_text": "118", "reported_period_end": "2025-03-31", "period_role": "current", "accounting_basis": "management", "consolidation_scope": "consolidated", "source_unit_scale": "millions"}\n'
    "]"
)


def test_enumerate_prompt_has_no_allowlist_and_valid_units(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, str] = {}

    def fake_call(prompt: str, **_: object) -> object:
        captured["prompt"] = prompt
        return json.loads(_ENUMERATE_JSON)

    monkeypatch.setattr(kes, "call_llm_structured", fake_call)
    rows = _llm_extract_enumerate("NU", "Q1 2025 [ir]", "Revenue was $1.2 billion. NIM 17.8%.")

    prompt = captured["prompt"]
    # It explicitly tells the model there is NO target list.
    assert "no list of metrics" in prompt.lower() or "no allowlist" in prompt.lower()
    # Every offered unit token is a real Unit enum value.
    for token in ('"percent"', '"actual"', '"count"', '"ratio"', '"bps"'):
        assert token in prompt
        assert token.strip('"') in {u.value for u in Unit}
    assert len(rows) == 3
    assert rows[0]["label"] == "Revenue"


def test_enumerate_strips_fence_without_truncating(monkeypatch: pytest.MonkeyPatch) -> None:
    fenced = "```json\n" + _ENUMERATE_JSON + "\n```"
    monkeypatch.setattr(kes, "call_llm_structured", _const_caller(fenced))
    rows = _llm_extract_enumerate("NU", "Q1 2025", "text", max_facts=4)
    assert len(rows) == 3


@pytest.mark.parametrize("max_facts", [2, 3])
def test_enumerate_rejects_truncated_or_saturated_fact_output(
    monkeypatch: pytest.MonkeyPatch, max_facts: int
) -> None:
    monkeypatch.setattr(kes, "call_llm_structured", _const_caller(_ENUMERATE_JSON))
    with pytest.raises(ValueError, match="fact limit"):
        _llm_extract_enumerate("NU", "Q1 2025", "text", max_facts=max_facts)


def test_enumerate_rejects_non_array_but_blank_input_short_circuits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(kes, "call_llm_structured", _const_caller('{"not": "an array"}'))
    with pytest.raises(ValidationError, match="valid list"):
        _llm_extract_enumerate("NU", "Q1 2025", "text")
    # Blank input short-circuits without even calling the model.
    calls: list[int] = []

    def _counting(prompt: str, **_: object) -> list[object]:
        calls.append(1)
        return []

    monkeypatch.setattr(kes, "call_llm_structured", _counting)
    assert _llm_extract_enumerate("NU", "Q1 2025", "   ") == []
    assert calls == []


def test_enumerate_rejects_malformed_items(monkeypatch: pytest.MonkeyPatch) -> None:
    bad = '[{"label": "Revenue", "value": 5, "unit": "actual", "currency": "USD"}, {"value": 9}, "junk", {"label": "X"}]'
    monkeypatch.setattr(kes, "call_llm_structured", _const_caller(bad))
    with pytest.raises(ValidationError, match="source_excerpt"):
        _llm_extract_enumerate("NU", "Q1 2025", "text")


# ---------------------------------------------------------------------------
# _build_capture_manifest
# ---------------------------------------------------------------------------


def test_build_capture_manifest_is_capture_origin_and_parses_rows() -> None:
    rows: list[dict[str, object]] = [
        {
            "label": "Revenue",
            "value": 1200000000,
            "unit": "actual",
            "currency": "USD",
            "source_excerpt": "x",
        },
        {"label": "Bad value", "value": "N/A", "unit": "actual"},  # unparseable -> skipped
        {"label": "", "value": 5, "unit": "actual"},  # blank label -> skipped
        {
            "label": "Take rate",
            "value": 1.5,
            "unit": "bogus",
            "currency": "USD",
        },  # bad unit -> ACTUAL fallback
    ]
    m = _build_capture_manifest("NU", datetime(2025, 3, 31), FiscalPeriodType.Q1, 42, rows)
    assert m.origin is DefinitionOrigin.CAPTURE
    assert m.source_doc_id == 42
    names = {v.name: v for v in m.values}
    assert set(names) == {"Revenue", "Take rate"}
    assert names["Revenue"].value == Decimal("1200000000")
    assert names["Take rate"].unit is Unit.ACTUAL  # bad token fell back


def test_build_capture_manifest_verifies_excerpt_against_source_text() -> None:
    """Phase C (§3.3): passing `source_text` upgrades a verbatim excerpt to a
    real `html_span` locator; without it (today's default), the escape hatch
    is unchanged."""
    from models.facts import FactLocator, LegacyEscapeHatch, LocatorKind

    rows: list[dict[str, object]] = [
        {
            "label": "GMV",
            "value": 1200000000,
            "unit": "actual",
            "currency": "USD",
            "source_excerpt": "GMV was $1.2B",
        },
    ]
    no_text = _build_capture_manifest("NU", datetime(2025, 3, 31), FiscalPeriodType.Q1, 42, rows)
    assert isinstance(no_text.values[0].locator, LegacyEscapeHatch)

    with_text = _build_capture_manifest(
        "NU",
        datetime(2025, 3, 31),
        FiscalPeriodType.Q1,
        42,
        rows,
        source_text="Key Business Metrics\nGMV was $1.2B this quarter.\n",
    )
    loc = with_text.values[0].locator
    assert isinstance(loc, FactLocator)
    assert loc.effective_kind() == LocatorKind.HTML_SPAN
    assert loc.html_span is not None and loc.html_span.doc_id == 42


# ---------------------------------------------------------------------------
# capture_for_ticker — end-to-end against a file DB + a .tmp brief
# ---------------------------------------------------------------------------

_PROVENANCE_UNIQUE = (
    "CREATE UNIQUE INDEX uq_kpi_facts_provenance "
    "ON kpi_facts (ticker, period_end, fiscal_period_type, kpi_definition_id, source_doc_id)"
)


def _make_capture_db(db_path: Path) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE kpi_definitions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL, name TEXT NOT NULL, unit TEXT NOT NULL,
            primary_source TEXT NOT NULL, fallback_source TEXT, ir_url TEXT,
            threshold_tier TEXT, threshold_low FLOAT, threshold_high FLOAT, notes TEXT,
            definition_origin TEXT NOT NULL DEFAULT 'analyst',
            UNIQUE(ticker, name)
        );
        CREATE TABLE kpi_facts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL, period_end TIMESTAMP NOT NULL,
            fiscal_period_type TEXT NOT NULL, kpi_definition_id INTEGER NOT NULL,
            value NUMERIC(24, 6) NOT NULL, unit TEXT NOT NULL, source_doc_id INTEGER NOT NULL,
            confidence FLOAT NOT NULL DEFAULT 1.0, extracted_by TEXT,
            locator TEXT, source_excerpt TEXT, supersedes_id INTEGER
        );
        CREATE TABLE kpi_fact_semantic_contexts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kpi_fact_id INTEGER NOT NULL,
            revision INTEGER NOT NULL,
            supersedes_context_id INTEGER,
            metric_name_as_reported TEXT NOT NULL,
            reported_period_end TEXT,
            period_role TEXT NOT NULL,
            publication_lane TEXT NOT NULL,
            accounting_basis TEXT NOT NULL,
            consolidation_scope TEXT NOT NULL,
            dimensions_json TEXT NOT NULL,
            unit_scale TEXT NOT NULL,
            source_row_label TEXT,
            source_column_header TEXT,
            source_value_text TEXT,
            status TEXT NOT NULL,
            reason_code TEXT,
            reviewed_by TEXT NOT NULL,
            knowledge_at TEXT NOT NULL,
            UNIQUE(kpi_fact_id, revision)
        );
        CREATE TABLE validation_issues (
            id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
            source_doc_id INTEGER, ticker TEXT, severity TEXT NOT NULL, rule TEXT NOT NULL,
            raw_value TEXT, expected TEXT, raised_at TIMESTAMP NOT NULL, resolved_at TIMESTAMP
        );
        CREATE TABLE ingestion_runs (
            run_id TEXT PRIMARY KEY, started_at TIMESTAMP, ended_at TIMESTAMP,
            directive TEXT, ticker_scope TEXT, status TEXT, error_summary TEXT
        );
        CREATE TABLE documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ticker TEXT, source_type TEXT, doc_type TEXT,
            period_end TIMESTAMP, file_path TEXT, sha256 TEXT, fetched_at TIMESTAMP,
            fetch_status TEXT, raw_bytes_size INTEGER, parent_document_id INTEGER
        );
        """
    )
    conn.execute(_PROVENANCE_UNIQUE)
    conn.commit()
    conn.close()


def _write_brief(repo_root: Path) -> None:
    tmp = repo_root / ".tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    (tmp / "NU_Q1_2025_press_release_summary.txt").write_text(
        "Revenue was $1.2 billion. NIM of 17.8%. We ended with 118 million customers.",
        encoding="utf-8",
    )


def test_capture_for_ticker_persists_capture_facts_and_canonicalizes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "data" / "portfolio.db"
    db.parent.mkdir(parents=True)
    _make_capture_db(db)
    _write_brief(tmp_path)

    # Seed an analyst "Revenue" definition — the enumerated "Revenue" must
    # canonicalize onto it (persist origin=CAPTURE), not mint a duplicate.
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    conn.execute(
        "INSERT INTO kpi_definitions (ticker, name, unit, primary_source, definition_origin) "
        "VALUES ('NU', 'Revenue', 'actual', 'llm_extracted', 'analyst')"
    )
    conn.commit()

    calls: list[int] = []

    def fake_call(prompt: str, **_: object) -> object:
        calls.append(1)
        return json.loads(_ENUMERATE_JSON)

    monkeypatch.setattr(kes, "call_llm_structured", fake_call)

    log = capture_for_ticker("NU", tmp_path, conn, source_group="ir")
    assert log.kpis_inserted_total == 3
    assert len(calls) == 1

    defs = {
        str(r["name"]): str(r["definition_origin"])
        for r in conn.execute("SELECT name, definition_origin FROM kpi_definitions").fetchall()
    }
    # "Revenue" stayed analyst (captured value collapsed onto it); the two new
    # metrics minted as capture.
    assert defs == {
        "Revenue": "analyst",
        "Net interest margin": "capture",
        "Customers": "capture",
    }
    # Facts carry provenance back to the registered brief doc.
    fact_count = conn.execute("SELECT COUNT(*) FROM kpi_facts").fetchone()[0]
    assert fact_count == 3
    customer = conn.execute(
        "SELECT f.value,c.unit_scale,c.source_value_text,f.source_excerpt "
        "FROM kpi_facts f JOIN kpi_definitions d ON d.id=f.kpi_definition_id "
        "JOIN kpi_fact_semantic_contexts c ON c.kpi_fact_id=f.id "
        "WHERE d.name='Customers'"
    ).fetchone()
    assert Decimal(str(customer["value"])) == Decimal("118000000")
    assert tuple(customer)[1:] == ("millions", "118", "118 million customers")
    assert [
        row[0]
        for row in conn.execute(
            "SELECT status FROM ingestion_runs WHERE directive='capture_kpis_from_ir'"
        ).fetchall()
    ] == ["ok"]

    # PR3: a Stage-B coverage record was written (3 seen, 3 captured).
    from pipeline.capture_coverage import load_coverage

    cov = load_coverage(tmp_path)
    assert len(cov) == 1
    assert cov[0].stage == "enumerate_capture"
    assert cov[0].source == "ir"
    assert cov[0].seen == 3
    assert cov[0].captured == 3

    # Idempotent: a second run skips the doc without another LLM call.
    log2 = capture_for_ticker("NU", tmp_path, conn, source_group="ir")
    assert log2.kpis_inserted_total == 0
    assert len(calls) == 1  # no new model call
    assert conn.execute("SELECT COUNT(*) FROM kpi_facts").fetchone()[0] == 3
    # No second coverage record (nothing was seen on the skipped re-run).
    assert len(load_coverage(tmp_path)) == 1
    conn.close()


# An enumerate batch where one ACTUAL value is an absurd magnitude — a share count
# the LLM emitted as a whole-dollar figure (e.g. a x1e6 mis-read). The persist-time
# ACTUAL magnitude cap must drop it while the sane Revenue fact lands.
_ABSURD_ENUMERATE_JSON = (
    "[\n"
    '  {"label": "Revenue", "value": 1200000000, "unit": "actual", "currency": "USD", "source_excerpt": "x", "reported_period_end": "2025-03-31", "period_role": "current", "accounting_basis": "gaap", "consolidation_scope": "consolidated", "source_unit_scale": "none"},\n'
    '  {"label": "Treasury stock", "value": 4609988545000000, "unit": "actual", "currency": "USD", "source_excerpt": "y", "reported_period_end": "2025-03-31", "period_role": "current", "accounting_basis": "gaap", "consolidation_scope": "consolidated", "source_unit_scale": "none"}\n'
    "]"
)


def test_capture_for_ticker_drops_absurd_actual_magnitude(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The persist-time ACTUAL magnitude cap protects the Stage-B enumerate path
    too: an absurd whole-dollar value is dropped-with-log, the sane fact stored,
    and the coverage record reflects the drop (never silent)."""
    db = tmp_path / "data" / "portfolio.db"
    db.parent.mkdir(parents=True)
    _make_capture_db(db)
    _write_brief(tmp_path)
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row

    monkeypatch.setattr(kes, "call_llm_structured", _const_caller(_ABSURD_ENUMERATE_JSON))

    log = capture_for_ticker("NU", tmp_path, conn, source_group="ir")
    assert log.kpis_inserted_total == 1  # only the sane Revenue fact
    assert conn.execute("SELECT COUNT(*) FROM kpi_facts").fetchone()[0] == 1

    from pipeline.capture_coverage import load_coverage

    cov = load_coverage(tmp_path)
    assert len(cov) == 1
    assert cov[0].seen == 2
    assert cov[0].captured == 1
    assert cov[0].dropped_validation == 1  # the absurd value, accounted for
    conn.close()


def test_capture_surfaces_accounting_suppression(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "data" / "portfolio.db"
    db.parent.mkdir(parents=True)
    _make_capture_db(db)
    _write_brief(tmp_path)
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    monkeypatch.setattr(kes, "call_llm_structured", _const_caller(_ENUMERATE_JSON))
    suppressed = PipelineRunSuppressedError(
        pipeline_key="pipeline_test",
        attempt_id="attempt_live",
        status=StageStatus.IN_PROGRESS,
    )

    captured: dict[str, object] = {}

    def suppress(*_: object, **kwargs: object) -> str:
        captured.update(kwargs)
        raise suppressed

    monkeypatch.setattr(kes, "start_run", suppress)
    with pytest.raises(PipelineRunSuppressedError) as exc_info:
        capture_for_ticker("NU", tmp_path, conn, source_group="ir", refresh=True)
    assert exc_info.value is suppressed
    inputs_obj = captured["invocation_inputs"]
    assert isinstance(inputs_obj, dict)
    inputs = cast(dict[str, object], inputs_obj)
    source_obj = inputs["source"]
    assert isinstance(source_obj, dict)
    source = cast(dict[str, object], source_obj)
    assert isinstance(source["sha256"], str)
    assert inputs["source_group"] == "ir"
    assert inputs["max_facts_per_doc"] == cast(int, getattr(kes, "_CAPTURE_MAX_FACTS"))
    assert captured["deduplicate_completed"] is True
    assert captured["force"] is True
    conn.close()


# ---------------------------------------------------------------------------
# capture_for_ir_pdf_docs — the ir_supplement PDF capture path
# ---------------------------------------------------------------------------


def test_fiscal_period_type_for_calendar_and_jan_fye() -> None:
    # Calendar-FY issuer: quarter derived straight from the period-end month.
    assert _fiscal_period_type_for("NU", datetime(2025, 3, 31)) is FiscalPeriodType.Q1
    assert _fiscal_period_type_for("NU", datetime(2025, 12, 31)) is FiscalPeriodType.Q4
    # Jan-FYE issuer (RBRK): Jan-31 is fiscal Q4, NOT calendar Q1 — the override
    # map is reversed so the supplement lands on the same axis as the analyst series.
    assert _fiscal_period_type_for("RBRK", datetime(2026, 1, 31)) is FiscalPeriodType.Q4
    assert _fiscal_period_type_for("RBRK", datetime(2025, 4, 30)) is FiscalPeriodType.Q1
    # Oct-FYE issuer (AMAT): Oct-31 is fiscal Q4, Jan-31 is fiscal Q1 (not Q4,
    # despite sharing January with Jan-FYE's Q4 above) — the reverse-lookup
    # only matches on (month, day), so it's unaffected by the two conventions'
    # different year_offset handling.
    assert _fiscal_period_type_for("AMAT", datetime(2025, 10, 31)) is FiscalPeriodType.Q4
    assert _fiscal_period_type_for("AMAT", datetime(2026, 1, 31)) is FiscalPeriodType.Q1


def _governed_fixture_call(
    prompt: str, *, response: Callable[..., object], **kwargs: object
) -> object:
    from llm.ledger import record_llm_call
    from llm.prompt_ab import apply_prompt_override
    from llm.structured import StructuredCallResult

    payload = response(prompt, **kwargs)
    raw = json.dumps(payload)
    effective = apply_prompt_override("kpi_summary_enumerate", None, prompt)
    record_llm_call(
        started_at=datetime.now(UTC),
        elapsed_ms=0,
        model="fixture-actual",
        prompt_sha=hashlib.sha256(effective.encode()).hexdigest(),
        prompt_chars=len(effective),
        purpose="kpi_summary_enumerate",
        ticker=None,
        scope=None,
        run_id=str(kwargs["run_id"]),
        response_text=raw,
        provider="fixture",
        transport="fixture",
        outcome="success",
    )
    schema = cast(TypeAdapter[object], kwargs["schema"])
    return StructuredCallResult(
        value=schema.validate_python(payload), raw_response=raw, prompt=prompt
    )


def _fixture_model_route(*args: object, **kwargs: object) -> tuple[str, str]:
    return ("fixture", "fixture")


def _register_supplement_pdf(
    conn: sqlite3.Connection,
    root: Path,
    ticker: str,
    period_end_iso: str,
    sha8: str,
    doc_type: str = "ir_supplement",
) -> Path:
    """Create an on-disk supplement PDF + its `documents` row; return the path."""
    pdf = root / "ir_documents" / ticker / period_end_iso / f"ir_supplement__{sha8}.pdf"
    pdf.parent.mkdir(parents=True, exist_ok=True)
    pdf.write_bytes(f"%PDF-1.4\n% dummy supplement {sha8}\n".encode())
    conn.execute(
        "INSERT INTO documents (ticker, source_type, doc_type, period_end, file_path, "
        "sha256, fetched_at, fetch_status, raw_bytes_size) "
        "VALUES (?, 'ir_doc', ?, ?, ?, ?, ?, 'ok', ?)",
        (
            ticker,
            doc_type,
            f"{period_end_iso} 00:00:00",
            str(pdf).replace("\\", "/"),
            hashlib.sha256(pdf.read_bytes()).hexdigest(),
            "2026-02-15 00:00:00",
            pdf.stat().st_size,
        ),
    )
    from provenance.evidence_backfill import ensure_legacy_document_evidence

    document_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
    if conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name='evidence_document_versions'"
    ).fetchone():
        ensure_legacy_document_evidence(conn, repo_root=root, document_id=document_id)
    conn.commit()
    return pdf


def test_capture_for_ir_pdf_docs_supplement_long_tail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, migrated_db: Callable[..., Path]
) -> None:
    """Direct PDF candidates remain source evidence until exact manual review."""
    db = tmp_path / "data" / "portfolio.db"
    db.parent.mkdir(parents=True)
    migrated_db(db)
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row

    seeded_names = {
        str(row[0]) for row in conn.execute("SELECT name FROM kpi_definitions WHERE ticker='RBRK'")
    }
    # Seed an analyst "Revenue" series at an earlier fiscal year-end (Q4).
    conn.execute(
        "INSERT INTO kpi_definitions (ticker, name, unit, primary_source, definition_origin) "
        "VALUES ('RBRK', 'Revenue', 'actual', 'ir_doc', 'analyst')"
    )
    rev_def_id = int(
        conn.execute(
            "SELECT id FROM kpi_definitions WHERE ticker='RBRK' AND name='Revenue'"
        ).fetchone()["id"]
    )
    _register_supplement_pdf(
        conn, tmp_path, "RBRK", "2025-01-31", "prior", doc_type="prior_fixture"
    )
    prior_id = int(conn.execute("SELECT MAX(id) FROM documents").fetchone()[0])
    conn.execute(
        "INSERT INTO kpi_facts (ticker, period_end, fiscal_period_type, kpi_definition_id, "
        "value, unit, source_doc_id, confidence, extracted_by) "
        "VALUES ('RBRK', '2025-01-31 00:00:00', 'Q4', ?, '500000000', 'actual', ?, 1.0, 'llm:x')",
        (rev_def_id, prior_id),
    )
    conn.commit()

    # A supplement PDF for the next fiscal year-end (2026-01-31 = fiscal Q4).
    _register_supplement_pdf(conn, tmp_path, "RBRK", "2026-01-31", "cbe9c445")

    def fake_pdf_text(path: str) -> str:
        return "Revenue was $1.2 billion. NIM of 17.8%. We ended with 118 million customers."

    def native_pages(raw: bytes) -> tuple[str, ...]:
        return (fake_pdf_text("fixture"),)

    monkeypatch.setattr("compute.ir_supplement_capture.project_pdf_page_texts", native_pages)
    monkeypatch.setattr(kes, "resolve_model_and_backend", _fixture_model_route)
    calls: list[int] = []

    def fake_call(prompt: str, **_: object) -> object:
        calls.append(1)
        return json.loads(_ENUMERATE_JSON)

    monkeypatch.setattr(
        kes, "call_llm_structured_with_raw", partial(_governed_fixture_call, response=fake_call)
    )

    log = capture_for_ir_pdf_docs("RBRK", tmp_path, conn)
    assert log.error is None and log.kpis_inserted_total == 0
    assert log.quarters_extracted == []
    assert len(calls) == 1

    # The analyst series is unchanged. Unreviewed long-tail labels cannot create
    # a definition or a fact in the legacy financial store.
    defs = {
        str(r["name"]): str(r["definition_origin"])
        for r in conn.execute(
            "SELECT name, definition_origin FROM kpi_definitions WHERE ticker='RBRK'"
        ).fetchall()
        if str(r["name"]) not in seeded_names
    }
    assert defs == {"Revenue": "analyst"}

    # The pre-existing analyst fact is byte-for-byte untouched.
    rev_prior = conn.execute(
        "SELECT value FROM kpi_facts WHERE kpi_definition_id=? AND period_end='2025-01-31 00:00:00'",
        (rev_def_id,),
    ).fetchone()
    assert Decimal(str(rev_prior["value"])) == Decimal("500000000")

    # The pre-existing analyst series does not gain an unreviewed fiscal Q4 row.
    rev_facts = conn.execute(
        "SELECT period_end, fiscal_period_type FROM kpi_facts "
        "WHERE kpi_definition_id=? ORDER BY period_end",
        (rev_def_id,),
    ).fetchall()
    assert len(rev_facts) == 1
    assert str(rev_facts[-1]["period_end"]).startswith("2025-01-31")
    assert str(rev_facts[-1]["fiscal_period_type"]) == "Q4"
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM evidence_nodes WHERE node_id LIKE 'ir-pdf-candidate:%'"
        ).fetchone()[0]
        == 3
    )
    assert conn.execute("SELECT COUNT(*) FROM fact_cells_v2").fetchone()[0] == 0

    # The exact response replays without another model call or a financial write.
    log2 = capture_for_ir_pdf_docs("RBRK", tmp_path, conn)
    assert log2.kpis_inserted_total == 0
    assert len(calls) == 1
    assert conn.execute("SELECT COUNT(*) FROM kpi_facts WHERE ticker='RBRK'").fetchone()[0] == 1
    conn.close()


def test_capture_for_ir_pdf_docs_skips_non_pdf_and_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An `.xlsx` supplement (deterministic spreadsheet path's job) and a PDF whose
    file is absent are both skipped without an LLM call."""
    db = tmp_path / "data" / "portfolio.db"
    db.parent.mkdir(parents=True)
    _make_capture_db(db)
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row

    # An xlsx supplement: must be ignored by the PDF path.
    conn.execute(
        "INSERT INTO documents (ticker, source_type, doc_type, period_end, file_path, "
        "sha256, fetched_at, fetch_status, raw_bytes_size) "
        "VALUES ('LLY', 'ir_doc', 'ir_supplement', '2025-09-30 00:00:00', "
        "'ir_documents/LLY/2025-09-30/ir_supplement__deadbeef.xlsx', 'b', '2025-10-01', 'ok', 1)"
    )
    # A PDF supplement whose file does not exist on disk.
    conn.execute(
        "INSERT INTO documents (ticker, source_type, doc_type, period_end, file_path, "
        "sha256, fetched_at, fetch_status, raw_bytes_size) "
        "VALUES ('LLY', 'ir_doc', 'ir_supplement', '2025-12-31 00:00:00', "
        "'ir_documents/LLY/2025-12-31/ir_supplement__missing0.pdf', 'c', '2026-01-01', 'ok', 1)"
    )
    conn.commit()

    calls: list[int] = []

    def fake_call(prompt: str, **_: object) -> object:
        calls.append(1)
        return json.loads(_ENUMERATE_JSON)

    monkeypatch.setattr(kes, "call_llm_structured", fake_call)

    log = capture_for_ir_pdf_docs("LLY", tmp_path, conn)
    assert log.kpis_inserted_total == 0
    assert calls == []  # xlsx filtered out; missing PDF skipped — no model call
    assert conn.execute("SELECT COUNT(*) FROM kpi_facts").fetchone()[0] == 0
    conn.close()


def test_capture_for_ir_pdf_docs_retains_native_page_locators_without_financial_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, migrated_db: Callable[..., Path]
) -> None:
    """A real PDF keeps exact page locators on immutable raw candidate nodes."""
    fitz = pytest.importorskip("fitz", reason="PyMuPDF (fitz) not installed")

    db = tmp_path / "data" / "portfolio.db"
    db.parent.mkdir(parents=True)
    migrated_db(db)
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row

    # A real 2-page supplement whose page 2 carries two of the three excerpts.
    pdf = tmp_path / "ir_documents" / "NU" / "2025-12-31" / "ir_supplement__cafe0001.pdf"
    pdf.parent.mkdir(parents=True, exist_ok=True)
    doc = fitz.open()
    doc.new_page().insert_text((72, 72), "Q4 2025 data pack")
    page2 = doc.new_page()
    page2.insert_text((72, 100), "Revenue was $1.2 billion")
    page2.insert_text((72, 140), "NIM of 17.8%")
    doc.save(str(pdf))
    doc.close()
    conn.execute(
        "INSERT INTO documents (ticker, source_type, doc_type, period_end, file_path, "
        "sha256, fetched_at, fetch_status, raw_bytes_size) "
        "VALUES ('NU', 'ir_doc', 'ir_supplement', '2025-12-31 00:00:00', ?, 'e', "
        "'2026-01-15 00:00:00', 'ok', 1)",
        (str(pdf).replace("\\", "/"),),
    )
    document_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
    conn.execute(
        "UPDATE documents SET sha256=?,raw_bytes_size=? WHERE id=?",
        (hashlib.sha256(pdf.read_bytes()).hexdigest(), pdf.stat().st_size, document_id),
    )
    from provenance.evidence_backfill import ensure_legacy_document_evidence

    ensure_legacy_document_evidence(conn, repo_root=tmp_path, document_id=document_id)
    conn.commit()

    import pypdf

    from provenance.fulltext_extractor_identity import PDF_FULLTEXT_PYPDF_VERSION

    monkeypatch.setattr(pypdf, "__version__", PDF_FULLTEXT_PYPDF_VERSION)
    monkeypatch.setattr(kes, "resolve_model_and_backend", _fixture_model_route)

    def extract_page(prompt: str, **kwargs: object) -> object:
        return json.loads(_ENUMERATE_JSON)[:2] if "NIM of 17.8%" in prompt else []

    monkeypatch.setattr(
        kes, "call_llm_structured_with_raw", partial(_governed_fixture_call, response=extract_page)
    )

    log = capture_for_ir_pdf_docs("NU", tmp_path, conn)
    assert log.error is None
    assert log.kpis_inserted_total == 0
    assert conn.execute("SELECT COUNT(*) FROM kpi_facts").fetchone()[0] == 0
    rows = conn.execute(
        "SELECT text,locator_json FROM evidence_nodes WHERE node_id LIKE 'ir-pdf-candidate:%'"
    ).fetchall()
    assert len(rows) == 2
    assert all(json.loads(str(row["locator_json"]))["page_number"] == 2 for row in rows)
    assert {json.loads(str(row["text"]))["row"]["raw_row"]["label"] for row in rows} == {
        "Revenue",
        "Net interest margin",
    }
    conn.close()
