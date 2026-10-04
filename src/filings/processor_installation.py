"""Resolve one explicit installed processor authority; never discover candidates."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, model_validator

from filings.inline_xbrl_processor import (
    ApprovedProcessorBundle,
    InlineXbrlProcessorError,
    load_approved_processor_bundle_manifest,
    verify_processor_installation,
)
from provenance.immutable_artifact import read_stable_artifact, require_no_reparse_points
from runtime.secrets import project_env_file

INSTALLATION_ENV = "EARNINGS_SUMMARY_FILING_XBRL_INSTALLATION"


class FilingXbrlInstallation(BaseModel):
    """Local paths to one separately approved installation."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["filing-xbrl-installation/v1"]
    bundle_manifest: Path
    runtime_root: Path
    sandbox_launcher: Path

    @model_validator(mode="after")
    def _absolute_paths(self) -> FilingXbrlInstallation:
        for path in (self.bundle_manifest, self.runtime_root, self.sandbox_launcher):
            if not path.is_absolute() or str(path).startswith(("//", "\\\\")):
                raise ValueError(
                    "installation paths must be absolute and local on the execution host"
                )
        return self


@dataclass(frozen=True)
class ResolvedFilingXbrlInstallation:
    installation: FilingXbrlInstallation
    approved_bundle: ApprovedProcessorBundle
    bundle_python: Path


def resolve_processor_installation(
    *,
    project_root: Path,
    installation_path: Path | None = None,
    bundle_manifest: Path | None = None,
    runtime_root: Path | None = None,
    sandbox_launcher: Path | None = None,
    bundle_python: Path | None = None,
) -> ResolvedFilingXbrlInstallation:
    """Resolve explicit paths or one configured descriptor, then verify all sealed bytes."""

    direct = (bundle_manifest, runtime_root, sandbox_launcher)
    if any(path is not None for path in direct):
        if installation_path is not None or not all(path is not None for path in direct):
            raise InlineXbrlProcessorError(
                "use one installation descriptor or all three installed paths",
                reason_code="installation_arguments_incomplete",
            )
        installation = FilingXbrlInstallation.model_validate(
            {
                "schema_version": "filing-xbrl-installation/v1",
                "bundle_manifest": bundle_manifest,
                "runtime_root": runtime_root,
                "sandbox_launcher": sandbox_launcher,
            }
        )
    else:
        if installation_path is None:
            configured = os.environ.get(INSTALLATION_ENV, "").strip()
            if not configured:
                configured = (
                    dotenv_values(project_env_file(project_root), interpolate=False).get(
                        INSTALLATION_ENV
                    )
                    or ""
                ).strip()
            if not configured:
                raise InlineXbrlProcessorError(
                    "filing-XBRL installation is not configured",
                    reason_code="installation_not_configured",
                )
            installation_path = Path(configured)
        if not installation_path.is_absolute() or str(installation_path).startswith(("//", "\\\\")):
            raise InlineXbrlProcessorError(
                "installation descriptor path must be absolute",
                reason_code="installation_descriptor_invalid",
            )
        require_no_reparse_points(installation_path)
        if installation_path.stat().st_nlink != 1 or installation_path.stat().st_size > 65536:
            raise InlineXbrlProcessorError(
                "installation descriptor is aliased or too large",
                reason_code="installation_descriptor_invalid",
            )
        _, body = read_stable_artifact(installation_path)
        if installation_path.stat().st_nlink != 1 or len(body) > 65536:
            raise InlineXbrlProcessorError(
                "installation descriptor is aliased or too large",
                reason_code="installation_descriptor_invalid",
            )
        installation = FilingXbrlInstallation.model_validate_json(body)
    approved = load_approved_processor_bundle_manifest(installation.bundle_manifest)
    python = verify_processor_installation(
        approved,
        runtime_root=installation.runtime_root,
        sandbox_launcher=installation.sandbox_launcher,
        bundle_python=bundle_python,
    )
    return ResolvedFilingXbrlInstallation(installation, approved, python)
