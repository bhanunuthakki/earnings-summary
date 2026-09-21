from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from quality import static_quality_gate
from quality.static_quality_gate import (
    StaticQualityGateError,
    compare_exact,
    parse_pyright_payload,
    subsystem,
)


def test_subsystem_keeps_src_packages_separate() -> None:
    assert subsystem("src/provenance/store.py") == "src/provenance"
    assert subsystem("src/db.py") == "src"
    assert subsystem("tests/test_db.py") == "tests"
    assert subsystem(".github/scripts/ci_gate.py") == ".github"


def test_exact_ceilings_pass() -> None:
    retained = ["src/db.py", "tests/test_db.py"]
    assert (
        compare_exact(
            retained,
            {"src": 2, "tests": 3},
            {"src": 0, "tests": 1},
            {"src": 2, "tests": 3},
            {"tests": 1},
        )
        == []
    )


def test_ceiling_increase_and_decrease_both_require_a_change() -> None:
    retained = ["src/db.py"]
    violations = compare_exact(
        retained,
        {"src": 2},
        {"src": 1},
        {"src": 3},
        {},
    )
    assert violations == [
        "src: pyright diagnostics increased from 2 to 3",
        "src: lower suppressions ceiling from 1 to 0",
    ]


def test_ceiling_keys_must_exactly_partition_active_subsystems() -> None:
    violations = compare_exact(
        ["src/db.py", "cron/job.py"],
        {"src": 0, "stale": 0},
        {"src": 0},
        {},
        {},
    )
    assert violations[:3] == [
        "pyright ceilings missing subsystem(s): cron",
        "pyright ceilings contain stale subsystem(s): stale",
        "suppressions ceilings missing subsystem(s): cron",
    ]


def test_parse_pyright_payload_requires_complete_retained_population(tmp_path: Path) -> None:
    payload: object = {"summary": {"filesAnalyzed": 0}, "generalDiagnostics": []}
    with pytest.raises(StaticQualityGateError, match="expected all 1 retained files"):
        parse_pyright_payload(tmp_path, payload, ["src/db.py"])


def test_parse_pyright_payload_rejects_non_retained_diagnostic(tmp_path: Path) -> None:
    payload: object = {
        "summary": {"filesAnalyzed": 1},
        "generalDiagnostics": [{"file": str(tmp_path / "scratch" / "draft.py")}],
    }
    with pytest.raises(StaticQualityGateError, match="non-retained"):
        parse_pyright_payload(tmp_path, payload, ["src/db.py"])


def test_parse_pyright_payload_maps_hidden_file_copy(tmp_path: Path) -> None:
    copied = "quality_pyright_123/scripts/ci_gate.py"
    payload: object = {
        "summary": {"filesAnalyzed": 1},
        "generalDiagnostics": [{"file": str(tmp_path / copied)}],
    }
    assert parse_pyright_payload(
        tmp_path,
        payload,
        [".github/scripts/ci_gate.py"],
        {copied: ".github/scripts/ci_gate.py"},
    ) == {".github": 1}


@pytest.mark.parametrize("kind", ["pyright diagnostics", "suppressions"])
def test_raising_a_matching_ceiling_cannot_erase_a_regression(kind: str) -> None:
    assert static_quality_gate.compare_descending(kind, {"src": 2}, {"src": 3}) == [
        f"src: {kind} ceiling increased from 2 to 3"
    ]


def test_new_subsystems_start_without_debt() -> None:
    assert static_quality_gate.compare_descending(
        "suppressions", {"src": 2}, {"src": 1, "cron": 1}
    ) == ["cron: suppressions ceiling increased from 0 to 1"]


def test_descending_ceilings_allow_cleanup_and_retirement() -> None:
    assert (
        static_quality_gate.compare_descending(
            "pyright diagnostics", {"src": 2, "retired": 3}, {"src": 1, "cron": 0}
        )
        == []
    )


def test_cli_rejects_raised_budget_even_when_diagnostics_match(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    source = tmp_path / "src" / "db.py"
    source.parent.mkdir()
    source.write_text("value = 1\n", encoding="utf-8")
    config = tmp_path / "ceilings.json"
    limits = {
        "schema_version": "bha-105.v1",
        "pyright_diagnostics": {"src": 0},
        "suppressions": {"src": 0},
    }
    config.write_text(json.dumps(limits), encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-qm",
            "baseline",
        ],
        cwd=tmp_path,
        check=True,
    )
    limits["pyright_diagnostics"] = {"src": 1}
    config.write_text(json.dumps(limits), encoding="utf-8")
    receipt = tmp_path / "pyright.json"
    receipt.write_text(
        json.dumps(
            {
                "summary": {"filesAnalyzed": 1},
                "generalDiagnostics": [{"file": str(source)}],
            }
        ),
        encoding="utf-8",
    )
    assert (
        static_quality_gate.main(
            [
                "--repo-root",
                str(tmp_path),
                "--config",
                str(config),
                "--base",
                "HEAD",
                "--pyright-json",
                str(receipt),
            ]
        )
        == 1
    )
    assert "ceiling increased from 0 to 1" in capsys.readouterr().err


def test_base_limits_fail_closed_without_a_valid_comparison(tmp_path: Path) -> None:
    with pytest.raises(StaticQualityGateError, match="base revision is invalid"):
        static_quality_gate.load_base_ceilings(tmp_path, tmp_path / "config.json", "--help")
    with pytest.raises(StaticQualityGateError, match="comparison base"):
        static_quality_gate.load_base_ceilings(tmp_path, tmp_path / "config.json", "missing")
