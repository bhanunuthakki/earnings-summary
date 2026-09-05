from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import cast

import pytest

from execution import capture_test_ci_performance as capture
from execution import collect_paired_ci_performance as collector
from execution.capture_test_ci_performance import (
    preload_digest,
    redacted_output,
    safe_test_environment,
)
from execution.collect_paired_ci_performance import (
    capture_command,
    collection_state_lock,
    main,
)
from src.quality.ci_collection import (
    CollectionManifest,
    capture_python_sha256,
)
from src.quality.ci_collection import (
    population as build_population,
)
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


def test_capture_exit_two_aborts_without_recording_completion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest, _, _ = _manifest(tmp_path)
    state = tmp_path / "state.json"

    def fail_capture(*_args: object, **_kwargs: object) -> list[str]:
        return [sys.executable, "-c", "raise SystemExit(2)"]

    monkeypatch.setattr(collector, "capture_command", fail_capture)
    with pytest.raises(SystemExit, match="capture failed for baseline-0: exit 2"):
        main(["--manifest", str(manifest), "--state", str(state), "--repo-root", str(Path.cwd())])

    assert not state.exists()
    assert not (tmp_path / "launch-order.log").exists()
    assert not (tmp_path / "receipts" / "baseline-0.json").exists()


def test_ci_registry_names_real_collector() -> None:
    command = COHORT_REGISTRY["ci"].declared_command
    assert "collect_paired_ci_performance.py" in command
    assert "--help" not in command


def test_population_payload_carries_shard_coordinates_for_capture(tmp_path: Path) -> None:
    repo = tmp_path / "revision"
    (repo / "tests").mkdir(parents=True)
    (repo / "tests" / "test_smoke.py").write_text(
        "def test_smoke():\n    assert True\n", encoding="utf-8"
    )
    subprocess.run(["git", "-C", str(repo), "init", "--quiet"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "tests@example.invalid"], check=True
    )
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Tests"], check=True)
    revision = _commit_fixture(repo)
    manifest = CollectionManifest(
        cohort="ci-shard",
        source_shard=1,
        source_shards=1,
        split_count=1,
        split_part=0,
        baseline_revision=revision,
        current_revision=revision,
        capture_mode="hermetic",
    )
    selector = Path.cwd() / ".github" / "scripts" / "ci_gate.py"
    payload, _ = build_population(repo, manifest, selector)
    assert {
        name: payload[name]
        for name in (
            "source_shard",
            "source_shards",
            "split_count",
            "split_part",
        )
    } == {
        "source_shard": 1,
        "source_shards": 1,
        "split_count": 1,
        "split_part": 0,
    }
    population_path = tmp_path / "population.json"
    population_path.write_text(json.dumps(payload), encoding="utf-8")
    args = argparse.Namespace(
        expected_population=population_path,
        cohort="ci-shard",
        source_shard=1,
        source_shards=1,
        split_count=1,
        split_part=0,
    )
    assert capture.selected_files(repo, args) == tuple(cast(list[str], payload["test_files"]))


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
    monkeypatch.setenv("LD_PRELOAD", "/tmp/untrusted.so")
    assert "LD_PRELOAD" not in safe_test_environment(tmp_path, "cold")


def test_production_collection_uses_dedicated_capture_harness(tmp_path: Path) -> None:
    manifest = CollectionManifest(
        cohort="full-suite",
        baseline_revision="a" * 40,
        current_revision="b" * 40,
    )
    command = capture_command(
        "production",
        tmp_path / "revision",
        tmp_path / "receipt.json",
        manifest,
        0,
        tmp_path / "fragments",
        sys.executable,
    )
    expected = (Path.cwd() / "execution" / "capture_test_ci_performance.py").resolve()
    assert Path(command[1]).resolve() == expected
    assert Path(command[1]).name == "capture_test_ci_performance.py"
    assert Path(command[1]).resolve() != Path(__file__).resolve()

    preload_command = capture_command(
        "production",
        tmp_path / "revision",
        tmp_path / "receipt.json",
        manifest,
        0,
        tmp_path / "fragments",
        sys.executable,
        "/tmp/sqlite-3.53.4.so",
        "a" * 64,
    )
    assert preload_command[-4:] == [
        "--sqlite-preload",
        "/tmp/sqlite-3.53.4.so",
        "--sqlite-preload-sha256",
        "a" * 64,
    ]


def test_capture_rejects_unvalidated_sqlite_preload(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="sqlite-preload"):
        safe_test_environment(tmp_path, "cold", sqlite_preload="relative/libsqlite3.so")
    with pytest.raises(SystemExit, match="required"):
        safe_test_environment(tmp_path, "cold", sqlite_preload="/tmp/sqlite.so")
    with pytest.raises(SystemExit, match="SHA-256"):
        safe_test_environment(
            tmp_path,
            "cold",
            sqlite_preload=str(Path(__file__).resolve()),
            sqlite_preload_sha256="0" * 64,
        )


