"""Deterministic, dependency-free extraction for native OOXML disclosures.

PowerPoint and Excel packages are ZIP containers of XML parts.  Reading those
parts directly keeps this evidence path reproducible and preserves formula/raw
value distinctions that a rendered-text conversion can erase.  The extractor
never follows external relationships and rejects archives whose expanded shape
would make a bounded pipeline unsafe.
"""

from __future__ import annotations

import io
import json
import posixpath
import re
import zipfile
from hashlib import sha256
from pathlib import Path
from typing import Literal
from urllib.parse import unquote, urlparse
from xml.etree import ElementTree

from pydantic import BaseModel, ConfigDict, Field, model_validator

from provenance.evidence_ledger import EvidenceLocator

OfficeFormat = Literal["pptx", "xlsx", "legacy_ppt", "legacy_xls"]
OfficeNodeKind = Literal["passage", "table", "table_row", "table_cell"]

_PPTX_MEDIA_TYPES = frozenset(
    {
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "application/vnd.ms-powerpoint.presentation.macroenabled.12",
    }
)
_XLSX_MEDIA_TYPES = frozenset(
    {
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.ms-excel.sheet.macroenabled.12",
    }
)
_LEGACY_PPT_MEDIA_TYPES = frozenset({"application/vnd.ms-powerpoint"})
_LEGACY_XLS_MEDIA_TYPES = frozenset({"application/vnd.ms-excel"})
_MAX_ARCHIVE_ENTRIES = 20_000
_MAX_ARCHIVE_UNCOMPRESSED_BYTES = 256 * 1024 * 1024
_MAX_XML_PART_BYTES = 32 * 1024 * 1024
_MAX_OUTPUT_NODES = 25_000
_MAX_NODE_CHARACTERS = 500_000
_CELL_REF = re.compile(r"^\$?([A-Z]{1,3})\$?([1-9][0-9]{0,6})$")

_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_DOC_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PRESENTATION_NS = "http://schemas.openxmlformats.org/presentationml/2006/main"
_DRAWING_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
_CHART_NS = "http://schemas.openxmlformats.org/drawingml/2006/chart"
_SPREADSHEET_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"


class StructuredOfficeNode(BaseModel):
    """One stable, locally-keyed evidence node emitted from an Office package."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    local_key: str = Field(min_length=1, max_length=256)
    parent_local_key: str | None = Field(default=None, min_length=1, max_length=256)
    node_kind: OfficeNodeKind
    text: str = Field(min_length=1)
    locator: EvidenceLocator

    @model_validator(mode="after")
    def _validate_parent(self) -> StructuredOfficeNode:
        if self.parent_local_key == self.local_key:
            raise ValueError("an Office node cannot parent itself")
        return self


class _ChartPoint(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    index: int = Field(ge=0)
    value: str
    level: int | None = Field(default=None, ge=0)


class _ChartCache(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: str = Field(min_length=1)
    point_count: int | None = Field(default=None, ge=0)
    format_code: str | None = None
    points: tuple[_ChartPoint, ...]


class _ChartDimension(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source_type: str = Field(min_length=1)
    formula: str | None = None
    literal_value: str | None = None
    cache: _ChartCache | None = None


class _ChartSeriesMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    series_index: int = Field(ge=0)
    series_order: int = Field(ge=0)
    name: _ChartDimension
    category: _ChartDimension
    value: _ChartDimension


class OfficeExtractionError(Exception):
    """A closed, user-visible reason why Office coverage could not be claimed."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def classify_office_format(source_ref: str, media_type: str | None) -> OfficeFormat | None:
    """Classify Office bytes from a normalized media type or source suffix."""

    suffix = Path(urlparse(source_ref).path).suffix.lower()
    normalized = None if media_type is None else media_type.partition(";")[0].strip().lower()
    if normalized in _PPTX_MEDIA_TYPES or suffix in {".pptx", ".pptm"}:
        return "pptx"
    if normalized in _XLSX_MEDIA_TYPES or suffix in {".xlsx", ".xlsm"}:
        return "xlsx"
    if normalized in _LEGACY_PPT_MEDIA_TYPES or suffix == ".ppt":
        return "legacy_ppt"
    if normalized in _LEGACY_XLS_MEDIA_TYPES or suffix == ".xls":
        return "legacy_xls"
    return None


