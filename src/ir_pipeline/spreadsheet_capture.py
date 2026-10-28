"""Capture and recover one typed IR spreadsheet parse in the evidence ledger."""

from __future__ import annotations

import json
import re
import sqlite3
import struct
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import cast

from ir_pipeline.config import IrConfig, SheetKpi
from ir_pipeline.spreadsheet import (
    SpreadsheetCellDisposition,
    SpreadsheetCellRead,
    SpreadsheetParseResult,
    SpreadsheetValue,
)
from models.facts import FactLocator, LocatorKind, SpreadsheetCellRef
from models.runs import StageStatus
from pipeline.invocation_fingerprint import payload_sha256
from pipeline.run_accounting import (
    JsonValue,
    assert_attempt_owned,
    end_run,
    end_run_owned,
    make_pipeline_key,
    start_run,
)
from provenance.evidence_ledger import EvidenceLedger, EvidenceLocator, EvidenceNode, ExtractionRun
from runtime.deterministic import canonical_json, content_sha256

_EXTRACTOR_NAME = "ir-spreadsheet-parser-capture"
_CAPTURE_SCHEMA = "ir-spreadsheet-parser-capture.v1"
_EFFECTIVE_CONFIG_SCHEMA = "ir-spreadsheet-effective-config.v1"
_ATTEMPT_DIRECTIVE = "capture_ir_spreadsheet"
_DISPOSITIONS = frozenset(
    {
        "selected",
        "nonnumeric",
        "unsupported_value",
        "unknown_period",
        "outside_window",
        "overwritten_period",
        "overwritten_configured_name",
        "sheet_missing",
        "header_missing",
        "row_missing",
    }
)
_INT_RE = re.compile(r"(?:0|-?[1-9][0-9]*)\Z")


@dataclass(frozen=True)
class SpreadsheetCaptureReceipt:
    extraction_run_id: str
    node_ids: tuple[str, ...]
    started_at: datetime
    completed_at: datetime
    replayed: bool
    config: IrConfig
    parsed: SpreadsheetParseResult


def capture_spreadsheet_parse(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    issuer_id: str,
    document_version_id: str,
    config: IrConfig,
    parsed: SpreadsheetParseResult,
) -> SpreadsheetCaptureReceipt:
    """Persist or independently verify a complete parse graph under one attempt."""
    if ticker.upper() != config.ticker.upper():
        raise ValueError("spreadsheet capture ticker differs from its effective config")
    if (
        parsed.parser_name != "ir_pipeline.spreadsheet"
        or parsed.parser_version != "source-handoff.v1"
        or type(parsed.max_quarters) is not int
        or parsed.max_quarters <= 0
        or not _is_sha256(parsed.workbook_sha256)
        or type(parsed.workbook_bytes_size) is not int
        or parsed.workbook_bytes_size <= 0
        or parsed.config_sha256 != payload_sha256(asdict(config))
    ):
        raise ValueError("spreadsheet capture parse identity or config is invalid")
    _validate_document_binding(
        conn,
        document_version_id=document_version_id,
        issuer_id=issuer_id,
        ticker=ticker,
        source_sha256=parsed.workbook_sha256,
        source_bytes_size=parsed.workbook_bytes_size,
    )
    config_value = asdict(config)
    effective_config_sha256 = _effective_config_sha256(
        cast("dict[str, JsonValue]", config_value), parsed.max_quarters
    )
    identity = {
        "issuer_id": issuer_id,
        "document_version_id": document_version_id,
        "source_sha256": parsed.workbook_sha256,
        "extractor_name": _EXTRACTOR_NAME,
        "parser_name": parsed.parser_name,
        "parser_version": parsed.parser_version,
        "effective_config_sha256": effective_config_sha256,
    }
    extraction_run_id = f"ir-spreadsheet-capture:{payload_sha256(identity)}"
    attempt_inputs = {
        "issuer_id": issuer_id,
        "document_version_id": document_version_id,
        "source_sha256": parsed.workbook_sha256,
        "effective_config_sha256": effective_config_sha256,
        "parser_name": parsed.parser_name,
        "parser_version": parsed.parser_version,
    }
    attempt_key = make_pipeline_key(_ATTEMPT_DIRECTIVE, [ticker], attempt_inputs)
    attempt_id = start_run(
        conn,
        _ATTEMPT_DIRECTIVE,
        [ticker],
        invocation_inputs=attempt_inputs,
        deduplicate_completed=False,
    )

    try:
        conn.execute("BEGIN IMMEDIATE")
        assert_attempt_owned(conn, run_id=attempt_id, pipeline_key=attempt_key)
        extraction_run, root, children = _capture_records(
            extraction_run_id=extraction_run_id,
            document_version_id=document_version_id,
            issuer_id=issuer_id,
            config_value=config_value,
            effective_config_sha256=effective_config_sha256,
            parsed=parsed,
        )
        created = _persist_or_verify(
            conn,
            extraction_run=extraction_run,
            root=root,
            children=children,
            config=config,
        )
        stored = read_spreadsheet_capture(conn, extraction_run_id)
        end_run_owned(
            conn,
            run_id=attempt_id,
            pipeline_key=attempt_key,
            status=StageStatus.OK,
        )
        conn.commit()
        return SpreadsheetCaptureReceipt(
            extraction_run_id=stored.extraction_run_id,
            node_ids=stored.node_ids,
            started_at=stored.started_at,
            completed_at=stored.completed_at,
            replayed=not created,
            config=stored.config,
            parsed=stored.parsed,
        )
    except Exception as exc:
        conn.rollback()
        end_run(
            conn,
            attempt_id,
            StageStatus.FAILED,
            error_summary=f"{type(exc).__name__}: spreadsheet capture failed",
        )
        raise


