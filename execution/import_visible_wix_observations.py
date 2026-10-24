"""Validate an automatic visible-Chrome Wix export and write a sealed IR bundle."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from ir_pipeline.approved_ir_observation_capture import (  # noqa: E402
    import_wix_visible_browser_export,
    load_reviewed_ir_period_selection,
)
from log_redact import redact  # noqa: E402
from provenance.immutable_artifact import read_stable_artifact  # noqa: E402
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite  # noqa: E402


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--reviewed-period-selection", type=Path)
    parser.add_argument("--db", type=Path)
    return parser


def _event(event: str, **fields: object) -> None:
    sys.stderr.write(json.dumps({"event": event, **fields}, sort_keys=True) + "\n")


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    selection_sha256: str | None = None
    try:
        input_path = _tmp_path(args.input, label="input")
        output_path = _tmp_path(args.output, label="output")
        if (args.reviewed_period_selection is None) != (args.db is None):
            raise ValueError("reviewed period selection requires an explicit database")
        source_bytes = input_path.read_bytes()
        if args.reviewed_period_selection is None:
            bundle = import_wix_visible_browser_export(source_bytes)
        else:
            selection_path = _tmp_path(args.reviewed_period_selection, label="selection")
            selection_snapshot, selection_bytes = read_stable_artifact(
                selection_path, max_bytes=100_000, allowed_root=PROJECT_ROOT / ".tmp"
            )
            selection = load_reviewed_ir_period_selection(selection_bytes)
            selection_sha256 = selection_snapshot.file_sha256
            connection = connect_sqlite(
                args.db, role=SQLiteConnectionRole.READ_ONLY, schema_preflight=True
            )
            try:
                bundle = import_wix_visible_browser_export(
                    source_bytes,
                    reviewed_selection=selection,
                    source_connection=connection,
                )
            finally:
                connection.close()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        _write_atomically(output_path, bundle.to_bytes())
    except Exception as exc:
        _event(
            "wix_visible_browser_export_import_failed",
            error_type=type(exc).__name__,
            error=redact(exc),
        )
        return 1
    receipt: dict[str, object] = {
        "issuer": "WIX",
        "input_sha256": hashlib.sha256(source_bytes).hexdigest(),
        "bundle_sha256": bundle.bundle_sha256,
        "artifact_count": len(bundle.artifacts),
        "output": str(output_path),
    }
    if selection_sha256 is not None:
        receipt["reviewed_period_selection_sha256"] = selection_sha256
    sys.stdout.write(json.dumps(receipt, sort_keys=True) + "\n")
    _event("wix_visible_browser_export_import_completed", output=str(output_path))
    return 0


def _tmp_path(value: Path, *, label: str) -> Path:
    resolved = value.resolve()
    if PROJECT_ROOT.resolve() not in resolved.parents or ".tmp" not in resolved.parts:
        raise ValueError(f"{label} must be under this repository's .tmp directory")
    return resolved


def _write_atomically(path: Path, content: bytes) -> None:
    descriptor, temporary_name = tempfile.mkstemp(prefix=".wix-visible-import-", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


if __name__ == "__main__":
    raise SystemExit(main())