def extract_office_nodes(
    source_ref: str,
    raw_bytes: bytes,
    office_format: OfficeFormat,
) -> tuple[StructuredOfficeNode, ...]:
    """Extract bounded slide- or worksheet-anchored evidence from OOXML."""

    if office_format in {"legacy_ppt", "legacy_xls"}:
        raise OfficeExtractionError("unsupported_legacy_office_format")
    try:
        with zipfile.ZipFile(io.BytesIO(raw_bytes)) as archive:
            infos = _validated_archive_inventory(archive)
            if office_format == "pptx":
                nodes = _extract_presentation(archive, infos, source_ref)
            else:
                nodes = _extract_workbook(archive, infos, source_ref)
    except OfficeExtractionError:
        raise
    except (ElementTree.ParseError, KeyError, ValueError, zipfile.BadZipFile) as error:
        raise OfficeExtractionError("unreadable_ooxml") from error
    if not nodes:
        raise OfficeExtractionError("no_substantive_text")
    if len(nodes) > _MAX_OUTPUT_NODES:
        raise OfficeExtractionError("office_node_limit_exceeded")
    return tuple(nodes)


def _validated_archive_inventory(
    archive: zipfile.ZipFile,
) -> dict[str, zipfile.ZipInfo]:
    infos = archive.infolist()
    if len(infos) > _MAX_ARCHIVE_ENTRIES:
        raise OfficeExtractionError("office_archive_entry_limit_exceeded")
    if any(info.flag_bits & 0x1 for info in infos):
        raise OfficeExtractionError("encrypted_office_archive")
    total_size = sum(info.file_size for info in infos)
    if total_size > _MAX_ARCHIVE_UNCOMPRESSED_BYTES:
        raise OfficeExtractionError("office_archive_expanded_size_limit_exceeded")
    inventory = {info.filename: info for info in infos if not info.is_dir()}
    if len(inventory) != sum(not info.is_dir() for info in infos):
        raise OfficeExtractionError("office_archive_duplicate_part")
    if "[Content_Types].xml" not in inventory:
        raise OfficeExtractionError("invalid_ooxml_package")
    return inventory


def _read_xml_part(
    archive: zipfile.ZipFile,
    inventory: dict[str, zipfile.ZipInfo],
    part_name: str,
) -> ElementTree.Element:
    info = inventory.get(part_name)
    if info is None:
        raise OfficeExtractionError("office_required_part_missing")
    if info.file_size > _MAX_XML_PART_BYTES:
        raise OfficeExtractionError("office_xml_part_size_limit_exceeded")
    return ElementTree.fromstring(archive.read(info))


def _read_part_bytes(
    archive: zipfile.ZipFile,
    inventory: dict[str, zipfile.ZipInfo],
    part_name: str,
) -> bytes:
    info = inventory.get(part_name)
    if info is None:
        raise OfficeExtractionError("office_required_part_missing")
    if info.file_size > _MAX_XML_PART_BYTES:
        raise OfficeExtractionError("office_xml_part_size_limit_exceeded")
    return archive.read(info)


def _relationships(
    archive: zipfile.ZipFile,
    inventory: dict[str, zipfile.ZipInfo],
    owner_part: str,
) -> dict[str, tuple[str, str]]:
    owner_directory, owner_name = posixpath.split(owner_part)
    relationship_part = posixpath.join(owner_directory, "_rels", owner_name + ".rels")
    if relationship_part not in inventory:
        return {}
    root = _read_xml_part(archive, inventory, relationship_part)
    relationships: dict[str, tuple[str, str]] = {}
    for relationship in root.findall(f"{{{_REL_NS}}}Relationship"):
        relationship_id = relationship.get("Id")
        target = relationship.get("Target")
        relationship_type = relationship.get("Type")
        target_mode = relationship.get("TargetMode")
        if target_mode == "External":
            raise OfficeExtractionError("office_external_relationship_forbidden")
        if target_mode not in {None, "Internal"}:
            raise OfficeExtractionError("office_relationship_contract_invalid")
        if not relationship_id or not target or not relationship_type:
            raise OfficeExtractionError("office_relationship_contract_invalid")
        if relationship_id in relationships:
            raise OfficeExtractionError("office_relationship_contract_invalid")
        parsed_target = urlparse(target)
        decoded_target = unquote(parsed_target.path)
        if (
            parsed_target.scheme
            or parsed_target.netloc
            or parsed_target.query
            or parsed_target.fragment
            or "\\" in decoded_target
        ):
            raise OfficeExtractionError("office_relationship_path_escape")
        # OPC relationship targets may be relative to the owner part or
        # absolute within the package (a leading slash).  Package-absolute is
        # not a filesystem absolute path and is valid after removing that one
        # root marker; traversal above the package root remains forbidden.
        unresolved = (
            decoded_target.removeprefix("/")
            if decoded_target.startswith("/")
            else posixpath.join(owner_directory, decoded_target)
        )
        resolved = posixpath.normpath(unresolved)
        if resolved in {"", ".", ".."} or resolved.startswith("../") or resolved.startswith("/"):
            raise OfficeExtractionError("office_relationship_path_escape")
        if resolved not in inventory:
            raise OfficeExtractionError("office_required_part_missing")
        relationships[relationship_id] = (resolved, relationship_type)
    return relationships