def read_spreadsheet_capture(
    conn: sqlite3.Connection, extraction_run_id: str
) -> SpreadsheetCaptureReceipt:
    """Validate and reconstruct a capture from stored rows without parsing a workbook."""
    run_row = conn.execute(
        "SELECT extraction_run_id,idempotency_key,document_version_id,input_sha256,"
        "extractor_name,extractor_config_sha256,extractor_code_version,output_sha256,"
        "started_at,completed_at,outcome FROM evidence_extraction_runs "
        "WHERE extraction_run_id=?",
        (extraction_run_id,),
    ).fetchone()
    if run_row is None:
        raise ValueError("spreadsheet capture extraction run is missing")
    run = _row(run_row)
    if (
        run[0] != extraction_run_id
        or run[1] != extraction_run_id
        or run[4] != _EXTRACTOR_NAME
        or run[10] != "succeeded"
    ):
        raise ValueError("spreadsheet capture extraction run identity is invalid")
    document_version_id = _string(run[2], "run document version")
    source_sha256 = _string(run[3], "run input hash")
    if not _is_sha256(source_sha256):
        raise ValueError("spreadsheet capture input hash is invalid")
    completed_at = _valid_run_clocks(run[8], run[9])
    started_at = _parse_aware_datetime(run[8])
    if started_at is None:
        raise ValueError("spreadsheet capture extraction run start clock is invalid")

    document = conn.execute(
        "SELECT document.issuer_id,document.blob_sha256,document.ticker,blob.byte_size "
        "FROM evidence_document_versions AS document "
        "JOIN evidence_content_blobs AS blob ON blob.sha256=document.blob_sha256 "
        "WHERE document.document_version_id=?",
        (document_version_id,),
    ).fetchone()
    if document is None:
        raise ValueError("spreadsheet capture document version is missing")
    issuer_id, blob_sha256, ticker = (
        _string(item, "document version field") for item in _row(document)[:3]
    )
    if blob_sha256 != source_sha256:
        raise ValueError("spreadsheet capture source differs from its document version")

    nodes = conn.execute(
        "SELECT node_id,evidence_key,revision,extraction_run_id,parent_node_id,"
        "supersedes_node_id,node_kind,text,locator_json,locator_sha256,recorded_at "
        "FROM evidence_nodes WHERE extraction_run_id=?",
        (extraction_run_id,),
    ).fetchall()
    by_id = {str(_row(node)[0]): _row(node) for node in nodes}
    root_id = f"{extraction_run_id}:root"
    if len(by_id) != len(nodes) or root_id not in by_id:
        raise ValueError("spreadsheet capture root or node identity is invalid")
    root_row = by_id[root_id]
    _validate_node_row(
        root_row,
        node_id=root_id,
        extraction_run_id=extraction_run_id,
        parent_node_id=None,
        node_kind="table",
        locator=None,
        expected_text=_string(root_row[7], "root node text"),
        completed_at=completed_at,
    )
    root_text = _string(root_row[7], "root node text")
    root_value = _strict_canonical_object(root_text, "spreadsheet capture root")
    _require_keys(
        root_value,
        {
            "schema_version",
            "issuer_id",
            "document_version_id",
            "source_sha256",
            "source_bytes_size",
            "parser_name",
            "parser_version",
            "parser_config_sha256",
            "effective_config",
            "effective_config_sha256",
            "max_quarters",
            "configured_name_order",
            "cell_count",
            "located_cell_count",
            "selection_manifest",
        },
        "spreadsheet capture root",
    )
    if (
        root_value["schema_version"] != _CAPTURE_SCHEMA
        or root_value["issuer_id"] != issuer_id
        or root_value["document_version_id"] != document_version_id
        or root_value["source_sha256"] != source_sha256
        or root_value["parser_name"] != "ir_pipeline.spreadsheet"
        or root_value["parser_version"] != "source-handoff.v1"
    ):
        raise ValueError("spreadsheet capture root binding or version is invalid")
    effective_config = _object(root_value["effective_config"], "effective config")
    config = _decode_config(effective_config)
    if config.ticker.upper() != ticker.upper():
        raise ValueError("spreadsheet capture config issuer does not match its document")
    max_quarters = _positive_int(root_value["max_quarters"], "max_quarters")
    effective_config_sha256 = _effective_config_sha256(
        cast("dict[str, JsonValue]", effective_config), max_quarters
    )
    if (
        effective_config_sha256 != root_value["effective_config_sha256"]
        or effective_config_sha256 != run[5]
        or payload_sha256(asdict(config)) != root_value["parser_config_sha256"]
    ):
        raise ValueError("spreadsheet capture config digest is invalid")
    if run[6] != f"{root_value['parser_name']}:{root_value['parser_version']}":
        raise ValueError("spreadsheet capture parser version differs from its run")
    byte_size = _positive_int(root_value["source_bytes_size"], "source_bytes_size")
    if _nonnegative_int(_row(document)[3], "document byte size") != byte_size:
        raise ValueError("spreadsheet capture byte size differs from its document blob")
    expected_identity: dict[str, JsonValue] = {
        "issuer_id": issuer_id,
        "document_version_id": document_version_id,
        "source_sha256": source_sha256,
        "extractor_name": _EXTRACTOR_NAME,
        "parser_name": _string(root_value["parser_name"], "parser name"),
        "parser_version": _string(root_value["parser_version"], "parser version"),
        "effective_config_sha256": effective_config_sha256,
    }
    expected_run_id = f"ir-spreadsheet-capture:{payload_sha256(expected_identity)}"
    if expected_run_id != extraction_run_id:
        raise ValueError("spreadsheet capture extraction identity is invalid")
    if not _is_sha256(root_value["parser_config_sha256"]):
        raise ValueError("spreadsheet capture parser config digest is invalid")

    manifest = _list(root_value["selection_manifest"], "selection_manifest")
    cell_count = _nonnegative_int(root_value["cell_count"], "cell_count")
    located_count = _nonnegative_int(root_value["located_cell_count"], "located_cell_count")
    if len(manifest) != cell_count:
        raise ValueError("spreadsheet capture selection count is invalid")
    cells: list[SpreadsheetCellRead] = []
    child_ids: list[str] = []
    child_hashes: list[str] = []
    for ordinal, raw_entry in enumerate(manifest):
        entry = _object(raw_entry, "selection entry")
        if _nonnegative_int(entry.get("ordinal"), "selection ordinal") != ordinal:
            raise ValueError("spreadsheet capture selection order is invalid")
        storage = entry.get("storage")
        if storage == "inline":
            _require_keys(entry, {"ordinal", "storage", "payload"}, "inline selection")
            payload = _object(entry["payload"], "inline cell payload")
            locator = None
        elif storage == "node":
            _require_keys(entry, {"ordinal", "storage", "node_id", "sha256"}, "node selection")
            node_id = _string(entry["node_id"], "child node id")
            digest = _string(entry["sha256"], "child hash")
            if node_id != f"{extraction_run_id}:cell:{ordinal}":
                raise ValueError("spreadsheet capture child order is invalid")
            node_row = by_id.get(node_id)
            if node_row is None:
                raise ValueError("spreadsheet capture referenced child is missing")
            locator = _decode_locator(node_row[8], node_row[9], document_version_id)
            _validate_node_row(
                node_row,
                node_id=node_id,
                extraction_run_id=extraction_run_id,
                parent_node_id=root_id,
                node_kind="table_cell",
                locator=locator,
                expected_text=_string(node_row[7], "child payload"),
                completed_at=completed_at,
            )
            payload_text = _string(node_row[7], "child payload")
            payload = _strict_canonical_object(payload_text, "spreadsheet capture cell payload")
            calculated_digest = _node_sha256(
                node_id,
                extraction_run_id,
                ordinal,
                payload_text,
                locator,
            )
            if digest != calculated_digest:
                raise ValueError("spreadsheet capture child hash is invalid")
            child_ids.append(node_id)
            child_hashes.append(digest)
        else:
            raise ValueError("spreadsheet capture selection storage is invalid")
        cells.append(_decode_cell(payload, ordinal, config, locator))

    if len(child_ids) != located_count or set(by_id) != {root_id, *child_ids}:
        raise ValueError("spreadsheet capture node population is incomplete or contains extras")
    output_sha256 = content_sha256(
        {
            "root_node_id": root_id,
            "root_text": root_text,
            "children": child_hashes,
        }
    )
    if output_sha256 != run[7]:
        raise ValueError("spreadsheet capture output digest is invalid")
    configured_names = _list(root_value["configured_name_order"], "configured_name_order")
    if not all(type(name) is str for name in configured_names):
        raise ValueError("spreadsheet capture configured name order is invalid")
    if len(set(cast("list[str]", configured_names))) != len(configured_names) or any(
        name not in {spec.kpi_name for spec in config.spreadsheet_kpis}
        for name in cast("list[str]", configured_names)
    ):
        raise ValueError("spreadsheet capture configured name order is not in its config")
    parsed = SpreadsheetParseResult(
        workbook_sha256=source_sha256,
        workbook_bytes_size=byte_size,
        config_sha256=str(root_value["parser_config_sha256"]),
        max_quarters=max_quarters,
        cells=tuple(cells),
        configured_name_order=tuple(cast("list[str]", configured_names)),
        parser_name="ir_pipeline.spreadsheet",
        parser_version="source-handoff.v1",
    )
    return SpreadsheetCaptureReceipt(
        extraction_run_id=extraction_run_id,
        node_ids=(root_id, *child_ids),
        started_at=started_at,
        completed_at=completed_at,
        replayed=True,
        config=config,
        parsed=parsed,
    )


