from __future__ import annotations

import datetime as dt
import json
import math
import sqlite3
import struct
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import cast

import openpyxl
import pytest

from ir_pipeline.config import IrConfig, SheetKpi
from ir_pipeline.ingest import ingest_spreadsheet_kpis
from ir_pipeline.spreadsheet import parse_spreadsheet_source
from provenance.evidence_ledger import EvidenceLocator
from runtime.deterministic import canonical_json, content_sha256
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite


def _workbook(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    assert sheet is not None
    sheet.title = "Indicators"
    for column, quarter in enumerate((dt.datetime(2025, 6, 30), dt.datetime(2025, 9, 30)), start=3):
        sheet.cell(4, column, quarter)
    sheet.cell(5, 2, "NII")
    sheet.cell(5, 3, 100)
    sheet.cell(5, 4, 120)
    workbook.save(path)
    workbook.close()


def _remove_table_triggers(conn: sqlite3.Connection, table: str) -> None:
    trigger_names = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name=?", (table,)
    ).fetchall()
    for row in trigger_names:
        name = str(row[0]).replace('"', '""')
        conn.execute(f'DROP TRIGGER "{name}"')


def test_stored_capture_reader_reconstructs_without_a_new_parse(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    repo_root = tmp_path / "repo"
    db_path = migrated_db(repo_root / "data" / "portfolio.db")
    path = repo_root / "co.xlsx"
    _workbook(path)
    config = IrConfig(
        ticker="CO",
        platform="mz",
        results_center_url="",
        reporting_currency="USD",
        spreadsheet_kpis=(SheetKpi("NII", "Indicators", "NII", "actual"),),
    )
    parsed = parse_spreadsheet_source(path, config, max_quarters=1)
    conn = connect_sqlite(db_path, role=SQLiteConnectionRole.WRITER, schema_preflight=True)
    conn.row_factory = sqlite3.Row
    try:
        ingest_spreadsheet_kpis(conn, "CO", config, parsed, path, repo_root=repo_root)
        run_id = str(
            conn.execute(
                "SELECT extraction_run_id FROM evidence_extraction_runs "
                "WHERE extractor_name='ir-spreadsheet-parser-capture'"
            ).fetchone()[0]
        )
        from ir_pipeline.spreadsheet_capture import read_spreadsheet_capture

        stored = read_spreadsheet_capture(conn, run_id)
        assert stored.extraction_run_id == run_id
        assert stored.config == config
        assert stored.parsed == parsed
        assert stored.replayed is True
    finally:
        conn.close()


def test_reader_round_trips_lossless_typed_values_and_unavailable_state(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    repo_root = tmp_path / "repo"
    db_path = migrated_db(repo_root / "data" / "portfolio.db")
    path = repo_root / "co.xlsx"
    _workbook(path)
    config = IrConfig(
        ticker="CO",
        platform="mz",
        results_center_url="",
        reporting_currency="USD",
        spreadsheet_kpis=(SheetKpi("NII", "Indicators", "NII", "actual"),),
    )
    parsed = parse_spreadsheet_source(path, config, max_quarters=1)
    selected = next(cell for cell in parsed.cells if cell.disposition == "selected")
    zone = dt.timezone(dt.timedelta(hours=-7))
    values: tuple[object, ...] = (
        True,
        2**70 + 37,
        -0.0,
        float("nan"),
        float("inf"),
        "東京🙂",
        None,
        dt.datetime(2025, 11, 2, 1, 30, 0, 123456, tzinfo=zone, fold=1),
        dt.date(2020, 2, 29),
        dt.time(1, 2, 3, 456789, tzinfo=zone, fold=1),
        dt.timedelta(days=-17, seconds=3, microseconds=8),
    )
    special_cells = tuple(
        replace(selected, raw_value=value, scaled_value=None, disposition="nonnumeric")
        for value in values
    )
    supported_blank = replace(selected, raw_value=None, scaled_value=None, disposition="nonnumeric")
    unavailable = replace(
        selected,
        raw_value=None,
        scaled_value=None,
        disposition="unsupported_value",
        representation="unavailable_openpyxl_type",
    )
    inline_missing = replace(
        selected,
        locator=None,
        header=None,
        period_end=None,
        raw_value=None,
        scaled_value=None,
        disposition="header_missing",
    )
    capture_input = replace(
        parsed,
        cells=(selected, *special_cells, supported_blank, unavailable, inline_missing),
    )
    conn = connect_sqlite(db_path, role=SQLiteConnectionRole.WRITER, schema_preflight=True)
    conn.row_factory = sqlite3.Row
    try:
        ingest_spreadsheet_kpis(conn, "CO", config, capture_input, path, repo_root=repo_root)
        run_id = str(
            conn.execute(
                "SELECT extraction_run_id FROM evidence_extraction_runs "
                "WHERE extractor_name='ir-spreadsheet-parser-capture'"
            ).fetchone()[0]
        )
        from ir_pipeline.spreadsheet_capture import read_spreadsheet_capture

        stored = read_spreadsheet_capture(conn, run_id)
        actual = [cell.raw_value for cell in stored.parsed.cells[1 : 1 + len(values)]]
        assert type(actual[0]) is bool and actual[0] is True
        assert type(actual[1]) is int and actual[1] == values[1]
        assert struct.pack(">d", cast("float", actual[2])) == struct.pack(">d", -0.0)
        assert math.isnan(cast("float", actual[3]))
        assert math.isinf(cast("float", actual[4]))
        assert actual[5] == values[5]
        assert actual[6] is None
        assert actual[7] == values[7]
        assert type(actual[8]) is dt.date and actual[8] == values[8]
        assert actual[9] == values[9] and cast("dt.time", actual[9]).fold == 1
        assert actual[10] == values[10]
        assert stored.parsed.cells[-3].representation == "openpyxl_data_only"
        assert stored.parsed.cells[-3].raw_value is None
        assert stored.parsed.cells[-2].representation == "unavailable_openpyxl_type"
        assert stored.parsed.cells[-2].raw_value is None
        assert stored.parsed.cells[-1].disposition == "header_missing"
        assert stored.node_ids[-1] != f"{run_id}:cell:{len(capture_input.cells) - 1}"
    finally:
        conn.close()


def test_empty_capture_is_stored_and_read_as_an_empty_complete_graph(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    repo_root = tmp_path / "repo"
    db_path = migrated_db(repo_root / "data" / "portfolio.db")
    path = repo_root / "co.xlsx"
    _workbook(path)
    config = IrConfig(
        ticker="CO",
        platform="mz",
        results_center_url="",
        reporting_currency="USD",
        spreadsheet_kpis=(SheetKpi("NII", "Indicators", "NII", "actual"),),
    )
    parsed = replace(parse_spreadsheet_source(path, config), cells=(), configured_name_order=())
    conn = connect_sqlite(db_path, role=SQLiteConnectionRole.WRITER, schema_preflight=True)
    conn.row_factory = sqlite3.Row
    try:
        ingest_spreadsheet_kpis(conn, "CO", config, parsed, path, repo_root=repo_root)
        run_id = str(
            conn.execute(
                "SELECT extraction_run_id FROM evidence_extraction_runs "
                "WHERE extractor_name='ir-spreadsheet-parser-capture'"
            ).fetchone()[0]
        )
        from ir_pipeline.spreadsheet_capture import read_spreadsheet_capture

        stored = read_spreadsheet_capture(conn, run_id)
        assert stored.parsed.cells == ()
        assert stored.node_ids == (f"{run_id}:root",)
        assert conn.execute("SELECT COUNT(*) FROM kpi_facts").fetchone()[0] == 0
    finally:
        conn.close()


@pytest.mark.parametrize(
    "corruption",
    [
        "missing_child",
        "extra_node",
        "noncanonical_root",
        "locator_hash",
        "child_payload",
        "wrong_parent",
        "outcome",
        "naive_clock",
    ],
)
def test_stored_reader_fails_closed_on_graph_corruption(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    corruption: str,
) -> None:
    repo_root = tmp_path / "repo"
    db_path = migrated_db(repo_root / "data" / "portfolio.db")
    path = repo_root / "co.xlsx"
    _workbook(path)
    config = IrConfig(
        ticker="CO",
        platform="mz",
        results_center_url="",
        reporting_currency="USD",
        spreadsheet_kpis=(SheetKpi("NII", "Indicators", "NII", "actual"),),
    )
    parsed = parse_spreadsheet_source(path, config, max_quarters=1)
    conn = connect_sqlite(db_path, role=SQLiteConnectionRole.WRITER, schema_preflight=True)
    conn.row_factory = sqlite3.Row
    try:
        ingest_spreadsheet_kpis(conn, "CO", config, parsed, path, repo_root=repo_root)
        run_id = str(
            conn.execute(
                "SELECT extraction_run_id FROM evidence_extraction_runs "
                "WHERE extractor_name='ir-spreadsheet-parser-capture'"
            ).fetchone()[0]
        )
        root_id = f"{run_id}:root"
        child = conn.execute(
            "SELECT node_id FROM evidence_nodes "
            "WHERE extraction_run_id=? AND node_kind='table_cell' ORDER BY node_id LIMIT 1",
            (run_id,),
        ).fetchone()
        assert child is not None
        child_id = str(child[0])
        sibling = conn.execute(
            "SELECT node_id FROM evidence_nodes WHERE extraction_run_id=? "
            "AND node_kind='table_cell' AND node_id!=? ORDER BY node_id LIMIT 1",
            (run_id, child_id),
        ).fetchone()
        _remove_table_triggers(conn, "evidence_nodes")
        _remove_table_triggers(conn, "evidence_extraction_runs")
        if corruption == "missing_child":
            conn.execute("DELETE FROM evidence_nodes WHERE node_id=?", (child_id,))
        elif corruption == "extra_node":
            conn.execute(
                "INSERT INTO evidence_nodes "
                "(node_id,evidence_key,revision,extraction_run_id,parent_node_id,node_kind,text,"
                "locator_json,locator_sha256,recorded_at) "
                "SELECT node_id||':extra',evidence_key||':extra',revision,extraction_run_id,"
                "parent_node_id,node_kind,text,locator_json,locator_sha256,recorded_at "
                "FROM evidence_nodes WHERE node_id=?",
                (child_id,),
            )
        elif corruption == "noncanonical_root":
            conn.execute("UPDATE evidence_nodes SET text=text||' ' WHERE node_id=?", (root_id,))
        elif corruption == "locator_hash":
            conn.execute(
                "UPDATE evidence_nodes SET locator_sha256=? WHERE node_id=?",
                ("0" * 64, child_id),
            )
        elif corruption == "child_payload":
            conn.execute("UPDATE evidence_nodes SET text=text||' ' WHERE node_id=?", (child_id,))
        elif corruption == "wrong_parent":
            assert sibling is not None
            conn.execute(
                "UPDATE evidence_nodes SET parent_node_id=? WHERE node_id=?",
                (str(sibling[0]), child_id),
            )
        elif corruption == "outcome":
            conn.execute(
                "UPDATE evidence_extraction_runs SET outcome='failed' WHERE extraction_run_id=?",
                (run_id,),
            )
        else:
            conn.execute(
                "UPDATE evidence_extraction_runs SET started_at='2026-01-01 00:00:00' "
                "WHERE extraction_run_id=?",
                (run_id,),
            )
        with pytest.raises(ValueError, match="capture"):
            from ir_pipeline.spreadsheet_capture import read_spreadsheet_capture

            read_spreadsheet_capture(conn, run_id)
        if corruption == "missing_child":
            before_retry_nodes = conn.execute(
                "SELECT node_id FROM evidence_nodes WHERE extraction_run_id=? ORDER BY node_id",
                (run_id,),
            ).fetchall()
            with pytest.raises(ValueError, match="capture"):
                ingest_spreadsheet_kpis(conn, "CO", config, parsed, path, repo_root=repo_root)
            after_retry_nodes = conn.execute(
                "SELECT node_id FROM evidence_nodes WHERE extraction_run_id=? ORDER BY node_id",
                (run_id,),
            ).fetchall()
            assert [tuple(row) for row in before_retry_nodes] == [
                tuple(row) for row in after_retry_nodes
            ]
            assert all(str(row[0]) != child_id for row in after_retry_nodes)
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM evidence_nodes WHERE node_id=?", (root_id,)
            ).fetchone()[0]
            == 1
        )
    finally:
        conn.close()


def test_stored_reader_rejects_unknown_header_fields_with_valid_commitments(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    repo_root = tmp_path / "repo"
    db_path = migrated_db(repo_root / "data" / "portfolio.db")
    path = repo_root / "co.xlsx"
    _workbook(path)
    config = IrConfig(
        ticker="CO",
        platform="mz",
        results_center_url="",
        reporting_currency="USD",
        spreadsheet_kpis=(SheetKpi("NII", "Indicators", "NII", "actual"),),
    )
    parsed = parse_spreadsheet_source(path, config, max_quarters=1)
    conn = connect_sqlite(db_path, role=SQLiteConnectionRole.WRITER, schema_preflight=True)
    conn.row_factory = sqlite3.Row
    try:
        ingest_spreadsheet_kpis(conn, "CO", config, parsed, path, repo_root=repo_root)
        run_id = str(
            conn.execute(
                "SELECT extraction_run_id FROM evidence_extraction_runs "
                "WHERE extractor_name='ir-spreadsheet-parser-capture'"
            ).fetchone()[0]
        )
        child_row = conn.execute(
            "SELECT node_id,text,locator_json,locator_sha256 FROM evidence_nodes "
            "WHERE extraction_run_id=? AND node_kind='table_cell' ORDER BY node_id LIMIT 1",
            (run_id,),
        ).fetchone()
        assert child_row is not None and child_row[2] is not None
        node_id, old_text, locator_json, locator_sha256 = map(str, child_row)
        child_payload = json.loads(old_text)
        assert child_payload["header"] is not None
        child_payload["header"]["unrecognized_metadata"] = "must reject"
        child_text = canonical_json(child_payload)
        locator = EvidenceLocator.model_validate(json.loads(locator_json))
        ordinal = int(node_id.rsplit(":", 1)[1])
        child_digest = content_sha256(
            {
                "node_id": node_id,
                "extraction_run_id": run_id,
                "parent_ordinal": 0,
                "ordinal": ordinal,
                "node_kind": "table_cell",
                "text": child_text,
                "locator_json": locator.canonical_json,
                "locator_sha256": locator_sha256,
            }
        )
        root_id = f"{run_id}:root"
        root_text = str(
            conn.execute("SELECT text FROM evidence_nodes WHERE node_id=?", (root_id,)).fetchone()[
                0
            ]
        )
        root_payload = json.loads(root_text)
        manifest_entry = next(
            entry for entry in root_payload["selection_manifest"] if entry.get("node_id") == node_id
        )
        manifest_entry["sha256"] = child_digest
        new_root_text = canonical_json(root_payload)
        child_hashes = [
            entry["sha256"]
            for entry in root_payload["selection_manifest"]
            if entry["storage"] == "node"
        ]
        output_digest = content_sha256(
            {"root_node_id": root_id, "root_text": new_root_text, "children": child_hashes}
        )
        _remove_table_triggers(conn, "evidence_nodes")
        _remove_table_triggers(conn, "evidence_extraction_runs")
        conn.execute("UPDATE evidence_nodes SET text=? WHERE node_id=?", (child_text, node_id))
        conn.execute("UPDATE evidence_nodes SET text=? WHERE node_id=?", (new_root_text, root_id))
        conn.execute(
            "UPDATE evidence_extraction_runs SET output_sha256=? WHERE extraction_run_id=?",
            (output_digest, run_id),
        )
        from ir_pipeline.spreadsheet_capture import read_spreadsheet_capture

        with pytest.raises(ValueError, match="header"):
            read_spreadsheet_capture(conn, run_id)
    finally:
        conn.close()