def _extract_presentation(
    archive: zipfile.ZipFile,
    inventory: dict[str, zipfile.ZipInfo],
    source_ref: str,
) -> list[StructuredOfficeNode]:
    presentation_part = "ppt/presentation.xml"
    root = _read_xml_part(archive, inventory, presentation_part)
    relationships = _relationships(archive, inventory, presentation_part)
    slide_ids = root.findall(f".//{{{_PRESENTATION_NS}}}sldIdLst/{{{_PRESENTATION_NS}}}sldId")
    if not slide_ids:
        raise OfficeExtractionError("presentation_slide_inventory_missing")
    nodes: list[StructuredOfficeNode] = []
    for slide_number, slide_id in enumerate(slide_ids, start=1):
        relationship_id = slide_id.get(f"{{{_DOC_REL_NS}}}id")
        relationship = relationships.get(relationship_id or "")
        if relationship is None or not relationship[1].endswith("/slide"):
            raise OfficeExtractionError("presentation_slide_relationship_missing")
        slide_part = relationship[0]
        slide_root = _read_xml_part(archive, inventory, slide_part)
        text_blocks = _presentation_text_blocks(slide_root)
        text = "\n".join(block for block in text_blocks if block.strip()).strip()
        if text:
            _require_bounded_node(text)
            nodes.append(
                StructuredOfficeNode(
                    local_key=f"slide:{slide_number}",
                    node_kind="passage",
                    text=text,
                    locator=EvidenceLocator(source_ref=source_ref, slide_number=slide_number),
                )
            )
        chart_nodes = _slide_chart_nodes(
            archive,
            inventory,
            slide_part,
            slide_root,
            source_ref,
            slide_number,
        )
        table_nodes = _slide_table_nodes(slide_part, slide_root, source_ref, slide_number)
        nodes.extend((chart_nodes[0], table_nodes[0]))
        native_nodes = chart_nodes[1:] + table_nodes[1:]
        native_nodes.sort(
            key=lambda node: (
                node.locator.shape_index if node.locator.shape_index is not None else -1
            )
        )
        nodes.extend(native_nodes)
    return nodes


def _presentation_text_blocks(root: ElementTree.Element) -> list[str]:
    blocks: list[str] = []
    table_paragraphs = {
        id(paragraph)
        for table in root.findall(f".//{{{_DRAWING_NS}}}tbl")
        for paragraph in table.findall(f".//{{{_DRAWING_NS}}}p")
    }
    for paragraph in root.findall(f".//{{{_DRAWING_NS}}}p"):
        if id(paragraph) in table_paragraphs:
            continue
        text = "".join(
            (item.text or "") for item in paragraph.findall(f".//{{{_DRAWING_NS}}}t")
        ).strip()
        if text:
            blocks.append(text)
    for properties in root.findall(f".//{{{_PRESENTATION_NS}}}cNvPr"):
        for attribute in ("title", "descr"):
            text = (properties.get(attribute) or "").strip()
            if text and text not in blocks:
                blocks.append(f"Alternative text: {text}")
    return blocks


