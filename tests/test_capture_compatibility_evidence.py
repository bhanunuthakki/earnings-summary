from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from quality.compatibility import CompatibilityEvidenceError, capture_compatibility_evidence

sys.path.insert(0, str(Path(__file__).parents[1] / "execution"))


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test")
    (tmp_path / "src").mkdir()
    (tmp_path / "execution").mkdir()
    (tmp_path / "evals/golden").mkdir(parents=True)
    (tmp_path / "src/app.py").write_text("VALUE = 1\n", encoding="utf-8")
    (tmp_path / "execution/run.py").write_text(
        "def main() -> None:\n    return None\n", encoding="utf-8"
    )
    (tmp_path / "evals/golden/route.json").write_text(
        '{"cases": [{"input": 1}]}\n', encoding="utf-8"
    )
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "baseline")
    return tmp_path


def test_receipt_is_typed_deterministic_and_reports_parity(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    baseline = _git(root, "rev-parse", "HEAD")
    first = capture_compatibility_evidence(root, baseline)
    second = capture_compatibility_evidence(root, baseline)
    assert first.model_dump() == second.model_dump()
    assert first.golden_count == 1
    assert {item.status for item in first.entrypoint_parity} == {"unchanged"}
    assert first.source_sha256 and first.baseline_sha256
    assert {item.name for item in first.categories} == {
        "flask_url_method_endpoint_map",
        "integrity_serialized_ordering",
        "public_import_surfaces",
        "dcf_formula_cell_receipts",
        "population_dry_run_apply_receipts",
        "report_dashboard_goldens",
    }
    assert first.hold is True


def test_changed_entrypoint_is_explicitly_detected(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    baseline = _git(root, "rev-parse", "HEAD")
    (root / "execution/run.py").write_text("def main() -> None:\n    return 1\n", encoding="utf-8")
    receipt = capture_compatibility_evidence(root, baseline)
    changed = next(item for item in receipt.entrypoint_parity if item.path == "execution/run.py")
    assert changed.status == "changed"


def test_missing_baseline_or_goldens_fails_closed(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    with pytest.raises(CompatibilityEvidenceError):
        capture_compatibility_evidence(root, "")
    (root / "evals/golden/route.json").unlink()
    with pytest.raises(CompatibilityEvidenceError):
        capture_compatibility_evidence(root, _git(root, "rev-parse", "HEAD"))


def test_cli_receipt_is_json_and_parse_errors_are_nonzero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _repo(tmp_path)
    baseline = _git(root, "rev-parse", "HEAD")
    from capture_compatibility_evidence import main

    assert main(["--repo-root", str(root), "--baseline", baseline]) == 2
    assert json.loads(capsys.readouterr().out)["schema_version"] == "quality-compatibility/v1"
