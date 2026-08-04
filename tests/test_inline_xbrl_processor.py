from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager
from pathlib import Path
from typing import IO, Protocol, cast

import pytest

import filings.inline_xbrl_processor as processor_module
from filings.inline_xbrl_processor import (
    ApprovedProcessorBundle,
    InlineXbrlProcessorError,
    InlineXbrlProcessorRequest,
    ProcessorBundleManifest,
    ProcessorPackageMember,
    RuntimeArtifactMember,
    load_processor_bundle_manifest,
    package_member_set_sha256,
    run_inline_xbrl_processor,
    runtime_artifact_set_sha256,
)

ROOT = Path(__file__).resolve().parents[1]


class _TestProcessorRunner(Protocol):
    def __call__(
        self,
        request: InlineXbrlProcessorRequest,
        *,
        manifest: ProcessorBundleManifest,
        runtime_root: Path,
        bundle_python: Path,
        sandbox_launcher: Path,
        environment: Mapping[str, str] | None = None,
    ) -> processor_module.InlineXbrlProcessorResult: ...


_run_inline_xbrl_processor_for_test = cast(
    _TestProcessorRunner,
    getattr(processor_module, "_run_inline_xbrl_processor"),
)
_stage_package_for_test = cast(
    Callable[..., tuple[InlineXbrlProcessorRequest, Path]],
    getattr(processor_module, "_stage_package"),
)
_tree_write_denial_fence_for_test = cast(
    Callable[[Sequence[Path]], AbstractContextManager[str]],
    getattr(processor_module, "_tree_write_denial_fence"),
)


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _sha(value: object) -> str:
    body = value if isinstance(value, bytes) else _canonical(value).encode()
    return hashlib.sha256(body).hexdigest()


def _request(tmp_path: Path, *, inline: bool = True) -> InlineXbrlProcessorRequest:
    body = b"<html><ix:nonFraction>10</ix:nonFraction></html>" if inline else b"<html/>"
    path = tmp_path / "filing.htm"
    path.write_bytes(body)
    member = ProcessorPackageMember(
        member_ordinal=0,
        member_role="primary_document",
        document_version_id="document-1",
        source_url="https://www.sec.gov/Archives/edgar/data/1/000000000126000001/filing.htm",
        local_path=path,
        blob_sha256=_sha(body),
        byte_size=len(body),
        media_type="text/html",
    )
    return InlineXbrlProcessorRequest(
        accession_number="0000000001-26-000001",
        entrypoint_ordinal=0,
        members=(member,),
        expected_cik="0000000001",
        package_member_set_sha256=package_member_set_sha256((member,)),
    )


def _qualified_manifest(
    tmp_path: Path,
) -> tuple[ProcessorBundleManifest, Path, Path, Path]:
    launcher = tmp_path / "sandbox-launcher.exe"
    runtime_root = tmp_path / "runtime"
    bundle = runtime_root / "Scripts" / "python.exe"
    bundle.parent.mkdir(parents=True)
    launcher.write_bytes(b"qualified OS sandbox launcher")
    bundle.write_bytes(b"qualified bundle Python")
    manifest = load_processor_bundle_manifest(ROOT / "config" / "filing_xbrl_processor_bundle.json")
    runtime_members = (
        RuntimeArtifactMember(
            relative_path="Scripts/python.exe",
            blob_sha256=_sha(bundle.read_bytes()),
            byte_size=bundle.stat().st_size,
        ),
    )
    execution = manifest.execution.model_copy(
        update={
            "sandbox_launcher_sha256": _sha(launcher.read_bytes()),
            "bundle_python_sha256": _sha(bundle.read_bytes()),
            "runtime_members": runtime_members,
            "runtime_artifact_sha256": runtime_artifact_set_sha256(runtime_members),
        }
    )
    return (
        manifest.model_copy(update={"execution": execution}),
        launcher,
        bundle,
        runtime_root,
    )


