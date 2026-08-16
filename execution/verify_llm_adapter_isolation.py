"""AST Static Check: Verify LLM Adapter Isolation (BHA-56).

Ensures that no business logic in `src/` or `execution/` directly imports vendor SDKs
(`anthropic`, `google.genai`, `openai`, `mistralai`, `groq`, `together`, etc.)
outside explicitly allowlisted provider adapter modules.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

# Vendor SDKs that must NEVER be imported outside designated adapters
BANNED_DIRECT_IMPORTS = frozenset(
    {
        "anthropic",
        "google.genai",
        "google.generativeai",
        "openai",
        "mistralai",
        "groq",
        "together",
    }
)

# Allowlisted adapter files permitted to interface with provider SDKs/CLIs
ALLOWLISTED_FILES = frozenset(
    {
        "src/llm/gemini_backend.py",
        "src/llm/openrouter_backend.py",
        "src/llm/codex_backend.py",
        "src/llm/cli.py",
        "src/llm/transport.py",
        "src/llm/frontier.py",
    }
)


def check_file(path: Path, repo_root: Path) -> list[str]:
    rel_path = path.relative_to(repo_root).as_posix()
    if rel_path in ALLOWLISTED_FILES:
        return []

    try:
        content = path.read_text(encoding="utf-8")
        tree = ast.parse(content, filename=str(path))
    except Exception as exc:
        return [f"Failed to parse {rel_path}: {exc}"]

    violations: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                for banned in BANNED_DIRECT_IMPORTS:
                    if alias.name == banned or alias.name.startswith(f"{banned}."):
                        violations.append(
                            f"{rel_path}:{node.lineno} imports '{alias.name}' directly (must route through src/llm/)"
                        )
        elif isinstance(node, ast.ImportFrom) and node.module:
            for banned in BANNED_DIRECT_IMPORTS:
                if node.module == banned or node.module.startswith(f"{banned}."):
                    violations.append(
                        f"{rel_path}:{node.lineno} imports from '{node.module}' directly (must route through src/llm/)"
                    )
    return violations


def main() -> int:
    repo_root = Path(__file__).resolve().parent.parent
    scan_dirs = [repo_root / "src", repo_root / "execution"]

    all_violations: list[str] = []
    for scan_dir in scan_dirs:
        for py_file in scan_dir.rglob("*.py"):
            violations = check_file(py_file, repo_root)
            all_violations.extend(violations)

    print("=== LLM Provider Adapter Isolation Audit ===")
    if all_violations:
        print(f"[FAIL] Found {len(all_violations)} direct provider SDK import violation(s):")
        for v in all_violations:
            print(f"  - {v}")
        return 1

    print(
        "[PASS] Zero direct provider SDK imports outside allowlisted adapters. Adapter boundary is hermetic."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
