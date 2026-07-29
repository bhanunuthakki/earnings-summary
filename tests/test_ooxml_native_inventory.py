from __future__ import annotations

import hashlib
import io
import json
import zipfile
from collections.abc import Mapping

import pytest
from pydantic import ValidationError

from provenance.evidence_ledger import EvidenceLocator
from provenance.ooxml_extraction import OfficeExtractionError, extract_office_nodes

CONTENT_TYPES = """\
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>
"""
REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
DOC_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
P_NS = "http://schemas.openxmlformats.org/presentationml/2006/main"
A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
C_NS = "http://schemas.openxmlformats.org/drawingml/2006/chart"
S_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"


def _package(parts: Mapping[str, str | bytes]) -> bytes:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", CONTENT_TYPES)
        for name, payload in parts.items():
            archive.writestr(name, payload)
    return stream.getvalue()


def _relationships(*relationships: str) -> str:
    return f'<Relationships xmlns="{REL_NS}">{"".join(relationships)}</Relationships>'


def _relationship(
    relationship_id: str,
    relationship_type: str,
    target: str,
    *,
    target_mode: str | None = None,
) -> str:
    mode = "" if target_mode is None else f' TargetMode="{target_mode}"'
    return (
        f'<Relationship Id="{relationship_id}" Type="{DOC_REL_NS}/{relationship_type}" '
        f'Target="{target}"{mode}/>'
    )


def _presentation(*slide_relationship_ids: str) -> str:
    slide_ids = "".join(
        f'<p:sldId id="{256 + index}" r:id="{relationship_id}"/>'
        for index, relationship_id in enumerate(slide_relationship_ids)
    )
    return (
        f'<p:presentation xmlns:p="{P_NS}" xmlns:r="{DOC_REL_NS}">'
        f"<p:sldIdLst>{slide_ids}</p:sldIdLst></p:presentation>"
    )


def _slide(*objects: str) -> str:
    return (
        f'<p:sld xmlns:p="{P_NS}" xmlns:a="{A_NS}" xmlns:c="{C_NS}" '
        f'xmlns:r="{DOC_REL_NS}"><p:cSld><p:spTree>{"".join(objects)}'
        "</p:spTree></p:cSld></p:sld>"
    )


def _text_shape(text: str) -> str:
    return (
        '<p:sp><p:nvSpPr><p:cNvPr id="1" name="Title"/></p:nvSpPr>'
        f"<p:txBody><a:p><a:r><a:t>{text}</a:t></a:r></a:p></p:txBody></p:sp>"
    )


def _chart_frame(relationship_id: str) -> str:
    return (
        "<p:graphicFrame><p:nvGraphicFramePr>"
        '<p:cNvPr id="2" name="Chart"/>'
        "</p:nvGraphicFramePr><a:graphic><a:graphicData>"
        f'<c:chart r:id="{relationship_id}"/>'
        "</a:graphicData></a:graphic></p:graphicFrame>"
    )


def _table_frame() -> str:
    return (
        "<p:graphicFrame><p:nvGraphicFramePr>"
        '<p:cNvPr id="3" name="Merged Table" title="Table title"/>'
        "</p:nvGraphicFramePr><a:graphic><a:graphicData><a:tbl>"
        "<a:tblGrid><a:gridCol/><a:gridCol/></a:tblGrid>"
        '<a:tr h="1">'
        '<a:tc gridSpan="2"><a:txBody><a:p><a:r><a:t>Header</a:t></a:r></a:p>'
        "</a:txBody><a:tcPr/></a:tc>"
        '<a:tc hMerge="1"><a:txBody><a:p/></a:txBody><a:tcPr/></a:tc>'
        '</a:tr><a:tr h="1">'
        "<a:tc><a:txBody><a:p><a:r><a:t>Value</a:t></a:r></a:p>"
        "</a:txBody><a:tcPr/></a:tc>"
        "<a:tc><a:txBody><a:p/></a:txBody><a:tcPr/></a:tc>"
        "</a:tr></a:tbl></a:graphicData></a:graphic></p:graphicFrame>"
    )