def _fact_output(
    request: InlineXbrlProcessorRequest,
    runtime_artifact_sha256: str,
) -> dict[str, object]:
    raw = {"concept": "Revenue", "value": "10"}
    locator = {
        "source_ref": request.members[0].source_url,
        "filing_ordinal": 0,
        "xbrl_package_member": request.members[0].source_url,
        "xbrl_context_id": "ctx-1",
        "xbrl_fact_id": "fact-1",
        "xbrl_element_path": "/html/body/ix:nonFraction[1]",
        "xbrl_concept_namespace": "https://example.test/taxonomy",
        "xbrl_concept_name": "Revenue",
    }
    raw_sha = _sha(raw)
    locator_sha = _sha(locator)
    source_entry = {
        "accession_number": request.accession_number,
        "observed_cik": request.expected_cik,
        "package_member_blob_sha256": request.members[0].blob_sha256,
        "package_member_ordinal": 0,
        "raw_fact_sha256": raw_sha,
        "source_locator_sha256": locator_sha,
    }
    fact: dict[str, object] = {
        "input_ordinal": 0,
        "package_member_ordinal": 0,
        "package_member_blob_sha256": request.members[0].blob_sha256,
        "accession_number": request.accession_number,
        "observed_cik": request.expected_cik,
        "evidence_text": "Revenue 10",
        "source_locator": locator,
        "source_locator_sha256": locator_sha,
        "canonical_raw_fact": raw,
        "raw_fact_sha256": raw_sha,
        "source_entry_sha256": _sha(source_entry),
        "normalization_outcome": "rejected",
        "rejection_reason_code": "fixture_rejection",
        "rejection_detail": "offline fixture",
        "footnotes": list[object](),
    }
    raw_set: list[object] = [
        {
            "input_ordinal": 0,
            "raw_fact_sha256": raw_sha,
            "source_entry_sha256": fact["source_entry_sha256"],
            "source_locator_sha256": locator_sha,
        }
    ]
    network: list[object] = []
    return {
        "bridge_protocol_version": "filing-xbrl-bridge.v1",
        "coordinates": {"arelle": "2.39.8", "edgar": "26.1", "xule": "30052"},
        "execution_evidence": {
            "sandbox_contract_version": "earnings-xbrl-os-sandbox.v1",
            "internet_connectivity": "os_denied",
            "network_requests_observed": 0,
            "accession_number": request.accession_number,
            "expected_cik": request.expected_cik,
            "package_member_set_sha256": request.package_member_set_sha256,
            "runtime_artifact_sha256": runtime_artifact_sha256,
        },
        "runtime_artifact_sha256": runtime_artifact_sha256,
        "package_member_set_sha256": request.package_member_set_sha256,
        "network_artifacts": network,
        "network_artifact_count": 0,
        "network_artifact_set_sha256": _sha(network),
        "facts": [fact],
        "raw_fact_set_sha256": _sha(raw_set),
        "footnote_count": 0,
        "footnote_set_sha256": _sha([]),
        "zero_fact_disposition": None,
    }


def test_qualified_bundle_manifest_is_exact_and_fail_closed_by_default() -> None:
    manifest = load_processor_bundle_manifest(ROOT / "config" / "filing_xbrl_processor_bundle.json")
    assert manifest.coordinates.model_dump() == {
        "arelle": "2.39.8",
        "edgar": "26.1",
        "xule": "30052",
    }
    assert manifest.execution.internet_connectivity == "os_denied"
    assert manifest.execution.sandbox_contract_version == "earnings-xbrl-os-sandbox.v1"
    assert manifest.execution.sandbox_launcher_sha256 == "0" * 64
    assert manifest.build_provenance.edgar_git_commit == (
        "99e94b6c6f5ca2ef06a9c2f29b0a4290a7f959db"  # pragma: allowlist secret
    )


def test_public_runner_refuses_caller_constructed_manifest(tmp_path: Path) -> None:
    manifest = load_processor_bundle_manifest(ROOT / "config" / "filing_xbrl_processor_bundle.json")
    caller_claimed_bundle = cast(ApprovedProcessorBundle, manifest)

    with pytest.raises(InlineXbrlProcessorError, match="bundle is not approved"):
        run_inline_xbrl_processor(
            _request(tmp_path),
            approved_bundle=caller_claimed_bundle,
            runtime_root=tmp_path / "runtime",
            bundle_python=tmp_path / "python.exe",
            sandbox_launcher=tmp_path / "launcher.exe",
        )
    assert manifest.qualification.profile == "sec-inline-xbrl-investor-grade.v1"


