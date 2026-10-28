"""End-to-end ingest of a capture-widened IR-spreadsheet config into kpi_facts.

Proves the S4 capture path on a real (synthetic) spreadsheet through the real
parse → ingest pipeline:

* capture rows land at IR_DOC tier with ``definition_origin='capture'``;
* a curated analyst series is NOT disturbed (regression) — its definition stays
  ``analyst`` and the claimed row is never re-minted as a capture duplicate;
* a captured label canonicalizes onto an existing series instead of fragmenting;
* an audited spreadsheet value SUPERSEDES the lower-tier LLM value for the same
  metric (tier precedence — the interaction with S3's broad LLM capture).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import sqlite3
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import ParamSpec, TypeVar, cast

import openpyxl
import pytest

import ir_pipeline.ingest as ingest_module
from ir_pipeline.config import IrConfig, SheetKpi, get_config
from ir_pipeline.config_builder import build_ir_config, widen_config
from ir_pipeline.ingest import ingest_spreadsheet_kpis
from ir_pipeline.spreadsheet import parse_spreadsheet, parse_spreadsheet_source
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite

PROJECT_ROOT = Path(__file__).resolve().parents[1]

_Q = [dt.datetime(2025, 6, 30), dt.datetime(2025, 9, 30), dt.datetime(2025, 12, 31)]

_P = ParamSpec("_P")
_R = TypeVar("_R")


def _stored_json_object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise AssertionError("stored cell payload must be a JSON object")
    parsed = cast("dict[object, object]", value)
    if not all(type(key) is str for key in parsed):
        raise AssertionError("stored cell payload keys must be strings")
    return cast("dict[str, object]", parsed)


def test_committed_nu_config_declares_monthly_arpac_currency() -> None:
    config = get_config("NU", PROJECT_ROOT)
    assert config is not None
    arpac = next(spec for spec in config.spreadsheet_kpis if spec.kpi_name == "Monthly ARPAC (USD)")
    assert config.reporting_currency == "USD"
    assert arpac.currency == "USD"


def _capture_kwargs(
    function: Callable[_P, _R],
    captured: dict[str, object],
) -> Callable[_P, _R]:
    def wrapped(*args: _P.args, **kwargs: _P.kwargs) -> _R:
        captured.update(kwargs)
        return function(*args, **kwargs)

    return wrapped


_SCHEMA = """
CREATE TABLE kpi_definitions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL, name TEXT NOT NULL, unit TEXT NOT NULL,
    primary_source TEXT NOT NULL, fallback_source TEXT, ir_url TEXT,
    threshold_tier TEXT, threshold_low FLOAT, threshold_high FLOAT, notes TEXT,
    definition_origin TEXT NOT NULL DEFAULT 'analyst',
    reporting_cadence TEXT NOT NULL DEFAULT 'quarterly',
    UNIQUE(ticker, name)
);
CREATE TABLE kpi_facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL, period_end TIMESTAMP NOT NULL, fiscal_period_type TEXT NOT NULL,
    kpi_definition_id INTEGER NOT NULL, value NUMERIC(24,6) NOT NULL, unit TEXT NOT NULL,
    source_doc_id INTEGER NOT NULL, confidence FLOAT NOT NULL DEFAULT 1.0,
    extracted_by TEXT, supersedes_id INTEGER, locator TEXT, source_excerpt TEXT
);
CREATE UNIQUE INDEX uq_kpi_facts_provenance
    ON kpi_facts (ticker, period_end, fiscal_period_type, kpi_definition_id, source_doc_id);
