"""Run IR-document processing code against an explicit mutable state root.

``process_ir_documents`` predates split code/state deployments and derives its
indexes, source files, anchors, and summary cache from its own checkout. This
thin adapter keeps that legacy implementation unchanged while binding every
mutable path to the product state checkout before the first index read.
"""

from __future__ import annotations

import argparse
import importlib
import sys
from pathlib import Path
from typing import Protocol, cast

import db
from db_paths import require_db_path
from runtime.secrets import load_project_env


class _IndexManager(Protocol):
    PROJECT_ROOT: str
    CACHE_DIR: str
    TRANSCRIPT_INDEX_PATH: str
    DOCUMENT_INDEX_PATH: str
    TRANSCRIPTS_RAW_DIR: str
    TRANSCRIPTS_PROCESSED_DIR: str


class ProcessIrDocuments(Protocol):
    PROJECT_ROOT: Path
    CACHE_DIR: Path
    index_manager: _IndexManager

    def main(self) -> None: ...


def _load_processor() -> ProcessIrDocuments:
    module = importlib.import_module("process_ir_documents")
    required = ("PROJECT_ROOT", "CACHE_DIR", "index_manager", "main")
    if not all(hasattr(module, name) for name in required):
        raise RuntimeError(
            "process_ir_documents module does not satisfy the state adapter contract"
        )
    return cast("ProcessIrDocuments", module)


def bind_state(
    processor: ProcessIrDocuments, state_root: Path, db_path: Path | None = None
) -> None:
    cache_dir = state_root / ".tmp"
    db.set_db_path(db_path or db.DB_PATH, state_root=state_root)
    processor.PROJECT_ROOT = state_root
    processor.CACHE_DIR = cache_dir
    processor.index_manager.PROJECT_ROOT = str(state_root)
    processor.index_manager.CACHE_DIR = str(cache_dir)
    processor.index_manager.TRANSCRIPT_INDEX_PATH = str(cache_dir / "transcript_index.json")
    processor.index_manager.DOCUMENT_INDEX_PATH = str(cache_dir / "document_index.json")
    processor.index_manager.TRANSCRIPTS_RAW_DIR = str(state_root / "transcripts" / "raw")
    processor.index_manager.TRANSCRIPTS_PROCESSED_DIR = str(
        state_root / "transcripts" / "processed"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ticker", required=True)
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--db", type=Path)
    parser.add_argument("--regenerate-missing", action="store_true")
    args = parser.parse_args(argv)

    state_root = args.repo_root.resolve()
    load_project_env(state_root)
    database = require_db_path(args.db)
    processor = _load_processor()
    bind_state(processor, state_root, database)
    original_argv = sys.argv
    try:
        sys.argv = ["process_ir_documents.py", "--ticker", args.ticker]
        if args.regenerate_missing:
            sys.argv.append("--regenerate-missing")
        processor.main()
    finally:
        sys.argv = original_argv
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