def test_preload_digest_detects_post_validation_mutation(tmp_path: Path) -> None:
    preload = tmp_path / "libsqlite3.so.0"
    preload.write_bytes(b"verified library")
    expected = preload_digest(preload)
    preload.write_bytes(b"mutated library")
    assert preload_digest(preload) != expected


def test_capture_rejects_population_paths_outside_tests_or_checkout(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "tests").mkdir(parents=True)
    (repo / "tests" / "test_smoke.py").write_text("def test_smoke(): pass\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "init", "--quiet"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "tests@example.invalid"], check=True
    )
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Tests"], check=True)
    _commit_fixture(repo)
    revision = subprocess.check_output(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], text=True
    ).strip()
    payload_path = tmp_path / "population.json"
    payload_path.write_text(
        json.dumps(
            {
                "schema_version": "ci-population/v1",
                "test_files": ["tests/../outside.py"],
                "node_ids": ["tests/../outside.py::test_smoke"],
                "revision": revision,
            }
        ),
        encoding="utf-8",
    )
    args = argparse.Namespace(expected_population=payload_path, cohort="full-suite")
    with pytest.raises(SystemExit, match="unsafe path"):
        capture.selected_files(repo, args)


def test_capture_rejects_supplied_full_suite_that_bypasses_universe(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "tests").mkdir(parents=True)
    for name in ("test_smoke.py", "test_second.py"):
        (repo / "tests" / name).write_text("def test_smoke(): pass\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "init", "--quiet"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "tests@example.invalid"], check=True
    )
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Tests"], check=True)
    _commit_fixture(repo)
    revision = subprocess.check_output(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], text=True
    ).strip()
    payload_path = tmp_path / "population.json"
    payload_path.write_text(
        json.dumps(
            {
                "schema_version": "ci-population/v1",
                "test_files": ["tests/test_smoke.py"],
                "node_ids": ["tests/test_smoke.py::test_smoke"],
                "revision": revision,
            }
        ),
        encoding="utf-8",
    )
    args = argparse.Namespace(expected_population=payload_path, cohort="full-suite")
    with pytest.raises(SystemExit, match="canonical universe"):
        capture.selected_files(repo, args)


def test_capture_selects_once_from_full_universe_not_supplied_shard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    (repo / "tests").mkdir(parents=True)
    for name in ("test_smoke.py", "test_second.py"):
        (repo / "tests" / name).write_text("def test_smoke(): pass\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "init", "--quiet"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "tests@example.invalid"], check=True
    )
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Tests"], check=True)
    revision = _commit_fixture(repo)
    payload_path = tmp_path / "population.json"
    payload_path.write_text(
        json.dumps(
            {
                "schema_version": "ci-population/v1",
                "test_files": ["tests/test_smoke.py"],
                "node_ids": ["tests/test_smoke.py::test_smoke"],
                "revision": revision,
                "source_shard": 1,
                "source_shards": 1,
                "split_count": 1,
                "split_part": 0,
            }
        ),
        encoding="utf-8",
    )
    observed: dict[str, str] = {}

    def fake_selector(universe: tuple[str, ...], args: argparse.Namespace) -> tuple[str, ...]:
        del args
        observed["input"] = "\n".join(universe) + "\n"
        return ("tests/test_smoke.py",)

    monkeypatch.setattr(capture, "trusted_selected_files", fake_selector)
    args = argparse.Namespace(
        expected_population=payload_path,
        cohort="ci-shard",
        source_shard=1,
        source_shards=1,
        split_count=1,
        split_part=0,
    )
    assert capture.selected_files(repo, args) == ("tests/test_smoke.py",)
    assert observed["input"] == "tests/test_second.py\ntests/test_smoke.py\n"


def test_private_capture_paths_must_resolve_outside_tested_checkout(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    with pytest.raises(SystemExit, match="outside the tested repository"):
        capture.trusted_plugin_loader(repo / "private", repo)
    with pytest.raises(SystemExit, match="outside the tested repository"):
        capture.outside_tested_root(repo / "private", repo, label="private capture directory")


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
            "--capture-python-sha256",
            capture_python_sha256(sys.executable),
        ],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1, result.stderr
    assert not marker.exists()
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["evidence_status"] == "invalid"
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
            "--capture-python-sha256",
            capture_python_sha256(sys.executable),
        ],
        cwd=Path.cwd(),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "expected node population is required" in result.stderr
