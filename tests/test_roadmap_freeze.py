from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import cast

import pytest

import quality.roadmap_freeze as roadmap_freeze
import quality.roadmap_freeze_inventory as roadmap_inventory
from quality.roadmap_freeze import RoadmapFreeze, validate_freeze

ROOT = Path(__file__).resolve().parents[1]
ARTIFACT = ROOT / "docs/quality/roadmap-freeze.json"


def test_checked_freeze_has_exact_cutset_and_actual_migration_cohort() -> None:
    freeze = RoadmapFreeze.model_validate_json(ARTIFACT.read_text(encoding="utf-8"))

    assert len(freeze.scc_cut_edges) == 31
    assert freeze.issue_train_matrix == {
        "BHA-104": "Train 1",
        "BHA-105": "Train 2",
        "BHA-106": "Train 3",
        "BHA-107": "Train 4",
        "BHA-108": "Train 5",
        "BHA-109": "Train 6",
        "BHA-110": "Train 7",
        "BHA-111": "Train 8",
    }
    assert [slice_.estimated_prs for slice_ in freeze.cleanup_slices] == [
        22,
        20,
        9,
        11,
        68,
        12,
        12,
        2,
    ]
    assert freeze.estimate_totals.total_estimated_prs == 156
    assert freeze.estimate_totals.critical_path_calendar_weeks == 57
    assert {
        "execution/comments_server.py",
        "src/pipeline/portfolio_panel.py",
    }.issubset({row.path for row in freeze.selected_loc_crossings})
    assert len(freeze.selected_loc_crossings) == 56
    assert freeze.target_arithmetic.migration_builders_baseline == 172
    assert freeze.target_arithmetic.migration_builders_to_convert == 112
    assert freeze.target_arithmetic.full_suite_gap_seconds == pytest.approx(442.151, abs=0.001)
    dispositions = Counter(
        (row.taxonomy, row.disposition) for row in freeze.migration_builder_dispositions
    )
    assert sum(dispositions.values()) == 172
    assert sum(count for key, count in dispositions.items() if key[1] == "retain_candidate") == 60
    assert sum(count for key, count in dispositions.items() if key[1] == "convert_candidate") == 112
    assert dispositions["seeded-upgrade", "convert_candidate"] == 57
    assert dispositions["direct-historical", "convert_candidate"] == 24
    assert dispositions["archived-graph", "convert_candidate"] == 7
    assert dispositions["direct-downgrade", "convert_candidate"] == 24
    assert dispositions["direct-downgrade", "retain_candidate"] == 58
    assert dispositions["archived-graph", "retain_candidate"] == 1
    assert dispositions["custom-bootstrap", "retain_candidate"] == 1
    assert {slice_.issue for slice_ in freeze.cleanup_slices} == set(freeze.issue_train_matrix)


def test_validator_proves_cutset_and_allows_missing_historical_perf_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = json.loads(ARTIFACT.read_text(encoding="utf-8"))
    original_is_file = Path.is_file

    def pretend_historical_receipt_is_missing(path: Path) -> bool:
        if path == ROOT / roadmap_freeze.PERFORMANCE_RECEIPT:
            return False
        return original_is_file(path)

    monkeypatch.setattr(Path, "is_file", pretend_historical_receipt_is_missing)
    candidate = tmp_path / "roadmap-freeze.json"
    candidate.write_text(json.dumps(payload), encoding="utf-8")

    freeze = validate_freeze(ROOT, candidate)

    assert freeze.status == "HOLD"
    assert freeze.performance_snapshot.paired is False


def test_validator_rejects_forged_performance_when_historical_receipt_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = json.loads(ARTIFACT.read_text(encoding="utf-8"))
    payload["performance_snapshot"].update(
        {
            "revision": "f" * 40,
            "process_wall_seconds": 500.0,
            "paired": True,
            "evidence_status": "pass",
            "network_isolation": "proven",
        }
    )
    payload["evidence"]["performance"].update(
        {"sha256": "0" * 64, "scoped_commit": "f" * 40, "scope": "WORKTREE"}
    )
    original_is_file = Path.is_file

    def pretend_historical_receipt_is_missing(path: Path) -> bool:
        if path.resolve() == (ROOT / roadmap_freeze.PERFORMANCE_RECEIPT).resolve():
            return False
        return original_is_file(path)

    monkeypatch.setattr(Path, "is_file", pretend_historical_receipt_is_missing)
    _assert_rejected(tmp_path, payload)


