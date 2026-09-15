"""Bounded contracts for paired immutable performance experiments."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

from quality import performance_experiment
from quality.git_env import clean_local_git_env
from quality.performance_experiment import (
    MAX_COMPANION_OUTPUT_BYTES,
    PerformanceExperimentError,
    capture_performance_experiment,
    run_experiment_subprocess,
)
from quality.performance_experiment_models import ExperimentDeclaration


def _git(root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
        env=clean_local_git_env(),
    )
    return completed.stdout.strip()


def _declaration(*, repeats: int = 7) -> dict[str, object]:
    return {
        "schema_version": "performance-experiment-declaration/v1",
        "experiment_id": "pf1-hermetic",
        "cohort_kind": "runner_fidelity_smoke",
        "representativeness": "not_510s_or_production_feasibility_evidence",
        "workload_id": "hermetic-workload/v1",
        "workload_entrypoint": "workload.py",
        "workload_argv": ["{runner}", "workload.py"],
        "fixture_path": "fixture.txt",
        "runner_id": "project-python/v1",
        "repeats": repeats,
        "timeout_seconds": 5.0,
        "cache_state": "fresh_process_os_cache_uncontrolled",
        "required_companions": [
            "coverage_sha256",
            "fixture_sha256",
            "peak_rss_bytes",
            "result_sha256",
            "rows",
            "sql_statements",
            "workload_id",
        ],
    }


def _workload(*, hostile: bool = False, mutate_source: bool = False) -> str:
    coverage = "a" * 64
    mutation = (
        "open('fixture.txt', 'a', encoding='utf-8').write('mutation\\n')" if mutate_source else ""
    )
    return f'''import json, os, pydantic
revision = os.environ["PERFORMANCE_EXPERIMENT_REVISION"]
coverage = "{"c" * 64}" if {hostile!r} and "measured" in os.environ["PERFORMANCE_EXPERIMENT_OUTPUT_DIR"] else "{coverage}"
{mutation}
print(json.dumps({{
    "schema_version": "performance-experiment-companion/v1",
    "workload_id": os.environ["PERFORMANCE_EXPERIMENT_WORKLOAD_ID"],
    "revision": revision,
    "fixture_sha256": os.environ["PERFORMANCE_EXPERIMENT_FIXTURE_SHA256"],
    "coverage_sha256": coverage,
    "result_sha256": "d" * 64,
    "sql_statements": 0,
    "rows": 1,
    "peak_rss_bytes": 123,
}}))
'''


def _duplicate_companion_workload() -> str:
    return """import json, os, pydantic
payload = {
    "schema_version": "performance-experiment-companion/v1",
    "workload_id": os.environ["PERFORMANCE_EXPERIMENT_WORKLOAD_ID"],
    "revision": os.environ["PERFORMANCE_EXPERIMENT_REVISION"],
    "fixture_sha256": os.environ["PERFORMANCE_EXPERIMENT_FIXTURE_SHA256"],
    "coverage_sha256": "a" * 64,
    "result_sha256": "d" * 64,
    "sql_statements": 0,
    "rows": 1,
    "peak_rss_bytes": 123,
}
text = json.dumps(payload)
print(text[:-1] + ', "rows": 1}')
"""


def _fixture_repo(tmp_path: Path, *, hostile_treatment: bool = False) -> tuple[Path, str, str]:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "fixture.txt").write_text("fixed fixture\n", encoding="utf-8")
    (root / "experiment.json").write_text(
        json.dumps(_declaration(), sort_keys=True), encoding="utf-8"
    )
    (root / "workload.py").write_text(_workload(), encoding="utf-8")
    (root / "subject.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@example.invalid")
    _git(root, "config", "user.name", "Test")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "control")
    control = _git(root, "rev-parse", "HEAD")
    if hostile_treatment:
        (root / "workload.py").write_text(_workload(hostile=True), encoding="utf-8")
        _git(root, "add", "workload.py")
        _git(root, "commit", "-qm", "hostile fixed adapter")
        control = _git(root, "rev-parse", "HEAD")
    (root / "subject.py").write_text("VALUE = 2\n", encoding="utf-8")
    _git(root, "add", "subject.py")
    _git(root, "commit", "-qm", "treatment")
    treatment = _git(root, "rev-parse", "HEAD")
    return root, control, treatment


def _alias_cache_repo(tmp_path: Path) -> tuple[Path, str, str]:
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "fixture.txt").write_text("fixed fixture\n", encoding="utf-8")
    (root / "experiment.json").write_text(
        json.dumps(_declaration(), sort_keys=True), encoding="utf-8"
    )
    (root / "src" / "alias_manager.py").write_text(
        """import json
