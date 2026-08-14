"""Architecture guards for semantic thesis-history consumers."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[1]


def _load_runner() -> ModuleType:
    path = ROOT / "execution" / "run_thesis_evaluator.py"
    spec = importlib.util.spec_from_file_location("run_thesis_evaluator_contract", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_evaluator_output_tables_are_not_invocation_inputs() -> None:
    runner = _load_runner()
    material = getattr(runner, "_MATERIAL_TABLE_QUERIES")
    assert "thesis_evaluations" not in material
    assert "thesis_state" not in material


def test_owner_facing_consumers_do_not_query_raw_evaluation_history() -> None:
    permitted = {
        ROOT / "src" / "compute" / "thesis_evaluation_episodes.py",
        ROOT / "src" / "compute" / "thesis_evaluator.py",
    }
    violations: list[str] = []
    for path in (ROOT / "src").rglob("*.py"):
        if path in permitted:
            continue
        text = path.read_text(encoding="utf-8")
        if "FROM thesis_evaluations" in text:
            violations.append(str(path.relative_to(ROOT)))
    assert violations == []
