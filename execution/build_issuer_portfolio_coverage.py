"""Build one deterministic portfolio coverage artifact from reconciled receipts.

The input artifacts are produced by ``reconcile_issuer_document_coverage.py``.
This command performs no database or network access: it validates those sealed
receipts, aggregates their document-level obligations, and writes one canonical
``issuer_portfolio_coverage.v1`` JSON artifact beneath the caller's ``.tmp``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import cast

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from pipeline.issuer_document_coverage import (  # noqa: E402
    ExtractorCoverageReconciliationOutput,
    IssuerDocumentCoverageReceipt,
    PortfolioCoverageInputError,
    PortfolioCoverageReport,
    build_portfolio_coverage_report,
    reconciliation_idempotency_key,
)


class CoverageBuildError(RuntimeError):
    """A stable CLI failure that is safe to emit without receipt contents."""

    def __init__(self, reason_code: str, *, path: Path | None = None) -> None:
        self.reason_code = reason_code
        self.path = path
        super().__init__(reason_code)


def _load_receipt(path: Path) -> ExtractorCoverageReconciliationOutput:
    try:
        payload = path.read_text(encoding="utf-8")
    except UnicodeError:
        raise CoverageBuildError("invalid_receipt", path=path) from None
    except OSError:
        raise CoverageBuildError("receipt_unreadable", path=path) from None
    try:
        decoded_raw: object = json.loads(payload)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise CoverageBuildError("invalid_receipt", path=path) from None
    if not isinstance(decoded_raw, dict):
        raise CoverageBuildError("invalid_receipt", path=path)
    decoded = cast(dict[str, object], decoded_raw)
    try:
        receipt = IssuerDocumentCoverageReceipt.model_validate(decoded.get("receipt"))
    except ValueError:
        raise CoverageBuildError("invalid_receipt", path=path) from None
    supplied_key = decoded.get("idempotency_key")
    if not isinstance(supplied_key, str):
        raise CoverageBuildError("invalid_receipt", path=path)
    if supplied_key != reconciliation_idempotency_key(receipt):
        raise CoverageBuildError("receipt_digest_mismatch", path=path)
    try:
        return ExtractorCoverageReconciliationOutput.model_validate(decoded)
    except ValueError:
        raise CoverageBuildError("invalid_receipt", path=path) from None


def _safe_output_path(repo_root: Path, output: Path, receipts: tuple[Path, ...]) -> Path:
    root = repo_root.resolve()
    declared_tmp = root / ".tmp"
    resolved_tmp = declared_tmp.resolve()
    if not resolved_tmp.is_relative_to(root):
        raise CoverageBuildError("output_outside_tmp", path=output)
    resolved_output = output.resolve()
    if not resolved_output.is_relative_to(resolved_tmp):
        raise CoverageBuildError("output_outside_tmp", path=output)
    if resolved_output in {path.resolve() for path in receipts}:
        raise CoverageBuildError("input_output_collision", path=output)
    return resolved_output


def _validated_readback(path: Path, expected: PortfolioCoverageReport) -> None:
    try:
        persisted = PortfolioCoverageReport.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise CoverageBuildError("output_readback_failed", path=path) from None
    if persisted != expected:
        raise CoverageBuildError("output_readback_failed", path=path)


def _replay_existing(path: Path, payload: bytes, report: PortfolioCoverageReport) -> bool:
    if path.is_symlink():
        raise CoverageBuildError("output_conflict", path=path)
    try:
        existing = path.read_bytes()
    except OSError:
        raise CoverageBuildError("output_unreadable", path=path) from None
    if existing != payload:
        raise CoverageBuildError("output_conflict", path=path)
    _validated_readback(path, report)
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


def _write_report(path: Path, report: PortfolioCoverageReport) -> bool:
    """Write once atomically; return True when identical bytes already existed."""
    payload = (report.canonical_json + "\n").encode("utf-8")
    if path.exists() or path.is_symlink():
        return _replay_existing(path, payload, report)

    path.parent.mkdir(parents=True, exist_ok=True)
    resolved_parent = path.parent.resolve()
    if path.resolve().parent != resolved_parent:
        raise CoverageBuildError("output_outside_tmp", path=path)

    temporary: Path | None = None
    published: os.stat_result | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=resolved_parent,
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
            return _replay_existing(path, payload, report)
        published = temporary.stat()
        _validated_readback(path, report)
        temporary.unlink(missing_ok=True)
        temporary = None
    except CoverageBuildError:
        _unlink_published_file(path, published)
        raise
    except OSError:
        _unlink_published_file(path, published)
        raise CoverageBuildError("output_write_failed", path=path) from None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return False


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--receipt",
        type=Path,
        action="append",
        required=True,
        help="Reconciliation receipt JSON; repeat for each source document",
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repo-root", type=Path, default=PROJECT_ROOT)
    return parser


def _emit_failure(error: CoverageBuildError) -> None:
    payload: dict[str, object] = {
        "event": "issuer_portfolio_coverage_failed",
        "reason_code": error.reason_code,
    }
    if error.path is not None:
        payload["path"] = str(error.path)
    sys.stderr.write(json.dumps(payload, sort_keys=True) + "\n")


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    receipt_paths = tuple(args.receipt)
    try:
        output_path = _safe_output_path(args.repo_root, args.output, receipt_paths)
        reconciliations = tuple(_load_receipt(path) for path in receipt_paths)
        try:
            report = build_portfolio_coverage_report(reconciliations)
        except PortfolioCoverageInputError as exc:
            raise CoverageBuildError(exc.reason_code) from None
        replayed = _write_report(output_path, report)
    except CoverageBuildError as exc:
        _emit_failure(exc)
        return 2

    sys.stdout.write(
        json.dumps(
            {
                "output": str(output_path),
                "receipt_count": len(reconciliations),
                "replayed": replayed,
                "row_count": len(report.rows),
                "schema_version": report.schema_version,
            },
            sort_keys=True,
        )
        + "\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
