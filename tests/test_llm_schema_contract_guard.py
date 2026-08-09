"""Static ratchets for governed production LLM boundaries."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PRODUCTION_ROOTS = (ROOT / "src", ROOT / "execution")
MIGRATED_LEGACY_PATHS = (
    ROOT / "src" / "bear_case_grader.py",
    ROOT / "src" / "decision_extractor.py",
    ROOT / "execution" / "canonicalize_segments.py",
    ROOT / "src" / "table_extractors" / "customer_concentration.py",
    ROOT / "src" / "table_extractors" / "investor_decks.py",
    ROOT / "execution" / "process_report_comments.py",
)


def _production_files() -> list[Path]:
    return sorted(path for root in PRODUCTION_ROOTS for path in root.rglob("*.py"))


def _call_name(node: ast.Call) -> str | None:
    if isinstance(node.func, ast.Name):
        return node.func.id
    return None


def test_every_structured_llm_call_has_an_explicit_schema() -> None:
    missing: list[str] = []
    for path in _production_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if _call_name(node) not in {"call_llm_structured", "call_llm_structured_with_raw"}:
                continue
            if not any(keyword.arg == "schema" for keyword in node.keywords):
                missing.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    assert not missing, "structured LLM calls without schema:\n" + "\n".join(missing)


def test_migrated_legacy_json_paths_keep_structured_and_spotlight_canaries() -> None:
    failures: list[str] = []
    for path in MIGRATED_LEGACY_PATHS:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        structured = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and _call_name(node) == "call_llm_structured"
        ]
        if not structured or any(
            not any(keyword.arg == "schema" for keyword in node.keywords) for node in structured
        ):
            failures.append(f"{path.relative_to(ROOT)} lacks a schema-bound structured call")
        if "spotlight(" not in source:
            failures.append(f"{path.relative_to(ROOT)} lacks an untrusted-input spotlight")

        direct = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and _call_name(node) == "call_llm"
        ]
        if path.name == "process_report_comments.py":
            # One intentionally prose-only Ask answer remains; every JSON-driving path is structured.
            if len(direct) != 1:
                failures.append(
                    f"{path.relative_to(ROOT)} expected one prose-only direct call, found {len(direct)}"
                )
        elif direct:
            failures.append(f"{path.relative_to(ROOT)} regressed to a direct LLM call")
    assert not failures, "\n".join(failures)
