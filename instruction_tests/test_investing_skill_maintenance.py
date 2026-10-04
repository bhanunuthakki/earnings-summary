"""Read-only maintenance checks, isolated from application state and providers."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import check_investing_skill as checker
from scripts.check_investing_skill import BASELINE, SKILL, main


def _fixture(root: Path) -> Path:
    source = root / "src/route.py"
    source.parent.mkdir(parents=True)
    source.write_text("ROUTE = 'portfolio'\n", encoding="utf-8")
    skill = root / SKILL
    (skill / "references").mkdir(parents=True)
    (skill / "SKILL.md").write_text("Read [routes](references/routes.md).\n", encoding="utf-8")
    (skill / "references/routes.md").write_text("Use the approved route.\n", encoding="utf-8")
    baseline = root / BASELINE
    baseline.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "review_note": "Reviewed task scope and route semantics.",
                "sources": [
                    {
                        "path": "src/route.py",
                        "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return baseline


def test_clean_check_and_record_are_idempotent(tmp_path: Path) -> None:
    baseline = _fixture(tmp_path)
    before = baseline.read_bytes()
    args = ["--repo-root", str(tmp_path)]
    assert main(args) == 0
    assert main(args) == 0
    assert main([*args, "--record-review", "--review-note", "Reviewed; no change."]) == 0
    assert baseline.read_bytes() == before


def test_existing_source_semantic_change_is_drift(tmp_path: Path) -> None:
    baseline = _fixture(tmp_path)
    before = baseline.read_bytes()
    (tmp_path / "src/route.py").write_text("ROUTE = 'all_accounts'\n", encoding="utf-8")
    assert main(["--repo-root", str(tmp_path)]) == 1
    assert baseline.read_bytes() == before


def test_record_requires_explicit_review_of_every_changed_source(tmp_path: Path) -> None:
    baseline = _fixture(tmp_path)
    before = baseline.read_bytes()
    (tmp_path / "src/route.py").write_text("ROUTE = 'all_accounts'\n", encoding="utf-8")
    args = [
        "--repo-root",
        str(tmp_path),
        "--record-review",
        "--review-note",
        "Reviewed route semantics.",
    ]
    assert main(args) == 2
    assert baseline.read_bytes() == before
    assert main([*args, "--reviewed-source", "src/route.py"]) == 0
    assert main(["--repo-root", str(tmp_path)]) == 0


def test_removed_source_is_invalid_and_cannot_be_reviewed_away(tmp_path: Path) -> None:
    baseline = _fixture(tmp_path)
    before = baseline.read_bytes()
    (tmp_path / "src/route.py").unlink()
    args = ["--repo-root", str(tmp_path)]
    assert main(args) == 2
    assert main([*args, "--record-review", "--review-note", "Reviewed removal."]) == 2
    assert baseline.read_bytes() == before


def test_manifest_path_escape_is_invalid(tmp_path: Path) -> None:
    baseline = _fixture(tmp_path)
    baseline.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "review_note": "Reviewed.",
                "sources": [{"path": "../outside", "sha256": "0" * 64}],
            }
        ),
        encoding="utf-8",
    )
    assert main(["--repo-root", str(tmp_path)]) == 2


def test_symlink_escape_is_invalid(tmp_path: Path) -> None:
    _fixture(tmp_path)
    source = tmp_path / "src/route.py"
    source.unlink()
    source.symlink_to(tmp_path.parent / "outside.py")
    assert main(["--repo-root", str(tmp_path)]) == 2


def test_reviewed_replacement_requires_fixed_references_and_both_paths(tmp_path: Path) -> None:
    baseline = _fixture(tmp_path)
    old = tmp_path / "src/route.py"
    new = tmp_path / "src/new_route.py"
    old.rename(new)
    routes = tmp_path / SKILL / "references/routes.md"
    routes.write_text("Use `src/route.py`.\n", encoding="utf-8")
    args = [
        "--repo-root",
        str(tmp_path),
        "--record-review",
        "--review-note",
        "Reviewed replacement route.",
        "--replace-reviewed-source",
        "src/route.py=src/new_route.py",
        "--reviewed-source",
        "src/route.py",
        "--reviewed-source",
        "src/new_route.py",
    ]
    before = baseline.read_bytes()
    assert main(args) == 2
    assert baseline.read_bytes() == before
    routes.write_text("Use `src/new_route.py`.\n", encoding="utf-8")
    assert main(args[:-2]) == 2
    assert main(args) == 0
    assert main(["--repo-root", str(tmp_path), "--check"]) == 0


def test_new_unreviewed_skill_source_fails_closed(tmp_path: Path) -> None:
    _fixture(tmp_path)
    (tmp_path / "src/extra.py").write_text("new_source = True\n", encoding="utf-8")
    (tmp_path / SKILL / "references/routes.md").write_text(
        "Use `src/extra.py`.\n", encoding="utf-8"
    )
    assert main(["--repo-root", str(tmp_path)]) == 2


def test_broken_and_escaping_skill_links_are_invalid(tmp_path: Path) -> None:
    _fixture(tmp_path)
    skill = tmp_path / SKILL / "SKILL.md"
    skill.write_text("Read [missing](references/missing.md).\n", encoding="utf-8")
    assert main(["--repo-root", str(tmp_path)]) == 2
    skill.write_text("Read [outside](../../../../../../outside.md).\n", encoding="utf-8")
    assert main(["--repo-root", str(tmp_path)]) == 2


def test_json_report_has_distinct_status_and_no_source_contents(tmp_path: Path) -> None:
    # Subprocess output proves the public CLI contract without loading app fixtures.
    _fixture(tmp_path)
    script = Path(__file__).resolve().parents[1] / "scripts/check_investing_skill.py"
    result = subprocess.run(
        [sys.executable, str(script), "--repo-root", str(tmp_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["status"] == "clean"
    assert payload["changed_sources"] == []
    assert "ROUTE" not in result.stdout


def test_cli_addition_requires_explicit_review_and_preserves_existing_sources(
    tmp_path: Path,
) -> None:
    baseline = _fixture(tmp_path)
    before = baseline.read_bytes()
    source_paths = ("src/extra.py", "execution/plan.py")
    for relative in source_paths:
        source = tmp_path / relative
        source.parent.mkdir(exist_ok=True)
        source.write_text("NEW_ROUTE = True\n", encoding="utf-8")
    (tmp_path / "src/route.py").write_text("ROUTE = 'all_accounts'\n", encoding="utf-8")
    (tmp_path / SKILL / "references/routes.md").write_text(
        "Use `src/extra.py` and `execution/plan.py`.\n", encoding="utf-8"
    )
    script = Path(__file__).resolve().parents[1] / "scripts/check_investing_skill.py"
    cli = [sys.executable, str(script), "--repo-root", str(tmp_path)]
    assert subprocess.run(cli, capture_output=True, check=False).returncode == 2
    assert baseline.read_bytes() == before
    args = [*cli, "--record-review", "--review-note", "Reviewed new routes and old change."]
    for relative in source_paths:
        args.extend(("--add-reviewed-source", relative))
    # New and previously changed paths each require the reviewer's exact name.
    assert subprocess.run(args, capture_output=True, check=False).returncode == 2
    assert baseline.read_bytes() == before
    for relative in source_paths:
        args.extend(("--reviewed-source", relative))
    assert subprocess.run(args, capture_output=True, check=False).returncode == 2
    assert baseline.read_bytes() == before
    args.extend(("--reviewed-source", "src/route.py"))
    result = subprocess.run(args, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["status"] == "clean"
    recorded = json.loads(baseline.read_text())
    assert {entry["path"]: entry["sha256"] for entry in recorded["sources"]} == {
        relative: hashlib.sha256((tmp_path / relative).read_bytes()).hexdigest()
        for relative in (*source_paths, "src/route.py")
    }
    assert subprocess.run(cli, capture_output=True, check=False).returncode == 0
    unchanged = baseline.read_bytes()
    # Already listed sources cannot be added again or silently reclassified.
    assert subprocess.run(args, capture_output=True, check=False).returncode == 2
    assert baseline.read_bytes() == unchanged


@pytest.mark.parametrize(
    "additions",
    [
        ("src/extra.py", "src/extra.py"),
        ("src/route.py",),
        ("src/missing.py",),
        (".tmp/private.py",),
        ("micro_thesis/owner.json",),
        ("src/extra.txt",),
        ("../outside.py",),
    ],
)
def test_addition_rejects_duplicate_missing_private_and_unsafe_sources(
    tmp_path: Path, additions: tuple[str, ...]
) -> None:
    baseline = _fixture(tmp_path)
    for relative in ("src/extra.py", ".tmp/private.py", "micro_thesis/owner.json", "src/extra.txt"):
        source = tmp_path / relative
        source.parent.mkdir(exist_ok=True)
        source.write_text("synthetic source\n", encoding="utf-8")
    before = baseline.read_bytes()
    args = ["--repo-root", str(tmp_path), "--record-review", "--review-note", "Reviewed."]
    for relative in additions:
        args.extend(("--add-reviewed-source", relative, "--reviewed-source", relative))
    assert main(args) == 2
    assert baseline.read_bytes() == before


def test_addition_is_record_only_and_does_not_require_a_direct_prose_reference(
    tmp_path: Path,
) -> None:
    baseline = _fixture(tmp_path)
    (tmp_path / "src/extra.py").write_text("dependency = True\n", encoding="utf-8")
    before = baseline.read_bytes()
    args = ["--repo-root", str(tmp_path), "--add-reviewed-source", "src/extra.py"]
    assert main(args) == 2
    assert baseline.read_bytes() == before
    assert (
        main(
            [
                *args,
                "--record-review",
                "--review-note",
                "Reviewed dependency.",
                "--reviewed-source",
                "src/extra.py",
            ]
        )
        == 0
    )


def test_added_source_does_not_waive_a_missing_original_source(tmp_path: Path) -> None:
    baseline = _fixture(tmp_path)
    (tmp_path / "src/route.py").unlink()
    (tmp_path / "src/extra.py").write_text("dependency = True\n", encoding="utf-8")
    before = baseline.read_bytes()
    assert (
        main(
            [
                "--repo-root",
                str(tmp_path),
                "--record-review",
                "--review-note",
                "Reviewed addition.",
                "--add-reviewed-source",
                "src/extra.py",
                "--reviewed-source",
                "src/extra.py",
            ]
        )
        == 2
    )
    assert baseline.read_bytes() == before


def test_added_source_change_during_recording_refuses_baseline_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    baseline = _fixture(tmp_path)
    source = tmp_path / "src/extra.py"
    source.write_text("dependency = True\n", encoding="utf-8")
    before = baseline.read_bytes()
    real_check = checker.check
    mutated = False

    def concurrent_change(
        root: Path,
        baseline_relative: str = BASELINE,
        replacement_sources: dict[str, str] | None = None,
    ) -> tuple[dict[str, object], dict[str, object]]:
        nonlocal mutated
        result = real_check(root, baseline_relative, replacement_sources)
        if replacement_sources is not None and not mutated:
            source.write_text("dependency = 'changed during review'\n", encoding="utf-8")
            mutated = True
        return result

    monkeypatch.setattr(checker, "check", concurrent_change)
    assert (
        main(
            [
                "--repo-root",
                str(tmp_path),
                "--record-review",
                "--review-note",
                "Reviewed.",
                "--add-reviewed-source",
                "src/extra.py",
                "--reviewed-source",
                "src/extra.py",
            ]
        )
        == 2
    )
    assert mutated
    assert baseline.read_bytes() == before