def _chart(series: str) -> str:
    return (
        f'<c:chartSpace xmlns:c="{C_NS}"><c:chart><c:plotArea><c:lineChart>'
        f"{series}</c:lineChart></c:plotArea></c:chart></c:chartSpace>"
    )


def _series(index: int, name: str, points: tuple[tuple[int, str], ...]) -> str:
    rendered_points = "".join(
        f'<c:pt idx="{point_index}"><c:v>{value}</c:v></c:pt>' for point_index, value in points
    )
    return (
        f'<c:ser><c:idx val="{index}"/><c:order val="{index}"/>'
        f"<c:tx><c:strRef><c:f>Sheet1!$B${index + 1}</c:f>"
        f'<c:strCache><c:ptCount val="1"/><c:pt idx="0"><c:v>{name}</c:v>'
        "</c:pt></c:strCache></c:strRef></c:tx>"
        "<c:cat><c:strRef><c:f>Sheet1!$A$2:$A$4</c:f><c:strCache>"
        '<c:ptCount val="3"/><c:pt idx="0"><c:v>Q1</c:v></c:pt>'
        '<c:pt idx="2"><c:v>Q3</c:v></c:pt></c:strCache></c:strRef></c:cat>'
        f"<c:val><c:numRef><c:f>Sheet1!$B$2:$B$4</c:f><c:numCache>"
        f'<c:formatCode>0.0</c:formatCode><c:ptCount val="3"/>{rendered_points}'
        "</c:numCache></c:numRef></c:val></c:ser>"
    )


def _pptx(
    slide_xml: str,
    *,
    slide_relationships: str = "",
    extra_parts: Mapping[str, str | bytes] | None = None,
) -> bytes:
    parts: dict[str, str | bytes] = {
        "ppt/presentation.xml": _presentation("rId1"),
        "ppt/_rels/presentation.xml.rels": _relationships(
            _relationship("rId1", "slide", "slides/slide1.xml")
        ),
        "ppt/slides/slide1.xml": slide_xml,
    }
    if slide_relationships:
        parts["ppt/slides/_rels/slide1.xml.rels"] = _relationships(slide_relationships)
    if extra_parts:
        parts.update(extra_parts)
    return _package(parts)


def _workbook(sheet_names: tuple[str, ...]) -> str:
    sheets = "".join(
        f'<sheet name="{name}" sheetId="{index}" r:id="rId{index}"/>'
        for index, name in enumerate(sheet_names, start=1)
    )
    return f'<workbook xmlns="{S_NS}" xmlns:r="{DOC_REL_NS}"><sheets>{sheets}</sheets></workbook>'


def _worksheet(table_relationship_ids: tuple[str, ...], cells: str = "") -> str:
    table_parts = ""
    if table_relationship_ids:
        members = "".join(
            f'<tablePart r:id="{relationship_id}"/>' for relationship_id in table_relationship_ids
        )
        table_parts = f'<tableParts count="{len(table_relationship_ids)}">{members}</tableParts>'
    return (
        f'<worksheet xmlns="{S_NS}" xmlns:r="{DOC_REL_NS}">'
        f"<sheetData>{cells}</sheetData>{table_parts}</worksheet>"
    )


def test_native_locator_grammar_is_closed_and_coordinate_bound() -> None:
    locator = EvidenceLocator(
        source_ref="deck.pptx",
        slide_number=1,
        office_object_kind="pptx_chart_series",
        office_package_part="ppt/charts/chart1.xml",
        office_relationship_id="rId9",
        office_object_ordinal=1,
        office_series_ordinal=2,
        office_part_sha256="a" * 64,
    )
    assert locator.model_dump(exclude_none=True)["office_series_ordinal"] == 2
    with pytest.raises(ValidationError):
        EvidenceLocator(
            source_ref="deck.pptx",
            slide_number=1,
            office_object_kind="pptx_chart_series",
            office_package_part="../chart.xml",
            office_object_ordinal=1,
            office_series_ordinal=1,
            office_part_sha256="a" * 64,
        )
    with pytest.raises(ValidationError):
        EvidenceLocator(
            source_ref="book.xlsx",
            sheet_name="Sheet1",
            office_object_kind="pptx_chart",
            office_package_part="ppt/charts/chart1.xml",
            office_object_ordinal=1,
            office_part_sha256="a" * 64,
        )


