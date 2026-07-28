"""Investor-grade closure adapters over exact native processing rows.

The adapter never accepts a caller-supplied member list.  It derives a
lane-specific ordered inventory, commits it atomically, and later replays the
same pinned native rows without callbacks.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field, JsonValue, TypeAdapter

from provenance.evidence_ledger import EvidenceLocator, EvidenceNode, EvidenceNodeKind
from provenance.fulltext_extractor_identity import (
    BASE_FULLTEXT_EXTRACTOR,
    OFFICE_FULLTEXT_EXTRACTOR,
    PDF_TABLE_EXTRACTOR_NAME,
    STRUCTURED_WEB_ARCHIVE_FULLTEXT_EXTRACTOR,
    pdf_table_extractor_code_version,
)
from provenance.pdf_table_extraction import PdfTableExtractionArtifact

DocumentProcessingLane = Literal[
    "html_native_hierarchy",
    "pdf_text",
    "pdf_ocr",
    "pdf_table",
    "image_ocr",
    "pptx_slides",
    "pptx_charts",
    "pptx_tables",
    "xlsx_workbook",
    "xlsx_sheets",
    "xlsx_tables",
    "transcript_turns",
    "transcript_speakers",
]
SealableDocumentProcessingLane = Literal[
    "html_native_hierarchy",
    "pdf_text",
    "pdf_ocr",
    "pdf_table",
    "image_ocr",
    "pptx_slides",
    "pptx_charts",
    "pptx_tables",
    "xlsx_workbook",
    "xlsx_sheets",
    "xlsx_tables",
    "transcript_turns",
    "transcript_speakers",
]

_ADAPTER_NAME = "native-document-processing-closure"
_ADAPTER_VERSION = "v3"
_ADAPTER_CONFIG_SHA256 = hashlib.sha256(
    b"native-document-processing-closure:v3:"
    b"exact-run,exact-node-output,ordered-native-members,pdf-preflight,"
    b"governed-ocr,immutable-transcript-locators,"
    b"exact-office-kind-inventories,office-source-order,office-parent-structure,"
    b"office-locator-coordinates,office-raw-part-sha256,"
    b"explicit-slide-sheet-workbook-zero-proofs,"
    b"sealed-pdf-table-artifact,ordered-page-table-row-cell-inventory,"
    b"pdf-geometry-dispositions-and-all-level-commitments"
).hexdigest()
_MAX_NATIVE_MEMBERS = 25_000
_MAX_SERIALIZED_MEMBER_SET_BYTES = 16 * 1024 * 1024
_UNSUPPORTED: dict[str, str] = {}
_JSON_OBJECT = TypeAdapter(dict[str, JsonValue])


class DocumentProcessingEvidenceError(RuntimeError):
    """Base class for fail-closed document-processing evidence errors."""

    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


class DocumentProcessingEvidenceMissingError(DocumentProcessingEvidenceError):
    """Required native rows are absent or incomplete."""


class DocumentProcessingEvidenceUnsupportedError(DocumentProcessingEvidenceError):
    """The native schema cannot honestly prove this lane."""


class DocumentProcessingEvidenceIntegrityError(DocumentProcessingEvidenceError):
    """Stored commitments or native rows no longer verify exactly."""


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class DocumentProcessingEvidenceReceipt(_FrozenModel):
    evidence_seal_id: str
    document_version_id: str
    processing_lane: SealableDocumentProcessingLane
    extraction_run_id: str
    member_count: int = Field(ge=0)
    member_set_sha256: str
    sealed_at: datetime
    exact_replay: bool


class PdfTableArtifactPersistenceReceipt(_FrozenModel):
    artifact_id: str
    document_version_id: str
    extraction_run_id: str
    disposition: Literal["sealed", "quarantined"]
    member_count: int = Field(ge=0)
    member_set_sha256: str
    exact_replay: bool


class VerifiedDocumentProcessingEvidence(_FrozenModel):
    evidence_seal_id: str
    document_version_id: str
    processing_lane: SealableDocumentProcessingLane
    extraction_run_id: str
    input_blob_sha256: str
    native_output_sha256: str
    member_count: int = Field(ge=0)
    member_set_sha256: str
    cutoff_at: datetime
    knowledge_at: datetime
    recorded_at: datetime
    sealed_at: datetime


@dataclass(frozen=True, slots=True)
class _NativeMember:
    native_table: str
    native_id: str
    native_parent_id: str | None
    locator_json: str
    content_sha256: str
    native_commitment_json: str
    native_knowledge_at: datetime
    native_recorded_at: datetime


@dataclass(frozen=True, slots=True)
class _Derived:
    document_version_id: str
    lane: SealableDocumentProcessingLane
    run_id: str
    assessment_table: str | None
    assessment_id: str | None
    input_blob_sha256: str
    native_output_sha256: str
    native_scope_json: str
    knowledge_at: datetime
    members: tuple[_NativeMember, ...]


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def _digest(value: str | object) -> str:
    encoded = value if isinstance(value, str) else _canonical_json(value)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _datetime(value: object, field: str) -> datetime:
    if isinstance(value, datetime):
        return _utc(value)
    try:
        return _utc(datetime.fromisoformat(str(value)))
    except (TypeError, ValueError) as exc:
        raise DocumentProcessingEvidenceIntegrityError(f"invalid_native_clock:{field}") from exc


def _iso(value: datetime) -> str:
    return _utc(value).isoformat()


def _rows(
    conn: sqlite3.Connection,
    sql: str,
    parameters: tuple[object, ...] = (),
) -> list[dict[str, object]]:
    cursor = conn.execute(sql, parameters)
    names = tuple(item[0] for item in cursor.description or ())
    return [dict(zip(names, tuple(row), strict=True)) for row in cursor.fetchall()]


def _one(
    conn: sqlite3.Connection,
    sql: str,
    parameters: tuple[object, ...],
    reason: str,
) -> dict[str, object]:
    rows = _rows(conn, sql, parameters)
    if len(rows) != 1:
        raise DocumentProcessingEvidenceMissingError(reason)
    return rows[0]


def _require_text(row: dict[str, object], field: str) -> str:
    value = row.get(field)
    if value is None or not str(value):
        raise DocumentProcessingEvidenceIntegrityError(f"missing_native_field:{field}")
    return str(value)


def _require_sha(row: dict[str, object], field: str) -> str:
    value = _require_text(row, field)
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise DocumentProcessingEvidenceIntegrityError(f"invalid_native_sha256:{field}")
    return value


def _require_int(row: dict[str, object], field: str) -> int:
    value = row.get(field)
    if isinstance(value, bool):
        raise DocumentProcessingEvidenceIntegrityError(f"invalid_native_integer:{field}")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            pass
    raise DocumentProcessingEvidenceIntegrityError(f"invalid_native_integer:{field}")


def _locator_int(
    locator: dict[str, JsonValue],
    field: str,
    *,
    default: int,
) -> int:
    value = locator.get(field, default)
    if isinstance(value, bool):
        return default
    return value if isinstance(value, int) else default


def _canonical_locator(raw: object) -> tuple[str, dict[str, JsonValue]]:
    if raw is None:
        payload: dict[str, JsonValue] = {}
    else:
        try:
            parsed = _JSON_OBJECT.validate_json(str(raw))
        except ValueError as exc:
            raise DocumentProcessingEvidenceIntegrityError("invalid_native_locator_json") from exc
        payload = dict(parsed)
    canonical = _canonical_json(payload)
    return canonical, payload


def _document_context(conn: sqlite3.Connection, document_version_id: str) -> dict[str, object]:
    return _one(
        conn,
        "SELECT document.document_version_id, document.blob_sha256, "
        "document.recorded_at AS document_recorded_at, blob.media_type, "
        "blob.byte_size, blob.recorded_at AS blob_recorded_at "
        "FROM evidence_document_versions document "
        "JOIN evidence_content_blobs blob ON blob.sha256=document.blob_sha256 "
        "WHERE document.document_version_id=?",
        (document_version_id,),
        "document_version_missing",
    )


def _run(
    conn: sqlite3.Connection,
    document_version_id: str,
    cutoff_at: datetime,
    *,
    extractor_name: str,
    code_version: str | None = None,
    config_sha256: str | None = None,
    pinned_run_id: str | None = None,
) -> dict[str, object]:
    clauses = [
        "document_version_id=?",
        "extractor_name=?",
        "outcome='succeeded'",
        "datetime(completed_at)<=datetime(?)",
    ]
    parameters: list[object] = [
        document_version_id,
        extractor_name,
        _iso(cutoff_at),
    ]
    if code_version is not None:
        clauses.append("extractor_code_version=?")
        parameters.append(code_version)
    if config_sha256 is not None:
        clauses.append("extractor_config_sha256=?")
        parameters.append(config_sha256)
    if pinned_run_id is not None:
        clauses.append("extraction_run_id=?")
        parameters.append(pinned_run_id)
    rows = _rows(
        conn,
        "SELECT extraction_run_id, document_version_id, input_sha256, "
        "extractor_name, extractor_config_sha256, extractor_code_version, "
        "output_sha256, started_at, completed_at, outcome "
        "FROM evidence_extraction_runs WHERE "
        + " AND ".join(clauses)
        + " ORDER BY completed_at, extraction_run_id",
        tuple(parameters),
    )
    if not rows:
        raise DocumentProcessingEvidenceMissingError("native_extraction_run_missing")
    if len(rows) != 1:
        raise DocumentProcessingEvidenceMissingError("native_extraction_run_ambiguous")
    return rows[0]


def _all_run_nodes(
    conn: sqlite3.Connection, run: dict[str, object], cutoff_at: datetime
) -> list[dict[str, object]]:
    rows = _rows(
        conn,
        "SELECT rowid AS native_rowid, node_id, evidence_key, revision, "
        "extraction_run_id, parent_node_id, supersedes_node_id, node_kind, "
        "text, locator_json, locator_sha256, recorded_at "
        "FROM evidence_nodes WHERE extraction_run_id=? "
        # The legacy extraction output digest was defined over insertion order.
        # This is the only use of SQLite rowid: the new sealed member ordinals
        # below become the database-neutral authority for all future replay.
        "ORDER BY native_rowid LIMIT ?",
        (_require_text(run, "extraction_run_id"), _MAX_NATIVE_MEMBERS + 2),
    )
    if len(rows) > _MAX_NATIVE_MEMBERS + 1:
        raise DocumentProcessingEvidenceMissingError("native_member_limit_exceeded")
    if not rows:
        raise DocumentProcessingEvidenceMissingError("native_evidence_nodes_missing")
    models: list[EvidenceNode] = []
    for row in rows:
        recorded_at = _datetime(row["recorded_at"], "evidence_nodes.recorded_at")
        if recorded_at > _utc(cutoff_at):
            raise DocumentProcessingEvidenceIntegrityError("native_node_after_cutoff")
        locator_json, locator_payload = _canonical_locator(row["locator_json"])
        stored_locator_sha = row["locator_sha256"]
        expected_locator_sha = _digest(locator_json)
        if stored_locator_sha is None or str(stored_locator_sha) != expected_locator_sha:
            raise DocumentProcessingEvidenceIntegrityError("native_locator_commitment_mismatch")
        node_kind_value = _require_text(row, "node_kind")
        if node_kind_value not in {
            "document",
            "section",
            "passage",
            "table",
            "table_row",
            "table_cell",
            "pdf_page",
            "transcript_turn",
            "claim",
        }:
            raise DocumentProcessingEvidenceIntegrityError("invalid_native_node_kind")
        models.append(
            EvidenceNode(
                node_id=_require_text(row, "node_id"),
                evidence_key=_require_text(row, "evidence_key"),
                revision=_require_int(row, "revision"),
                extraction_run_id=_require_text(row, "extraction_run_id"),
                parent_node_id=(
                    None if row["parent_node_id"] is None else str(row["parent_node_id"])
                ),
                supersedes_node_id=(
                    None if row["supersedes_node_id"] is None else str(row["supersedes_node_id"])
                ),
                node_kind=cast(EvidenceNodeKind, node_kind_value),
                text=_require_text(row, "text"),
                locator=EvidenceLocator.model_validate(locator_payload),
                locator_sha256=expected_locator_sha,
                recorded_at=recorded_at,
            )
        )
    output_payload = [model.model_dump(mode="json", exclude_none=True) for model in models]
    if _digest(output_payload) != _require_sha(run, "output_sha256"):
        raise DocumentProcessingEvidenceIntegrityError(
            "native_extraction_output_commitment_mismatch"
        )
    return rows


def _node_member(
    row: dict[str, object],
    run: dict[str, object],
    *,
    lane: str,
) -> _NativeMember:
    locator_json, _ = _canonical_locator(row["locator_json"])
    text = _require_text(row, "text")
    run_completed = _datetime(run["completed_at"], "run.completed_at")
    node_recorded = _datetime(row["recorded_at"], "node.recorded_at")
    commitment = {
        "evidence_key": _require_text(row, "evidence_key"),
        "extraction_run_id": _require_text(row, "extraction_run_id"),
        "lane": lane,
        "locator_sha256": _digest(locator_json),
        "node_id": _require_text(row, "node_id"),
        "node_kind": _require_text(row, "node_kind"),
        "parent_node_id": (None if row["parent_node_id"] is None else str(row["parent_node_id"])),
        "recorded_at": _iso(node_recorded),
        "revision": _require_int(row, "revision"),
        "supersedes_node_id": (
            None if row["supersedes_node_id"] is None else str(row["supersedes_node_id"])
        ),
        "text_sha256": _digest(text),
    }
    return _NativeMember(
        native_table="evidence_nodes",
        native_id=_require_text(row, "node_id"),
        native_parent_id=(None if row["parent_node_id"] is None else str(row["parent_node_id"])),
        locator_json=locator_json,
        content_sha256=_digest(text),
        native_commitment_json=_canonical_json(commitment),
        native_knowledge_at=run_completed,
        native_recorded_at=max(run_completed, node_recorded),
    )


def _office_locator(row: dict[str, object]) -> dict[str, JsonValue]:
    return _canonical_locator(row["locator_json"])[1]


def _office_text(
    locator: dict[str, JsonValue],
    field: str,
    *,
    reason: str,
) -> str:
    value = locator.get(field)
    if not isinstance(value, str) or not value:
        raise DocumentProcessingEvidenceMissingError(reason)
    return value


def _office_positive_int(
    locator: dict[str, JsonValue],
    field: str,
    *,
    reason: str,
) -> int:
    value = locator.get(field)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise DocumentProcessingEvidenceMissingError(reason)
    return value


def _office_non_negative_int(
    locator: dict[str, JsonValue],
    field: str,
    *,
    reason: str,
) -> int:
    value = locator.get(field)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise DocumentProcessingEvidenceMissingError(reason)
    return value


def _office_rows_by_kind(
    nodes: list[dict[str, object]],
    kinds: set[str],
) -> list[dict[str, object]]:
    return [row for row in nodes if _office_locator(row).get("office_object_kind") in kinds]


def _require_exact_text(
    row: dict[str, object],
    expected: str,
    *,
    reason: str,
) -> None:
    if _require_text(row, "text") != expected:
        raise DocumentProcessingEvidenceIntegrityError(reason)


def _require_parent(
    child: dict[str, object],
    parent: dict[str, object],
    *,
    reason: str,
) -> None:
    if child["parent_node_id"] != parent["node_id"]:
        raise DocumentProcessingEvidenceIntegrityError(reason)


def _pptx_inventory_rows(
    nodes: list[dict[str, object]],
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    inventories: dict[str, list[dict[str, object]]] = {
        "pptx_chart_inventory": [],
        "pptx_table_inventory": [],
    }
    for row in nodes:
        kind = _office_locator(row).get("office_object_kind")
        if kind in inventories:
            inventories[kind].append(row)
    charts = inventories["pptx_chart_inventory"]
    tables = inventories["pptx_table_inventory"]
    if not charts or len(charts) != len(tables):
        raise DocumentProcessingEvidenceMissingError("pptx_object_inventory_incomplete")
    expected_slides = list(range(1, len(charts) + 1))
    chart_slides = [
        _office_positive_int(
            _office_locator(row),
            "slide_number",
            reason="pptx_object_inventory_incomplete",
        )
        for row in charts
    ]
    table_slides = [
        _office_positive_int(
            _office_locator(row),
            "slide_number",
            reason="pptx_object_inventory_incomplete",
        )
        for row in tables
    ]
    if chart_slides != expected_slides or table_slides != expected_slides:
        raise DocumentProcessingEvidenceMissingError("pptx_object_inventory_incomplete")
    for chart, table in zip(charts, tables, strict=True):
        chart_locator = _office_locator(chart)
        table_locator = _office_locator(table)
        if (
            chart["node_kind"] != "passage"
            or table["node_kind"] != "passage"
            or chart["parent_node_id"] is None
            or chart["parent_node_id"] != table["parent_node_id"]
            or _office_text(
                chart_locator,
                "source_ref",
                reason="pptx_object_inventory_incomplete",
            )
            != _office_text(
                table_locator,
                "source_ref",
                reason="pptx_object_inventory_incomplete",
            )
            or _office_text(
                chart_locator,
                "office_package_part",
                reason="pptx_object_inventory_incomplete",
            )
            != _office_text(
                table_locator,
                "office_package_part",
                reason="pptx_object_inventory_incomplete",
            )
        ):
            raise DocumentProcessingEvidenceIntegrityError(
                "pptx_object_inventory_structure_mismatch"
            )
    return charts, tables


def _pptx_lane_nodes(
    nodes: list[dict[str, object]],
    lane: Literal["pptx_charts", "pptx_tables"],
) -> list[dict[str, object]]:
    chart_inventories, table_inventories = _pptx_inventory_rows(nodes)
    inventories = chart_inventories if lane == "pptx_charts" else table_inventories
    object_kind = "pptx_chart" if lane == "pptx_charts" else "pptx_table"
    member_kinds = (
        {"pptx_chart_series"} if lane == "pptx_charts" else {"pptx_table_row", "pptx_table_cell"}
    )
    lane_kinds = {f"{object_kind}_inventory", object_kind, *member_kinds}
    selected = _office_rows_by_kind(nodes, lane_kinds)
    expected_order: list[dict[str, object]] = []
    seen_coordinates: set[tuple[object, ...]] = set()
    for inventory in inventories:
        inventory_locator = _office_locator(inventory)
        slide_number = _office_positive_int(
            inventory_locator,
            "slide_number",
            reason=f"{object_kind}_inventory_incomplete",
        )
        source_ref = _office_text(
            inventory_locator,
            "source_ref",
            reason=f"{object_kind}_inventory_incomplete",
        )
        slide_part = _office_text(
            inventory_locator,
            "office_package_part",
            reason=f"{object_kind}_inventory_incomplete",
        )
        objects = [
            row
            for row in selected
            if _office_locator(row).get("office_object_kind") == object_kind
            and _office_locator(row).get("slide_number") == slide_number
        ]
        _require_exact_text(
            inventory,
            (
                f"PPTX {'chart' if lane == 'pptx_charts' else 'table'} "
                f"inventory: count={len(objects)}"
            ),
            reason=f"{object_kind}_inventory_count_mismatch",
        )
        expected_order.append(inventory)
        previous_shape = -1
        for ordinal, native_object in enumerate(objects, start=1):
            locator = _office_locator(native_object)
            shape_index = _office_non_negative_int(
                locator,
                "shape_index",
                reason=f"{object_kind}_locator_incomplete",
            )
            if (
                native_object["node_kind"] != "table"
                or locator.get("slide_number") != slide_number
                or locator.get("office_object_ordinal") != ordinal
                or locator.get("source_ref") != source_ref
                or shape_index <= previous_shape
            ):
                raise DocumentProcessingEvidenceIntegrityError(f"{object_kind}_coordinate_mismatch")
            previous_shape = shape_index
            _require_parent(
                native_object,
                inventory,
                reason=f"{object_kind}_parent_mismatch",
            )
            coordinate = (
                slide_number,
                shape_index,
                ordinal,
                locator.get("office_package_part"),
            )
            if coordinate in seen_coordinates:
                raise DocumentProcessingEvidenceIntegrityError(
                    f"{object_kind}_coordinate_duplicate"
                )
            seen_coordinates.add(coordinate)
            expected_order.append(native_object)
            if lane == "pptx_charts":
                relationship_id = _office_text(
                    locator,
                    "office_relationship_id",
                    reason="pptx_chart_locator_incomplete",
                )
                part = _office_text(
                    locator,
                    "office_package_part",
                    reason="pptx_chart_locator_incomplete",
                )
                part_sha = _office_text(
                    locator,
                    "office_part_sha256",
                    reason="pptx_chart_raw_part_commitment_missing",
                )
                series = [
                    row
                    for row in selected
                    if _office_locator(row).get("office_object_kind") == "pptx_chart_series"
                    and row["parent_node_id"] == native_object["node_id"]
                ]
                suffix = "; empty=true" if not series else ""
                _require_exact_text(
                    native_object,
                    (f"PPTX chart: part={part}; series_count={len(series)}{suffix}"),
                    reason="pptx_chart_series_count_mismatch",
                )
                for series_ordinal, member in enumerate(series, start=1):
                    member_locator = _office_locator(member)
                    if (
                        member["node_kind"] != "table_row"
                        or member_locator.get("slide_number") != slide_number
                        or member_locator.get("shape_index") != shape_index
                        or member_locator.get("office_object_ordinal") != ordinal
                        or member_locator.get("office_series_ordinal") != series_ordinal
                        or member_locator.get("source_ref") != source_ref
                        or member_locator.get("office_package_part") != part
                        or member_locator.get("office_relationship_id") != relationship_id
                        or member_locator.get("office_part_sha256") != part_sha
                    ):
                        raise DocumentProcessingEvidenceIntegrityError(
                            "pptx_chart_series_coordinate_mismatch"
                        )
                    expected_order.append(member)
            else:
                if (
                    locator.get("office_package_part") != slide_part
                    or locator.get("table_name") is None
                    or locator.get("office_relationship_id") is not None
                    or locator.get("office_part_sha256") is not None
                ):
                    raise DocumentProcessingEvidenceIntegrityError("pptx_table_locator_mismatch")
                rows = [
                    row
                    for row in selected
                    if _office_locator(row).get("office_object_kind") == "pptx_table_row"
                    and row["parent_node_id"] == native_object["node_id"]
                ]
                table_text = _require_text(native_object, "text")
                prefix = f"PPTX native table: rows={len(rows)}; grid_columns="
                grid_text, separator, name = table_text.removeprefix(prefix).partition("; name=")
                if (
                    not table_text.startswith(prefix)
                    or not separator
                    or not grid_text.isdigit()
                    or int(grid_text) < 1
                    or name != locator.get("table_name")
                ):
                    raise DocumentProcessingEvidenceIntegrityError(
                        "pptx_table_inventory_count_mismatch"
                    )
                for row_ordinal, table_row in enumerate(rows, start=1):
                    row_locator = _office_locator(table_row)
                    if (
                        table_row["node_kind"] != "table_row"
                        or row_locator.get("slide_number") != slide_number
                        or row_locator.get("shape_index") != shape_index
                        or row_locator.get("office_object_ordinal") != ordinal
                        or row_locator.get("table_row_index") != row_ordinal
                        or row_locator.get("table_name") != locator.get("table_name")
                        or row_locator.get("source_ref") != source_ref
                        or row_locator.get("office_package_part") != slide_part
                    ):
                        raise DocumentProcessingEvidenceIntegrityError(
                            "pptx_table_row_coordinate_mismatch"
                        )
                    _require_parent(
                        table_row,
                        native_object,
                        reason="pptx_table_row_parent_mismatch",
                    )
                    cells = [
                        row
                        for row in selected
                        if _office_locator(row).get("office_object_kind") == "pptx_table_cell"
                        and row["parent_node_id"] == table_row["node_id"]
                    ]
                    _require_exact_text(
                        table_row,
                        f"PPTX native table row: cell_count={len(cells)}",
                        reason="pptx_table_cell_count_mismatch",
                    )
                    expected_order.append(table_row)
                    for column_ordinal, cell in enumerate(cells, start=1):
                        cell_locator = _office_locator(cell)
                        if (
                            cell["node_kind"] != "table_cell"
                            or cell_locator.get("slide_number") != slide_number
                            or cell_locator.get("shape_index") != shape_index
                            or cell_locator.get("office_object_ordinal") != ordinal
                            or cell_locator.get("table_row_index") != row_ordinal
                            or cell_locator.get("table_column_index") != column_ordinal
                            or cell_locator.get("table_name") != locator.get("table_name")
                            or cell_locator.get("source_ref") != source_ref
                            or cell_locator.get("office_package_part") != slide_part
                        ):
                            raise DocumentProcessingEvidenceIntegrityError(
                                "pptx_table_cell_coordinate_mismatch"
                            )
                        expected_order.append(cell)
    if [row["node_id"] for row in selected] != [row["node_id"] for row in expected_order]:
        raise DocumentProcessingEvidenceIntegrityError(f"{object_kind}_source_order_mismatch")
    return selected


def _xlsx_table_lane_nodes(
    nodes: list[dict[str, object]],
) -> list[dict[str, object]]:
    selected = _office_rows_by_kind(
        nodes,
        {"xlsx_named_table_inventory", "xlsx_named_table"},
    )
    inventories = [
        row
        for row in selected
        if _office_locator(row).get("office_object_kind") == "xlsx_named_table_inventory"
    ]
    workbook_inventories = [row for row in inventories if "sheet_name" not in _office_locator(row)]
    sheet_inventories = [row for row in inventories if "sheet_name" in _office_locator(row)]
    if len(workbook_inventories) != 1 or not sheet_inventories:
        raise DocumentProcessingEvidenceMissingError("xlsx_named_table_inventory_incomplete")
    workbook = workbook_inventories[0]
    workbook_locator = _office_locator(workbook)
    source_ref = _office_text(
        workbook_locator,
        "source_ref",
        reason="xlsx_named_table_inventory_incomplete",
    )
    if (
        workbook["node_kind"] != "passage"
        or workbook["parent_node_id"] is None
        or workbook_locator.get("office_package_part") != "xl/workbook.xml"
    ):
        raise DocumentProcessingEvidenceIntegrityError("xlsx_workbook_inventory_structure_mismatch")
    all_tables = [
        row
        for row in selected
        if _office_locator(row).get("office_object_kind") == "xlsx_named_table"
    ]
    _require_exact_text(
        workbook,
        f"XLSX named-table inventory: count={len(all_tables)}",
        reason="xlsx_workbook_inventory_count_mismatch",
    )
    expected_order = [workbook]
    previous_parent_position = -1
    positions = {str(row["node_id"]): position for position, row in enumerate(nodes)}
    seen_sheets: set[str] = set()
    seen_parts: set[str] = set()
    for inventory in sheet_inventories:
        locator = _office_locator(inventory)
        sheet_name = _office_text(
            locator,
            "sheet_name",
            reason="xlsx_named_table_inventory_incomplete",
        )
        worksheet_part = _office_text(
            locator,
            "office_package_part",
            reason="xlsx_named_table_inventory_incomplete",
        )
        parent_id = inventory["parent_node_id"]
        parent = next(
            (row for row in nodes if row["node_id"] == parent_id),
            None,
        )
        if parent is None:
            raise DocumentProcessingEvidenceIntegrityError("xlsx_sheet_inventory_parent_missing")
        parent_locator = _office_locator(parent)
        parent_position = positions[str(parent["node_id"])]
        if (
            inventory["node_kind"] != "passage"
            or parent["node_kind"] != "table"
            or parent_locator.get("office_object_kind") is not None
            or parent_locator.get("sheet_name") != sheet_name
            or parent_locator.get("table_name") != sheet_name
            or parent_locator.get("source_ref") != source_ref
            or locator.get("source_ref") != source_ref
            or parent["parent_node_id"] != workbook["parent_node_id"]
            or parent_position <= previous_parent_position
            or sheet_name in seen_sheets
            or worksheet_part in seen_parts
        ):
            raise DocumentProcessingEvidenceIntegrityError(
                "xlsx_sheet_inventory_structure_mismatch"
            )
        previous_parent_position = parent_position
        seen_sheets.add(sheet_name)
        seen_parts.add(worksheet_part)
        tables = [row for row in all_tables if row["parent_node_id"] == inventory["node_id"]]
        _require_exact_text(
            inventory,
            f"XLSX sheet named-table inventory: count={len(tables)}",
            reason="xlsx_sheet_inventory_count_mismatch",
        )
        expected_order.append(inventory)
        for ordinal, table in enumerate(tables, start=1):
            table_locator = _office_locator(table)
            if (
                table["node_kind"] != "table"
                or table_locator.get("sheet_name") != sheet_name
                or table_locator.get("source_ref") != source_ref
                or table_locator.get("office_object_ordinal") != ordinal
                or table_locator.get("table_name") is None
                or (
                    table_locator.get("cell_address") is None
                    and table_locator.get("cell_range") is None
                )
                or table_locator.get("office_relationship_id") is None
                or table_locator.get("office_part_sha256") is None
            ):
                raise DocumentProcessingEvidenceIntegrityError(
                    "xlsx_named_table_coordinate_mismatch"
                )
            expected_order.append(table)
    if len(expected_order) != 1 + len(sheet_inventories) + len(all_tables):
        raise DocumentProcessingEvidenceIntegrityError("xlsx_named_table_parent_mismatch")
    if [row["node_id"] for row in selected] != [row["node_id"] for row in expected_order]:
        raise DocumentProcessingEvidenceIntegrityError("xlsx_named_table_source_order_mismatch")
    return selected


def _node_derived(
    conn: sqlite3.Connection,
    document: dict[str, object],
    lane: SealableDocumentProcessingLane,
    cutoff_at: datetime,
    pinned_run_id: str | None,
) -> _Derived:
    media_type = _require_text(document, "media_type").partition(";")[0].lower()
    expected_media_types = {
        "html_native_hierarchy": {"text/html", "application/xhtml+xml"},
        "pdf_text": {"application/pdf"},
        "pptx_slides": {
            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            "application/vnd.ms-powerpoint.presentation.macroenabled.12",
        },
        "pptx_charts": {
            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            "application/vnd.ms-powerpoint.presentation.macroenabled.12",
        },
        "pptx_tables": {
            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            "application/vnd.ms-powerpoint.presentation.macroenabled.12",
        },
        "xlsx_workbook": {
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "application/vnd.ms-excel.sheet.macroenabled.12",
        },
        "xlsx_sheets": {
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "application/vnd.ms-excel.sheet.macroenabled.12",
        },
        "xlsx_tables": {
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "application/vnd.ms-excel.sheet.macroenabled.12",
        },
    }
    allowed_media_types = expected_media_types.get(lane)
    if allowed_media_types is not None and media_type not in allowed_media_types:
        raise DocumentProcessingEvidenceMissingError(
            "document_media_type_does_not_match_processing_lane"
        )
    identities = {
        "html_native_hierarchy": (
            STRUCTURED_WEB_ARCHIVE_FULLTEXT_EXTRACTOR.name,
            STRUCTURED_WEB_ARCHIVE_FULLTEXT_EXTRACTOR.code_version,
            STRUCTURED_WEB_ARCHIVE_FULLTEXT_EXTRACTOR.config_sha256,
        ),
        "pdf_text": (
            BASE_FULLTEXT_EXTRACTOR.name,
            BASE_FULLTEXT_EXTRACTOR.code_version,
            BASE_FULLTEXT_EXTRACTOR.config_sha256,
        ),
        "pptx_slides": (
            OFFICE_FULLTEXT_EXTRACTOR.name,
            OFFICE_FULLTEXT_EXTRACTOR.code_version,
            OFFICE_FULLTEXT_EXTRACTOR.config_sha256,
        ),
        "pptx_charts": (
            OFFICE_FULLTEXT_EXTRACTOR.name,
            OFFICE_FULLTEXT_EXTRACTOR.code_version,
            OFFICE_FULLTEXT_EXTRACTOR.config_sha256,
        ),
        "pptx_tables": (
            OFFICE_FULLTEXT_EXTRACTOR.name,
            OFFICE_FULLTEXT_EXTRACTOR.code_version,
            OFFICE_FULLTEXT_EXTRACTOR.config_sha256,
        ),
        "xlsx_workbook": (
            OFFICE_FULLTEXT_EXTRACTOR.name,
            OFFICE_FULLTEXT_EXTRACTOR.code_version,
            OFFICE_FULLTEXT_EXTRACTOR.config_sha256,
        ),
        "xlsx_sheets": (
            OFFICE_FULLTEXT_EXTRACTOR.name,
            OFFICE_FULLTEXT_EXTRACTOR.code_version,
            OFFICE_FULLTEXT_EXTRACTOR.config_sha256,
        ),
        "xlsx_tables": (
            OFFICE_FULLTEXT_EXTRACTOR.name,
            OFFICE_FULLTEXT_EXTRACTOR.code_version,
            OFFICE_FULLTEXT_EXTRACTOR.config_sha256,
        ),
        "transcript_turns": (
            "legacy-evidence-backfill",
            "evidence-backfill@1",
            None,
        ),
        "transcript_speakers": (
            "legacy-evidence-backfill",
            "evidence-backfill@1",
            None,
        ),
    }
    extractor_name, code_version, config_sha256 = identities[lane]
    run = _run(
        conn,
        _require_text(document, "document_version_id"),
        cutoff_at,
        extractor_name=extractor_name,
        code_version=code_version,
        config_sha256=config_sha256,
        pinned_run_id=pinned_run_id,
    )
    nodes = _all_run_nodes(conn, run, cutoff_at)
    assessment_table: str | None = None
    assessment_id: str | None = None
    scope_extra: dict[str, object] = {}
    selected: list[dict[str, object]]
    selected_members: tuple[_NativeMember, ...] = ()
    if lane == "html_native_hierarchy":
        selected = [row for row in nodes if row["node_kind"] != "document"]
        if not selected:
            raise DocumentProcessingEvidenceMissingError("html_hierarchy_empty")
    elif lane == "pdf_text":
        assessment = _pdf_native_assessment(
            conn,
            _require_text(document, "document_version_id"),
            cutoff_at,
        )
        assessment_table = "ocr_document_assessments"
        assessment_id = _require_text(assessment, "assessment_id")
        preflight = _pdf_preflight(conn, assessment, cutoff_at)
        selected = [row for row in nodes if row["node_kind"] == "pdf_page"]
        by_page = {
            _locator_int(
                _canonical_locator(row["locator_json"])[1],
                "page_number",
                default=0,
            ): row
            for row in selected
        }
        members: list[_NativeMember] = []
        for page in preflight:
            page_number = _require_int(page, "page_number")
            character_count = _require_int(page, "native_character_count")
            node = by_page.pop(page_number, None)
            if character_count > 0:
                if node is None or _digest(_require_text(node, "text")) != _require_sha(
                    page, "native_text_sha256"
                ):
                    raise DocumentProcessingEvidenceMissingError("pdf_native_page_output_missing")
                members.append(_node_member(node, run, lane=lane))
            elif node is not None:
                raise DocumentProcessingEvidenceIntegrityError(
                    "pdf_empty_preflight_page_has_output"
                )
            else:
                locator = _canonical_json({"page_number": page_number})
                native = {
                    "assessment_id": assessment_id,
                    "native_character_count": 0,
                    "native_text_sha256": _require_sha(page, "native_text_sha256"),
                    "page_number": page_number,
                    "requires_ocr": False,
                }
                assessed_at = _datetime(assessment["assessed_at"], "assessment.assessed_at")
                members.append(
                    _NativeMember(
                        native_table="ocr_preflight_pages",
                        native_id=f"{assessment_id}:{page_number}",
                        native_parent_id=assessment_id,
                        locator_json=locator,
                        content_sha256=_require_sha(page, "native_text_sha256"),
                        native_commitment_json=_canonical_json(native),
                        native_knowledge_at=assessed_at,
                        native_recorded_at=assessed_at,
                    )
                )
        if by_page:
            raise DocumentProcessingEvidenceIntegrityError("pdf_native_output_has_extra_pages")
        selected_members = tuple(members)
        scope_extra = _assessment_scope(assessment)
    elif lane == "pptx_slides":
        selected = [
            row
            for row in nodes
            if row["node_kind"] == "passage"
            and _office_locator(row).get("office_object_kind") is None
        ]
        selected.sort(
            key=lambda row: _locator_int(
                _canonical_locator(row["locator_json"])[1],
                "slide_number",
                default=0,
            )
        )
        if not selected or any(
            _locator_int(
                _canonical_locator(row["locator_json"])[1],
                "slide_number",
                default=0,
            )
            != ordinal
            for ordinal, row in enumerate(selected, start=1)
        ):
            raise DocumentProcessingEvidenceMissingError("pptx_slide_inventory_incomplete")
    elif lane in {"pptx_charts", "pptx_tables"}:
        selected = _pptx_lane_nodes(
            nodes,
            cast(Literal["pptx_charts", "pptx_tables"], lane),
        )
    elif lane == "xlsx_tables":
        selected = _xlsx_table_lane_nodes(nodes)
    elif lane in {"xlsx_workbook", "xlsx_sheets"}:
        selected = [
            row
            for row in nodes
            if row["node_kind"] != "document"
            and (
                lane == "xlsx_workbook"
                or (
                    row["node_kind"] == "table"
                    and "sheet_name" in _canonical_locator(row["locator_json"])[1]
                    and _office_locator(row).get("office_object_kind") is None
                )
            )
        ]
        if not selected:
            raise DocumentProcessingEvidenceMissingError("xlsx_sheet_inventory_empty")
    else:
        selected = [row for row in nodes if row["node_kind"] == "transcript_turn"]
        selected.sort(
            key=lambda row: _locator_int(
                _canonical_locator(row["locator_json"])[1],
                "transcript_turn_sequence",
                default=-1,
            )
        )
        if not selected:
            raise DocumentProcessingEvidenceMissingError("transcript_turn_inventory_empty")
        sequences = [
            _locator_int(
                _canonical_locator(row["locator_json"])[1],
                "transcript_turn_sequence",
                default=-1,
            )
            for row in selected
        ]
        if sequences != list(range(sequences[0], sequences[0] + len(sequences))):
            raise DocumentProcessingEvidenceMissingError("transcript_turn_sequence_incomplete")
        if lane == "transcript_speakers":
            for row in selected:
                locator = _canonical_locator(row["locator_json"])[1]
                if not all(
                    locator.get(field)
                    for field in (
                        "transcript_speaker",
                        "transcript_time_code_start",
                        "transcript_time_code_end",
                    )
                ):
                    raise DocumentProcessingEvidenceUnsupportedError(
                        "transcript_speaker_or_timecode_not_immutably_bound"
                    )
    if lane != "pdf_text":
        selected_members = tuple(_node_member(row, run, lane=lane) for row in selected)
    return _derived_from_parts(
        document,
        lane,
        run,
        assessment_table,
        assessment_id,
        scope_extra,
        selected_members,
    )


def _pdf_native_assessment(
    conn: sqlite3.Connection,
    document_version_id: str,
    cutoff_at: datetime,
) -> dict[str, object]:
    return _one(
        conn,
        "SELECT assessment_id, document_version_id, input_sha256, detector_name, "
        "detector_config_sha256, detector_code_version, native_output_sha256, "
        "page_count, outcome, reason_code, assessed_at "
        "FROM ocr_document_assessments WHERE document_version_id=? "
        "AND outcome='native_sufficient' "
        "AND datetime(assessed_at)<=datetime(?) "
        "ORDER BY assessed_at, assessment_id",
        (document_version_id, _iso(cutoff_at)),
        "pdf_native_sufficient_assessment_missing_or_ambiguous",
    )


def _pdf_preflight(
    conn: sqlite3.Connection,
    assessment: dict[str, object],
    cutoff_at: datetime,
) -> list[dict[str, object]]:
    assessed_at = _datetime(assessment["assessed_at"], "assessment.assessed_at")
    if assessed_at > _utc(cutoff_at):
        raise DocumentProcessingEvidenceIntegrityError("pdf_assessment_after_cutoff")
    rows = _rows(
        conn,
        "SELECT assessment_id, page_number, native_character_count, "
        "native_text_sha256, requires_ocr FROM ocr_preflight_pages "
        "WHERE assessment_id=? ORDER BY page_number",
        (_require_text(assessment, "assessment_id"),),
    )
    page_count = _require_int(assessment, "page_count")
    if [_require_int(row, "page_number") for row in rows] != list(range(1, page_count + 1)):
        raise DocumentProcessingEvidenceMissingError("pdf_preflight_page_set_incomplete")
    return rows


def _assessment_scope(assessment: dict[str, object]) -> dict[str, object]:
    return {
        "assessment": {
            key: (_iso(_datetime(value, f"assessment.{key}")) if key == "assessed_at" else value)
            for key, value in assessment.items()
        }
    }


def _ocr_derived(
    conn: sqlite3.Connection,
    document: dict[str, object],
    lane: Literal["pdf_ocr", "image_ocr"],
    cutoff_at: datetime,
    pinned_run_id: str | None,
    pinned_assessment_id: str | None,
) -> _Derived:
    is_pdf = lane == "pdf_ocr"
    run = _run(
        conn,
        _require_text(document, "document_version_id"),
        cutoff_at,
        extractor_name=("governed-pdf-ocr" if is_pdf else "governed-image-ocr"),
        pinned_run_id=pinned_run_id,
    )
    governance_table = "ocr_extraction_governance" if is_pdf else "image_ocr_extraction_governance"
    assessment_table = "ocr_document_assessments" if is_pdf else "image_ocr_assessments"
    result_table = "ocr_page_results" if is_pdf else "image_ocr_results"
    governance = _one(
        conn,
        f"SELECT * FROM {governance_table} WHERE extraction_run_id=?",
        (_require_text(run, "extraction_run_id"),),
        "ocr_governance_missing",
    )
    assessment_id = _require_text(governance, "assessment_id")
    if pinned_assessment_id is not None and assessment_id != pinned_assessment_id:
        raise DocumentProcessingEvidenceIntegrityError("ocr_assessment_identity_mismatch")
    assessment = _one(
        conn,
        f"SELECT * FROM {assessment_table} WHERE assessment_id=? "
        "AND document_version_id=? AND outcome='ocr_required'",
        (
            assessment_id,
            _require_text(document, "document_version_id"),
        ),
        "ocr_required_assessment_missing",
    )
    assessed_at = _datetime(assessment["assessed_at"], "assessment.assessed_at")
    governance_at = _datetime(governance["recorded_at"], "governance.recorded_at")
    if max(assessed_at, governance_at) > _utc(cutoff_at):
        raise DocumentProcessingEvidenceIntegrityError("ocr_parent_after_cutoff")
    nodes = _all_run_nodes(conn, run, cutoff_at)
    node_by_id = {_require_text(row, "node_id"): row for row in nodes}
    results = _rows(
        conn,
        f"SELECT * FROM {result_table} WHERE extraction_run_id=? ORDER BY page_number",
        (_require_text(run, "extraction_run_id"),),
    )
    if is_pdf:
        preflight = _pdf_preflight(conn, assessment, cutoff_at)
        required_pages = [
            _require_int(row, "page_number") for row in preflight if bool(row["requires_ocr"])
        ]
    else:
        preflight = []
        required_pages = [1]
    if [_require_int(row, "page_number") for row in results] != required_pages:
        raise DocumentProcessingEvidenceMissingError("ocr_result_page_set_incomplete")
    members: list[_NativeMember] = []
    for result in results:
        if result["outcome"] != "accepted" or result["node_id"] is None:
            raise DocumentProcessingEvidenceMissingError("ocr_page_not_accepted")
        node_id = str(result["node_id"])
        node = node_by_id.get(node_id)
        if node is None:
            raise DocumentProcessingEvidenceMissingError("ocr_result_node_missing")
        locator_json, _ = _canonical_locator(result["locator_json"])
        if _digest(locator_json) != _require_sha(result, "locator_sha256"):
            raise DocumentProcessingEvidenceIntegrityError("ocr_result_locator_mismatch")
        if _canonical_locator(node["locator_json"])[0] != locator_json:
            raise DocumentProcessingEvidenceIntegrityError("ocr_result_node_locator_mismatch")
        if _digest(_require_text(node, "text")) != _require_sha(result, "output_sha256"):
            raise DocumentProcessingEvidenceIntegrityError("ocr_result_output_mismatch")
        result_recorded = _datetime(result["recorded_at"], "result.recorded_at")
        if result_recorded > _utc(cutoff_at):
            raise DocumentProcessingEvidenceIntegrityError("ocr_result_after_cutoff")
        commitment = {
            "governance_sha256": _digest(_scope_row(governance)),
            "lane": lane,
            "node_sha256": _digest(_node_member(node, run, lane=lane).native_commitment_json),
            "result": _scope_row(result),
        }
        members.append(
            _NativeMember(
                native_table=result_table,
                native_id=(
                    f"{_require_text(run, 'extraction_run_id')}:"
                    f"{_require_int(result, 'page_number')}"
                ),
                native_parent_id=_require_text(run, "extraction_run_id"),
                locator_json=locator_json,
                content_sha256=_require_sha(result, "output_sha256"),
                native_commitment_json=_canonical_json(commitment),
                native_knowledge_at=_datetime(run["completed_at"], "run.completed_at"),
                native_recorded_at=max(
                    _datetime(run["completed_at"], "run.completed_at"),
                    result_recorded,
                    governance_at,
                    assessed_at,
                ),
            )
        )
    scope_extra: dict[str, object] = {
        "assessment": _scope_row(assessment),
        "governance": _scope_row(governance),
        "preflight": [_scope_row(row) for row in preflight],
    }
    return _derived_from_parts(
        document,
        lane,
        run,
        assessment_table,
        assessment_id,
        scope_extra,
        tuple(members),
    )


def _scope_row(row: dict[str, object]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in sorted(row.items()):
        if isinstance(value, datetime):
            result[key] = _iso(value)
        elif key.endswith("_at") and value is not None:
            result[key] = _iso(_datetime(value, key))
        elif isinstance(value, bytes):
            result[key] = value.hex()
        else:
            result[key] = value
    return result


def _pdf_table_member_rows(
    artifact_id: str,
    artifact: PdfTableExtractionArtifact,
) -> tuple[tuple[str, str, str | None, str, str, str | None], ...]:
    """Flatten the artifact without losing its canonical hierarchy or geometry."""

    rows: list[tuple[str, str, str | None, str, str, str | None]] = []
    for page in artifact.pages:
        page_id = f"{artifact_id}:page:{page.page_number}"
        rows.append(
            (
                "page",
                page_id,
                artifact_id,
                _canonical_json({"page_number": page.page_number}),
                _canonical_json(page.model_dump(mode="json")),
                page.disposition,
            )
        )
        for table in page.tables:
            table_id = f"{page_id}:table:{table.table_index}"
            rows.append(
                (
                    "table",
                    table_id,
                    page_id,
                    _canonical_json(
                        {
                            "page_number": table.page_number,
                            "table_index": table.table_index,
                        }
                    ),
                    _canonical_json(table.model_dump(mode="json")),
                    None,
                )
            )
            for row in table.rows:
                row_id = f"{table_id}:row:{row.row_index}"
                rows.append(
                    (
                        "row",
                        row_id,
                        table_id,
                        _canonical_json(
                            {
                                "page_number": row.page_number,
                                "row_index": row.row_index,
                                "table_index": row.table_index,
                            }
                        ),
                        _canonical_json(row.model_dump(mode="json")),
                        None,
                    )
                )
                for cell in row.cells:
                    rows.append(
                        (
                            "cell",
                            f"{row_id}:cell:{cell.column_index}",
                            row_id,
                            _canonical_json(
                                {
                                    "column_index": cell.column_index,
                                    "page_number": cell.page_number,
                                    "row_index": cell.row_index,
                                    "table_index": cell.table_index,
                                }
                            ),
                            _canonical_json(cell.model_dump(mode="json")),
                            None,
                        )
                    )
    return tuple(rows)


def record_pdf_table_extraction_artifact(
    conn: sqlite3.Connection,
    *,
    document_version_id: str,
    extraction_run_id: str,
    raw_pdf_bytes: bytes,
    artifact: PdfTableExtractionArtifact,
    recorded_at: datetime,
) -> PdfTableArtifactPersistenceReceipt:
    """Durably seal an exact PDF table artifact and its ordered native inventory."""

    recorded = _utc(recorded_at)
    document = _document_context(conn, document_version_id)
    if (
        hashlib.sha256(raw_pdf_bytes).hexdigest() != artifact.raw_pdf_sha256
        or len(raw_pdf_bytes) != artifact.raw_byte_count
        or artifact.raw_pdf_sha256 != _require_sha(document, "blob_sha256")
        or artifact.raw_byte_count != _require_int(document, "byte_size")
    ):
        raise DocumentProcessingEvidenceIntegrityError("pdf_table_document_bytes_mismatch")
    code_version = pdf_table_extractor_code_version(
        detector_version=artifact.detector.detector_version,
        pymupdf_version=artifact.detector.pymupdf_version,
        mupdf_version=artifact.detector.mupdf_version,
    )
    run = _run(
        conn,
        document_version_id,
        recorded,
        extractor_name=PDF_TABLE_EXTRACTOR_NAME,
        code_version=code_version,
        config_sha256=artifact.detector.configuration_sha256,
        pinned_run_id=extraction_run_id,
    )
    if (
        _require_sha(run, "input_sha256") != artifact.raw_pdf_sha256
        or _require_sha(run, "output_sha256") != artifact.ordered_page_table_seal_sha256
    ):
        raise DocumentProcessingEvidenceIntegrityError("pdf_table_run_commitment_mismatch")
    artifact_json = _canonical_json(artifact.model_dump(mode="json"))
    identity = _digest(
        {
            "document_version_id": document_version_id,
            "extraction_run_id": extraction_run_id,
            "artifact_sha256": _digest(artifact_json),
        }
    )
    artifact_id = f"pdfte_{identity}"
    native_rows = _pdf_table_member_rows(artifact_id, artifact)
    canonical_members = [
        {
            "canonical_object_sha256": _digest(canonical_object_json),
            "disposition": disposition,
            "locator_sha256": _digest(locator_json),
            "member_kind": member_kind,
            "member_ordinal": ordinal,
            "native_id": native_id,
            "native_parent_id": parent_id,
        }
        for ordinal, (
            member_kind,
            native_id,
            parent_id,
            locator_json,
            canonical_object_json,
            disposition,
        ) in enumerate(native_rows)
    ]
    member_set_json = _canonical_json(canonical_members)
    existing = _rows(
        conn,
        "SELECT artifact_json,artifact_sha256 FROM pdf_table_extraction_artifact_headers "
        "WHERE artifact_id=?",
        (artifact_id,),
    )
    if existing:
        if (
            len(existing) != 1
            or _require_text(existing[0], "artifact_json") != artifact_json
            or _require_sha(existing[0], "artifact_sha256") != _digest(artifact_json)
        ):
            raise DocumentProcessingEvidenceIntegrityError(
                "pdf_table_artifact_idempotency_mismatch"
            )
        stored_members = _rows(
            conn,
            "SELECT member_ordinal,member_kind,native_id,native_parent_id,"
            "locator_json,locator_sha256,disposition,canonical_object_json,"
            "canonical_object_sha256 "
            "FROM pdf_table_extraction_artifact_members "
            "WHERE artifact_id=? ORDER BY member_ordinal",
            (artifact_id,),
        )
        if len(stored_members) != len(native_rows):
            raise DocumentProcessingEvidenceIntegrityError(
                "pdf_table_artifact_idempotency_mismatch"
            )
        for ordinal, (stored, expected) in enumerate(zip(stored_members, native_rows, strict=True)):
            member_kind, native_id, parent_id, locator_json, object_json, disposition = expected
            if (
                _require_int(stored, "member_ordinal"),
                _require_text(stored, "member_kind"),
                _require_text(stored, "native_id"),
                (None if stored["native_parent_id"] is None else str(stored["native_parent_id"])),
                _require_text(stored, "locator_json"),
                _require_sha(stored, "locator_sha256"),
                None if stored["disposition"] is None else str(stored["disposition"]),
                _require_text(stored, "canonical_object_json"),
                _require_sha(stored, "canonical_object_sha256"),
            ) != (
                ordinal,
                member_kind,
                native_id,
                parent_id,
                locator_json,
                _digest(locator_json),
                disposition,
                object_json,
                _digest(object_json),
            ):
                raise DocumentProcessingEvidenceIntegrityError(
                    "pdf_table_artifact_idempotency_mismatch"
                )
        seal = _one(
            conn,
            "SELECT member_count,canonical_member_set_json,member_set_sha256 "
            "FROM pdf_table_extraction_artifact_seals WHERE artifact_id=?",
            (artifact_id,),
            "pdf_table_artifact_final_seal_missing",
        )
        if (
            _require_int(seal, "member_count") != len(native_rows)
            or _require_text(seal, "canonical_member_set_json") != member_set_json
            or _require_sha(seal, "member_set_sha256") != _digest(member_set_json)
        ):
            raise DocumentProcessingEvidenceIntegrityError(
                "pdf_table_artifact_idempotency_mismatch"
            )
        return PdfTableArtifactPersistenceReceipt(
            artifact_id=artifact_id,
            document_version_id=document_version_id,
            extraction_run_id=extraction_run_id,
            disposition=artifact.disposition,
            member_count=len(native_rows),
            member_set_sha256=_digest(member_set_json),
            exact_replay=True,
        )
    conn.execute("SAVEPOINT record_pdf_table_extraction_artifact")
    try:
        conn.execute(
            "INSERT INTO pdf_table_extraction_artifact_headers ("
            "artifact_id,document_version_id,extraction_run_id,schema_version,"
            "disposition,quarantine_reason,raw_pdf_sha256,raw_byte_count,pdf_page_count,"
            "detector_name,detector_version,pymupdf_version,mupdf_version,"
            "extractor_code_version,"
            "detector_config_json,detector_config_sha256,detector_identity_sha256,"
            "ordered_page_table_seal_sha256,artifact_json,artifact_sha256,recorded_at"
            ") VALUES (" + ",".join("?" for _ in range(21)) + ")",
            (
                artifact_id,
                document_version_id,
                extraction_run_id,
                artifact.schema_version,
                artifact.disposition,
                artifact.quarantine_reason,
                artifact.raw_pdf_sha256,
                artifact.raw_byte_count,
                artifact.pdf_page_count,
                artifact.detector.detector_name,
                artifact.detector.detector_version,
                artifact.detector.pymupdf_version,
                artifact.detector.mupdf_version,
                code_version,
                artifact.detector.canonical_config_json,
                artifact.detector.configuration_sha256,
                artifact.detector.detector_identity_sha256,
                artifact.ordered_page_table_seal_sha256,
                artifact_json,
                _digest(artifact_json),
                recorded,
            ),
        )
        for ordinal, (
            member_kind,
            native_id,
            parent_id,
            locator_json,
            canonical_object_json,
            disposition,
        ) in enumerate(native_rows):
            conn.execute(
                "INSERT INTO pdf_table_extraction_artifact_members ("
                "artifact_id,member_ordinal,member_kind,native_id,native_parent_id,"
                "locator_json,locator_sha256,disposition,canonical_object_json,"
                "canonical_object_sha256,recorded_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    artifact_id,
                    ordinal,
                    member_kind,
                    native_id,
                    parent_id,
                    locator_json,
                    _digest(locator_json),
                    disposition,
                    canonical_object_json,
                    _digest(canonical_object_json),
                    recorded,
                ),
            )
        conn.execute(
            "INSERT INTO pdf_table_extraction_artifact_seals ("
            "artifact_id,member_count,canonical_member_set_json,"
            "member_set_sha256,sealed_at) VALUES (?,?,?,?,?)",
            (
                artifact_id,
                len(native_rows),
                member_set_json,
                _digest(member_set_json),
                recorded,
            ),
        )
    except Exception:
        conn.execute("ROLLBACK TO SAVEPOINT record_pdf_table_extraction_artifact")
        conn.execute("RELEASE SAVEPOINT record_pdf_table_extraction_artifact")
        raise
    conn.execute("RELEASE SAVEPOINT record_pdf_table_extraction_artifact")
    return PdfTableArtifactPersistenceReceipt(
        artifact_id=artifact_id,
        document_version_id=document_version_id,
        extraction_run_id=extraction_run_id,
        disposition=artifact.disposition,
        member_count=len(native_rows),
        member_set_sha256=_digest(member_set_json),
        exact_replay=False,
    )


def _pdf_table_derived(
    conn: sqlite3.Connection,
    document: dict[str, object],
    cutoff_at: datetime,
    pinned_run_id: str | None,
    pinned_artifact_id: str | None,
) -> _Derived:
    clauses = [
        "header.document_version_id=?",
        "header.disposition='sealed'",
        "datetime(header.recorded_at)<=datetime(?)",
    ]
    parameters: list[object] = [
        _require_text(document, "document_version_id"),
        _iso(cutoff_at),
    ]
    if pinned_run_id is not None:
        clauses.append("header.extraction_run_id=?")
        parameters.append(pinned_run_id)
    if pinned_artifact_id is not None:
        clauses.append("header.artifact_id=?")
        parameters.append(pinned_artifact_id)
    artifact_row = _one(
        conn,
        "SELECT header.*,seal.member_count,seal.canonical_member_set_json,"
        "seal.member_set_sha256,seal.sealed_at "
        "FROM pdf_table_extraction_artifact_headers header "
        "JOIN pdf_table_extraction_artifact_seals seal "
        "ON seal.artifact_id=header.artifact_id WHERE "
        + " AND ".join(clauses)
        + " ORDER BY header.recorded_at,header.artifact_id",
        tuple(parameters),
        "sealed_pdf_table_artifact_missing_or_ambiguous",
    )
    artifact_json = _require_text(artifact_row, "artifact_json")
    if _digest(artifact_json) != _require_sha(artifact_row, "artifact_sha256"):
        raise DocumentProcessingEvidenceIntegrityError("pdf_table_artifact_commitment_mismatch")
    try:
        artifact = PdfTableExtractionArtifact.model_validate_json(artifact_json)
    except ValueError as exc:
        raise DocumentProcessingEvidenceIntegrityError(
            "pdf_table_artifact_schema_or_commitment_invalid"
        ) from exc
    if artifact.disposition != "sealed" or any(
        page.disposition == "quarantined" for page in artifact.pages
    ):
        raise DocumentProcessingEvidenceMissingError("pdf_table_artifact_not_publishable")
    code_version = pdf_table_extractor_code_version(
        detector_version=artifact.detector.detector_version,
        pymupdf_version=artifact.detector.pymupdf_version,
        mupdf_version=artifact.detector.mupdf_version,
    )
    if (
        _require_text(artifact_row, "schema_version") != artifact.schema_version
        or _require_text(artifact_row, "disposition") != artifact.disposition
        or (
            None
            if artifact_row["quarantine_reason"] is None
            else str(artifact_row["quarantine_reason"])
        )
        != artifact.quarantine_reason
        or _require_int(artifact_row, "raw_byte_count") != artifact.raw_byte_count
        or (
            None
            if artifact_row["pdf_page_count"] is None
            else _require_int(artifact_row, "pdf_page_count")
        )
        != artifact.pdf_page_count
        or _require_text(artifact_row, "detector_name") != artifact.detector.detector_name
        or _require_text(artifact_row, "detector_version") != artifact.detector.detector_version
        or _require_text(artifact_row, "pymupdf_version") != artifact.detector.pymupdf_version
        or _require_text(artifact_row, "mupdf_version") != artifact.detector.mupdf_version
        or _require_text(artifact_row, "extractor_code_version") != code_version
        or _require_text(artifact_row, "detector_config_json")
        != artifact.detector.canonical_config_json
        or artifact.raw_pdf_sha256 != _require_sha(document, "blob_sha256")
        or artifact.raw_byte_count != _require_int(document, "byte_size")
        or artifact.ordered_page_table_seal_sha256
        != _require_sha(artifact_row, "ordered_page_table_seal_sha256")
        or artifact.detector.configuration_sha256
        != _require_sha(artifact_row, "detector_config_sha256")
        or artifact.detector.detector_identity_sha256
        != _require_sha(artifact_row, "detector_identity_sha256")
    ):
        raise DocumentProcessingEvidenceIntegrityError("pdf_table_artifact_header_mismatch")
    run = _run(
        conn,
        _require_text(document, "document_version_id"),
        cutoff_at,
        extractor_name=PDF_TABLE_EXTRACTOR_NAME,
        code_version=code_version,
        config_sha256=artifact.detector.configuration_sha256,
        pinned_run_id=_require_text(artifact_row, "extraction_run_id"),
    )
    if (
        _require_sha(run, "input_sha256") != artifact.raw_pdf_sha256
        or _require_sha(run, "output_sha256") != artifact.ordered_page_table_seal_sha256
    ):
        raise DocumentProcessingEvidenceIntegrityError("pdf_table_run_commitment_mismatch")
    artifact_id = _require_text(artifact_row, "artifact_id")
    expected = _pdf_table_member_rows(artifact_id, artifact)
    stored = _rows(
        conn,
        "SELECT * FROM pdf_table_extraction_artifact_members "
        "WHERE artifact_id=? ORDER BY member_ordinal",
        (artifact_id,),
    )
    if len(stored) != len(expected):
        raise DocumentProcessingEvidenceIntegrityError("pdf_table_member_set_mismatch")
    canonical_members: list[dict[str, object]] = []
    members: list[_NativeMember] = []
    run_completed = _datetime(run["completed_at"], "run.completed_at")
    artifact_recorded = _datetime(artifact_row["recorded_at"], "artifact.recorded_at")
    for ordinal, (stored_row, expected_row) in enumerate(zip(stored, expected, strict=True)):
        member_kind, native_id, parent_id, locator_json, object_json, disposition = expected_row
        expected_tuple = (
            ordinal,
            member_kind,
            native_id,
            parent_id,
            locator_json,
            _digest(locator_json),
            disposition,
            object_json,
            _digest(object_json),
        )
        stored_tuple = (
            _require_int(stored_row, "member_ordinal"),
            _require_text(stored_row, "member_kind"),
            _require_text(stored_row, "native_id"),
            None if stored_row["native_parent_id"] is None else str(stored_row["native_parent_id"]),
            _require_text(stored_row, "locator_json"),
            _require_sha(stored_row, "locator_sha256"),
            None if stored_row["disposition"] is None else str(stored_row["disposition"]),
            _require_text(stored_row, "canonical_object_json"),
            _require_sha(stored_row, "canonical_object_sha256"),
        )
        if stored_tuple != expected_tuple:
            raise DocumentProcessingEvidenceIntegrityError("pdf_table_native_member_mismatch")
        canonical_members.append(
            {
                "canonical_object_sha256": _digest(object_json),
                "disposition": disposition,
                "locator_sha256": _digest(locator_json),
                "member_kind": member_kind,
                "member_ordinal": ordinal,
                "native_id": native_id,
                "native_parent_id": parent_id,
            }
        )
        members.append(
            _NativeMember(
                native_table="pdf_table_extraction_artifact_members",
                native_id=native_id,
                native_parent_id=parent_id,
                locator_json=locator_json,
                content_sha256=_digest(object_json),
                native_commitment_json=object_json,
                native_knowledge_at=run_completed,
                native_recorded_at=artifact_recorded,
            )
        )
    member_set_json = _canonical_json(canonical_members)
    if (
        _require_int(artifact_row, "member_count") != len(expected)
        or _require_text(artifact_row, "canonical_member_set_json") != member_set_json
        or _require_sha(artifact_row, "member_set_sha256") != _digest(member_set_json)
        or _datetime(artifact_row["sealed_at"], "artifact.sealed_at") != artifact_recorded
    ):
        raise DocumentProcessingEvidenceIntegrityError("pdf_table_member_set_mismatch")
    return _derived_from_parts(
        document,
        "pdf_table",
        run,
        "pdf_table_extraction_artifact_headers",
        artifact_id,
        {"artifact": _scope_row(artifact_row)},
        tuple(members),
    )


def _derived_from_parts(
    document: dict[str, object],
    lane: SealableDocumentProcessingLane,
    run: dict[str, object],
    assessment_table: str | None,
    assessment_id: str | None,
    scope_extra: dict[str, object],
    members: tuple[_NativeMember, ...],
) -> _Derived:
    if not members:
        raise DocumentProcessingEvidenceMissingError("native_member_set_empty")
    run_completed = _datetime(run["completed_at"], "run.completed_at")
    scope = {
        "adapter_config_sha256": _ADAPTER_CONFIG_SHA256,
        "document": {
            "blob_recorded_at": _iso(_datetime(document["blob_recorded_at"], "blob.recorded_at")),
            "blob_sha256": _require_sha(document, "blob_sha256"),
            "byte_size": _require_int(document, "byte_size"),
            "document_recorded_at": _iso(
                _datetime(
                    document["document_recorded_at"],
                    "document.recorded_at",
                )
            ),
            "document_version_id": _require_text(document, "document_version_id"),
            "media_type": _require_text(document, "media_type"),
        },
        "native": scope_extra,
        "run": _scope_row(run),
    }
    return _Derived(
        document_version_id=_require_text(document, "document_version_id"),
        lane=lane,
        run_id=_require_text(run, "extraction_run_id"),
        assessment_table=assessment_table,
        assessment_id=assessment_id,
        input_blob_sha256=_require_sha(run, "input_sha256"),
        native_output_sha256=_require_sha(run, "output_sha256"),
        native_scope_json=_canonical_json(scope),
        knowledge_at=max((run_completed, *(item.native_knowledge_at for item in members))),
        members=members,
    )


def _derive(
    conn: sqlite3.Connection,
    document_version_id: str,
    processing_lane: str,
    cutoff_at: datetime,
    *,
    pinned_run_id: str | None = None,
    pinned_assessment_id: str | None = None,
) -> _Derived:
    if processing_lane in _UNSUPPORTED:
        raise DocumentProcessingEvidenceUnsupportedError(_UNSUPPORTED[processing_lane])
    supported = {
        "html_native_hierarchy",
        "pdf_text",
        "pdf_ocr",
        "pdf_table",
        "image_ocr",
        "pptx_slides",
        "pptx_charts",
        "pptx_tables",
        "xlsx_workbook",
        "xlsx_sheets",
        "xlsx_tables",
        "transcript_turns",
        "transcript_speakers",
    }
    if processing_lane not in supported:
        raise DocumentProcessingEvidenceUnsupportedError("unknown_document_processing_lane")
    lane = cast(SealableDocumentProcessingLane, processing_lane)
    document = _document_context(conn, document_version_id)
    if lane == "pdf_table":
        return _pdf_table_derived(
            conn,
            document,
            cutoff_at,
            pinned_run_id,
            pinned_assessment_id,
        )
    if lane in {"pdf_ocr", "image_ocr"}:
        return _ocr_derived(
            conn,
            document,
            cast(Literal["pdf_ocr", "image_ocr"], lane),
            cutoff_at,
            pinned_run_id,
            pinned_assessment_id,
        )
    return _node_derived(
        conn,
        document,
        lane,
        cutoff_at,
        pinned_run_id,
    )


def _member_payload(
    evidence_seal_id: str, ordinal: int, member: _NativeMember
) -> dict[str, object]:
    return {
        "content_sha256": member.content_sha256,
        "evidence_seal_id": evidence_seal_id,
        "locator_sha256": _digest(member.locator_json),
        "member_ordinal": ordinal,
        "native_commitment_sha256": _digest(member.native_commitment_json),
        "native_id": member.native_id,
        "native_knowledge_at": _iso(member.native_knowledge_at),
        "native_parent_id": member.native_parent_id,
        "native_recorded_at": _iso(member.native_recorded_at),
        "native_table": member.native_table,
    }


def _header_payload(derived: _Derived, cutoff_at: datetime) -> dict[str, object]:
    return {
        "adapter_config_sha256": _ADAPTER_CONFIG_SHA256,
        "adapter_name": _ADAPTER_NAME,
        "adapter_version": _ADAPTER_VERSION,
        "assessment_id": derived.assessment_id,
        "assessment_table": derived.assessment_table,
        "cutoff_at": _iso(cutoff_at),
        "document_version_id": derived.document_version_id,
        "extraction_run_id": derived.run_id,
        "input_blob_sha256": derived.input_blob_sha256,
        "native_output_sha256": derived.native_output_sha256,
        "native_scope_sha256": _digest(derived.native_scope_json),
        "processing_lane": derived.lane,
    }


def publish_document_processing_evidence(
    conn: sqlite3.Connection,
    *,
    document_version_id: str,
    processing_lane: DocumentProcessingLane | str,
    cutoff_at: datetime,
    recorded_at: datetime,
) -> DocumentProcessingEvidenceReceipt:
    """Derive and atomically seal one complete native processing inventory."""

    cutoff = _utc(cutoff_at)
    recorded = _utc(recorded_at)
    if recorded < cutoff:
        raise ValueError("recorded_at cannot precede cutoff_at")
    existing = _rows(
        conn,
        "SELECT evidence_seal_id FROM document_processing_evidence_headers "
        "WHERE document_version_id=? AND processing_lane=? "
        "AND datetime(cutoff_at)=datetime(?)",
        (document_version_id, str(processing_lane), _iso(cutoff)),
    )
    if existing:
        if len(existing) != 1:
            raise DocumentProcessingEvidenceIntegrityError(
                "duplicate_processing_evidence_coordinate"
            )
        verified = verify_document_processing_evidence(
            conn,
            str(existing[0]["evidence_seal_id"]),
            document_version_id=document_version_id,
            processing_lane=str(processing_lane),
            cutoff_at=cutoff,
        )
        return DocumentProcessingEvidenceReceipt(
            evidence_seal_id=verified.evidence_seal_id,
            document_version_id=verified.document_version_id,
            processing_lane=verified.processing_lane,
            extraction_run_id=verified.extraction_run_id,
            member_count=verified.member_count,
            member_set_sha256=verified.member_set_sha256,
            sealed_at=verified.sealed_at,
            exact_replay=True,
        )
    derived = _derive(
        conn,
        document_version_id,
        str(processing_lane),
        cutoff,
    )
    identity = _digest(
        {
            "cutoff_at": _iso(cutoff),
            "document_version_id": document_version_id,
            "processing_lane": processing_lane,
            "run_id": derived.run_id,
        }
    )
    evidence_seal_id = f"dpe_{identity}"
    header_json = _canonical_json(_header_payload(derived, cutoff))
    member_jsons = tuple(
        _canonical_json(_member_payload(evidence_seal_id, ordinal, member))
        for ordinal, member in enumerate(derived.members)
    )
    member_set_json = _canonical_json([json.loads(item) for item in member_jsons])
    if len(derived.members) > _MAX_NATIVE_MEMBERS:
        raise DocumentProcessingEvidenceMissingError("native_member_limit_exceeded")
    if len(member_set_json.encode("utf-8")) > _MAX_SERIALIZED_MEMBER_SET_BYTES:
        raise DocumentProcessingEvidenceMissingError("native_member_set_size_limit_exceeded")
    sealed_at = recorded
    conn.execute("SAVEPOINT publish_document_processing_evidence")
    try:
        conn.execute(
            "INSERT INTO document_processing_evidence_headers ("
            "evidence_seal_id,idempotency_key,document_version_id,"
            "processing_lane,extraction_run_id,assessment_table,assessment_id,"
            "adapter_name,adapter_version,adapter_config_sha256,"
            "input_blob_sha256,native_output_sha256,native_scope_json,"
            "native_scope_sha256,canonical_header_json,header_sha256,"
            "cutoff_at,knowledge_at,recorded_at) VALUES (" + ",".join("?" for _ in range(19)) + ")",
            (
                evidence_seal_id,
                f"dpek_{identity}",
                derived.document_version_id,
                derived.lane,
                derived.run_id,
                derived.assessment_table,
                derived.assessment_id,
                _ADAPTER_NAME,
                _ADAPTER_VERSION,
                _ADAPTER_CONFIG_SHA256,
                derived.input_blob_sha256,
                derived.native_output_sha256,
                derived.native_scope_json,
                _digest(derived.native_scope_json),
                header_json,
                _digest(header_json),
                cutoff,
                derived.knowledge_at,
                recorded,
            ),
        )
        for ordinal, (member, canonical_member_json) in enumerate(
            zip(derived.members, member_jsons, strict=True)
        ):
            conn.execute(
                "INSERT INTO document_processing_evidence_members ("
                "evidence_seal_id,member_ordinal,native_table,native_id,"
                "native_parent_id,locator_json,locator_sha256,content_sha256,"
                "native_commitment_json,native_commitment_sha256,"
                "canonical_member_json,member_sha256,native_knowledge_at,"
                "native_recorded_at) VALUES (" + ",".join("?" for _ in range(14)) + ")",
                (
                    evidence_seal_id,
                    ordinal,
                    member.native_table,
                    member.native_id,
                    member.native_parent_id,
                    member.locator_json,
                    _digest(member.locator_json),
                    member.content_sha256,
                    member.native_commitment_json,
                    _digest(member.native_commitment_json),
                    canonical_member_json,
                    _digest(canonical_member_json),
                    member.native_knowledge_at,
                    member.native_recorded_at,
                ),
            )
        conn.execute(
            "INSERT INTO document_processing_evidence_seals ("
            "evidence_seal_id,member_count,canonical_member_set_json,"
            "member_set_sha256,sealed_at) VALUES (?,?,?,?,?)",
            (
                evidence_seal_id,
                len(derived.members),
                member_set_json,
                _digest(member_set_json),
                sealed_at,
            ),
        )
    except Exception:
        conn.execute("ROLLBACK TO SAVEPOINT publish_document_processing_evidence")
        conn.execute("RELEASE SAVEPOINT publish_document_processing_evidence")
        raise
    conn.execute("RELEASE SAVEPOINT publish_document_processing_evidence")
    return DocumentProcessingEvidenceReceipt(
        evidence_seal_id=evidence_seal_id,
        document_version_id=derived.document_version_id,
        processing_lane=derived.lane,
        extraction_run_id=derived.run_id,
        member_count=len(derived.members),
        member_set_sha256=_digest(member_set_json),
        sealed_at=sealed_at,
        exact_replay=False,
    )


def verify_document_processing_evidence(
    conn: sqlite3.Connection,
    evidence_seal_id: str,
    *,
    document_version_id: str,
    processing_lane: DocumentProcessingLane | str,
    cutoff_at: datetime,
) -> VerifiedDocumentProcessingEvidence:
    """Recompute a sealed publication from its exact pinned native rows."""

    header = _one(
        conn,
        "SELECT * FROM document_processing_evidence_headers WHERE evidence_seal_id=?",
        (evidence_seal_id,),
        "processing_evidence_header_missing",
    )
    seal = _one(
        conn,
        "SELECT * FROM document_processing_evidence_seals WHERE evidence_seal_id=?",
        (evidence_seal_id,),
        "processing_evidence_final_seal_missing",
    )
    cutoff = _utc(cutoff_at)
    if (
        _require_text(header, "document_version_id") != document_version_id
        or _require_text(header, "processing_lane") != str(processing_lane)
        or _datetime(header["cutoff_at"], "header.cutoff_at") != cutoff
    ):
        raise DocumentProcessingEvidenceIntegrityError("processing_evidence_coordinate_mismatch")
    if (
        _require_text(header, "adapter_name") != _ADAPTER_NAME
        or _require_text(header, "adapter_version") != _ADAPTER_VERSION
        or _require_sha(header, "adapter_config_sha256") != _ADAPTER_CONFIG_SHA256
    ):
        raise DocumentProcessingEvidenceIntegrityError(
            "processing_evidence_adapter_identity_mismatch"
        )
    derived = _derive(
        conn,
        document_version_id,
        str(processing_lane),
        cutoff,
        pinned_run_id=_require_text(header, "extraction_run_id"),
        pinned_assessment_id=(
            None if header["assessment_id"] is None else str(header["assessment_id"])
        ),
    )
    expected_header_json = _canonical_json(_header_payload(derived, cutoff))
    if (
        _require_text(header, "native_scope_json") != derived.native_scope_json
        or _require_sha(header, "native_scope_sha256") != _digest(derived.native_scope_json)
        or _require_text(header, "canonical_header_json") != expected_header_json
        or _require_sha(header, "header_sha256") != _digest(expected_header_json)
    ):
        raise DocumentProcessingEvidenceIntegrityError("processing_evidence_header_mismatch")
    stored_members = _rows(
        conn,
        "SELECT * FROM document_processing_evidence_members "
        "WHERE evidence_seal_id=? ORDER BY member_ordinal",
        (evidence_seal_id,),
    )
    expected_member_jsons = tuple(
        _canonical_json(_member_payload(evidence_seal_id, ordinal, member))
        for ordinal, member in enumerate(derived.members)
    )
    if len(stored_members) != len(derived.members):
        raise DocumentProcessingEvidenceIntegrityError("processing_evidence_member_count_mismatch")
    for ordinal, (stored, native, canonical_member_json) in enumerate(
        zip(stored_members, derived.members, expected_member_jsons, strict=True)
    ):
        expected = (
            ordinal,
            native.native_table,
            native.native_id,
            native.native_parent_id,
            native.locator_json,
            _digest(native.locator_json),
            native.content_sha256,
            native.native_commitment_json,
            _digest(native.native_commitment_json),
            canonical_member_json,
            _digest(canonical_member_json),
            native.native_knowledge_at,
            native.native_recorded_at,
        )
        actual = (
            _require_int(stored, "member_ordinal"),
            str(stored["native_table"]),
            str(stored["native_id"]),
            (None if stored["native_parent_id"] is None else str(stored["native_parent_id"])),
            str(stored["locator_json"]),
            str(stored["locator_sha256"]),
            str(stored["content_sha256"]),
            str(stored["native_commitment_json"]),
            str(stored["native_commitment_sha256"]),
            str(stored["canonical_member_json"]),
            str(stored["member_sha256"]),
            _datetime(stored["native_knowledge_at"], "member.knowledge_at"),
            _datetime(stored["native_recorded_at"], "member.recorded_at"),
        )
        normalized_expected = (
            *expected[:-2],
            _utc(expected[-2]),
            _utc(expected[-1]),
        )
        if actual != normalized_expected:
            raise DocumentProcessingEvidenceIntegrityError("processing_evidence_member_mismatch")
    member_set_json = _canonical_json([json.loads(item) for item in expected_member_jsons])
    if (
        _require_int(seal, "member_count") != len(derived.members)
        or _require_text(seal, "canonical_member_set_json") != member_set_json
        or _require_sha(seal, "member_set_sha256") != _digest(member_set_json)
    ):
        raise DocumentProcessingEvidenceIntegrityError("processing_evidence_seal_mismatch")
    return VerifiedDocumentProcessingEvidence(
        evidence_seal_id=evidence_seal_id,
        document_version_id=document_version_id,
        processing_lane=derived.lane,
        extraction_run_id=derived.run_id,
        input_blob_sha256=derived.input_blob_sha256,
        native_output_sha256=derived.native_output_sha256,
        member_count=len(derived.members),
        member_set_sha256=_digest(member_set_json),
        cutoff_at=cutoff,
        knowledge_at=_datetime(header["knowledge_at"], "header.knowledge_at"),
        recorded_at=_datetime(header["recorded_at"], "header.recorded_at"),
        sealed_at=_datetime(seal["sealed_at"], "seal.sealed_at"),
    )
