"""Run exactly one managed issuer-document staging phase through SQLite bootstrap.

Usage:
    python execution/sqlite_bootstrap.py execution/manage_issuer_document_sources.py \
        prepare --request REQUEST.json --state-root STATE_ROOT --db PORTFOLIO.db
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(SRC_ROOT))

from execution.sqlite_bootstrap import require_managed_sqlite_runtime  # noqa: E402


class _RequestModel(Protocol):
    @classmethod
    def model_validate_json(cls, json_data: str | bytes) -> _IssuerDocumentRequest: ...


class _IssuerDocumentRequest(Protocol):
    @property
    def attempt_id(self) -> str: ...


class _StagingReceipt(Protocol):
    @property
    def request(self) -> _IssuerDocumentRequest: ...

    @property
    def receipt_sha256(self) -> str: ...

    @property
    def documents(self) -> tuple[object, ...]: ...


class _Publication(Protocol):
    @property
    def staging_receipt(self) -> _StagingReceipt: ...

    @property
    def receipt_sha256(self) -> str: ...

    @property
    def result_sha256(self) -> str: ...

    @property
    def committed(self) -> bool: ...

    @property
    def inserted_document_ids(self) -> tuple[int, ...]: ...

    @property
    def reused_document_ids(self) -> tuple[int, ...]: ...


class _CodedError(Protocol):
    code: str


class _PublisherError(_CodedError, Protocol):
    committed: bool
    inventory_state: str
    result_state: str


@dataclass(frozen=True)
class ManagedIssuerDocumentSeams:
    request_model: type[_RequestModel]
    prepare: Callable[..., _StagingReceipt]
    validate: Callable[..., _StagingReceipt]
    publish: Callable[..., _Publication]
    preparation_error: type[RuntimeError]
    publisher_error: type[RuntimeError]
    authentication_error: type[RuntimeError]
    contention_error: type[RuntimeError]


def _load_seams() -> ManagedIssuerDocumentSeams:
    """Import execution seams only after managed SQLite startup is verified."""
    from execution.fetch_ir_documents import (
        IssuerDocumentPreparationError,
        SourceAuthenticationDeniedError,
        prepare_issuer_document_sources,
    )
    from pipeline.managed_ir_sources import (
        IssuerDocumentStagingRequest,
        PreparedIssuerDocumentPublisherError,
        publish_prepared_issuer_documents,
        validate_prepared_staging,
    )
    from runtime.job_runtime import JobAlreadyRunningError

    return ManagedIssuerDocumentSeams(
        request_model=IssuerDocumentStagingRequest,
        prepare=prepare_issuer_document_sources,
        validate=validate_prepared_staging,
        publish=publish_prepared_issuer_documents,
        preparation_error=IssuerDocumentPreparationError,
        publisher_error=PreparedIssuerDocumentPublisherError,
        authentication_error=SourceAuthenticationDeniedError,
        contention_error=JobAlreadyRunningError,
    )


def _emit_error(code: str, phase: str, **fields: object) -> None:
    sys.stderr.write(json.dumps({"code": code, "phase": phase, **fields}, sort_keys=True) + "\n")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("prepare", "validate", "publish"))
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--state-root", type=Path, required=True)
    parser.add_argument("--db", type=Path, required=True)
    return parser


def _publication_result_path(attempt_id: str) -> str:
    return f"data/managed_ir_publications/{attempt_id}/publication_result.json"


def _receipt_result(phase: str, receipt: _StagingReceipt) -> dict[str, object]:
    return {
        "attempt_id": receipt.request.attempt_id,
        "document_count": len(receipt.documents),
        "phase": phase,
        "receipt_sha256": receipt.receipt_sha256,
    }


def _publication_result(publication: _Publication) -> dict[str, object]:
    receipt = publication.staging_receipt
    return {
        "attempt_id": receipt.request.attempt_id,
        "committed": publication.committed,
        "document_count": len(receipt.documents),
        "inserted_document_ids": list(publication.inserted_document_ids),
        "phase": "publish",
        "receipt_sha256": publication.receipt_sha256,
        "result_path": _publication_result_path(receipt.request.attempt_id),
        "result_sha256": publication.result_sha256,
        "reused_document_ids": list(publication.reused_document_ids),
    }


def _read_request(path: Path, seams: ManagedIssuerDocumentSeams) -> _IssuerDocumentRequest:
    return seams.request_model.model_validate_json(path.read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        require_managed_sqlite_runtime()
    except (ImportError, OSError, RuntimeError, ValueError):
        _emit_error("managed_runtime_required", args.phase)
        return 78

    seams = _load_seams()
    try:
        request = _read_request(args.request, seams)
    except OSError:
        _emit_error("request_unreadable", args.phase)
        return 2
    except ValueError:
        _emit_error("request_invalid", args.phase)
        return 2

    try:
        if args.phase == "prepare":
            result = _receipt_result(
                args.phase,
                seams.prepare(request, state_root=args.state_root, db_path=args.db),
            )
        elif args.phase == "validate":
            result = _receipt_result(
                args.phase,
                seams.validate(request, state_root=args.state_root, db_path=args.db),
            )
        else:
            result = _publication_result(
                seams.publish(request, state_root=args.state_root, db_path=args.db)
            )
    except seams.authentication_error:
        _emit_error("source_authentication_denied", args.phase)
        return 10
    except seams.contention_error:
        _emit_error("managed_lock_contended", args.phase)
        return 75
    except seams.preparation_error as exc:
        error = cast(_CodedError, exc)
        _emit_error(error.code, args.phase)
        return 2
    except seams.publisher_error as exc:
        error = cast(_PublisherError, exc)
        _emit_error(
            error.code,
            args.phase,
            committed=error.committed,
            inventory_state=error.inventory_state,
            result_state=error.result_state,
        )
        return 2
    except (OSError, RuntimeError, ValueError):
        _emit_error("managed_phase_failed", args.phase)
        return 2

    sys.stdout.write(json.dumps(result, separators=(",", ":"), sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
