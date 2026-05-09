"""Models for the manual IR-upload categorizer.

The categorizer classifies a file dropped in `ir_documents/` (root level) into a
`(ticker, doc_type, period_end)` tuple, then registers it in the canonical
`documents` table per the data-provenance contract. These models are the
deterministic contract between the classifier (`src/ir_uploads.py`) and the CLI
(`execution/categorize_ir_uploads.py`).
"""

from __future__ import annotations

from datetime import date
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, Field

from .documents import DocType


class Confidence(StrEnum):
    """How sure the classifier is. `LOW` rows are rejected (never registered)."""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class CategorizationResult(BaseModel):
    """Successful classification — promoted into a `documents` row."""

    ticker: str
    doc_type: DocType
    period_end: date
    period_label: str = Field(description="Human label, e.g. 'Q3 2024' or 'FY 2025'")
    confidence: Confidence
    ticker_evidence: list[str]
    doc_type_evidence: list[str]
    period_evidence: list[str]


class CategorizationFailure(BaseModel):
    """Classifier could not identify ticker, doc_type, or period with sufficient confidence.

    Files with this outcome are moved to `ir_documents/_unsorted/` alongside a
    sidecar `.error.json` containing this payload — never registered, never
    silently dropped.
    """

    reason: str
    text_sample: str
    ticker_guess: str | None = None
    doc_type_guess: DocType | None = None
    period_guess: date | None = None


class CategorizedFile(BaseModel):
    """The fully-resolved record written to the run manifest."""

    original_path: Path
    sha256: str = Field(min_length=64, max_length=64)
    raw_bytes_size: int = Field(ge=0)
    canonical_path: Path
    result: CategorizationResult