def _capture_records(
    *,
    extraction_run_id: str,
    document_version_id: str,
    issuer_id: str,
    config_value: dict[str, object],
    effective_config_sha256: str,
    parsed: SpreadsheetParseResult,
) -> tuple[ExtractionRun, EvidenceNode, tuple[EvidenceNode, ...]]:
    root_id = f"{extraction_run_id}:root"
    completed_at = datetime.now(UTC)
    selection_manifest: list[dict[str, object]] = []
    children: list[EvidenceNode] = []
    child_hashes: list[str] = []
    for ordinal, cell in enumerate(parsed.cells):
        payload = _cell_payload(cell, ordinal)
        payload_text = canonical_json(payload)
        locator = _cell_locator(document_version_id, cell)
        if locator is None:
            selection_manifest.append({"ordinal": ordinal, "storage": "inline", "payload": payload})
            continue
        node_id = f"{extraction_run_id}:cell:{ordinal}"
        digest = _node_sha256(node_id, extraction_run_id, ordinal, payload_text, locator)
        selection_manifest.append(
            {"ordinal": ordinal, "storage": "node", "node_id": node_id, "sha256": digest}
        )
        children.append(
            EvidenceNode(
                node_id=node_id,
                evidence_key=node_id,
                revision=1,
                extraction_run_id=extraction_run_id,
                parent_node_id=root_id,
                node_kind="table_cell",
                text=payload_text,
                locator=locator,
                recorded_at=completed_at,
            )
        )
        child_hashes.append(digest)
    root_value: dict[str, object] = {
        "schema_version": _CAPTURE_SCHEMA,
        "issuer_id": issuer_id,
        "document_version_id": document_version_id,
        "source_sha256": parsed.workbook_sha256,
        "source_bytes_size": parsed.workbook_bytes_size,
        "parser_name": parsed.parser_name,
        "parser_version": parsed.parser_version,
        "parser_config_sha256": parsed.config_sha256,
        "effective_config": config_value,
        "effective_config_sha256": effective_config_sha256,
        "max_quarters": parsed.max_quarters,
        "configured_name_order": list(parsed.configured_name_order),
        "cell_count": len(parsed.cells),
        "located_cell_count": len(children),
        "selection_manifest": selection_manifest,
    }
    root_text = canonical_json(root_value)
    output_sha256 = content_sha256(
        {"root_node_id": root_id, "root_text": root_text, "children": child_hashes}
    )
    extraction_run = ExtractionRun(
        extraction_run_id=extraction_run_id,
        idempotency_key=extraction_run_id,
        document_version_id=document_version_id,
        input_sha256=parsed.workbook_sha256,
        extractor_name=_EXTRACTOR_NAME,
        extractor_config_sha256=effective_config_sha256,
        extractor_code_version=f"{parsed.parser_name}:{parsed.parser_version}",
        output_sha256=output_sha256,
        started_at=completed_at,
        completed_at=completed_at,
        outcome="succeeded",
    )
    root = EvidenceNode(
        node_id=root_id,
        evidence_key=root_id,
        revision=1,
        extraction_run_id=extraction_run_id,
        node_kind="table",
        text=root_text,
        recorded_at=completed_at,
    )
    return extraction_run, root, tuple(children)