def test_validator_rejects_tampered_performance_path(tmp_path: Path) -> None:
    payload = json.loads(ARTIFACT.read_text(encoding="utf-8"))
    payload["evidence"]["performance"]["path"] = "tampered-performance.json"
    _assert_rejected(tmp_path, payload)


def _assert_rejected(tmp_path: Path, payload: dict[str, object]) -> None:
    candidate = tmp_path / "roadmap-freeze.json"
    candidate.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError):
        validate_freeze(ROOT, candidate)


def test_validator_rejects_tampered_builder_cohort(tmp_path: Path) -> None:
    payload = json.loads(ARTIFACT.read_text(encoding="utf-8"))
    payload["migration_builder_dispositions"].pop()
    _assert_rejected(tmp_path, payload)


def test_validator_rejects_tampered_type_cluster(tmp_path: Path) -> None:
    payload = json.loads(ARTIFACT.read_text(encoding="utf-8"))
    payload["type_debt_clusters"][0]["count"] += 1
    _assert_rejected(tmp_path, payload)


def test_validator_rejects_self_hashed_forged_type_cluster_membership(tmp_path: Path) -> None:
    payload = json.loads(ARTIFACT.read_text(encoding="utf-8"))
    payload["type_debt_clusters"][0]["source_zone"] = "forged"
    payload["type_debt_clusters_sha256"] = hashlib.sha256(
        json.dumps(payload["type_debt_clusters"], sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    _assert_rejected(tmp_path, payload)


def test_type_debt_membership_uses_tracked_authority_when_raw_receipt_is_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    static = roadmap_inventory.read_json(ROOT, roadmap_freeze.STATIC_RECEIPT)
    diagnostics = cast(list[dict[str, object]], static["diagnostics"])
    pyright = next(row for row in diagnostics if row.get("tool") == "pyright")
    receipt_path = pyright.get("receipt_path")
    assert isinstance(receipt_path, str)
    raw_path = (ROOT / receipt_path).resolve()
    original_is_file = Path.is_file

    def hide_raw_pyright(path: Path) -> bool:
        return False if path.resolve() == raw_path else original_is_file(path)

    monkeypatch.setattr(Path, "is_file", hide_raw_pyright)
    totals, clusters = roadmap_inventory.type_debt(ROOT, static)
    assert totals == roadmap_freeze.FROZEN_TYPE_DEBT_TOTALS
    assert len(clusters) == 61


def test_type_debt_evidence_digest_ignores_pyright_runtime_metadata(tmp_path: Path) -> None:
    receipt = tmp_path / "pyright.json"
    static: dict[str, object] = {
        "diagnostics": [{"tool": "pyright", "receipt_path": "pyright.json"}]
    }
    rows = [
        {
            "file": str(tmp_path / "src/app.py"),
            "severity": "error",
            "message": 'Type of "value" is unknown',
            "range": {
                "start": {"line": 3, "character": 0},
                "end": {"line": 3, "character": 5},
            },
            "rule": "reportUnknownVariableType",
        }
    ]
    receipt.write_text(
        json.dumps({"version": "1.1.411", "time": "first", "generalDiagnostics": rows}),
        encoding="utf-8",
    )
    first_raw_sha256 = hashlib.sha256(receipt.read_bytes()).hexdigest()
    _, first_clusters = roadmap_inventory.type_debt(tmp_path, static)
    receipt.write_text(
        json.dumps(
            {
                "version": "1.1.411",
                "time": "second",
                "summary": {"timeInSec": 99.0},
                "generalDiagnostics": rows,
            }
        ),
        encoding="utf-8",
    )
    second_raw_sha256 = hashlib.sha256(receipt.read_bytes()).hexdigest()
    _, second_clusters = roadmap_inventory.type_debt(tmp_path, static)

    assert first_raw_sha256 != second_raw_sha256
    assert first_clusters == second_clusters
    assert first_clusters[0].evidence_sha256 != first_raw_sha256


def test_validator_rejects_changed_pyright_diagnostic_membership(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = json.loads(ARTIFACT.read_text(encoding="utf-8"))
    raw_path = (ROOT / roadmap_freeze.STATIC_RECEIPT).resolve()
    static = roadmap_inventory.read_json(ROOT, roadmap_freeze.STATIC_RECEIPT)
    diagnostics = cast(list[dict[str, object]], static["diagnostics"])
    pyright = next(row for row in diagnostics if row.get("tool") == "pyright")
    receipt_path = pyright.get("receipt_path")
    assert isinstance(receipt_path, str)
    raw_path = (ROOT / receipt_path).resolve()
    original_read_text = Path.read_text

    def tamper_membership(
        path: Path, encoding: str | None = None, errors: str | None = None
    ) -> str:
        text = original_read_text(path, encoding=encoding, errors=errors)
        if path.resolve() != raw_path:
            return text
        raw = json.loads(text)
        raw["generalDiagnostics"][0]["message"] = "forged diagnostic membership"
        return json.dumps(raw)

    monkeypatch.setattr(Path, "read_text", tamper_membership)
    _assert_rejected(tmp_path, payload)


def test_validator_rejects_changed_estimate_matrix_even_when_totals_reconcile(
    tmp_path: Path,
) -> None:
    payload = json.loads(ARTIFACT.read_text(encoding="utf-8"))
    payload["cleanup_slices"][0]["estimated_prs"] -= 6
    payload["cleanup_slices"][0]["estimated_calendar_weeks"] -= 1
    payload["estimate_totals"]["total_estimated_prs"] = 150
    payload["estimate_totals"]["critical_path_calendar_weeks"] = 56
    _assert_rejected(tmp_path, payload)


def test_validator_rejects_tampered_scc_source_path(tmp_path: Path) -> None:
    payload = json.loads(ARTIFACT.read_text(encoding="utf-8"))
    payload["scc_cut_edges"][0]["source_path"] = "tampered.py"
    _assert_rejected(tmp_path, payload)


def test_validator_rejects_tampered_issue_train_mapping(tmp_path: Path) -> None:
    payload = json.loads(ARTIFACT.read_text(encoding="utf-8"))
    payload["issue_train_matrix"]["BHA-104"] = "Train 8"
    _assert_rejected(tmp_path, payload)


def test_validator_rejects_mutated_mandatory_loc_cap(tmp_path: Path) -> None:
    payload = json.loads(ARTIFACT.read_text(encoding="utf-8"))
    crossings = cast(list[dict[str, object]], payload["selected_loc_crossings"])
    root = next(row for row in crossings if row.get("path") == "execution/comments_server.py")
    root["target_cap"] = 601
    _assert_rejected(tmp_path, payload)


def test_validator_rejects_train_dependency_reordering(tmp_path: Path) -> None:
    payload = json.loads(ARTIFACT.read_text(encoding="utf-8"))
    deps = payload["train_plan"][2]["depends_on"]
    payload["train_plan"][2]["depends_on"] = list(reversed(deps))
    _assert_rejected(tmp_path, payload)


def test_validator_rejects_budget_mapping_omission_and_overlap(tmp_path: Path) -> None:
    payload = json.loads(ARTIFACT.read_text(encoding="utf-8"))
    payload["budget_mappings"].pop()
    payload["budget_mappings"].append(dict(payload["budget_mappings"][0]))
    _assert_rejected(tmp_path, payload)


def test_validator_rejects_status_or_closure_drift(tmp_path: Path) -> None:
    payload = json.loads(ARTIFACT.read_text(encoding="utf-8"))
    payload["artifact_acceptance_status"] = "HOLD"
    payload["bha115_closure"]["rejudge_required"] = False
    _assert_rejected(tmp_path, payload)