def _slide_chart_nodes(
    archive: zipfile.ZipFile,
    inventory: dict[str, zipfile.ZipInfo],
    slide_part: str,
    slide_root: ElementTree.Element,
    source_ref: str,
    slide_number: int,
) -> list[StructuredOfficeNode]:
    relationships = _relationships(archive, inventory, slide_part)
    frames = slide_root.findall(f".//{{{_PRESENTATION_NS}}}graphicFrame")
    chart_elements = [
        (shape_index, chart)
        for shape_index, frame in enumerate(frames)
        if (chart := frame.find(f".//{{{_CHART_NS}}}chart")) is not None
    ]
    nodes = [
        StructuredOfficeNode(
            local_key=f"slide:{slide_number}:chart-inventory",
            node_kind="passage",
            text=f"PPTX chart inventory: count={len(chart_elements)}",
            locator=EvidenceLocator(
                source_ref=source_ref,
                slide_number=slide_number,
                office_object_kind="pptx_chart_inventory",
                office_package_part=slide_part,
            ),
        )
    ]
    for chart_ordinal, (shape_index, chart_element) in enumerate(chart_elements, start=1):
        relationship_id = chart_element.get(f"{{{_DOC_REL_NS}}}id")
        relationship = relationships.get(relationship_id or "")
        if relationship is None or not relationship[1].endswith("/chart"):
            raise OfficeExtractionError("presentation_chart_relationship_missing")
        chart_part = relationship[0]
        raw_chart = _read_part_bytes(archive, inventory, chart_part)
        try:
            chart_root = ElementTree.fromstring(raw_chart)
        except ElementTree.ParseError as error:
            raise OfficeExtractionError("unreadable_ooxml") from error
        part_sha256 = sha256(raw_chart).hexdigest()
        series_elements = chart_root.findall(f".//{{{_CHART_NS}}}ser")
        chart_key = f"slide:{slide_number}:chart:{chart_ordinal}"
        empty_suffix = "; empty=true" if not series_elements else ""
        nodes.append(
            StructuredOfficeNode(
                local_key=chart_key,
                parent_local_key=f"slide:{slide_number}:chart-inventory",
                node_kind="table",
                text=(
                    f"PPTX chart: part={chart_part}; "
                    f"series_count={len(series_elements)}{empty_suffix}"
                ),
                locator=EvidenceLocator(
                    source_ref=source_ref,
                    slide_number=slide_number,
                    shape_index=shape_index,
                    office_object_kind="pptx_chart",
                    office_package_part=chart_part,
                    office_relationship_id=relationship_id,
                    office_object_ordinal=chart_ordinal,
                    office_part_sha256=part_sha256,
                ),
            )
        )
        for series_ordinal, series in enumerate(series_elements, start=1):
            metadata = _chart_series_metadata(series)
            text = metadata.model_dump_json(exclude_none=True)
            _require_bounded_node(text)
            nodes.append(
                StructuredOfficeNode(
                    local_key=f"{chart_key}:series:{series_ordinal}",
                    parent_local_key=chart_key,
                    node_kind="table_row",
                    text=text,
                    locator=EvidenceLocator(
                        source_ref=source_ref,
                        slide_number=slide_number,
                        shape_index=shape_index,
                        office_object_kind="pptx_chart_series",
                        office_package_part=chart_part,
                        office_relationship_id=relationship_id,
                        office_object_ordinal=chart_ordinal,
                        office_series_ordinal=series_ordinal,
                        office_part_sha256=part_sha256,
                    ),
                )
            )
    return nodes


def _chart_series_metadata(series: ElementTree.Element) -> _ChartSeriesMetadata:
    return _ChartSeriesMetadata(
        series_index=_chart_unsigned_attribute(series, "idx"),
        series_order=_chart_unsigned_attribute(series, "order"),
        name=_chart_dimension(series, ("tx",)),
        category=_chart_dimension(series, ("cat", "xVal")),
        value=_chart_dimension(series, ("val", "yVal")),
    )


def _chart_unsigned_attribute(series: ElementTree.Element, local_name: str) -> int:
    element = series.find(f"{{{_CHART_NS}}}{local_name}")
    if element is None:
        raise OfficeExtractionError("presentation_chart_contract_invalid")
    value = element.get("val")
    if value is None:
        raise OfficeExtractionError("presentation_chart_contract_invalid")
    return _non_negative_int(value, reason="presentation_chart_contract_invalid")


def _chart_dimension(
    series: ElementTree.Element,
    local_names: tuple[str, ...],
) -> _ChartDimension:
    container = next(
        (
            found
            for local_name in local_names
            if (found := series.find(f"{{{_CHART_NS}}}{local_name}")) is not None
        ),
        None,
    )
    if container is None:
        return _ChartDimension(source_type="missing")
    children = list(container)
    if not children:
        return _ChartDimension(source_type="empty")
    source = children[0]
    source_type = _xml_local_name(source.tag)
    if source_type not in {
        "strRef",
        "numRef",
        "multiLvlStrRef",
        "strLit",
        "numLit",
        "v",
    }:
        raise OfficeExtractionError("presentation_chart_contract_invalid")
    if source_type == "v":
        return _ChartDimension(
            source_type=source_type,
            literal_value=source.text or "",
        )
    formulas = source.findall(f".//{{{_CHART_NS}}}f")
    if len(formulas) > 1:
        raise OfficeExtractionError("presentation_chart_contract_invalid")
    formula = None
    if formulas:
        formula = (formulas[0].text or "").strip()
        if not formula:
            raise OfficeExtractionError("presentation_chart_contract_invalid")
    caches = [
        candidate
        for candidate in source.iter()
        if _xml_local_name(candidate.tag)
        in {"strCache", "numCache", "multiLvlStrCache", "strLit", "numLit"}
    ]
    if len(caches) > 1:
        raise OfficeExtractionError("presentation_chart_contract_invalid")
    cache = None if not caches else _chart_cache(caches[0])
    return _ChartDimension(
        source_type=source_type,
        formula=formula,
        cache=cache,
    )


