"""Adversarial contract tests for raw performance timing."""

from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

import pytest

from quality.git_env import clean_local_git_env
from quality.performance import (
    ADMISSION_HOLD_REASON,
    PerformanceExecutionError,
    PerformanceIdentityError,
    PerformanceInputError,
    PerformanceOutputError,
    benchmark_environment,
    capture_performance_baseline,
)
from quality.performance_models import CompanionMeasures
from quality.performance_support import (
    bootstrap_ci_95,
    declared_config_hash,
    describe_samples,
    scanner_identity,
    source_identity,
    source_revision,
    tracked_python_source_hash,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
HERMETIC = f"{sys.executable} -c 'print(\"ok\")'"


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        check=True,
        env=clean_local_git_env(),
    )


def _fixture_repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "execution").mkdir()
    (root / "src" / "a.py").write_text("A = 1\n", encoding="utf-8")
    (root / "execution" / "b.py").write_text("B = 1\n", encoding="utf-8")
    (root / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    (root / "requirements.lock").write_text("", encoding="utf-8")
    for name in (
        "src/log_redact.py",
        "src/quality/git_env.py",
        "src/quality/performance.py",
        "src/quality/performance_models.py",
        "src/quality/performance_support.py",
        "src/runtime/python_process.py",
        "execution/capture_performance_baseline.py",
    ):
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(name, encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@example.invalid")
    _git(root, "config", "user.name", "Test")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "fixture")
    return root


def test_successful_collection_is_always_admission_hold() -> None:
    receipt = capture_performance_baseline(REPO_ROOT, HERMETIC, samples=7)
    assert receipt.collection_status == "COMPLETE"
    assert receipt.admission_status == "HOLD"
    assert receipt.status == "HOLD"
    assert receipt.hold is True
    assert receipt.hold_reasons[0] == ADMISSION_HOLD_REASON
    assert receipt.timing.count == 7
    assert receipt.exit_codes == [0] * 8
    assert receipt.revision and len(receipt.revision) == 40
    assert receipt.source_identity in {"clean_head", "working_tree"}
    assert all(
        len(value) == 64
        for value in (
            receipt.source_sha256,
            receipt.config_sha256,
            receipt.scanner_sha256,
        )
    )
    assert receipt.warmup_seconds > 0
    assert receipt.environment["python"]


def test_measured_samples_are_ordinal_and_warmup_is_separate() -> None:
    receipt = capture_performance_baseline(REPO_ROOT, HERMETIC, samples=7)
    assert [sample.ordinal for sample in receipt.timing_samples] == list(range(1, 8))
    assert {sample.label for sample in receipt.timing_samples} == {"measured"}
    assert receipt.timing.samples == [sample.elapsed_seconds for sample in receipt.timing_samples]
    assert len(receipt.exit_codes) == len(receipt.timing_samples) + 1


def test_statistics_are_deterministic() -> None:
    samples = [0.10, 0.11, 0.09, 0.12, 0.10, 0.11, 0.10]
    first = describe_samples(samples)
    assert first == describe_samples(samples)
    median, mad, interval, verdict = first
    assert median is not None
    assert median == pytest.approx(0.10)
    assert mad is not None and mad >= 0
    assert interval is not None and interval[0] <= median <= interval[1]
    assert verdict in {"stable", "unstable"}
    assert bootstrap_ci_95([0.5]) is None
    assert describe_samples([0.1, 0.2])[3] == "insufficient"


def test_git_subprocess_isolated_from_inherited_repository_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import quality.performance_support as support

    real_run = support.run_git_subprocess
    observed = False

    def inspect_run(
        args: list[str], *, cwd: Path, env: dict[str, str]
    ) -> subprocess.CompletedProcess[bytes]:
        nonlocal observed
        assert not any(key.upper().startswith("GIT_") for key in env)
        observed = True
        return real_run(args, cwd=cwd, env=env)

    monkeypatch.setenv("GIT_DIR", "/tmp/decoy.git")
    monkeypatch.setattr(support, "run_git_subprocess", inspect_run)
    assert source_revision(REPO_ROOT)
    assert observed


def test_fixture_git_ignores_outer_repository_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outer = tmp_path / "outer"
    outer.mkdir()
    _git(outer, "init", "-q")
    sentinel = outer / "sentinel"
    sentinel.write_text("unchanged\n", encoding="utf-8")
    _git(outer, "add", "sentinel")
    index_before = _git(outer, "status", "--porcelain=v1").stdout

    monkeypatch.setenv("GIT_DIR", str(outer / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(outer))
    monkeypatch.setenv("GIT_INDEX_FILE", str(outer / ".git" / "index"))
    fixture = _fixture_repo(tmp_path / "nested")

    assert (fixture / ".git").is_dir()
    assert sentinel.read_text(encoding="utf-8") == "unchanged\n"
    assert _git(outer, "status", "--porcelain=v1").stdout == index_before


def test_tracked_source_hash_ignores_untracked_and_includes_execution(
    tmp_path: Path,
) -> None:
    root = _fixture_repo(tmp_path)
    original = tracked_python_source_hash(root)
    (root / "src" / "decoy.py").write_text("SECRET = 1\n", encoding="utf-8")
    assert tracked_python_source_hash(root) == original
    (root / "execution" / "b.py").write_text("B = 2\n", encoding="utf-8")
    assert tracked_python_source_hash(root) != original


def test_source_identity_distinguishes_clean_head_and_working_tree(tmp_path: Path) -> None:
    root = _fixture_repo(tmp_path)
    assert source_identity(root) == "clean_head"
    (root / "src" / "a.py").write_text("A = 2\n", encoding="utf-8")
    assert source_identity(root) == "working_tree"


def test_config_hash_requires_tracked_files_and_binds_content(tmp_path: Path) -> None:
    root = _fixture_repo(tmp_path)
    original = declared_config_hash(root, ["pyproject.toml"])
    (root / "pyproject.toml").write_text("[project]\nname='y'\n", encoding="utf-8")
    assert declared_config_hash(root, ["pyproject.toml"]) != original
    (root / "untracked.toml").write_text("x=1\n", encoding="utf-8")
    with pytest.raises(PerformanceIdentityError):
        declared_config_hash(root, ["untracked.toml"])


def test_scanner_identity_binds_local_import_closure(tmp_path: Path) -> None:
    root = _fixture_repo(tmp_path)
    original, _ = scanner_identity(root)
    (root / "src/runtime/python_process.py").write_text("changed", encoding="utf-8")
    changed, _ = scanner_identity(root)
    assert changed != original
    (root / "src/log_redact.py").write_text("changed again", encoding="utf-8")
    redactor_changed, _ = scanner_identity(root)
    assert redactor_changed != changed


@pytest.mark.parametrize("samples", [0, -1, 22, True, "7", 7.0, None])
def test_invalid_samples_fail_before_any_subprocess(
    monkeypatch: pytest.MonkeyPatch, samples: object
) -> None:
    import quality.performance_support as support

    def unexpected(*args: object, **kwargs: object) -> object:
        raise AssertionError("subprocess must not run")

    monkeypatch.setattr(support, "run_git_subprocess", unexpected)
    with pytest.raises(PerformanceInputError):
        capture_performance_baseline(REPO_ROOT, HERMETIC, samples=samples)


@pytest.mark.parametrize("timeout", [0, -1.0, float("inf"), float("nan"), "5", None, True])
def test_invalid_timeout_fails_before_any_subprocess(
    monkeypatch: pytest.MonkeyPatch, timeout: object
) -> None:
    import quality.performance_support as support

    def unexpected(*args: object, **kwargs: object) -> object:
        raise AssertionError("subprocess must not run")

    monkeypatch.setattr(support, "run_git_subprocess", unexpected)
    with pytest.raises(PerformanceInputError):
        capture_performance_baseline(REPO_ROOT, HERMETIC, timeout_seconds=timeout)


@pytest.mark.parametrize("command", [None, "", "   ", '"unterminated'])
def test_invalid_command_fails_before_any_subprocess(
    monkeypatch: pytest.MonkeyPatch, command: object
) -> None:
    import quality.performance_support as support

    def unexpected(*args: object, **kwargs: object) -> object:
        raise AssertionError("subprocess must not run")

    monkeypatch.setattr(support, "run_git_subprocess", unexpected)
    with pytest.raises(PerformanceInputError):
        capture_performance_baseline(REPO_ROOT, command)


@pytest.mark.parametrize(
    "configs",
    [[], ["pyproject.toml", "pyproject.toml"], ["../outside"], ["C:\\x"], [1]],
)
def test_invalid_config_declaration_fails_before_any_subprocess(
    monkeypatch: pytest.MonkeyPatch, configs: object
) -> None:
    import quality.performance_support as support

    def unexpected(*args: object, **kwargs: object) -> object:
        raise AssertionError("subprocess must not run")

    monkeypatch.setattr(support, "run_git_subprocess", unexpected)
    with pytest.raises(PerformanceInputError):
        capture_performance_baseline(REPO_ROOT, HERMETIC, config_paths=configs)


@pytest.mark.parametrize(
    "stdout",
    [b"", b"not-nul-terminated", b"src/a.py\x00\x00", b"src/\xff.py\x00"],
)
def test_malformed_tracked_listing_fails_closed(
    monkeypatch: pytest.MonkeyPatch, stdout: bytes
) -> None:
    import quality.performance_support as support

    def fake_run(
        args: list[str], *, cwd: Path, env: dict[str, str]
    ) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(args, 0, stdout=stdout, stderr=b"")

    monkeypatch.setattr(support, "run_git_subprocess", fake_run)
    with pytest.raises(PerformanceIdentityError):
        tracked_python_source_hash(REPO_ROOT)


@pytest.mark.parametrize("revision", [b"", b"HEAD\n", b"f" * 39 + b"\n", b"\xff\n"])
def test_malformed_revision_fails_closed(monkeypatch: pytest.MonkeyPatch, revision: bytes) -> None:
    import quality.performance_support as support

    def fake_run(
        args: list[str], *, cwd: Path, env: dict[str, str]
    ) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(args, 0, stdout=revision, stderr=b"")

    monkeypatch.setattr(support, "run_git_subprocess", fake_run)
    with pytest.raises(PerformanceIdentityError):
        source_revision(REPO_ROOT)


def test_missing_tracked_source_fails_closed(tmp_path: Path) -> None:
    root = _fixture_repo(tmp_path)
    (root / "src" / "a.py").unlink()
    with pytest.raises(PerformanceIdentityError):
        tracked_python_source_hash(root)


def test_managed_repo_script_argv_is_recorded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import quality.performance as performance

    seen: list[list[str]] = []

    def wrap(root: Path, argv: list[str]) -> list[str]:
        assert root == REPO_ROOT
        return ["managed", *argv]

    def fake_run(
        argv: list[str], *, cwd: Path, timeout: float, env: dict[str, str]
    ) -> subprocess.CompletedProcess[bytes]:
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout=b"ok", stderr=b"")

    monkeypatch.setattr(performance, "ensure_managed_python_argv", wrap)
    monkeypatch.setattr(performance, "run_benchmark_subprocess", fake_run)
    receipt = capture_performance_baseline(
        REPO_ROOT, f"{sys.executable} execution/example.py", samples=1
    )
    assert receipt.command_argv[0] == "managed"
    assert seen == [receipt.command_argv, receipt.command_argv]


def test_benchmark_environment_removes_credentials_and_preserves_path() -> None:
    cleaned = benchmark_environment(
        {
            "PATH": "/bin",
            "OPENROUTER_API_KEY": "secret",  # pragma: allowlist secret
            "SESSION_TOKEN": "secret",
            "AWS_ACCESS_KEY_ID": "secret",
            "AWS_PROFILE": "secret-profile",
            "PYTHONPATH": "/injected",
            "GIT_DIR": "/decoy",
            "ORDINARY": "value",
        }
    )
    assert cleaned == {"PATH": "/bin", "ORDINARY": "value"}


def test_output_accounting_is_exact_and_preview_is_redacted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import quality.performance as performance

    outputs = [(b"api_key=supersecret", b"warm-err"), (b"measured", b"measure-err")]
    calls = 0

    def fake_run(
        argv: list[str], *, cwd: Path, timeout: float, env: dict[str, str]
    ) -> subprocess.CompletedProcess[bytes]:
        nonlocal calls
        stdout, stderr = outputs[calls]
        calls += 1
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr=stderr)

    monkeypatch.setattr(performance, "run_benchmark_subprocess", fake_run)
    receipt = capture_performance_baseline(REPO_ROOT, HERMETIC, samples=1)
    exact = b"api_key=supersecretwarm-errmeasuredmeasure-err"
    assert receipt.output_bytes == len(exact)
    assert receipt.output_sha256 == hashlib.sha256(exact).hexdigest()
    assert "supersecret" not in receipt.output_preview
    assert "api_key=***" in receipt.output_preview


