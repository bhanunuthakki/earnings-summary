"""Neutral schema for immutable README updater release receipts."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from readme_updater import ReadmeUpdateResult


class LlmCallAttestation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: int = Field(gt=0)
    purpose: Literal["readme_update", "readme_update_judge"]
    template_id: str = Field(min_length=1)
    template_version: str = Field(min_length=1)
    prompt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    response_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    model: str = Field(min_length=1)
    provider: str | None = None
    transport: str | None = None


class StoredReadmeReceipt(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["2"]
    run_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    starting_readme_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    starting_readme: str = Field(min_length=1, max_length=250_000)
    evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    release_approved: bool
    link_violations: tuple[str, ...] = ()
    llm_calls: tuple[LlmCallAttestation, ...]
    result: ReadmeUpdateResult


__all__ = ["LlmCallAttestation", "StoredReadmeReceipt"]
