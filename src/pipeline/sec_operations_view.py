"""Read-only policy versus observed SEC coverage from governed persisted evidence."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict

from pipeline.source_policy import (
    ArtifactKind,
    CollectionSource,
    decision_for,
    instrument_allows_artifact,
)
from provenance.sec_execution import SecExecutionReceipt, read_sec_executions
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite


class SecDocumentCoverageView(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    record_id: str
    family: str
    period: str
    state: str
    document_version_id: str | None = None
    captured_at: str | None = None
    capture_age_seconds: float | None = None
    exact_bytes: bool = False
    locator_available: bool = False
    locator_url: str | None = None
    amendment: bool = False
    supersedes: str | None = None
    source: str = "SEC native filing"


class SecExecutionView(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    receipt: SecExecutionReceipt
    population_matches: bool
    timestamp_valid: bool


class SecCoverageCompanyView(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    ticker: str
    name: str
    role: str
    sec_validated: bool
    filing_regime: str
    coverage_status: str
    coverage_tone: Literal["ok", "warn", "bad"]
    notes: str
    acquisition_mode: str = "disabled"
    inventory_id: str | None = None
    inventory_observed_at: str | None = None
    inventory_state: str = "unavailable"
    expected_native_count: int = 0
    captured_native_count: int = 0
    companyfacts_snapshot_count: int = 0
    documents: tuple[SecDocumentCoverageView, ...] = ()
    execution_state: Literal["available", "empty", "unavailable", "not_yet_wired"] = "not_yet_wired"
    executions: tuple[SecExecutionView, ...] = ()


class SecCoverageSummaryView(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    state: Literal["available", "empty", "unavailable", "not_yet_wired"] = "unavailable"
    total_tracked: int = 0
    portfolio_count: int = 0
    evaluation_count: int = 0
    watchlist_count: int = 0
    validated_count: int = 0
    gap_count: int = 0
    execution_gap_count: int = 0
    companies: tuple[SecCoverageCompanyView, ...] = ()


_EVIDENCE_TABLES = frozenset(
    {
        "source_inventory_snapshots",
        "source_inventory_snapshot_seals",
        "expected_documents",
        "source_coverage_assessments",
        "evidence_document_versions",
        "evidence_source_observations",
        "evidence_document_observation_links",
        "evidence_content_blobs",
        "evidence_blob_location_observations",
        "evidence_nodes",
        "evidence_extraction_runs",
    }
)


def _age(value: str | None, now: datetime) -> float | None:
    if not value:
        return None
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        stamp = stamp.replace(tzinfo=UTC) if stamp.tzinfo is None else stamp.astimezone(UTC)
        age = (now - stamp).total_seconds()
        return age if age >= 0 else None
    except ValueError:
        return None


def _sec_locator(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    parsed = urlsplit(value)
    host = parsed.hostname or ""
    if (
        parsed.scheme == "https"
        and (host == "sec.gov" or host.endswith(".sec.gov"))
        and not parsed.username
        and not parsed.password
        and not parsed.query
    ):
        return value
    return None


def _document(
    conn: sqlite3.Connection,
    version_id: str | None,
    *,
    ticker: str,
    expected_accession: str | None,
    now: datetime,
) -> tuple[sqlite3.Row | None, bool, bool, float | None]:
    if not version_id:
        return None, False, False, None
    row = conn.execute(
        "SELECT version.*,(SELECT observed.retrieved_at FROM evidence_source_observations AS observed WHERE observed.blob_sha256=version.blob_sha256 AND observed.source_kind=source.source_kind AND (observed.observation_id=version.observation_id OR EXISTS(SELECT 1 FROM evidence_document_observation_links AS link WHERE link.document_version_id=version.document_version_id AND link.observation_id=observed.observation_id)) ORDER BY julianday(observed.retrieved_at) DESC,observed.observation_id DESC LIMIT 1) AS retrieved_at,source.source_kind,(SELECT json_extract(node.locator_json,'$.source_ref') FROM evidence_nodes AS node JOIN evidence_extraction_runs AS run ON run.extraction_run_id=node.extraction_run_id WHERE run.document_version_id=version.document_version_id AND run.outcome='succeeded' AND json_valid(node.locator_json) ORDER BY node.node_id LIMIT 1) AS locator_ref FROM evidence_document_versions AS version JOIN evidence_source_observations AS source ON source.observation_id=version.observation_id AND source.blob_sha256=version.blob_sha256 WHERE version.document_version_id=? AND version.ticker=?",
        (version_id, ticker),
    ).fetchone()
    if row is None or (
        expected_accession is not None and row["accession_number"] != expected_accession
    ):
        return None, False, False, None
    byte_proof = (
        conn.execute(
            "SELECT 1 FROM evidence_blob_location_observations AS location JOIN evidence_content_blobs AS blob ON blob.sha256=location.blob_sha256 WHERE location.blob_sha256=? AND location.availability_state='present' AND location.verified_sha256=location.blob_sha256 AND location.verified_byte_size=blob.byte_size AND NOT EXISTS(SELECT 1 FROM evidence_blob_location_observations AS newer WHERE newer.blob_sha256=location.blob_sha256 AND newer.storage_uri=location.storage_uri AND newer.location_sequence>location.location_sequence) LIMIT 1",
            (row["blob_sha256"],),
        ).fetchone()
        is not None
    )
    locator = (
        conn.execute(
            "SELECT 1 FROM evidence_nodes AS node JOIN evidence_extraction_runs AS run ON run.extraction_run_id=node.extraction_run_id WHERE run.document_version_id=? AND run.outcome='succeeded' AND json_valid(node.locator_json) AND COALESCE(json_extract(node.locator_json,'$.source_ref'),'')<>'' LIMIT 1",
            (version_id,),
        ).fetchone()
        is not None
    )
    age = _age(str(row["retrieved_at"]), now)
    if _age(str(row["recorded_at"]), now) is None:
        return row, False, False, None
    return row, byte_proof, locator, age


def _observed(
    conn: sqlite3.Connection, ticker: str, now: datetime
) -> tuple[sqlite3.Row | None, tuple[SecDocumentCoverageView, ...]]:
    inventory = conn.execute(
        "SELECT inventory.*,seal.completion_status FROM source_inventory_snapshots AS inventory LEFT JOIN source_inventory_snapshot_seals AS seal ON seal.snapshot_id=inventory.snapshot_id WHERE inventory.ticker=? AND inventory.source_kind='sec_submissions' ORDER BY inventory.recorded_at DESC,inventory.revision DESC LIMIT 1",
        (ticker,),
    ).fetchone()
    documents: list[SecDocumentCoverageView] = []
    if inventory is not None:
        rows = conn.execute(
            "SELECT expected.*,assessment.coverage_status,assessment.document_version_id,assessment.recorded_at AS assessment_recorded_at,assessment.reason_code,assessment.policy_name FROM expected_documents AS expected LEFT JOIN source_coverage_assessments AS assessment ON assessment.expected_document_id=expected.expected_document_id AND NOT EXISTS(SELECT 1 FROM source_coverage_assessments AS newer WHERE newer.expected_document_id=assessment.expected_document_id AND newer.revision>assessment.revision) WHERE expected.snapshot_id=? AND expected.source_kind='sec_filing' ORDER BY expected.period_end DESC,expected.form_type,expected.expected_document_id",
            (inventory["snapshot_id"],),
        ).fetchall()
        for expected in rows:
            version_id = (
                str(expected["document_version_id"]) if expected["document_version_id"] else None
            )
            accession = str(expected["accession_number"]) if expected["accession_number"] else None
            version, exact, locator, age = _document(
                conn, version_id, ticker=ticker, expected_accession=accession, now=now
            )
            state = str(expected["coverage_status"] or "not_discovered")
            if (
                state == "fetch_failed"
                and expected["policy_name"] == "sealed-sec-expected-document-capture"
                and expected["reason_code"]
                in {"sec_fetch_timeout", "sec_fetch_network_deferred", "sec_fetch_transient_status"}
            ):
                state = "deferred"
            if version is not None and (
                version["issuer_id"] != expected["issuer_id"]
                or expected["issuer_id"] != inventory["issuer_id"]
                or version["source_kind"] != "sec_filing"
                or version["form_type"] != expected["form_type"]
                or str(version["period_end"])[:10] != str(expected["period_end"])[:10]
                or _age(str(expected["assessment_recorded_at"]), now) is None
            ):
                exact = locator = False
            if (
                version is not None
                and conn.execute(
                    "SELECT 1 FROM evidence_document_versions WHERE document_key=? AND version_sequence>? LIMIT 1",
                    (version["document_key"], version["version_sequence"]),
                ).fetchone()
                is not None
            ):
                state = "stale"
            if state in {"captured", "extracted", "indexed"} and (
                version is None or not accession or not exact or not locator or age is None
            ):
                state = "provenance_incomplete"
            form = str(expected["form_type"] or expected["document_type"])
            documents.append(
                SecDocumentCoverageView(
                    record_id=str(expected["expected_document_id"]),
                    family=form,
                    period=str(expected["period_end"] or "period unavailable")[:10],
                    state=state,
                    document_version_id=version_id,
                    captured_at=str(version["retrieved_at"]) if version is not None else None,
                    capture_age_seconds=age,
                    exact_bytes=exact,
                    locator_available=locator,
                    locator_url=_sec_locator(version["locator_ref"])
                    if version is not None
                    else None,
                    amendment=form.endswith("/A"),
                    supersedes=str(version["replaces_document_version_id"])
                    if version is not None and version["replaces_document_version_id"]
                    else None,
                )
            )
    snapshots = conn.execute(
        "SELECT document_version_id FROM evidence_document_versions AS version WHERE ticker=? AND document_type='companyfacts_snapshot' AND form_type='SEC-COMPANYFACTS' AND accession_number IS NULL AND NOT EXISTS(SELECT 1 FROM evidence_document_versions AS newer WHERE newer.document_key=version.document_key AND newer.version_sequence>version.version_sequence) ORDER BY recorded_at DESC LIMIT 1",
        (ticker,),
    ).fetchall()
    for snapshot in snapshots:
        version_id = str(snapshot[0])
        version, exact, locator, age = _document(
            conn, version_id, ticker=ticker, expected_accession=None, now=now
        )
        if version is None or version["source_kind"] != "sec_companyfacts":
            continue
        if inventory is not None and version["issuer_id"] != inventory["issuer_id"]:
            exact = locator = False
        documents.append(
            SecDocumentCoverageView(
                record_id=version_id,
                family="CompanyFacts aggregate",
                period="aggregate · multiple periods",
                state="captured"
                if exact and locator and age is not None
                else "provenance_incomplete",
                document_version_id=version_id,
                captured_at=str(version["retrieved_at"]),
                capture_age_seconds=age,
                exact_bytes=exact,
                locator_available=locator,
                locator_url=_sec_locator(version["locator_ref"]),
                supersedes=str(version["replaces_document_version_id"])
                if version["replaces_document_version_id"]
                else None,
                source="SEC CompanyFacts",
            )
        )
    return inventory, tuple(documents)


def _execution_matches(
    conn: sqlite3.Connection,
    receipt: SecExecutionReceipt,
    ticker: str,
    current_snapshot: str | None,
    population: set[str],
) -> bool:
    expected = receipt.scope.expected_document_ids
    if expected:
        rows = conn.execute(
            "SELECT expected_document_id,ticker FROM expected_documents WHERE expected_document_id IN (SELECT value FROM json_each(?))",
            (json.dumps(expected),),
        ).fetchall()
        if (
            len(rows) != len(expected)
            or not {str(row[0]) for row in rows if row[1] == ticker} <= population
        ):
            return False
    snapshots = receipt.result.snapshot_ids if receipt.result else receipt.scope.snapshot_ids
    if snapshots:
        rows = conn.execute(
            "SELECT snapshot_id,ticker FROM source_inventory_snapshots WHERE snapshot_id IN (SELECT value FROM json_each(?))",
            (json.dumps(snapshots),),
        ).fetchall()
        return len(rows) == len(snapshots) and {
            str(row[0]) for row in rows if row[1] == ticker
        } == {current_snapshot}
    return receipt.scope.kind == "inventory_sync"


def read_sec_coverage_state(
    db_path: Path | None, *, as_of: datetime | None = None
) -> SecCoverageSummaryView:
    """An empty successful roster differs from unavailable and unwired evidence storage."""
    if db_path is None or not db_path.is_file():
        return SecCoverageSummaryView()
    now = as_of or datetime.now(UTC)
    now = now.replace(tzinfo=UTC) if now.tzinfo is None else now.astimezone(UTC)
    try:
        conn = connect_sqlite(db_path, role=SQLiteConnectionRole.READ_ONLY)
        conn.row_factory = sqlite3.Row
        try:
            tables = {
                str(row[0])
                for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            if "tracked_companies" not in tables:
                return SecCoverageSummaryView()
            columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(tracked_companies)")}
            instrument = "instrument_type" if "instrument_type" in columns else "NULL"
            rows = conn.execute(
                f"SELECT ticker,name,list_type,sec_validated,filing_regime,{instrument} AS instrument_type FROM tracked_companies WHERE archived_at IS NULL ORDER BY CASE list_type WHEN 'portfolio' THEN 1 WHEN 'evaluation' THEN 2 WHEN 'watchlist' THEN 3 ELSE 4 END,ticker"
            ).fetchall()
            if not rows:
                return SecCoverageSummaryView(state="empty")
            wired = tables >= _EVIDENCE_TABLES
            companies: list[SecCoverageCompanyView] = []
            for row in rows:
                role = str(row["list_type"])
                ticker = str(row["ticker"])
                try:
                    allowed = decision_for(
                        role, CollectionSource.SEC, ArtifactKind.FILING_PACKAGE, requested=False
                    ).allowed
                except ValueError:
                    allowed = False
                applicable = instrument_allows_artifact(
                    row["instrument_type"],
                    source=CollectionSource.SEC,
                    artifact_kind=ArtifactKind.FILING_PACKAGE,
                )
                inventory, documents = (
                    _observed(conn, ticker, now) if wired and allowed and applicable else (None, ())
                )
                native = [doc for doc in documents if doc.source == "SEC native filing"]
                captured = sum(doc.state in {"captured", "extracted", "indexed"} for doc in native)
                aggregates = sum(
                    doc.source == "SEC CompanyFacts" and doc.state == "captured"
                    for doc in documents
                )
                status = "Partial"
                notes = (
                    "Observed capture and required inventory are separate; unresolved work remains."
                )
                tone: Literal["ok", "warn", "bad"] = "warn"
                if not allowed or not applicable:
                    status = "Disabled"
                    notes = (
                        "Corporate SEC collection is not authorized for this role or instrument."
                    )
                elif not wired:
                    status = "Not yet wired"
                    notes = "Coverage storage is absent; eligibility does not establish observed coverage."
                elif not bool(row["sec_validated"]):
                    status = "Unavailable"
                    notes = "Issuer SEC identity is unvalidated; no complete-coverage claim is admitted."
                elif inventory is None:
                    status = "Unavailable"
                    notes = "No governed SEC inventory; required filing population is unknown."
                else:
                    age = _age(str(inventory["completed_at"]), now)
                    if age is None or _age(str(inventory["recorded_at"]), now) is None:
                        status = "Unavailable"
                        notes = "Inventory observation time is invalid or in the future."
                    elif (
                        inventory["completion_status"] == "complete"
                        and inventory["outcome"] == "succeeded"
                        and bool(inventory["authoritative"])
                        and captured == len(native)
                        and aggregates == 1
                    ):
                        status = "Covered / freshness unknown"
                        notes = "Sealed inventory and exact-byte capture/locator proof cover the listed population. No issuer-specific freshness policy is available; extraction completeness is separate."
                execution_state: Literal["available", "empty", "unavailable", "not_yet_wired"] = (
                    "not_yet_wired"
                )
                executions: tuple[SecExecutionView, ...] = ()
                if "sec_execution_receipts" in tables:
                    try:
                        receipts = read_sec_executions(conn, ticker=ticker)
                        execution_state = "available" if receipts else "empty"
                        population = {doc.record_id for doc in native}
                        current_snapshot = (
                            str(inventory["snapshot_id"]) if inventory is not None else None
                        )
                        executions = tuple(
                            SecExecutionView(
                                receipt=receipt,
                                population_matches=_execution_matches(
                                    conn, receipt, ticker, current_snapshot, population
                                ),
                                timestamp_valid=_age(receipt.recorded_at.isoformat(), now)
                                is not None,
                            )
                            for receipt in receipts
                        )
                    except (sqlite3.Error, ValueError):
                        execution_state = "unavailable"
                companies.append(
                    SecCoverageCompanyView(
                        ticker=ticker,
                        name=str(row["name"] or ticker),
                        role=role.capitalize(),
                        sec_validated=bool(row["sec_validated"]),
                        filing_regime=str(row["filing_regime"] or "unknown"),
                        coverage_status=status,
                        coverage_tone=tone,
                        notes=notes,
                        acquisition_mode="automatic" if allowed and applicable else "disabled",
                        inventory_id=str(inventory["snapshot_id"])
                        if inventory is not None
                        else None,
                        inventory_observed_at=str(inventory["completed_at"])
                        if inventory is not None
                        else None,
                        inventory_state=str(inventory["completion_status"] or "unsealed")
                        if inventory is not None
                        else "unavailable",
                        expected_native_count=len(native),
                        captured_native_count=captured,
                        companyfacts_snapshot_count=aggregates,
                        documents=documents,
                        execution_state=execution_state,
                        executions=executions,
                    )
                )
            return SecCoverageSummaryView(
                state="available" if wired else "not_yet_wired",
                total_tracked=len(rows),
                portfolio_count=sum(row["list_type"] == "portfolio" for row in rows),
                evaluation_count=sum(row["list_type"] == "evaluation" for row in rows),
                watchlist_count=sum(row["list_type"] == "watchlist" for row in rows),
                validated_count=sum(bool(row["sec_validated"]) for row in rows),
                gap_count=sum(
                    item.coverage_status not in {"Covered / freshness unknown", "Disabled"}
                    for item in companies
                ),
                execution_gap_count=sum(
                    item.execution_state == "unavailable"
                    or any(
                        not execution.timestamp_valid
                        or (execution.population_matches and execution.receipt.state != "succeeded")
                        for execution in item.executions
                    )
                    for item in companies
                    if item.acquisition_mode == "automatic"
                ),
                companies=tuple(companies),
            )
        finally:
            conn.close()
    except (OSError, sqlite3.Error, ValueError):
        return SecCoverageSummaryView()
