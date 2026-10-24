"""Explicit local byte-read authority shared by financial source consumers."""

from __future__ import annotations

from pathlib import Path
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, field_validator


class SourceReadContext(BaseModel):
    """A caller grants named content roots; a database location grants none."""

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    content_roots: tuple[Path, ...]
    ocr_state_root: Path | None = None
    max_documents: int = Field(default=28, gt=0, le=28, strict=True)
    max_document_bytes: int = Field(default=100_000_000, gt=0, le=100_000_000, strict=True)
    max_total_bytes: int = Field(default=250_000_000, gt=0, le=250_000_000, strict=True)

    @field_validator("content_roots")
    @classmethod
    def explicit_roots(cls, roots: tuple[Path, ...]) -> tuple[Path, ...]:
        if not roots or any(not root.is_absolute() or root == Path(root.anchor) for root in roots):
            raise ValueError("explicit absolute content roots are required")
        if any(".." in root.parts for root in roots):
            raise ValueError("content roots must be lexical absolute paths")
        return roots

    @field_validator("ocr_state_root")
    @classmethod
    def explicit_ocr_root(cls, root: Path | None) -> Path | None:
        if root is not None and (
            not root.is_absolute() or root == Path(root.anchor) or ".." in root.parts
        ):
            raise ValueError("explicit absolute OCR state root is required")
        return root

    @classmethod
    def for_sec_state_root(cls, state_root: Path) -> Self:
        """Preserve the existing SEC source-byte authority exactly."""
        if (
            not state_root.is_absolute()
            or state_root == Path(state_root.anchor)
            or ".." in state_root.parts
        ):
            raise ValueError("explicit absolute state root is required")
        return cls(content_roots=(state_root / "data" / "evidence" / "blobs",))

    @classmethod
    def for_product_state_root(cls, state_root: Path) -> Self:
        """Grant named SEC, IR PDF, and OCR reads under an explicit state root."""
        base = cls.for_sec_state_root(state_root)
        return base.model_copy(
            update={
                "content_roots": (*base.content_roots, state_root / "ir_documents"),
                "ocr_state_root": state_root,
            }
        )
