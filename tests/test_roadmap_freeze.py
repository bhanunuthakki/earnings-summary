from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest

import quality.roadmap_freeze as roadmap_freeze
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
    }.issubset(freeze.selected_loc_crossings)
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


def test_validator_rejects_tampered_scc_source_path(tmp_path: Path) -> None:
    payload = json.loads(ARTIFACT.read_text(encoding="utf-8"))
    payload["scc_cut_edges"][0]["source_path"] = "tampered.py"
    _assert_rejected(tmp_path, payload)


def test_validator_rejects_tampered_issue_train_mapping(tmp_path: Path) -> None:
    payload = json.loads(ARTIFACT.read_text(encoding="utf-8"))
    payload["issue_train_matrix"]["BHA-104"] = "Train 8"
    _assert_rejected(tmp_path, payload)
