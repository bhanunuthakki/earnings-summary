"""Immutable collection authorization and issuer acquisition policy."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import sqlite3
from datetime import date
from enum import StrEnum
from pathlib import Path
from urllib.parse import unquote, urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from models.companies import ListType
from models.documents import DocType
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite

POLICY_VERSION = "2026-08-12.2"
_DNS_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


def _canonical_dns_host(host: str) -> str | None:
    if host != host.casefold() or not host.isascii() or len(host) > 253:
        return None
    if host != host.strip() or host.endswith(".") or ".." in host:
        return None
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        return None
    labels = host.split(".")
    if len(labels) < 2 or any(_DNS_LABEL.fullmatch(label) is None for label in labels):
        return None
    return host


def _canonical_safe_path(raw_path: str) -> str | None:
    if not raw_path.startswith("/") or not raw_path.isascii():
        return None
    current = raw_path
    for _ in range(5):
        if any(ord(character) < 32 or ord(character) == 127 for character in current):
            return None
        if (
            "\\" in current
            or "//" in current
            or any(segment in {".", ".."} for segment in current.split("/"))
        ):
            return None
        decoded = unquote(current)
        if decoded == current:
            return current
        if decoded.count("/") != current.count("/") or "\\" in decoded:
            return None
        current = decoded
    return None


def canonical_https_url(url: str) -> tuple[str, str] | None:
    """Return strict ASCII DNS host and traversal-safe path for a plain HTTPS URL."""
    try:
        parsed = urlsplit(url)
        explicit_port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme != "https"
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or explicit_port is not None
        or parsed.fragment
    ):
        return None
    host = _canonical_dns_host(parsed.hostname)
    path = _canonical_safe_path(parsed.path or "/")
    if host is None or path is None:
        return None
    return host, path


class CollectionSource(StrEnum):
    SEC = "sec"
    IR = "ir"
    FMP = "fmp"
    TRANSCRIPT = "transcript"


class ArtifactKind(StrEnum):
    METADATA = "metadata"
    COMPANY_FACTS = "company_facts"
    FILING_PACKAGE = "filing_package"
    FILING_SECTION = "filing_section"
    FINANCIAL_FACT = "financial_fact"
    IR_DOCUMENT = "ir_document"
    TEXT_TRANSCRIPT = "text_transcript"
    WEBCAST = "webcast"


class CollectionMode(StrEnum):
    AUTOMATIC_FULL = "automatic_full"
    ON_DEMAND_FULL = "on_demand_full"
    METADATA_ONLY = "metadata_only"
    SCREENING_ONLY = "screening_only"
    CATALOG_ONLY = "catalog_only"


class AuthorizationReason(StrEnum):
    AUTOMATIC = "automatic"
    OWNER_REQUESTED = "owner_requested"
    METADATA_ALLOWED = "metadata_allowed"
    SCREENING_FACT_ALLOWED = "screening_fact_allowed"
    REQUEST_REQUIRED = "request_required"
    COVERAGE_DEPTH_DENIED = "coverage_depth_denied"
    WEBCAST_EXCLUDED = "webcast_excluded"
    SOURCE_ARTIFACT_MISMATCH = "source_artifact_mismatch"


class CompanyFactsRole(StrEnum):
    FACT_FEED_ONLY = "fact_feed_only"


class FilingForm(StrEnum):
    FORM_10K = "10-K"
    FORM_10Q = "10-Q"
    FORM_8K = "8-K"
    FORM_20F = "20-F"
    FORM_40F = "40-F"
    FORM_6K = "6-K"


class FilingSection(StrEnum):
    ITEM_1 = "item_1"
    ITEM_1A = "item_1a"
    ITEM_2 = "item_2"
    ITEM_4 = "item_4"
    ITEM_5 = "item_5"
    ITEM_7 = "item_7"


class AdapterKey(StrEnum):
    RUBRIK_QUARTER_TABLE = "rubrik_quarter_table"
    WIX_VISIBLE_QUARTER = "wix_visible_quarter"


class TranscriptSource(StrEnum):
    MANIFEST = "manifest"
    ISSUER_IR = "issuer_ir"
    AGGREGATOR = "aggregator"
    AUDIO = "audio"


class ReportedQuarterWindow(BaseModel):
    """Canonical maximum history admitted by quarter-bounded collectors."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    max_quarters: int = Field(default=5, ge=1, le=20)


class SourcePolicyConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    policy_version: str = POLICY_VERSION
    reported_quarter_window: ReportedQuarterWindow = Field(default_factory=ReportedQuarterWindow)


SOURCE_POLICY_CONFIG = SourcePolicyConfig()


class AcquisitionDecision(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    allowed: bool
    reason: AuthorizationReason
    mode: CollectionMode
    reported_quarter_window: ReportedQuarterWindow | None = None


class CollectionTarget(BaseModel):
    """One company considered by a source runner before any external work."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    ticker: str = Field(min_length=1, max_length=16)
    coverage_role: ListType
    requested: bool = False

    @field_validator("ticker")
    @classmethod
    def _normalize_ticker(cls, value: str) -> str:
        ticker = value.strip().upper()
        if not ticker or not ticker.replace(".", "").replace("-", "").isalnum():
            raise ValueError("ticker contains unsupported characters")
        return ticker


class CollectionTargetDecision(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    target: CollectionTarget
    decision: AcquisitionDecision


class CollectionSelection(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    allowed: tuple[CollectionTargetDecision, ...]
    denied: tuple[CollectionTargetDecision, ...]


class StoredIdentityStatus(StrEnum):
    AUTHORIZED = "authorized"
    IDENTITY_UNAVAILABLE = "stored_identity_unavailable"
    IDENTITY_NOT_FOUND = "stored_identity_not_found"
    IDENTITY_AMBIGUOUS = "stored_identity_ambiguous"
    ROLE_INVALID = "stored_role_invalid"
    POLICY_DENIED = "policy_denied"


class StoredCollectionAuthorization(BaseModel):
    """DB-bound identity plus the canonical collection decision."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    status: StoredIdentityStatus
    target: CollectionTarget | None = None
    decision: AcquisitionDecision | None = None
    fiscal_year_end_month: int | None = Field(default=None, ge=1, le=12)

    @property
    def allowed(self) -> bool:
        return (
            self.status is StoredIdentityStatus.AUTHORIZED
            and self.decision is not None
            and self.decision.allowed
        )


class SectionRule(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    form: FilingForm
    sections: tuple[FilingSection, ...]


class NameRule(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    source_name: str
    canonical_name: str


class SecIssuerRules(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    company_facts_role: CompanyFactsRole = CompanyFactsRole.FACT_FEED_ONLY
    filing_forms: tuple[FilingForm, ...]
    relevant_sections: tuple[SectionRule, ...] = ()


class IrEndpointRule(BaseModel):
    """An exact HTTPS host and its explicitly approved path surface."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    host: str
    exact_paths: tuple[str, ...] = ()
    path_prefixes: tuple[str, ...] = ()

    @field_validator("host")
    @classmethod
    def _validate_host(cls, value: str) -> str:
        if _canonical_dns_host(value) is None:
            raise ValueError("approved IR host must be an exact lowercase hostname")
        return value

    @field_validator("exact_paths", "path_prefixes")
    @classmethod
    def _validate_paths(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("approved IR paths must be unique")
        for path in value:
            if _canonical_safe_path(path) != path or "?" in path or "#" in path or "%" in path:
                raise ValueError("approved IR paths must be canonical safe absolute URL paths")
        return value

    @model_validator(mode="after")
    def _require_path_surface(self) -> IrEndpointRule:
        if not self.exact_paths and not self.path_prefixes:
            raise ValueError("approved IR endpoint must include an exact path or path prefix")
        return self


class IrIssuerRules(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    authority_url: str
    adapter_key: AdapterKey
    approved_endpoints: tuple[IrEndpointRule, ...]
    fiscal_year_end: str
    admitted_doc_types: tuple[DocType, ...]
    reported_quarter_window: int = Field(
        default=SOURCE_POLICY_CONFIG.reported_quarter_window.max_quarters,
        ge=1,
        le=SOURCE_POLICY_CONFIG.reported_quarter_window.max_quarters,
    )
    admits_webcasts: bool = False

    @model_validator(mode="after")
    def _validate_authority_endpoint(self) -> IrIssuerRules:
        canonical = canonical_https_url(self.authority_url)
        parsed = urlsplit(self.authority_url)
        if canonical is None or parsed.query:
            raise ValueError("IR authority URL must be a plain exact HTTPS endpoint")
        if len(self.approved_endpoints) != len(set(self.approved_endpoints)):
            raise ValueError("approved IR endpoints must be unique")
        authority_host, authority_path = canonical
        exact_authority = any(
            authority_host == endpoint.host and authority_path in endpoint.exact_paths
            for endpoint in self.approved_endpoints
        )
        if not exact_authority:
            raise ValueError("IR authority URL must be an exact approved endpoint path")
        return self


def ir_url_is_authorized(rules: IrIssuerRules, url: str) -> bool:
    """Return whether ``url`` is inside an issuer's exact approved endpoint surface."""
    canonical = canonical_https_url(url)
    if canonical is None:
        return False
    host, path = canonical
    for endpoint in rules.approved_endpoints:
        if host != endpoint.host:
            continue
        if path in endpoint.exact_paths:
            return True
        for prefix in endpoint.path_prefixes:
            boundary = prefix if prefix.endswith("/") else f"{prefix}/"
            if path == prefix.rstrip("/") or path.startswith(boundary):
                return True
    return False


class FmpIssuerRules(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    endpoint_aliases: tuple[NameRule, ...] = ()
    label_overrides: tuple[NameRule, ...] = ()


class TranscriptIssuerRules(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    accepts_ir_text_transcripts: bool = True
    accepts_webcasts: bool = False
    source_priority: tuple[TranscriptSource, ...] = (
        TranscriptSource.MANIFEST,
        TranscriptSource.ISSUER_IR,
        TranscriptSource.AGGREGATOR,
        TranscriptSource.AUDIO,
    )


class IssuerAcquisitionPolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    issuer_id: str
    ticker_aliases: tuple[str, ...]
    policy_version: str = POLICY_VERSION
    sec: SecIssuerRules
    ir: IrIssuerRules
    fmp: FmpIssuerRules = Field(default_factory=FmpIssuerRules)
    transcript: TranscriptIssuerRules = Field(default_factory=TranscriptIssuerRules)

    def canonical_json(self) -> str:
        return json.dumps(
            self.model_dump(mode="json", exclude_none=False),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )

    @property
    def policy_sha256(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()


_ROLE_MODES: dict[ListType, CollectionMode] = {
    ListType.PORTFOLIO: CollectionMode.AUTOMATIC_FULL,
    ListType.EVALUATION: CollectionMode.ON_DEMAND_FULL,
    ListType.WATCHLIST: CollectionMode.METADATA_ONLY,
    ListType.INDEX_MEMBER: CollectionMode.SCREENING_ONLY,
    ListType.NONE: CollectionMode.CATALOG_ONLY,
    ListType.ETF: CollectionMode.CATALOG_ONLY,
}
_COLLECTION_PRIORITY: dict[ListType, int] = {
    ListType.PORTFOLIO: 0,
    ListType.EVALUATION: 1,
    ListType.WATCHLIST: 2,
    ListType.INDEX_MEMBER: 3,
    ListType.ETF: 4,
    ListType.NONE: 5,
}
DISPLAY_ROLE_ORDER: tuple[ListType, ...] = (
    ListType.PORTFOLIO,
    ListType.EVALUATION,
    ListType.WATCHLIST,
    ListType.INDEX_MEMBER,
)
_SOURCE_ARTIFACTS: dict[CollectionSource, frozenset[ArtifactKind]] = {
    CollectionSource.SEC: frozenset(
        {
            ArtifactKind.METADATA,
            ArtifactKind.COMPANY_FACTS,
            ArtifactKind.FILING_PACKAGE,
            ArtifactKind.FILING_SECTION,
            ArtifactKind.FINANCIAL_FACT,
        }
    ),
    CollectionSource.IR: frozenset({ArtifactKind.METADATA, ArtifactKind.IR_DOCUMENT}),
    CollectionSource.FMP: frozenset({ArtifactKind.METADATA, ArtifactKind.FINANCIAL_FACT}),
    CollectionSource.TRANSCRIPT: frozenset(
        {ArtifactKind.METADATA, ArtifactKind.TEXT_TRANSCRIPT, ArtifactKind.WEBCAST}
    ),
}


def mode_for_role(role: ListType | str) -> CollectionMode:
    try:
        normalized = role if isinstance(role, ListType) else ListType(role)
    except ValueError as exc:
        raise ValueError(f"unknown coverage role: {role!r}") from exc
    return _ROLE_MODES[normalized]


def decision_for(
    role: ListType | str,
    source: CollectionSource | str,
    artifact_kind: ArtifactKind | str,
    *,
    requested: bool,
) -> AcquisitionDecision:
    mode = mode_for_role(role)
    try:
        source_value = source if isinstance(source, CollectionSource) else CollectionSource(source)
    except ValueError as exc:
        raise ValueError(f"unknown collection source: {source!r}") from exc
    try:
        artifact = (
            artifact_kind
            if isinstance(artifact_kind, ArtifactKind)
            else ArtifactKind(artifact_kind)
        )
    except ValueError as exc:
        raise ValueError(f"unknown artifact kind: {artifact_kind!r}") from exc
    reported_quarter_window = (
        SOURCE_POLICY_CONFIG.reported_quarter_window
        if (source_value, artifact)
        in {
            (CollectionSource.IR, ArtifactKind.IR_DOCUMENT),
            (CollectionSource.TRANSCRIPT, ArtifactKind.TEXT_TRANSCRIPT),
        }
        else None
    )
    if artifact not in _SOURCE_ARTIFACTS[source_value]:
        return AcquisitionDecision(
            allowed=False,
            reason=AuthorizationReason.SOURCE_ARTIFACT_MISMATCH,
            mode=mode,
            reported_quarter_window=reported_quarter_window,
        )
    if artifact is ArtifactKind.WEBCAST:
        return AcquisitionDecision(
            allowed=False,
            reason=AuthorizationReason.WEBCAST_EXCLUDED,
            mode=mode,
            reported_quarter_window=reported_quarter_window,
        )
    if mode is CollectionMode.SCREENING_ONLY:
        allowed = source_value is CollectionSource.FMP and artifact is ArtifactKind.FINANCIAL_FACT
        return AcquisitionDecision(
            allowed=allowed,
            reason=(
                AuthorizationReason.SCREENING_FACT_ALLOWED
                if allowed
                else AuthorizationReason.COVERAGE_DEPTH_DENIED
            ),
            mode=mode,
            reported_quarter_window=reported_quarter_window,
        )
    if artifact is ArtifactKind.METADATA:
        allowed = mode is not CollectionMode.CATALOG_ONLY
        return AcquisitionDecision(
            allowed=allowed,
            reason=(
                AuthorizationReason.METADATA_ALLOWED
                if allowed
                else AuthorizationReason.COVERAGE_DEPTH_DENIED
            ),
            mode=mode,
            reported_quarter_window=reported_quarter_window,
        )
    if mode is CollectionMode.AUTOMATIC_FULL:
        return AcquisitionDecision(
            allowed=True,
            reason=AuthorizationReason.AUTOMATIC,
            mode=mode,
            reported_quarter_window=reported_quarter_window,
        )
    if mode is CollectionMode.ON_DEMAND_FULL:
        return AcquisitionDecision(
            allowed=requested,
            reason=(
                AuthorizationReason.OWNER_REQUESTED
                if requested
                else AuthorizationReason.REQUEST_REQUIRED
            ),
            mode=mode,
            reported_quarter_window=reported_quarter_window,
        )
    return AcquisitionDecision(
        allowed=False,
        reason=AuthorizationReason.COVERAGE_DEPTH_DENIED,
        mode=mode,
        reported_quarter_window=reported_quarter_window,
    )


def select_collection_targets(
    targets: tuple[CollectionTarget, ...],
    *,
    source: CollectionSource,
    artifact_kind: ArtifactKind,
) -> CollectionSelection:
    """Authorize and priority-order a runner scope through the canonical policy.

    Portfolio always precedes explicitly requested evaluation work. Denied names
    remain in the receipt so scheduled runners can explain skipped coverage
    instead of silently broadening their crawl.
    """

    ordered = sorted(
        targets,
        key=lambda target: (_COLLECTION_PRIORITY[target.coverage_role], target.ticker),
    )
    decisions = tuple(
        CollectionTargetDecision(
            target=target,
            decision=decision_for(
                target.coverage_role,
                source,
                artifact_kind,
                requested=target.requested,
            ),
        )
        for target in ordered
    )
    return CollectionSelection(
        allowed=tuple(item for item in decisions if item.decision.allowed),
        denied=tuple(item for item in decisions if not item.decision.allowed),
    )


def authorize_stored_collection_target(
    db_path: Path,
    ticker: str,
    *,
    requested: bool,
    source: CollectionSource,
    artifact_kind: ArtifactKind,
) -> StoredCollectionAuthorization:
    """Bind a network decision to one active stored company identity.

    Missing, malformed, or ambiguous roster state is denied before external
    work. The caller decides whether a denial is terminal or a batch skip.
    """

    normalized_ticker = ticker.strip().upper()
    if not db_path.is_file():
        return StoredCollectionAuthorization(status=StoredIdentityStatus.IDENTITY_UNAVAILABLE)
    try:
        conn = connect_sqlite(str(db_path), role=SQLiteConnectionRole.READ_ONLY)
        try:
            return authorize_collection_target_in_connection(
                conn,
                normalized_ticker,
                requested=requested,
                source=source,
                artifact_kind=artifact_kind,
            )
        finally:
            conn.close()
    except (OSError, sqlite3.Error):
        return StoredCollectionAuthorization(status=StoredIdentityStatus.IDENTITY_UNAVAILABLE)


def authorize_collection_target_in_connection(
    conn: sqlite3.Connection,
    ticker: str,
    *,
    requested: bool,
    source: CollectionSource,
    artifact_kind: ArtifactKind,
) -> StoredCollectionAuthorization:
    """Authorize one active stored identity using a caller-owned connection."""

    normalized_ticker = ticker.strip().upper()
    try:
        rows = conn.execute(
            "SELECT ticker,list_type FROM tracked_companies "
            "WHERE UPPER(ticker)=? AND archived_at IS NULL",
            (normalized_ticker,),
        ).fetchall()
    except sqlite3.Error:
        return StoredCollectionAuthorization(status=StoredIdentityStatus.IDENTITY_UNAVAILABLE)
    if not rows:
        return StoredCollectionAuthorization(status=StoredIdentityStatus.IDENTITY_NOT_FOUND)
    if len(rows) != 1:
        return StoredCollectionAuthorization(status=StoredIdentityStatus.IDENTITY_AMBIGUOUS)
    row = rows[0]
    try:
        target = CollectionTarget(
            ticker=str(row[0]),
            coverage_role=ListType(str(row[1])),
            requested=requested,
        )
    except (ValueError, TypeError):
        return StoredCollectionAuthorization(status=StoredIdentityStatus.ROLE_INVALID)
    columns = {
        str(column[1]) for column in conn.execute("PRAGMA table_info(tracked_companies)").fetchall()
    }
    fye_row = (
        conn.execute(
            "SELECT fiscal_year_end FROM tracked_companies "
            "WHERE UPPER(ticker)=? AND archived_at IS NULL",
            (normalized_ticker,),
        ).fetchone()
        if "fiscal_year_end" in columns
        else None
    )
    fye_raw = fye_row[0] if fye_row is not None else None
    fiscal_year_end_month: int | None = None
    if isinstance(fye_raw, str) and len(fye_raw) >= 2:
        try:
            candidate_month = int(fye_raw[:2])
        except ValueError:
            candidate_month = 0
        if 1 <= candidate_month <= 12:
            fiscal_year_end_month = candidate_month
    decision = decision_for(
        target.coverage_role,
        source,
        artifact_kind,
        requested=target.requested,
    )
    return StoredCollectionAuthorization(
        status=(
            StoredIdentityStatus.AUTHORIZED
            if decision.allowed
            else StoredIdentityStatus.POLICY_DENIED
        ),
        target=target,
        decision=decision,
        fiscal_year_end_month=fiscal_year_end_month,
    )


def reported_quarter_is_in_window(
    *,
    fiscal_year: int,
    fiscal_quarter: int,
    fiscal_year_end_month: int,
    as_of: date,
    max_quarters: int = SOURCE_POLICY_CONFIG.reported_quarter_window.max_quarters,
) -> bool:
    """Return whether a fiscal quarter is among the latest completed policy window."""

    if not 1 <= fiscal_quarter <= 4 or not 1 <= fiscal_year_end_month <= 12:
        return False
    completed: list[tuple[date, int, int]] = []
    for year in range(as_of.year - 2, as_of.year + 2):
        for quarter in range(1, 5):
            months_before_fye = (4 - quarter) * 3
            end_year = year
            end_month = fiscal_year_end_month - months_before_fye
            while end_month < 1:
                end_month += 12
                end_year -= 1
            if end_month == 12:
                next_month = date(end_year + 1, 1, 1)
            else:
                next_month = date(end_year, end_month + 1, 1)
            period_end = date.fromordinal(next_month.toordinal() - 1)
            if period_end <= as_of:
                completed.append((period_end, year, quarter))
    newest = sorted(completed, reverse=True)[:max_quarters]
    return any(year == fiscal_year and quarter == fiscal_quarter for _, year, quarter in newest)


def _reviewed_policies() -> tuple[IssuerAcquisitionPolicy, ...]:
    return (
        IssuerAcquisitionPolicy(
            issuer_id="sec-cik-0001943896",
            ticker_aliases=("RBRK",),
            sec=SecIssuerRules(
                filing_forms=(FilingForm.FORM_10K, FilingForm.FORM_10Q, FilingForm.FORM_8K),
                relevant_sections=(
                    SectionRule(
                        form=FilingForm.FORM_10K,
                        sections=(
                            FilingSection.ITEM_1,
                            FilingSection.ITEM_1A,
                            FilingSection.ITEM_7,
                        ),
                    ),
                    SectionRule(
                        form=FilingForm.FORM_10Q,
                        sections=(FilingSection.ITEM_1, FilingSection.ITEM_2),
                    ),
                ),
            ),
            ir=IrIssuerRules(
                authority_url="https://ir.rubrik.com/financials/quarterly-results/default.aspx",
                adapter_key=AdapterKey.RUBRIK_QUARTER_TABLE,
                approved_endpoints=(
                    IrEndpointRule(
                        host="ir.rubrik.com",
                        exact_paths=("/financials/quarterly-results/default.aspx",),
                        path_prefixes=("/news-events/press-releases/detail", "/static-files"),
                    ),
                    IrEndpointRule(
                        host="s203.q4cdn.com",
                        path_prefixes=("/667520861/files",),
                    ),
                ),
                fiscal_year_end="01-31",
                admitted_doc_types=(
                    DocType.IR_PRESS_RELEASE,
                    DocType.IR_PRESENTATION,
                    DocType.IR_TRANSCRIPT,
                ),
            ),
        ),
        IssuerAcquisitionPolicy(
            issuer_id="sec-cik-0001576789",
            ticker_aliases=("WIX",),
            sec=SecIssuerRules(
                filing_forms=(FilingForm.FORM_20F, FilingForm.FORM_6K),
                relevant_sections=(
                    SectionRule(
                        form=FilingForm.FORM_20F,
                        sections=(FilingSection.ITEM_4, FilingSection.ITEM_5),
                    ),
                ),
            ),
            ir=IrIssuerRules(
                authority_url="https://investors.wix.com/financials",
                adapter_key=AdapterKey.WIX_VISIBLE_QUARTER,
                approved_endpoints=(
                    IrEndpointRule(
                        host="investors.wix.com",
                        exact_paths=("/financials",),
                        path_prefixes=("/static-files",),
                    ),
                    IrEndpointRule(host="static.wixstatic.com", path_prefixes=("/media",)),
                    IrEndpointRule(
                        host="4f4a3186-9467-4c09-aa74-51fe1affec20.usrfiles.com",
                        path_prefixes=("/ugd",),
                    ),
                ),
                fiscal_year_end="12-31",
                admitted_doc_types=(
                    DocType.IR_PRESS_RELEASE,
                    DocType.IR_PRESENTATION,
                    DocType.IR_INVESTOR_UPDATE,
                    DocType.IR_TRANSCRIPT,
                ),
            ),
        ),
    )


def build_issuer_registry(
    policies: tuple[IssuerAcquisitionPolicy, ...],
) -> dict[str, IssuerAcquisitionPolicy]:
    """Build one case-insensitive namespace for canonical IDs and aliases."""
    registry: dict[str, IssuerAcquisitionPolicy] = {}
    for policy in policies:
        keys = (policy.issuer_id, *policy.ticker_aliases)
        for key in keys:
            normalized = key.casefold()
            if normalized in registry:
                raise ValueError(f"duplicate or ambiguous issuer identifier: {key!r}")
            registry[normalized] = policy
    return registry


_ISSUER_REGISTRY = build_issuer_registry(_reviewed_policies())


def issuer_policy(identifier: str) -> IssuerAcquisitionPolicy:
    policy = _ISSUER_REGISTRY.get(identifier.casefold())
    if policy is None:
        raise ValueError(f"unknown issuer acquisition policy: {identifier!r}")
    return policy


def issuer_policies() -> tuple[IssuerAcquisitionPolicy, ...]:
    """Return canonical issuer policies once each, in reviewed order."""

    return _reviewed_policies()