def _chart_cache(cache: ElementTree.Element) -> _ChartCache:
    point_count_element = cache.find(f"{{{_CHART_NS}}}ptCount")
    point_count = None
    if point_count_element is not None:
        value = point_count_element.get("val")
        if value is None:
            raise OfficeExtractionError("presentation_chart_contract_invalid")
        point_count = _non_negative_int(value, reason="presentation_chart_contract_invalid")
    format_element = cache.find(f"{{{_CHART_NS}}}formatCode")
    format_code = None if format_element is None else (format_element.text or "").strip()
    levels = cache.findall(f"{{{_CHART_NS}}}lvl")
    points: list[_ChartPoint] = []
    containers = levels if levels else [cache]
    for level_index, point_container in enumerate(containers):
        seen_indices: set[int] = set()
        for point in point_container.findall(f"{{{_CHART_NS}}}pt"):
            point_index_text = point.get("idx")
            value_element = point.find(f"{{{_CHART_NS}}}v")
            if point_index_text is None or value_element is None:
                raise OfficeExtractionError("presentation_chart_contract_invalid")
            point_index = _non_negative_int(
                point_index_text,
                reason="presentation_chart_contract_invalid",
            )
            if point_index in seen_indices or (
                point_count is not None and point_index >= point_count
            ):
                raise OfficeExtractionError("presentation_chart_contract_invalid")
            seen_indices.add(point_index)
            points.append(
                _ChartPoint(
                    index=point_index,
                    value=value_element.text or "",
                    level=level_index if levels else None,
                )
            )
    return _ChartCache(
        kind=_xml_local_name(cache.tag),
        point_count=point_count,
        format_code=format_code or None,
        points=tuple(points),
    )


def _slide_table_nodes(
    slide_part: str,
    slide_root: ElementTree.Element,
    source_ref: str,
    slide_number: int,
) -> list[StructuredOfficeNode]:
    frames = slide_root.findall(f".//{{{_PRESENTATION_NS}}}graphicFrame")
    tables: list[tuple[int, ElementTree.Element, str]] = []
    for shape_index, frame in enumerate(frames):
        table = frame.find(f".//{{{_DRAWING_NS}}}tbl")
        if table is None:
            continue
        properties = frame.find(f".//{{{_PRESENTATION_NS}}}cNvPr")
        name = (
            f"Table {len(tables) + 1}"
            if properties is None
            else (properties.get("name") or "").strip() or f"Table {len(tables) + 1}"
        )
        tables.append((shape_index, table, name))
    nodes = [
        StructuredOfficeNode(
            local_key=f"slide:{slide_number}:table-inventory",
            node_kind="passage",
            text=f"PPTX table inventory: count={len(tables)}",
            locator=EvidenceLocator(
                source_ref=source_ref,
                slide_number=slide_number,
                office_object_kind="pptx_table_inventory",
                office_package_part=slide_part,
            ),
        )
    ]
    for table_ordinal, (shape_index, table, name) in enumerate(tables, start=1):
        rows = table.findall(f"{{{_DRAWING_NS}}}tr")
        grid = table.find(f"{{{_DRAWING_NS}}}tblGrid")
        if grid is None:
            raise OfficeExtractionError("presentation_table_grid_missing")
        grid_columns = grid.findall(f"{{{_DRAWING_NS}}}gridCol")
        table_key = f"slide:{slide_number}:table:{table_ordinal}"
        nodes.append(
            StructuredOfficeNode(
                local_key=table_key,
                parent_local_key=f"slide:{slide_number}:table-inventory",
                node_kind="table",
                text=(
                    f"PPTX native table: rows={len(rows)}; "
                    f"grid_columns={len(grid_columns)}; name={name}"
                ),
                locator=EvidenceLocator(
                    source_ref=source_ref,
                    slide_number=slide_number,
                    shape_index=shape_index,
                    table_name=name,
                    office_object_kind="pptx_table",
                    office_package_part=slide_part,
                    office_object_ordinal=table_ordinal,
                ),
            )
        )
        for row_ordinal, row in enumerate(rows, start=1):
            row_key = f"{table_key}:row:{row_ordinal}"
            cells = row.findall(f"{{{_DRAWING_NS}}}tc")
            nodes.append(
                StructuredOfficeNode(
                    local_key=row_key,
                    parent_local_key=table_key,
                    node_kind="table_row",
                    text=f"PPTX native table row: cell_count={len(cells)}",
                    locator=EvidenceLocator(
                        source_ref=source_ref,
                        slide_number=slide_number,
                        shape_index=shape_index,
                        table_name=name,
                        table_row_index=row_ordinal,
                        office_object_kind="pptx_table_row",
                        office_package_part=slide_part,
                        office_object_ordinal=table_ordinal,
                    ),
                )
            )
            for column_ordinal, cell in enumerate(cells, start=1):
                cell_text = _presentation_table_cell_text(cell)
                nodes.append(
                    StructuredOfficeNode(
                        local_key=f"{row_key}:cell:{column_ordinal}",
                        parent_local_key=row_key,
                        node_kind="table_cell",
                        text=cell_text,
                        locator=EvidenceLocator(
                            source_ref=source_ref,
                            slide_number=slide_number,
                            shape_index=shape_index,
                            table_name=name,
                            table_row_index=row_ordinal,
                            table_column_index=column_ordinal,
                            office_object_kind="pptx_table_cell",
                            office_package_part=slide_part,
                            office_object_ordinal=table_ordinal,
                        ),
                    )
                )
    return nodes


