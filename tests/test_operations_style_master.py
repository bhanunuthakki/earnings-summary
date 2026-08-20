"""Ownership regressions for operations and quality diagnostic panels."""

from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).parents[1]
OWNED = (
    "triage_panel.py",
    "calibration_scorecard_panel.py",
    "evals_panel.py",
    "discovery_panel.py",
    "mobile_inbox_panel.py",
    "cron_health_panel.py",
    "operations_panel.py",
    "provenance_panel.py",
    "validation_issues_panel.py",
    "data_policy_settings_panel.py",
    "model_eval_panel.py",
    "restatements_panel.py",
    "section_coverage_panel.py",
    "credibility_panel.py",
    "dcf_coverage_panel.py",
    "ir_coverage_panel.py",
    "fact_overrides_panel.py",
)
SELECTOR = re.compile(r"(?m)^\s*(?:\.[A-Za-z][\w-]*|#[A-Za-z][\w-]*)\s*\{")


def test_owned_consumers_have_no_local_css_or_inline_presentation() -> None:
    for name in OWNED:
        source = (ROOT / "src" / "pipeline" / name).read_text(encoding="utf-8")
        tree = ast.parse(source)
        css_literals = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and SELECTOR.search(node.value)
        ]
        assert not css_literals, f"{name} contains a local CSS literal"
        assert not re.search(r'\bstyle\s*=\s*["\']', source), f"{name} contains inline presentation"
        assert "operations_styles" in source or name == "provenance_panel.py"


def test_operations_master_is_token_clean() -> None:
    source = (ROOT / "src" / "pipeline" / "operations_styles.py").read_text(encoding="utf-8")
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b", source)
    assert "--k-dot-size" not in source
