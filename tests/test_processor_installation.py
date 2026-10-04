from __future__ import annotations

import json
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import NoReturn, cast

import pytest

from execution import ingest_sec_filing_xbrl as cli
from execution.build_filing_xbrl_processor_bundle import (
    FilingXbrlBundleBuildRequest,
    build_filing_xbrl_processor_bundle,
)
from filings import inline_xbrl_processor as processor
from filings import processor_installation as installation_module
from filings.processor_installation import resolve_processor_installation


@pytest.fixture
def installed_descriptor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = Path(__file__).resolve().parents[1]
    runtime = tmp_path / "runtime"
    (runtime / "Scripts").mkdir(parents=True)
    (runtime / "Scripts" / "python.exe").write_bytes(b"synthetic Python")
    for source, name in (
        (root / "execution" / "filing_xbrl_bridge.py", "earnings_summary_xbrl_bridge.py"),
        (root / "src" / "filings" / "xbrl_units.py", "earnings_summary_xbrl_units.py"),
    ):
        shutil.copyfile(source, runtime / name)
    (runtime / "offline-cache").mkdir()
    (runtime / "offline-cache" / "taxonomy.xsd").write_bytes(b"synthetic taxonomy")
    launcher = tmp_path / "launcher.exe"
    launcher.write_bytes(b"synthetic launcher")
    manifest = tmp_path / "bundle.json"
    result = build_filing_xbrl_processor_bundle(
        FilingXbrlBundleBuildRequest(
            template=root / "config" / "filing_xbrl_processor_bundle.json",
            runtime_root=runtime,
            sandbox_launcher=launcher,
            output=manifest,
        )
    )
    parsed = processor.load_processor_bundle_manifest(manifest)
    seal = tmp_path / "synthetic-seal.json"
    seal.write_text(
        json.dumps(
            {
                "schema_version": "filing-xbrl-bundle-approval/v1",
                "manifest_sha256": result.manifest_sha256,
                "manifest_artifact_sha256": result.output_sha256,
                "runtime_artifact_sha256": result.runtime_artifact_sha256,
                "sandbox_launcher_sha256": result.sandbox_launcher_sha256,
                "bridge_source_sha256": parsed.build_provenance.bridge_source_sha256,
                "launcher_source_sha256": parsed.build_provenance.launcher_source_sha256,
                "unit_source_sha256": parsed.build_provenance.unit_source_sha256,
            }
        )
    )

    def synthetic_loader(path: Path) -> processor.ApprovedProcessorBundle:
        # Tests have a synthetic approval authority; public production loading is unchanged.
        loader = cast(
            Callable[..., processor.ApprovedProcessorBundle],
            getattr(processor, "_load_approved_processor_bundle_manifest"),
        )
        return loader(path, approval_seal_path=seal)

    monkeypatch.setattr(
        installation_module, "load_approved_processor_bundle_manifest", synthetic_loader
    )
    descriptor = tmp_path / "installed.json"
    descriptor.write_text(
        json.dumps(
            {
                "schema_version": "filing-xbrl-installation/v1",
                "bundle_manifest": str(manifest),
                "runtime_root": str(runtime),
                "sandbox_launcher": str(launcher),
            }
        )
    )
    return descriptor


def test_preflight_missing_installation_is_structured_and_does_not_open_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("EARNINGS_SUMMARY_FILING_XBRL_INSTALLATION", raising=False)
    monkeypatch.setenv("EARNINGS_SUMMARY_ENV_FILE", str(tmp_path / "absent.env"))

    def forbidden(*_args: object, **_kwargs: object) -> NoReturn:
        raise AssertionError("read-only preflight must not open a database or lock")

    monkeypatch.setattr(cli, "connect_sqlite", forbidden)
    monkeypatch.setattr(cli, "JobLock", forbidden)
    assert cli.main(["--preflight"]) == 3
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["reason_code"] == "installation_not_configured"
    assert receipt["native_qualification"] == "not_run"
    assert receipt["decision_grade"] is False


