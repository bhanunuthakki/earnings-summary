"""Grep-guard: the v3-only orphan execution/fetch_fmp_statements.py stays retired.

It was unreferenced dead code (nothing scheduled or imported it) whose one
unique value — a Pydantic pre-write validation gate — was ported into the live
stable cacher execution/save_fmp_data.py in the /api/v3 -> /stable migration.
The repo has no CI workflows, so this pytest acts as the grep guard: it fails if
the orphan file reappears or if any .py/.bat re-introduces an import or
subprocess reference to it, so it can't silently come back.
"""

from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# The scan excludes this guard file by path, so it may name the orphan freely.
_NEEDLE = "fetch_fmp_statements"

_SKIP_DIRS = {
    ".git",
    ".claude",
    "venv",
    ".venv",
    ".tmp",
    ".cache",
    "__pycache__",
    "node_modules",
    "data",
    "transcripts",
    "ir_documents",
    "output",
}
_SCAN_SUFFIXES = {".py", ".bat"}


def _code_files() -> list[Path]:
    out: list[Path] = []
    for p in PROJECT_ROOT.rglob("*"):
        if p.is_dir() or p.suffix not in _SCAN_SUFFIXES:
            continue
        if any(part in _SKIP_DIRS for part in p.relative_to(PROJECT_ROOT).parts):
            continue
        out.append(p)
    return out


def test_orphan_files_are_gone() -> None:
    assert not (PROJECT_ROOT / "execution" / "fetch_fmp_statements.py").exists()
    assert not (PROJECT_ROOT / "tests" / "test_fetch_fmp_statements_validation.py").exists()


def test_no_code_references_the_orphan() -> None:
    """No .py/.bat may import or shell out to the retired orphan. This guard file
    names the module, so it excludes itself from the scan."""
    self_path = Path(__file__).resolve()
    offenders: list[str] = []
    for f in _code_files():
        if f.resolve() == self_path:
            continue
        try:
            text = f.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if _NEEDLE in text:
            offenders.append(str(f.relative_to(PROJECT_ROOT)))
    assert not offenders, (
        f"Retired orphan '{_NEEDLE}' is referenced again in: {offenders}. It was "
        "deleted in the v3->stable migration; its validation gate now lives in "
        "execution/save_fmp_data.py (_validate_stable_record)."
    )