CREATE TABLE validation_issues (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL, source_doc_id INTEGER, ticker TEXT,
    severity TEXT NOT NULL, rule TEXT NOT NULL, raw_value TEXT, expected TEXT,
    raised_at TIMESTAMP NOT NULL, resolved_at TIMESTAMP
);
CREATE TABLE documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT, source_type TEXT, doc_type TEXT, period_end TIMESTAMP,
    file_path TEXT, sha256 TEXT, fetched_at TIMESTAMP, fetch_status TEXT,
    raw_bytes_size INTEGER, source_quality_tier TEXT
);
CREATE TABLE ingestion_runs (
    run_id TEXT PRIMARY KEY, started_at TIMESTAMP, ended_at TIMESTAMP,
    directive TEXT, ticker_scope TEXT, status TEXT, error_summary TEXT
);
"""


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(_SCHEMA)
    c.commit()
    return c


def _write_sheet(path: Path) -> None:
    wb = openpyxl.Workbook()
    ws = wb.active
    assert ws is not None
    ws.title = "Indicators"
    for j, q in enumerate(_Q):  # header row 4, labels col B, values col C+
        ws.cell(4, 3 + j, q)
    rows = [
        ("Fee Revenue", [50.0, 55.0, 60.0]),
        ("NII", [100.0, 110.0, 120.0]),
        ("Net interest margin (%)", [0.100, 0.095, 0.094]),  # decimal percent
        ("Total Available Funding", [38000.0, 39000.0, 40000.0]),
    ]
    for i, (lab, vals) in enumerate(rows):
        r = 5 + i
        ws.cell(r, 2, lab)
        for j, v in enumerate(vals):
            ws.cell(r, 3 + j, v)
    wb.save(str(path))


def _seed_doc(conn: sqlite3.Connection, *, ticker: str, period: dt.datetime) -> int:
    cur = conn.execute(
        "INSERT INTO documents (ticker, source_type, doc_type, period_end, fetched_at, "
        "fetch_status) VALUES (?, 'ir_doc', 'press_release', ?, ?, 'ok')",
        (ticker, period, dt.datetime(2026, 1, 1)),
    )
    conn.commit()
    return int(cur.lastrowid or 0)


def _defs(conn: sqlite3.Connection, ticker: str) -> dict[str, sqlite3.Row]:
    return {
        str(r["name"]): r
        for r in conn.execute(
            "SELECT * FROM kpi_definitions WHERE ticker = ?", (ticker.upper(),)
        ).fetchall()
    }


def _facts(conn: sqlite3.Connection, def_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM kpi_facts WHERE kpi_definition_id = ? ORDER BY period_end", (def_id,)
    ).fetchall()


def _series(conn: sqlite3.Connection, def_id: int) -> dict[str, float]:
    """A definition's facts as ``{period_end_str: value}``.

    This connection registers no datetime converter (mirroring the prod
    connection), so ``period_end`` reads back as the adapted string — key on
    ``str(period)`` to look one up, exactly as the schema stores it.
    """
    return {str(f["period_end"]): float(f["value"]) for f in _facts(conn, def_id)}


_Q4 = str(_Q[2])  # the 2025-12-31 period as stored (adapted string)


# ---------------------------------------------------------------------------


def test_capture_only_ticker_ingests_at_ir_doc_with_capture_origin(tmp_path: Path) -> None:
    conn = _conn()
    path = tmp_path / "co.xlsx"
    _write_sheet(path)
    cfg = build_ir_config(
        "CO",
        path,
        platform="mz",
        results_center_url="",
        repo_root=tmp_path,
        persist=False,
        reporting_currency="USD",
    )
    parsed = parse_spreadsheet(path, cfg, max_quarters=8)
    inserted, doc_id = ingest_spreadsheet_kpis(conn, "CO", cfg, parsed, path)

    assert inserted > 0
    # The spreadsheet document is registered at IR_DOC tier.
    doc = conn.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()
    assert doc["source_type"] == "ir_doc" and doc["doc_type"] == "ir_historical_spreadsheet"

    defs = _defs(conn, "CO")
    assert {"Fee Revenue", "NII", "Net interest margin (%)", "Total Available Funding"} <= set(defs)
    # Every minted definition is capture origin (no curated watchlist for CO).
    assert all(d["definition_origin"] == "capture" for d in defs.values())
    # Facts carry the deterministic IR-spreadsheet extractor tag at the IR doc.
    nii_facts = _facts(conn, int(defs["NII"]["id"]))
    assert len(nii_facts) == len(_Q)
    assert all(
        f["extracted_by"] == "ir_spreadsheet" and f["source_doc_id"] == doc_id for f in nii_facts
    )
    # Decimal percent scaled (0.094 -> 9.4%).
    nim = _series(conn, int(defs["Net interest margin (%)"]["id"]))
    assert round(nim[_Q4], 1) == 9.4
    assert [
        row[0]
        for row in conn.execute(
            "SELECT status FROM ingestion_runs WHERE directive='ingest_ir_spreadsheet'"
        ).fetchall()
    ] == ["ok"]


def test_ingest_fingerprints_source_config_and_parsed_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = _conn()
    path = tmp_path / "co.xlsx"
    _write_sheet(path)
    cfg = build_ir_config(
        "CO",
        path,
        platform="mz",
        results_center_url="",
        repo_root=tmp_path,
        persist=False,
        reporting_currency="USD",
    )
    parsed = parse_spreadsheet(path, cfg, max_quarters=8)
    captured: dict[str, object] = {}
    real_start = ingest_module.start_run
    monkeypatch.setattr(ingest_module, "start_run", _capture_kwargs(real_start, captured))
    ingest_spreadsheet_kpis(conn, "CO", cfg, parsed, path)

    inputs_obj = captured["invocation_inputs"]
    assert isinstance(inputs_obj, dict)
    inputs = cast(dict[str, object], inputs_obj)
    source_obj = inputs["source"]
    assert isinstance(source_obj, dict)
    source = cast(dict[str, object], source_obj)
    assert isinstance(source["sha256"], str)
    assert isinstance(inputs["config_sha256"], str)
    assert isinstance(inputs["parsed_sha256"], str)
    assert captured["deduplicate_completed"] is True


def test_curated_analyst_series_not_disturbed_by_widening(tmp_path: Path) -> None:
    """Regression: a hand-built analyst row keeps its definition + origin, and the
    row it claims is never re-minted as a capture duplicate."""
    conn = _conn()
    # Seed the curated analyst definition + a prior fact for one period.
    conn.execute(
        "INSERT INTO kpi_definitions (ticker, name, unit, primary_source, definition_origin) "
        "VALUES ('CO', 'Net Fee Revenue (USD)', 'actual', 'ir_doc', 'analyst')"
    )
    analyst_id = int(conn.execute("SELECT id FROM kpi_definitions").fetchone()["id"])
    prior_doc = _seed_doc(conn, ticker="CO", period=_Q[0])
    conn.execute(
        "INSERT INTO kpi_facts (ticker, period_end, fiscal_period_type, kpi_definition_id, "
        "value, unit, source_doc_id, extracted_by) VALUES "
        "('CO', ?, 'Q2', ?, '50', 'actual', ?, 'llm:haiku')",
        (_Q[0], analyst_id, prior_doc),
    )
    conn.commit()

    path = tmp_path / "co.xlsx"
    _write_sheet(path)
    # A curated analyst config that maps the "Fee Revenue" row to the canonical name.
    base = IrConfig(
        ticker="CO",
        platform="mz",
        results_center_url="",
        reporting_currency="USD",
        spreadsheet_kpis=(
            SheetKpi("Net Fee Revenue (USD)", "Indicators", "Fee Revenue", "usd", 1.0),
        ),
    )
    cfg = widen_config(base, path)
    parsed = parse_spreadsheet(path, cfg, max_quarters=8)
    ingest_spreadsheet_kpis(conn, "CO", cfg, parsed, path)

    defs = _defs(conn, "CO")
    # The curated definition still exists, still analyst origin.
    assert defs["Net Fee Revenue (USD)"]["id"] == analyst_id
    assert defs["Net Fee Revenue (USD)"]["definition_origin"] == "analyst"
    # No capture duplicate for the claimed "Fee Revenue" row.
    assert "Fee Revenue" not in defs
    # The analyst series received the spreadsheet's values (IR_DOC).
    assert _series(conn, analyst_id)[_Q4] == 60.0
    # Capture rows for the OTHER labels still landed at capture origin.
    assert defs["NII"]["definition_origin"] == "capture"


def test_capture_label_reuses_existing_definition(tmp_path: Path) -> None:
    """A captured raw label canonicalizes onto an existing same-metric series
    (unit-only difference) rather than minting a fragmented duplicate."""
    conn = _conn()
    conn.execute(
        "INSERT INTO kpi_definitions (ticker, name, unit, primary_source, definition_origin) "
        "VALUES ('CO', 'NII (USD)', 'actual', 'ir_doc', 'capture')"
    )
    existing_id = int(conn.execute("SELECT id FROM kpi_definitions").fetchone()["id"])
    conn.commit()

    path = tmp_path / "co.xlsx"
    _write_sheet(path)
    cfg = build_ir_config(
        "CO",
        path,
        platform="mz",
        results_center_url="",
        repo_root=tmp_path,
        persist=False,
        reporting_currency="USD",
    )
    parsed = parse_spreadsheet(path, cfg, max_quarters=8)
    ingest_spreadsheet_kpis(conn, "CO", cfg, parsed, path)

    defs = _defs(conn, "CO")
    # "NII" canonicalized onto "NII (USD)" — no bare "NII" duplicate minted.
    assert "NII" not in defs
    assert _series(conn, existing_id)[_Q4] == 120.0  # the existing series received the values


def test_ir_doc_value_supersedes_llm_incumbent_for_same_metric(tmp_path: Path) -> None:
    """Tier precedence: an LLM-extracted value for a metric the spreadsheet also
    carries is superseded by the audited IR_DOC value (the S3↔S4 interaction)."""
    conn = _conn()
    # An LLM capture (S3) already minted "NII" and stored a value for 2025-12-31.
    conn.execute(
        "INSERT INTO kpi_definitions (ticker, name, unit, primary_source, definition_origin) "
        "VALUES ('CO', 'NII', 'actual', 'ir_doc', 'capture')"
    )
    nii_id = int(conn.execute("SELECT id FROM kpi_definitions").fetchone()["id"])
    llm_doc = _seed_doc(conn, ticker="CO", period=_Q[2])
    conn.execute(
        "INSERT INTO kpi_facts (ticker, period_end, fiscal_period_type, kpi_definition_id, "
        "value, unit, source_doc_id, extracted_by) VALUES "
        "('CO', ?, 'Q4', ?, '999', 'actual', ?, 'llm:claude-haiku')",
        (_Q[2], nii_id, llm_doc),
    )
    conn.commit()

    path = tmp_path / "co.xlsx"
    _write_sheet(path)
    cfg = build_ir_config(
        "CO",
        path,
        platform="mz",
        results_center_url="",
        repo_root=tmp_path,
        persist=False,
        reporting_currency="USD",
    )
    parsed = parse_spreadsheet(path, cfg, max_quarters=8)
    ingest_spreadsheet_kpis(conn, "CO", cfg, parsed, path)

    q4 = [f for f in _facts(conn, nii_id) if str(f["period_end"]) == _Q4]
    # History is append-only: the audited row points to the lower-tier incumbent.
    assert len(q4) == 2
    assert q4[0]["extracted_by"] == "llm:claude-haiku"
    assert q4[1]["extracted_by"] == "ir_spreadsheet"
    assert q4[1]["supersedes_id"] == q4[0]["id"]
    assert float(q4[1]["value"]) == 120.0


def test_post_cutover_ingest_dual_writes_evidence_and_preserves_history(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = tmp_path / "repo"
    db_path = migrated_db(repo_root / "data" / "portfolio.db")
    spreadsheet_dir = repo_root / "data" / "ir_spreadsheets" / "CO"
    spreadsheet_dir.mkdir(parents=True)
    first_path = spreadsheet_dir / "co-first.xlsx"
    second_path = spreadsheet_dir / "co-second.xlsx"
    _write_sheet(first_path)
    _write_sheet(second_path)
    workbook = openpyxl.load_workbook(second_path)
    workbook["Indicators"].cell(6, 5, 121.0)
    workbook.save(second_path)
    cfg = IrConfig(
        ticker="CO",
        platform="mz",
        results_center_url="https://example.invalid/results",
        spreadsheet_kpis=(SheetKpi("NII", "Indicators", "NII", "actual", currency="USD"),),
    )
    first_parsed = parse_spreadsheet_source(first_path, cfg, max_quarters=1)
    second_parsed = parse_spreadsheet_source(second_path, cfg, max_quarters=1)
    assert first_parsed.workbook_sha256 != second_parsed.workbook_sha256
    capture_receipts: list[object] = []
    real_capture = ingest_module.capture_spreadsheet_parse

    def record_capture(*args: object, **kwargs: object) -> object:
        receipt = cast("Callable[..., object]", real_capture)(*args, **kwargs)
        capture_receipts.append(receipt)
        return receipt

    monkeypatch.setattr(ingest_module, "capture_spreadsheet_parse", record_capture)

    conn = connect_sqlite(
        db_path,
        role=SQLiteConnectionRole.WRITER,
        schema_preflight=True,
    )
    conn.row_factory = sqlite3.Row
    try:
        first_inserted, first_doc = ingest_spreadsheet_kpis(
            conn,
            "CO",
            cfg,
            first_parsed,
            first_path,
            repo_root=repo_root,
        )
        conn.execute(
            "UPDATE documents SET fetched_at = '2025-01-01 00:00:00' WHERE id = ?",
            (first_doc,),
        )
        second_inserted, second_doc = ingest_spreadsheet_kpis(
            conn,
            "CO",
            cfg,
            second_parsed,
            second_path,
            repo_root=repo_root,
        )
        facts = conn.execute(
            "SELECT id, value, supersedes_id, source_doc_id, locator FROM kpi_facts "
            "WHERE ticker = 'CO' ORDER BY id"
        ).fetchall()
        original_capture = conn.execute(
            "SELECT extraction_run_id,started_at,completed_at,output_sha256 "
            "FROM evidence_extraction_runs WHERE extractor_name=? "
            "ORDER BY document_version_id",
            ("ir-spreadsheet-parser-capture",),
        ).fetchall()
        original_nodes = conn.execute(
            "SELECT node_id,text,locator_json,recorded_at FROM evidence_nodes "
            "WHERE extraction_run_id IN (SELECT extraction_run_id "
            "FROM evidence_extraction_runs WHERE extractor_name=?) ORDER BY node_id",
            ("ir-spreadsheet-parser-capture",),
        ).fetchall()
        from pipeline.run_accounting import PipelineRunSuppressedError

        with pytest.raises(PipelineRunSuppressedError):
            ingest_spreadsheet_kpis(
                conn,
                "CO",
                cfg,
                second_parsed,
                second_path,
                repo_root=repo_root,
            )
        evidence_count = conn.execute(
            "SELECT COUNT(*) FROM evidence_document_versions WHERE legacy_document_id IN (?, ?)",
            (first_doc, second_doc),
        ).fetchone()[0]
        capture_runs = conn.execute(
            "SELECT extraction_run_id, document_version_id, input_sha256, outcome "
            "FROM evidence_extraction_runs WHERE extractor_name = ? "
            "ORDER BY document_version_id",
            ("ir-spreadsheet-parser-capture",),
        ).fetchall()
        replayed_capture = conn.execute(
            "SELECT extraction_run_id,started_at,completed_at,output_sha256 "
            "FROM evidence_extraction_runs WHERE extractor_name=? "
            "ORDER BY document_version_id",
            ("ir-spreadsheet-parser-capture",),
        ).fetchall()
        replayed_nodes = conn.execute(
            "SELECT node_id,text,locator_json,recorded_at FROM evidence_nodes "
            "WHERE extraction_run_id IN (SELECT extraction_run_id "
            "FROM evidence_extraction_runs WHERE extractor_name=?) ORDER BY node_id",
            ("ir-spreadsheet-parser-capture",),
        ).fetchall()
        capture_attempts = conn.execute(
            "SELECT status FROM ingestion_runs WHERE directive=? ORDER BY started_at",
            ("capture_ir_spreadsheet",),
        ).fetchall()
        revision_count = conn.execute(
            "SELECT COUNT(*) FROM fact_observation_revisions "
            "WHERE fact_table = 'kpi_facts' AND fact_row_id IN (?, ?)",
            (facts[0]["id"], facts[1]["id"]),
        ).fetchone()[0]
    finally:
        conn.close()

    assert (first_inserted, second_inserted) == (1, 1)
    assert evidence_count == 2
    assert len(capture_runs) == 2
    assert {str(row[1]) for row in capture_runs} == {
        f"legacy-doc-{first_doc}",
        f"legacy-doc-{second_doc}",
    }
    assert {str(row[2]) for row in capture_runs} == {
        first_parsed.workbook_sha256,
        second_parsed.workbook_sha256,
    }
    assert all(str(row[3]) == "succeeded" for row in capture_runs)
    assert len(capture_receipts) == 3
    first_receipt, replay_receipt = capture_receipts[1], capture_receipts[2]
    assert getattr(replay_receipt, "replayed") is True
    assert getattr(replay_receipt, "extraction_run_id") == getattr(
        first_receipt, "extraction_run_id"
    )
    assert getattr(replay_receipt, "node_ids") == getattr(first_receipt, "node_ids")
    assert getattr(replay_receipt, "started_at") == getattr(first_receipt, "started_at")
    assert getattr(replay_receipt, "completed_at") == getattr(first_receipt, "completed_at")
    assert [tuple(row) for row in original_capture] == [tuple(row) for row in replayed_capture]
    assert [tuple(row) for row in original_nodes] == [tuple(row) for row in replayed_nodes]
    assert [str(row[0]) for row in capture_attempts] == ["ok", "ok", "ok"]
    assert revision_count == 2
    assert len(facts) == 2
    assert facts[1]["supersedes_id"] == facts[0]["id"]
    assert (facts[0]["source_doc_id"], facts[1]["source_doc_id"]) == (
        first_doc,
        second_doc,
    )

    assert [float(fact["value"]) for fact in facts] == [120.0, 121.0]
    for fact in facts:
        locator = json.loads(fact["locator"])
        assert locator["kind"] == "spreadsheet_cell"
        assert locator["spreadsheet_cell"] == {"sheet_name": "Indicators", "cell_address": "E6"}


@pytest.mark.parametrize("source_backed", [False, True], ids=["numeric", "typed"])
def test_identical_workbook_bytes_cannot_reuse_another_issuer_document(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    source_backed: bool,
) -> None:
    from pipeline.run_accounting import PipelineRunSuppressedError

    repo_root = tmp_path / "repo"
    db_path = migrated_db(repo_root / "data" / "portfolio.db")
    first_path = repo_root / "data" / "ir_spreadsheets" / "CO" / "history.xlsx"
    other_path = repo_root / "data" / "ir_spreadsheets" / "NN" / "history.xlsx"
    first_path.parent.mkdir(parents=True)
    other_path.parent.mkdir(parents=True)
    _write_sheet(first_path)
    workbook_bytes = first_path.read_bytes()
    other_path.write_bytes(workbook_bytes)
    configs = {
        ticker: IrConfig(
            ticker=ticker,
            platform="mz",
            results_center_url="https://example.invalid/results",
            spreadsheet_kpis=(SheetKpi("NII", "Indicators", "NII", "actual", currency="USD"),),
        )
        for ticker in ("CO", "NN")
    }
    first_parsed = parse_spreadsheet_source(first_path, configs["CO"], max_quarters=1)
    other_parsed = parse_spreadsheet_source(other_path, configs["NN"], max_quarters=1)
    assert first_parsed.workbook_sha256 == other_parsed.workbook_sha256
    first_input = first_parsed if source_backed else first_parsed.numeric_projection()
    other_input = other_parsed if source_backed else other_parsed.numeric_projection()

    conn = connect_sqlite(db_path, role=SQLiteConnectionRole.WRITER, schema_preflight=True)
    conn.row_factory = sqlite3.Row
    try:
        inserted, document_id = ingest_spreadsheet_kpis(
            conn, "CO", configs["CO"], first_input, first_path, repo_root=repo_root
        )
        assert inserted == 1
        with pytest.raises(PipelineRunSuppressedError):
            ingest_spreadsheet_kpis(
                conn, "CO", configs["CO"], first_input, first_path, repo_root=repo_root
            )
        changes_before_conflict = conn.total_changes
        with pytest.raises(ValueError, match=r"spreadsheet document hash.*another issuer"):
            ingest_spreadsheet_kpis(
                conn, "NN", configs["NN"], other_input, other_path, repo_root=repo_root
            )
        assert conn.total_changes == changes_before_conflict
        documents = conn.execute("SELECT id, ticker, sha256 FROM documents").fetchall()
        assert [tuple(row) for row in documents] == [
            (document_id, "CO", first_parsed.workbook_sha256)
        ]
        facts = conn.execute(
            "SELECT f.ticker, d.ticker, f.source_doc_id FROM kpi_facts f "
            "JOIN documents d ON d.id = f.source_doc_id ORDER BY f.id"
        ).fetchall()
        assert [tuple(row) for row in facts] == [("CO", "CO", document_id)]
        evidence = conn.execute(
            "SELECT ticker FROM evidence_document_versions WHERE legacy_document_id = ?",
            (document_id,),
        ).fetchall()
        assert [tuple(row) for row in evidence] == [("CO",)]
        assert first_path.read_bytes() == other_path.read_bytes() == workbook_bytes
    finally:
        conn.close()


@pytest.mark.parametrize("reported_label", ["NII", "Net Interest Income"])
def test_typed_handoff_preserves_numeric_writer_semantics_and_reader_lane(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    reported_label: str,
) -> None:
    """Synthetic NU-shaped input compares the real writer, not NU financial coverage."""
    from pipeline.kpi_semantics import (
        KpiSemanticContext,
        current_kpi_semantic_context,
        semantic_admission_sql,
    )

    contexts: dict[str, KpiSemanticContext] = {}
    for mode in ("numeric", "typed"):
        root = tmp_path / mode
        database = migrated_db(root / "data" / "portfolio.db")
        workbook_path = root / "source.xlsx"
        _write_sheet(workbook_path)
        workbook = openpyxl.load_workbook(workbook_path)
        workbook["Indicators"].cell(6, 2, reported_label)
        workbook.save(workbook_path)
        workbook.close()
        config = IrConfig(
            ticker="NU",
            platform="mz",
            results_center_url="https://example.invalid/results",
            spreadsheet_kpis=(
                SheetKpi("NII", "Indicators", reported_label, "actual", currency="USD"),
            ),
        )
        parsed = parse_spreadsheet_source(workbook_path, config, max_quarters=1)
        conn = connect_sqlite(database, role=SQLiteConnectionRole.WRITER, schema_preflight=True)
        conn.row_factory = sqlite3.Row
        try:
            inserted, _ = ingest_spreadsheet_kpis(
                conn,
                "NU",
                config,
                parsed if mode == "typed" else parsed.numeric_projection(),
                workbook_path,
                repo_root=root,
            )
            assert inserted == 1
            fact = conn.execute("SELECT id,value FROM kpi_facts WHERE ticker='NU'").fetchone()
            assert fact is not None and float(fact["value"]) == 120.0
            context = current_kpi_semantic_context(conn, kpi_fact_id=int(fact["id"]))
            assert context is not None
            contexts[mode] = context.context
            assert context.context.status.value == "legacy_unknown"
            assert context.context.publication_lane.value == "unclassified"
            join, predicate = semantic_admission_sql(conn, fact_alias="kf")
            readable = conn.execute(
                f"SELECT kf.value FROM kpi_facts kf {join} WHERE kf.ticker='NU' AND {predicate}"
            ).fetchall()
            assert [float(row[0]) for row in readable] == [120.0]
        finally:
            conn.close()

    # Only the exact source label is enriched. Basis, status, dimensions,
    # period and publication lane stay equal to the writer's numeric default.
    assert contexts["numeric"].metric_name_as_reported == "NII"
    assert contexts["typed"].metric_name_as_reported == reported_label
    assert contexts["typed"].model_dump(exclude={"metric_name_as_reported"}) == contexts[
        "numeric"
    ].model_dump(exclude={"metric_name_as_reported"})


def test_unsupported_cell_is_explicitly_skipped_without_losing_other_periods(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.xlsx"
    source.write_bytes(b"synthetic parser boundary")

    class Sheet:
        def iter_rows(self, *, values_only: bool) -> list[tuple[object, ...]]:
            assert values_only
            return [("Metric", *_Q), ("NII", 100.0, object(), 120.0)]

    class Workbook:
        sheetnames = ("Indicators",)

        def __getitem__(self, name: str) -> Sheet:
            assert name == "Indicators"
            return Sheet()

        def close(self) -> None:
            pass

    def loaded(*_args: object, **_kwargs: object) -> Workbook:
        return Workbook()

    monkeypatch.setattr(openpyxl, "load_workbook", loaded)
    config = IrConfig(
        ticker="SYNTH",
        platform="mz",
        results_center_url="",
        label_col=0,
        spreadsheet_kpis=(SheetKpi("NII", "Indicators", "NII", "actual"),),
    )
    parsed = parse_spreadsheet_source(source, config)
    assert parsed.numeric_projection() == {"NII": {_Q[0]: 100.0, _Q[2]: 120.0}}
    unsupported = [cell for cell in parsed.cells if cell.disposition == "unsupported_value"]
    assert len(unsupported) == 1
    assert unsupported[0].raw_value is None and unsupported[0].scaled_value is None
    assert unsupported[0].representation == "unavailable_openpyxl_type"
    assert unsupported[0].period_end == _Q[1]
    assert unsupported[0].locator is not None
    assert parse_spreadsheet(source, config) == parsed.numeric_projection()


def test_typed_parser_keeps_whole_winner_through_both_overwrite_rules(tmp_path: Path) -> None:
    conn = _conn()
    conn.execute(
        "INSERT INTO kpi_definitions (ticker,name,unit,primary_source) "
        "VALUES ('CO','NII (USD)','actual','ir_doc')"
    )
    conn.commit()
    path = tmp_path / "co.xlsx"
    _write_sheet(path)
    wb = openpyxl.load_workbook(path)
    ws = wb["Indicators"]
    ws.cell(4, 6, _Q[-1])
    ws.cell(6, 6, 121)
    wb.save(path)
    wb.close()
    cfg = IrConfig(
        ticker="CO",
        platform="mz",
        results_center_url="",
        reporting_currency="USD",
        spreadsheet_kpis=(
            SheetKpi("NII", "Indicators", "Fee Revenue", "actual", scale=2, origin="capture"),
            SheetKpi("NII (USD)", "Indicators", "NII", "actual", scale=3, origin="capture"),
            SheetKpi(
                "NII", "Indicators", "Total Available Funding", "actual", scale=4, origin="capture"
            ),
        ),
    )
    parsed = parse_spreadsheet_source(path, cfg, max_quarters=1)
    assert parsed.numeric_projection() == {"NII": {_Q[-1]: 160000.0}, "NII (USD)": {_Q[-1]: 363.0}}
    assert parse_spreadsheet(path, cfg, max_quarters=1) == parsed.numeric_projection()
    assert {cell.disposition for cell in parsed.cells} >= {
        "selected",
        "overwritten_configured_name",
        "overwritten_period",
        "outside_window",
    }
    inserted, _ = ingest_spreadsheet_kpis(conn, "CO", cfg, parsed, path)
    assert inserted == 1
    row = conn.execute("SELECT value,locator FROM kpi_facts").fetchone()
    assert float(row["value"]) == 363.0
    assert json.loads(row["locator"])["spreadsheet_cell"] == {
        "sheet_name": "Indicators",
        "cell_address": "F6",
    }
    winner = parsed.selected_series()["NII (USD)"][_Q[-1]]
    assert winner.raw_label == "NII" and winner.spec.kpi_name == "NII (USD)"
    assert winner.spec.scale == 3 and winner.raw_value == 121
    assert winner.representation == "openpyxl_data_only"
    assert winner.header is not None and winner.header.cell_address == "F4"
    assert winner.locator is not None
    assert parsed.workbook_sha256 == hashlib.sha256(path.read_bytes()).hexdigest()
    assert parsed.workbook_bytes_size == path.stat().st_size


def test_typed_ingest_rejects_changed_bytes_or_config_before_writes(tmp_path: Path) -> None:
    conn = _conn()
    path = tmp_path / "co.xlsx"
    _write_sheet(path)
    cfg = IrConfig(
        ticker="CO",
        platform="mz",
        results_center_url="",
        reporting_currency="USD",
        spreadsheet_kpis=(SheetKpi("NII", "Indicators", "NII", "actual"),),
    )
    parsed = parse_spreadsheet_source(path, cfg)
    wb = openpyxl.load_workbook(path)
    wb["Indicators"].cell(6, 5, 121)
    wb.save(path)
    wb.close()
    with pytest.raises(ValueError, match="bytes changed"):
        ingest_spreadsheet_kpis(conn, "CO", cfg, parsed, path)
    assert conn.total_changes == 0
    fresh = parse_spreadsheet_source(path, cfg)
    changed_config = IrConfig(
        ticker="CO",
        platform="mz",
        results_center_url="changed",
        reporting_currency="USD",
        spreadsheet_kpis=cfg.spreadsheet_kpis,
    )
    with pytest.raises(ValueError, match="config changed"):
        ingest_spreadsheet_kpis(conn, "CO", changed_config, fresh, path)
    assert conn.total_changes == 0


def test_typed_parser_retains_skips_and_writer_keeps_unknown_and_rejected_states(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    from pipeline.kpi_semantics import current_kpi_semantic_context

    repo_root = tmp_path / "repo"
    db_path = migrated_db(repo_root / "data" / "portfolio.db")
    path = repo_root / "co.xlsx"
    _write_sheet(path)
    wb = openpyxl.load_workbook(path)
    ws = wb["Indicators"]
    ws.cell(6, 4, "pending")
    ws.cell(4, 6, _Q[-1])
    ws.cell(6, 6, 121)
    ws.cell(4, 7, "undisclosed period")
    ws.cell(6, 7, 777)
    ws.cell(9, 2, "Rejected percentage")
    ws.cell(9, 5, 2001)
    wb.save(path)
    wb.close()
    cfg = IrConfig(
        ticker="CO",
        platform="mz",
        results_center_url="",
        reporting_currency="USD",
        spreadsheet_kpis=(
            SheetKpi("NII", "Indicators", "NII", "actual"),
            SheetKpi("NII", "Indicators", "NII", "actual"),
            SheetKpi("Rejected percentage", "Indicators", "Rejected percentage", "percent"),
            SheetKpi("Absent", "Missing", "absent", "ratio"),
            SheetKpi("Absent row", "Indicators", "absent row", "ratio"),
        ),
    )
    parsed = parse_spreadsheet_source(path, cfg, max_quarters=1)
    assert any(
        cell.disposition == "nonnumeric" and cell.raw_value == "pending" for cell in parsed.cells
    )
    assert any(
        cell.disposition == "unknown_period" and cell.raw_value == 777 and cell.period_end is None
        for cell in parsed.cells
    )
    assert any(cell.disposition == "sheet_missing" for cell in parsed.cells)
    rejected = parsed.selected_series()["Rejected percentage"][_Q[-1]]
    assert rejected.raw_value == 2001 and rejected.scaled_value == 2001.0
    conn = connect_sqlite(db_path, role=SQLiteConnectionRole.WRITER, schema_preflight=True)
    conn.row_factory = sqlite3.Row
    try:
        inserted, doc_id = ingest_spreadsheet_kpis(
            conn, "CO", cfg, parsed, path, repo_root=repo_root
        )
        assert inserted == 1
        fact = conn.execute("SELECT id FROM kpi_facts").fetchone()
        context = current_kpi_semantic_context(conn, kpi_fact_id=fact["id"])
        assert context is not None and context.context.status.value == "legacy_unknown"
        assert context.context.publication_lane.value == "unclassified"
        assert context.context.metric_name_as_reported == "NII"
        issue = conn.execute(
            "SELECT rule,raw_value,source_doc_id FROM validation_issues"
        ).fetchone()
        assert issue["rule"] == "plausible_range" and "2001" in issue["raw_value"]
        assert issue["source_doc_id"] == doc_id
        assert parsed.selected_series()["Rejected percentage"][_Q[-1]] == rejected
        capture_run = conn.execute(
            "SELECT extraction_run_id FROM evidence_extraction_runs WHERE extractor_name=?",
            ("ir-spreadsheet-parser-capture",),
        ).fetchone()
        assert capture_run is not None
        capture_nodes = conn.execute(
            "SELECT node_id,node_kind,text,locator_json FROM evidence_nodes "
            "WHERE extraction_run_id=?",
            (capture_run[0],),
        ).fetchall()
        root = next(row for row in capture_nodes if row["node_kind"] == "table")
        root_value = json.loads(root["text"])
        children = {
            row["node_id"]: row
            for row in conn.execute(
                "SELECT node_id,node_kind,text,locator_json FROM evidence_nodes "
                "WHERE extraction_run_id=? AND node_kind='table_cell'",
                (capture_run[0],),
            )
        }
        selections = root_value["selection_manifest"]
        assert len(selections) == len(parsed.cells)
        assert root_value["cell_count"] == len(parsed.cells)
        assert [item["ordinal"] for item in selections] == list(range(len(parsed.cells)))
        located = [cell for cell in parsed.cells if cell.locator is not None]
        assert len(children) == len(located)
        assert root_value["located_cell_count"] == len(located)
        assert all(len(item["sha256"]) == 64 for item in selections if item["storage"] == "node")
        captured: list[tuple[dict[str, object], sqlite3.Row | None]] = []
        for item in selections:
            if item["storage"] == "inline":
                captured.append((_stored_json_object(item["payload"]), None))
            else:
                node = children[item["node_id"]]
                captured.append((_stored_json_object(json.loads(node["text"])), node))
        captured_cells = [item[0] for item in captured]
        assert [cell["ordinal"] for cell in captured_cells] == list(range(len(parsed.cells)))
        assert {cell["disposition"] for cell in captured_cells} >= {
            "selected",
            "nonnumeric",
            "unknown_period",
            "sheet_missing",
            "row_missing",
            "overwritten_period",
            "overwritten_configured_name",
            "outside_window",
        }
        assert any(
            cell["raw_value"] == {"tag": "int", "decimal": "777"}
            and cell["disposition"] == "unknown_period"
            for cell in captured_cells
        )
        assert any(
            cell["disposition"] == "sheet_missing" and node is None for cell, node in captured
        )
        assert any(cell["disposition"] == "row_missing" and node is None for cell, node in captured)
    finally:
        conn.close()


def test_capture_output_conflict_fails_before_legacy_completed_run_suppression(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    repo_root = tmp_path / "repo"
    db_path = migrated_db(repo_root / "data" / "portfolio.db")
    path = repo_root / "co.xlsx"
    _write_sheet(path)
    cfg = IrConfig(
        ticker="CO",
        platform="mz",
        results_center_url="",
        reporting_currency="USD",
        spreadsheet_kpis=(SheetKpi("NII", "Indicators", "NII", "actual"),),
    )
    parsed = parse_spreadsheet_source(path, cfg)
    changed_output = replace(
        parsed,
        cells=(replace(parsed.cells[0], disposition="unsupported_value"), *parsed.cells[1:]),
    )
    conn = connect_sqlite(db_path, role=SQLiteConnectionRole.WRITER, schema_preflight=True)
    conn.row_factory = sqlite3.Row
    try:
        ingest_spreadsheet_kpis(conn, "CO", cfg, parsed, path, repo_root=repo_root)
        fact_count = conn.execute("SELECT COUNT(*) FROM kpi_facts").fetchone()[0]
        with pytest.raises(ValueError, match="conflicts with its input identity"):
            ingest_spreadsheet_kpis(conn, "CO", cfg, changed_output, path, repo_root=repo_root)
        assert conn.execute("SELECT COUNT(*) FROM kpi_facts").fetchone()[0] == fact_count
        attempts = conn.execute(
            "SELECT status FROM ingestion_runs WHERE directive=? ORDER BY started_at",
            ("capture_ir_spreadsheet",),
        ).fetchall()
        assert [str(row[0]) for row in attempts] == ["ok", "failed"]
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM evidence_extraction_runs WHERE extractor_name=?",
                ("ir-spreadsheet-parser-capture",),
            ).fetchone()[0]
            == 1
        )
    finally:
        conn.close()


@pytest.mark.parametrize("locator_change", ["address", "removed"])
def test_capture_replay_rejects_locator_only_output_change(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    locator_change: str,
) -> None:
    from models.facts import SpreadsheetCellRef

    repo_root = tmp_path / "repo"
    db_path = migrated_db(repo_root / "data" / "portfolio.db")
    path = repo_root / "co.xlsx"
    _write_sheet(path)
    cfg = IrConfig(
        ticker="CO",
        platform="mz",
        results_center_url="",
        reporting_currency="USD",
        spreadsheet_kpis=(SheetKpi("NII", "Indicators", "NII", "actual"),),
    )
    parsed = parse_spreadsheet_source(path, cfg, max_quarters=1)
    selected_index = next(
        index for index, cell in enumerate(parsed.cells) if cell.disposition == "selected"
    )
    selected = parsed.cells[selected_index]
    assert selected.locator is not None and selected.locator.spreadsheet_cell is not None
    changed_locator = (
        None
        if locator_change == "removed"
        else selected.locator.model_copy(
            update={
                "spreadsheet_cell": SpreadsheetCellRef(
                    sheet_name=selected.locator.spreadsheet_cell.sheet_name,
                    cell_address="G6",
                )
            }
        )
    )
    changed_cells = list(parsed.cells)
    changed_cells[selected_index] = replace(selected, locator=changed_locator)
    changed_parsed = replace(parsed, cells=tuple(changed_cells))
    conn = connect_sqlite(db_path, role=SQLiteConnectionRole.WRITER, schema_preflight=True)
    conn.row_factory = sqlite3.Row
    try:
        ingest_spreadsheet_kpis(conn, "CO", cfg, parsed, path, repo_root=repo_root)
        facts_before = conn.execute(
            "SELECT id,value,period_end,locator FROM kpi_facts ORDER BY id"
        ).fetchall()
        runs_before = conn.execute(
            "SELECT extraction_run_id,started_at,completed_at,output_sha256 "
            "FROM evidence_extraction_runs WHERE extractor_name=?",
            ("ir-spreadsheet-parser-capture",),
        ).fetchall()
        nodes_before = conn.execute(
            "SELECT node_id,text,locator_json,locator_sha256,recorded_at FROM evidence_nodes "
            "WHERE extraction_run_id IN (SELECT extraction_run_id FROM evidence_extraction_runs "
            "WHERE extractor_name=?) ORDER BY node_id",
            ("ir-spreadsheet-parser-capture",),
        ).fetchall()
        with pytest.raises(ValueError, match="conflicts with its input identity"):
            ingest_spreadsheet_kpis(conn, "CO", cfg, changed_parsed, path, repo_root=repo_root)
        assert [
            tuple(row)
            for row in conn.execute("SELECT id,value,period_end,locator FROM kpi_facts ORDER BY id")
        ] == [tuple(row) for row in facts_before]
        assert [
            tuple(row)
            for row in conn.execute(
                "SELECT extraction_run_id,started_at,completed_at,output_sha256 "
                "FROM evidence_extraction_runs WHERE extractor_name=?",
                ("ir-spreadsheet-parser-capture",),
            )
        ] == [tuple(row) for row in runs_before]
        assert [
            tuple(row)
            for row in conn.execute(
                "SELECT node_id,text,locator_json,locator_sha256,recorded_at FROM evidence_nodes "
                "WHERE extraction_run_id IN (SELECT extraction_run_id FROM evidence_extraction_runs "
                "WHERE extractor_name=?) ORDER BY node_id",
                ("ir-spreadsheet-parser-capture",),
            )
        ] == [tuple(row) for row in nodes_before]
        assert [
            str(row[0])
            for row in conn.execute(
                "SELECT status FROM ingestion_runs WHERE directive=? ORDER BY started_at",
                ("capture_ir_spreadsheet",),
            )
        ] == ["ok", "failed"]
    finally:
        conn.close()


def test_capture_effective_window_is_part_of_input_identity(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    repo_root = tmp_path / "repo"
    db_path = migrated_db(repo_root / "data" / "portfolio.db")
    path = repo_root / "co.xlsx"
    _write_sheet(path)
    cfg = IrConfig(
        ticker="CO",
        platform="mz",
        results_center_url="",
        reporting_currency="USD",
        spreadsheet_kpis=(SheetKpi("NII", "Indicators", "NII", "actual"),),
    )
    one_quarter = parse_spreadsheet_source(path, cfg, max_quarters=1)
    two_quarters = parse_spreadsheet_source(path, cfg, max_quarters=2)
    conn = connect_sqlite(db_path, role=SQLiteConnectionRole.WRITER, schema_preflight=True)
    conn.row_factory = sqlite3.Row
    try:
        ingest_spreadsheet_kpis(conn, "CO", cfg, one_quarter, path, repo_root=repo_root)
        ingest_spreadsheet_kpis(conn, "CO", cfg, two_quarters, path, repo_root=repo_root)
        captures = conn.execute(
            "SELECT extraction_run_id,extractor_config_sha256 FROM evidence_extraction_runs "
            "WHERE extractor_name=? ORDER BY extraction_run_id",
            ("ir-spreadsheet-parser-capture",),
        ).fetchall()
        assert len(captures) == 2
        assert captures[0][0] != captures[1][0]
        assert captures[0][1] != captures[1][1]
    finally:
        conn.close()


@pytest.mark.parametrize("failure_point", ["child", "success_receipt"])
def test_capture_failure_rolls_back_graph_and_fails_attempt(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    failure_point: str,
) -> None:
    import ir_pipeline.spreadsheet_capture as capture_module

    repo_root = tmp_path / "repo"
    db_path = migrated_db(repo_root / "data" / "portfolio.db")
    path = repo_root / "co.xlsx"
    _write_sheet(path)
    cfg = IrConfig(
        ticker="CO",
        platform="mz",
        results_center_url="",
        reporting_currency="USD",
        spreadsheet_kpis=(SheetKpi("NII", "Indicators", "NII", "actual"),),
    )
    parsed = parse_spreadsheet_source(path, cfg)
    if failure_point == "child":
        original_persist = capture_module.EvidenceLedger.persist
        failed = False

        def fail_child(self: object, record: object) -> object:
            nonlocal failed
            if getattr(record, "node_kind", None) == "table_cell" and not failed:
                failed = True
                raise sqlite3.IntegrityError("injected child failure")
            return cast("Callable[[object, object], object]", original_persist)(self, record)

        monkeypatch.setattr(capture_module.EvidenceLedger, "persist", fail_child)
    else:

        def fail_receipt(*args: object, **kwargs: object) -> None:
            del args, kwargs
            raise sqlite3.IntegrityError("injected success receipt failure")

        monkeypatch.setattr(capture_module, "end_run_owned", fail_receipt)

    conn = connect_sqlite(db_path, role=SQLiteConnectionRole.WRITER, schema_preflight=True)
    conn.row_factory = sqlite3.Row
    try:
        with pytest.raises(sqlite3.IntegrityError, match="injected"):
            ingest_spreadsheet_kpis(conn, "CO", cfg, parsed, path, repo_root=repo_root)
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM evidence_extraction_runs WHERE extractor_name=?",
                ("ir-spreadsheet-parser-capture",),
            ).fetchone()[0]
            == 0
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM evidence_nodes WHERE extraction_run_id LIKE 'ir-spreadsheet-capture:%'"
            ).fetchone()[0]
            == 0
        )
        assert conn.execute("SELECT COUNT(*) FROM kpi_facts").fetchone()[0] == 0
        attempt = conn.execute(
            "SELECT status FROM ingestion_runs WHERE directive=?",
            ("capture_ir_spreadsheet",),
        ).fetchone()
        assert attempt is not None and str(attempt[0]) == "failed"
        monkeypatch.undo()
        inserted, _ = ingest_spreadsheet_kpis(conn, "CO", cfg, parsed, path, repo_root=repo_root)
        assert inserted == 3
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM evidence_extraction_runs WHERE extractor_name=?",
                ("ir-spreadsheet-parser-capture",),
            ).fetchone()[0]
            == 1
        )
        attempts = conn.execute(
            "SELECT status FROM ingestion_runs WHERE directive=? ORDER BY started_at",
            ("capture_ir_spreadsheet",),
        ).fetchall()
        assert [str(row[0]) for row in attempts] == ["failed", "ok"]
    finally:
        conn.close()


def test_typed_ingest_preserves_per_period_failure_and_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _conn()
    path = tmp_path / "co.xlsx"
    _write_sheet(path)
    cfg = IrConfig(
        ticker="CO",
        platform="mz",
        results_center_url="",
        reporting_currency="USD",
        spreadsheet_kpis=(SheetKpi("NII", "Indicators", "NII", "actual"),),
    )
    parsed = parse_spreadsheet_source(path, cfg)
    original = ingest_module.persist_manifest
    calls = 0

    def fail_second(
        connection: sqlite3.Connection,
        *,
        run_id: str,
        manifest: ingest_module.KpiExtractionManifest,
        commit: bool = True,
    ) -> object:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("second period failed")
        return original(connection, run_id=run_id, manifest=manifest, commit=commit)

    monkeypatch.setattr(ingest_module, "persist_manifest", fail_second)
    with pytest.raises(RuntimeError, match="second period failed"):
        ingest_spreadsheet_kpis(conn, "CO", cfg, parsed, path)
    assert conn.execute("SELECT COUNT(*) FROM kpi_facts").fetchone()[0] == 1
    assert not conn.in_transaction
    assert conn.execute("SELECT status FROM ingestion_runs").fetchone()[0] == "failed"
    monkeypatch.setattr(ingest_module, "persist_manifest", original)
    inserted, _ = ingest_spreadsheet_kpis(conn, "CO", cfg, parsed, path)
    assert inserted == 2
    assert conn.execute("SELECT COUNT(*) FROM kpi_facts").fetchone()[0] == 3


@pytest.mark.parametrize("typed", [False, True])
def test_ingest_resolves_each_configured_spec_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, typed: bool
) -> None:
    """Resolution includes absent and overwritten specs; selection keeps whole cells."""
    from models.facts import Unit

    conn = _conn()
    path = tmp_path / "co.xlsx"
    _write_sheet(path)
    conn.execute(
        "INSERT INTO kpi_definitions (ticker,name,unit,primary_source) "
        "VALUES ('CO','NII (USD)','actual','ir_doc')"
    )
    conn.commit()
    cfg = IrConfig(
        ticker="CO",
        platform="mz",
        results_center_url="",
        reporting_currency="USD",
        spreadsheet_kpis=(
            SheetKpi("NII", "Indicators", "Fee Revenue", "actual", origin="capture"),
            SheetKpi("NII (USD)", "Indicators", "NII", "actual"),
            SheetKpi("NII", "Indicators", "Total Available Funding", "actual", origin="capture"),
            SheetKpi("Absent", "Missing", "Absent", "percent", origin="capture"),
        ),
    )
    parsed = parse_spreadsheet_source(path, cfg, max_quarters=1)
    resolved: list[tuple[str, Unit]] = []
    units: list[str] = []
    original = ingest_module.canonical_metric_name
    original_unit: Callable[[str], Unit] = getattr(ingest_module, "_unit")

    def resolve(connection: sqlite3.Connection, ticker: str, name: str, unit: Unit) -> str:
        resolved.append((name, unit))
        return original(connection, ticker, name, unit)

    def resolve_unit(raw: str) -> Unit:
        units.append(raw)
        return original_unit(raw)

    monkeypatch.setattr(ingest_module, "canonical_metric_name", resolve)
    monkeypatch.setattr(ingest_module, "_unit", resolve_unit)
    try:
        inserted, _ = ingest_spreadsheet_kpis(
            conn, "CO", cfg, parsed if typed else parsed.numeric_projection(), path
        )
        assert resolved == [("NII", Unit.ACTUAL), ("NII", Unit.ACTUAL), ("Absent", Unit.PERCENT)]
        assert units == [spec.unit for spec in cfg.spreadsheet_kpis]
        assert inserted == 1
        definition = _defs(conn, "CO")["NII (USD)"]
        assert definition["definition_origin"] == "analyst"
        fact = _facts(conn, definition["id"])[0]
        assert float(fact["value"]) == 120.0
        if typed:
            assert json.loads(fact["locator"])["spreadsheet_cell"] == {
                "sheet_name": "Indicators",
                "cell_address": "E6",
            }
    finally:
        conn.close()


@pytest.mark.parametrize("currency", [None, "ZZZ"])
def test_ingest_validates_overwritten_absent_spec_currency(
    tmp_path: Path, currency: str | None
) -> None:
    conn = _conn()
    path = tmp_path / "co.xlsx"
    _write_sheet(path)
    cfg = IrConfig(
        ticker="CO",
        platform="mz",
        results_center_url="",
        spreadsheet_kpis=(
            SheetKpi("Absent", "Missing", "Absent", "actual", currency=currency),
            SheetKpi("Absent", "Missing", "Absent", "percent"),
            SheetKpi("NII", "Indicators", "NII", "actual", currency="USD"),
        ),
    )
    try:
        with pytest.raises(ValueError, match=r"no explicit currency|unsupported currency"):
            ingest_spreadsheet_kpis(conn, "CO", cfg, parse_spreadsheet_source(path, cfg), path)
        assert conn.execute("SELECT COUNT(*) FROM kpi_facts").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM ingestion_runs").fetchone()[0] == 0
    finally:
        conn.close()


def test_ingest_resolves_all_specs_before_currency_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from models.facts import Unit

    conn = _conn()
    path = tmp_path / "co.xlsx"
    _write_sheet(path)
    cfg = IrConfig(
        ticker="CO",
        platform="mz",
        results_center_url="",
        spreadsheet_kpis=(
            SheetKpi("NII", "Indicators", "NII", "actual"),
            SheetKpi("Later", "Missing", "Later", "percent", origin="capture"),
        ),
    )

    def fail_resolution(connection: sqlite3.Connection, ticker: str, name: str, unit: Unit) -> str:
        raise sqlite3.DatabaseError("synthetic definition resolution failure")

    monkeypatch.setattr(ingest_module, "canonical_metric_name", fail_resolution)
    try:
        with pytest.raises(sqlite3.DatabaseError, match="synthetic definition resolution failure"):
            ingest_spreadsheet_kpis(conn, "CO", cfg, parse_spreadsheet_source(path, cfg), path)
        assert conn.execute("SELECT COUNT(*) FROM kpi_facts").fetchone()[0] == 0
    finally:
        conn.close()


def test_ingest_rejects_conflicting_currency_after_canonical_merge(tmp_path: Path) -> None:
    conn = _conn()
    path = tmp_path / "co.xlsx"
    _write_sheet(path)
    conn.execute(
        "INSERT INTO kpi_definitions (ticker,name,unit,primary_source) "
        "VALUES ('CO','NII (USD)','actual','ir_doc')"
    )
    conn.commit()
    cfg = IrConfig(
        ticker="CO",
        platform="mz",
        results_center_url="",
        spreadsheet_kpis=(
            SheetKpi("NII", "Indicators", "NII", "actual", origin="capture", currency="USD"),
            SheetKpi("NII (USD)", "Indicators", "NII", "actual", currency="EUR"),
        ),
    )
    try:
        with pytest.raises(ValueError, match="conflicting currencies"):
            ingest_spreadsheet_kpis(conn, "CO", cfg, parse_spreadsheet_source(path, cfg), path)
        assert conn.execute("SELECT COUNT(*) FROM kpi_facts").fetchone()[0] == 0
    finally:
        conn.close()


def test_failed_period_rolls_back_partial_writes_before_failure_accounting(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    """A real second-value constraint failure cannot commit the first pending value."""
    repo_root = tmp_path / "repo"
    db_path = migrated_db(repo_root / "data" / "portfolio.db")
    path = repo_root / "co.xlsx"
    _write_sheet(path)
    cfg = IrConfig(
        ticker="CO",
        platform="mz",
        results_center_url="",
        reporting_currency="USD",
        spreadsheet_kpis=(
            SheetKpi("NII", "Indicators", "NII", "actual"),
            SheetKpi("Fee Revenue", "Indicators", "Fee Revenue", "actual"),
        ),
    )
    parsed = parse_spreadsheet_source(path, cfg)
    conn = connect_sqlite(db_path, role=SQLiteConnectionRole.WRITER, schema_preflight=True)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute(
            "CREATE TRIGGER fail_second_value BEFORE INSERT ON kpi_facts "
            "WHEN NEW.period_end LIKE '2025-09-30%' AND NEW.kpi_definition_id IN "
            "(SELECT id FROM kpi_definitions WHERE name='Fee Revenue') "
            "BEGIN SELECT RAISE(ABORT, 'synthetic second KPI failure'); END"
        )
        conn.commit()
        with pytest.raises(sqlite3.IntegrityError, match="synthetic second KPI failure"):
            ingest_spreadsheet_kpis(conn, "CO", cfg, parsed, path, repo_root=repo_root)
        retained = [
            tuple(row)
            for row in conn.execute("SELECT id,value,period_end,locator FROM kpi_facts ORDER BY id")
        ]
        assert len(retained) == 2
        assert {str(row[2])[:10] for row in retained} == {"2025-06-30"}
        assert not conn.in_transaction
        runs = conn.execute(
            "SELECT directive,status,error_summary FROM ingestion_runs ORDER BY started_at"
        ).fetchall()
        capture_run = next(row for row in runs if row["directive"] == "capture_ir_spreadsheet")
        failed_period_run = next(row for row in runs if row["status"] == "failed")
        assert capture_run["status"] == "ok"
        assert "synthetic second KPI failure" in failed_period_run["error_summary"]
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM fact_observation_revisions WHERE fact_table='kpi_facts'"
            ).fetchone()[0]
            == 2
        )
        conn.execute("DROP TRIGGER fail_second_value")
        conn.commit()
        inserted, _ = ingest_spreadsheet_kpis(conn, "CO", cfg, parsed, path, repo_root=repo_root)
        assert inserted == 4
        assert [
            tuple(row)
            for row in conn.execute(
                "SELECT id,value,period_end,locator FROM kpi_facts ORDER BY id LIMIT 2"
            )
        ] == retained
        assert conn.execute("SELECT COUNT(*) FROM kpi_facts").fetchone()[0] == 6
        assert [
            row[0]
            for row in conn.execute(
                "SELECT status FROM ingestion_runs WHERE directive!='capture_ir_spreadsheet' "
                "ORDER BY rowid"
            )
        ] == ["failed", "ok"]
    finally:
        conn.close()


def test_refresh_cli_carries_actual_parser_locator_to_existing_writer(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import argparse
    from types import SimpleNamespace

    from execution import refresh_ir_kpis

    repo_root = tmp_path / "repo"
    db_path = migrated_db(repo_root / "data" / "portfolio.db")
    path = repo_root / "co.xlsx"
    _write_sheet(path)
    cfg = IrConfig(
        ticker="CO",
        platform="mz",
        results_center_url="",
        reporting_currency="USD",
        spreadsheet_kpis=(SheetKpi("NII", "Indicators", "NII", "actual"),),
    )
    args = argparse.Namespace(
        ticker="CO",
        quarters=1,
        file=path,
        url=None,
        discover=False,
        platform=None,
        results_center_url=None,
        repo_root=repo_root,
        db=db_path,
        owner_requested=True,
    )
    monkeypatch.setattr(refresh_ir_kpis, "_parse_args", lambda: args)

    def authorized(*_args: object, **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(allowed=True)

    def configured(*_args: object) -> IrConfig:
        return cfg

    def widened(config: IrConfig, _path: Path) -> IrConfig:
        return config

    def saved(*_args: object) -> None:
        pass

    monkeypatch.setattr(refresh_ir_kpis, "authorize_stored_collection_target", authorized)
    monkeypatch.setattr(refresh_ir_kpis, "get_config", configured)
    monkeypatch.setattr("ir_pipeline.config_builder.widen_config", widened)
    monkeypatch.setattr(refresh_ir_kpis, "save_config", saved)
    assert refresh_ir_kpis.main() == 0
    output = json.loads(capsys.readouterr().out)
    assert output["rows_inserted"] == 1 and output["kpis"] == {"NII": 1}
    with sqlite3.connect(db_path) as conn:
        fact = conn.execute("SELECT value,locator FROM kpi_facts").fetchone()
        source_sha = conn.execute(
            "SELECT sha256 FROM documents WHERE id=?", (output["doc_id"],)
        ).fetchone()[0]
    assert float(fact[0]) == 120
    assert json.loads(fact[1])["spreadsheet_cell"] == {
        "sheet_name": "Indicators",
        "cell_address": "E6",
    }
    assert source_sha == hashlib.sha256(path.read_bytes()).hexdigest()
