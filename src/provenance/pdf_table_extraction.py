"""Deterministic, fail-closed table extraction from immutable PDF bytes.

This module proves table coverage only relative to one recorded PyMuPDF
``Page.find_tables`` policy.  It does not claim semantic exhaustiveness:
borderless, unusual, or image-only tables can remain outside that detector's
capabilities.  Ambiguous detector output is therefore quarantined instead of
being mislabeled as a table-free page.
"""

from __future__ import annotations

import contextlib
import importlib
import io
import json
import math
from collections.abc import Sequence
from dataclasses import dataclass, replace
from hashlib import sha256
from types import TracebackType
from typing import Literal, Protocol, Self, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_SCHEMA_VERSION = "pdf-table-extraction@1"
_POLICY_VERSION = "pymupdf-dual-table-policy@1"
_COORDINATE_SPACE = "pymupdf-rotated-page-points"
_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_STRATEGIES = ("lines", "text")

DetectorStrategy = Literal["lines", "text"]
PageDisposition = Literal[
    "tables_detected",
    "no_tables_detected",
    "quarantined",
]
DocumentDisposition = Literal["sealed", "quarantined"]


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _digest(value: object) -> str:
    return sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _validate_sha256(value: str) -> str:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError("must be a lowercase SHA-256 hex digest")
    return value


def _coordinate(value: float) -> float:
    if not math.isfinite(value):
        raise ValueError("PDF coordinates must be finite")
    rounded = round(value, 6)
    return 0.0 if rounded == 0 else rounded


class PdfTableExtractionConfig(BaseModel):
    """Closed detector policy plus caller-adjustable bounded-output limits."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    policy_version: Literal["pymupdf-dual-table-policy@1"] = _POLICY_VERSION
    strategies: tuple[Literal["lines"], Literal["text"]] = _STRATEGIES
    snap_tolerance: float = Field(default=3.0, ge=0.0, le=100.0)
    join_tolerance: float = Field(default=3.0, ge=0.0, le=100.0)
    edge_min_length: float = Field(default=3.0, ge=0.0, le=10_000.0)
    min_words_vertical: int = Field(default=3, ge=1, le=100)
    min_words_horizontal: int = Field(default=1, ge=1, le=100)
    intersection_tolerance: float = Field(default=3.0, ge=0.0, le=100.0)
    text_tolerance: float = Field(default=3.0, ge=0.0, le=100.0)
    scanned_image_page_area_ratio: float = Field(default=0.8, gt=0.0, le=1.0)
    maximum_pages: int = Field(default=2_000, ge=1, le=10_000)
    maximum_tables_per_page: int = Field(default=100, ge=1, le=1_000)
    maximum_cells_per_table: int = Field(default=25_000, ge=1, le=250_000)
    maximum_serialized_bytes: int = Field(
        default=32 * 1024 * 1024,
        ge=4_096,
        le=256 * 1024 * 1024,
    )

    @model_validator(mode="after")
    def _pinned_detector_policy(self) -> Self:
        expected = {
            "snap_tolerance": 3.0,
            "join_tolerance": 3.0,
            "edge_min_length": 3.0,
            "min_words_vertical": 3,
            "min_words_horizontal": 1,
            "intersection_tolerance": 3.0,
            "text_tolerance": 3.0,
            "scanned_image_page_area_ratio": 0.8,
        }
        for field_name, expected_value in expected.items():
            if getattr(self, field_name) != expected_value:
                raise ValueError(f"{field_name} is pinned by {_POLICY_VERSION}")
        return self


class PdfDetectorIdentity(BaseModel):
    """Exact library, engine, policy, and canonical configuration identity."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    detector_name: Literal["PyMuPDF.Page.find_tables"]
    detector_version: Literal["pymupdf-dual-table-detector@1"]
    pymupdf_version: str = Field(min_length=1, max_length=64)
    mupdf_version: str = Field(min_length=1, max_length=64)
    canonical_config_json: str = Field(min_length=2)
    configuration_sha256: str = Field(pattern=_SHA256_PATTERN)
    detector_identity_sha256: str = Field(pattern=_SHA256_PATTERN)

    _configuration_hash = field_validator("configuration_sha256")(_validate_sha256)
    _identity_hash = field_validator("detector_identity_sha256")(_validate_sha256)

    @model_validator(mode="after")
    def _commitments(self) -> Self:
        try:
            config_value = json.loads(self.canonical_config_json)
        except json.JSONDecodeError as error:
            raise ValueError("canonical detector configuration must be JSON") from error
        if _canonical_json(config_value) != self.canonical_config_json:
            raise ValueError("detector configuration JSON is not canonical")
        if _digest(config_value) != self.configuration_sha256:
            raise ValueError("detector configuration commitment mismatch")
        identity = {
            "detector_name": self.detector_name,
            "detector_version": self.detector_version,
            "pymupdf_version": self.pymupdf_version,
            "mupdf_version": self.mupdf_version,
            "configuration_sha256": self.configuration_sha256,
        }
        if _digest(identity) != self.detector_identity_sha256:
            raise ValueError("detector identity commitment mismatch")
        return self