def test_processor_uses_hash_pinned_os_sandbox_and_closed_sets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = _request(tmp_path)
    manifest, launcher, bundle, runtime_root = _qualified_manifest(tmp_path)
    observed: dict[str, object] = {}

    def fake_capped(
        command: object,
        payload: object,
        **kwargs: object,
    ) -> tuple[int, bytes, bytes]:
        observed["command"] = command
        observed["environment"] = kwargs["environment"]
        observed["payload"] = payload
        return (
            0,
            _canonical(_fact_output(request, manifest.execution.runtime_artifact_sha256)).encode(),
            b"",
        )

    monkeypatch.setattr(processor_module, "run_capped_process", fake_capped)
    result = _run_inline_xbrl_processor_for_test(
        request,
        manifest=manifest,
        bundle_python=bundle,
        sandbox_launcher=launcher,
        runtime_root=runtime_root,
        environment={
            "SYSTEMROOT": "C:\\Windows",
            "SECRET_TOKEN": "must-not-cross",  # pragma: allowlist secret
        },
    )
    assert result.runtime_artifact_sha256 == manifest.execution.runtime_artifact_sha256
    command = observed["command"]
    assert isinstance(command, tuple)
    assert command[:9] == (
        str(launcher),
        "--contract",
        "earnings-xbrl-os-sandbox.v1",
        "--deny-network",
        "--read-tree",
        str(runtime_root.resolve()),
        "--read-input-tree",
        str(
            (
                runtime_root.parent
                / "filing-xbrl-package-cache"
                / request.package_member_set_sha256
            ).resolve()
        ),
        "--",
    )
    assert command[-2:] == (
        "--runtime-artifact-sha256",
        manifest.execution.runtime_artifact_sha256,
    )
    environment = observed["environment"]
    assert isinstance(environment, dict)
    assert "SECRET_TOKEN" not in environment
    assert "EARNINGS_XBRL_NETWORK" not in environment
    assert environment["PYTHONDONTWRITEBYTECODE"] == "1"
    staged = json.loads(cast(bytes, observed["payload"]))["members"][0]["local_path"]
    assert Path(staged).read_bytes() == request.members[0].local_path.read_bytes()
    assert (
        Path(staged)
        .relative_to(runtime_root.parent)
        .as_posix()
        .endswith("www.sec.gov/Archives/edgar/data/1/000000000126000001/filing.htm")
    )


def test_processor_rejects_colliding_package_member_names(tmp_path: Path) -> None:
    request = _request(tmp_path)
    second_path = tmp_path / "taxonomy.xsd"
    second_path.write_bytes(b"one")
    third_path = tmp_path / "different" / "taxonomy.xsd"
    third_path.parent.mkdir()
    third_path.write_bytes(b"two")
    members = (
        request.members[0],
        ProcessorPackageMember(
            member_ordinal=1,
            member_role="issuer_taxonomy",
            document_version_id="taxonomy-1",
            source_url="https://www.sec.gov/Archives/one/Taxonomy.xsd",
            local_path=second_path,
            blob_sha256=_sha(b"one"),
            byte_size=3,
            media_type="application/xml",
        ),
        ProcessorPackageMember(
            member_ordinal=2,
            member_role="issuer_taxonomy",
            document_version_id="taxonomy-2",
            source_url="https://WWW.SEC.GOV/Archives/one/taxonomy.xsd",
            local_path=third_path,
            blob_sha256=_sha(b"two"),
            byte_size=3,
            media_type="application/xml",
        ),
    )
    colliding = request.model_copy(
        update={
            "members": members,
            "package_member_set_sha256": package_member_set_sha256(members),
        }
    )
    manifest, launcher, bundle, runtime_root = _qualified_manifest(tmp_path)

    with pytest.raises(InlineXbrlProcessorError, match="URL-path collision"):
        _run_inline_xbrl_processor_for_test(
            colliding,
            manifest=manifest,
            bundle_python=bundle,
            sandbox_launcher=launcher,
            runtime_root=runtime_root,
        )


def test_processor_rejects_hardlinked_runtime_member(tmp_path: Path) -> None:
    request = _request(tmp_path)
    manifest, launcher, bundle, runtime_root = _qualified_manifest(tmp_path)
    alias = tmp_path / "bundle-alias.exe"
    try:
        os.link(bundle, alias)
    except OSError:
        pytest.skip("hardlinks are unavailable")

    with pytest.raises(InlineXbrlProcessorError, match="hardlink alias"):
        _run_inline_xbrl_processor_for_test(
            request,
            manifest=manifest,
            bundle_python=bundle,
            sandbox_launcher=launcher,
            runtime_root=runtime_root,
        )


def test_processor_rejects_runtime_junction(tmp_path: Path) -> None:
    if os.name != "nt":
        pytest.skip("Windows junctions are unavailable")
    request = _request(tmp_path)
    manifest, launcher, bundle, runtime_root = _qualified_manifest(tmp_path)
    target = tmp_path / "junction-target"
    target.mkdir()
    (target / "sitecustomize.py").write_text("raise RuntimeError('injected')")
    junction = runtime_root / "junction"
    created = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(junction), str(target)],
        capture_output=True,
        check=False,
        text=True,
    )
    if created.returncode != 0:
        pytest.skip("junction creation is unavailable")

    with pytest.raises(InlineXbrlProcessorError, match="reparse point"):
        _run_inline_xbrl_processor_for_test(
            request,
            manifest=manifest,
            bundle_python=bundle,
            sandbox_launcher=launcher,
            runtime_root=runtime_root,
        )


