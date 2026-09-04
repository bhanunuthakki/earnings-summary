from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from execution.collect_paired_ci_performance import main
from src.quality.performance import COHORT_REGISTRY


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