class PdfBoundingBox(BaseModel):
    """Four coordinates in the explicitly recorded page coordinate space."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    x0: float
    y0: float
    x1: float
    y1: float

    @model_validator(mode="after")
    def _valid_rectangle(self) -> Self:
        coordinates = (self.x0, self.y0, self.x1, self.y1)
        if any(not math.isfinite(value) for value in coordinates):
            raise ValueError("PDF bounding boxes require finite coordinates")
        if self.x1 < self.x0 or self.y1 < self.y0:
            raise ValueError("PDF bounding box right/bottom precedes left/top")
        return self


class PdfMatrix(BaseModel):
    """A PyMuPDF affine matrix recorded without lossy string formatting."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    a: float
    b: float
    c: float
    d: float
    e: float
    f: float

    @model_validator(mode="after")
    def _finite(self) -> Self:
        if any(
            not math.isfinite(value) for value in (self.a, self.b, self.c, self.d, self.e, self.f)
        ):
            raise ValueError("PDF matrices require finite coordinates")
        return self


class PdfTableCell(BaseModel):
    """One position-preserving cell; empty text and absent geometry are retained."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    page_number: int = Field(gt=0)
    table_index: int = Field(gt=0)
    row_index: int = Field(gt=0)
    column_index: int = Field(gt=0)
    bbox: PdfBoundingBox | None
    text: str
    canonical_sha256: str = Field(pattern=_SHA256_PATTERN)

    _commitment_hash = field_validator("canonical_sha256")(_validate_sha256)

    @model_validator(mode="after")
    def _commitment(self) -> Self:
        if _digest(_cell_payload(self)) != self.canonical_sha256:
            raise ValueError("PDF table cell commitment mismatch")
        return self


class PdfTableRow(BaseModel):
    """One ordered row and its exact ordered cell commitments."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    page_number: int = Field(gt=0)
    table_index: int = Field(gt=0)
    row_index: int = Field(gt=0)
    bbox: PdfBoundingBox | None
    cells: tuple[PdfTableCell, ...] = Field(min_length=1)
    canonical_sha256: str = Field(pattern=_SHA256_PATTERN)

    _commitment_hash = field_validator("canonical_sha256")(_validate_sha256)

    @model_validator(mode="after")
    def _ordered_cells_and_commitment(self) -> Self:
        for column_index, cell in enumerate(self.cells, start=1):
            if (
                cell.page_number != self.page_number
                or cell.table_index != self.table_index
                or cell.row_index != self.row_index
                or cell.column_index != column_index
            ):
                raise ValueError("PDF table cells are not in canonical row order")
        if _digest(_row_payload(self)) != self.canonical_sha256:
            raise ValueError("PDF table row commitment mismatch")
        return self


class PdfExtractedTable(BaseModel):
    """One deterministically ordered table selected from the dual strategy union."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    page_number: int = Field(gt=0)
    table_index: int = Field(gt=0)
    detected_by: tuple[DetectorStrategy, ...] = Field(min_length=1, max_length=2)
    bbox: PdfBoundingBox
    row_count: int = Field(gt=0)
    column_count: int = Field(gt=0)
    rows: tuple[PdfTableRow, ...] = Field(min_length=1)
    canonical_sha256: str = Field(pattern=_SHA256_PATTERN)

    _commitment_hash = field_validator("canonical_sha256")(_validate_sha256)

    @model_validator(mode="after")
    def _shape_order_and_commitment(self) -> Self:
        if self.detected_by not in {("lines",), ("text",), ("lines", "text")}:
            raise ValueError("detected_by is not in pinned strategy order")
        if len(self.rows) != self.row_count:
            raise ValueError("PDF table row_count does not match rows")
        for row_index, row in enumerate(self.rows, start=1):
            if (
                row.page_number != self.page_number
                or row.table_index != self.table_index
                or row.row_index != row_index
                or len(row.cells) != self.column_count
            ):
                raise ValueError("PDF table rows are not in canonical order or shape")
        if _digest(_table_payload(self)) != self.canonical_sha256:
            raise ValueError("PDF table commitment mismatch")
        return self


class PdfPageTableExtraction(BaseModel):
    """Terminal table disposition for one PDF page."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    page_number: int = Field(gt=0)
    disposition: PageDisposition
    quarantine_reason: str | None = Field(default=None, min_length=1, max_length=128)
    coordinate_space: Literal["pymupdf-rotated-page-points"] = _COORDINATE_SPACE
    rotation_degrees: Literal[0, 90, 180, 270]
    page_bbox: PdfBoundingBox
    media_box: PdfBoundingBox
    crop_box: PdfBoundingBox
    rotation_matrix: PdfMatrix
    derotation_matrix: PdfMatrix
    tables: tuple[PdfExtractedTable, ...]
    canonical_sha256: str = Field(pattern=_SHA256_PATTERN)

    _commitment_hash = field_validator("canonical_sha256")(_validate_sha256)

    @model_validator(mode="after")
    def _terminal_state_and_commitment(self) -> Self:
        if self.disposition == "tables_detected":
            if not self.tables or self.quarantine_reason is not None:
                raise ValueError("tables_detected requires tables and no quarantine reason")
        elif self.disposition == "no_tables_detected":
            if self.tables or self.quarantine_reason is not None:
                raise ValueError("no_tables_detected cannot include tables or a reason")
        elif self.tables or self.quarantine_reason is None:
            raise ValueError("quarantined pages require a reason and cannot publish tables")
        for table_index, table in enumerate(self.tables, start=1):
            if table.page_number != self.page_number or table.table_index != table_index:
                raise ValueError("PDF page tables are not in canonical order")
        if _digest(_page_payload(self)) != self.canonical_sha256:
            raise ValueError("PDF page commitment mismatch")
        return self