def _persist_or_verify(
    conn: sqlite3.Connection,
    *,
    extraction_run: ExtractionRun,
    root: EvidenceNode,
    children: tuple[EvidenceNode, ...],
    config: IrConfig,
) -> bool:
    existing = conn.execute(
        "SELECT output_sha256 FROM evidence_extraction_runs WHERE extraction_run_id=?",
        (extraction_run.extraction_run_id,),
    ).fetchone()
    if existing is not None:
        stored = read_spreadsheet_capture(conn, extraction_run.extraction_run_id)
        if stored.config != config or existing[0] != extraction_run.output_sha256:
            raise ValueError("spreadsheet capture existing graph conflicts with its input identity")
        return False
    existing_nodes = conn.execute(
        "SELECT node_id FROM evidence_nodes WHERE extraction_run_id=?",
        (extraction_run.extraction_run_id,),
    ).fetchall()
    if existing_nodes or any(
        conn.execute("SELECT 1 FROM evidence_nodes WHERE node_id=?", (node.node_id,)).fetchone()
        is not None
        for node in (root, *children)
    ):
        raise ValueError("spreadsheet capture graph exists without its extraction run")
    semantic = conn.execute(
        "SELECT extraction_run_id FROM evidence_extraction_runs "
        "WHERE document_version_id=? AND input_sha256=? AND extractor_name=? "
        "AND extractor_config_sha256=? AND extractor_code_version=?",
        (
            extraction_run.document_version_id,
            extraction_run.input_sha256,
            extraction_run.extractor_name,
            extraction_run.extractor_config_sha256,
            extraction_run.extractor_code_version,
        ),
    ).fetchone()
    if semantic is not None:
        raise ValueError("spreadsheet capture semantic identity already has another run")
    ledger = EvidenceLedger(conn)
    ledger.persist(extraction_run)
    ledger.persist(root)
    for child in children:
        ledger.persist(child)
    stored = read_spreadsheet_capture(conn, extraction_run.extraction_run_id)
    if stored.config != config:
        raise ValueError("spreadsheet capture graph differs from its source parse")
    return True


