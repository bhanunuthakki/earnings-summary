"""Verify current source populations before selecting reported fiscal quarters.

Source policy defines the window state and fiscal calendar. This owner reads
retained evidence, seals, and source reviews under one SQLite snapshot.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import url2pathname

from pipeline.source_policy import (
    SOURCE_POLICY_CONFIG,
    ReportedPeriodStatus,
    ReportedPeriodWindow,
    fiscal_quarter_end,
)
from runtime import deterministic
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite


def _captured_reported_period_proof(
    conn: sqlite3.Connection,
    *,
    item: object,
    ticker: str,
    as_of: date,
) -> bool:
    """Require a current captured version and an independently located period witness."""
    from pipeline.issuer_document_inventory import (
        ExpectedIssuerDocument,
        FiscalPeriodWitness,
        IssuerDocumentInventoryError,
        IssuerDocumentInventoryRequest,
        current_reported_period_review_identity,
        reviewable_current_reported_period_source,
        validate_fiscal_period_witness,
        verified_current_reported_period_review,
    )
    from provenance.source_coverage import ExpectedDocument

    expected = ExpectedDocument.model_validate(item)
    if expected.period_end is None or expected.source_url is None:
        return False
    coverage = conn.execute(
        "SELECT coverage_status,document_version_id,reason_details_json "
        "FROM v_source_coverage_current WHERE expected_document_id=?",
        (expected.expected_document_id,),
    ).fetchone()
    if (
        coverage is None
        or coverage[0] not in {"captured", "extracted", "indexed"}
        or not coverage[1]
    ):
        return False
    version = conn.execute(
        "SELECT document_version_id,issuer_id,ticker,document_type,form_type,"
        "blob_sha256,observation_id,period_end,recorded_at "
        "FROM v_evidence_document_versions_canonical WHERE document_version_id=?",
        (coverage[1],),
    ).fetchone()
    legacy_press_release = (
        (
            expected.source_kind == "ir_document"
            and expected.document_type == "earnings_release"
            and (version[3], version[4]) == ("ir_press_release", "ir_press_release")
        )
        if version is not None
        else False
    )
    if (
        version is None
        or (version[1], version[2]) != (expected.issuer_id, ticker)
        or (expected.source_kind != "ir_document" and version[4] != expected.form_type)
        or (
            expected.source_kind == "ir_document"
            and (
                expected.form_type is not None or (version[4] != "IR" and not legacy_press_release)
            )
        )
        or (
            version[3] != expected.document_type
            and not (
                expected.source_kind == "ir_document"
                and expected.document_type == "earnings_release"
                and version[3] in {"ir_document", "ir_press_release"}
            )
        )
        or (
            datetime.fromisoformat(str(version[7])).date() != expected.period_end.date()
            if version[7] is not None
            else expected.source_kind != "ir_document"
        )
    ):
        return False
    observation = conn.execute(
        "SELECT source_url,source_kind,blob_sha256,retrieved_at FROM evidence_source_observations "
        "WHERE observation_id=?",
        (version[6],),
    ).fetchone()
    blob = conn.execute(
        "SELECT storage_uri,byte_size FROM evidence_content_blobs WHERE sha256=?",
        (version[5],),
    ).fetchone()
    if (
        observation is None
        or blob is None
        or observation[0] != expected.source_url
        or (
            observation[1] != expected.source_kind
            and not (legacy_press_release and observation[1] == "ir_doc")
        )
        or (
            expected.source_kind == "ir_document"
            and expected.document_type == "earnings_release"
            and not reviewable_current_reported_period_source(observation[1], version[3])
        )
        or observation[2] != version[5]
        or datetime.fromisoformat(str(observation[3])).date() > as_of
        or datetime.fromisoformat(str(version[8])).date() > as_of
    ):
        return False
    location = conn.execute(
        "SELECT availability_state,verified_sha256,verified_byte_size FROM "
        "evidence_blob_location_observations WHERE blob_sha256=? AND storage_uri=? "
        "ORDER BY location_sequence DESC LIMIT 1",
        (version[5], blob[0]),
    ).fetchone()
    if location is None or tuple(location) != ("present", version[5], blob[1]):
        return False
    uri = urlsplit(str(blob[0]))
    if uri.scheme != "file" or uri.netloc not in {"", "localhost"}:
        return False
    try:
        raw = Path(url2pathname(uri.path)).read_bytes()
    except OSError:
        return False
    if len(raw) != int(blob[1]) or hashlib.sha256(raw).hexdigest() != version[5]:
        return False

    cutoff = datetime.combine(as_of, time.max, tzinfo=UTC)
    profiles = conn.execute(
        "SELECT profile_revision_id,fiscal_year_end FROM issuer_profile_revisions "
        "WHERE issuer_id=? AND status='active' ORDER BY revision DESC LIMIT 1",
        (expected.issuer_id,),
    ).fetchall()
    if len(profiles) != 1:
        return False
    profile_id, fiscal_year_end = str(profiles[0][0]), str(profiles[0][1])
    periods = [
        (year, quarter)
        for year in (expected.period_end.year, expected.period_end.year + 1)
        for quarter in range(1, 5)
        if fiscal_quarter_end(
            fiscal_year=year, fiscal_quarter=quarter, fiscal_year_end=fiscal_year_end
        )
        == expected.period_end.date()
    ]
    if len(periods) != 1:
        return False
    year, quarter = periods[0]
    try:
        request = IssuerDocumentInventoryRequest(
            ticker=ticker,
            fiscal_year=year,
            fiscal_quarter=quarter,
            period_end=expected.period_end.date(),
            expected_documents=(
                ExpectedIssuerDocument(
                    source_url=expected.source_url, document_type=expected.document_type
                ),
            ),
            fiscal_period_witness=FiscalPeriodWitness(
                issuer_id=expected.issuer_id,
                profile_revision_id=profile_id,
                fiscal_year_end=fiscal_year_end,
                document_version_id=str(version[0]),
                document_sha256=str(version[5]),
                knowledge_cutoff=cutoff,
                observed_through=cutoff,
            ),
        )
        validate_fiscal_period_witness(conn, request=request)
    except (ValueError, IssuerDocumentInventoryError, sqlite3.Error):
        return False

    if expected.source_kind == "ir_document" and expected.document_type == "earnings_release":
        review = verified_current_reported_period_review(
            conn,
            document_version_id=str(version[0]),
            source_sha256=str(version[5]),
            period_end=expected.period_end.date(),
        )
        if (
            review is None
            or review.issuer_id != expected.issuer_id
            or review.ticker != ticker
            or review.profile_revision_id != profile_id
            or review.reviewed_at.date() > as_of
            or review.knowledge_cutoff.date() > as_of
            or review.observed_through.date() > as_of
        ):
            return False
        node_id, review_sha = current_reported_period_review_identity(review)
        try:
            coverage_details = json.loads(str(coverage[2]))
        except (TypeError, ValueError):
            return False
        if not isinstance(coverage_details, dict):
            return False
        if (
            coverage_details.get("imported_detail_native_period_document_version_id") != version[0]
            or coverage_details.get("imported_detail_native_period_review_node_id") != node_id
            or coverage_details.get("imported_detail_native_period_review_sha256") != review_sha
        ):
            return False
    return True


def _reported_period_window_from_inventory_unpinned(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    as_of: date,
    max_quarters: int = SOURCE_POLICY_CONFIG.reported_quarter_window.max_quarters,
) -> ReportedPeriodWindow:
    """Verify current SEC and IR source populations before selecting reported quarters.

    Missing period evidence remains unknown. A quarter control, crawl date, or
    inferred calendar end cannot establish publication or close the window.
    """

    from ir_pipeline.source_inventory import reviewed_document_type, reviewed_period_candidate
    from pipeline.issuer_document_inventory import reviewed_current_period_for_url
    from provenance.source_coverage import ExpectedDocument, SourceInventorySnapshot
    from provenance.source_inventory_seal import (
        InventoryComponent,
        InventorySeal,
        component_digest,
    )

    if max_quarters < 1:
        raise ValueError("max_quarters must be positive")
    try:
        current = conn.execute(
            "SELECT * FROM v_source_inventory_current WHERE UPPER(ticker)=? "
            "AND source_kind IN ('sec_submissions','ir_crawl')",
            (ticker.strip().upper(),),
        )
    except sqlite3.OperationalError:
        return ReportedPeriodWindow(
            status=ReportedPeriodStatus.UNKNOWN, reason="source_inventory_unavailable"
        )
    columns = tuple(item[0] for item in current.description or ())
    rows = [dict(zip(columns, row, strict=True)) for row in current.fetchall()]
    issuers = {str(row["issuer_id"]) for row in rows}
    if len(issuers) > 1:
        return ReportedPeriodWindow(status=ReportedPeriodStatus.AMBIGUOUS, reason="issuer_identity")
    by_kind: dict[str, dict[str, object]] = {}
    for row in rows:
        kind = str(row["source_kind"])
        if kind in by_kind:
            return ReportedPeriodWindow(
                status=ReportedPeriodStatus.AMBIGUOUS, reason="inventory_population"
            )
        by_kind[kind] = row
    if set(by_kind) != {"sec_submissions", "ir_crawl"}:
        return ReportedPeriodWindow(
            status=ReportedPeriodStatus.UNKNOWN, reason="source_inventory_missing"
        )

    end_of_day = datetime.combine(as_of + timedelta(days=1), time.min, tzinfo=UTC)
    snapshots: list[str] = []
    reported: set[date] = set()
    period_documents: list[ExpectedDocument] = []
    seal_proofs: list[tuple[str, str, str]] = []
    coverage_proofs: list[tuple[str, str, str, str]] = []
    for kind in ("sec_submissions", "ir_crawl"):
        snapshot = SourceInventorySnapshot.model_validate(by_kind[kind])
        snapshots.append(snapshot.snapshot_id)
        seal_row = conn.execute(
            "SELECT * FROM source_inventory_snapshot_seals WHERE snapshot_id=?",
            (snapshot.snapshot_id,),
        ).fetchone()
        if seal_row is None:
            return ReportedPeriodWindow(
                status=ReportedPeriodStatus.STALE, reason="inventory_unsealed"
            )
        seal_columns = tuple(
            item[1] for item in conn.execute("PRAGMA table_info(source_inventory_snapshot_seals)")
        )
        seal = InventorySeal.model_validate(dict(zip(seal_columns, seal_row, strict=True)))
        recorded_at = snapshot.recorded_at
        sealed_at = seal.sealed_at
        if (
            not snapshot.authoritative
            or snapshot.outcome != "succeeded"
            or seal.completion_status != "complete"
            or (
                recorded_at.replace(tzinfo=UTC)
                if recorded_at.tzinfo is None
                else recorded_at.astimezone(UTC)
            )
            >= end_of_day
            or (
                sealed_at.replace(tzinfo=UTC)
                if sealed_at.tzinfo is None
                else sealed_at.astimezone(UTC)
            )
            >= end_of_day
        ):
            return ReportedPeriodWindow(
                status=ReportedPeriodStatus.STALE, reason="inventory_not_current_complete"
            )
        component_cursor = conn.execute(
            "SELECT * FROM source_inventory_components WHERE snapshot_id=? "
            "ORDER BY ordinal,component_key",
            (snapshot.snapshot_id,),
        )
        component_columns = tuple(item[0] for item in component_cursor.description or ())
        components = tuple(
            InventoryComponent.model_validate(dict(zip(component_columns, row, strict=True)))
            for row in component_cursor.fetchall()
        )
        if (
            len(components) != seal.expected_component_count
            or component_digest(components) != seal.component_digest_sha256
            or any(item.required and item.outcome != "succeeded" for item in components)
        ):
            return ReportedPeriodWindow(
                status=ReportedPeriodStatus.AMBIGUOUS, reason="inventory_seal_mismatch"
            )
        seal_proofs.append(
            (snapshot.snapshot_id, seal.component_digest_sha256, seal.sealed_at.isoformat())
        )
        document_cursor = conn.execute(
            "SELECT * FROM expected_documents WHERE snapshot_id=?",
            (snapshot.snapshot_id,),
        )
        document_columns = tuple(item[0] for item in document_cursor.description or ())
        documents = tuple(
            ExpectedDocument.model_validate(dict(zip(document_columns, row, strict=True)))
            for row in document_cursor.fetchall()
        )
        if not documents or any(
            item.issuer_id != snapshot.issuer_id
            or item.source_kind != ("sec_filing" if kind == "sec_submissions" else "ir_document")
            for item in documents
        ):
            return ReportedPeriodWindow(
                status=ReportedPeriodStatus.AMBIGUOUS, reason="document_population"
            )
        for item in documents:
            if kind == "sec_submissions":
                form = (item.form_type or "").upper()
                if form.removesuffix("/A") not in {"10-Q", "10-K", "20-F", "40-F"}:
                    continue
                if item.filing_at is None or item.period_end is None:
                    return ReportedPeriodWindow(
                        status=ReportedPeriodStatus.UNKNOWN, reason="sec_period_unqualified"
                    )
                filing_at = item.filing_at
                if (
                    filing_at.replace(tzinfo=UTC)
                    if filing_at.tzinfo is None
                    else filing_at.astimezone(UTC)
                ) >= end_of_day:
                    continue
            elif item.period_end is None:
                return ReportedPeriodWindow(
                    status=ReportedPeriodStatus.UNKNOWN, reason="ir_period_unqualified"
                )
            else:
                if item.source_url is None:
                    return ReportedPeriodWindow(
                        status=ReportedPeriodStatus.UNKNOWN, reason="ir_period_proof_missing"
                    )
                witness = reviewed_period_candidate(
                    conn,
                    issuer_id=snapshot.issuer_id,
                    ticker=ticker.strip().upper(),
                    url=item.source_url,
                    recorded_at=snapshot.recorded_at,
                )
                native = reviewed_current_period_for_url(
                    conn,
                    issuer_id=snapshot.issuer_id,
                    ticker=ticker.strip().upper(),
                    source_url=item.source_url,
                    observed_through=snapshot.recorded_at,
                )
                if item.document_type != "earnings_release":
                    # Only an exact reviewed non-release type can rule this
                    # document out as a newer reported quarter. An unreviewed
                    # ir_document can still be an earnings release.
                    if (
                        item.document_type
                        not in {"presentation", "earnings_material", "earnings_transcript"}
                        or witness is None
                        or witness.quarter_end != item.period_end.date()
                        or reviewed_document_type(witness) != item.document_type
                        or native is not None
                    ):
                        return ReportedPeriodWindow(
                            status=ReportedPeriodStatus.UNKNOWN, reason="ir_period_proof_missing"
                        )
                    continue
                approved_matches = (
                    witness is not None
                    and witness.quarter_end == item.period_end.date()
                    and reviewed_document_type(witness) == item.document_type
                )
                native_matches = (
                    native is not None
                    and native.period_end == item.period_end.date()
                    and item.document_type == "earnings_release"
                )
                if not approved_matches and not native_matches:
                    return ReportedPeriodWindow(
                        status=ReportedPeriodStatus.UNKNOWN, reason="ir_period_proof_missing"
                    )
                if (
                    witness is not None
                    and native is not None
                    and witness.quarter_end != native.period_end
                ):
                    return ReportedPeriodWindow(
                        status=ReportedPeriodStatus.AMBIGUOUS, reason="ir_period_proof_conflict"
                    )
            if item.period_end.date() > as_of:
                return ReportedPeriodWindow(
                    status=ReportedPeriodStatus.AMBIGUOUS, reason="future_reported_period"
                )
            period_documents.append(item)
            if (
                kind == "sec_submissions"
                and (item.form_type or "").upper().removesuffix("/A") == "10-Q"
            ) or (kind == "ir_crawl" and item.document_type == "earnings_release"):
                reported.add(item.period_end.date())
    if len(reported) < max_quarters:
        return ReportedPeriodWindow(
            status=ReportedPeriodStatus.UNKNOWN, reason="reported_period_population_short"
        )
    selected_periods = tuple(sorted(reported, reverse=True)[:max_quarters])
    for item in period_documents:
        if item.period_end is None or item.period_end.date() not in selected_periods:
            continue
        if not _captured_reported_period_proof(
            conn,
            item=item,
            ticker=ticker.strip().upper(),
            as_of=as_of,
        ):
            return ReportedPeriodWindow(
                status=ReportedPeriodStatus.UNKNOWN,
                reason="captured_period_proof_missing",
            )
        coverage_proof = conn.execute(
            "SELECT assessment_id,document_version_id,reason_details_json "
            "FROM v_source_coverage_current WHERE expected_document_id=?",
            (item.expected_document_id,),
        ).fetchone()
        if coverage_proof is None:
            return ReportedPeriodWindow(
                status=ReportedPeriodStatus.UNKNOWN,
                reason="coverage_proof_missing",
            )
        coverage_proofs.append(
            (
                item.expected_document_id,
                str(coverage_proof[0]),
                str(coverage_proof[1]),
                str(coverage_proof[2]),
            )
        )
    profile_rows = conn.execute(
        "SELECT fiscal_year_end FROM issuer_profile_revisions "
        "WHERE issuer_id=? AND status='active' ORDER BY revision DESC LIMIT 1",
        (next(iter(issuers)),),
    ).fetchall()
    if len(profile_rows) != 1:
        return ReportedPeriodWindow(
            status=ReportedPeriodStatus.UNKNOWN, reason="fiscal_profile_unavailable"
        )
    fiscal_year_end = str(profile_rows[0][0])
    proof_sha256 = hashlib.sha256(
        deterministic.canonical_json_ascii(
            {
                "seal_proofs": seal_proofs,
                "coverage_proofs": sorted(coverage_proofs),
                "selected_periods": [item.isoformat() for item in selected_periods],
                "fiscal_year_end": fiscal_year_end,
            }
        ).encode("utf-8")
    ).hexdigest()
    return ReportedPeriodWindow(
        status=ReportedPeriodStatus.COMPLETE,
        period_ends=selected_periods,
        snapshot_ids=tuple(snapshots),
        fiscal_year_end=fiscal_year_end,
        proof_sha256=proof_sha256,
        reason="sealed_source_population",
    )


def reported_period_window_from_inventory(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    as_of: date,
    max_quarters: int = SOURCE_POLICY_CONFIG.reported_quarter_window.max_quarters,
) -> ReportedPeriodWindow:
    """Read the complete proof under one SQLite snapshot when the caller owns no transaction."""
    own_snapshot = not conn.in_transaction
    if own_snapshot:
        conn.execute("BEGIN")
        conn.execute("SELECT 1 FROM sqlite_schema LIMIT 1").fetchone()
    try:
        return _reported_period_window_from_inventory_unpinned(
            conn, ticker=ticker, as_of=as_of, max_quarters=max_quarters
        )
    finally:
        if own_snapshot:
            conn.rollback()


def reported_quarter_selection_in_connection(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    fiscal_year: int,
    fiscal_quarter: int,
    fiscal_year_end_month: int | None,
    as_of: date,
) -> bool | None:
    """Return true/false only with a complete source window; otherwise pending."""

    if fiscal_year_end_month is None:
        return None
    try:
        following = (
            date(fiscal_year + 1, 1, 1)
            if fiscal_year_end_month == 12
            else date(fiscal_year, fiscal_year_end_month + 1, 1)
        )
        fiscal_year_end = date.fromordinal(following.toordinal() - 1).strftime("%m-%d")
        period_end = fiscal_quarter_end(
            fiscal_year=fiscal_year,
            fiscal_quarter=fiscal_quarter,
            fiscal_year_end=fiscal_year_end,
        )
    except ValueError:
        return None
    return reported_period_window_from_inventory(conn, ticker=ticker, as_of=as_of).includes(
        period_end
    )


def reported_quarter_selection(
    db_path: Path,
    *,
    ticker: str,
    fiscal_year: int,
    fiscal_quarter: int,
    fiscal_year_end_month: int | None,
    as_of: date,
) -> bool | None:
    """Read-only source selection for entrypoints without a caller-owned connection."""

    try:
        conn = connect_sqlite(db_path, role=SQLiteConnectionRole.READ_ONLY)
    except (OSError, sqlite3.Error):
        return None
    try:
        return reported_quarter_selection_in_connection(
            conn,
            ticker=ticker,
            fiscal_year=fiscal_year,
            fiscal_quarter=fiscal_quarter,
            fiscal_year_end_month=fiscal_year_end_month,
            as_of=as_of,
        )
    finally:
        conn.close()