def test_windows_tree_fence_blocks_and_then_restores_runtime_writes(tmp_path: Path) -> None:
    if os.name != "nt":
        pytest.skip("Windows share-mode fence is unavailable")
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()

    with _tree_write_denial_fence_for_test((runtime_root,)), pytest.raises(OSError):
        (runtime_root / "sitecustomize.py").write_text("injected")
    (runtime_root / "restored.py").write_text("restored")


def test_package_cache_refuses_new_entry_at_completed_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()
    cache_root = tmp_path / "filing-xbrl-package-cache"
    completed = cache_root / ("a" * 64)
    completed.mkdir(parents=True)
    (completed / "filing.htm").write_bytes(b"cached")
    monkeypatch.setattr(processor_module, "_PACKAGE_CACHE_MAX_COMPLETED", 1)

    with pytest.raises(InlineXbrlProcessorError, match="entry limit"):
        _stage_package_for_test(_request(tmp_path), runtime_root=runtime_root)


def test_package_staging_refuses_source_growth_before_copying_extra_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()
    request = _request(tmp_path)
    source = request.members[0].local_path
    source_body = source.read_bytes()
    original_open = Path.open

    def growing_open(
        path: Path,
        mode: str = "r",
        buffering: int = -1,
        encoding: str | None = None,
        errors: str | None = None,
        newline: str | None = None,
    ) -> IO[str] | IO[bytes]:
        if path == source and mode == "rb":
            return io.BytesIO(source_body + b"growth")
        return original_open(
            path,
            mode=mode,
            buffering=buffering,
            encoding=encoding,
            errors=errors,
            newline=newline,
        )

    monkeypatch.setattr(Path, "open", growing_open)
    with pytest.raises(InlineXbrlProcessorError, match="grew while staging"):
        _stage_package_for_test(request, runtime_root=runtime_root)

    staged_files = tuple(
        path for path in (tmp_path / "filing-xbrl-package-cache").rglob("*") if path.is_file()
    )
    assert sum(path.stat().st_size for path in staged_files) <= request.members[0].byte_size


def test_processor_rejects_unpinned_launcher_before_execution(tmp_path: Path) -> None:
    request = _request(tmp_path)
    manifest, launcher, bundle, runtime_root = _qualified_manifest(tmp_path)
    launcher.write_bytes(b"tampered launcher")
    with pytest.raises(InlineXbrlProcessorError, match="launcher fails"):
        _run_inline_xbrl_processor_for_test(
            request,
            manifest=manifest,
            bundle_python=bundle,
            sandbox_launcher=launcher,
            runtime_root=runtime_root,
        )


def test_processor_rejects_zero_facts_for_independently_detected_inline_xbrl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = _request(tmp_path)
    manifest, launcher, bundle, runtime_root = _qualified_manifest(tmp_path)
    empty_output: dict[str, object] = {
        "bridge_protocol_version": "filing-xbrl-bridge.v1",
        "coordinates": {"arelle": "2.39.8", "edgar": "26.1", "xule": "30052"},
        "execution_evidence": {
            "sandbox_contract_version": "earnings-xbrl-os-sandbox.v1",
            "internet_connectivity": "os_denied",
            "network_requests_observed": 0,
            "accession_number": request.accession_number,
            "expected_cik": request.expected_cik,
            "package_member_set_sha256": request.package_member_set_sha256,
            "runtime_artifact_sha256": manifest.execution.runtime_artifact_sha256,
        },
        "runtime_artifact_sha256": manifest.execution.runtime_artifact_sha256,
        "package_member_set_sha256": request.package_member_set_sha256,
        "network_artifacts": list[object](),
        "network_artifact_count": 0,
        "network_artifact_set_sha256": _sha([]),
        "facts": list[object](),
        "raw_fact_set_sha256": _sha([]),
        "footnote_count": 0,
        "footnote_set_sha256": _sha([]),
        "zero_fact_disposition": "verified_no_inline_xbrl",
    }

    def fake_empty(
        command: object,
        payload: object,
        **kwargs: object,
    ) -> tuple[int, bytes, bytes]:
        del command, payload, kwargs
        return 0, _canonical(empty_output).encode(), b""

    monkeypatch.setattr(processor_module, "run_capped_process", fake_empty)
    with pytest.raises(InlineXbrlProcessorError, match="zero facts"):
        _run_inline_xbrl_processor_for_test(
            request,
            manifest=manifest,
            bundle_python=bundle,
            sandbox_launcher=launcher,
            runtime_root=runtime_root,
        )