def _decode_cell(
    payload: dict[str, object],
    ordinal: int,
    config: IrConfig,
    locator: EvidenceLocator | None,
) -> SpreadsheetCellRead:
    _require_keys(
        payload,
        {
            "schema_version",
            "ordinal",
            "configured_spec",
            "raw_label",
            "period_end",
            "raw_value",
            "scaled_value",
            "value_representation",
            "disposition",
            "header",
        },
        "spreadsheet cell payload",
    )
    if payload["schema_version"] != _CAPTURE_SCHEMA or payload["ordinal"] != ordinal:
        raise ValueError("spreadsheet cell payload version or ordinal is invalid")
    spec = _decode_spec(_object(payload["configured_spec"], "configured spec"))
    if not any(
        canonical_json(asdict(candidate)) == canonical_json(asdict(spec))
        for candidate in config.spreadsheet_kpis
    ):
        raise ValueError("spreadsheet cell spec is not present in the effective config")
    raw_label = payload["raw_label"]
    if raw_label is not None and type(raw_label) is not str:
        raise ValueError("spreadsheet cell label is invalid")
    period_value = _decode_tagged(payload["period_end"])
    period_tag = _object(payload["period_end"], "period value").get("tag")
    if period_tag not in {"none", "datetime"}:
        raise ValueError("spreadsheet cell period tag is invalid")
    if period_value is not None and type(period_value) is not datetime:
        raise ValueError("spreadsheet cell period is not a datetime")
    raw_value = _decode_tagged(payload["raw_value"])
    representation = payload["value_representation"]
    if type(representation) is not str or representation not in {
        "openpyxl_data_only",
        "unavailable_openpyxl_type",
    }:
        raise ValueError("spreadsheet cell representation is invalid")
    raw_tag = _object(payload["raw_value"], "raw value").get("tag")
    if raw_tag == "unavailable":
        if representation != "unavailable_openpyxl_type":
            raise ValueError("unavailable spreadsheet value representation conflicts")
        raw_value = None
    elif representation != "openpyxl_data_only":
        raise ValueError("spreadsheet cell availability metadata conflicts")
    scaled_value = _decode_tagged(payload["scaled_value"])
    scaled_tag = _object(payload["scaled_value"], "scaled value").get("tag")
    if scaled_tag not in {"none", "float64"}:
        raise ValueError("spreadsheet scaled value tag is invalid")
    if scaled_value is not None and type(scaled_value) is not float:
        raise ValueError("spreadsheet scaled value is not a float")
    disposition = payload["disposition"]
    if type(disposition) is not str or disposition not in _DISPOSITIONS:
        raise ValueError("spreadsheet cell disposition is unknown")
    header_value = payload["header"]
    header: SpreadsheetCellRef | None = None
    if header_value is not None:
        try:
            header_payload = _object(header_value, "spreadsheet header")
            _require_keys(
                header_payload,
                {"sheet_name", "cell_address", "cell_range", "table_name"},
                "spreadsheet header",
            )
            header = SpreadsheetCellRef.model_validate(header_value)
        except (TypeError, ValueError) as exc:
            raise ValueError("spreadsheet header locator is invalid") from exc
        if header.model_dump(mode="json") != header_payload:
            raise ValueError("spreadsheet header locator is not a canonical round-trip")
    fact_locator: FactLocator | None = None
    if locator is not None:
        if disposition in {"sheet_missing", "header_missing", "row_missing"}:
            raise ValueError("missing spreadsheet selection has a physical locator")
        if locator.sheet_name is None:
            raise ValueError("spreadsheet selection locator has no worksheet name")
        spreadsheet_cell = SpreadsheetCellRef(
            sheet_name=locator.sheet_name,
            cell_address=locator.cell_address,
            cell_range=locator.cell_range,
        )
        fact_locator = FactLocator(
            kind=LocatorKind.SPREADSHEET_CELL,
            spreadsheet_cell=spreadsheet_cell,
        )
    elif disposition not in {"sheet_missing", "header_missing", "row_missing"}:
        raise ValueError("spreadsheet selection has no physical locator")
    validated_raw_value: SpreadsheetValue = raw_value
    return SpreadsheetCellRead(
        spec=spec,
        raw_label=raw_label,
        locator=fact_locator,
        header=header,
        period_end=period_value,
        raw_value=validated_raw_value,
        scaled_value=scaled_value,
        disposition=cast("SpreadsheetCellDisposition", disposition),
        representation=representation,
    )


