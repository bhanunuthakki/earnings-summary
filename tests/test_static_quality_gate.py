from __future__ import annotations

from pathlib import Path

import pytest

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