def test_pptx_emits_exact_chart_series_and_native_table_hierarchy() -> None:
    chart_xml = _chart(
        _series(0, "Revenue", ((0, "10"), (2, "30")))
        + _series(1, "Margin", ((0, "40"), (1, "50"), (2, "60")))
    )
    raw = _pptx(
        _slide(_text_shape("Narrative only"), _table_frame(), _chart_frame("rId9")),
        slide_relationships=_relationship("rId9", "chart", "../charts/chart1.xml"),
        extra_parts={"ppt/charts/chart1.xml": chart_xml},
    )
    nodes = extract_office_nodes("deck.pptx", raw, "pptx")
    by_key = {node.local_key: node for node in nodes}

    assert by_key["slide:1"].text == "Narrative only\nAlternative text: Table title"
    assert [node.local_key for node in nodes].index("slide:1:table:1") < [
        node.local_key for node in nodes
    ].index("slide:1:chart:1")
    assert by_key["slide:1:chart-inventory"].text == "PPTX chart inventory: count=1"
    chart = by_key["slide:1:chart:1"]
    assert chart.node_kind == "table"
    assert chart.text.endswith("series_count=2")
    assert chart.locator.office_part_sha256 == hashlib.sha256(chart_xml.encode()).hexdigest()
    first_series = by_key["slide:1:chart:1:series:1"]
    metadata = json.loads(first_series.text)
    assert metadata["category"]["formula"] == "Sheet1!$A$2:$A$4"
    assert metadata["category"]["cache"]["points"] == [
        {"index": 0, "value": "Q1"},
        {"index": 2, "value": "Q3"},
    ]
    assert metadata["value"]["cache"]["format_code"] == "0.0"
    assert metadata["value"]["cache"]["points"] == [
        {"index": 0, "value": "10"},
        {"index": 2, "value": "30"},
    ]

    assert by_key["slide:1:table-inventory"].text == "PPTX table inventory: count=1"
    table = by_key["slide:1:table:1"]
    assert table.node_kind == "table"
    assert table.text == "PPTX native table: rows=2; grid_columns=2; name=Merged Table"
    assert by_key["slide:1:table:1:row:1"].parent_local_key == table.local_key
    merged = by_key["slide:1:table:1:row:1:cell:1"]
    assert "grid_span=2" in merged.text
    continuation = by_key["slide:1:table:1:row:1:cell:2"]
    assert "horizontal_merge_continuation=true" in continuation.text
    assert by_key["slide:1:table:1:row:2:cell:2"].text.startswith("text=<empty>")


def test_pptx_empty_chart_and_zero_object_slide_are_explicit_and_deterministic() -> None:
    empty_chart = _chart("")
    raw = _pptx(
        _slide(_chart_frame("rId2")),
        slide_relationships=_relationship("rId2", "chart", "../charts/empty.xml"),
        extra_parts={"ppt/charts/empty.xml": empty_chart},
    )
    first = extract_office_nodes("empty.pptx", raw, "pptx")
    second = extract_office_nodes("empty.pptx", raw, "pptx")
    assert first == second
    assert [node.local_key for node in first] == [
        "slide:1:chart-inventory",
        "slide:1:table-inventory",
        "slide:1:chart:1",
    ]
    assert first[2].text.endswith("series_count=0; empty=true")
    assert len({node.locator.canonical_sha256 for node in first}) == len(first)

    zero = extract_office_nodes("zero.pptx", _pptx(_slide()), "pptx")
    assert [node.text for node in zero] == [
        "PPTX chart inventory: count=0",
        "PPTX table inventory: count=0",
    ]


