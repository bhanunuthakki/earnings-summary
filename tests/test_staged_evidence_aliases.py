"""Malformed staged paths still protect their files from output replacement."""

import json
from pathlib import Path

import pytest

from execution import reconcile_quality_baseline as cli


@pytest.mark.parametrize("kind", ["dotdot", "absolute", "overlong", "duplicate"])
@pytest.mark.parametrize("hardlink", [False, True])
def test_rejected_staged_path_still_protects_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str, hardlink: bool
) -> None:
    subject = tmp_path / "subject"
    subject.mkdir()
    staging = tmp_path / "staging"
    staging.mkdir()
    if kind == "overlong":
        relative = "/".join(["a" * 90] * 4) + "/input.json"
        target = staging / relative
        target.parent.mkdir(parents=True)
    else:
        target = tmp_path / "input.json"
        relative = str(target) if kind == "absolute" else "../input.json"
    target.write_bytes(b"retained input bytes")
    entry = {"path": relative, "sha256": "a" * 64}
    manifest = staging / "manifest.json"
    if kind == "duplicate":
        manifest.write_text(
            '{"architecture":'
            + json.dumps(entry)
            + ',"architecture":{"path":"other.json","sha256":"'
            + "b" * 64
            + '"}}'
        )
    else:
        manifest.write_text(json.dumps({"architecture": entry}))
    output = target
    if hardlink:
        output = tmp_path / "output.json"
        output.hardlink_to(target)

    def forbidden_reconcile(*args: object) -> None:
        raise AssertionError("output aliases must be rejected before reconciliation")

    monkeypatch.setattr(cli, "reconcile_staged_subject", forbidden_reconcile)
    assert (
        cli.main(
            [
                "--subject-root",
                str(subject),
                "--staged-manifest",
                str(manifest),
                "--output",
                str(output),
            ]
        )
        == 1
    )
    assert target.read_bytes() == b"retained input bytes"