def test_processor_rejects_fact_bound_to_wrong_cik(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = _request(tmp_path)
    manifest, launcher, bundle, runtime_root = _qualified_manifest(tmp_path)
    output = _fact_output(request, manifest.execution.runtime_artifact_sha256)
    facts_value = output["facts"]
    assert isinstance(facts_value, list)
    assert isinstance(facts_value[0], dict)
    fact = dict(cast(dict[str, object], facts_value[0]))
    fact["observed_cik"] = "0000000002"
    source_entry: dict[str, object] = {
        "accession_number": fact["accession_number"],
        "observed_cik": fact["observed_cik"],
        "package_member_blob_sha256": fact["package_member_blob_sha256"],
        "package_member_ordinal": fact["package_member_ordinal"],
        "raw_fact_sha256": fact["raw_fact_sha256"],
        "source_locator_sha256": fact["source_locator_sha256"],
    }
    fact["source_entry_sha256"] = _sha(source_entry)
    output["facts"] = [fact]
    output["raw_fact_set_sha256"] = _sha(
        [
            {
                "input_ordinal": 0,
                "raw_fact_sha256": fact["raw_fact_sha256"],
                "source_entry_sha256": fact["source_entry_sha256"],
                "source_locator_sha256": fact["source_locator_sha256"],
            }
        ]
    )

    def fake_wrong_cik(
        command: object,
        payload: object,
        **kwargs: object,
    ) -> tuple[int, bytes, bytes]:
        del command, payload, kwargs
        return 0, _canonical(output).encode(), b""

    monkeypatch.setattr(processor_module, "run_capped_process", fake_wrong_cik)
    with pytest.raises(InlineXbrlProcessorError, match="CIK"):
        _run_inline_xbrl_processor_for_test(
            request,
            manifest=manifest,
            bundle_python=bundle,
            sandbox_launcher=launcher,
            runtime_root=runtime_root,
        )


def test_processor_rejects_execution_evidence_not_bound_to_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = _request(tmp_path)
    manifest, launcher, bundle, runtime_root = _qualified_manifest(tmp_path)
    output = _fact_output(request, manifest.execution.runtime_artifact_sha256)
    execution_evidence = output["execution_evidence"]
    assert isinstance(execution_evidence, dict)
    execution_evidence["expected_cik"] = "0000000002"

    def fake_wrong_execution_evidence(
        command: object,
        payload: object,
        **kwargs: object,
    ) -> tuple[int, bytes, bytes]:
        del command, payload, kwargs
        return 0, _canonical(output).encode(), b""

    monkeypatch.setattr(
        processor_module,
        "run_capped_process",
        fake_wrong_execution_evidence,
    )
    with pytest.raises(InlineXbrlProcessorError, match="execution evidence"):
        _run_inline_xbrl_processor_for_test(
            request,
            manifest=manifest,
            bundle_python=bundle,
            sandbox_launcher=launcher,
            runtime_root=runtime_root,
        )


def test_capped_process_terminates_stdout_overflow() -> None:
    with pytest.raises(InlineXbrlProcessorError, match="byte budget"):
        processor_module.run_capped_process(
            (sys.executable, "-c", "import sys; sys.stdout.write('x'*1000000)"),
            b"",
            environment=os.environ,
            timeout_seconds=10,
            maximum_stdout_bytes=1024,
            maximum_stderr_bytes=1024,
        )


def test_sandbox_refusal_diagnostic_is_closed() -> None:
    refusal_stage = cast(
        Callable[[bytes], str | None],
        getattr(processor_module, "_sandbox_refusal_stage"),
    )
    stderr = (
        b'noise\n{"event":"filing_xbrl_sandbox_refused",'
        b'"error_type":"LauncherWin32Exception","native_error_code":5,'
        b'"stage":"cannot apply sandbox acl"}\n'
    )
    assert refusal_stage(stderr) == ("stage=cannot apply sandbox acl; native_error_code=5")
    assert (
        refusal_stage(
            b'{"event":"filing_xbrl_sandbox_refused","native_error_code":5,"stage":"C:\\\\secret"}'
        )
        == "refusal evidence malformed"
    )
