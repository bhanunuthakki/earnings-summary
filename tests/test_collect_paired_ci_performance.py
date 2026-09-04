from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from execution.capture_test_ci_performance import redacted_output, safe_test_environment
from execution.collect_paired_ci_performance import (
    collection_state_lock,
    main,
)
from src.quality.ci_collection import capture_python_sha256
from src.quality.performance import COHORT_REGISTRY


def _commit_fixture(root: Path, message: str = "fixture") -> str:
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "--quiet", "-m", message], check=True)
    return subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()


def _manifest(root: Path, *, timing_profile: str = "stable") -> tuple[Path, str, str]:
    base = subprocess.check_output(["git", "rev-parse", "HEAD~1"], text=True).strip()
    current = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    path = root / "manifest.json"
    path.write_text(
        json.dumps(
            {
                "cohort": "ci-shard",
                "source_shard": 1,
                "source_shards": 1,
                "split_count": 1,
                "split_part": 0,
                "baseline_revision": base,
                "current_revision": current,
                "capture_mode": "hermetic",
                "timing_profile": timing_profile,
            }
        )
    )
    return path, base, current


def test_collection_manifest_resume_and_idempotency(tmp_path: Path) -> None:
    manifest, _, _ = _manifest(tmp_path)
    state = tmp_path / "state.json"
    assert (
        main(
            [
                "--manifest",
                str(manifest),
                "--state",
                str(state),
                "--repo-root",
                str(Path.cwd()),
                "--interrupt-after",
                "2",
            ]
        )
        == 130
    )
    assert json.loads(state.read_text())["status"] == "interrupted"
    assert (
        main(["--manifest", str(manifest), "--state", str(state), "--repo-root", str(Path.cwd())])
        == 2
    )
    first = state.read_text()
    state_payload = json.loads(first)
    assert state_payload["measured_pairs"] == 7
    assert state_payload["stop_reason"] == "stable"
    assert (tmp_path / "launch-order.log").read_text().splitlines()[:4] == [
        "baseline-0",
        "current-0",
        "baseline-1",
        "current-1",
    ]
    assert (
        main(["--manifest", str(manifest), "--state", str(state), "--repo-root", str(Path.cwd())])
        == 2
    )
    assert state.read_text() == first


def test_manifest_rejects_same_revision_and_forged_state(tmp_path: Path) -> None:
    manifest, base, _ = _manifest(tmp_path)
    payload = json.loads(manifest.read_text())
    payload["current_revision"] = base
    manifest.write_text(json.dumps(payload))
    with pytest.raises(SystemExit):
        main(
            [
                "--manifest",
                str(manifest),
                "--state",
                str(tmp_path / "state"),
                "--repo-root",
                str(Path.cwd()),
            ]
        )


def test_hermetic_noisy_profile_expands_adaptively(tmp_path: Path) -> None:
    manifest, _, _ = _manifest(tmp_path, timing_profile="noisy")
    state = tmp_path / "state.json"
    assert (
        main(["--manifest", str(manifest), "--state", str(state), "--repo-root", str(Path.cwd())])
        == 2
    )
    payload = json.loads(state.read_text())
    assert payload["measured_pairs"] > 7
    assert payload["stop_reason"] == "stable"


def test_hermetic_max_unstable_exhausts_at_21_and_holds(tmp_path: Path) -> None:
    manifest, _, _ = _manifest(tmp_path, timing_profile="max-unstable")
    state = tmp_path / "state.json"
    assert (
        main(["--manifest", str(manifest), "--state", str(state), "--repo-root", str(Path.cwd())])
        == 2
    )
    payload = json.loads(state.read_text())
    assert payload["measured_pairs"] == 21
    assert payload["stop_reason"] == "max-exhausted"
    pairing = json.loads((tmp_path / "pairing.json").read_text())
    assert pairing["status"] == "HOLD"
    assert "unstable" in " ".join(pairing["hold_reasons"])


def test_resume_mid_pair_continues_current_before_next_baseline(tmp_path: Path) -> None:
    manifest, _, _ = _manifest(tmp_path)
    state = tmp_path / "state.json"
    assert (
        main(
            [
                "--manifest",
                str(manifest),
                "--state",
                str(state),
                "--repo-root",
                str(Path.cwd()),
                "--interrupt-after",
                "3",
            ]
        )
        == 130
    )
    assert (
        main(["--manifest", str(manifest), "--state", str(state), "--repo-root", str(Path.cwd())])
        == 2
    )
    launches = (tmp_path / "launch-order.log").read_text().splitlines()
    assert launches[:5] == ["baseline-0", "current-0", "baseline-1", "current-1", "baseline-2"]
    assert launches.count("baseline-1") == 1