def test_preflight_template_is_rejected_without_native_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    template = Path(__file__).resolve().parents[1] / "config" / "filing_xbrl_processor_bundle.json"
    assert (
        cli.main(
            [
                "--preflight",
                "--bundle-manifest",
                str(template),
                "--runtime-root",
                str(tmp_path),
                "--sandbox-launcher",
                str(tmp_path / "launcher.exe"),
            ]
        )
        == 3
    )
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["reason_code"] == "bundle_template_uninstalled"


def test_configured_installation_derives_python_and_preflight_opens_no_database(
    installed_descriptor: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(installation_module.INSTALLATION_ENV, str(installed_descriptor))

    def forbidden(*_args: object, **_kwargs: object) -> NoReturn:
        raise AssertionError("preflight must not launch a child, open a database, or take a lock")

    monkeypatch.setattr(cli, "connect_sqlite", forbidden)
    monkeypatch.setattr(cli, "JobLock", forbidden)
    monkeypatch.setattr(processor, "run_capped_process", forbidden)
    resolved = resolve_processor_installation(project_root=installed_descriptor.parent)
    assert (
        resolved.bundle_python == installed_descriptor.parent / "runtime" / "Scripts" / "python.exe"
    )
    assert cli.main(["--preflight"]) == 0
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["status"] == "ready_for_native_qualification"
    assert receipt["native_qualification"] == "not_run"
    assert receipt["decision_grade"] is False


def test_preflight_rejects_changed_installed_unit_bytes(
    installed_descriptor: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    helper = installed_descriptor.parent / "runtime" / "earnings_summary_xbrl_units.py"
    helper.write_bytes(b"changed helper")
    assert cli.main(["--preflight", "--installation", str(installed_descriptor)]) == 3
    assert json.loads(capsys.readouterr().out)["status"] == "rejected"


def test_partial_override_cannot_merge_a_configured_installation(
    installed_descriptor: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(installation_module.INSTALLATION_ENV, str(installed_descriptor))
    assert cli.main(["--preflight", "--runtime-root", str(installed_descriptor.parent)]) == 3
    assert json.loads(capsys.readouterr().out)["reason_code"] == "installation_arguments_incomplete"


def test_unapproved_candidate_cannot_pass_preflight(
    installed_descriptor: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        installation_module,
        "load_approved_processor_bundle_manifest",
        processor.load_approved_processor_bundle_manifest,
    )
    assert cli.main(["--preflight", "--installation", str(installed_descriptor)]) == 3
    assert json.loads(capsys.readouterr().out)["reason_code"] == "bundle_unapproved"


def test_configured_descriptor_cannot_use_relative_paths(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    descriptor = tmp_path / "relative.json"
    descriptor.write_text(
        json.dumps(
            {
                "schema_version": "filing-xbrl-installation/v1",
                "bundle_manifest": "bundle.json",
                "runtime_root": "runtime",
                "sandbox_launcher": "launcher.exe",
            }
        )
    )
    assert cli.main(["--preflight", "--installation", str(descriptor)]) == 3
    assert json.loads(capsys.readouterr().out)["reason_code"] == "installation_evidence_invalid"


def test_preflight_rejects_descriptor_symlink(
    installed_descriptor: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    alias = installed_descriptor.parent / "alias.json"
    alias.symlink_to(installed_descriptor)
    assert cli.main(["--preflight", "--installation", str(alias)]) == 3
    assert json.loads(capsys.readouterr().out)["status"] == "rejected"


def test_preflight_rejects_host_unit_source_drift_against_seal(
    installed_descriptor: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = Path(__file__).resolve().parents[1]
    reviewed_root = installed_descriptor.parent / "reviewed-source"
    (reviewed_root / "execution").mkdir(parents=True)
    (reviewed_root / "src" / "filings").mkdir(parents=True)
    for name in ("filing_xbrl_bridge.py", "filing_xbrl_appcontainer_launcher.cs"):
        shutil.copyfile(root / "execution" / name, reviewed_root / "execution" / name)
    (reviewed_root / "src" / "filings" / "xbrl_units.py").write_bytes(b"changed host source")
    monkeypatch.setattr(processor, "_PROJECT_ROOT", reviewed_root)
    assert cli.main(["--preflight", "--installation", str(installed_descriptor)]) == 3
    assert json.loads(capsys.readouterr().out)["reason_code"] == "bundle_reviewed_source_changed"
