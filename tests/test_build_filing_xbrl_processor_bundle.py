from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest

import filings.inline_xbrl_processor as processor_module
from execution.build_filing_xbrl_processor_bundle import (
    FilingXbrlBundleBuildRequest,
    build_filing_xbrl_processor_bundle,
)
from filings.inline_xbrl_processor import (
    ApprovedProcessorBundle,
    InlineXbrlProcessorError,
    load_approved_processor_bundle_manifest,
    load_processor_bundle_manifest,
)

ROOT = Path(__file__).resolve().parents[1]
_load_approved_bundle_for_test = cast(
    Callable[..., ApprovedProcessorBundle],
    getattr(processor_module, "_load_approved_processor_bundle_manifest"),
)


def _write_offline_cache(runtime: Path) -> None:
    cache_member = runtime / "offline-cache" / "https" / "taxonomy.example" / "2026.xsd"
    cache_member.parent.mkdir(parents=True)
    cache_member.write_bytes(b"sealed taxonomy")


def test_bundle_builder_seals_full_runtime_and_exact_replay(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    python = runtime / "Scripts" / "python.exe"
    python.parent.mkdir(parents=True)
    python.write_bytes(b"python")
    shutil.copyfile(
        ROOT / "execution" / "filing_xbrl_bridge.py",
        runtime / "earnings_summary_xbrl_bridge.py",
    )
    _write_offline_cache(runtime)
    launcher = tmp_path / "launcher.exe"
    launcher.write_bytes(b"launcher")
    output = tmp_path / "qualified.json"
    request = FilingXbrlBundleBuildRequest(
        template=ROOT / "config" / "filing_xbrl_processor_bundle.json",
        runtime_root=runtime,
        sandbox_launcher=launcher,
        output=output,
    )

    first = build_filing_xbrl_processor_bundle(request)
    second = build_filing_xbrl_processor_bundle(request)

    assert first.published is True
    assert second.published is False
    assert first.runtime_member_count == 3
    sealed = load_processor_bundle_manifest(output)
    assert sealed.execution.runtime_artifact_sha256 == first.runtime_artifact_sha256
    assert [member.relative_path for member in sealed.execution.runtime_members] == [
        "Scripts/python.exe",
        "earnings_summary_xbrl_bridge.py",
        "offline-cache/https/taxonomy.example/2026.xsd",
    ]
    assert json.loads(output.read_text())["execution"]["sandbox_launcher_sha256"] == (
        first.sandbox_launcher_sha256
    )


def test_bundle_builder_rejects_missing_offline_taxonomy_cache(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    python = runtime / "Scripts" / "python.exe"
    python.parent.mkdir(parents=True)
    python.write_bytes(b"python")
    shutil.copyfile(
        ROOT / "execution" / "filing_xbrl_bridge.py",
        runtime / "earnings_summary_xbrl_bridge.py",
    )
    launcher = tmp_path / "launcher.exe"
    launcher.write_bytes(b"launcher")

    with pytest.raises(ValueError, match="offline taxonomy cache"):
        build_filing_xbrl_processor_bundle(
            FilingXbrlBundleBuildRequest(
                template=ROOT / "config" / "filing_xbrl_processor_bundle.json",
                runtime_root=runtime,
                sandbox_launcher=launcher,
                output=tmp_path / "qualified.json",
            )
        )


def test_bundle_builder_rejects_mutable_bytecode(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    python = runtime / "Scripts" / "python.exe"
    python.parent.mkdir(parents=True)
    python.write_bytes(b"python")
    shutil.copyfile(
        ROOT / "execution" / "filing_xbrl_bridge.py",
        runtime / "earnings_summary_xbrl_bridge.py",
    )
    cache = runtime / "__pycache__"
    cache.mkdir()
    (cache / "bridge.pyc").write_bytes(b"mutable")
    launcher = tmp_path / "launcher.exe"
    launcher.write_bytes(b"launcher")

    with pytest.raises(ValueError, match="mutable Python bytecode"):
        build_filing_xbrl_processor_bundle(
            FilingXbrlBundleBuildRequest(
                template=ROOT / "config" / "filing_xbrl_processor_bundle.json",
                runtime_root=runtime,
                sandbox_launcher=launcher,
                output=tmp_path / "qualified.json",
            )
        )


def test_approved_bundle_loader_rejects_unsealed_manifest(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    python = runtime / "Scripts" / "python.exe"
    python.parent.mkdir(parents=True)
    python.write_bytes(b"python")
    shutil.copyfile(
        ROOT / "execution" / "filing_xbrl_bridge.py",
        runtime / "earnings_summary_xbrl_bridge.py",
    )
    _write_offline_cache(runtime)
    launcher = tmp_path / "launcher.exe"
    launcher.write_bytes(b"launcher")
    output = tmp_path / "qualified.json"
    result = build_filing_xbrl_processor_bundle(
        FilingXbrlBundleBuildRequest(
            template=ROOT / "config" / "filing_xbrl_processor_bundle.json",
            runtime_root=runtime,
            sandbox_launcher=launcher,
            output=output,
        )
    )
    manifest = load_processor_bundle_manifest(output)
    seal = tmp_path / "approval.json"
    seal.write_text(
        json.dumps(
            {
                "schema_version": "filing-xbrl-bundle-approval/v1",
                "manifest_sha256": result.manifest_sha256,
                "manifest_artifact_sha256": result.output_sha256,
                "runtime_artifact_sha256": result.runtime_artifact_sha256,
                "sandbox_launcher_sha256": result.sandbox_launcher_sha256,
                "bridge_source_sha256": manifest.build_provenance.bridge_source_sha256,
                "launcher_source_sha256": manifest.build_provenance.launcher_source_sha256,
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    approved_bundle = _load_approved_bundle_for_test(
        output,
        approval_seal_path=seal,
    )
    assert approved_bundle.manifest == manifest

    replacement = tmp_path / "replacement.exe"
    replacement.write_bytes(b"replacement")
    unapproved = tmp_path / "unapproved.json"
    build_filing_xbrl_processor_bundle(
        FilingXbrlBundleBuildRequest(
            template=ROOT / "config" / "filing_xbrl_processor_bundle.json",
            runtime_root=runtime,
            sandbox_launcher=replacement,
            output=unapproved,
        )
    )
    with pytest.raises(InlineXbrlProcessorError, match="not approved"):
        _load_approved_bundle_for_test(unapproved, approval_seal_path=seal)


def test_public_approval_loader_cannot_accept_a_caller_supplied_seal(
    tmp_path: Path,
) -> None:
    arbitrary_loader = cast(Callable[..., object], load_approved_processor_bundle_manifest)

    with pytest.raises(TypeError, match="approval_seal_path"):
        arbitrary_loader(
            tmp_path / "manifest.json",
            approval_seal_path=tmp_path / "caller-seal.json",
        )


def test_bundle_builder_rejects_hardlink_alias(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    python = runtime / "Scripts" / "python.exe"
    python.parent.mkdir(parents=True)
    python.write_bytes(b"python")
    alias = runtime / "python-alias.exe"
    try:
        os.link(python, alias)
    except OSError:
        pytest.skip("hardlinks are unavailable")
    shutil.copyfile(
        ROOT / "execution" / "filing_xbrl_bridge.py",
        runtime / "earnings_summary_xbrl_bridge.py",
    )
    launcher = tmp_path / "launcher.exe"
    launcher.write_bytes(b"launcher")

    with pytest.raises(ValueError, match="hardlink alias"):
        build_filing_xbrl_processor_bundle(
            FilingXbrlBundleBuildRequest(
                template=ROOT / "config" / "filing_xbrl_processor_bundle.json",
                runtime_root=runtime,
                sandbox_launcher=launcher,
                output=tmp_path / "qualified.json",
            )
        )


def test_bundle_builder_rejects_runtime_junction(tmp_path: Path) -> None:
    if os.name != "nt":
        pytest.skip("Windows junctions are unavailable")
    runtime = tmp_path / "runtime"
    python = runtime / "Scripts" / "python.exe"
    python.parent.mkdir(parents=True)
    python.write_bytes(b"python")
    shutil.copyfile(
        ROOT / "execution" / "filing_xbrl_bridge.py",
        runtime / "earnings_summary_xbrl_bridge.py",
    )
    target = tmp_path / "junction-target"
    target.mkdir()
    (target / "injected.py").write_text("raise RuntimeError('injected')")
    junction = runtime / "junction"
    created = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(junction), str(target)],
        capture_output=True,
        check=False,
        text=True,
    )
    if created.returncode != 0:
        pytest.skip("junction creation is unavailable")
    launcher = tmp_path / "launcher.exe"
    launcher.write_bytes(b"launcher")

    with pytest.raises(InlineXbrlProcessorError, match="reparse point"):
        build_filing_xbrl_processor_bundle(
            FilingXbrlBundleBuildRequest(
                template=ROOT / "config" / "filing_xbrl_processor_bundle.json",
                runtime_root=runtime,
                sandbox_launcher=launcher,
                output=tmp_path / "qualified.json",
            )
        )