def _presentation_table_cell_text(cell: ElementTree.Element) -> str:
    paragraphs: list[str] = []
    for paragraph in cell.findall(f".//{{{_DRAWING_NS}}}p"):
        paragraphs.append(
            "".join(text.text or "" for text in paragraph.findall(f".//{{{_DRAWING_NS}}}t"))
        )
    rendered = "\n".join(paragraphs)
    grid_span = _normalized_positive_xml_integer(cell.get("gridSpan"))
    row_span = _normalized_positive_xml_integer(cell.get("rowSpan"))
    attributes: list[tuple[str, str | None]] = [
        ("grid_span", grid_span),
        ("row_span", row_span),
        (
            "horizontal_merge_continuation",
            _normalized_xml_boolean(cell.get("hMerge")),
        ),
        (
            "vertical_merge_continuation",
            _normalized_xml_boolean(cell.get("vMerge")),
        ),
    ]
    metadata = "; ".join(f"{name}={value}" for name, value in attributes if value is not None)
    text = f"text={rendered if rendered else '<empty>'}"
    result = text if not metadata else f"{text}; {metadata}"
    _require_bounded_node(result)
    return result


def _normalized_xml_boolean(value: str | None) -> str | None:
    if value is None:
        return None
    if value in {"1", "true", "on"}:
        return "true"
    if value in {"0", "false", "off"}:
        return "false"
    raise OfficeExtractionError("presentation_table_contract_invalid")


def _normalized_positive_xml_integer(value: str | None) -> str | None:
    if value is None:
        return None
    return str(_positive_int(value, reason="presentation_table_contract_invalid"))


def _xml_local_name(tag: str) -> str:
    return tag.rpartition("}")[2]


def _extract_workbook(
    archive: zipfile.ZipFile,
    inventory: dict[str, zipfile.ZipInfo],
    source_ref: str,
) -> list[StructuredOfficeNode]:
    workbook_part = "xl/workbook.xml"
    root = _read_xml_part(archive, inventory, workbook_part)
    relationships = _relationships(archive, inventory, workbook_part)
    shared_strings = _shared_strings(archive, inventory)
    nodes: list[StructuredOfficeNode] = []
    sheets = root.findall(f".//{{{_SPREADSHEET_NS}}}sheets/{{{_SPREADSHEET_NS}}}sheet")
    if not sheets:
        raise OfficeExtractionError("workbook_sheet_inventory_missing")
    named_table_count = 0
    for sheet_index, sheet in enumerate(sheets, start=1):
        sheet_name = (sheet.get("name") or "").strip()
        relationship_id = sheet.get(f"{{{_DOC_REL_NS}}}id")
        relationship = relationships.get(relationship_id or "")
        if not sheet_name or relationship is None or not relationship[1].endswith("/worksheet"):
            raise OfficeExtractionError("workbook_sheet_relationship_missing")
        worksheet_part = relationship[0]
        worksheet_root = _read_xml_part(archive, inventory, worksheet_part)
        sheet_rows = _worksheet_rows(
            worksheet_root,
            source_ref,
            sheet_name,
            sheet_index,
            shared_strings,
        )
        state = (sheet.get("state") or "visible").strip()
        sheet_key = f"sheet:{sheet_index}"
        nodes.append(
            StructuredOfficeNode(
                local_key=sheet_key,
                node_kind="table",
                text=f"Worksheet: {sheet_name} (state={state})",
                locator=EvidenceLocator(
                    source_ref=source_ref,
                    table_name=sheet_name,
                    sheet_name=sheet_name,
                ),
            )
        )
        nodes.extend(sheet_rows)
        named_tables = _worksheet_named_table_nodes(
            archive,
            inventory,
            worksheet_part,
            worksheet_root,
            source_ref,
            sheet_name,
            sheet_index,
        )
        named_table_count += len(named_tables) - 1
        nodes.extend(named_tables)
        if len(nodes) > _MAX_OUTPUT_NODES:
            raise OfficeExtractionError("office_node_limit_exceeded")
    nodes.insert(
        0,
        StructuredOfficeNode(
            local_key="workbook:named-table-inventory",
            node_kind="passage",
            text=f"XLSX named-table inventory: count={named_table_count}",
            locator=EvidenceLocator(
                source_ref=source_ref,
                office_object_kind="xlsx_named_table_inventory",
                office_package_part=workbook_part,
            ),
        ),
    )
    return nodes


