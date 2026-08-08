from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "execution"))

from evaluate_deletion_catalog import Catalog, evaluate  # noqa: E402


def _catalog() -> Catalog:
    path = ROOT / "docs" / "design" / "deletion_catalog_2026_08.json"
    return Catalog.model_validate_json(path.read_text(encoding="utf-8"))


def test_approved_latest_governed_code_deletion_is_complete_and_restorable() -> None:
    catalog = _catalog()
    report = evaluate(ROOT, catalog)

    assert report.valid is True
    assert {candidate.id for candidate in report.candidates} == {
        "latest-governed-plane",
        "zero-ref-legacy-tables",
    }
    assert all(candidate.eligible for candidate in report.candidates)
    assert all(candidate.issues == [] for candidate in report.candidates)
    assert all(candidate.data_restore_verified is False for candidate in report.candidates)
    assert sum(len(candidate.schema_targets) for candidate in catalog.candidates) == 25


def test_present_target_fails_closed(tmp_path: Path) -> None:
    catalog = _catalog()
    candidate = catalog.candidates[0]
    target = candidate.code_targets[0]
    path = tmp_path / target
    path.parent.mkdir(parents=True)
    path.write_text("# unexpectedly restored\n", encoding="utf-8")

    report = evaluate(tmp_path, catalog)

    assert report.valid is False
    assert any(issue.startswith("targets_still_present:") for issue in report.candidates[0].issues)


def test_stale_test_import_of_deleted_module_fails_closed(tmp_path: Path) -> None:
    catalog = _catalog()
    stale_test = tmp_path / "tests" / "test_stale_latest_state.py"
    stale_test.parent.mkdir(parents=True)
    stale_test.write_text(
        "from provenance.latest_governed_state import LatestGovernedState\n",
        encoding="utf-8",
    )

    report = evaluate(tmp_path, catalog)

    assert report.valid is False
    assert any(issue.startswith("active_imports:") for issue in report.candidates[0].issues)


def test_catalog_rejects_unsorted_or_unverified_targets() -> None:
    payload = json.loads(
        (ROOT / "docs" / "design" / "deletion_catalog_2026_08.json").read_text(encoding="utf-8")
    )
    payload["candidates"][0]["code_targets"] = list(
        reversed(payload["candidates"][0]["code_targets"])
    )
    payload["candidates"][0]["code_restore_verified"] = False

    with pytest.raises(ValueError):
        Catalog.model_validate(payload)
