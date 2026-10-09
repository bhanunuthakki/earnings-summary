"""Structural contract for the cockpit app factory and its panel registry."""

from __future__ import annotations

import ast
from collections.abc import Callable
from pathlib import Path

import comments_server
import pytest
from comments_server_panel_registry import PANEL_BUILDERS
from flask.testing import FlaskClient

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# The branch names of the ``if name == "..."`` chain the registry replaced.
PRE_SPLIT_PANEL_NAMES = (
    "overview",
    "operations",
    "portfolio",
    "performance_risk",
    "portfolio_synthesis",
    "positioning",
    "portfolio_risk",
    "red_team",
    "portfolio_health",
    "portfolio_allocation",
    "portfolio_record",
    "ir_coverage",
    "source_calls",
    "cron_health",
    "dcf_coverage",
    "dcf_globals",
    "validation",
    "provenance",
    "overrides",
    "credibility",
    "section_coverage",
    "explore",
    "diet",
    "musings",
    "discovery",
    "journal",
    "triage",
    "ledger_decisions",
    "ticker_settings",
    "data_policy_settings",
    "restatements",
    "actions",
    "thesis_ledger",
    "decisions_record",
    "advisor_memos",
    "evals",
    "model_eval",
)


@pytest.fixture
def client(tmp_path: Path, migrated_db: Callable[..., Path]) -> FlaskClient:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    migrated_db(data_dir / "portfolio.db")
    return comments_server.create_app(tmp_path).test_client()


def _function_lengths(path: Path) -> dict[str, int]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return {
        node.name: (node.end_lineno or node.lineno) - node.lineno + 1
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def test_panel_registry_holds_exactly_the_pre_split_names() -> None:
    assert len(PRE_SPLIT_PANEL_NAMES) == 37
    assert sorted(PANEL_BUILDERS) == sorted(PRE_SPLIT_PANEL_NAMES)


@pytest.mark.parametrize("name", PRE_SPLIT_PANEL_NAMES)
def test_registered_panel_renders_its_fragment(client: FlaskClient, name: str) -> None:
    assert name in PANEL_BUILDERS
    response = client.get(f"/api/panel/{name}")
    assert response.status_code == 200
    assert response.mimetype == "text/html"
    assert response.get_data()


def test_unknown_panel_name_is_404(client: FlaskClient) -> None:
    assert client.get("/api/panel/no_such_panel").status_code == 404


def test_create_app_is_a_wiring_function() -> None:
    lengths = _function_lengths(PROJECT_ROOT / "execution" / "comments_server.py")
    assert lengths["create_app"] <= 300


def test_no_execution_function_exceeds_1000_lines() -> None:
    oversized = [
        f"{path.relative_to(PROJECT_ROOT)}::{name} ({length})"
        for path in sorted((PROJECT_ROOT / "execution").rglob("*.py"))
        for name, length in _function_lengths(path).items()
        if length > 1000
    ]
    assert not oversized