def _shared_strings(
    archive: zipfile.ZipFile,
    inventory: dict[str, zipfile.ZipInfo],
) -> tuple[str, ...]:
    part = "xl/sharedStrings.xml"
    if part not in inventory:
        return ()
    root = _read_xml_part(archive, inventory, part)
    return tuple(
        "".join(text.text or "" for text in item.findall(f".//{{{_SPREADSHEET_NS}}}t"))
        for item in root.findall(f"{{{_SPREADSHEET_NS}}}si")
    )


def _worksheet_rows(
    root: ElementTree.Element,
    source_ref: str,
    sheet_name: str,
    sheet_index: int,
    shared_strings: tuple[str, ...],
) -> list[StructuredOfficeNode]:
    rows: list[StructuredOfficeNode] = []
    for ordinal, row in enumerate(
        root.findall(f".//{{{_SPREADSHEET_NS}}}sheetData/{{{_SPREADSHEET_NS}}}row"),
        start=1,
    ):
        row_number_text = row.get("r")
        row_number = ordinal if row_number_text is None else _positive_int(row_number_text)
        rendered_cells: list[tuple[str, str]] = []
        for cell in row.findall(f"{{{_SPREADSHEET_NS}}}c"):
            address = _normalized_cell_address(cell.get("r"))
            rendered = _render_cell(cell, shared_strings)
            if rendered is not None:
                rendered_cells.append((address, rendered))
        if not rendered_cells:
            continue
        rendered_cells.sort(key=lambda item: _cell_sort_key(item[0]))
        first_address, last_address = rendered_cells[0][0], rendered_cells[-1][0]
        text = " | ".join(f"{address}: {value}" for address, value in rendered_cells)
        _require_bounded_node(text)
        rows.append(
            StructuredOfficeNode(
                local_key=f"sheet:{sheet_index}:row:{row_number}",
                parent_local_key=f"sheet:{sheet_index}",
                node_kind="table_row",
                text=text,
                locator=EvidenceLocator(
                    source_ref=source_ref,
                    table_name=sheet_name,
                    sheet_name=sheet_name,
                    cell_range=f"{first_address}:{last_address}",
                ),
            )
        )
        if len(rows) > _MAX_OUTPUT_NODES:
            raise OfficeExtractionError("office_node_limit_exceeded")
    return rows


def _worksheet_named_table_nodes(
    archive: zipfile.ZipFile,
    inventory: dict[str, zipfile.ZipInfo],
    worksheet_part: str,
    worksheet_root: ElementTree.Element,
    source_ref: str,
    sheet_name: str,
    sheet_index: int,
) -> list[StructuredOfficeNode]:
    table_parts = worksheet_root.find(f"{{{_SPREADSHEET_NS}}}tableParts")
    members = [] if table_parts is None else table_parts.findall(f"{{{_SPREADSHEET_NS}}}tablePart")
    if table_parts is not None:
        count_text = table_parts.get("count")
        if count_text is None or _non_negative_int(
            count_text, reason="worksheet_table_parts_contract_invalid"
        ) != len(members):
            raise OfficeExtractionError("worksheet_table_parts_contract_invalid")
    inventory_key = f"sheet:{sheet_index}:named-table-inventory"
    nodes = [
        StructuredOfficeNode(
            local_key=inventory_key,
            parent_local_key=f"sheet:{sheet_index}",
            node_kind="passage",
            text=f"XLSX sheet named-table inventory: count={len(members)}",
            locator=EvidenceLocator(
                source_ref=source_ref,
                sheet_name=sheet_name,
                office_object_kind="xlsx_named_table_inventory",
                office_package_part=worksheet_part,
            ),
        )
    ]
    if not members:
        return nodes
    relationships = _relationships(archive, inventory, worksheet_part)
    for table_ordinal, member in enumerate(members, start=1):
        relationship_id = member.get(f"{{{_DOC_REL_NS}}}id")
        relationship = relationships.get(relationship_id or "")
        if relationship is None or not relationship[1].endswith("/table"):
            raise OfficeExtractionError("worksheet_table_relationship_missing")
        table_part = relationship[0]
        raw_table = _read_part_bytes(archive, inventory, table_part)
        try:
            table_root = ElementTree.fromstring(raw_table)
        except ElementTree.ParseError as error:
            raise OfficeExtractionError("unreadable_ooxml") from error
        if _xml_local_name(table_root.tag) != "table":
            raise OfficeExtractionError("worksheet_table_contract_invalid")
        table_id = table_root.get("id")
        name = (table_root.get("name") or "").strip()
        display_name = (table_root.get("displayName") or "").strip()
        table_ref, cell_address, cell_range = _normalized_table_ref(table_root.get("ref"))
        if (
            table_id is None
            or not name
            or not display_name
            or _positive_int(table_id, reason="worksheet_table_contract_invalid") < 1
        ):
            raise OfficeExtractionError("worksheet_table_contract_invalid")
        nodes.append(
            StructuredOfficeNode(
                local_key=f"sheet:{sheet_index}:named-table:{table_ordinal}",
                parent_local_key=inventory_key,
                node_kind="table",
                text=(
                    f"XLSX named table: id={table_id}; name={name}; "
                    f"displayName={display_name}; ref={table_ref}"
                ),
                locator=EvidenceLocator(
                    source_ref=source_ref,
                    table_name=display_name,
                    sheet_name=sheet_name,
                    cell_address=cell_address,
                    cell_range=cell_range,
                    office_object_kind="xlsx_named_table",
                    office_package_part=table_part,
                    office_relationship_id=relationship_id,
                    office_object_ordinal=table_ordinal,
                    office_part_sha256=sha256(raw_table).hexdigest(),
                ),
            )
        )
    return nodes


