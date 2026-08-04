"""Qualified offline subprocess boundary for filing-native Inline XBRL.

The application never imports Arelle.  A separately built processor bundle
owns Arelle, EDGAR and XULE dependencies and speaks one closed JSON protocol.
The bridge runs with ``-I`` and offline connectivity; its runtime artifact
digest and exact coordinates are verified before any output is admitted.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import threading
from collections.abc import Generator, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Literal, Self, cast
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

_SHA256 = r"^[0-9a-f]{64}$"
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_CANONICAL_APPROVAL_SEAL = _PROJECT_ROOT / "config" / "filing_xbrl_processor_approval.json"
_APPROVAL_CAPABILITY = object()
_PACKAGE_CACHE_MAX_COMPLETED = 64
_PACKAGE_CACHE_MAX_BYTES = 8 * 1024 * 1024 * 1024
_PACKAGE_CACHE_MIN_FREE_BYTES = 2 * 1024 * 1024 * 1024


class InlineXbrlProcessorError(RuntimeError):
    """The isolated processor failed or violated its qualified contract."""


class _Closed(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ProcessorCoordinates(_Closed):
    arelle: Literal["2.39.8"]
    edgar: Literal["26.1"]
    xule: Literal["30052"]


class RuntimeArtifactMember(_Closed):
    relative_path: str = Field(min_length=1, max_length=1024)
    blob_sha256: str = Field(pattern=_SHA256)
    byte_size: int = Field(ge=0)

    @model_validator(mode="after")
    def _normalized_path(self) -> Self:
        path = PurePosixPath(self.relative_path)
        if (
            path.is_absolute()
            or "\\" in self.relative_path
            or any(part in {"", ".", ".."} for part in path.parts)
            or path.as_posix() != self.relative_path
        ):
            raise ValueError("runtime member path must be normalized and relative")
        return self


class ProcessorExecution(_Closed):
    isolated_python: Literal[True]
    internet_connectivity: Literal["os_denied"]
    sandbox_contract_version: Literal["earnings-xbrl-os-sandbox.v1"]
    sandbox_launcher_sha256: str = Field(pattern=_SHA256)
    bundle_python_sha256: str = Field(pattern=_SHA256)
    bundle_python_relative_path: str = Field(min_length=1, max_length=1024)
    runtime_members: tuple[RuntimeArtifactMember, ...] = Field(
        min_length=1,
        max_length=100_000,
    )
    runtime_artifact_sha256: str = Field(pattern=_SHA256)
    maximum_stdout_bytes: int = Field(gt=0, le=500_000_000)
    maximum_stderr_bytes: int = Field(gt=0, le=10_000_000)
    timeout_seconds: int = Field(gt=0, le=3600)

    @model_validator(mode="after")
    def _closed_runtime_lock(self) -> Self:
        paths = tuple(member.relative_path for member in self.runtime_members)
        if len(paths) != len(set(paths)) or paths != tuple(sorted(paths)):
            raise ValueError("runtime members must be unique and canonically sorted")
        python_members = tuple(
            member
            for member in self.runtime_members
            if member.relative_path == self.bundle_python_relative_path
        )
        if len(python_members) != 1:
            raise ValueError("runtime lock must contain the bundle Python executable")
        if python_members[0].blob_sha256 != self.bundle_python_sha256:
            raise ValueError("bundle Python digest must match its runtime-lock member")
        return self


class ProcessorQualification(_Closed):
    profile: Literal["sec-inline-xbrl-investor-grade.v1"]
    require_os_network_denial: Literal[True]
    require_runtime_artifact_sha256: Literal[True]
    require_exact_coordinates: Literal[True]
    require_sec_filing_identity: Literal[True]
    require_source_locator_commitments: Literal[True]
    require_network_artifact_commitments: Literal[True]
    require_footnote_commitments: Literal[True]
    require_zero_fact_host_verification: Literal[True]


class ProcessorBuildProvenance(_Closed):
    python: Literal["3.13.11"]
    arelle_git_commit: Literal["9c1ce7e70d270385c723b185020a91416d724715"]
    arelle_wheel_sha256: Literal["eb2511bf95ad34c94441282b7e683c637ee73cd45b76b6649c9a35ca858e603a"]
    edgar_git_commit: Literal["99e94b6c6f5ca2ef06a9c2f29b0a4290a7f959db"]
    xule_git_commit: Literal["40f774ced269ee6637a96d51db244efd6337e689"]
    bridge_source_sha256: str = Field(pattern=_SHA256)
    launcher_source_sha256: str = Field(pattern=_SHA256)


class ProcessorBundleManifest(_Closed):
    bundle_name: str = Field(min_length=1, max_length=128)
    bridge_module: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_.]*$")
    bridge_protocol_version: Literal["filing-xbrl-bridge.v1"]
    coordinates: ProcessorCoordinates
    execution: ProcessorExecution
    qualification: ProcessorQualification
    build_provenance: ProcessorBuildProvenance

    @property
    def canonical_json(self) -> str:
        return _canonical(self.model_dump(mode="json"))

    @property
    def manifest_sha256(self) -> str:
        return _sha(self.canonical_json.encode())


class ProcessorBundleApprovalSeal(_Closed):
    schema_version: Literal["filing-xbrl-bundle-approval/v1"]
    manifest_sha256: str = Field(pattern=_SHA256)
    manifest_artifact_sha256: str = Field(pattern=_SHA256)
    runtime_artifact_sha256: str = Field(pattern=_SHA256)
    sandbox_launcher_sha256: str = Field(pattern=_SHA256)
    bridge_source_sha256: str = Field(pattern=_SHA256)
    launcher_source_sha256: str = Field(pattern=_SHA256)


@dataclass(frozen=True)
class ApprovedProcessorBundle:
    """Opaque capability proving one manifest passed the committed approval root."""

    manifest: ProcessorBundleManifest
    approval_seal: ProcessorBundleApprovalSeal
    approval_seal_sha256: str
    _capability: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._capability is not _APPROVAL_CAPABILITY:
            raise TypeError("approved filing-XBRL bundles are created only by the seal loader")


class ProcessorPackageMember(_Closed):
    member_ordinal: int = Field(ge=0)
    member_role: Literal[
        "primary_document",
        "filing_attachment",
        "issuer_taxonomy",
        "standard_taxonomy",
        "network_artifact",
    ]
    document_version_id: str | None = Field(default=None, min_length=1, max_length=128)
    source_url: str = Field(min_length=1, max_length=4096)
    local_path: Path
    blob_sha256: str = Field(pattern=_SHA256)
    byte_size: int = Field(ge=0, le=512 * 1024 * 1024)
    media_type: str = Field(min_length=1, max_length=255)


class InlineXbrlProcessorRequest(_Closed):
    accession_number: str = Field(pattern=r"^\d{10}-\d{2}-\d{6}$")
    entrypoint_ordinal: int = Field(ge=0)
    members: tuple[ProcessorPackageMember, ...] = Field(min_length=1, max_length=10_000)
    expected_cik: str = Field(pattern=r"^\d{10}$")
    package_member_set_sha256: str = Field(pattern=_SHA256)

    @model_validator(mode="after")
    def _closed_package(self) -> Self:
        ordinals = tuple(member.member_ordinal for member in self.members)
        if ordinals != tuple(range(len(self.members))):
            raise ValueError("package members must be in contiguous canonical order")
        if self.entrypoint_ordinal >= len(self.members):
            raise ValueError("entrypoint ordinal is outside the package")
        if self.members[self.entrypoint_ordinal].member_role != "primary_document":
            raise ValueError("entrypoint must be the primary document")
        if sum(member.member_role == "primary_document" for member in self.members) != 1:
            raise ValueError("package must contain exactly one primary document")
        if sum(member.byte_size for member in self.members) > 2 * 1024 * 1024 * 1024:
            raise ValueError("package exceeds the total byte limit")
        for member in self.members:
            evidence_backed = member.member_role in {
                "primary_document",
                "filing_attachment",
                "issuer_taxonomy",
            }
            if evidence_backed != (member.document_version_id is not None):
                raise ValueError(
                    "filing and issuer-taxonomy members require one evidence document; "
                    "standard/network artifacts cannot claim one"
                )
        expected = package_member_set_sha256(self.members)
        if expected != self.package_member_set_sha256:
            raise ValueError("package member-set digest does not match")
        return self


class ProcessorNetworkArtifact(_Closed):
    source_url: str = Field(min_length=1, max_length=4096)
    blob_sha256: str = Field(pattern=_SHA256)


class ProcessorExecutionEvidence(_Closed):
    """Processor-side evidence that is independently checked by the host."""

    sandbox_contract_version: Literal["earnings-xbrl-os-sandbox.v1"]
    internet_connectivity: Literal["os_denied"]
    network_requests_observed: Literal[0]
    accession_number: str = Field(pattern=r"^\d{10}-\d{2}-\d{6}$")
    expected_cik: str = Field(pattern=r"^\d{10}$")
    package_member_set_sha256: str = Field(pattern=_SHA256)
    runtime_artifact_sha256: str = Field(pattern=_SHA256)


class ProcessorFootnote(_Closed):
    footnote_ordinal: int = Field(ge=0)
    canonical_footnote: dict[str, JsonValue]
    footnote_sha256: str = Field(pattern=_SHA256)

    @model_validator(mode="after")
    def _digest(self) -> Self:
        if _sha(_canonical(self.canonical_footnote).encode()) != self.footnote_sha256:
            raise ValueError("footnote digest does not match canonical payload")
        return self


class ProcessorRawFact(_Closed):
    input_ordinal: int = Field(ge=0)
    package_member_ordinal: int = Field(ge=0)
    package_member_blob_sha256: str = Field(pattern=_SHA256)
    accession_number: str = Field(pattern=r"^\d{10}-\d{2}-\d{6}$")
    observed_cik: str = Field(pattern=r"^\d{10}$")
    evidence_text: str = Field(min_length=1)
    source_locator: dict[str, JsonValue]
    source_locator_sha256: str = Field(pattern=_SHA256)
    canonical_raw_fact: dict[str, JsonValue]
    raw_fact_sha256: str = Field(pattern=_SHA256)
    source_entry_sha256: str = Field(pattern=_SHA256)
    normalization_outcome: Literal["normalized", "rejected"]
    normalized_fact: dict[str, JsonValue] | None = None
    rejection_reason_code: str | None = Field(default=None, min_length=1, max_length=128)
    rejection_detail: str | None = Field(default=None, min_length=1, max_length=4096)
    footnotes: tuple[ProcessorFootnote, ...] = ()

    @model_validator(mode="after")
    def _exact_commitments(self) -> Self:
        required_locator_fields = {
            "source_ref",
            "filing_ordinal",
            "xbrl_package_member",
            "xbrl_fact_id",
            "xbrl_element_path",
            "xbrl_concept_namespace",
            "xbrl_concept_name",
            "xbrl_context_id",
        }
        missing_locator_fields = sorted(
            field
            for field in required_locator_fields
            if not _present_locator_field(self.source_locator, field)
        )
        if missing_locator_fields:
            raise ValueError(
                "XBRL source locator is incomplete: " + ", ".join(missing_locator_fields)
            )
        if _sha(_canonical(self.source_locator).encode()) != self.source_locator_sha256:
            raise ValueError("source locator digest does not match")
        if _sha(_canonical(self.canonical_raw_fact).encode()) != self.raw_fact_sha256:
            raise ValueError("raw fact digest does not match")
        source_entry = {
            "accession_number": self.accession_number,
            "observed_cik": self.observed_cik,
            "package_member_blob_sha256": self.package_member_blob_sha256,
            "package_member_ordinal": self.package_member_ordinal,
            "raw_fact_sha256": self.raw_fact_sha256,
            "source_locator_sha256": self.source_locator_sha256,
        }
        if _sha(_canonical(source_entry).encode()) != self.source_entry_sha256:
            raise ValueError("source entry digest does not bind the admitted filing member")
        normalized = self.normalization_outcome == "normalized"
        if normalized != (self.normalized_fact is not None):
            raise ValueError("only normalized facts may carry normalized_fact")
        if normalized and (
            self.rejection_reason_code is not None or self.rejection_detail is not None
        ):
            raise ValueError("normalized facts cannot carry rejection details")
        if not normalized and (self.rejection_reason_code is None or self.rejection_detail is None):
            raise ValueError("rejected facts require a reason and detail")
        footnote_ordinals = tuple(item.footnote_ordinal for item in self.footnotes)
        if footnote_ordinals != tuple(range(len(self.footnotes))):
            raise ValueError("footnote ordinals must be contiguous")
        return self


class InlineXbrlProcessorResult(_Closed):
    bridge_protocol_version: Literal["filing-xbrl-bridge.v1"]
    coordinates: ProcessorCoordinates
    execution_evidence: ProcessorExecutionEvidence
    runtime_artifact_sha256: str = Field(pattern=_SHA256)
    package_member_set_sha256: str = Field(pattern=_SHA256)
    network_artifacts: tuple[ProcessorNetworkArtifact, ...]
    network_artifact_count: int = Field(ge=0)
    network_artifact_set_sha256: str = Field(pattern=_SHA256)
    facts: tuple[ProcessorRawFact, ...]
    raw_fact_set_sha256: str = Field(pattern=_SHA256)
    footnote_count: int = Field(ge=0)
    footnote_set_sha256: str = Field(pattern=_SHA256)
    zero_fact_disposition: Literal["verified_no_inline_xbrl"] | None = None

    @model_validator(mode="after")
    def _complete_sets(self) -> Self:
        ordinals = tuple(item.input_ordinal for item in self.facts)
        if ordinals != tuple(range(len(self.facts))):
            raise ValueError("raw fact ordinals must be contiguous")
        if self.network_artifact_count != len(self.network_artifacts):
            raise ValueError("network artifact completeness count does not match")
        if self.network_artifact_set_sha256 != network_artifact_set_sha256(self.network_artifacts):
            raise ValueError("network artifact set digest does not match")
        if self.raw_fact_set_sha256 != raw_fact_set_sha256(self.facts):
            raise ValueError("raw fact set digest does not match")
        footnotes = tuple(
            (fact.input_ordinal, footnote) for fact in self.facts for footnote in fact.footnotes
        )
        if self.footnote_count != len(footnotes):
            raise ValueError("footnote completeness count does not match")
        if self.footnote_set_sha256 != footnote_set_sha256(footnotes):
            raise ValueError("footnote set digest does not match")
        if bool(self.facts) == (self.zero_fact_disposition is not None):
            raise ValueError("zero-fact disposition is required only for an empty fact set")
        return self


def load_processor_bundle_manifest(path: Path) -> ProcessorBundleManifest:
    try:
        return ProcessorBundleManifest.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise InlineXbrlProcessorError("filing-XBRL bundle manifest is invalid") from exc


def load_approved_processor_bundle_manifest(
    path: Path,
) -> ApprovedProcessorBundle:
    """Load one external bundle only when the committed review root approves it."""

    return _load_approved_processor_bundle_manifest(
        path,
        approval_seal_path=_CANONICAL_APPROVAL_SEAL,
    )


def _load_approved_processor_bundle_manifest(
    path: Path,
    *,
    approval_seal_path: Path,
) -> ApprovedProcessorBundle:
    """Validate one bundle against an explicit seal for qualification tooling/tests."""

    try:
        manifest_bytes = _read_stable_file(path)
        manifest = ProcessorBundleManifest.model_validate_json(manifest_bytes)
        seal_bytes = _read_stable_file(approval_seal_path)
        seal = ProcessorBundleApprovalSeal.model_validate_json(seal_bytes)
    except (OSError, ValueError) as exc:
        raise InlineXbrlProcessorError("approved filing-XBRL bundle evidence is invalid") from exc
    if manifest_bytes != (manifest.canonical_json + "\n").encode():
        raise InlineXbrlProcessorError("approved filing-XBRL bundle manifest is not canonical")
    if _sha(manifest_bytes) != seal.manifest_artifact_sha256:
        raise InlineXbrlProcessorError("filing-XBRL manifest artifact digest is not approved")
    if manifest.manifest_sha256 != seal.manifest_sha256:
        raise InlineXbrlProcessorError("filing-XBRL bundle is not in the committed approval seal")
    expected = (
        manifest.execution.runtime_artifact_sha256,
        manifest.execution.sandbox_launcher_sha256,
        manifest.build_provenance.bridge_source_sha256,
        manifest.build_provenance.launcher_source_sha256,
    )
    sealed = (
        seal.runtime_artifact_sha256,
        seal.sandbox_launcher_sha256,
        seal.bridge_source_sha256,
        seal.launcher_source_sha256,
    )
    if expected != sealed:
        raise InlineXbrlProcessorError("filing-XBRL bundle approval commitments do not match")
    source_paths = (
        _PROJECT_ROOT / "execution" / "filing_xbrl_bridge.py",
        _PROJECT_ROOT / "execution" / "filing_xbrl_appcontainer_launcher.cs",
    )
    observed_sources = tuple(_sha(_read_stable_file(source)) for source in source_paths)
    if observed_sources != sealed[2:]:
        raise InlineXbrlProcessorError("filing-XBRL reviewed source identity changed")
    return ApprovedProcessorBundle(
        manifest=manifest,
        approval_seal=seal,
        approval_seal_sha256=_sha(seal_bytes),
        _capability=_APPROVAL_CAPABILITY,
    )


def _read_stable_file(path: Path) -> bytes:
    _require_local_path(path, "filing-XBRL approval artifact")
    _require_no_reparse_points(path)
    before = path.stat()
    if before.st_nlink != 1:
        raise InlineXbrlProcessorError("filing-XBRL approval artifact has a hardlink alias")
    body = path.read_bytes()
    after = path.stat()
    identity_before = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    identity_after = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    if identity_before != identity_after or len(body) != before.st_size:
        raise InlineXbrlProcessorError("filing-XBRL approval artifact changed while reading")
    return body


def package_member_set_sha256(members: Sequence[ProcessorPackageMember]) -> str:
    payload = [
        {
            "blob_sha256": member.blob_sha256,
            "byte_size": member.byte_size,
            "document_version_id": member.document_version_id,
            "media_type": member.media_type,
            "member_ordinal": member.member_ordinal,
            "member_role": member.member_role,
            "source_url": member.source_url,
        }
        for member in members
    ]
    return _sha(_canonical(payload).encode())


def network_artifact_set_sha256(
    artifacts: Sequence[ProcessorNetworkArtifact],
) -> str:
    payload = [
        {"blob_sha256": item.blob_sha256, "source_url": item.source_url} for item in artifacts
    ]
    return _sha(_canonical(payload).encode())


def runtime_artifact_set_sha256(
    members: Sequence[RuntimeArtifactMember],
) -> str:
    payload = [
        {
            "blob_sha256": member.blob_sha256,
            "byte_size": member.byte_size,
            "relative_path": member.relative_path,
        }
        for member in members
    ]
    return _sha(_canonical(payload).encode())


def raw_fact_set_sha256(facts: Sequence[ProcessorRawFact]) -> str:
    payload = [
        {
            "input_ordinal": fact.input_ordinal,
            "raw_fact_sha256": fact.raw_fact_sha256,
            "source_entry_sha256": fact.source_entry_sha256,
            "source_locator_sha256": fact.source_locator_sha256,
        }
        for fact in facts
    ]
    return _sha(_canonical(payload).encode())


def footnote_set_sha256(
    footnotes: Sequence[tuple[int, ProcessorFootnote]],
) -> str:
    payload = [
        {
            "canonical_footnote": footnote.canonical_footnote,
            "footnote_ordinal": footnote.footnote_ordinal,
            "footnote_sha256": footnote.footnote_sha256,
            "input_ordinal": input_ordinal,
        }
        for input_ordinal, footnote in footnotes
    ]
    return _sha(_canonical(payload).encode())


def run_inline_xbrl_processor(
    request: InlineXbrlProcessorRequest,
    *,
    approved_bundle: ApprovedProcessorBundle,
    runtime_root: Path,
    bundle_python: Path,
    sandbox_launcher: Path,
    environment: Mapping[str, str] | None = None,
) -> InlineXbrlProcessorResult:
    if not isinstance(cast(object, approved_bundle), ApprovedProcessorBundle):
        raise InlineXbrlProcessorError("filing-XBRL processor bundle is not approved")
    return _run_inline_xbrl_processor(
        request,
        manifest=approved_bundle.manifest,
        runtime_root=runtime_root,
        bundle_python=bundle_python,
        sandbox_launcher=sandbox_launcher,
        environment=environment,
    )


def _run_inline_xbrl_processor(
    request: InlineXbrlProcessorRequest,
    *,
    manifest: ProcessorBundleManifest,
    runtime_root: Path,
    bundle_python: Path,
    sandbox_launcher: Path,
    environment: Mapping[str, str] | None = None,
) -> InlineXbrlProcessorResult:
    """Run and verify the qualified processor without importing its dependencies."""

    _verify_executable(
        sandbox_launcher,
        manifest.execution.sandbox_launcher_sha256,
        "OS sandbox launcher",
    )
    runtime_artifact_sha256 = _verify_runtime_closure(
        runtime_root,
        manifest.execution.runtime_members,
        expected_sha256=manifest.execution.runtime_artifact_sha256,
    )
    expected_python = runtime_root / PurePosixPath(manifest.execution.bundle_python_relative_path)
    if bundle_python.resolve() != expected_python.resolve():
        raise InlineXbrlProcessorError(
            "qualified filing-XBRL bundle Python is outside its runtime lock"
        )
    _verify_executable(
        bundle_python,
        manifest.execution.bundle_python_sha256,
        "qualified filing-XBRL bundle Python",
    )
    for member in request.members:
        _verify_member(member)
    staged_request, staged_root = _stage_package(request, runtime_root=runtime_root)
    command = (
        str(sandbox_launcher),
        "--contract",
        manifest.execution.sandbox_contract_version,
        "--deny-network",
        "--read-tree",
        str(runtime_root.resolve()),
        "--read-input-tree",
        str(staged_root),
        "--",
        str(bundle_python),
        "-I",
        "-m",
        manifest.bridge_module,
        "--protocol",
        manifest.bridge_protocol_version,
        "--runtime-artifact-sha256",
        runtime_artifact_sha256,
    )
    clean_environment = _isolated_environment(environment)
    payload = _canonical(staged_request.model_dump(mode="json"))
    fenced_paths = (
        sandbox_launcher,
        runtime_root,
        *enumerate_closed_local_tree(runtime_root, label="qualified filing-XBRL runtime"),
        staged_root,
        *enumerate_closed_local_tree(staged_root, label="staged filing-XBRL package"),
    )
    with _tree_write_denial_fence((runtime_root, staged_root)) as tree_fence_mode:
        if os.name == "nt" and tree_fence_mode != "windows-deny-write-acl":
            raise InlineXbrlProcessorError("filing-XBRL tree write denial is unavailable")
        with _write_denial_fence(fenced_paths) as fence_mode:
            if os.name == "nt" and fence_mode != "windows-deny-write":
                raise InlineXbrlProcessorError("filing-XBRL write-denial fence is unavailable")
            _verify_executable(
                sandbox_launcher,
                manifest.execution.sandbox_launcher_sha256,
                "OS sandbox launcher",
            )
            _verify_runtime_closure(
                runtime_root,
                manifest.execution.runtime_members,
                expected_sha256=manifest.execution.runtime_artifact_sha256,
            )
            _verify_staged_package(staged_root, staged_request.members)
            returncode, stdout, stderr = run_capped_process(
                command,
                payload.encode(),
                environment=clean_environment,
                timeout_seconds=manifest.execution.timeout_seconds,
                maximum_stdout_bytes=manifest.execution.maximum_stdout_bytes,
                maximum_stderr_bytes=manifest.execution.maximum_stderr_bytes,
            )
            _verify_executable(
                sandbox_launcher,
                manifest.execution.sandbox_launcher_sha256,
                "OS sandbox launcher",
            )
            _verify_runtime_closure(
                runtime_root,
                manifest.execution.runtime_members,
                expected_sha256=manifest.execution.runtime_artifact_sha256,
            )
            _verify_staged_package(staged_root, staged_request.members)
    if returncode != 0:
        refusal = _sandbox_refusal_stage(stderr) if returncode == 125 else None
        detail = "" if refusal is None else f"; sandbox {refusal}"
        raise InlineXbrlProcessorError(
            f"filing-XBRL processor rejected the package (exit {returncode}{detail})"
        )
    try:
        result = InlineXbrlProcessorResult.model_validate_json(stdout)
    except ValueError as exc:
        raise InlineXbrlProcessorError("filing-XBRL processor output violates protocol") from exc
    if result.coordinates != manifest.coordinates:
        raise InlineXbrlProcessorError("filing-XBRL processor coordinates are not qualified")
    if result.runtime_artifact_sha256 != runtime_artifact_sha256:
        raise InlineXbrlProcessorError("filing-XBRL runtime artifact is not qualified")
    expected_execution_evidence = ProcessorExecutionEvidence(
        sandbox_contract_version=manifest.execution.sandbox_contract_version,
        internet_connectivity=manifest.execution.internet_connectivity,
        network_requests_observed=0,
        accession_number=request.accession_number,
        expected_cik=request.expected_cik,
        package_member_set_sha256=request.package_member_set_sha256,
        runtime_artifact_sha256=runtime_artifact_sha256,
    )
    if result.execution_evidence != expected_execution_evidence:
        raise InlineXbrlProcessorError(
            "filing-XBRL processor execution evidence is not bound to the request"
        )
    if result.package_member_set_sha256 != request.package_member_set_sha256:
        raise InlineXbrlProcessorError("filing-XBRL processor changed its package identity")
    expected_network = {
        (member.source_url, member.blob_sha256)
        for member in request.members
        if member.member_role in {"issuer_taxonomy", "standard_taxonomy", "network_artifact"}
    }
    observed_network = {(item.source_url, item.blob_sha256) for item in result.network_artifacts}
    if observed_network != expected_network:
        raise InlineXbrlProcessorError("processor network/taxonomy artifact closure is not exact")
    for fact in result.facts:
        if fact.package_member_ordinal >= len(request.members):
            raise InlineXbrlProcessorError("raw fact references an undeclared package member")
        member = request.members[fact.package_member_ordinal]
        if member.member_role not in {"primary_document", "filing_attachment"}:
            raise InlineXbrlProcessorError("raw fact originates from a non-instance artifact")
        if fact.package_member_blob_sha256 != member.blob_sha256:
            raise InlineXbrlProcessorError("raw fact package-member digest does not match")
        if fact.accession_number != request.accession_number:
            raise InlineXbrlProcessorError("raw fact accession does not match the request")
        if fact.observed_cik != request.expected_cik:
            raise InlineXbrlProcessorError("raw fact CIK does not match the expected issuer")
        if (
            fact.source_locator.get("source_ref") != member.source_url
            or fact.source_locator.get("xbrl_package_member") != member.source_url
            or fact.source_locator.get("filing_ordinal") != fact.input_ordinal
        ):
            raise InlineXbrlProcessorError("raw fact locator is outside its declared member")
    if not result.facts:
        entrypoint = request.members[request.entrypoint_ordinal]
        if result.zero_fact_disposition != "verified_no_inline_xbrl":
            raise InlineXbrlProcessorError("empty fact set lacks a no-XBRL disposition")
        if _contains_inline_xbrl(entrypoint.local_path):
            raise InlineXbrlProcessorError("processor returned zero facts for Inline XBRL input")
    return result


def _stage_package(
    request: InlineXbrlProcessorRequest,
    *,
    runtime_root: Path,
) -> tuple[InlineXbrlProcessorRequest, Path]:
    """Publish one immutable, content-addressed AppContainer input directory."""

    cache_root = runtime_root.parent / "filing-xbrl-package-cache"
    _require_local_path(cache_root, "filing-XBRL package cache")
    try:
        cache_root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise InlineXbrlProcessorError("filing-XBRL package cache is unavailable") from exc
    _require_no_reparse_points(cache_root)
    destination = cache_root / request.package_member_set_sha256
    names: dict[str, tuple[str, int]] = {}
    staged_members: list[ProcessorPackageMember] = []
    for member in request.members:
        relative = _source_url_relative_path(member.source_url)
        identity = (member.blob_sha256, member.byte_size)
        key = relative.as_posix().casefold()
        existing = names.get(key)
        if existing is not None and existing != identity:
            raise InlineXbrlProcessorError("filing-XBRL package URL-path collision")
        names[key] = identity
        staged_members.append(member.model_copy(update={"local_path": destination / relative}))

    if destination.exists():
        _verify_staged_package(destination, tuple(staged_members))
        return request.model_copy(update={"members": tuple(staged_members)}), destination.resolve()

    _admit_package_cache(
        cache_root,
        incoming_bytes=sum(identity[1] for identity in names.values()),
    )
    try:
        if sum(1 for path in cache_root.glob(".incomplete-*") if path.is_dir()) >= 4:
            raise InlineXbrlProcessorError(
                "filing-XBRL package staging has reached its failed-run limit"
            )
        staged = Path(tempfile.mkdtemp(prefix=".incomplete-", dir=cache_root))
    except OSError as exc:
        raise InlineXbrlProcessorError("filing-XBRL package staging cannot start") from exc
    published_members = tuple(
        member.model_copy(
            update={"local_path": staged / member.local_path.relative_to(destination)}
        )
        for member in staged_members
    )
    try:
        copied: set[str] = set()
        for source, target in zip(request.members, published_members, strict=True):
            key = target.local_path.relative_to(staged).as_posix().casefold()
            if key in copied:
                continue
            copied.add(key)
            target.local_path.parent.mkdir(parents=True, exist_ok=True)
            with source.local_path.open("rb") as reader, target.local_path.open("xb") as writer:
                _copy_exact_member(reader, writer, expected_bytes=source.byte_size)
                writer.flush()
                os.fsync(writer.fileno())
            _verify_member(target)
        try:
            staged.rename(destination)
        except FileExistsError:
            _verify_staged_package(destination, tuple(staged_members))
        except OSError as exc:
            if destination.exists():
                _verify_staged_package(destination, tuple(staged_members))
            else:
                raise InlineXbrlProcessorError(
                    "filing-XBRL package staging cannot publish"
                ) from exc
    except (InlineXbrlProcessorError, OSError) as exc:
        if isinstance(exc, InlineXbrlProcessorError):
            raise
        raise InlineXbrlProcessorError("filing-XBRL package staging failed") from exc
    _verify_staged_package(destination, tuple(staged_members))
    return request.model_copy(update={"members": tuple(staged_members)}), destination.resolve()


def _copy_exact_member(
    reader: BinaryIO,
    writer: BinaryIO,
    *,
    expected_bytes: int,
) -> None:
    remaining = expected_bytes
    while remaining:
        chunk = reader.read(min(remaining, 1024 * 1024))
        if not chunk:
            raise InlineXbrlProcessorError("filing-XBRL package member shrank while staging")
        writer.write(chunk)
        remaining -= len(chunk)
    if reader.read(1):
        raise InlineXbrlProcessorError("filing-XBRL package member grew while staging")


def _source_url_relative_path(source_url: str) -> PurePosixPath:
    parsed = urlsplit(source_url)
    hostname = parsed.hostname
    if (
        parsed.scheme.casefold() != "https"
        or hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in {None, 443}
        or parsed.query
        or parsed.fragment
    ):
        raise InlineXbrlProcessorError("filing-XBRL member URL is not one closed HTTPS path")
    raw_path = PurePosixPath(parsed.path)
    parts = raw_path.parts[1:] if raw_path.is_absolute() else raw_path.parts
    if not parts:
        raise InlineXbrlProcessorError("filing-XBRL member URL has no safe path")
    safe_parts = (hostname.casefold(), *parts)
    forbidden = set('<>:"\\|?*')
    if any(
        part in {"", ".", ".."}
        or part.endswith((" ", "."))
        or any(character in forbidden or ord(character) < 32 for character in part)
        for part in safe_parts
    ):
        raise InlineXbrlProcessorError("filing-XBRL member URL has an unsafe path")
    return PurePosixPath(*safe_parts)


def _verify_staged_package(
    root: Path,
    members: tuple[ProcessorPackageMember, ...],
) -> None:
    _require_no_reparse_points(root)
    expected = {member.local_path.relative_to(root).as_posix().casefold() for member in members}
    observed = {
        path.relative_to(root).as_posix().casefold()
        for path in enumerate_closed_local_tree(root, label="staged filing-XBRL package")
        if path.is_file()
    }
    if observed != expected:
        raise InlineXbrlProcessorError("staged filing-XBRL package closure changed")
    for member in members:
        _verify_member(member)


def _require_no_reparse_points(path: Path) -> None:
    current = Path(os.path.abspath(path))
    while True:
        try:
            attributes = getattr(current.lstat(), "st_file_attributes", 0)
        except OSError as exc:
            raise InlineXbrlProcessorError("filing-XBRL path identity is unavailable") from exc
        if current.is_symlink() or attributes & 0x400:
            raise InlineXbrlProcessorError("filing-XBRL path contains a reparse point")
        if current.parent == current:
            break
        current = current.parent


def enumerate_closed_local_tree(root: Path, *, label: str) -> tuple[Path, ...]:
    """Enumerate one local tree without ever descending through a reparse point."""

    _require_local_path(root, label)
    _require_no_reparse_points(root)
    if not root.is_dir():
        raise InlineXbrlProcessorError(f"{label} is not a directory")
    pending = [root]
    observed: list[Path] = []
    try:
        while pending:
            current = pending.pop()
            with os.scandir(current) as entries:
                children = sorted(entries, key=lambda entry: entry.name.casefold())
            directories: list[Path] = []
            for entry in children:
                path = Path(entry.path)
                _require_no_reparse_points(path)
                if entry.is_symlink():
                    raise InlineXbrlProcessorError(f"{label} contains a symbolic link")
                if entry.is_dir(follow_symlinks=False):
                    directories.append(path)
                elif not entry.is_file(follow_symlinks=False):
                    raise InlineXbrlProcessorError(f"{label} contains a special file")
                observed.append(path)
            pending.extend(reversed(directories))
    except OSError as exc:
        raise InlineXbrlProcessorError(f"{label} cannot be enumerated") from exc
    return tuple(sorted(observed, key=lambda path: path.relative_to(root).as_posix()))


def _admit_package_cache(cache_root: Path, *, incoming_bytes: int) -> None:
    entries = enumerate_closed_local_tree(
        cache_root,
        label="filing-XBRL package cache",
    )
    top_level = tuple(path for path in entries if path.parent == cache_root)
    completed = tuple(
        path for path in top_level if path.is_dir() and not path.name.startswith(".incomplete-")
    )
    unexpected = tuple(
        path
        for path in top_level
        if not path.is_dir() or (path.name.startswith(".incomplete-") and not path.is_dir())
    )
    if unexpected:
        raise InlineXbrlProcessorError("filing-XBRL package cache contains an unknown entry")
    if len(completed) >= _PACKAGE_CACHE_MAX_COMPLETED:
        raise InlineXbrlProcessorError("filing-XBRL package cache reached its entry limit")
    completed_roots = {path for path in completed}
    completed_bytes = sum(
        path.stat().st_size
        for path in entries
        if path.is_file() and any(root in path.parents for root in completed_roots)
    )
    if completed_bytes + incoming_bytes > _PACKAGE_CACHE_MAX_BYTES:
        raise InlineXbrlProcessorError("filing-XBRL package cache reached its byte limit")
    try:
        free_bytes = shutil.disk_usage(cache_root).free
    except OSError as exc:
        raise InlineXbrlProcessorError("filing-XBRL package cache headroom is unavailable") from exc
    if free_bytes - incoming_bytes < _PACKAGE_CACHE_MIN_FREE_BYTES:
        raise InlineXbrlProcessorError("filing-XBRL package cache lacks disk headroom")


@contextmanager
def _tree_write_denial_fence(roots: Sequence[Path]) -> Generator[str, None, None]:
    """Hold exact current-user deny-write ACEs through the admitted operation."""

    if os.name != "nt":
        yield "non-windows-test-only"
        return
    unique = tuple(dict.fromkeys(Path(os.path.abspath(root)) for root in roots))
    powershell = _windows_system_directory() / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    _require_no_reparse_points(powershell)
    processes: list[subprocess.Popen[str]] = []
    try:
        for root in unique:
            _require_no_reparse_points(root)
            if not root.is_dir():
                raise InlineXbrlProcessorError("filing-XBRL fenced tree is unavailable")
            process = _start_windows_acl_fence(powershell, root)
            processes.append(process)
            _require_tree_nonwritable(root)
        yield "windows-deny-write-acl"
    finally:
        failures: list[Exception] = []
        for process in reversed(processes):
            try:
                _release_windows_acl_fence(process)
            except Exception as exc:
                failures.append(exc)
        if failures:
            raise InlineXbrlProcessorError(
                "filing-XBRL write-denial ACL restoration failed"
            ) from failures[0]


def _windows_system_directory() -> Path:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_system_directory = kernel32.GetSystemDirectoryW
    get_system_directory.argtypes = [wintypes.LPWSTR, wintypes.UINT]
    get_system_directory.restype = wintypes.UINT
    buffer = ctypes.create_unicode_buffer(32_768)
    length = get_system_directory(buffer, len(buffer))
    if length == 0 or length >= len(buffer):
        raise InlineXbrlProcessorError("Windows system directory is unavailable")
    path = Path(buffer.value)
    _require_local_path(path, "Windows system directory")
    return path


def _start_windows_acl_fence(
    powershell: Path,
    root: Path,
) -> subprocess.Popen[str]:
    script = r"""
