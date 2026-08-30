"""Run the NVO patent extractor against an explicit mutable state root."""

from __future__ import annotations

import argparse
import importlib
import sys
from pathlib import Path
from typing import Protocol, cast

CODE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE_ROOT / "src"))

import db  # noqa: E402


class _NvoExtractor(Protocol):
    PROJECT_ROOT: Path
    OUT_DIR: Path
    IR_DOCS_DIR: Path
    SOURCES_DIR: Path

    def load_project_env(self, project_root: Path) -> None: ...

    def main(self) -> None: ...


def _load_extractor() -> _NvoExtractor:
    module = importlib.import_module("extract_nvo_patent_timeline")
    required = (
        "PROJECT_ROOT",
        "OUT_DIR",
        "IR_DOCS_DIR",
        "SOURCES_DIR",
        "load_project_env",
        "main",
    )
    if not all(hasattr(module, name) for name in required):
        raise RuntimeError("NVO extractor does not satisfy the state adapter contract")
    return cast("_NvoExtractor", module)


def _bind_state(extractor: _NvoExtractor, state_root: Path) -> None:
    extractor.PROJECT_ROOT = state_root
    extractor.OUT_DIR = state_root / ".tmp" / "nvo_patents"
    extractor.IR_DOCS_DIR = state_root / "ir_documents" / "NVO"
    extractor.SOURCES_DIR = state_root / "micro_thesis" / "sources" / "NVO"
    extractor.load_project_env(state_root)
    db.set_db_path(state_root / "data" / "portfolio.db")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--pdf", type=Path)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)

    extractor = _load_extractor()
    _bind_state(extractor, args.repo_root.resolve())
    legacy_argv = ["extract_nvo_patent_timeline.py"]
    if args.pdf is not None:
        legacy_argv.extend(["--pdf", str(args.pdf)])
    if args.force:
        legacy_argv.append("--force")
    original_argv = sys.argv
    try:
        sys.argv = legacy_argv
        extractor.main()
    finally:
        sys.argv = original_argv
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
