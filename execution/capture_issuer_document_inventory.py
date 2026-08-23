"""Capture a sealed inventory of registered local IR documents for one quarter.

This command is deliberately read-only with respect to SQLite.  Its only
write is a canonical receipt below the selected repository's ``.tmp`` tree,
published once with conflict-preserving atomic no-replace semantics.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import stat
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from pipeline.issuer_document_inventory import (  # noqa: E402
    IssuerDocumentInventoryError,
    IssuerDocumentInventoryReceipt,
    IssuerDocumentInventoryRequest,
    build_issuer_document_inventory,
)
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite  # noqa: E402


class InventoryBuildError(RuntimeError):
    """A stable CLI failure that does not leak database or document contents."""

    def __init__(self, reason_code: str, *, path: Path | None = None) -> None:
        self.reason_code = reason_code
        self.path = path
        super().__init__(reason_code)


def _safe_output_path(repo_root: Path, output: Path) -> Path:
    root = repo_root.resolve()
    declared_tmp = root / ".tmp"
    lexical_tmp = Path(os.path.abspath(declared_tmp))
    lexical_output = Path(os.path.abspath(output))
    try:
        relative = lexical_output.relative_to(lexical_tmp)
    except ValueError:
        raise InventoryBuildError("output_outside_tmp", path=output) from None
    probe = lexical_tmp
    for part in relative.parts:
        if _link_or_reparse(probe):
            raise InventoryBuildError("output_conflict", path=output)
        probe /= part
    resolved_tmp = declared_tmp.resolve()
    if not resolved_tmp.is_relative_to(root):
        raise InventoryBuildError("output_outside_tmp", path=output)
    if _link_or_reparse(lexical_output):
        raise InventoryBuildError("output_conflict", path=output)
    resolved_output = output.resolve()
    if not resolved_output.is_relative_to(resolved_tmp):
        raise InventoryBuildError("output_outside_tmp", path=output)
    return resolved_output


def _link_or_reparse(path: Path) -> bool:
    try:
        file_stat = path.lstat()
    except FileNotFoundError:
        return False
    except NotADirectoryError:
        # Parent setup owns this case so it can return output_write_failed
        # without conflating an ordinary blocked path with a link conflict.
        return False
    except OSError:
        return True
    return bool(
        path.is_symlink()
        or (
            getattr(file_stat, "st_file_attributes", 0)
            & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        )
    )


def _validated_readback(path: Path, expected: IssuerDocumentInventoryReceipt) -> None:
    try:
        raw = path.read_text(encoding="utf-8")
        persisted = IssuerDocumentInventoryReceipt.model_validate_json(raw)
    except (OSError, ValueError):
        raise InventoryBuildError("output_readback_failed", path=path) from None
    if persisted != expected or raw != expected.canonical_json + "\n":
        raise InventoryBuildError("output_readback_failed", path=path)


def _replay_existing(path: Path, payload: bytes, receipt: IssuerDocumentInventoryReceipt) -> bool:
    if path.is_symlink():
        raise InventoryBuildError("output_conflict", path=path)
    try:
        existing = path.read_bytes()
    except OSError:
        raise InventoryBuildError("output_unreadable", path=path) from None
    if existing != payload:
        raise InventoryBuildError("output_conflict", path=path)
    _validated_readback(path, receipt)
    return True


def _unlink_published_file(path: Path, published: os.stat_result | None) -> None:
    if published is None:
        return
    try:
        current = path.lstat()
    except FileNotFoundError:
        return
    if (current.st_dev, current.st_ino) == (published.st_dev, published.st_ino):
        path.unlink(missing_ok=True)


def _write_receipt(path: Path, receipt: IssuerDocumentInventoryReceipt) -> bool:
    """Write once atomically; return True when exactly replaying existing bytes."""
    payload = (receipt.canonical_json + "\n").encode("utf-8")
    if path.exists() or path.is_symlink():
        return _replay_existing(path, payload, receipt)

    temporary: Path | None = None
    published: os.stat_result | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        parent = path.parent.resolve()
        if path.resolve().parent != parent:
            raise InventoryBuildError("output_outside_tmp", path=path)
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            return _replay_existing(path, payload, receipt)
        published = temporary.stat()
        _validated_readback(path, receipt)
        temporary.unlink(missing_ok=True)
        temporary = None
    except InventoryBuildError:
        _unlink_published_file(path, published)
        raise
    except OSError:
        _unlink_published_file(path, published)
        raise InventoryBuildError("output_write_failed", path=path) from None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return False


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repo-root", type=Path, default=PROJECT_ROOT)
    return parser


def _emit_failure(error: InventoryBuildError) -> None:
    payload: dict[str, object] = {
        "event": "issuer_document_inventory_failed",
        "reason_code": error.reason_code,
    }
    if error.path is not None:
        payload["path"] = str(error.path)
    sys.stderr.write(json.dumps(payload, sort_keys=True) + "\n")


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        output = _safe_output_path(args.repo_root, args.output)
        try:
            request = IssuerDocumentInventoryRequest.model_validate_json(
                args.request.read_text(encoding="utf-8")
            )
        except (OSError, ValueError):
            raise InventoryBuildError("invalid_request", path=args.request) from None
        try:
            conn = connect_sqlite(
                args.db,
                role=SQLiteConnectionRole.READ_ONLY,
                schema_preflight=True,
            )
        except (OSError, ValueError, sqlite3.Error):
            raise InventoryBuildError("schema_drift", path=args.db) from None
        try:
            receipt = build_issuer_document_inventory(
                conn,
                database_path=args.db,
                repo_root=args.repo_root,
                request=request,
            )
        except IssuerDocumentInventoryError as exc:
            raise InventoryBuildError(exc.reason_code, path=args.db) from None
        finally:
            conn.close()
        replayed = _write_receipt(output, receipt)
    except InventoryBuildError as exc:
        _emit_failure(exc)
        return 2
    sys.stdout.write(
        json.dumps(
            {
                "output": str(output),
                "receipt_sha256": receipt.receipt_sha256,
                "record_count": len(receipt.records),
                "replayed": replayed,
                "schema_version": receipt.schema_version,
            },
            sort_keys=True,
        )
        + "\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
