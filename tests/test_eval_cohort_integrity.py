"""Unit Tests: LLM Eval Golden Cohort Integrity and Invariants (BHA-60)."""

from __future__ import annotations

import json
from pathlib import Path


def test_all_golden_datasets_exist_and_parse() -> None:
    repo_root = Path(__file__).resolve().parent.parent
    golden_dir = repo_root / "evals" / "golden"
    assert golden_dir.exists(), "evals/golden directory must exist"

    json_files = list(golden_dir.glob("*.json"))
    assert len(json_files) >= 20, f"Expected at least 20 golden cohorts, found {len(json_files)}"

    for jf in json_files:
        content = jf.read_text(encoding="utf-8")
        data = json.loads(content)
        assert isinstance(data, (dict, list)), f"{jf.name} must be a JSON dict or list"

        if isinstance(data, dict):
            # Must declare purpose or cases
            assert "cases" in data or "purpose" in data or "items" in data, (
                f"{jf.name} missing purpose or cases"
            )


def test_news_structuring_cohort_size_and_schema() -> None:
    repo_root = Path(__file__).resolve().parent.parent
    ns_file = repo_root / "evals" / "golden" / "news_structuring.json"
    data = json.loads(ns_file.read_text(encoding="utf-8"))

    assert data.get("purpose") == "news_structuring"
    cases = data.get("cases", [])
    assert len(cases) >= 6, f"Expected at least 6 cases for news_structuring cohort, got {len(cases)}"
    case_ids = {c["id"] for c in cases}
    assert len(case_ids) == len(cases), "Duplicate case IDs found in news_structuring.json"
