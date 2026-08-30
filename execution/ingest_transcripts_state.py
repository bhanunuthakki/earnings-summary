"""Run transcript ingestion code against an explicit mutable state root."""

from __future__ import annotations

import argparse
import importlib
import sys
from pathlib import Path
from typing import Protocol, cast


class _IndexManager(Protocol):
    PROJECT_ROOT: str
    CACHE_DIR: str
    TRANSCRIPT_INDEX_PATH: str
    DOCUMENT_INDEX_PATH: str
    TRANSCRIPTS_RAW_DIR: str
    TRANSCRIPTS_PROCESSED_DIR: str


class _IngestTranscripts(Protocol):
    PROJECT_ROOT: Path
    index_manager: _IndexManager

    def main(self) -> int: ...


def _load_ingester() -> _IngestTranscripts:
    module = importlib.import_module("ingest_transcripts")
    required = ("PROJECT_ROOT", "_TRANSCRIPT_DIRS", "index_manager", "main")
    if not all(hasattr(module, name) for name in required):
        raise RuntimeError("ingest_transcripts module does not satisfy the state adapter contract")
    return cast("_IngestTranscripts", module)


def _bind_state(ingester: _IngestTranscripts, state_root: Path) -> None:
    cache_dir = state_root / ".tmp"
    ingester.PROJECT_ROOT = state_root
    vars(ingester)["_TRANSCRIPT_DIRS"] = (
        state_root / "transcripts" / "processed",
        state_root / "transcripts" / "raw",
    )
    ingester.index_manager.PROJECT_ROOT = str(state_root)
    ingester.index_manager.CACHE_DIR = str(cache_dir)
    ingester.index_manager.TRANSCRIPT_INDEX_PATH = str(cache_dir / "transcript_index.json")
    ingester.index_manager.DOCUMENT_INDEX_PATH = str(cache_dir / "document_index.json")
    ingester.index_manager.TRANSCRIPTS_RAW_DIR = str(state_root / "transcripts" / "raw")
    ingester.index_manager.TRANSCRIPTS_PROCESSED_DIR = str(state_root / "transcripts" / "processed")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--no-promote", action="store_true")
    args = parser.parse_args(argv)

    state_root = args.repo_root.resolve()
    ingester = _load_ingester()
    _bind_state(ingester, state_root)
    legacy_argv = [
        "ingest_transcripts.py",
        "--db",
        str(state_root / "data" / "portfolio.db"),
    ]
    if args.no_promote:
        legacy_argv.append("--no-promote")
    original_argv = sys.argv
    try:
        sys.argv = legacy_argv
        return ingester.main()
    finally:
        sys.argv = original_argv


if __name__ == "__main__":
    raise SystemExit(main())