def _render_cell(
    cell: ElementTree.Element,
    shared_strings: tuple[str, ...],
) -> str | None:
    cell_type = cell.get("t")
    formula_node = cell.find(f"{{{_SPREADSHEET_NS}}}f")
    value_node = cell.find(f"{{{_SPREADSHEET_NS}}}v")
    formula = None if formula_node is None else (formula_node.text or "").strip()
    raw_value = None if value_node is None else (value_node.text or "")
    if cell_type == "inlineStr":
        raw_value = "".join(
            item.text or ""
            for item in cell.findall(f"{{{_SPREADSHEET_NS}}}is//{{{_SPREADSHEET_NS}}}t")
        )
    elif cell_type == "s" and raw_value is not None:
        index = _non_negative_int(raw_value)
        if index >= len(shared_strings):
            raise OfficeExtractionError("workbook_shared_string_index_invalid")
        raw_value = shared_strings[index]
    elif cell_type == "b" and raw_value is not None:
        if raw_value not in {"0", "1"}:
            raise OfficeExtractionError("workbook_boolean_value_invalid")
        raw_value = "FALSE" if raw_value == "0" else "TRUE"
    elif cell_type == "e" and raw_value is not None:
        raw_value = f"error={raw_value}"
    if formula:
        formula_text = "=" + formula.removeprefix("=")
        if raw_value is None or raw_value == "":
            return f"formula={json.dumps(formula_text, ensure_ascii=False)}; cached=<missing>"
        return (
            f"formula={json.dumps(formula_text, ensure_ascii=False)}; "
            f"cached={json.dumps(raw_value, ensure_ascii=False)}"
        )
    if raw_value is None or raw_value == "":
        return None
    if cell_type in {"s", "inlineStr", "str", "d"}:
        return json.dumps(raw_value, ensure_ascii=False)
    return f"raw={raw_value}"


def _normalized_cell_address(value: str | None) -> str:
    if value is None:
        raise OfficeExtractionError("workbook_cell_address_missing")
    normalized = value.upper()
    if _CELL_REF.fullmatch(normalized) is None:
        raise OfficeExtractionError("workbook_cell_address_invalid")
    return normalized


def _cell_sort_key(address: str) -> tuple[int, int]:
    match = _CELL_REF.fullmatch(address)
    if match is None:
        raise OfficeExtractionError("workbook_cell_address_invalid")
    column = 0
    for character in match.group(1):
        column = column * 26 + ord(character) - ord("A") + 1
    return (int(match.group(2)), column)


def _normalized_table_ref(
    value: str | None,
) -> tuple[str, str | None, str | None]:
    if value is None:
        raise OfficeExtractionError("worksheet_table_range_missing")
    cells = value.upper().split(":")
    if len(cells) == 1:
        address = _normalized_cell_address(cells[0])
        return address, address, None
    if len(cells) != 2:
        raise OfficeExtractionError("worksheet_table_range_invalid")
    start, end = (_normalized_cell_address(cell) for cell in cells)
    if _cell_sort_key(end) < _cell_sort_key(start):
        raise OfficeExtractionError("worksheet_table_range_invalid")
    cell_range = f"{start}:{end}"
    return cell_range, None, cell_range


def _positive_int(value: str, *, reason: str = "workbook_row_number_invalid") -> int:
    parsed = _non_negative_int(value, reason=reason)
    if parsed < 1:
        raise OfficeExtractionError(reason)
    return parsed


def _non_negative_int(value: str, *, reason: str = "workbook_integer_value_invalid") -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise OfficeExtractionError(reason) from error
    if parsed < 0:
        raise OfficeExtractionError(reason)
    return parsed


def _require_bounded_node(text: str) -> None:
    if len(text) > _MAX_NODE_CHARACTERS:
        raise OfficeExtractionError("office_node_text_limit_exceeded")
