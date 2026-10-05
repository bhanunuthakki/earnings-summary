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


@pytest.mark.parametrize(
    "opening,closing", [("```mermaid", "```"), ("````text", "`````"), ("~~~text", "~~~~")]
)
def test_source_reference_after_fenced_block_is_checked(
    tmp_path: Path, opening: str, closing: str
) -> None:
    _fixture(tmp_path)
    (tmp_path / "src/extra.py").write_text("new_source = True\n", encoding="utf-8")
    (tmp_path / SKILL / "references/routes.md").write_text(
        f"{opening}\nExample\n{closing}\nUse `src/extra.py --db explicit`.\n",
        encoding="utf-8",
    )
    assert main(["--repo-root", str(tmp_path)]) == 2


def test_fenced_examples_do_not_create_source_or_link_references(tmp_path: Path) -> None:
    _fixture(tmp_path)
    (tmp_path / SKILL / "references/routes.md").write_text(
        "```text\n`src/example.py`\n[Example](missing.md)\n```\nUse `src/route.py`.\n",
        encoding="utf-8",
    )
    assert main(["--repo-root", str(tmp_path)]) == 0


@pytest.mark.parametrize("exists", [True, False])
def test_reference_after_multi_backtick_code_is_checked(tmp_path: Path, exists: bool) -> None:
    _fixture(tmp_path)
    if exists:
        (tmp_path / "src/extra.py").write_text("new_source = True\n")
    (tmp_path / SKILL / "references/routes.md").write_text(
        "A ``literal ` example`` then `src/extra.py --db explicit`.\n"
    )
    assert main(["--repo-root", str(tmp_path)]) == 2


def test_whitespace_code_span_does_not_crash(tmp_path: Path) -> None:
    _fixture(tmp_path)
    (tmp_path / SKILL / "references/routes.md").write_text("Whitespace ` ` then `src/route.py`.\n")
    assert main(["--repo-root", str(tmp_path)]) == 0


def test_new_source_registration_requires_review_and_is_idempotent(tmp_path: Path) -> None:
    baseline = _fixture(tmp_path)
    (tmp_path / "src/extra.py").write_text("new_source = True\n", encoding="utf-8")
    (tmp_path / SKILL / "references/routes.md").write_text("Use `src/extra.py`.\n")
    args = [
        "--repo-root",
        str(tmp_path),
        "--record-review",
        "--review-note",
        "Reviewed new route.",
        "--add-reviewed-source",
        "src/extra.py",
        "--reviewed-source",
        "src/extra.py",
    ]
    before = baseline.read_bytes()
    assert main(args[:-2]) == 2
    assert baseline.read_bytes() == before
    assert main(args) == 0
    accepted = baseline.read_bytes()
    assert main(["--repo-root", str(tmp_path), "--check"]) == 0
    assert (
        main(["--repo-root", str(tmp_path), "--record-review", "--review-note", "No change."]) == 0
    )
    assert baseline.read_bytes() == accepted


@pytest.mark.parametrize(
    "source", ["src/route.py", "../outside.py", "data/private.py", "src/extra.txt"]
)
def test_invalid_new_source_registration_preserves_baseline(tmp_path: Path, source: str) -> None:
    baseline = _fixture(tmp_path)
    if not source.startswith("../"):
        file = tmp_path / source
        file.parent.mkdir(parents=True, exist_ok=True)
        if not file.exists():
            file.write_text("example\n")
    before = baseline.read_bytes()
    assert (
        main(
            [
                "--repo-root",
                str(tmp_path),
                "--record-review",
                "--review-note",
                "Reviewed.",
                "--add-reviewed-source",
                source,
                "--reviewed-source",
                source,
            ]
        )
        == 2
    )
    assert baseline.read_bytes() == before


def test_duplicate_new_source_registration_preserves_baseline(tmp_path: Path) -> None:
    baseline = _fixture(tmp_path)
    (tmp_path / "src/extra.py").write_text("new_source = True\n")
    before = baseline.read_bytes()
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
                "--add-reviewed-source",
                "src/extra.py",
                "--reviewed-source",
                "src/extra.py",
            ]
        )
        == 2
    )
    assert baseline.read_bytes() == before


@pytest.mark.parametrize("operation", ["add", "replace"])
def test_wrong_suffix_symlink_cannot_corrupt_baseline(tmp_path: Path, operation: str) -> None:
    baseline = _fixture(tmp_path)
    (tmp_path / "src/link.txt").symlink_to(tmp_path / "src/route.py")
    before = baseline.read_bytes()
    args = ["--repo-root", str(tmp_path), "--record-review", "--review-note", "Reviewed."]
    if operation == "add":
        args.extend(["--add-reviewed-source", "src/link.txt", "--reviewed-source", "src/link.txt"])
    else:
        args.extend(
            [
                "--replace-reviewed-source",
                "src/route.py=src/link.txt",
                "--reviewed-source",
                "src/route.py",
                "--reviewed-source",
                "src/link.txt",
            ]
        )
    assert main(args) == 2
    assert baseline.read_bytes() == before


@pytest.mark.parametrize("target", [".", "src"])
def test_directory_symlink_registration_preserves_baseline(tmp_path: Path, target: str) -> None:
    baseline = _fixture(tmp_path)
    (tmp_path / "src/linked.py").symlink_to(tmp_path / target, target_is_directory=True)
    before = baseline.read_bytes()
    assert (
        main(
            [
                "--repo-root",
                str(tmp_path),
                "--record-review",
                "--review-note",
                "Reviewed.",
                "--add-reviewed-source",
                "src/linked.py",
                "--reviewed-source",
                "src/linked.py",
            ]
        )
        == 2
    )
    assert baseline.read_bytes() == before


def test_prose_change_during_review_preserves_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    baseline = _fixture(tmp_path)
    (tmp_path / "src/route.py").write_text("ROUTE = 'changed'\n")
    before = baseline.read_bytes()
    original = checker.check
    calls = 0

    def changing_check(
        root: Path,
        baseline_relative: str = BASELINE,
        replacement_sources: dict[str, str] | None = None,
    ) -> tuple[dict[str, object], dict[str, object]]:
        nonlocal calls
        calls += 1
        if calls == 3:
            (tmp_path / SKILL / "references/routes.md").write_text("Use a different policy.\n")
        return original(root, baseline_relative, replacement_sources)

    monkeypatch.setattr(checker, "check", changing_check)
    assert (
        main(
            [
                "--repo-root",
                str(tmp_path),
                "--record-review",
                "--review-note",
                "Reviewed.",
                "--reviewed-source",
                "src/route.py",
            ]
        )
        == 2
    )
    assert baseline.read_bytes() == before


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