def test_binary_output_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    import quality.performance as performance

    def fake_run(
        argv: list[str], *, cwd: Path, timeout: float, env: dict[str, str]
    ) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(argv, 0, stdout=b"\xff", stderr=b"")

    monkeypatch.setattr(performance, "run_benchmark_subprocess", fake_run)
    with pytest.raises(PerformanceOutputError):
        capture_performance_baseline(REPO_ROOT, HERMETIC, samples=1)


def test_valid_utf8_crossing_preview_byte_boundary_is_accepted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import quality.performance as performance

    boundary_text = ("a" * 3999 + "é").encode("utf-8")

    def fake_run(
        argv: list[str], *, cwd: Path, timeout: float, env: dict[str, str]
    ) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(argv, 0, stdout=boundary_text, stderr=b"")

    monkeypatch.setattr(performance, "run_benchmark_subprocess", fake_run)
    receipt = capture_performance_baseline(REPO_ROOT, HERMETIC, samples=1)
    assert receipt.collection_status == "COMPLETE"


def test_invalid_utf8_beyond_preview_boundary_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import quality.performance as performance

    def fake_run(
        argv: list[str], *, cwd: Path, timeout: float, env: dict[str, str]
    ) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(argv, 0, stdout=b"a" * 5000 + b"\xff", stderr=b"")

    monkeypatch.setattr(performance, "run_benchmark_subprocess", fake_run)
    with pytest.raises(PerformanceOutputError):
        capture_performance_baseline(REPO_ROOT, HERMETIC, samples=1)


