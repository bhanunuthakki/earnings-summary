"""Exact, source-bound layout representation for governed PDF OCR pages.

This leaf parses retained Tesseract TSV and verifies raster geometry. It has
no provider, database, or processing-seal authority.
"""

from __future__ import annotations

import csv
import hashlib
import io
import unicodedata
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from runtime.deterministic import normalize_extracted_text

_SHA256_PATTERN = r"^[0-9a-f]{64}$"


class OCRLayoutError(RuntimeError):
    """A bounded reason for invalid retained layout bytes or geometry."""

    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


class OCRWord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    ordinal: int = Field(ge=0)
    block: int = Field(ge=0)
    paragraph: int = Field(ge=0)
    line: int = Field(ge=0)
    word: int = Field(ge=0)
    text: str = Field(min_length=1)
    confidence: float = Field(ge=0, le=100)
    left: int = Field(ge=0)
    top: int = Field(ge=0)
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    char_start: int = Field(ge=0)
    char_end: int = Field(gt=0)


class OCRPageLayout(BaseModel):
    """Versioned raster coordinates; PDF boxes are not inferred from these words."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal["tesseract-tsv-raster@1"] = "tesseract-tsv-raster@1"
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    dpi: int = Field(gt=0)
    media_box: tuple[float, float, float, float]
    rotation: Literal[0, 90, 180, 270]
    raster_to_pdf: tuple[float, float, float, float, float, float]
    tsv_sha256: str = Field(pattern=_SHA256_PATTERN)
    render_sha256: str = Field(pattern=_SHA256_PATTERN)
    words: tuple[OCRWord, ...]

    @model_validator(mode="after")
    def _validate_geometry(self) -> OCRPageLayout:
        if any(
            word.left + word.width > self.width or word.top + word.height > self.height
            for word in self.words
        ):
            raise ValueError("OCR word exceeds raster bounds")
        left, bottom, right, top = self.media_box
        if right <= left or top <= bottom:
            raise ValueError("OCR media box is invalid")
        display_width, display_height = (
            (top - bottom, right - left)
            if self.rotation in (90, 270)
            else (right - left, top - bottom)
        )
        if (
            abs(self.width - display_width * self.dpi / 72) > 2
            or abs(self.height - display_height * self.dpi / 72) > 2
        ):
            raise ValueError("render dimensions do not match PDF geometry")
        if self.raster_to_pdf != _raster_to_pdf(
            self.media_box, self.rotation, self.width, self.height
        ):
            raise ValueError("raster transform does not match PDF geometry")
        return self


class OCRPageOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    page_number: int = Field(gt=0)
    text: str
    mean_confidence: float = Field(ge=0.0, le=100.0)
    layout: OCRPageLayout | None = None
    raw_tsv: bytes | None = Field(default=None, exclude=True)
    render_png: bytes | None = Field(default=None, exclude=True)


def _raster_to_pdf(
    box: tuple[float, float, float, float], rotation: int, width: int, height: int
) -> tuple[float, float, float, float, float, float]:
    left, bottom, right, top = box
    sx = (top - bottom if rotation in (90, 270) else right - left) / width
    sy = (right - left if rotation in (90, 270) else top - bottom) / height
    if rotation == 0:
        return (sx, 0.0, left, 0.0, -sy, top)
    if rotation == 90:
        return (0.0, sy, left, sx, 0.0, bottom)
    if rotation == 180:
        return (-sx, 0.0, right, 0.0, sy, bottom)
    return (0.0, -sy, right, -sx, 0.0, top)


def png_dimensions(raw: bytes) -> tuple[int, int]:
    if len(raw) < 24 or raw[:8] != b"\x89PNG\r\n\x1a\n" or raw[12:16] != b"IHDR":
        raise OCRLayoutError("invalid_render_png")
    width, height = int.from_bytes(raw[16:20], "big"), int.from_bytes(raw[20:24], "big")
    if not 0 < width <= 100_000 or not 0 < height <= 100_000 or width * height > 100_000_000:
        raise OCRLayoutError("invalid_render_dimensions")
    return width, height


def parse_tesseract_pdf_tsv(
    page_number: int,
    raw_tsv: bytes,
    *,
    render_png: bytes,
    width: int,
    height: int,
    dpi: int,
    media_box: tuple[float, float, float, float],
    rotation: int,
) -> OCRPageOutput:
    try:
        decoded = raw_tsv.decode("utf-8")
        rows = csv.DictReader(io.StringIO(decoded), delimiter="\t")
        required = {
            "level",
            "page_num",
            "block_num",
            "par_num",
            "line_num",
            "word_num",
            "left",
            "top",
            "width",
            "height",
            "conf",
            "text",
        }
        if rows.fieldnames is None or not required.issubset(rows.fieldnames):
            raise ValueError("TSV columns missing")
        words: list[OCRWord] = []
        confidences: list[float] = []
        offset = 0
        for row in rows:
            if int(row["level"]) != 5:
                continue
            if int(row["page_num"]) != 1:
                raise ValueError("TSV page mismatch")
            raw_word = row["text"]
            confidence = float(row["conf"])
            if raw_word:
                if raw_word != raw_word.strip() or confidence < 0:
                    raise ValueError("nonempty OCR word has invalid text or confidence")
                word_text = unicodedata.normalize("NFKC", raw_word)
                if words:
                    offset += 1
                words.append(
                    OCRWord(
                        ordinal=len(words),
                        block=int(row["block_num"]),
                        paragraph=int(row["par_num"]),
                        line=int(row["line_num"]),
                        word=int(row["word_num"]),
                        text=word_text,
                        confidence=confidence,
                        left=int(row["left"]),
                        top=int(row["top"]),
                        width=int(row["width"]),
                        height=int(row["height"]),
                        char_start=offset,
                        char_end=offset + len(word_text),
                    )
                )
                confidences.append(confidence)
                offset += len(word_text)
        text = " ".join(word.text for word in words)
        if normalize_extracted_text(text) != text:
            raise ValueError("OCR word text cannot use the declared text normalization")
    except (KeyError, TypeError, ValueError, UnicodeDecodeError):
        raise OCRLayoutError("invalid_ocr_output") from None
    try:
        if rotation not in (0, 90, 180, 270):
            raise ValueError("unsupported rotation")
        layout = OCRPageLayout(
            width=width,
            height=height,
            dpi=dpi,
            media_box=media_box,
            rotation=rotation,
            raster_to_pdf=_raster_to_pdf(media_box, rotation, width, height),
            tsv_sha256=hashlib.sha256(raw_tsv).hexdigest(),
            render_sha256=hashlib.sha256(render_png).hexdigest(),
            words=tuple(words),
        )
    except ValueError:
        raise OCRLayoutError("invalid_ocr_layout") from None
    return OCRPageOutput(
        page_number=page_number,
        text=text,
        mean_confidence=sum(confidences) / len(confidences) if confidences else 0.0,
        layout=layout,
        raw_tsv=raw_tsv,
        render_png=render_png,
    )


def verify_ocr_layout_bytes(
    layout: OCRPageLayout, raw_tsv: bytes, render_png: bytes
) -> OCRPageOutput:
    """Reparse exact retained output without an OCR provider or inferred words."""
    if png_dimensions(render_png) != (layout.width, layout.height):
        raise OCRLayoutError("ocr_render_dimensions_mismatch")
    rebuilt = parse_tesseract_pdf_tsv(
        1,
        raw_tsv,
        render_png=render_png,
        width=layout.width,
        height=layout.height,
        dpi=layout.dpi,
        media_box=layout.media_box,
        rotation=layout.rotation,
    )
    if rebuilt.layout != layout:
        raise OCRLayoutError("ocr_layout_reconstruction_mismatch")
    return rebuilt