$ErrorActionPreference = 'Stop'
$root = $env:EARNINGS_XBRL_FENCE_ROOT
$sid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User
$rights = [System.Security.AccessControl.FileSystemRights]::WriteData `
    -bor [System.Security.AccessControl.FileSystemRights]::AppendData `
    -bor [System.Security.AccessControl.FileSystemRights]::WriteExtendedAttributes `
    -bor [System.Security.AccessControl.FileSystemRights]::DeleteSubdirectoriesAndFiles `
    -bor [System.Security.AccessControl.FileSystemRights]::WriteAttributes `
    -bor [System.Security.AccessControl.FileSystemRights]::Delete
$inheritance = [System.Security.AccessControl.InheritanceFlags]::ContainerInherit `
    -bor [System.Security.AccessControl.InheritanceFlags]::ObjectInherit
$rule = [System.Security.AccessControl.FileSystemAccessRule]::new(
    $sid,
    $rights,
    $inheritance,
    [System.Security.AccessControl.PropagationFlags]::None,
    [System.Security.AccessControl.AccessControlType]::Deny)
$added = $false
try {
    $acl = [System.IO.Directory]::GetAccessControl(
        $root,
        [System.Security.AccessControl.AccessControlSections]::Access)
    $existing = @($acl.GetAccessRules($true, $false, [System.Security.Principal.SecurityIdentifier]) |
        Where-Object {
            $_.IdentityReference -eq $sid -and
            $_.AccessControlType -eq [System.Security.AccessControl.AccessControlType]::Deny
        })
    if ($existing.Count -ne 0) { throw 'pre-existing current-user deny ACE' }
    [void]$acl.AddAccessRule($rule)
    [System.IO.Directory]::SetAccessControl($root, $acl)
    $added = $true
    [Console]::Out.WriteLine('READY')
    [Console]::Out.Flush()
    if ([Console]::In.ReadLine() -ne 'RELEASE') { throw 'fence release was not acknowledged' }
}
finally {
    if ($added) {
        $acl = [System.IO.Directory]::GetAccessControl(
            $root,
            [System.Security.AccessControl.AccessControlSections]::Access)
        $acl.RemoveAccessRuleSpecific($rule)
        [System.IO.Directory]::SetAccessControl($root, $acl)
    }
}
"""
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    system_root = str(_windows_system_directory().parent)
    environment = {
        "EARNINGS_XBRL_FENCE_ROOT": str(root),
        "SystemRoot": system_root,
        "TEMP": tempfile.gettempdir(),
        "TMP": tempfile.gettempdir(),
    }
    try:
        process = subprocess.Popen(
            (
                str(powershell),
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-EncodedCommand",
                encoded,
            ),
            cwd=root.parent,
            env=environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except OSError as exc:
        raise InlineXbrlProcessorError("Windows ACL fence cannot start") from exc
    ready: list[str] = []

    def _read_ready() -> None:
        if process.stdout is not None:
            ready.append(process.stdout.readline())

    reader = threading.Thread(target=_read_ready, daemon=True)
    reader.start()
    reader.join(30)
    if reader.is_alive() or ready != ["READY\n"]:
        if not reader.is_alive() and process.poll() is None:
            with suppress(subprocess.TimeoutExpired):
                process.wait(timeout=5)
        refusal = _windows_acl_refusal(process)
        _release_windows_acl_fence(process, require_success=False)
        raise InlineXbrlProcessorError(f"Windows ACL fence did not become ready ({refusal})")
    return process


def _windows_acl_refusal(process: subprocess.Popen[str]) -> str:
    if process.poll() is None or process.stderr is None:
        return "timeout"
    try:
        detail = process.stderr.read(4096)
    except OSError:
        return "script_refused"
    if "pre-existing current-user deny ACE" in detail:
        return "preexisting_deny"
    if "Access is denied" in detail or "UnauthorizedAccessException" in detail:
        return "access_denied"
    if "Set-Acl" in detail:
        return "acl_update_refused"
    return "script_refused"


def _release_windows_acl_fence(
    process: subprocess.Popen[str],
    *,
    require_success: bool = True,
) -> None:
    try:
        if process.poll() is None and process.stdin is not None:
            try:
                process.stdin.write("RELEASE\n")
                process.stdin.flush()
            except OSError:
                pass
            finally:
                with suppress(OSError):
                    process.stdin.close()
        process.wait(timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        raise InlineXbrlProcessorError("Windows ACL fence did not terminate") from exc
    finally:
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                stream.close()
    if require_success and process.returncode != 0:
        raise InlineXbrlProcessorError("Windows ACL fence cleanup failed")


def _require_tree_nonwritable(root: Path) -> None:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    create_file.restype = wintypes.HANDLE
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL
    invalid = wintypes.HANDLE(-1).value
    for path in (root, *enumerate_closed_local_tree(root, label="filing-XBRL fenced tree")):
        is_directory = path.is_dir()
        flags = 0x02000000 if is_directory else 0x00000080
        rights = (
            (0x00000002, 0x00000004, 0x00000010, 0x00000040, 0x00000100)
            if is_directory
            else (0x40000000, 0x00010000)
        )
        for right in rights:
            handle = create_file(
                str(path),
                right,
                0x00000001 | 0x00000002 | 0x00000004,
                None,
                3,
                flags,
                None,
            )
            if handle != invalid:
                close_handle(handle)
                raise InlineXbrlProcessorError("filing-XBRL fenced tree remains writable")
            if ctypes.get_last_error() != 5:
                raise InlineXbrlProcessorError("filing-XBRL fenced tree access cannot be verified")


@contextmanager
def _write_denial_fence(paths: Sequence[Path]) -> Generator[str, None, None]:
    unique = tuple(dict.fromkeys(Path(os.path.abspath(path)) for path in paths))
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create_file = kernel32.CreateFileW
        create_file.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        create_file.restype = wintypes.HANDLE
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = [wintypes.HANDLE]
        close_handle.restype = wintypes.BOOL
        handles: list[int] = []
        try:
            for path in unique:
                _require_no_reparse_points(path)
                observed = path.stat()
                is_directory = path.is_dir()
                if not is_directory and observed.st_nlink != 1:
                    raise InlineXbrlProcessorError(
                        "filing-XBRL fenced artifact has a hardlink alias"
                    )
                handle = create_file(
                    str(path),
                    0x80000000,
                    0x00000001,
                    None,
                    3,
                    0x02000000 if is_directory else 0x00000080,
                    None,
                )
                if handle == wintypes.HANDLE(-1).value:
                    raise InlineXbrlProcessorError(
                        f"filing-XBRL write-denial fence failed ({ctypes.get_last_error()})"
                    )
                handles.append(int(handle))
            yield "windows-deny-write"
        finally:
            for handle in reversed(handles):
                close_handle(wintypes.HANDLE(handle))
        return
    yield "non-windows-test-only"


def _verify_member(member: ProcessorPackageMember) -> None:
    _require_local_path(member.local_path, "filing-XBRL package member")
    try:
        if member.local_path.stat().st_nlink != 1:
            raise InlineXbrlProcessorError("filing-XBRL package member has a hardlink alias")
        body = member.local_path.read_bytes()
    except OSError as exc:
        raise InlineXbrlProcessorError("filing-XBRL package member is unavailable") from exc
    if len(body) != member.byte_size or _sha(body) != member.blob_sha256:
        raise InlineXbrlProcessorError("filing-XBRL package member fails hash verification")


def _verify_executable(path: Path, expected_sha256: str, label: str) -> None:
    _require_local_path(path, label)
    try:
        if path.stat().st_nlink != 1:
            raise InlineXbrlProcessorError(f"{label} has a hardlink alias")
        body = path.read_bytes()
    except OSError as exc:
        raise InlineXbrlProcessorError(f"{label} is unavailable") from exc
    if _sha(body) != expected_sha256:
        raise InlineXbrlProcessorError(f"{label} fails independent hash verification")


def _verify_runtime_closure(
    root: Path,
    locked_members: Sequence[RuntimeArtifactMember],
    *,
    expected_sha256: str,
) -> str:
    _require_local_path(root, "qualified filing-XBRL runtime root")
    if not root.is_dir():
        raise InlineXbrlProcessorError("qualified filing-XBRL runtime root is unavailable")
    locked = {member.relative_path: member for member in locked_members}
    observed_paths: set[str] = set()
    candidates = enumerate_closed_local_tree(
        root,
        label="qualified filing-XBRL runtime",
    )
    for path in candidates:
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        observed_paths.add(relative)
        member = locked.get(relative)
        if member is None:
            raise InlineXbrlProcessorError(
                "qualified filing-XBRL runtime contains an unlocked file"
            )
        try:
            if path.stat().st_nlink != 1:
                raise InlineXbrlProcessorError(
                    "qualified filing-XBRL runtime member has a hardlink alias"
                )
            body = path.read_bytes()
        except OSError as exc:
            raise InlineXbrlProcessorError(
                "qualified filing-XBRL runtime member is unavailable"
            ) from exc
        if len(body) != member.byte_size or _sha(body) != member.blob_sha256:
            raise InlineXbrlProcessorError(
                "qualified filing-XBRL runtime member fails hash verification"
            )
    if observed_paths != set(locked):
        raise InlineXbrlProcessorError(
            "qualified filing-XBRL runtime lock contains unavailable members"
        )
    actual = runtime_artifact_set_sha256(locked_members)
    if actual != expected_sha256:
        raise InlineXbrlProcessorError(
            "qualified filing-XBRL runtime closure digest does not match"
        )
    return actual


def _require_local_path(path: Path, label: str) -> None:
    rendered = str(path)
    if (
        not path.is_absolute()
        or rendered.startswith(("\\\\", "//", "/\\\\", "\\//"))
        or path.anchor.startswith(("\\\\", "//"))
    ):
        raise InlineXbrlProcessorError(f"{label} must use an absolute non-UNC local path")


def _contains_inline_xbrl(path: Path) -> bool:
    try:
        body = path.read_bytes().lower()
    except OSError as exc:
        raise InlineXbrlProcessorError("filing entrypoint is unavailable") from exc
    return b"<ix:" in body or b"xmlns:ix=" in body


def run_capped_process(
    command: Sequence[str],
    payload: bytes,
    *,
    environment: Mapping[str, str],
    timeout_seconds: int,
    maximum_stdout_bytes: int,
    maximum_stderr_bytes: int,
) -> tuple[int, bytes, bytes]:
    """Stream both pipes, terminating immediately when either hard cap is crossed."""

    try:
        process = subprocess.Popen(  # nosec B603 -- exact qualified launcher is hash-pinned
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=dict(environment),
        )
    except OSError as exc:
        raise InlineXbrlProcessorError("qualified filing-XBRL processor failed to run") from exc
    overflow = threading.Event()
    stdout_chunks: list[bytes] = []
    stderr_chunks: list[bytes] = []

    def drain(
        pipe: BinaryIO,
        chunks: list[bytes],
        maximum: int,
    ) -> None:
        total = 0
        while True:
            chunk = pipe.read(65_536)
            if not chunk:
                return
            total += len(chunk)
            if total > maximum:
                overflow.set()
                process.terminate()
                return
            chunks.append(chunk)

    assert process.stdout is not None
    assert process.stderr is not None
    stdout_thread = threading.Thread(
        target=drain,
        args=(process.stdout, stdout_chunks, maximum_stdout_bytes),
        daemon=True,
    )
    stderr_thread = threading.Thread(
        target=drain,
        args=(process.stderr, stderr_chunks, maximum_stderr_bytes),
        daemon=True,
    )
    stdout_thread.start()
    stderr_thread.start()
    try:
        assert process.stdin is not None
        process.stdin.write(payload)
        process.stdin.close()
        returncode = process.wait(timeout=timeout_seconds)
    except (OSError, subprocess.TimeoutExpired) as exc:
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        raise InlineXbrlProcessorError("qualified filing-XBRL processor timed out") from exc
    stdout_thread.join(timeout=2)
    stderr_thread.join(timeout=2)
    if stdout_thread.is_alive() or stderr_thread.is_alive():
        process.kill()
        process.wait()
        raise InlineXbrlProcessorError("filing-XBRL processor pipes did not close")
    if overflow.is_set():
        if process.poll() is None:
            process.kill()
            process.wait()
        raise InlineXbrlProcessorError("filing-XBRL processor output exceeds its byte budget")
    return returncode, b"".join(stdout_chunks), b"".join(stderr_chunks)


def _isolated_environment(environment: Mapping[str, str] | None) -> dict[str, str]:
    source = os.environ if environment is None else environment
    allowed = {
        key: value
        for key, value in source.items()
        if key.upper() in {"SYSTEMROOT", "WINDIR", "TEMP", "TMP", "TZ", "LOCALAPPDATA"}
    }
    allowed.update(
        {
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONHASHSEED": "0",
            "PYTHONNOUSERSITE": "1",
        }
    )
    return allowed


def _sandbox_refusal_stage(stderr: bytes) -> str | None:
    for raw_line in reversed(stderr.splitlines()):
        try:
            value = json.loads(raw_line)
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(value, dict) or value.get("event") != "filing_xbrl_sandbox_refused":
            continue
        fields = cast(dict[str, object], value)
        stage = fields.get("stage")
        native_error = fields.get("native_error_code")
        if (
            not isinstance(stage, str)
            or not stage
            or len(stage) > 128
            or any(
                not character.isascii() or (not character.isalnum() and character not in " _-")
                for character in stage
            )
            or not isinstance(native_error, int)
            or isinstance(native_error, bool)
        ):
            return "refusal evidence malformed"
        return f"stage={stage}; native_error_code={native_error}"
    return "refusal evidence unavailable"


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _present_locator_field(locator: Mapping[str, JsonValue], field: str) -> bool:
    value = locator.get(field)
    if field == "filing_ordinal":
        return isinstance(value, int) and not isinstance(value, bool) and value >= 0
    return isinstance(value, str) and bool(value.strip())


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()