from pathlib import Path

CACHE_DIR = str(Path(__file__).resolve().parents[1] / ".tmp")
ALIASES_FILE = str(Path(CACHE_DIR) / "ticker_aliases.json")

def resolve_ticker(ticker: str) -> str:
    path = Path(ALIASES_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"GOOGL": "GOOG"}), encoding="utf-8")
    return "GOOG" if ticker == "GOOGL" else ticker
""",
        encoding="utf-8",
    )
    (root / "workload.py").write_text(
        _workload().replace(
            "revision = os.environ",
            "from pathlib import Path\n"
            "import sys\n"
            "sys.path.insert(0, str(Path(__file__).resolve().parent / 'src'))\n"
            "import alias_manager\n"
            "alias_manager.resolve_ticker('GOOGL')\n"
            "revision = os.environ",
        ),
        encoding="utf-8",
    )
    (root / "subject.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@example.invalid")
    _git(root, "config", "user.name", "Test")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "control")
    control = _git(root, "rev-parse", "HEAD")
    (root / "subject.py").write_text("VALUE = 2\n", encoding="utf-8")
    _git(root, "add", "subject.py")
    _git(root, "commit", "-qm", "treatment")
    treatment = _git(root, "rev-parse", "HEAD")
    return root, control, treatment


def test_real_hermetic_subprocess_collects_complete_but_held_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_clean_local_git_env = performance_experiment.clean_local_git_env
    real_run_experiment_subprocess = performance_experiment.run_experiment_subprocess

    def marked_clean_local_git_env(
        environ: dict[str, str] | None = None,
    ) -> dict[str, str]:
        cleaned = real_clean_local_git_env(environ)
        cleaned["PERFORMANCE_TEST_CANONICAL_GIT_ENV"] = "applied"
        return cleaned

    def assert_marked_environment(
        argv: Sequence[str], *, cwd: Path, env: Mapping[str, str], timeout: float
    ) -> subprocess.CompletedProcess[bytes]:
        assert env["PERFORMANCE_TEST_CANONICAL_GIT_ENV"] == "applied"
        return real_run_experiment_subprocess(argv, cwd=cwd, env=env, timeout=timeout)

    monkeypatch.setattr(performance_experiment, "clean_local_git_env", marked_clean_local_git_env)
    monkeypatch.setattr(
        performance_experiment, "run_experiment_subprocess", assert_marked_environment
    )
    root, control, treatment = _fixture_repo(tmp_path)
    receipt = capture_performance_experiment(
        root,
        declaration_path="experiment.json",
        control_revision=control,
        treatment_revision=treatment,
    )

    assert receipt.collection_status == "COMPLETE"
    assert receipt.causal_feasibility_status == "HOLD"
    assert receipt.admission_status == "HOLD"
    assert receipt.hold is True
    assert receipt.control.revision == control
    assert receipt.treatment.revision == treatment
    assert receipt.control.tree_sha256 != receipt.treatment.tree_sha256
    assert receipt.control.workload_sha256 == receipt.treatment.workload_sha256
    assert receipt.control.fixture_sha256 == receipt.treatment.fixture_sha256
    assert receipt.runner.executable == sys.executable
    assert receipt.runner.resolved_executable == str(Path(sys.executable).resolve())
    assert receipt.runner.executable_sha256
    assert receipt.runner.protocol_sha256
    assert len(receipt.warmups) == 2
    assert len(receipt.measured_samples) == 14
    assert receipt.control_stats.count == receipt.treatment_stats.count == 7
    assert receipt.paired_stats.count == 7
    assert receipt.paired_stats.bootstrap_ci_95_delta_seconds is not None
    assert {sample.companion_trust for sample in receipt.measured_samples} == {
        "self_reported_unverified"
    }
    assert {sample.variable_measurement_policy for sample in receipt.measured_samples} == {
        "sql_statements_and_peak_rss_are_per_sample_unverified"
    }
    assert receipt.isolation.process_isolation == "unavailable"
    assert receipt.isolation.network_isolation == "unavailable"
    assert any("unverified" in reason for reason in receipt.hold_reasons)


def test_runtime_bootstrap_redirects_historical_alias_cache_without_source_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, control, treatment = _alias_cache_repo(tmp_path)

    with pytest.raises(PerformanceExperimentError, match="isolation proof is incomplete"):
        capture_performance_experiment(
            root,
            declaration_path="experiment.json",
            control_revision=control,
            treatment_revision=treatment,
        )

    real_run = performance_experiment.run_experiment_subprocess
    cache_paths: list[Path] = []

    def observe_external_cache(
        argv: Sequence[str], *, cwd: Path, env: Mapping[str, str], timeout: float
    ) -> subprocess.CompletedProcess[bytes]:
        completed = real_run(argv, cwd=cwd, env=env, timeout=timeout)
        cache_path = Path(env["PERFORMANCE_EXPERIMENT_OUTPUT_DIR"]) / "alias-cache"
        assert (cache_path / "ticker_aliases.json").is_file()
        assert cwd not in cache_path.parents
        cache_paths.append(cache_path)
        return completed

    monkeypatch.setattr(performance_experiment, "run_experiment_subprocess", observe_external_cache)
    receipt = capture_performance_experiment(
        root,
        declaration_path="experiment.json",
        control_revision=control,
        treatment_revision=treatment,
        runtime_bootstrap="alias_cache_redirect_v1",
    )

    assert receipt.schema_version == "performance-experiment-receipt/v2"
    assert receipt.runtime_bootstrap is not None
    assert receipt.runtime_bootstrap.policy == "alias_cache_redirect_v1"
    assert receipt.runtime_bootstrap.bootstrap_sha256
    assert receipt.runtime_bootstrap.cache_lifecycle == "fresh_external_output_directory_per_sample"
    assert receipt.runtime_bootstrap.overridden_module_path == "src/alias_manager.py"
    assert receipt.runtime_bootstrap.overridden_names == ("CACHE_DIR", "ALIASES_FILE")
    assert receipt.runtime_bootstrap.injected_environment_keys == (
        "PERFORMANCE_EXPERIMENT_SNAPSHOT",
        "PERFORMANCE_EXPERIMENT_WORKLOAD_ENTRYPOINT",
    )
    assert receipt.runtime_bootstrap.effective_argv == (
        "{runner}",
        "{runtime_bootstrap}",
        "{snapshot}/workload.py",
    )
    assert any("bootstrap" in reason for reason in receipt.hold_reasons)
    assert receipt.isolation.source_trees_unchanged is True
    assert len(cache_paths) == 16
    assert len(set(cache_paths)) == len(cache_paths)


def test_runtime_bootstrap_fails_when_historical_alias_interface_is_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, control, treatment = _fixture_repo(tmp_path)
    real_run = performance_experiment.run_experiment_subprocess
    workload_ran: list[bool] = []

    def observe_bootstrap_failure(
        argv: Sequence[str], *, cwd: Path, env: Mapping[str, str], timeout: float
    ) -> subprocess.CompletedProcess[bytes]:
        completed = real_run(argv, cwd=cwd, env=env, timeout=timeout)
        workload_ran.append(bool(completed.stdout))
        return completed

    monkeypatch.setattr(
        performance_experiment, "run_experiment_subprocess", observe_bootstrap_failure
    )

    with pytest.raises(PerformanceExperimentError, match="workload exited nonzero"):
        capture_performance_experiment(
            root,
            declaration_path="experiment.json",
            control_revision=control,
            treatment_revision=treatment,
            runtime_bootstrap="alias_cache_redirect_v1",
        )
    assert workload_ran == [False]


def test_runtime_bootstrap_mutation_fails_its_recorded_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, control, treatment = _alias_cache_repo(tmp_path)
    real_run = performance_experiment.run_experiment_subprocess

    def mutate_bootstrap_after_execution(
        argv: Sequence[str], *, cwd: Path, env: Mapping[str, str], timeout: float
    ) -> subprocess.CompletedProcess[bytes]:
        completed = real_run(argv, cwd=cwd, env=env, timeout=timeout)
        Path(argv[1]).write_text("raise RuntimeError('changed')\n", encoding="utf-8")
        return completed

    monkeypatch.setattr(
        performance_experiment,
        "run_experiment_subprocess",
        mutate_bootstrap_after_execution,
    )
    with pytest.raises(PerformanceExperimentError, match="bootstrap identity drifted"):
        capture_performance_experiment(
            root,
            declaration_path="experiment.json",
            control_revision=control,
            treatment_revision=treatment,
            runtime_bootstrap="alias_cache_redirect_v1",
        )


def test_runtime_bootstrap_symlink_replacement_fails_topology(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, control, treatment = _alias_cache_repo(tmp_path)
    real_run = performance_experiment.run_experiment_subprocess

    def replace_bootstrap_with_symlink(
        argv: Sequence[str], *, cwd: Path, env: Mapping[str, str], timeout: float
    ) -> subprocess.CompletedProcess[bytes]:
        completed = real_run(argv, cwd=cwd, env=env, timeout=timeout)
        bootstrap = Path(argv[1])
        replacement = bootstrap.with_suffix(".replacement")
        replacement.write_bytes(bootstrap.read_bytes())
        bootstrap.unlink()
        bootstrap.symlink_to(replacement)
        return completed

    monkeypatch.setattr(
        performance_experiment,
        "run_experiment_subprocess",
        replace_bootstrap_with_symlink,
    )
    with pytest.raises(PerformanceExperimentError, match="bootstrap topology is invalid"):
        capture_performance_experiment(
            root,
            declaration_path="experiment.json",
            control_revision=control,
            treatment_revision=treatment,
            runtime_bootstrap="alias_cache_redirect_v1",
        )


def test_runtime_bootstrap_does_not_hide_genuine_source_mutation(tmp_path: Path) -> None:
    root, _, _ = _alias_cache_repo(tmp_path)
    (root / "workload.py").write_text(
        _workload(mutate_source=True).replace(
            "revision = os.environ",
            "from pathlib import Path\n"
            "import sys\n"
            "sys.path.insert(0, str(Path(__file__).resolve().parent / 'src'))\n"
            "import alias_manager\n"
            "alias_manager.resolve_ticker('GOOGL')\n"
            "revision = os.environ",
        ),
        encoding="utf-8",
    )
    _git(root, "add", "workload.py")
    _git(root, "commit", "-qm", "mutating control")
    control = _git(root, "rev-parse", "HEAD")
    (root / "subject.py").write_text("VALUE = 3\n", encoding="utf-8")
    _git(root, "add", "subject.py")
    _git(root, "commit", "-qm", "mutating treatment")
    treatment = _git(root, "rev-parse", "HEAD")

    with pytest.raises(PerformanceExperimentError, match="isolation proof is incomplete"):
        capture_performance_experiment(
            root,
            declaration_path="experiment.json",
            control_revision=control,
            treatment_revision=treatment,
            runtime_bootstrap="alias_cache_redirect_v1",
        )


def test_pair_order_alternates_after_unscored_warmups(tmp_path: Path) -> None:
    root, control, treatment = _fixture_repo(tmp_path)
    receipt = capture_performance_experiment(
        root,
        declaration_path="experiment.json",
        control_revision=control,
        treatment_revision=treatment,
    )
    pairs = {
        ordinal: [
            sample.arm for sample in receipt.measured_samples if sample.pair_ordinal == ordinal
        ]
        for ordinal in range(1, 8)
    }
    assert pairs[1] == ["control", "treatment"]
    assert pairs[2] == ["treatment", "control"]
    assert all(sample not in receipt.measured_samples for sample in receipt.warmups)


def test_hostile_child_cannot_forge_pair_coverage(tmp_path: Path) -> None:
    root, control, treatment = _fixture_repo(tmp_path, hostile_treatment=True)
    with pytest.raises(PerformanceExperimentError, match="identity differs across the series"):
        capture_performance_experiment(
            root,
            declaration_path="experiment.json",
            control_revision=control,
            treatment_revision=treatment,
        )


def test_duplicate_json_keys_fail_closed(tmp_path: Path) -> None:
    root, _, _ = _fixture_repo(tmp_path)
    duplicate = json.dumps(_declaration(), sort_keys=True)[:-1] + ', "repeats": 7}'
    (root / "experiment.json").write_text(duplicate, encoding="utf-8")
    _git(root, "add", "experiment.json")
    _git(root, "commit", "-qm", "duplicate declaration control")
    control = _git(root, "rev-parse", "HEAD")
    (root / "subject.py").write_text("VALUE = 3\n", encoding="utf-8")
    _git(root, "add", "subject.py")
    _git(root, "commit", "-qm", "duplicate declaration treatment")
    treatment = _git(root, "rev-parse", "HEAD")
    with pytest.raises(PerformanceExperimentError, match="duplicate keys"):
        capture_performance_experiment(
            root,
            declaration_path="experiment.json",
            control_revision=control,
            treatment_revision=treatment,
        )


def test_duplicate_companion_keys_fail_closed(tmp_path: Path) -> None:
    root, _, _ = _fixture_repo(tmp_path)
    (root / "workload.py").write_text(_duplicate_companion_workload(), encoding="utf-8")
    _git(root, "add", "workload.py")
    _git(root, "commit", "-qm", "duplicate companion control")
    control = _git(root, "rev-parse", "HEAD")
    (root / "subject.py").write_text("VALUE = 3\n", encoding="utf-8")
    _git(root, "add", "subject.py")
    _git(root, "commit", "-qm", "duplicate companion treatment")
    treatment = _git(root, "rev-parse", "HEAD")
    with pytest.raises(PerformanceExperimentError, match="duplicate keys"):
        capture_performance_experiment(
            root,
            declaration_path="experiment.json",
            control_revision=control,
            treatment_revision=treatment,
        )


def test_non_finite_runner_timing_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import quality.performance_experiment as performance

    root, control, treatment = _fixture_repo(tmp_path)
    ticks = iter((0.0, float("inf")))
    monkeypatch.setattr(performance.time, "perf_counter", lambda: next(ticks))
    with pytest.raises(PerformanceExperimentError, match="elapsed timing is invalid"):
        capture_performance_experiment(
            root,
            declaration_path="experiment.json",
            control_revision=control,
            treatment_revision=treatment,
        )


@pytest.mark.skipif(os.name != "posix", reason="PF1 process-group support is POSIX-only")
def test_timeout_terminates_descendant_process(tmp_path: Path) -> None:
    sentinel = tmp_path / "descendant-survived"
    descendant = (
        "import pathlib,sys,time; time.sleep(0.4); "
        "pathlib.Path(sys.argv[1]).write_text('alive', encoding='utf-8')"
    )
    parent = (
        "import subprocess,sys,time; "
        f"subprocess.Popen([sys.executable, '-c', {descendant!r}, sys.argv[1]]); "
        "time.sleep(60)"
    )
    with pytest.raises(PerformanceExperimentError, match="timed out") as raised:
        run_experiment_subprocess(
            [sys.executable, "-c", parent, str(sentinel)],
            cwd=tmp_path,
            env=os.environ,
            timeout=0.1,
        )
    assert str(sentinel) not in str(raised.value)
    time.sleep(0.5)
    assert not sentinel.exists()


@pytest.mark.skipif(os.name != "posix", reason="PF1 process-group support is POSIX-only")
def test_successful_parent_terminates_descendant_process(tmp_path: Path) -> None:
    sentinel = tmp_path / "descendant-survived"
    descendant = (
        "import pathlib,sys,time; time.sleep(0.4); "
        "pathlib.Path(sys.argv[1]).write_text('alive', encoding='utf-8')"
    )
    parent = (
        "import subprocess,sys; "
        f"subprocess.Popen([sys.executable, '-c', {descendant!r}, sys.argv[1]], "
        "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)"
    )
    with pytest.raises(PerformanceExperimentError, match="left descendant processes"):
        run_experiment_subprocess(
            [sys.executable, "-c", parent, str(sentinel)],
            cwd=tmp_path,
            env=os.environ,
            timeout=2,
        )
    time.sleep(0.5)
    assert not sentinel.exists()


@pytest.mark.skipif(os.name != "posix", reason="PF1 process-group support is POSIX-only")
def test_oversized_output_is_terminated_without_echo(tmp_path: Path) -> None:
    marker = "private-output-marker"
    code = f"import sys; sys.stdout.write({marker!r} + 'x' * {MAX_COMPANION_OUTPUT_BYTES})"
    with pytest.raises(PerformanceExperimentError, match="output exceeded limit") as raised:
        run_experiment_subprocess(
            [sys.executable, "-c", code],
            cwd=tmp_path,
            env=os.environ,
            timeout=2,
        )
    assert marker not in str(raised.value)


def test_tracked_smoke_declaration_is_narrow_and_strict() -> None:
    root = Path(__file__).resolve().parents[1]
    declaration = ExperimentDeclaration.model_validate_json(
        (root / "config/quality_performance_pf1_smoke.json").read_bytes()
    )
    assert declaration.cohort_kind == "runner_fidelity_smoke"
    assert declaration.representativeness == "not_510s_or_production_feasibility_evidence"
    assert declaration.workload_argv == (
        "{runner}",
        "execution/performance_pf1_smoke.py",
    )
    assert declaration.fixture_path == "tests/fixtures/performance/pf1-smoke.txt"


def test_smoke_adapter_binds_selected_and_executed_nodes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from execution import performance_pf1_smoke as smoke

    root = Path(__file__).resolve().parents[1]
    output = tmp_path / "output"
    output.mkdir()
    fixture = root / smoke.FIXTURE_PATH
    monkeypatch.chdir(root)
    monkeypatch.setenv("PERFORMANCE_EXPERIMENT_OUTPUT_DIR", str(output))
    monkeypatch.setenv("PERFORMANCE_EXPERIMENT_REVISION", "a" * 40)
    monkeypatch.setenv(
        "PERFORMANCE_EXPERIMENT_FIXTURE_SHA256",
        hashlib.sha256(fixture.read_bytes()).hexdigest(),
    )
    monkeypatch.setenv("PERFORMANCE_EXPERIMENT_WORKLOAD_ID", smoke.WORKLOAD_ID)

    def fake_pytest(argv: list[str]) -> pytest.ExitCode:
        junit_arg = next(value for value in argv if value.startswith("--junitxml="))
        junit = Path(junit_arg.split("=", 1)[1])
        cases = "".join(
            f'<testcase classname="tests.test_capture_poller" name="{node.rsplit("::", 1)[1]}" />'
            for node in smoke.SELECTED_NODES
        )
        junit.write_text(f"<testsuites><testsuite>{cases}</testsuite></testsuites>")
        print("passed")
        return pytest.ExitCode.OK

    rss_kib = 123
    rss_calls: list[int] = []

    class Usage:
        ru_maxrss = rss_kib

    def fake_getrusage(who: int) -> Usage:
        rss_calls.append(who)
        return Usage()

    monkeypatch.setattr(smoke.pytest, "main", fake_pytest)
    monkeypatch.setattr(smoke.resource, "getrusage", fake_getrusage)
    assert smoke.main() == 0
    companion = json.loads(capsys.readouterr().out)
    assert companion["rows"] == len(smoke.SELECTED_NODES)
    assert companion["coverage_sha256"]
    assert companion["result_sha256"]
    assert companion["peak_rss_bytes"] == rss_kib * (1 if sys.platform == "darwin" else 1024)
    assert rss_calls == [smoke.resource.RUSAGE_SELF]
    assert (output / "pytest.stdout").read_text(encoding="utf-8") == "passed\n"


def test_smoke_adapter_bounds_in_process_pytest_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from execution import performance_pf1_smoke as smoke

    root = Path(__file__).resolve().parents[1]
    output = tmp_path / "output"
    output.mkdir()
    fixture = root / smoke.FIXTURE_PATH
    monkeypatch.chdir(root)
    monkeypatch.setenv("PERFORMANCE_EXPERIMENT_OUTPUT_DIR", str(output))
    monkeypatch.setenv("PERFORMANCE_EXPERIMENT_REVISION", "a" * 40)
    monkeypatch.setenv(
        "PERFORMANCE_EXPERIMENT_FIXTURE_SHA256",
        hashlib.sha256(fixture.read_bytes()).hexdigest(),
    )
    monkeypatch.setenv("PERFORMANCE_EXPERIMENT_WORKLOAD_ID", smoke.WORKLOAD_ID)

    def noisy_pytest(_argv: list[str]) -> pytest.ExitCode:
        print("x" * (MAX_COMPANION_OUTPUT_BYTES + 1))
        return pytest.ExitCode.OK

    monkeypatch.setattr(smoke.pytest, "main", noisy_pytest)
    assert smoke.main() == 1
    assert (output / "pytest.stdout").stat().st_size <= MAX_COMPANION_OUTPUT_BYTES


def test_smoke_adapter_rejects_xml_entities(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from execution import performance_pf1_smoke as smoke

    root = Path(__file__).resolve().parents[1]
    output = tmp_path / "output"
    output.mkdir()
    fixture = root / smoke.FIXTURE_PATH
    monkeypatch.chdir(root)
    monkeypatch.setenv("PERFORMANCE_EXPERIMENT_OUTPUT_DIR", str(output))
    monkeypatch.setenv("PERFORMANCE_EXPERIMENT_REVISION", "a" * 40)
    monkeypatch.setenv(
        "PERFORMANCE_EXPERIMENT_FIXTURE_SHA256",
        hashlib.sha256(fixture.read_bytes()).hexdigest(),
    )
    monkeypatch.setenv("PERFORMANCE_EXPERIMENT_WORKLOAD_ID", smoke.WORKLOAD_ID)

    def entity_junit(argv: list[str]) -> pytest.ExitCode:
        junit_arg = next(value for value in argv if value.startswith("--junitxml="))
        junit = Path(junit_arg.split("=", 1)[1])
        cases = "".join(
            f'<testcase classname="tests.test_capture_poller" name="{node.rsplit("::", 1)[1]}" />'
            for node in smoke.SELECTED_NODES
        )
        junit.write_text(
            '<!DOCTYPE testsuites [<!ENTITY injected "unexpected">]>'
            f'<testsuites name="&injected;"><testsuite>{cases}</testsuite></testsuites>',
            encoding="utf-8",
        )
        return pytest.ExitCode.OK

    monkeypatch.setattr(smoke.pytest, "main", entity_junit)
    assert smoke.main() == 1


def test_safe_tar_capability_is_required(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import quality.performance_experiment as performance

    root, control, treatment = _fixture_repo(tmp_path)
    monkeypatch.setattr(performance.tarfile, "data_filter", None)
    with pytest.raises(PerformanceExperimentError, match=r"tarfile\.data_filter"):
        capture_performance_experiment(
            root,
            declaration_path="experiment.json",
            control_revision=control,
            treatment_revision=treatment,
        )


def test_non_finite_declaration_number_fails_closed(tmp_path: Path) -> None:
    root, _, _ = _fixture_repo(tmp_path)
    invalid = json.dumps(_declaration(), sort_keys=True).replace(
        '"timeout_seconds": 5.0', '"timeout_seconds": NaN'
    )
    (root / "experiment.json").write_text(invalid, encoding="utf-8")
    _git(root, "add", "experiment.json")
    _git(root, "commit", "-qm", "nonfinite declaration control")
    control = _git(root, "rev-parse", "HEAD")
    (root / "subject.py").write_text("VALUE = 3\n", encoding="utf-8")
    _git(root, "add", "subject.py")
    _git(root, "commit", "-qm", "nonfinite declaration treatment")
    treatment = _git(root, "rev-parse", "HEAD")
    with pytest.raises(PerformanceExperimentError, match="non-finite"):
        capture_performance_experiment(
            root,
            declaration_path="experiment.json",
            control_revision=control,
            treatment_revision=treatment,
        )


def test_source_mutation_fails_isolation_proof(tmp_path: Path) -> None:
    root, _, _ = _fixture_repo(tmp_path)
    (root / "workload.py").write_text(_workload(mutate_source=True), encoding="utf-8")
    _git(root, "add", "workload.py")
    _git(root, "commit", "-qm", "mutating control")
    control = _git(root, "rev-parse", "HEAD")
    (root / "subject.py").write_text("VALUE = 3\n", encoding="utf-8")
    _git(root, "add", "subject.py")
    _git(root, "commit", "-qm", "mutating treatment")
    treatment = _git(root, "rev-parse", "HEAD")
    with pytest.raises(PerformanceExperimentError, match="isolation proof is incomplete"):
        capture_performance_experiment(
            root,
            declaration_path="experiment.json",
            control_revision=control,
            treatment_revision=treatment,
        )


def test_declaration_and_fixture_drift_fail_before_execution(tmp_path: Path) -> None:
    root, control, treatment = _fixture_repo(tmp_path)
    (root / "experiment.json").write_text(
        json.dumps(_declaration(repeats=8), sort_keys=True), encoding="utf-8"
    )
    _git(root, "add", "experiment.json")
    _git(root, "commit", "-qm", "declaration drift")
    drifted = _git(root, "rev-parse", "HEAD")
    with pytest.raises(PerformanceExperimentError, match="declaration drifted"):
        capture_performance_experiment(
            root,
            declaration_path="experiment.json",
            control_revision=control,
            treatment_revision=drifted,
        )

    (root / "experiment.json").write_text(
        json.dumps(_declaration(), sort_keys=True), encoding="utf-8"
    )
    (root / "fixture.txt").write_text("changed fixture\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "fixture drift")
    fixture_drifted = _git(root, "rev-parse", "HEAD")
    with pytest.raises(PerformanceExperimentError, match="fixture drifted"):
        capture_performance_experiment(
            root,
            declaration_path="experiment.json",
            control_revision=treatment,
            treatment_revision=fixture_drifted,
        )


def test_rejects_same_revision_and_under_seven_repeats(tmp_path: Path) -> None:
    root, control, _ = _fixture_repo(tmp_path)
    with pytest.raises(PerformanceExperimentError, match="must be distinct"):
        capture_performance_experiment(
            root,
            declaration_path="experiment.json",
            control_revision=control,
            treatment_revision=control,
        )

    (root / "experiment.json").write_text(
        json.dumps(_declaration(repeats=6), sort_keys=True), encoding="utf-8"
    )
    _git(root, "add", "experiment.json")
    _git(root, "commit", "-qm", "invalid declaration")
    invalid_control = _git(root, "rev-parse", "HEAD")
    (root / "subject.py").write_text("VALUE = 3\n", encoding="utf-8")
    _git(root, "add", "subject.py")
    _git(root, "commit", "-qm", "invalid treatment")
    invalid_treatment = _git(root, "rev-parse", "HEAD")
    with pytest.raises(PerformanceExperimentError, match="declaration is invalid"):
        capture_performance_experiment(
            root,
            declaration_path="experiment.json",
            control_revision=invalid_control,
            treatment_revision=invalid_treatment,
        )