def test_xlsx_follows_table_parts_and_proves_sheet_and_workbook_zero_counts() -> None:
    parts = {
        "xl/workbook.xml": _workbook(("Named", "Empty")),
        "xl/_rels/workbook.xml.rels": _relationships(
            _relationship("rId1", "worksheet", "worksheets/sheet1.xml"),
            _relationship("rId2", "worksheet", "worksheets/sheet2.xml"),
        ),
        "xl/worksheets/sheet1.xml": _worksheet(
            ("rIdTable",),
            '<row r="1"><c r="A1" t="inlineStr"><is><t>Revenue</t></is></c>'
            '<c r="B1"><v>10</v></c></row>',
        ),
        "xl/worksheets/_rels/sheet1.xml.rels": _relationships(
            _relationship("rIdTable", "table", "../tables/table7.xml")
        ),
        "xl/tables/table7.xml": (
            f'<table xmlns="{S_NS}" id="7" name="Internal_Name" '
            'displayName="Revenue_Table" ref="A1:B9"/>'
        ),
        "xl/worksheets/sheet2.xml": _worksheet(()),
    }
    nodes = extract_office_nodes("book.xlsx", _package(parts), "xlsx")
    by_key = {node.local_key: node for node in nodes}

    assert by_key["workbook:named-table-inventory"].text == ("XLSX named-table inventory: count=1")
    assert by_key["sheet:1:named-table-inventory"].text.endswith("count=1")
    named = by_key["sheet:1:named-table:1"]
    assert named.text == (
        "XLSX named table: id=7; name=Internal_Name; displayName=Revenue_Table; ref=A1:B9"
    )
    assert named.locator.table_name == "Revenue_Table"
    assert named.locator.cell_range == "A1:B9"
    assert named.locator.office_package_part == "xl/tables/table7.xml"
    assert by_key["sheet:2:named-table-inventory"].text.endswith("count=0")
    assert "sheet:2" in by_key
    assert len({node.locator.canonical_sha256 for node in nodes}) == len(nodes)


@pytest.mark.parametrize(
    ("relationship", "extra_parts", "reason"),
    (
        (
            _relationship(
                "rId9",
                "chart",
                "https://example.com/chart.xml",
                target_mode="External",
            ),
            {},
            "office_external_relationship_forbidden",
        ),
        (
            _relationship("rId9", "chart", "../../../chart.xml"),
            {},
            "office_relationship_path_escape",
        ),
        (
            _relationship("rId9", "chart", "../charts/missing.xml"),
            {},
            "office_required_part_missing",
        ),
        (
            _relationship("rId9", "chart", "../charts/chart.xml")
            + _relationship("rId9", "chart", "../charts/chart.xml"),
            {"ppt/charts/chart.xml": _chart("")},
            "office_relationship_contract_invalid",
        ),
    ),
)
def test_pptx_chart_relationships_fail_closed(
    relationship: str,
    extra_parts: Mapping[str, str | bytes],
    reason: str,
) -> None:
    raw = _pptx(
        _slide(_chart_frame("rId9")),
        slide_relationships=relationship,
        extra_parts=extra_parts,
    )
    with pytest.raises(OfficeExtractionError) as error:
        extract_office_nodes("bad.pptx", raw, "pptx")
    assert error.value.reason == reason


def test_xlsx_table_relationship_contract_fails_closed() -> None:
    parts = {
        "xl/workbook.xml": _workbook(("Broken",)),
        "xl/_rels/workbook.xml.rels": _relationships(
            _relationship("rId1", "worksheet", "worksheets/sheet1.xml")
        ),
        "xl/worksheets/sheet1.xml": _worksheet(("rIdMissing",)),
    }
    with pytest.raises(OfficeExtractionError) as error:
        extract_office_nodes("bad.xlsx", _package(parts), "xlsx")
    assert error.value.reason == "worksheet_table_relationship_missing"
