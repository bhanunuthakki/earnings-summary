"""Qualified offline subprocess boundary for filing-native Inline XBRL.

The application never imports Arelle.  A separately built processor bundle
owns Arelle, EDGAR and XULE dependencies and speaks one closed JSON protocol.
The bridge runs with ``-I`` and offline connectivity; its runtime artifact
digest and exact coordinates are verified before any output is admitted.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import threading
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

_SHA256 = r"^[0-9a-f]{64}$"


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


class ProcessorBundleManifest(_Closed):
    bundle_name: str = Field(min_length=1, max_length=128)
    bridge_module: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_.]*$")
    bridge_protocol_version: Literal["filing-xbrl-bridge.v1"]
    coordinates: ProcessorCoordinates
    execution: ProcessorExecution
    qualification: ProcessorQualification

    @property
    def canonical_json(self) -> str:
        return _canonical(self.model_dump(mode="json"))

    @property
    def manifest_sha256(self) -> str:
        return _sha(self.canonical_json.encode())


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
    byte_size: int = Field(ge=0)
    media_type: str = Field(min_length=1, max_length=255)


class InlineXbrlProcessorRequest(_Closed):
    accession_number: str = Field(pattern=r"^\d{10}-\d{2}-\d{6}$")
    entrypoint_ordinal: int = Field(ge=0)
    members: tuple[ProcessorPackageMember, ...] = Field(min_length=1)
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
    command = (
        str(sandbox_launcher),
        "--contract",
        manifest.execution.sandbox_contract_version,
        "--deny-network",
        "--",
        str(bundle_python),
        "-I",
        "-m",
        manifest.bridge_module,
        "--protocol",
        manifest.bridge_protocol_version,
    )
    clean_environment = _isolated_environment(environment)
    payload = _canonical(request.model_dump(mode="json"))
    returncode, stdout, _stderr = run_capped_process(
        command,
        payload.encode(),
        environment=clean_environment,
        timeout_seconds=manifest.execution.timeout_seconds,
        maximum_stdout_bytes=manifest.execution.maximum_stdout_bytes,
        maximum_stderr_bytes=manifest.execution.maximum_stderr_bytes,
    )
    if returncode != 0:
        raise InlineXbrlProcessorError(
            f"filing-XBRL processor rejected the package (exit {returncode})"
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


def _verify_member(member: ProcessorPackageMember) -> None:
    _require_local_path(member.local_path, "filing-XBRL package member")
    try:
        body = member.local_path.read_bytes()
    except OSError as exc:
        raise InlineXbrlProcessorError("filing-XBRL package member is unavailable") from exc
    if len(body) != member.byte_size or _sha(body) != member.blob_sha256:
        raise InlineXbrlProcessorError("filing-XBRL package member fails hash verification")


def _verify_executable(path: Path, expected_sha256: str, label: str) -> None:
    _require_local_path(path, label)
    try:
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
    try:
        candidates = sorted(root.rglob("*"))
    except OSError as exc:
        raise InlineXbrlProcessorError(
            "qualified filing-XBRL runtime cannot be enumerated"
        ) from exc
    for path in candidates:
        if path.is_symlink():
            raise InlineXbrlProcessorError("qualified filing-XBRL runtime cannot contain symlinks")
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
        if key.upper() in {"SYSTEMROOT", "WINDIR", "TEMP", "TMP", "TZ"}
    }
    allowed.update(
        {
            "PYTHONHASHSEED": "0",
            "PYTHONNOUSERSITE": "1",
        }
    )
    return allowed


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _present_locator_field(locator: Mapping[str, JsonValue], field: str) -> bool:
    value = locator.get(field)
    if field == "filing_ordinal":
        return isinstance(value, int) and not isinstance(value, bool) and value >= 0
    return isinstance(value, str) and bool(value.strip())


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()