def test_ci_registry_names_real_collector() -> None:
    command = COHORT_REGISTRY["ci"].declared_command
    assert "collect_paired_ci_performance.py" in command
    assert "--help" not in command


def test_complete_state_rejects_forged_stable_stop(tmp_path: Path) -> None:
    manifest, _, _ = _manifest(tmp_path)
    state = tmp_path / "state.json"
    assert (
        main(["--manifest", str(manifest), "--state", str(state), "--repo-root", str(Path.cwd())])
        == 2
    )
    payload = json.loads(state.read_text())
    payload["stop_reason"] = "max-exhausted"
    state.write_text(json.dumps(payload))
    with pytest.raises(SystemExit, match="max-exhausted"):
        main(["--manifest", str(manifest), "--state", str(state), "--repo-root", str(Path.cwd())])


def test_collection_state_lock_rejects_second_writer(tmp_path: Path) -> None:
    state = tmp_path / "state.json"
    ready = tmp_path / "ready"
    child = tmp_path / "contender.py"
    child.write_text(
        "from pathlib import Path\n"
        "from execution.collect_paired_ci_performance import collection_state_lock\n"
        f"path = Path({str(state)!r})\n"
        "try:\n"
        "    with collection_state_lock(path):\n"
        "        raise SystemExit(0)\n"
        "except SystemExit as exc:\n"
        f"    Path({str(ready)!r}).write_text(str(exc.code))\n"
        "    raise\n"
    )
    with collection_state_lock(state):
        child_env = os.environ.copy()
        child_env["PYTHONPATH"] = str(Path.cwd())
        result = subprocess.run([sys.executable, str(child)], check=False, env=child_env)
        assert result.returncode != 0
        assert "already locked" in ready.read_text()


def test_aggregate_rejects_insufficient_max_runs() -> None:
    from src.quality.test_ci_pairing import aggregate_test_ci_pairs

    result = aggregate_test_ci_pairs([], [], max_runs=7)
    assert result.status == "INVALID"
    assert "max_runs" in " ".join(result.invalid_reasons)