def _decode_config(value: dict[str, object]) -> IrConfig:
    _require_keys(
        value,
        {
            "ticker",
            "platform",
            "results_center_url",
            "reporting_currency",
            "spreadsheet_kpis",
            "label_col",
        },
        "effective config",
    )
    specs = _list(value["spreadsheet_kpis"], "spreadsheet_kpis")
    return IrConfig(
        ticker=_string(value["ticker"], "config ticker"),
        platform=_string(value["platform"], "config platform"),
        results_center_url=_string(value["results_center_url"], "config results URL"),
        reporting_currency=_optional_string(value["reporting_currency"], "reporting currency"),
        spreadsheet_kpis=tuple(_decode_spec(_object(item, "configured spec")) for item in specs),
        label_col=_nonnegative_int(value["label_col"], "label_col"),
    )


def _decode_spec(value: dict[str, object]) -> SheetKpi:
    _require_keys(
        value,
        {"kpi_name", "sheet", "row_label", "unit", "scale", "origin", "exact_label", "currency"},
        "configured spec",
    )
    scale = value["scale"]
    if isinstance(scale, bool) or not isinstance(scale, (int, float)):
        raise ValueError("configured scale is invalid")
    exact_label = value["exact_label"]
    if type(exact_label) is not bool:
        raise ValueError("configured exact_label is invalid")
    return SheetKpi(
        kpi_name=_string(value["kpi_name"], "KPI name"),
        sheet=_string(value["sheet"], "worksheet name"),
        row_label=_string(value["row_label"], "row label"),
        unit=_string(value["unit"], "unit"),
        scale=cast("float", scale),
        origin=_string(value["origin"], "origin"),
        exact_label=exact_label,
        currency=_optional_string(value["currency"], "spec currency"),
    )


def _cell_payload(cell: SpreadsheetCellRead, ordinal: int) -> dict[str, object]:
    return {
        "schema_version": _CAPTURE_SCHEMA,
        "ordinal": ordinal,
        "configured_spec": asdict(cell.spec),
        "raw_label": cell.raw_label,
        "period_end": _tag_value(cell.period_end),
        "raw_value": _tag_value(cell.raw_value, unavailable=cell.representation),
        "scaled_value": _tag_value(cell.scaled_value),
        "value_representation": cell.representation,
        "disposition": cell.disposition,
        "header": None if cell.header is None else cell.header.model_dump(mode="json"),
    }


def _tag_value(value: object, *, unavailable: str | None = None) -> dict[str, object]:
    if unavailable == "unavailable_openpyxl_type":
        return {"tag": "unavailable", "representation": unavailable}
    if value is None:
        return {"tag": "none"}
    if isinstance(value, bool):
        return {"tag": "bool", "value": value}
    if isinstance(value, int):
        return {"tag": "int", "decimal": str(value)}
    if isinstance(value, float):
        return {"tag": "float64", "bits": struct.pack(">d", value).hex()}
    if isinstance(value, str):
        return {"tag": "str", "value": value}
    if isinstance(value, datetime):
        return {"tag": "datetime", "iso": value.isoformat(), "fold": value.fold}
    if isinstance(value, date):
        return {"tag": "date", "iso": value.isoformat()}
    if isinstance(value, time):
        return {"tag": "time", "iso": value.isoformat(), "fold": value.fold}
    if isinstance(value, timedelta):
        micros = (value.days * 86_400 + value.seconds) * 1_000_000 + value.microseconds
        return {"tag": "timedelta_microseconds", "value": str(micros)}
    raise TypeError(f"unsupported spreadsheet capture value: {type(value).__name__}")


