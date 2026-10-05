"""Source-bound SEC identity and corporate metadata for new active tickers.

Metadata authorization precedes requests. The SEC ticker registry proves CIK,
submissions prove reporting metadata, and inline-XBRL cover facts can prove
the requested security kind. Missing or conflicting evidence stays explicit.
This adapter neither parses financial facts nor certifies archive completeness.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal, cast

from bs4 import BeautifulSoup

from net.client import HTTP_CLIENT, HttpCallError, RetryPolicy
from pipeline.fmp_doc_index import classify_filing_regime_from_sec_forms
from pipeline.sec_xbrl import NO_SEC_FILERS, resolve_companyfacts_cik
from pipeline.source_policy import (
    ArtifactKind,
    CollectionSource,
    authorize_collection_target_in_connection,
)
from provenance.issuer_registry import IssuerProfileRevision, IssuerRegistry
from provenance.issuer_registry_bootstrap import (
    SEC_COMPANY_TICKERS_URL,
    BootstrapRequest,
    bootstrap_issuer_reporting_registry,
    capture_sec_authority_metadata,
    parse_sec_company_tickers,
)
from schema_compat import require_current_for_write
from sec_identity import sec_user_agent


class IdentityStatus(StrEnum):
    READY = "ready"
    MISSING_SOURCE_METADATA = "missing_source_metadata"
    IDENTITY_CONFLICT = "source_identity_conflict"
    FETCH_FAILED = "metadata_fetch_failed"
    POLICY_DENIED = "metadata_policy_denied"
    NOT_APPLICABLE = "not_applicable"


@dataclass(frozen=True)
class MetadataReference:
    source_url: str
    source_observation_id: str
    source_sha256: str
    locator: str


@dataclass(frozen=True)
class SecOnboardingIdentityResult:
    status: IdentityStatus
    detail: str
    cik: str | None = None
    instrument_type: Literal["equity", "adr"] | None = None
    filing_regime: str | None = None
    fiscal_year_end: str | None = None
    sources: tuple[MetadataReference, ...] = ()


@dataclass(frozen=True)
class _Submissions:
    name: str
    entity_type: str
    fiscal_year_end: str | None
    regime: str | None
    cover_url: str | None


def _fetch_bytes(url: str) -> bytes:
    response = HTTP_CLIENT.request(
        "GET",
        url,
        headers={"User-Agent": sec_user_agent()},
        timeout=(10, 60),
        retry=RetryPolicy(max_attempts=1),
    )
    body = bytes(response.content)
    if not body or len(body) > 32_000_000:
        raise ValueError("SEC identity metadata exceeds the bounded response contract")
    return body


def _object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    values: dict[str, object] = {}
    for key, value in pairs:
        if key in values:
            raise ValueError("SEC submissions contains duplicate JSON keys")
        values[key] = value
    return values


def _parse_submissions(raw: bytes, *, cik: str, ticker: str) -> _Submissions:
    decoded: object = json.loads(raw, object_pairs_hook=_object)
    if not isinstance(decoded, dict):
        raise ValueError("SEC submissions requires an object")
    payload = cast("dict[str, object]", decoded)
    raw_cik = payload.get("cik")
    if not isinstance(raw_cik, (int, str)) or isinstance(raw_cik, bool):
        raise ValueError("SEC submissions has no valid issuer CIK")
    if str(raw_cik).zfill(10) != cik:
        raise ValueError("SEC submissions conflicts with the registered CIK")
    tickers = payload.get("tickers")
    if not isinstance(tickers, list) or ticker not in tickers:
        raise ValueError("SEC submissions does not identify the requested current ticker")
    name = payload.get("name")
    entity = payload.get("entityType")
    if not isinstance(name, str) or not name.strip() or not isinstance(entity, str):
        raise ValueError("SEC submissions lacks typed issuer metadata")
    fye: str | None = None
    fiscal = payload.get("fiscalYearEnd")
    if fiscal not in (None, ""):
        if not isinstance(fiscal, str) or re.fullmatch(r"[0-9]{4}", fiscal) is None:
            raise ValueError("SEC fiscalYearEnd must use exact MMDD format")
        date(2000, int(fiscal[:2]), int(fiscal[2:]))
        fye = f"{fiscal[:2]}-{fiscal[2:]}"
    filings = payload.get("filings")
    if not isinstance(filings, dict):
        return _Submissions(name, entity, fye, None, None)
    recent = cast("dict[str, object]", filings).get("recent")
    if not isinstance(recent, dict):
        return _Submissions(name, entity, fye, None, None)
    recent_map = cast("dict[str, object]", recent)
    columns: dict[str, list[str]] = {}
    for field in ("form", "accessionNumber", "primaryDocument", "filingDate"):
        column = recent_map.get(field)
        if not isinstance(column, list):
            raise ValueError("SEC submissions recent metadata column changed")
        raw_column = cast("list[object]", column)
        if any(not isinstance(item, str) for item in raw_column):
            raise ValueError("SEC submissions recent metadata column changed")
        columns[field] = cast("list[str]", raw_column)
    if len({len(column) for column in columns.values()}) != 1:
        raise ValueError("SEC submissions recent metadata columns differ in length")
    dates = [date.fromisoformat(value) for value in columns["filingDate"]]
    order = sorted(range(len(dates)), key=lambda index: dates[index], reverse=True)
    forms = [columns["form"][index] for index in order]
    regime = classify_filing_regime_from_sec_forms(forms)
    if regime is None and any(form in {"10-Q", "10-Q/A"} for form in forms):
        regime = "10-K"  # An exact US quarterly form establishes the US periodic regime.
    cover_url: str | None = None
    for index in order:
        if columns["form"][index] not in {
            "10-K",
            "10-K/A",
            "10-Q",
            "10-Q/A",
            "20-F",
            "20-F/A",
            "40-F",
            "40-F/A",
        }:
            continue
        accession = columns["accessionNumber"][index]
        document = columns["primaryDocument"][index]
        if re.fullmatch(r"[0-9]{10}-[0-9]{2}-[0-9]{6}", accession) is None:
            raise ValueError("SEC metadata accession is malformed")
        if re.fullmatch(r"[A-Za-z0-9_.-]+\.html?", document) is None:
            raise ValueError("SEC primary cover filename is malformed")
        cover_url = (
            f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/"
            f"{accession.replace('-', '')}/{document}"
        )
        break
    return _Submissions(name.strip(), entity.strip(), fye, regime, cover_url)


def _classify_cover(
    raw: bytes, *, cik: str, ticker: str
) -> tuple[Literal["equity", "adr"], str, str] | None:
    soup = BeautifulSoup(raw, "html.parser")
    html = soup.find("html")
    if html is None:
        return None
    prefix = next(
        (
            key[6:]
            for key, uri in html.attrs.items()
            if key.startswith("xmlns:")
            and isinstance(uri, str)
            and re.fullmatch(r"https?://xbrl\.sec\.gov/dei/[0-9-]+", uri)
        ),
        None,
    )
    if prefix is None:
        return None
    values: dict[tuple[str, str], list[str]] = {}
    for fact in soup.find_all("ix:nonnumeric"):
        name = fact.get("name")
        context = fact.get("contextref")
        if isinstance(name, str) and isinstance(context, str) and name.startswith(f"{prefix}:"):
            key = (name.split(":", 1)[1], context)
            values.setdefault(key, []).append(" ".join(fact.get_text(" ", strip=True).split()))
    ciks = [
        value
        for (name, _context), items in values.items()
        if name == "EntityCentralIndexKey"
        for value in items
    ]
    if not ciks or any(value.zfill(10) != cik for value in ciks):
        raise ValueError("SEC security cover does not prove the requested issuer CIK")
    titles = {
        title
        for (name, context), symbols in values.items()
        if name == "TradingSymbol" and ticker in symbols
        for title in values.get(("Security12bTitle", context), ())
    }
    if len(titles) != 1:
        return None
    title = next(iter(titles))
    folded = title.casefold()
    if re.search(r"\bamerican depositary (?:shares|receipts)\b", folded):
        return "adr", title, "dei:Security12bTitle+dei:TradingSymbol/same-context"
    if re.search(r"\b(?:common (?:stock|shares)|ordinary shares)\b", folded) and not re.search(
        r"\b(?:preferred|warrants?|units?|depositary|receipts?)\b", folded
    ):
        return "equity", title, "dei:Security12bTitle+dei:TradingSymbol/same-context"
    return None


def ensure_sec_onboarding_identity(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    project_root: Path,
    knowledge_at: datetime | None = None,
    fetch: Callable[[str], bytes] | None = None,
) -> SecOnboardingIdentityResult:
    """Fill only proven, non-conflicting metadata for one active ticker."""
    upper = ticker.strip().upper()
    stamp = knowledge_at or datetime.now(UTC)
    authorization = authorize_collection_target_in_connection(
        conn,
        upper,
        requested=False,
        source=CollectionSource.SEC,
        artifact_kind=ArtifactKind.METADATA,
    )
    if not authorization.allowed:
        return SecOnboardingIdentityResult(IdentityStatus.POLICY_DENIED, authorization.status.value)
    try:
        require_current_for_write(conn)
        row = conn.execute(
            "SELECT instrument_type,filing_regime,fiscal_year_end FROM tracked_companies "
            "WHERE UPPER(ticker)=? AND archived_at IS NULL",
            (upper,),
        ).fetchone()
    except (RuntimeError, sqlite3.Error):
        return SecOnboardingIdentityResult(
            IdentityStatus.IDENTITY_CONFLICT, "metadata schema unavailable"
        )
    if row is None:
        return SecOnboardingIdentityResult(
            IdentityStatus.POLICY_DENIED, "active metadata unavailable"
        )
    if row[0] == "etf" or upper in NO_SEC_FILERS:
        return SecOnboardingIdentityResult(
            IdentityStatus.NOT_APPLICABLE, "separate ETF or non-filer lane"
        )
    get = fetch or _fetch_bytes
    references: list[MetadataReference] = []
    blob_root = project_root / "data/evidence/blobs"
    try:
        registry = IssuerRegistry(conn)
        binding = conn.execute(
            "SELECT outcome,material_dissent,reason_code FROM legacy_issuer_binding_revisions "
            "WHERE recorded_issuer_id=? ORDER BY revision DESC LIMIT 1",
            (f"legacy-ticker:{upper}",),
        ).fetchone()
        if binding is not None and (
            binding[1] or (binding[0] != "selected" and binding[2] != "sec_ticker_missing")
        ):
            return SecOnboardingIdentityResult(
                IdentityStatus.IDENTITY_CONFLICT, "existing issuer binding unresolved or disputed"
            )
        raw_tickers: bytes | None = None
        if binding is None or binding[0] != "selected":
            raw_tickers = get(SEC_COMPANY_TICKERS_URL)
            ticker_capture = capture_sec_authority_metadata(
                conn,
                raw_body=raw_tickers,
                request=BootstrapRequest(
                    source_url=SEC_COMPANY_TICKERS_URL,
                    blob_root=blob_root,
                    apply=True,
                    recorded_at=stamp,
                ),
                source_kind="sec_company_tickers",
            )
            references.append(
                MetadataReference(
                    SEC_COMPANY_TICKERS_URL,
                    ticker_capture.source_observation_id,
                    ticker_capture.source_sha256,
                    "ticker/unique-cik",
                )
            )
            conn.commit()
            entries = [
                entry for entry in parse_sec_company_tickers(raw_tickers) if entry.ticker == upper
            ]
            if len(entries) != 1:
                return SecOnboardingIdentityResult(
                    IdentityStatus.MISSING_SOURCE_METADATA
                    if not entries
                    else IdentityStatus.IDENTITY_CONFLICT,
                    "SEC ticker authority has no unique match",
                    sources=tuple(references),
                )
            cik = entries[0].normalized_cik
        else:
            cik = resolve_companyfacts_cik(conn, upper, knowledge_at=stamp)
        if (
            raw_tickers is None
            and row[0] in {"equity", "adr"}
            and row[1] in {"10-K", "20-F", "40-F"}
            and row[2]
        ):
            return SecOnboardingIdentityResult(
                IdentityStatus.READY,
                "existing stored corporate metadata",
                cik,
                cast(Literal["equity", "adr"], row[0]),
                str(row[1]),
                str(row[2]),
                tuple(references),
            )
        submissions_url = f"https://data.sec.gov/submissions/CIK{cik}.json"
        raw_submissions = get(submissions_url)
        capture = capture_sec_authority_metadata(
            conn,
            raw_body=raw_submissions,
            request=BootstrapRequest(
                source_url=submissions_url, blob_root=blob_root, apply=True, recorded_at=stamp
            ),
            source_kind="sec_submissions",
        )
        references.append(
            MetadataReference(
                submissions_url,
                capture.source_observation_id,
                capture.source_sha256,
                "$.entityType/$.fiscalYearEnd/$.filings.recent",
            )
        )
        conn.commit()
        # Parse after retention, so contract failures remain inspectable.
        metadata = _parse_submissions(raw_submissions, cik=cik, ticker=upper)
        if (
            metadata.entity_type != "operating"
            or not metadata.regime
            or not metadata.fiscal_year_end
        ):
            return SecOnboardingIdentityResult(
                IdentityStatus.MISSING_SOURCE_METADATA,
                "operating issuer, fiscal year end or reporting regime unproven",
                cik=cik,
                sources=tuple(references),
            )
        instrument: Literal["equity", "adr"] | None = (
            cast(Literal["equity", "adr"], row[0]) if row[0] in {"equity", "adr"} else None
        )
        title = "stored corporate instrument"
        locator = "stored instrument metadata"
        if instrument is None:
            if metadata.cover_url is None:
                return SecOnboardingIdentityResult(
                    IdentityStatus.MISSING_SOURCE_METADATA,
                    "security cover metadata unavailable",
                    cik=cik,
                    sources=tuple(references),
                )
            raw_cover = get(metadata.cover_url)
            cover = capture_sec_authority_metadata(
                conn,
                raw_body=raw_cover,
                request=BootstrapRequest(
                    source_url=metadata.cover_url,
                    blob_root=blob_root,
                    apply=True,
                    recorded_at=stamp,
                ),
                source_kind="sec_security_cover",
            )
            references.append(
                MetadataReference(
                    metadata.cover_url,
                    cover.source_observation_id,
                    cover.source_sha256,
                    "inline-XBRL cover",
                )
            )
            conn.commit()
            classified = _classify_cover(raw_cover, cik=cik, ticker=upper)
            if classified is None:
                return SecOnboardingIdentityResult(
                    IdentityStatus.MISSING_SOURCE_METADATA,
                    "requested security kind unproven in cover metadata",
                    cik=cik,
                    sources=tuple(references),
                )
            instrument, title, locator = classified
        expected = (instrument, metadata.regime, metadata.fiscal_year_end)
        if any(
            stored is not None and str(stored) != value
            for stored, value in zip(row, expected, strict=True)
        ):
            return SecOnboardingIdentityResult(
                IdentityStatus.IDENTITY_CONFLICT,
                "stored instrument/regime/fiscal metadata conflicts with SEC evidence",
                cik=cik,
                sources=tuple(references),
            )
        if raw_tickers is not None:
            bootstrap_issuer_reporting_registry(
                conn,
                raw_body=raw_tickers,
                request=BootstrapRequest(
                    source_url=SEC_COMPANY_TICKERS_URL,
                    blob_root=blob_root,
                    apply=True,
                    recorded_at=stamp,
                    ticker_scope=(upper,),
                ),
            )
            # Verify the exact imported authority again before admitting financial HTTP.
            if resolve_companyfacts_cik(conn, upper, knowledge_at=stamp) != cik:
                raise ValueError("SEC bootstrap changed the verified CIK")
        subject = registry.canonicalize_recorded_issuer(
            f"legacy-ticker:{upper}", knowledge_at=stamp
        )
        current = conn.execute(
            "SELECT profile_revision_id,revision,fiscal_year_end,filing_regime FROM issuer_profile_revisions WHERE issuer_id=? ORDER BY revision DESC LIMIT 1",
            (subject.issuer_id,),
        ).fetchone()
        if current is None:
            raise ValueError("canonical issuer profile is unavailable")
        if current[2] is not None and str(current[2]) != metadata.fiscal_year_end:
            return SecOnboardingIdentityResult(
                IdentityStatus.IDENTITY_CONFLICT,
                "canonical fiscal metadata conflicts with SEC evidence",
                cik=cik,
                sources=tuple(references),
            )
        if current[3] not in {None, "SEC", metadata.regime}:
            return SecOnboardingIdentityResult(
                IdentityStatus.IDENTITY_CONFLICT,
                "canonical reporting regime conflicts with SEC evidence",
                cik=cik,
                sources=tuple(references),
            )
        revision_id = (
            "sec-onboarding-profile:"
            + hashlib.sha256(
                (
                    subject.issuer_id + references[-1].source_sha256 + metadata.fiscal_year_end
                ).encode()
            ).hexdigest()
        )
        with conn:
            registry.persist(
                IssuerProfileRevision(
                    profile_revision_id=revision_id,
                    idempotency_key=revision_id,
                    issuer_id=subject.issuer_id,
                    revision=int(current[1]) + 1,
                    legal_name=metadata.name,
                    filing_regime=metadata.regime,
                    fiscal_year_end=metadata.fiscal_year_end,
                    status="active",
                    decision_kind="imported",
                    reason_code="sec_onboarding_metadata_import",
                    reason_details=(
                        ("source_observation_id", capture.source_observation_id),
                        ("security_source_observation_id", references[-1].source_observation_id),
                        ("security_title", title),
                        ("security_locator", locator),
                        ("instrument_type", instrument),
                        ("classification_policy", "sec-cover-security-kind-v1"),
                    ),
                    effective_at=stamp,
                    knowledge_at=stamp,
                    recorded_at=stamp,
                    supersedes_profile_revision_id=str(current[0]),
                )
            )
            conn.execute(
                "UPDATE tracked_companies SET instrument_type=COALESCE(instrument_type,?),filing_regime=COALESCE(filing_regime,?),fiscal_year_end=COALESCE(fiscal_year_end,?) WHERE UPPER(ticker)=? AND archived_at IS NULL",
                (*expected, upper),
            )
        return SecOnboardingIdentityResult(
            IdentityStatus.READY,
            "source-bound SEC corporate metadata",
            cik,
            instrument,
            metadata.regime,
            metadata.fiscal_year_end,
            tuple(references),
        )
    except HttpCallError:
        conn.rollback()
        return SecOnboardingIdentityResult(
            IdentityStatus.FETCH_FAILED,
            "bounded SEC metadata request failed",
            sources=tuple(references),
        )
    except (OSError, ValueError, RuntimeError, sqlite3.Error):
        conn.rollback()
        return SecOnboardingIdentityResult(
            IdentityStatus.IDENTITY_CONFLICT,
            "SEC identity metadata contract failed",
            sources=tuple(references),
        )
