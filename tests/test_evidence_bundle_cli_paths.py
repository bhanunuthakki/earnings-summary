"""Reject an escaping default manifest before reading untrusted file bytes."""

from pathlib import Path

import pytest

from execution import collect_evidence_bundle as cli
from quality.evidence_bundle_models import CollectionManifest


@pytest.mark.parametrize("mode", ["assemble", "validate", "record"])
@pytest.mark.parametrize("escape", ["outside", "dotdot", "symlink"])
def test_default_manifest_escape_is_rejected_before_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str, escape: str
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    if escape == "outside":
        staging = outside
    elif escape == "dotdot":
        staging = root / ".tmp" / ".." / ".." / "outside"
    else:
        (root / ".tmp").mkdir()
        staging = root / ".tmp" / "alias"
        staging.symlink_to(outside, target_is_directory=True)
    reads: list[Path] = []

    def forbidden_read(path: Path) -> CollectionManifest:
        reads.append(path)
        raise ValueError("loader was reached")

    monkeypatch.setattr(cli, "load_collection_manifest", forbidden_read)
    result = cli.main(
        [
            "--mode",
            mode,
            "--repo-root",
            str(root),
            "--staging-dir",
            str(staging),
            "--subject",
            "a" * 40,
            "--bundle",
            "b" * 40,
        ]
    )
    assert result == 2
    assert reads == []


def test_default_staging_uses_selected_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reads: list[Path] = []

    def capture_read(path: Path) -> CollectionManifest:
        reads.append(path)
        raise ValueError("test stops before loading")

    monkeypatch.setattr(cli, "load_collection_manifest", capture_read)
    assert cli.main(["--mode", "assemble", "--repo-root", str(tmp_path)]) == 2
    assert reads == [tmp_path / ".tmp" / "quality" / "evidence-bundle" / "manifest.json"]
