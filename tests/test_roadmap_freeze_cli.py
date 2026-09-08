"""Exercise the freeze CLI against small, clean Git subjects and raw handoffs."""

import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from execution import freeze_quality_roadmap as cli
from quality.architecture import build_architecture_receipt
from quality.roadmap_freeze import GENERATOR_PATHS
from quality.roadmap_freeze_bundle import load_dependency_inputs, validate_freeze_index
from quality.roadmap_freeze_models import FreezeReceipt


@pytest.fixture
def freeze_subject(tmp_path: Path) -> tuple[Path, Path, Path]:
    root = tmp_path / "subject"
    root.mkdir()
    (root / ".gitignore").write_text(".tmp/\n", encoding="utf-8")
    checkout = Path(__file__).resolve().parents[1]
    for relative in GENERATOR_PATHS:
        destination = root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(checkout / relative, destination)
    (root / "src" / "large.py").write_text(
        "".join(f"VALUE_{index} = {index}\n" for index in range(1001)), encoding="utf-8"
    )
    for arguments in (
        ("init", "-q"),
        ("config", "user.email", "fixture@example.invalid"),
        ("config", "user.name", "Fixture"),
        ("add", "."),
        ("commit", "-qm", "fixture"),
    ):
        subprocess.run(["git", *arguments], cwd=root, check=True, capture_output=True)
    staging = root / ".tmp" / "inputs"
    staging.mkdir(parents=True)
    source = staging / "architecture.raw"
    source.write_text(build_architecture_receipt(root, "WORKTREE").model_dump_json())
    manifest = staging / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "architecture": {
                    "path": source.name,
                    "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                }
            }
        )
    )
    return root, manifest, source


def test_cli_retains_honest_hold_and_roundtrips_native_bindings(
    freeze_subject: tuple[Path, Path, Path],
) -> None:
    root, manifest, source = freeze_subject
    output = root / ".tmp" / "freeze.json"
    assert (
        cli.main(
            [
                "--repo-root",
                str(root),
                "--input-manifest",
                str(manifest),
                "--output",
                str(output),
            ]
        )
        == 2
    )
    raw = output.read_bytes()
    receipt = FreezeReceipt.model_validate_json(raw)
    assert receipt.artifact_status == receipt.program_status == "HOLD"
    assert receipt.coverage.observed_delivered_net_reduction is None
    assert (
        validate_freeze_index(
            root,
            raw,
            receipt.subject_commit,
            {
                "docs/quality/architecture-ratchet.json": source.read_bytes(),
            },
        )
        == receipt
    )


@pytest.mark.parametrize("target", ["manifest", "source", "plan", "owner"])
@pytest.mark.parametrize("alias", ["direct", "hardlink"])
def test_cli_rejects_output_alias_before_loading(
    freeze_subject: tuple[Path, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    target: str,
    alias: str,
) -> None:
    root, manifest, source = freeze_subject
    protected = manifest if target == "manifest" else source
    options: list[str] = []
    if target in ("plan", "owner"):
        protected = root / ".tmp" / f"{target}.json"
        protected.write_text("{}")
        options = ["--plan" if target == "plan" else "--owner-snapshot", str(protected)]
    original = protected.read_bytes()
    output = protected
    if alias == "hardlink":
        output = root / ".tmp" / "output.json"
        output.hardlink_to(protected)
    calls: list[Path] = []

    def forbidden_loader(_root: Path, path: Path) -> None:
        calls.append(path)
        raise ValueError("must reject before loading")

    monkeypatch.setattr(cli, "load_dependency_inputs", forbidden_loader)
    assert (
        cli.main(
            [
                "--repo-root",
                str(root),
                "--input-manifest",
                str(manifest),
                "--output",
                str(output),
                *options,
            ]
        )
        == 1
    )
    assert calls == []
    assert protected.read_bytes() == original


def test_dependency_loader_rejects_tampered_raw_bytes(
    freeze_subject: tuple[Path, Path, Path],
) -> None:
    root, manifest, source = freeze_subject
    source.write_bytes(source.read_bytes() + b" ")
    with pytest.raises(ValueError, match="hash mismatch"):
        load_dependency_inputs(root, manifest)


def test_dependency_loader_rejects_duplicate_manifest_keys(
    freeze_subject: tuple[Path, Path, Path],
) -> None:
    root, manifest, _source = freeze_subject
    manifest.write_text('{"architecture":{},"architecture":{}}')
    with pytest.raises(ValueError, match="duplicate JSON key"):
        load_dependency_inputs(root, manifest)


@pytest.mark.parametrize(
    "field",
    ["subject_commit", "subject_tree", "generator_sha256", "census", "source_hash", "oracle"],
)
def test_bundle_adapter_rejects_forged_index_fields(
    freeze_subject: tuple[Path, Path, Path],
    field: str,
) -> None:
    root, manifest, source = freeze_subject
    output = root / ".tmp" / "freeze.json"
    assert (
        cli.main(
            [
                "--repo-root",
                str(root),
                "--input-manifest",
                str(manifest),
                "--output",
                str(output),
            ]
        )
        == 2
    )
    original = FreezeReceipt.model_validate_json(output.read_bytes())
    value = json.loads(output.read_bytes())
    if field in ("subject_commit", "subject_tree"):
        value[field] = "f" * 40
    elif field == "generator_sha256":
        value[field] = "f" * 64
    elif field == "source_hash":
        value["evidence"][0]["sha256"] = "f" * 64
    elif field == "oracle":
        value["evidence"][0]["oracle_status"] = "HOLD"
    else:
        value["candidate_census"][0]["source_identity"] = "src/invented.py"
    with pytest.raises(ValueError):
        validate_freeze_index(
            root,
            json.dumps(value).encode(),
            original.subject_commit,
            {
                "docs/quality/architecture-ratchet.json": source.read_bytes(),
            },
        )