def _decode_tagged(value: object) -> SpreadsheetValue:
    tagged = _object(value, "tagged spreadsheet value")
    tag = tagged.get("tag")
    if tag == "none":
        _require_keys(tagged, {"tag"}, "none value")
        return None
    if tag == "unavailable":
        _require_keys(tagged, {"tag", "representation"}, "unavailable value")
        if tagged["representation"] != "unavailable_openpyxl_type":
            raise ValueError("unknown unavailable value representation")
        return None
    if tag == "bool":
        _require_keys(tagged, {"tag", "value"}, "boolean value")
        if type(tagged["value"]) is not bool:
            raise ValueError("boolean value is invalid")
        return tagged["value"]
    if tag == "int":
        _require_keys(tagged, {"tag", "decimal"}, "integer value")
        decimal = _string(tagged["decimal"], "integer decimal")
        if not _INT_RE.fullmatch(decimal):
            raise ValueError("integer decimal is not canonical")
        return int(decimal)
    if tag == "float64":
        _require_keys(tagged, {"tag", "bits"}, "float value")
        bits = _string(tagged["bits"], "float bits")
        if not re.fullmatch(r"[0-9a-f]{16}", bits):
            raise ValueError("float64 bits are invalid")
        return struct.unpack(">d", bytes.fromhex(bits))[0]
    if tag == "str":
        _require_keys(tagged, {"tag", "value"}, "string value")
        return _string(tagged["value"], "string value")
    if tag == "datetime":
        _require_keys(tagged, {"tag", "iso", "fold"}, "datetime value")
        fold = _fold(tagged["fold"])
        iso_value = _string(tagged["iso"], "datetime ISO")
        parsed = datetime.fromisoformat(iso_value)
        if parsed.isoformat() != iso_value:
            raise ValueError("datetime ISO value is not canonical")
        return parsed.replace(fold=fold)
    if tag == "date":
        _require_keys(tagged, {"tag", "iso"}, "date value")
        iso_value = _string(tagged["iso"], "date ISO")
        parsed = date.fromisoformat(iso_value)
        if type(parsed) is not date or parsed.isoformat() != iso_value:
            raise ValueError("date tag contains a datetime")
        return parsed
    if tag == "time":
        _require_keys(tagged, {"tag", "iso", "fold"}, "time value")
        fold = _fold(tagged["fold"])
        iso_value = _string(tagged["iso"], "time ISO")
        parsed = time.fromisoformat(iso_value)
        if parsed.isoformat() != iso_value:
            raise ValueError("time ISO value is not canonical")
        return parsed.replace(fold=fold)
    if tag == "timedelta_microseconds":
        _require_keys(tagged, {"tag", "value"}, "timedelta value")
        micros = _string(tagged["value"], "timedelta microseconds")
        if not _INT_RE.fullmatch(micros):
            raise ValueError("timedelta microseconds are invalid")
        return timedelta(microseconds=int(micros))
    raise ValueError(f"unknown spreadsheet value tag: {tag!r}")


def _cell_locator(document_version_id: str, cell: SpreadsheetCellRead) -> EvidenceLocator | None:
    if cell.locator is None:
        return None
    spreadsheet_cell = cell.locator.spreadsheet_cell
    if spreadsheet_cell is None:
        return None
    return EvidenceLocator(
        source_ref=document_version_id,
        sheet_name=spreadsheet_cell.sheet_name,
        cell_address=spreadsheet_cell.cell_address,
        cell_range=spreadsheet_cell.cell_range,
    )


def _decode_locator(
    locator_json: object, locator_sha256: object, document_version_id: str
) -> EvidenceLocator:
    if not isinstance(locator_json, str) or not isinstance(locator_sha256, str):
        raise ValueError("spreadsheet capture child locator is missing")
    raw: object
    try:
        raw = json.loads(locator_json)
        locator = EvidenceLocator.model_validate(raw)
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise ValueError("spreadsheet capture child locator is invalid") from exc
    if (
        locator.canonical_json != locator_json
        or locator.canonical_sha256 != locator_sha256
        or locator.source_ref != document_version_id
        or locator.sheet_name is None
        or (locator.cell_address is None) == (locator.cell_range is None)
    ):
        raise ValueError("spreadsheet capture child locator binding is invalid")
    return locator