def test_output_preview_is_bounded() -> None:
    command = f"{sys.executable} -c 'print(\"x\" * 20000)'"
    receipt = capture_performance_baseline(REPO_ROOT, command, samples=1)
    assert receipt.output_bytes > len(receipt.output_preview.encode())
    assert len(receipt.output_preview.encode()) <= 240


def test_shell_metacharacters_are_not_executed(tmp_path: Path) -> None:
    marker = tmp_path / "injected"
    command = f'{sys.executable} -c "import sys; print(sys.argv[1])" "; touch {marker}"'
    receipt = capture_performance_baseline(REPO_ROOT, command, samples=1)
    assert receipt.collection_status == "COMPLETE"
    assert not marker.exists()


def test_nonzero_command_is_typed_failure() -> None:
    command = f'{sys.executable} -c "import sys; sys.exit(3)"'
    with pytest.raises(PerformanceExecutionError):
        capture_performance_baseline(REPO_ROOT, command, samples=1)


def test_timeout_is_typed_failure() -> None:
    command = f'{sys.executable} -c "import time; time.sleep(2)"'
    with pytest.raises(PerformanceExecutionError):
        capture_performance_baseline(REPO_ROOT, command, samples=1, timeout_seconds=0.05)


def test_missing_executable_is_typed_failure() -> None:
    with pytest.raises(PerformanceExecutionError):
        capture_performance_baseline(REPO_ROOT, "missing-binary-xyz", samples=1)