class PdfTableExtractionArtifact(BaseModel):
    """Provider-neutral sealed output or explicit quarantine artifact."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["pdf-table-extraction@1"] = _SCHEMA_VERSION
    disposition: DocumentDisposition
    quarantine_reason: str | None = Field(default=None, min_length=1, max_length=128)
    raw_pdf_sha256: str = Field(pattern=_SHA256_PATTERN)
    raw_byte_count: int = Field(ge=0)
    pdf_page_count: int | None = Field(default=None, ge=0)
    detector: PdfDetectorIdentity
    pages: tuple[PdfPageTableExtraction, ...]
    ordered_page_table_seal_sha256: str = Field(pattern=_SHA256_PATTERN)

    _raw_hash = field_validator("raw_pdf_sha256")(_validate_sha256)
    _seal_hash = field_validator("ordered_page_table_seal_sha256")(_validate_sha256)

    @model_validator(mode="after")
    def _closed_document_and_seal(self) -> Self:
        if self.disposition == "sealed":
            if self.quarantine_reason is not None:
                raise ValueError("sealed PDF extraction cannot have a quarantine reason")
            if self.pdf_page_count is None or self.pdf_page_count != len(self.pages):
                raise ValueError("sealed PDF extraction requires a complete page inventory")
            if any(page.disposition == "quarantined" for page in self.pages):
                raise ValueError("sealed PDF extraction cannot contain quarantined pages")
        elif self.quarantine_reason is None:
            raise ValueError("quarantined PDF extraction requires a reason")
        for page_number, page in enumerate(self.pages, start=1):
            if page.page_number != page_number:
                raise ValueError("PDF pages are not in canonical order")
        if _digest(_document_seal_payload(self)) != self.ordered_page_table_seal_sha256:
            raise ValueError("ordered PDF page/table seal mismatch")
        return self


@dataclass(frozen=True, slots=True)
class _RawCell:
    bbox: tuple[float, float, float, float] | None
    text: str


@dataclass(frozen=True, slots=True)
class _RawTable:
    strategy: DetectorStrategy
    bbox: tuple[float, float, float, float]
    rows: tuple[tuple[_RawCell, ...], ...]
    detected_by: tuple[DetectorStrategy, ...]

    @property
    def row_count(self) -> int:
        return len(self.rows)

    @property
    def column_count(self) -> int:
        return len(self.rows[0])


class _PageQuarantineError(RuntimeError):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class PdfTableDependencyError(RuntimeError):
    """Raised when the optional, pinned PDF detector dependency is unavailable."""


class _RectProtocol(Protocol):
    x0: float
    y0: float
    x1: float
    y1: float


class _MatrixProtocol(Protocol):
    a: float
    b: float
    c: float
    d: float
    e: float
    f: float


class _PageProtocol(Protocol):
    rotation: int
    rect: _RectProtocol
    mediabox: _RectProtocol
    cropbox: _RectProtocol
    rotation_matrix: _MatrixProtocol
    derotation_matrix: _MatrixProtocol

    def find_tables(self, **kwargs: object) -> object: ...

    def get_text(self, option: str) -> object: ...

    def get_images(self, *, full: bool) -> object: ...

    def get_image_info(self, *, hashes: bool, xrefs: bool) -> object: ...


class _DocumentProtocol(Protocol):
    page_count: int
    needs_pass: bool

    def __enter__(self) -> Self: ...

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None: ...

    def __getitem__(self, page_index: int) -> _PageProtocol: ...


class _PyMuPDFProtocol(Protocol):
    VersionBind: str
    VersionFitz: str

    def open(self, *, stream: bytes, filetype: str) -> _DocumentProtocol: ...


def _load_pymupdf() -> _PyMuPDFProtocol:
    try:
        module = importlib.import_module("fitz")
    except ImportError as error:
        raise PdfTableDependencyError(
            "PDF table extraction requires the optional PyMuPDF dependency"
        ) from error
    version_bind = getattr(module, "VersionBind", None)
    version_fitz = getattr(module, "VersionFitz", None)
    open_pdf = getattr(module, "open", None)
    if not isinstance(version_bind, str) or not isinstance(version_fitz, str):
        raise PdfTableDependencyError("installed PyMuPDF does not expose version identity")
    if not callable(open_pdf):
        raise PdfTableDependencyError("installed PyMuPDF does not expose fitz.open")
    return cast(_PyMuPDFProtocol, module)


def _cell_payload(cell: PdfTableCell) -> dict[str, object]:
    return {
        "page_number": cell.page_number,
        "table_index": cell.table_index,
        "row_index": cell.row_index,
        "column_index": cell.column_index,
        "bbox": None if cell.bbox is None else cell.bbox.model_dump(mode="json"),
        "text": cell.text,
    }


def _row_payload(row: PdfTableRow) -> dict[str, object]:
    return {
        "page_number": row.page_number,
        "table_index": row.table_index,
        "row_index": row.row_index,
        "bbox": None if row.bbox is None else row.bbox.model_dump(mode="json"),
        "cell_sha256s": [cell.canonical_sha256 for cell in row.cells],
    }


def _table_payload(table: PdfExtractedTable) -> dict[str, object]:
    return {
        "page_number": table.page_number,
        "table_index": table.table_index,
        "detected_by": list(table.detected_by),
        "bbox": table.bbox.model_dump(mode="json"),
        "row_count": table.row_count,
        "column_count": table.column_count,
        "row_sha256s": [row.canonical_sha256 for row in table.rows],
    }


def _page_payload(page: PdfPageTableExtraction) -> dict[str, object]:
    return {
        "page_number": page.page_number,
        "disposition": page.disposition,
        "quarantine_reason": page.quarantine_reason,
        "coordinate_space": page.coordinate_space,
        "rotation_degrees": page.rotation_degrees,
        "page_bbox": page.page_bbox.model_dump(mode="json"),
        "media_box": page.media_box.model_dump(mode="json"),
        "crop_box": page.crop_box.model_dump(mode="json"),
        "rotation_matrix": page.rotation_matrix.model_dump(mode="json"),
        "derotation_matrix": page.derotation_matrix.model_dump(mode="json"),
        "table_sha256s": [table.canonical_sha256 for table in page.tables],
    }


def _document_seal_payload(artifact: PdfTableExtractionArtifact) -> dict[str, object]:
    return {
        "schema_version": artifact.schema_version,
        "disposition": artifact.disposition,
        "quarantine_reason": artifact.quarantine_reason,
        "raw_pdf_sha256": artifact.raw_pdf_sha256,
        "raw_byte_count": artifact.raw_byte_count,
        "pdf_page_count": artifact.pdf_page_count,
        "detector_identity_sha256": artifact.detector.detector_identity_sha256,
        "configuration_sha256": artifact.detector.configuration_sha256,
        "pages": [
            {
                "page_number": page.page_number,
                "page_sha256": page.canonical_sha256,
                "table_sha256s": [table.canonical_sha256 for table in page.tables],
            }
            for page in artifact.pages
        ],
    }


def _detector_identity(
    config: PdfTableExtractionConfig,
    pymupdf: _PyMuPDFProtocol,
) -> PdfDetectorIdentity:
    config_value = config.model_dump(mode="json")
    canonical_config = _canonical_json(config_value)
    config_sha = _digest(config_value)
    identity = {
        "detector_name": "PyMuPDF.Page.find_tables",
        "detector_version": "pymupdf-dual-table-detector@1",
        "pymupdf_version": pymupdf.VersionBind,
        "mupdf_version": pymupdf.VersionFitz,
        "configuration_sha256": config_sha,
    }
    return PdfDetectorIdentity(
        detector_name="PyMuPDF.Page.find_tables",
        detector_version="pymupdf-dual-table-detector@1",
        pymupdf_version=pymupdf.VersionBind,
        mupdf_version=pymupdf.VersionFitz,
        canonical_config_json=canonical_config,
        configuration_sha256=config_sha,
        detector_identity_sha256=_digest(identity),
    )


def _bbox(value: Sequence[float]) -> PdfBoundingBox:
    if len(value) != 4:
        raise _PageQuarantineError("detector_bbox_contract_invalid")
    return PdfBoundingBox(
        x0=_coordinate(float(value[0])),
        y0=_coordinate(float(value[1])),
        x1=_coordinate(float(value[2])),
        y1=_coordinate(float(value[3])),
    )


def _raw_bbox(value: object) -> tuple[float, float, float, float] | None:
    if value is None:
        return None
    if not isinstance(value, (tuple, list)):
        raise _PageQuarantineError("detector_bbox_contract_invalid")
    items = cast(Sequence[object], value)
    if len(items) != 4 or any(not isinstance(item, (int, float)) for item in items):
        raise _PageQuarantineError("detector_bbox_contract_invalid")
    return (
        _coordinate(float(cast(float | int, items[0]))),
        _coordinate(float(cast(float | int, items[1]))),
        _coordinate(float(cast(float | int, items[2]))),
        _coordinate(float(cast(float | int, items[3]))),
    )


def _matrix(value: Sequence[float]) -> PdfMatrix:
    if len(value) != 6:
        raise _PageQuarantineError("page_matrix_contract_invalid")
    parts = tuple(_coordinate(float(item)) for item in value)
    return PdfMatrix(
        a=parts[0],
        b=parts[1],
        c=parts[2],
        d=parts[3],
        e=parts[4],
        f=parts[5],
    )


class _TableRowProtocol(Protocol):
    cells: Sequence[object]


class _TableProtocol(Protocol):
    row_count: int
    col_count: int
    bbox: object
    rows: Sequence[_TableRowProtocol]

    def extract(self) -> list[list[str | None]]: ...


class _TableFinderProtocol(Protocol):
    tables: Sequence[_TableProtocol]


def _detect_strategy(
    page: _PageProtocol,
    strategy: DetectorStrategy,
    config: PdfTableExtractionConfig,
) -> tuple[_RawTable, ...]:
    """Internal deterministic seam kept patchable for disagreement tests."""

    kwargs: dict[str, object] = {
        "strategy": strategy,
        "snap_tolerance": config.snap_tolerance,
        "join_tolerance": config.join_tolerance,
        "edge_min_length": config.edge_min_length,
        "min_words_vertical": config.min_words_vertical,
        "min_words_horizontal": config.min_words_horizontal,
        "intersection_tolerance": config.intersection_tolerance,
        "text_tolerance": config.text_tolerance,
    }
    # PyMuPDF 1.27 emits an optional-package suggestion on stdout.  The
    # detector result, not process-global console noise, is this pure lane's output.
    with contextlib.redirect_stdout(io.StringIO()):
        finder = page.find_tables(**kwargs)
    if finder is None or not hasattr(finder, "tables"):
        raise _PageQuarantineError("detector_contract_invalid")
    typed_finder = cast(_TableFinderProtocol, finder)
    raw_tables = list(typed_finder.tables)
    if len(raw_tables) > config.maximum_tables_per_page:
        raise _PageQuarantineError("tables_per_page_limit_exceeded")

    detected: list[_RawTable] = []
    for table in raw_tables:
        row_count = int(table.row_count)
        column_count = int(table.col_count)
        if row_count <= 0 or column_count <= 0:
            raise _PageQuarantineError("detector_shape_contract_invalid")
        cell_count = row_count * column_count
        if cell_count > config.maximum_cells_per_table:
            raise _PageQuarantineError("cells_per_table_limit_exceeded")

        # The hard cell cap above is checked before text/cell materialization.
        extracted = table.extract()
        table_rows = list(table.rows)
        if len(extracted) != row_count or len(table_rows) != row_count:
            raise _PageQuarantineError("detector_shape_contract_invalid")
        rows: list[tuple[_RawCell, ...]] = []
        for row_geometry, row_text in zip(table_rows, extracted, strict=True):
            geometries = list(row_geometry.cells)
            texts = list(row_text)
            if len(geometries) != column_count or len(texts) != column_count:
                raise _PageQuarantineError("detector_shape_contract_invalid")
            cells = tuple(
                _RawCell(
                    bbox=_raw_bbox(cell_bbox),
                    text="" if text is None else str(text),
                )
                for cell_bbox, text in zip(geometries, texts, strict=True)
            )
            rows.append(cells)
        table_bbox = _raw_bbox(table.bbox)
        if table_bbox is None:
            raise _PageQuarantineError("detector_bbox_contract_invalid")
        detected.append(
            _RawTable(
                strategy=strategy,
                bbox=table_bbox,
                rows=tuple(rows),
                detected_by=(strategy,),
            )
        )
    return tuple(detected)


def _area(bbox: tuple[float, float, float, float]) -> float:
    return max(0.0, bbox[2] - bbox[0]) * max(0.0, bbox[3] - bbox[1])


def _intersection_area(
    left: tuple[float, float, float, float],
    right: tuple[float, float, float, float],
) -> float:
    width = max(0.0, min(left[2], right[2]) - max(left[0], right[0]))
    height = max(0.0, min(left[3], right[3]) - max(left[1], right[1]))
    return width * height


def _substantive_rows(table: _RawTable) -> tuple[tuple[str, ...], ...]:
    return tuple(
        tuple(cell.text for cell in row)
        for row in table.rows
        if any(cell.text != "" for cell in row)
    )


def _merge_detector_results(
    by_strategy: dict[DetectorStrategy, tuple[_RawTable, ...]],
) -> tuple[_RawTable, ...]:
    candidates = sorted(
        (table for strategy in _STRATEGIES for table in by_strategy[strategy]),
        key=lambda table: (*table.bbox[1::-1], table.bbox[3], table.bbox[2], table.strategy),
    )
    accepted: list[_RawTable] = []
    for candidate in candidates:
        duplicate_index: int | None = None
        for index, existing in enumerate(accepted):
            intersection = _intersection_area(candidate.bbox, existing.bbox)
            if intersection == 0:
                continue
            minimum_area = min(_area(candidate.bbox), _area(existing.bbox))
            same_region = minimum_area > 0 and intersection / minimum_area >= 0.75
            if (
                same_region
                and candidate.strategy != existing.strategy
                and candidate.column_count == existing.column_count
                and _substantive_rows(candidate) == _substantive_rows(existing)
            ):
                duplicate_index = index
                break
            if same_region and candidate.strategy != existing.strategy:
                raise _PageQuarantineError("detector_disagreement")
            raise _PageQuarantineError("ambiguous_table_overlap")
        if duplicate_index is None:
            accepted.append(candidate)
            continue
        existing = accepted[duplicate_index]
        detected_by = tuple(
            strategy
            for strategy in _STRATEGIES
            if strategy in {existing.strategy, candidate.strategy}
        )
        # Prefer line geometry when the complementary text strategy agrees on
        # the substantive grid; otherwise retain the already canonical candidate.
        chosen = existing if existing.strategy == "lines" else candidate
        accepted[duplicate_index] = replace(
            chosen,
            detected_by=cast(tuple[DetectorStrategy, ...], detected_by),
        )
    return tuple(
        sorted(
            accepted,
            key=lambda table: (table.bbox[1], table.bbox[0], table.bbox[3], table.bbox[2]),
        )
    )


def _build_table(raw: _RawTable, page_number: int, table_index: int) -> PdfExtractedTable:
    rows: list[PdfTableRow] = []
    for row_index, raw_row in enumerate(raw.rows, start=1):
        cells: list[PdfTableCell] = []
        for column_index, raw_cell in enumerate(raw_row, start=1):
            cell_bbox = None if raw_cell.bbox is None else _bbox(raw_cell.bbox)
            payload = {
                "page_number": page_number,
                "table_index": table_index,
                "row_index": row_index,
                "column_index": column_index,
                "bbox": None if cell_bbox is None else cell_bbox.model_dump(mode="json"),
                "text": raw_cell.text,
            }
            cells.append(
                PdfTableCell(
                    page_number=page_number,
                    table_index=table_index,
                    row_index=row_index,
                    column_index=column_index,
                    bbox=cell_bbox,
                    text=raw_cell.text,
                    canonical_sha256=_digest(payload),
                )
            )
        present_boxes = [cell.bbox for cell in cells if cell.bbox is not None]
        row_bbox = _bounding_union(present_boxes)
        row_payload = {
            "page_number": page_number,
            "table_index": table_index,
            "row_index": row_index,
            "bbox": None if row_bbox is None else row_bbox.model_dump(mode="json"),
            "cell_sha256s": [cell.canonical_sha256 for cell in cells],
        }
        rows.append(
            PdfTableRow(
                page_number=page_number,
                table_index=table_index,
                row_index=row_index,
                bbox=row_bbox,
                cells=tuple(cells),
                canonical_sha256=_digest(row_payload),
            )
        )
    table_bbox = _bbox(raw.bbox)
    table_payload = {
        "page_number": page_number,
        "table_index": table_index,
        "detected_by": list(raw.detected_by),
        "bbox": table_bbox.model_dump(mode="json"),
        "row_count": raw.row_count,
        "column_count": raw.column_count,
        "row_sha256s": [row.canonical_sha256 for row in rows],
    }
    return PdfExtractedTable(
        page_number=page_number,
        table_index=table_index,
        detected_by=raw.detected_by,
        bbox=table_bbox,
        row_count=raw.row_count,
        column_count=raw.column_count,
        rows=tuple(rows),
        canonical_sha256=_digest(table_payload),
    )


def _bounding_union(boxes: list[PdfBoundingBox]) -> PdfBoundingBox | None:
    if not boxes:
        return None
    return PdfBoundingBox(
        x0=min(box.x0 for box in boxes),
        y0=min(box.y0 for box in boxes),
        x1=max(box.x1 for box in boxes),
        y1=max(box.y1 for box in boxes),
    )


def _has_scanned_page_image(
    page: _PageProtocol,
    page_bbox: PdfBoundingBox,
    config: PdfTableExtractionConfig,
) -> bool:
    image_info_value = page.get_image_info(hashes=False, xrefs=False)
    if not isinstance(image_info_value, list):
        raise _PageQuarantineError("page_image_inventory_contract_invalid")
    page_area = (page_bbox.x1 - page_bbox.x0) * (page_bbox.y1 - page_bbox.y0)
    if page_area <= 0:
        raise _PageQuarantineError("page_bbox_contract_invalid")
    for raw_info in cast(list[object], image_info_value):
        if not isinstance(raw_info, dict):
            raise _PageQuarantineError("page_image_inventory_contract_invalid")
        info = cast(dict[str, object], raw_info)
        image_bbox = _raw_bbox(info.get("bbox"))
        if image_bbox is None:
            raise _PageQuarantineError("page_image_inventory_contract_invalid")
        if _area(image_bbox) / page_area >= config.scanned_image_page_area_ratio:
            return True
    return False


def _page_geometry(
    page: _PageProtocol,
) -> tuple[
    Literal[0, 90, 180, 270],
    PdfBoundingBox,
    PdfBoundingBox,
    PdfBoundingBox,
    PdfMatrix,
    PdfMatrix,
]:
    rotation = page.rotation
    if rotation not in {0, 90, 180, 270}:
        raise _PageQuarantineError("unsupported_page_rotation")
    page_rect = page.rect
    media_box = page.mediabox
    crop_box = page.cropbox
    rotation_matrix = page.rotation_matrix
    derotation_matrix = page.derotation_matrix
    return (
        cast(Literal[0, 90, 180, 270], rotation),
        _bbox((page_rect.x0, page_rect.y0, page_rect.x1, page_rect.y1)),
        _bbox((media_box.x0, media_box.y0, media_box.x1, media_box.y1)),
        _bbox((crop_box.x0, crop_box.y0, crop_box.x1, crop_box.y1)),
        _matrix(
            (
                rotation_matrix.a,
                rotation_matrix.b,
                rotation_matrix.c,
                rotation_matrix.d,
                rotation_matrix.e,
                rotation_matrix.f,
            )
        ),
        _matrix(
            (
                derotation_matrix.a,
                derotation_matrix.b,
                derotation_matrix.c,
                derotation_matrix.d,
                derotation_matrix.e,
                derotation_matrix.f,
            )
        ),
    )


def _build_page(
    page: _PageProtocol,
    page_number: int,
    config: PdfTableExtractionConfig,
) -> PdfPageTableExtraction:
    rotation, page_bbox, media_box, crop_box, rotation_matrix, derotation_matrix = _page_geometry(
        page
    )
    disposition: PageDisposition
    reason: str | None = None
    tables: tuple[PdfExtractedTable, ...] = ()
    try:
        by_strategy: dict[DetectorStrategy, tuple[_RawTable, ...]] = {
            "lines": _detect_strategy(page, "lines", config),
            "text": _detect_strategy(page, "text", config),
        }
        raw_tables = _merge_detector_results(by_strategy)
        if len(raw_tables) > config.maximum_tables_per_page:
            raise _PageQuarantineError("tables_per_page_limit_exceeded")
        if raw_tables:
            tables = tuple(
                _build_table(raw_table, page_number, table_index)
                for table_index, raw_table in enumerate(raw_tables, start=1)
            )
            disposition = "tables_detected"
        else:
            text_value = page.get_text("text")
            if not isinstance(text_value, str):
                raise _PageQuarantineError("page_text_contract_invalid")
            images_value = page.get_images(full=True)
            if not isinstance(images_value, list):
                raise _PageQuarantineError("page_image_inventory_contract_invalid")
            has_images = bool(cast(list[object], images_value))
            scanned_page = has_images and _has_scanned_page_image(page, page_bbox, config)
            if text_value == "" and has_images:
                disposition = "quarantined"
                reason = "image_only_page"
            elif scanned_page:
                disposition = "quarantined"
                reason = "scanned_page"
            else:
                disposition = "no_tables_detected"
    except _PageQuarantineError as error:
        disposition = "quarantined"
        reason = error.reason
        tables = ()
    except (RuntimeError, TypeError, ValueError):
        disposition = "quarantined"
        reason = "page_detector_failure"
        tables = ()
    unsealed = PdfPageTableExtraction.model_construct(
        page_number=page_number,
        disposition=disposition,
        quarantine_reason=reason,
        coordinate_space=_COORDINATE_SPACE,
        rotation_degrees=rotation,
        page_bbox=page_bbox,
        media_box=media_box,
        crop_box=crop_box,
        rotation_matrix=rotation_matrix,
        derotation_matrix=derotation_matrix,
        tables=tables,
        canonical_sha256="0" * 64,
    )
    return PdfPageTableExtraction(
        page_number=page_number,
        disposition=disposition,
        quarantine_reason=reason,
        coordinate_space=_COORDINATE_SPACE,
        rotation_degrees=rotation,
        page_bbox=page_bbox,
        media_box=media_box,
        crop_box=crop_box,
        rotation_matrix=rotation_matrix,
        derotation_matrix=derotation_matrix,
        tables=tables,
        canonical_sha256=_digest(_page_payload(unsealed)),
    )


def _artifact(
    *,
    disposition: DocumentDisposition,
    quarantine_reason: str | None,
    raw_pdf_sha256: str,
    raw_byte_count: int,
    pdf_page_count: int | None,
    detector: PdfDetectorIdentity,
    pages: tuple[PdfPageTableExtraction, ...],
) -> PdfTableExtractionArtifact:
    unsealed = PdfTableExtractionArtifact.model_construct(
        schema_version=_SCHEMA_VERSION,
        disposition=disposition,
        quarantine_reason=quarantine_reason,
        raw_pdf_sha256=raw_pdf_sha256,
        raw_byte_count=raw_byte_count,
        pdf_page_count=pdf_page_count,
        detector=detector,
        pages=pages,
        ordered_page_table_seal_sha256="0" * 64,
    )
    return PdfTableExtractionArtifact(
        schema_version=_SCHEMA_VERSION,
        disposition=disposition,
        quarantine_reason=quarantine_reason,
        raw_pdf_sha256=raw_pdf_sha256,
        raw_byte_count=raw_byte_count,
        pdf_page_count=pdf_page_count,
        detector=detector,
        pages=pages,
        ordered_page_table_seal_sha256=_digest(_document_seal_payload(unsealed)),
    )


def extract_pdf_tables(
    raw_pdf: bytes,
    *,
    config: PdfTableExtractionConfig | None = None,
) -> PdfTableExtractionArtifact:
    """Extract a replayable table artifact from exact immutable PDF bytes.

    A ``sealed`` result means every page reached a terminal non-quarantine
    disposition under the recorded detector policy.  It does not mean that no
    human-semantic table exists outside that policy.
    """

    extraction_config = config or PdfTableExtractionConfig()
    pymupdf = _load_pymupdf()
    detector = _detector_identity(extraction_config, pymupdf)
    raw_hash = sha256(raw_pdf).hexdigest()
    byte_count = len(raw_pdf)

    try:
        document = pymupdf.open(stream=raw_pdf, filetype="pdf")
    except (RuntimeError, TypeError, ValueError):
        return _artifact(
            disposition="quarantined",
            quarantine_reason="malformed_pdf",
            raw_pdf_sha256=raw_hash,
            raw_byte_count=byte_count,
            pdf_page_count=None,
            detector=detector,
            pages=(),
        )

    with document:
        page_count = document.page_count
        if document.needs_pass:
            return _artifact(
                disposition="quarantined",
                quarantine_reason="encrypted_pdf",
                raw_pdf_sha256=raw_hash,
                raw_byte_count=byte_count,
                pdf_page_count=page_count,
                detector=detector,
                pages=(),
            )
        if page_count > extraction_config.maximum_pages:
            return _artifact(
                disposition="quarantined",
                quarantine_reason="page_limit_exceeded",
                raw_pdf_sha256=raw_hash,
                raw_byte_count=byte_count,
                pdf_page_count=page_count,
                detector=detector,
                pages=(),
            )
        pages = tuple(
            _build_page(document[page_index], page_index + 1, extraction_config)
            for page_index in range(page_count)
        )

    quarantined = any(page.disposition == "quarantined" for page in pages)
    artifact = _artifact(
        disposition="quarantined" if quarantined else "sealed",
        quarantine_reason="one_or_more_pages_quarantined" if quarantined else None,
        raw_pdf_sha256=raw_hash,
        raw_byte_count=byte_count,
        pdf_page_count=page_count,
        detector=detector,
        pages=pages,
    )
    serialized_size = len(artifact.model_dump_json(exclude_none=False).encode("utf-8"))
    if serialized_size <= extraction_config.maximum_serialized_bytes:
        return artifact
    # Do not publish a partial page/table prefix as closed coverage.  The
    # bounded quarantine envelope retains exact input and detector identity.
    return _artifact(
        disposition="quarantined",
        quarantine_reason="serialized_output_limit_exceeded",
        raw_pdf_sha256=raw_hash,
        raw_byte_count=byte_count,
        pdf_page_count=page_count,
        detector=detector,
        pages=(),
    )
