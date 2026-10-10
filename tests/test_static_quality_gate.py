from __future__ import annotations

import hashlib
import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

from quality import static_quality_gate
from quality.static_quality_gate import (
    StaticQualityGateError,
    compare_exact,
    failure_diagnostics_receipt,
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


def test_local_gate_includes_new_retained_sources_without_staging(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    source = tmp_path / "src" / "existing.py"
    source.parent.mkdir()
    source.write_text("value = 1\n", encoding="utf-8")
    config = tmp_path / "ceilings.json"
    config.write_text(
        json.dumps(
            {
                "schema_version": "bha-105.v1",
                "pyright_diagnostics": {"src": 0},
                "suppressions": {"src": 0},
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / ".gitignore").write_text("src/ignored.py\n", encoding="utf-8")
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
    (source.parent / "new.py").write_text("value = 2\n", encoding="utf-8")
    (source.parent / "ignored.py").write_text("value = 3\n", encoding="utf-8")
    excluded = tmp_path / "alembic" / "versions" / "new.py"
    excluded.parent.mkdir(parents=True)
    excluded.write_text("value = 4\n", encoding="utf-8")
    receipt = tmp_path / "pyright.json"
    receipt.write_text(
        json.dumps({"summary": {"filesAnalyzed": 2}, "generalDiagnostics": []}),
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
        == 0
    )
    assert "2 retained files" in capsys.readouterr().out
    index = subprocess.run(
        ["git", "ls-files", "--", "src/new.py"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
    assert index.stdout == ""


def _diagnostic(path: str, severity: str = "error") -> dict[str, object]:
    return {
        "file": path,
        "severity": severity,
        "rule": "reportPRIVATE_SENTINEL",
        "range": {"start": {"line": 1, "character": 2}, "end": {"line": 1, "character": 4}},
        "message": "PRIVATE_SENTINEL https://example.invalid/?token=PRIVATE_SENTINEL",
    }


@pytest.fixture
def diagnostic_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, dict[str, object]]:
    source = tmp_path / "src" / "db.py"
    source.parent.mkdir()
    source.write_text("value = 1\n", encoding="utf-8")
    config = tmp_path / "ceilings.json"
    config.write_text("{}", encoding="utf-8")
    payload: dict[str, object] = {
        "summary": {"filesAnalyzed": 1},
        "generalDiagnostics": [_diagnostic(str(source))],
    }

    def retained(root: Path) -> list[str]:
        return ["src/db.py"]

    def ceilings(path: Path) -> tuple[dict[str, int], dict[str, int]]:
        return {"src": 0}, {"src": 0}

    def base_ceilings(
        root: Path, config_path: Path, base: str
    ) -> tuple[dict[str, int], dict[str, int]]:
        return {"src": 0}, {"src": 0}

    def collect(root: Path, pythonpath: str, retained: list[str]) -> tuple[object, dict[str, str]]:
        return payload, {}

    monkeypatch.setattr(static_quality_gate, "_retained_python_files", retained)
    monkeypatch.setattr(static_quality_gate, "load_ceilings", ceilings)
    monkeypatch.setattr(static_quality_gate, "load_base_ceilings", base_ceilings)
    monkeypatch.setattr(static_quality_gate, "_run_pyright", collect)
    return tmp_path, payload


def test_failure_receipt_is_private_complete_and_replayable(tmp_path: Path) -> None:
    copied = "quality_pyright_PRIVATE_SENTINEL/scripts/ci_gate.py"
    rows = [
        _diagnostic(str(tmp_path / copied), severity)
        for severity in ("error", "warning", "information")
    ]
    rows.append(rows[0].copy())
    payload = {"summary": {"filesAnalyzed": 1}, "generalDiagnostics": rows}
    config = tmp_path / "ceilings.json"
    config.write_text("PRIVATE_SENTINEL", encoding="utf-8")
    receipt = failure_diagnostics_receipt(
        tmp_path,
        payload,
        [".github/scripts/ci_gate.py"],
        {copied: ".github/scripts/ci_gate.py"},
        config,
    )
    serialized = json.dumps(receipt)
    assert "PRIVATE_SENTINEL" not in serialized
    assert str(tmp_path) not in serialized
    assert "example.invalid" not in serialized
    assert parse_pyright_payload(tmp_path, receipt, [".github/scripts/ci_gate.py"]) == {
        ".github": 4
    }
    expected = {
        "file": ".github/scripts/ci_gate.py",
        "severity": "error",
        "rule": "hashed",
        "rule_sha256": hashlib.sha256(b"reportPRIVATE_SENTINEL").hexdigest(),
        "range": {"start": {"line": 1, "character": 2}, "end": {"line": 1, "character": 4}},
        "message_sha256": hashlib.sha256(str(rows[0]["message"]).encode()).hexdigest(),
    }
    assert receipt["generalDiagnostics"] == [
        expected,
        {**expected, "severity": "warning"},
        {**expected, "severity": "information"},
        expected,
    ]


def test_receipt_accepts_optional_rule_and_range(tmp_path: Path) -> None:
    row = _diagnostic("src/db.py")
    row.pop("rule")
    row.pop("range")
    config = tmp_path / "ceilings.json"
    config.write_text("{}", encoding="utf-8")
    receipt = failure_diagnostics_receipt(
        tmp_path,
        {"summary": {"filesAnalyzed": 1}, "generalDiagnostics": [row]},
        ["src/db.py"],
        {},
        config,
    )
    assert receipt["generalDiagnostics"] == [
        {
            "file": "src/db.py",
            "severity": "error",
            "rule": None,
            "rule_sha256": None,
            "range": None,
            "message_sha256": hashlib.sha256(str(row["message"]).encode()).hexdigest(),
        }
    ]


@pytest.mark.parametrize(
    "field,value",
    [
        ("severity", "PRIVATE_SENTINEL"),
        ("rule", 42),
        ("range", {"start": {"line": True, "character": 0}, "end": {"line": 0, "character": 0}}),
        ("message", None),
    ],
)
def test_bad_receipt_fields_preserve_failure_without_payload(
    diagnostic_gate: tuple[Path, dict[str, object]],
    capsys: pytest.CaptureFixture[str],
    field: str,
    value: object,
) -> None:
    root, payload = diagnostic_gate
    row = _diagnostic(str(root / "src/db.py"))
    row[field] = value
    payload["generalDiagnostics"] = [row]
    assert (
        static_quality_gate.main(
            ["--repo-root", str(root), "--config", "ceilings.json", "--failure-diagnostics"]
        )
        == 1
    )
    output = capsys.readouterr()
    assert "static-quality diagnostics unavailable" in output.err
    assert "PRIVATE_SENTINEL" not in output.err + output.out
    assert "pyright diagnostics increased" in output.err


def test_failure_flag_emits_receipt_and_replays_same_failure(
    diagnostic_gate: tuple[Path, dict[str, object]], capsys: pytest.CaptureFixture[str]
) -> None:
    root, _ = diagnostic_gate
    args = ["--repo-root", str(root), "--config", "ceilings.json"]
    assert static_quality_gate.main([*args, "--failure-diagnostics"]) == 1
    output = capsys.readouterr()
    prefix = "static-quality diagnostics: BEGIN\n"
    serialized = output.err.split(prefix, 1)[1].split("\nstatic-quality diagnostics: END", 1)[0]
    receipt = root / "safe.json"
    receipt.write_text(serialized, encoding="utf-8")
    assert "PRIVATE_SENTINEL" not in output.err + output.out
    assert str(root) not in output.err + output.out
    assert static_quality_gate.main([*args, "--pyright-json", str(receipt)]) == 1
    assert capsys.readouterr().err == "src: pyright diagnostics increased from 0 to 1\n"


def test_success_and_disabled_flag_do_not_emit_receipt(
    diagnostic_gate: tuple[Path, dict[str, object]], capsys: pytest.CaptureFixture[str]
) -> None:
    root, payload = diagnostic_gate
    args = ["--repo-root", str(root), "--config", "ceilings.json"]
    assert static_quality_gate.main(args) == 1
    assert capsys.readouterr().err == "src: pyright diagnostics increased from 0 to 1\n"
    payload["generalDiagnostics"] = []
    assert static_quality_gate.main([*args, "--failure-diagnostics"]) == 0
    output = capsys.readouterr()
    assert output.err == ""
    assert "static-quality ceilings exact" in output.out


def test_incomplete_population_keeps_exit_two_and_no_receipt(
    diagnostic_gate: tuple[Path, dict[str, object]], capsys: pytest.CaptureFixture[str]
) -> None:
    root, payload = diagnostic_gate
    payload["summary"] = {"filesAnalyzed": 0}
    assert (
        static_quality_gate.main(
            ["--repo-root", str(root), "--config", "ceilings.json", "--failure-diagnostics"]
        )
        == 2
    )
    output = capsys.readouterr()
    assert "static-quality gate failed closed: evidence unavailable" in output.err
    assert "static-quality diagnostics unavailable" in output.err
    assert "static-quality diagnostics: " not in output.err
    assert "PRIVATE_SENTINEL" not in output.err + output.out


@pytest.mark.parametrize("kind", ["summary", "path"])
def test_malformed_population_does_not_echo_untrusted_values(
    diagnostic_gate: tuple[Path, dict[str, object]],
    capsys: pytest.CaptureFixture[str],
    kind: str,
) -> None:
    root, payload = diagnostic_gate
    if kind == "summary":
        payload["summary"] = {"filesAnalyzed": "PRIVATE_SENTINEL"}
    else:
        payload["generalDiagnostics"] = [_diagnostic(str(root / "PRIVATE_SENTINEL.py"))]
    assert (
        static_quality_gate.main(
            ["--repo-root", str(root), "--config", "ceilings.json", "--failure-diagnostics"]
        )
        == 2
    )
    output = capsys.readouterr()
    assert (
        output.err
        == "static-quality gate failed closed: evidence unavailable\nstatic-quality diagnostics unavailable\n"
    )
    assert "PRIVATE_SENTINEL" not in output.err + output.out


@pytest.mark.parametrize(
    "stderr,expected",
    [
        (
            "FATAL ERROR: Reached heap limit Allocation failed - JavaScript heap out of memory",
            "node_heap_exhaustion_marker",
        ),
        (
            "FATAL ERROR: Ineffective mark-compacts near heap limit Allocation failed - JavaScript heap out of memory",
            "node_heap_exhaustion_marker",
        ),
        (
            "PRIVATE_SENTINEL: FATAL ERROR: Reached heap limit Allocation failed - JavaScript heap out of memory",
            "unclassified",
        ),
        (
            "FATAL ERROR: Reached heap limit Allocation failed - JavaScript heap out of memory PRIVATE_SENTINEL",
            "unclassified",
        ),
        ("PRIVATE_SENTINEL", "unclassified"),
    ],
)
def test_process_classifier_retains_only_exact_marker_observations(
    stderr: str, expected: str
) -> None:
    error = static_quality_gate.PyrightProcessError("main", 250, stderr)
    assert error.reason == expected
    assert str(error) == "Pyright failed before reporting diagnostics (250)"
    assert "PRIVATE_SENTINEL" not in repr(vars(error))


@pytest.mark.parametrize("hidden", [False, True])
def test_pyright_process_stage_and_hidden_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    hidden: bool,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = tmp_path / ".github" / "ci.py"
    source.parent.mkdir()
    source.write_text("value = 1\n", encoding="utf-8")
    config = tmp_path / "ceilings.json"
    config.write_text(
        json.dumps(
            {
                "schema_version": "bha-105.v1",
                "pyright_diagnostics": {".github": 0},
                "suppressions": {".github": 0},
            }
        ),
        encoding="utf-8",
    )
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
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
    original_run = subprocess.run
    calls = 0

    def run(
        args: list[str],
        *,
        cwd: Path,
        text: bool,
        capture_output: bool,
        check: bool,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        nonlocal calls
        if Path(args[0]).name != "pyright":
            return original_run(args, cwd=cwd, text=True, capture_output=True, check=False, env=env)
        calls += 1
        if hidden and calls == 1:
            return subprocess.CompletedProcess(
                [], 0, '{"generalDiagnostics":[],"summary":{"filesAnalyzed":0}}', ""
            )
        return subprocess.CompletedProcess([], 250, "PRIVATE_SENTINEL", "PRIVATE_SENTINEL")

    monkeypatch.setattr(static_quality_gate.subprocess, "run", run)
    assert (
        static_quality_gate.main(
            [
                "--repo-root",
                str(tmp_path),
                "--config",
                "ceilings.json",
                "--base",
                "HEAD",
                "--failure-diagnostics",
            ]
        )
        == 2
    )
    output = capsys.readouterr()
    stage = "hidden" if hidden else "main"
    assert f'"stage": "{stage}", "returncode": 250, "reason": "unclassified"' in output.err
    assert "PRIVATE_SENTINEL" not in output.err + output.out
    assert calls == (2 if hidden else 1)
    assert not list(tmp_path.glob("quality_pyright_*"))


def test_process_failure_opt_in_output_is_private_and_keeps_exit_two(
    diagnostic_gate: tuple[Path, dict[str, object]],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root, _ = diagnostic_gate

    def collect(*args: object) -> tuple[object, dict[str, str]]:
        raise static_quality_gate.PyrightProcessError("main", 250, "PRIVATE_SENTINEL " + str(root))

    monkeypatch.setattr(static_quality_gate, "_run_pyright", collect)
    args = ["--repo-root", str(root), "--config", "ceilings.json"]
    assert static_quality_gate.main(args) == 2
    assert (
        capsys.readouterr().err
        == "static-quality gate failed closed: Pyright failed before reporting diagnostics (250)\n"
    )
    assert static_quality_gate.main([*args, "--failure-diagnostics"]) == 2
    output = capsys.readouterr()
    assert '"stage": "main", "returncode": 250, "reason": "unclassified"' in output.err
    assert "static-quality diagnostics unavailable" in output.err
    assert "PRIVATE_SENTINEL" not in output.err + output.out
    assert str(root) not in output.err + output.out


def test_receipt_failure_preserves_gate_result(
    diagnostic_gate: tuple[Path, dict[str, object]],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root, _ = diagnostic_gate

    def unavailable(*args: object) -> dict[str, object]:
        raise OSError("PRIVATE_SENTINEL")

    monkeypatch.setattr(static_quality_gate, "failure_diagnostics_receipt", unavailable)
    assert (
        static_quality_gate.main(
            ["--repo-root", str(root), "--config", "ceilings.json", "--failure-diagnostics"]
        )
        == 1
    )
    output = capsys.readouterr()
    assert "static-quality diagnostics unavailable" in output.err
    assert "PRIVATE_SENTINEL" not in output.err + output.out


def test_failed_diagnostic_stream_preserves_gate_result(
    diagnostic_gate: tuple[Path, dict[str, object]], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _ = diagnostic_gate

    class DiagnosticStream(io.StringIO):
        def write(self, value: str) -> int:
            if value.startswith("static-quality diagnostics"):
                raise OSError("PRIVATE_SENTINEL")
            return super().write(value)

    stream = DiagnosticStream()
    monkeypatch.setattr(sys, "stderr", stream)
    assert (
        static_quality_gate.main(
            ["--repo-root", str(root), "--config", "ceilings.json", "--failure-diagnostics"]
        )
        == 1
    )
    assert stream.getvalue() == "src: pyright diagnostics increased from 0 to 1\n"