def test_cli_exit_codes_and_output_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from execution import capture_performance_baseline as cli

    output = tmp_path / "receipt.json"
    assert cli.main(["--command", HERMETIC, "--output", str(output)]) == 0
    assert output.exists()
    assert cli.main(["--command", HERMETIC, "--output", str(output), "--samples", "0"]) == 2
    bad = f'{sys.executable} -c "import sys; sys.exit(3)"'
    assert cli.main(["--command", bad, "--output", str(output)]) == 1

    def write_failure(self: Path, *args: object, **kwargs: object) -> int:
        raise OSError("synthetic")

    monkeypatch.setattr(Path, "write_text", write_failure)
    assert cli.main(["--command", HERMETIC, "--output", str(output)]) == 1


def test_cli_unexpected_exception_is_safe_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from execution import capture_performance_baseline as cli

    def unexpected(*args: object, **kwargs: object) -> object:
        raise RuntimeError("secret detail")

    monkeypatch.setattr(cli, "capture_performance_baseline", unexpected)
    assert cli.main(["--command", HERMETIC, "--output", str(tmp_path / "receipt.json")]) == 1


def test_companion_measures_reject_negative_values() -> None:
    with pytest.raises(ValueError):
        CompanionMeasures(
            sql_statements=-1,
            rows=None,
            elapsed_seconds=None,
            peak_rss_bytes=None,
        )
