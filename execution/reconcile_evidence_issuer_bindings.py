"""Reconcile recorded SEC-CIK evidence subjects to canonical issuers."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from contextlib import nullcontext
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from log_redact import redact  # noqa: E402
from provenance.issuer_registry import (  # noqa: E402
    IssuerRegistry,
    UnresolvedIssuerIdentityError,
    ensure_sec_cik_evidence_binding,
)
from runtime.job_runtime import JobLock  # noqa: E402
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite  # noqa: E402


class BindingItem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    ticker: str
    recorded_issuer_id: str
    canonical_issuer_id: str | None = None
    outcome: Literal["would_bind", "created", "already_bound", "unresolved"]


class BindingResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: Literal["dry_run", "apply"]
    requested_tickers: tuple[str, ...] = Field(min_length=1)
    items: tuple[BindingItem, ...]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--ticker", action="append", required=True)
    parser.add_argument("--apply", action="store_true")
    return parser


def _event(event: str, **fields: object) -> None:
    sys.stderr.write(json.dumps({"event": event, **fields}, sort_keys=True) + "\n")


def _run(conn: sqlite3.Connection, *, tickers: tuple[str, ...], apply: bool) -> BindingResult:
    normalized_tickers = tuple(sorted({ticker.strip().upper() for ticker in tickers}))
    if not normalized_tickers or any(not ticker for ticker in normalized_tickers):
        raise ValueError("tickers must be non-empty")
    placeholders = ",".join("?" for _ in normalized_tickers)
    rows = conn.execute(
        "SELECT DISTINCT UPPER(ticker), issuer_id FROM evidence_document_versions "
        f"WHERE UPPER(ticker) IN ({placeholders}) "  # nosec B608 -- placeholders are bound
        "AND issuer_id GLOB 'sec-cik-[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]' "
        "ORDER BY UPPER(ticker), issuer_id",
        normalized_tickers,
    ).fetchall()
    found_tickers = {str(row[0]) for row in rows}
    missing = set(normalized_tickers) - found_tickers
    if missing:
        raise ValueError("no SEC-CIK evidence subject for: " + ", ".join(sorted(missing)))
    now = datetime.now(UTC)
    registry = IssuerRegistry(conn)
    planned: list[tuple[str, str, str | None, bool]] = []
    for ticker, recorded_issuer_id in rows:
        recorded = str(recorded_issuer_id)
        try:
            canonical = registry.resolve_identifier(
                "sec_cik",
                recorded.removeprefix("sec-cik-"),
                knowledge_at=now,
            )
        except UnresolvedIssuerIdentityError:
            planned.append((str(ticker), recorded, None, False))
            continue
        current = conn.execute(
            "SELECT canonical_issuer_id, outcome FROM v_legacy_issuer_bindings_current "
            "WHERE recorded_issuer_id = ?",
            (recorded,),
        ).fetchone()
        already_bound = current is not None and tuple(current) == (
            canonical.issuer_id,
            "selected",
        )
        planned.append((str(ticker), recorded, canonical.issuer_id, already_bound))
    unresolved = [item for item in planned if item[2] is None]
    if apply and unresolved:
        raise UnresolvedIssuerIdentityError("one or more SEC-CIK subjects are unresolved")
    created_subjects: set[str] = set()
    if apply:
        conn.execute("BEGIN IMMEDIATE")
        try:
            for _, recorded, _, already_bound in planned:
                if not already_bound and ensure_sec_cik_evidence_binding(
                    conn,
                    recorded_issuer_id=recorded,
                    recorded_at=now,
                ):
                    created_subjects.add(recorded)
            conn.commit()
            for _, recorded, canonical, _ in planned:
                persisted = conn.execute(
                    "SELECT canonical_issuer_id, outcome "
                    "FROM v_legacy_issuer_bindings_current "
                    "WHERE recorded_issuer_id = ?",
                    (recorded,),
                ).fetchone()
                if canonical is not None and (
                    persisted is None or tuple(persisted) != (canonical, "selected")
                ):
                    raise RuntimeError(f"SEC-CIK binding did not persist for {recorded}")
        except Exception:
            conn.rollback()
            raise
    items = tuple(
        BindingItem(
            ticker=ticker,
            recorded_issuer_id=recorded,
            canonical_issuer_id=canonical,
            outcome=(
                "unresolved"
                if canonical is None
                else "already_bound"
                if already_bound
                else "created"
                if apply and recorded in created_subjects
                else "would_bind"
            ),
        )
        for ticker, recorded, canonical, already_bound in planned
    )
    return BindingResult(
        mode="apply" if apply else "dry_run",
        requested_tickers=normalized_tickers,
        items=items,
    )


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    mode = "apply" if args.apply else "dry_run"
    try:
        role = SQLiteConnectionRole.WRITER if args.apply else SQLiteConnectionRole.READ_ONLY
        lock = (
            JobLock(
                PROJECT_ROOT,
                "reconcile-evidence-issuer-bindings",
                [f"sqlite:{args.db.resolve()}"],
            )
            if args.apply
            else nullcontext()
        )
        with lock:
            conn = connect_sqlite(args.db, role=role, schema_preflight=bool(args.apply))
            try:
                result = _run(conn, tickers=tuple(args.ticker), apply=bool(args.apply))
            finally:
                conn.close()
    except Exception as exc:
        _event(
            "evidence_issuer_binding_reconciliation_failed",
            mode=mode,
            error_type=type(exc).__name__,
            error=redact(exc),
        )
        return 1
    sys.stdout.write(result.model_dump_json() + "\n")
    _event(
        "evidence_issuer_binding_reconciliation_completed",
        mode=mode,
        item_count=len(result.items),
    )
    return 2 if any(item.outcome == "unresolved" for item in result.items) else 0


if __name__ == "__main__":
    raise SystemExit(main())