def test_capture_environment_is_allowlisted_and_excludes_provider_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "secret-access-key")
    monkeypatch.setenv("GITHUB_TOKEN", "secret-github-token")
    monkeypatch.setenv("FMP_API_KEY", "secret-provider-key")
    environment = safe_test_environment(tmp_path, "cold")
    assert "AWS_ACCESS_KEY_ID" not in environment
    assert "GITHUB_TOKEN" not in environment
    assert "FMP_API_KEY" not in environment
    assert environment["NO_NETWORK"] == "1"
    assert environment["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] == "1"


def test_capture_output_is_redacted_before_echo_and_retention() -> None:
    output = redacted_output(b"request failed: https://example.test/?apikey=secret-key")
    assert b"secret-key" not in output
    assert b"apikey" in output


def test_capture_python_must_be_the_attested_collector_runtime() -> None:
    assert capture_python_sha256(sys.executable)
    with pytest.raises(SystemExit, match="identity"):
        capture_python_sha256("/usr/bin/python3")


def test_resume_rejects_forged_counters_and_shard_identity(tmp_path: Path) -> None:
    manifest, _, _ = _manifest(tmp_path)
    state_path = tmp_path / "state.json"
    assert (
        main(
            [
                "--manifest",
                str(manifest),
                "--state",
                str(state_path),
                "--repo-root",
                str(Path.cwd()),
                "--interrupt-after",
                "1",
            ]
        )
        == 130
    )
    state = json.loads(state_path.read_text())
    state["baseline_completed"] = 7
    state_path.write_text(json.dumps(state))
    with pytest.raises(SystemExit, match="counters"):
        main(
            [
                "--manifest",
                str(manifest),
                "--state",
                str(state_path),
                "--repo-root",
                str(Path.cwd()),
            ]
        )

    state = json.loads(state_path.read_text())
    receipt_path = Path(state["receipt_paths"][0])
    receipt = json.loads(receipt_path.read_text())
    receipt["cohort"]["source_shard"] = 2
    receipt["cohort"]["source_shards"] = 2
    from src.quality.test_ci_performance import FrozenTestCohort, cohort_identity

    receipt["cohort_sha256"] = cohort_identity(FrozenTestCohort.model_validate(receipt["cohort"]))
    receipt_path.write_text(json.dumps(receipt))
    state["baseline_completed"] = 0
    state_path.write_text(json.dumps(state))
    with pytest.raises(SystemExit, match="shard coordinates"):
        main(
            [
                "--manifest",
                str(manifest),
                "--state",
                str(state_path),
                "--repo-root",
                str(Path.cwd()),
            ]
        )


def test_production_capture_pins_plugin_and_keeps_revision_source(tmp_path: Path) -> None:
    """A revision cannot replace the collector plugin or source imports."""
    repo = tmp_path / "revision"
    (repo / "src" / "quality").mkdir(parents=True)
    (repo / "tests").mkdir()
    (repo / "src" / "__init__.py").write_text("\n")
    (repo / "src" / "quality" / "__init__.py").write_text("\n")
    marker = repo / "malicious-plugin-loaded"
    (repo / "src" / "quality" / "pytest_performance_plugin.py").write_text(
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('forged')\n"
        "def pytest_sessionfinish(session, exitstatus):\n"
        "    del session, exitstatus\n",
        encoding="utf-8",
    )
    (repo / "tests" / "test_smoke.py").write_text(
        "def test_smoke():\n    assert True\n", encoding="utf-8"
    )
    subprocess.run(["git", "-C", str(repo), "init", "--quiet"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "audit@example.invalid"], check=True
    )
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Audit Fixture"], check=True)
    revision = _commit_fixture(repo)
    selector = Path.cwd() / ".github" / "scripts" / "ci_gate.py"
    population = {
        "schema_version": "ci-population/v1",
        "test_files": ["tests/test_smoke.py"],
        "node_ids": ["tests/test_smoke.py::test_smoke"],
        "baseline_revision": revision,
        "current_revision": revision,
        "selector_sha256": hashlib.sha256(selector.read_bytes()).hexdigest(),
    }
    population_path = tmp_path / "population.json"
    population_path.write_text(json.dumps(population), encoding="utf-8")
    population_sha = hashlib.sha256(
        json.dumps(population, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    receipt_path = tmp_path / "receipt.json"
    fragments = tmp_path / "fragments"
    assert not marker.exists()
    capture = Path.cwd() / "execution" / "capture_test_ci_performance.py"
    plugin = Path.cwd() / "src" / "quality" / "pytest_performance_plugin.py"
    result = subprocess.run(
        [
            sys.executable,
            str(capture),
            "--repo-root",
            str(repo),
            "--cohort",
            "full-suite",
            "--cache-state",
            "cold",
            "--receipt",
            str(receipt_path),
            "--fragments-dir",
            str(fragments),
            "--expected-population",
            str(population_path),
            "--expected-population-sha256",
            population_sha,
            "--trusted-harness-sha256",
            hashlib.sha256(capture.read_bytes()).hexdigest(),
            "--trusted-selector-sha256",
            hashlib.sha256(selector.read_bytes()).hexdigest(),
            "--trusted-plugin-sha256",
            hashlib.sha256(plugin.read_bytes()).hexdigest(),
        ],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert not marker.exists()
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert [node for worker in receipt["workers"] for node in worker["node_ids"]] == [
        "tests/test_smoke.py::test_smoke"
    ]


def test_production_capture_rejects_omitted_expected_node_population(tmp_path: Path) -> None:
    selector_sha256 = hashlib.sha256(
        (Path.cwd() / ".github" / "scripts" / "ci_gate.py").read_bytes()
    ).hexdigest()
    payload = {
        "schema_version": "ci-population/v1",
        "test_files": ["tests/test_smoke.py"],
        "baseline_revision": "a" * 40,
        "current_revision": "b" * 40,
        "selector_sha256": selector_sha256,
    }
    population_path = tmp_path / "population.json"
    population_path.write_text(json.dumps(payload), encoding="utf-8")
    capture = Path.cwd() / "execution" / "capture_test_ci_performance.py"
    result = subprocess.run(
        [
            sys.executable,
            str(capture),
            "--cohort",
            "full-suite",
            "--cache-state",
            "cold",
            "--receipt",
            str(tmp_path / "receipt.json"),
            "--expected-population",
            str(population_path),
            "--expected-population-sha256",
            hashlib.sha256(
                json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
            "--trusted-harness-sha256",
            hashlib.sha256(capture.read_bytes()).hexdigest(),
            "--trusted-selector-sha256",
            selector_sha256,
            "--trusted-plugin-sha256",
            hashlib.sha256(
                (Path.cwd() / "src" / "quality" / "pytest_performance_plugin.py").read_bytes()
            ).hexdigest(),
        ],
        cwd=Path.cwd(),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "expected node population is required" in result.stderr
