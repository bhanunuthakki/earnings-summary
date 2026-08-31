"""Fail closed when private or generated material enters the public Git tree."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

FORBIDDEN_PATHS = (
    re.compile(r"^(?:scratch|outputs?)/"),
    re.compile(r"^(?:docs/(?:design|hardening)|mockups)/"),
    re.compile(r"^HOW_TO_USE_REPORTS\.(?:md|html)$"),
    re.compile(r"^directives/self_host.*\.md$"),
    re.compile(r"^micro_thesis/holdings/"),
    re.compile(r"^dcf/(?:.*/)?[^/]+\.xlsx$", re.IGNORECASE),
    re.compile(
        r"(?:^|/)(?:portfolio|runtime|local_state|state)\.(?:db|sqlite|sqlite3)$", re.IGNORECASE
    ),
    re.compile(r"(?:^|/)(?:\.env|credentials\.json|token\.json)$", re.IGNORECASE),
)
HOME_PATH = re.compile(r"(?:/Users/[^/\s]+|/home/[^/\s]+|[A-Za-z]:\\Users\\[^\\\s]+)")
PERSONAL_EMAIL = re.compile(
    r"\b(?:bhanu|nuthakki)[^@\s]*@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b",
    re.IGNORECASE,
)
PRIVATE_DETAIL = re.compile(
    r"\b(?:my|your|personal)\s+(?:portfolio|holdings|cost\s+basis|account\s+balance)\b"
    r"|\b(?:cost\s+basis|unrealized\s+p&l|account\s+balance)\s*[:=]\s*[$€£]?\d",
    re.IGNORECASE,
)


def is_exempt_document(relative: str) -> bool:
    """Keep conceptual operator prose and test fixtures out of data scanning."""
    return relative.startswith(
        ("tests/", "instruction_tests/", "docs/", "directives/", "evals/")
    ) or relative in {
        "HOW_TO_USE_REPORTS.md",
        "AGENTS.md",
        "GEMINI.md",
        "CLAUDE.md",
        "cron/SETUP_WINDOWS_SCHEDULER.md",
    }


def tracked_files(repo_root: Path) -> list[str]:
    result = subprocess.run(
        ["git", "-C", str(repo_root), "ls-files", "-z"],
        check=True,
        capture_output=True,
    )
    return [item for item in result.stdout.decode().split("\0") if item]


def verify(repo_root: Path) -> list[str]:
    violations: list[str] = []
    for relative in tracked_files(repo_root):
        path = Path(relative)
        if any(pattern.search(relative) for pattern in FORBIDDEN_PATHS):
            violations.append(f"forbidden tracked path: {relative}")
            continue
        if relative == "execution/verify_public_tree.py":
            continue
        try:
            text = (repo_root / path).read_text(encoding="utf-8")
        except (UnicodeDecodeError, IsADirectoryError):
            continue
        if not relative.startswith(("tests/", "instruction_tests/")) and HOME_PATH.search(text):
            violations.append(f"personal home path in: {relative}")
        if PERSONAL_EMAIL.search(text):
            violations.append(f"personal email in: {relative}")
        if (
            path.suffix.lower() in {".json", ".yaml", ".yml", ".html", ".ndjson"}
            and not is_exempt_document(relative)
            and PRIVATE_DETAIL.search(text)
        ):
            violations.append(f"portfolio detail phrase in: {relative}")
    return violations


def main() -> int:
    repo_root = Path(__file__).resolve().parents[1]
    violations = verify(repo_root)
    if violations:
        print("Public-tree verification failed:")
        print("\n".join(f"- {item}" for item in violations))
        return 1
    print("Public-tree verification passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