def _node_sha256(
    node_id: str,
    extraction_run_id: str,
    ordinal: int,
    payload_text: str,
    locator: EvidenceLocator,
) -> str:
    return content_sha256(
        {
            "node_id": node_id,
            "extraction_run_id": extraction_run_id,
            "parent_ordinal": 0,
            "ordinal": ordinal,
            "node_kind": "table_cell",
            "text": payload_text,
            "locator_json": locator.canonical_json,
            "locator_sha256": locator.canonical_sha256,
        }
    )


def _validate_document_binding(
    conn: sqlite3.Connection,
    *,
    document_version_id: str,
    issuer_id: str,
    ticker: str,
    source_sha256: str,
    source_bytes_size: int,
) -> None:
    row = conn.execute(
        "SELECT document.issuer_id,document.blob_sha256,document.ticker,blob.byte_size "
        "FROM evidence_document_versions AS document "
        "JOIN evidence_content_blobs AS blob ON blob.sha256=document.blob_sha256 "
        "WHERE document.document_version_id=?",
        (document_version_id,),
    ).fetchone()
    if row is None:
        raise ValueError("spreadsheet capture document version is missing")
    row_values = _row(row)
    actual_issuer, actual_hash, actual_ticker = (
        _string(value, "document version field") for value in row_values[:3]
    )
    if (
        actual_issuer != issuer_id
        or actual_hash != source_sha256
        or actual_ticker.upper() != ticker.upper()
        or _nonnegative_int(row_values[3], "document byte size") != source_bytes_size
    ):
        raise ValueError("spreadsheet capture source binding differs from its document version")


def _validate_node_row(
    row: tuple[object, ...],
    *,
    node_id: str,
    extraction_run_id: str,
    parent_node_id: str | None,
    node_kind: str,
    locator: EvidenceLocator | None,
    expected_text: str,
    completed_at: datetime,
) -> None:
    expected = (
        node_id,
        node_id,
        1,
        extraction_run_id,
        parent_node_id,
        None,
        node_kind,
        expected_text,
        None if locator is None else locator.canonical_json,
        None if locator is None else locator.canonical_sha256,
    )
    if tuple(row[:10]) != expected or _parse_aware_datetime(row[10]) != completed_at:
        raise ValueError("spreadsheet capture evidence node metadata is invalid")


def _effective_config_sha256(config_value: dict[str, JsonValue], max_quarters: int) -> str:
    identity: dict[str, JsonValue] = {
        "schema_version": _EFFECTIVE_CONFIG_SCHEMA,
        "config": config_value,
        "max_quarters": max_quarters,
    }
    return payload_sha256(identity)


def _valid_run_clocks(start_value: object, complete_value: object) -> datetime:
    started = _parse_aware_datetime(start_value)
    completed = _parse_aware_datetime(complete_value)
    if started is None or completed is None or completed < started:
        raise ValueError("spreadsheet capture run clocks are invalid")
    return completed


def _parse_aware_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(UTC)


def _strict_canonical_object(value: str, name: str) -> dict[str, object]:
    try:
        decoded: object = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{name} is not valid JSON") from exc
    if not isinstance(decoded, dict):
        raise ValueError(f"{name} is not canonical JSON")
    parsed = cast("dict[str, object]", decoded)
    if canonical_json(parsed) != value:
        raise ValueError(f"{name} is not canonical JSON")
    return parsed


def _require_keys(value: dict[str, object], keys: set[str], name: str) -> None:
    if set(value) != keys:
        raise ValueError(f"{name} fields are incomplete or unknown")


def _object(value: object, name: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    parsed = cast("dict[object, object]", value)
    if not all(type(key) is str for key in parsed):
        raise ValueError(f"{name} must be an object")
    return cast("dict[str, object]", parsed)


def _list(value: object, name: str) -> list[object]:
    if not isinstance(value, list):
        raise ValueError(f"{name} must be an array")
    return cast("list[object]", value)


def _string(value: object, name: str) -> str:
    if type(value) is not str:
        raise ValueError(f"{name} must be text")
    return value


def _optional_string(value: object, name: str) -> str | None:
    return None if value is None else _string(value, name)


def _positive_int(value: object, name: str) -> int:
    parsed = _nonnegative_int(value, name)
    if parsed == 0:
        raise ValueError(f"{name} must be positive")
    return parsed


def _nonnegative_int(value: object, name: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"{name} must be a nonnegative integer")
    return value


def _fold(value: object) -> int:
    if type(value) is not int or value not in {0, 1}:
        raise ValueError("temporal fold must be zero or one")
    return value


def _is_sha256(value: object) -> bool:
    return type(value) is str and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _row(value: object) -> tuple[object, ...]:
    if not isinstance(value, sqlite3.Row):
        raise ValueError("spreadsheet capture database row is invalid")
    return tuple(value)
