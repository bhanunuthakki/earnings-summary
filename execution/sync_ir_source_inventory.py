"""Crawl one issuer IR site and persist a sealed, evidence-backed source inventory."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

try:
    from _lib import PROJECT_ROOT
except ImportError:
    from execution._lib import PROJECT_ROOT

from ir_pipeline.authority import (
    IRAuthorityEvidence,
    PublisherEndpointRule,
)
from ir_pipeline.discover.generic import discover_document_inventory
from ir_pipeline.meli_inventory import CapturedMeliQuarter, load_captured_meli_quarter
from ir_pipeline.source_inventory import (
    meli_quarter_discovery,
    source_inventory_request,
    sync_ir_source_inventory,
)
from provenance.inventory_identity import (
    InventoryIdentityError,
    issuer_registry_available,
    resolve_ir_inventory_subject,
)
from runtime.job_runtime import JobAlreadyRunningError, JobLock
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite

_COLLECTOR = "sync-ir-source-inventory@2"


class CrawlPageSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    page_ordinal: int = Field(gt=0)
    outcome: str
    anchor_count: int = Field(ge=0)
    failure_reason: str | None = None


class SyncResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: str
    selected_quarter: CapturedMeliQuarter | None = None
    ticker: str
    issuer_id: str
    candidate_count: int = Field(ge=0)
    page_count: int = Field(ge=0)
    crawl_stop_reason: str
    page_outcomes: tuple[CrawlPageSummary, ...]
    complete: bool
    snapshot_id: str
    records_created: int = Field(ge=0)
    records_replayed: int = Field(ge=0)


def _event(event: str, **fields: object) -> None:
    sys.stderr.write(json.dumps({"event": event, **fields}, sort_keys=True) + "\n")


def _config_sha(
    *,
    timeout_ms: int,
    max_pages: int,
    time_budget_s: float,
    publisher_file_rules: tuple[PublisherEndpointRule, ...],
    authority: IRAuthorityEvidence | None,
    selected_quarter: CapturedMeliQuarter | None = None,
) -> str:
    return hashlib.sha256(
        json.dumps(
            {
                **(
                    {"selected_quarter": selected_quarter.model_dump(mode="json")}
                    if selected_quarter is not None
                    else {}
                ),
                "collector": _COLLECTOR,
                "timeout_ms": timeout_ms,
                "max_pages": max_pages,
                "time_budget_s": time_budget_s,
                "check_robots": True,
                "untruncated": True,
                "publisher_file_rules": [
                    rule.model_dump(mode="json") for rule in publisher_file_rules
                ],
                "authority": (None if authority is None else authority.model_dump(mode="json")),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def _publisher_rule(value: str) -> PublisherEndpointRule:
    host, separator, path_prefix = value.partition("/")
    return PublisherEndpointRule(
        host=host,
        path_prefix="/" + path_prefix if separator else "/",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--issuer-id", required=True)
    parser.add_argument("--ticker", required=True)
    parser.add_argument("--ir-url", required=True)
    parser.add_argument("--revision", type=int, required=True)
    parser.add_argument("--timeout-ms", type=int, default=60_000)
    parser.add_argument("--max-pages", type=int, default=8)
    parser.add_argument("--time-budget-s", type=float, default=240.0)
    parser.add_argument(
        "--publisher-file-endpoint",
        action="append",
        default=[],
        metavar="HOST[/PATH_PREFIX]",
    )
    parser.add_argument(
        "--authority-evidence",
        type=Path,
        help="Strict JSON publisher-universe evidence bound to existing raw observations",
    )
    parser.add_argument(
        "--blob-root",
        type=Path,
        default=PROJECT_ROOT / "data" / "evidence" / "blobs",
    )
    parser.add_argument(
        "--meli-captured-observation",
        help="Offline MELI quarter: existing original HTML SourceObservation; no crawl or capture",
    )
    parser.add_argument("--fiscal-year", type=int)
    parser.add_argument("--fiscal-quarter", type=int, choices=range(1, 5))
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    if args.meli_captured_observation is not None:
        if (
            str(args.ticker).strip().upper() != "MELI"
            or args.fiscal_year is None
            or not 2000 <= args.fiscal_year <= 2100
            or args.fiscal_quarter is None
        ):
            parser.error("captured MELI mode requires MELI, --fiscal-year and --fiscal-quarter")
        if args.authority_evidence is not None:
            parser.error("selected quarter cannot claim archive authority")
    elif args.fiscal_year is not None or args.fiscal_quarter is not None:
        parser.error("period selection requires --meli-captured-observation")

    if args.apply:
        try:
            with JobLock(
                PROJECT_ROOT,
                "sync-ir-source-inventory",
                [
                    f"sqlite:{args.db.resolve()}",
                    f"evidence-blobs:{args.blob_root.resolve()}",
                    f"source-inventory:{args.issuer_id}",
                ],
            ):
                return _run(args)
        except JobAlreadyRunningError:
            _event(
                "ir_source_inventory_locked",
                ticker=str(args.ticker).strip().upper(),
            )
            return 75
    return _run(args)


def _run(args: argparse.Namespace) -> int:
    ticker = str(args.ticker).strip().upper()
    started_at = datetime.now(UTC)
    _event("ir_source_inventory_started", ticker=ticker, mode="apply" if args.apply else "dry_run")
    conn: sqlite3.Connection | None = None
    try:
        identity_conn = connect_sqlite(
            args.db,
            role=SQLiteConnectionRole.READ_ONLY,
            schema_preflight=False,
        )
        try:
            if issuer_registry_available(identity_conn):
                resolve_ir_inventory_subject(
                    identity_conn,
                    issuer_id=str(args.issuer_id),
                    ticker=ticker,
                    ir_url=str(args.ir_url),
                    knowledge_at=started_at,
                )
        except InventoryIdentityError as exc:
            _event(
                "ir_source_inventory_identity_hard_stop",
                ticker=ticker,
                error_type=type(exc).__name__,
            )
            return 2
        finally:
            identity_conn.close()
        publisher_file_rules = tuple(
            _publisher_rule(value) for value in args.publisher_file_endpoint
        )
        authority = (
            None
            if args.authority_evidence is None
            else IRAuthorityEvidence.model_validate_json(
                args.authority_evidence.read_text(encoding="utf-8")
            )
        )
        selected_quarter: CapturedMeliQuarter | None = None
        if args.meli_captured_observation is not None:
            captured_conn = connect_sqlite(
                args.db, role=SQLiteConnectionRole.READ_ONLY, schema_preflight=False
            )
            try:
                selected_quarter = load_captured_meli_quarter(
                    captured_conn,
                    issuer_id=str(args.issuer_id),
                    ticker=ticker,
                    ir_url=str(args.ir_url),
                    source_observation_id=str(args.meli_captured_observation),
                    fiscal_year=int(args.fiscal_year),
                    fiscal_quarter=int(args.fiscal_quarter),
                    publisher_file_rules=publisher_file_rules,
                    blob_root=Path(args.blob_root),
                    knowledge_at=started_at,
                )
            finally:
                captured_conn.close()
            inventory = meli_quarter_discovery(selected_quarter)
        else:
            inventory = discover_document_inventory(
                ir_url=str(args.ir_url),
                timeout_ms=int(args.timeout_ms),
                time_budget_s=float(args.time_budget_s),
                max_pages=int(args.max_pages),
                publisher_file_rules=publisher_file_rules,
            )
        completed_at = datetime.now(UTC)
        request = source_inventory_request(
            issuer_id=str(args.issuer_id),
            ticker=ticker,
            ir_url=str(args.ir_url),
            revision=int(args.revision),
            inventory=inventory,
            authority=authority,
            selected_quarter=selected_quarter,
            retrieval_config_sha256=_config_sha(
                timeout_ms=int(args.timeout_ms),
                max_pages=int(args.max_pages),
                time_budget_s=float(args.time_budget_s),
                publisher_file_rules=publisher_file_rules,
                authority=authority,
                selected_quarter=selected_quarter,
            ),
            collector_code_version=_COLLECTOR,
            started_at=started_at,
            completed_at=completed_at,
            recorded_at=completed_at,
            reconciled_at=completed_at,
            apply=bool(args.apply),
        )
        role = SQLiteConnectionRole.WRITER if args.apply else SQLiteConnectionRole.READ_ONLY
        conn = connect_sqlite(
            args.db,
            role=role,
            schema_preflight=bool(args.apply),
        )
        result = sync_ir_source_inventory(
            conn,
            request,
            blob_root=Path(args.blob_root),
        )
    except Exception as exc:
        _event(
            "ir_source_inventory_failed",
            ticker=ticker,
            error_type=type(exc).__name__,
        )
        return 1
    finally:
        if conn is not None:
            conn.close()

    output = SyncResult(
        mode=result.mode,
        selected_quarter=selected_quarter,
        ticker=ticker,
        issuer_id=str(args.issuer_id),
        candidate_count=result.candidate_count,
        page_count=result.page_count,
        crawl_stop_reason=inventory.crawl_stop_reason,
        page_outcomes=tuple(
            CrawlPageSummary(
                page_ordinal=index,
                outcome=page.outcome,
                anchor_count=page.anchor_count,
                failure_reason=page.failure_reason,
            )
            for index, page in enumerate(inventory.pages, start=1)
        ),
        complete=result.complete,
        snapshot_id=result.snapshot_id,
        records_created=result.records_created,
        records_replayed=result.records_replayed,
    )
    sys.stdout.write(output.model_dump_json() + "\n")
    _event(
        "ir_source_inventory_completed",
        ticker=ticker,
        mode=result.mode,
        complete=result.complete,
        candidate_count=result.candidate_count,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
