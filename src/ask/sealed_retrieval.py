"""Strict production Ask retrieval over promoted, sealed Research Snapshots."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Self, cast
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ask.audit_store import canonical_json, digest_text, retrieval_query_sha256
from provenance.research_snapshot import ResearchSnapshotRequest, verify_research_snapshot
from search.embedding_promotion import LocalVectorRuntimeConfig
from search.exact_semantic import ExactSemanticRuntime
from search.heterogeneous_retrieval import (
    HeterogeneousRetrievalError,
    HeterogeneousRetrievalReceipt,
    HeterogeneousRetrievalRequest,
    NarrativeBundle,
    RetrievalFilters,
    retrieve_heterogeneous,
    verify_heterogeneous_retrieval_trace,
)

ReadinessOutcome = Literal["ready", "coverage_incomplete", "unavailable"]
ReasonCode = Literal[
    "ready",
    "empty_scope",
    "scope_identity_incomplete",
    "promotion_missing",
    "promotion_withdrawn",
    "promotion_invalid",
    "promotion_stale",
    "promotion_verifier_mismatch",
    "source_inventory_incomplete",
    "research_snapshot_not_admitted",
    "fact_projection_unsealed",
    "semantic_projection_unsealed",
    "semantic_runtime_unavailable",
    "incomparable_snapshot_cutoffs",
    "retrieval_failed",
    "trace_verification_failed",
]

ASK_RETRIEVAL_POLICY_VERSION = "ask-sealed-retrieval.v1"
ASK_RETRIEVAL_VERIFIER_NAME = "ask.sealed_retrieval.verify_retrieval_promotion"
ASK_RETRIEVAL_VERIFIER_VERSION = "1"
ASK_RETRIEVAL_VERIFIER_MANIFEST_VERSION = "ask-verifier-manifest.v1"
PRODUCTION_SCOPE_REGISTRY_ID = "ask-retrieval-production-scopes"
PRODUCTION_SCOPE_SCHEMA_VERSION = 1
PRODUCTION_SUPPORTED_COHORT = ("operating_company:legal_registrant",)
_VERIFIER_CONFIG = {
    "common_cutoff_required": True,
    "exact_semantic_recompute_required": True,
    "fact_projection_seal_required": True,
    "research_snapshot_admission_required": True,
    "source_inventory_closure_required": True,
}
_VERIFIER_ARTIFACTS = (
    ("src/ask/audit_store.py", "ask-audit-store.v1"),
    ("src/ask/sealed_retrieval.py", "ask-sealed-retrieval.v1"),
    ("src/provenance/research_snapshot.py", "research-snapshot-verifier.v1"),
    ("src/search/exact_semantic.py", "exact-semantic-verifier.v1"),
    ("src/search/heterogeneous_retrieval.py", "heterogeneous-trace-verifier.v1"),
)


def _canonical_artifact_sha256(path: Path) -> str:
    """Hash repository text with Git-style LF canonicalization."""

    text = path.read_text(encoding="utf-8")
    canonical = text.replace("\r\n", "\n").replace("\r", "\n")
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def current_verifier_manifest() -> dict[str, object]:
    """Return all load-bearing verifier artifacts and their explicit versions."""

    repo_root = Path(__file__).resolve().parents[2]
    artifacts = [
        {
            "path": relative_path,
            "artifact_version": artifact_version,
            "sha256": _canonical_artifact_sha256(repo_root / relative_path),
        }
        for relative_path, artifact_version in _VERIFIER_ARTIFACTS
    ]
    return {
        "manifest_version": ASK_RETRIEVAL_VERIFIER_MANIFEST_VERSION,
        "artifacts": artifacts,
    }


def current_verifier_identity() -> tuple[str, str, str, str, str]:
    """Return the current policy/verifier identity promotion specs must pin."""

    code_sha256 = digest_text(canonical_json(current_verifier_manifest()))
    config_sha256 = digest_text(canonical_json(_VERIFIER_CONFIG))
    return (
        ASK_RETRIEVAL_POLICY_VERSION,
        ASK_RETRIEVAL_VERIFIER_NAME,
        ASK_RETRIEVAL_VERIFIER_VERSION,
        code_sha256,
        config_sha256,
    )


def _sha(value: str) -> str:
    normalized = value.lower()
    if len(normalized) != 64 or any(char not in "0123456789abcdef" for char in normalized):
        raise ValueError("must be a lowercase SHA-256 digest")
    return normalized


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _db_time(value: datetime) -> str:
    return _utc(value).replace(tzinfo=None).isoformat(sep=" ")


def _datetime(value: object) -> datetime:
    parsed = datetime.fromisoformat(str(value))
    return _utc(parsed)


def _integer(value: object) -> int:
    return int(str(value))


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RetrievalScope(_Frozen):
    scope_key: str = Field(min_length=1, max_length=256)
    ticker: str = Field(min_length=1, max_length=32)
    issuer_id: str = Field(min_length=1, max_length=128)
    reporting_entity_id: str = Field(min_length=1, max_length=128)

    @field_validator("ticker")
    @classmethod
    def _ticker(cls, value: str) -> str:
        return value.strip().upper()


class RetrievalPromotion(_Frozen):
    promotion_id: str = Field(min_length=1, max_length=128)
    idempotency_key: str = Field(min_length=1, max_length=256)
    scope_key: str = Field(min_length=1, max_length=256)
    revision: int = Field(gt=0)
    issuer_id: str = Field(min_length=1, max_length=128)
    reporting_entity_id: str = Field(min_length=1, max_length=128)
    research_snapshot_id: str = Field(min_length=1, max_length=128)
    research_snapshot_sha256: str
    fact_generation_id: str = Field(min_length=1, max_length=128)
    fact_projection_seal_sha256: str
    source_inventory_ids: tuple[str, ...]
    narrative_bundles: tuple[NarrativeBundle, ...]
    cutoff_at: datetime
    policy_version: str = Field(min_length=1, max_length=64)
    verifier_name: str = Field(min_length=1, max_length=128)
    verifier_version: str = Field(min_length=1, max_length=64)
    verifier_code_sha256: str
    verifier_config_sha256: str
    status: Literal["promoted", "withdrawn"]
    supersedes_promotion_id: str | None = Field(default=None, max_length=128)
    recorded_at: datetime

    _hashes = field_validator(
        "research_snapshot_sha256",
        "fact_projection_seal_sha256",
        "verifier_code_sha256",
        "verifier_config_sha256",
    )(_sha)

    @model_validator(mode="after")
    def _shape(self) -> Self:
        if self.source_inventory_ids != tuple(sorted(set(self.source_inventory_ids))):
            raise ValueError("source inventory ids must be unique and sorted")
        manifest_ids = [item.corpus_manifest_id for item in self.narrative_bundles]
        if manifest_ids != sorted(set(manifest_ids)):
            raise ValueError("narrative bundles must be unique and sorted")
        if any(
            item.vector_index_run_id is None or item.embedding_promotion_id is None
            for item in self.narrative_bundles
        ):
            raise ValueError("production Ask promotions require semantic bundles")
        if (self.revision == 1) != (self.supersedes_promotion_id is None):
            raise ValueError("promotion revision chain is incomplete")
        if _utc(self.recorded_at) < _utc(self.cutoff_at):
            raise ValueError("promotion cannot predate its cutoff")
        return self


class ReadyRetrievalScope(_Frozen):
    scope: RetrievalScope
    promotion: RetrievalPromotion


class RetrievalReadiness(_Frozen):
    outcome: ReadinessOutcome
    reason_code: ReasonCode
    details: str
    scopes: tuple[ReadyRetrievalScope, ...] = ()

    @model_validator(mode="after")
    def _outcome_shape(self) -> Self:
        if self.outcome == "ready":
            if not self.scopes:
                raise ValueError("ready retrieval requires at least one exact scope")
            if len({_utc(item.promotion.cutoff_at) for item in self.scopes}) != 1:
                raise ValueError("ready retrieval scopes require one common cutoff")
        elif self.scopes:
            raise ValueError("non-ready retrieval cannot expose partial scopes")
        return self


class SealedRetrievalPlan(_Frozen):
    request_id: str = Field(min_length=1, max_length=128)
    question: str = Field(min_length=1)
    scopes: tuple[ReadyRetrievalScope, ...]
    requests: tuple[HeterogeneousRetrievalRequest, ...]
    created_at: datetime

    @model_validator(mode="after")
    def _exact_requests(self) -> Self:
        if len(self.scopes) != len(self.requests) or not self.scopes:
            raise ValueError("sealed retrieval plan scopes and requests must be one-to-one")
        for scope, request in zip(self.scopes, self.requests, strict=True):
            expected_key = (
                f"ask-request:{self.request_id}:{retrieval_query_sha256(self.question)}:"
                f"{scope.promotion.promotion_id}"
            )
            if (
                request.idempotency_key != expected_key
                or request.query_text != self.question
                or request.research_snapshot_id
                != scope.promotion.research_snapshot_id
                or request.fact_generation_id != scope.promotion.fact_generation_id
                or request.narrative_bundles != scope.promotion.narrative_bundles
                or request.filters.reporting_entity_id
                != scope.promotion.reporting_entity_id
            ):
                raise ValueError("sealed retrieval request differs from its promotion")
        return self


class SealedRetrievalExecution(_Frozen):
    plan: SealedRetrievalPlan
    receipts: tuple[HeterogeneousRetrievalReceipt, ...]


class SealedEvidenceItem(_Frozen):
    n: int = Field(gt=0)
    kind: Literal["narrative", "fact"]
    label: str
    text: str
    href: str
    source_url: str
    ticker: str | None
    doc_type: str
    period: str | None
    value: str | None
    trace_id: str
    result_ordinal: int = Field(ge=0)
    candidate_id: str
    source_commitment_sha256: str
    research_snapshot_id: str
    as_of_at: datetime
    document_version_id: str
    evidence_node_id: str
    node_kind: str
    locator: dict[str, object]
    score: str

    _source_hash = field_validator("source_commitment_sha256")(_sha)

    def citation_payload(self) -> dict[str, object]:
        return {
            "n": self.n,
            "kind": self.kind,
            "label": self.label,
            "href": self.href,
            "source_url": self.source_url,
            "confidence": None,
            "ticker": self.ticker,
            "doc_type": self.doc_type,
            "period": self.period,
            "value": self.value,
            "trace_id": self.trace_id,
            "result_ordinal": self.result_ordinal,
            "candidate_id": self.candidate_id,
            "source_commitment_sha256": self.source_commitment_sha256,
            "research_snapshot_id": self.research_snapshot_id,
            "as_of_at": self.as_of_at.isoformat(),
            "document_version_id": self.document_version_id,
            "evidence_node_id": self.evidence_node_id,
            "node_kind": self.node_kind,
            "locator": self.locator,
        }


class PromotionVerificationError(RuntimeError):
    reason_code: ReasonCode
    details: str

    def __init__(self, reason_code: ReasonCode, details: str) -> None:
        self.reason_code = reason_code
        self.details = details
        super().__init__(f"{reason_code}: {details}")


def derive_production_scope_registry(
    conn: sqlite3.Connection,
) -> dict[str, object]:
    """Derive the complete supported production Ask scope set from live registries."""

    core_rows = conn.execute(
        "SELECT scope_revision_id,scope_key,issuer_id "
        "FROM v_issuer_reporting_scope_current "
        "WHERE inclusion_state='core' "
        "ORDER BY scope_key,issuer_id,scope_revision_id"
    ).fetchall()
    if not core_rows:
        raise ValueError("canonical registry has no enabled core Ask scopes")
    core_identities = [(str(row[1]), str(row[2])) for row in core_rows]
    if len(core_identities) != len(set(core_identities)):
        raise ValueError("canonical registry has duplicate core scope identities")
    scopes: list[RetrievalScope] = []
    revisions: list[str] = []
    for raw in core_rows:
        revision_id, scope_key, issuer_id = (str(value) for value in raw)
        issuer_rows = conn.execute(
            "SELECT entity_kind FROM issuer_entities WHERE issuer_id=?",
            (issuer_id,),
        ).fetchall()
        reporting_rows = conn.execute(
            "SELECT reporting_entity_id FROM reporting_entities "
            "WHERE issuer_id=? AND reporting_entity_kind='legal_registrant'",
            (issuer_id,),
        ).fetchall()
        listing_rows = conn.execute(
            "SELECT normalized_ticker FROM v_security_listings_canonical "
            "WHERE issuer_id=? AND status='listed'",
            (issuer_id,),
        ).fetchall()
        if (
            len(issuer_rows) != 1
            or str(issuer_rows[0][0]) != "operating_company"
            or len(reporting_rows) != 1
            or len(listing_rows) != 1
        ):
            raise ValueError(
                f"core scope {scope_key}/{issuer_id} has missing, duplicate, "
                "or unsupported reporting identity"
            )
        reporting_entity_id = str(reporting_rows[0][0])
        ticker = str(listing_rows[0][0]).strip().upper()
        if not reporting_entity_id or not ticker:
            raise ValueError(
                f"core scope {scope_key}/{issuer_id} has an empty reporting identity"
            )
        scopes.append(
            RetrievalScope(
                scope_key=scope_key,
                ticker=ticker,
                issuer_id=issuer_id,
                reporting_entity_id=reporting_entity_id,
            )
        )
        revisions.append(revision_id)
    canonical_scopes = canonical_json(
        [item.model_dump(mode="json") for item in scopes]
    )
    core: dict[str, object] = {
        "registry_id": PRODUCTION_SCOPE_REGISTRY_ID,
        "schema_version": PRODUCTION_SCOPE_SCHEMA_VERSION,
        "supported_cohort": list(PRODUCTION_SUPPORTED_COHORT),
        "source_scope_revision_ids": sorted(revisions),
        "scopes": [item.model_dump(mode="json") for item in scopes],
        "scope_set_sha256": digest_text(canonical_scopes),
    }
    return core | {"registry_sha256": digest_text(canonical_json(core))}


def load_production_scopes(
    conn: sqlite3.Connection,
    registry_path: Path,
    *,
    requested_tickers: tuple[str, ...] | None = None,
) -> tuple[RetrievalScope, ...]:
    """Load a committed registry only when it exactly equals the live cohort."""

    try:
        decoded = json.loads(registry_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"production scope registry is unavailable: {exc}") from exc
    if not isinstance(decoded, dict):
        raise ValueError("production scope registry must be a JSON object")
    payload = cast(dict[str, object], decoded)
    if (
        payload.get("registry_id") != PRODUCTION_SCOPE_REGISTRY_ID
        or payload.get("schema_version") != PRODUCTION_SCOPE_SCHEMA_VERSION
        or payload.get("supported_cohort") != list(PRODUCTION_SUPPORTED_COHORT)
    ):
        raise ValueError("production scope registry contract is invalid")
    registry_sha256 = payload.get("registry_sha256")
    core = {key: value for key, value in payload.items() if key != "registry_sha256"}
    if (
        not isinstance(registry_sha256, str)
        or digest_text(canonical_json(core)) != registry_sha256
    ):
        raise ValueError("production scope registry commitment mismatch")
    if canonical_json(payload) != canonical_json(derive_production_scope_registry(conn)):
        raise ValueError(
            "production scope registry differs from the live frozen cohort/source revisions"
        )
    raw_scopes = payload.get("scopes")
    if not isinstance(raw_scopes, list) or not raw_scopes:
        raise ValueError("production scope registry must contain scopes")
    scope_payloads = cast(list[object], raw_scopes)
    scopes = tuple(RetrievalScope.model_validate(item) for item in scope_payloads)
    if scopes != tuple(sorted(scopes, key=lambda item: item.scope_key)):
        raise ValueError("production scopes must be sorted by scope_key")
    scope_set_sha256 = payload.get("scope_set_sha256")
    if scope_set_sha256 != digest_text(
        canonical_json([item.model_dump(mode="json") for item in scopes])
    ):
        raise ValueError("production scope-set commitment mismatch")
    if requested_tickers is None:
        return scopes
    normalized = tuple(sorted(set(ticker.strip().upper() for ticker in requested_tickers)))
    if not normalized:
        raise ValueError("sealed Ask requires at least one requested ticker")
    by_ticker: dict[str, list[RetrievalScope]] = {}
    for scope in scopes:
        by_ticker.setdefault(scope.ticker, []).append(scope)
    selected: list[RetrievalScope] = []
    for ticker in normalized:
        candidates = by_ticker.get(ticker, [])
        if len(candidates) != 1:
            raise ValueError(
                f"production Ask scope for {ticker} is missing or ambiguous"
            )
        selected.append(candidates[0])
    return tuple(sorted(selected, key=lambda item: item.scope_key))


@contextmanager
def _savepoint(conn: sqlite3.Connection, name: str):
    conn.execute(f"SAVEPOINT {name}")
    try:
        yield
    except Exception:
        conn.execute(f"ROLLBACK TO SAVEPOINT {name}")
        conn.execute(f"RELEASE SAVEPOINT {name}")
        raise
    conn.execute(f"RELEASE SAVEPOINT {name}")


def _promotion_payloads(
    promotion: RetrievalPromotion,
) -> tuple[str, str, str, str]:
    inventory_json = canonical_json(promotion.source_inventory_ids)
    bundles_json = canonical_json(
        [item.model_dump(mode="json") for item in promotion.narrative_bundles]
    )
    return (
        inventory_json,
        digest_text(inventory_json),
        bundles_json,
        digest_text(bundles_json),
    )


def _promotion_values(promotion: RetrievalPromotion) -> tuple[object, ...]:
    inventory_json, inventory_sha, bundles_json, bundles_sha = _promotion_payloads(
        promotion
    )
    return (
        promotion.promotion_id,
        promotion.idempotency_key,
        promotion.scope_key,
        promotion.revision,
        promotion.issuer_id,
        promotion.reporting_entity_id,
        promotion.research_snapshot_id,
        promotion.research_snapshot_sha256,
        promotion.fact_generation_id,
        promotion.fact_projection_seal_sha256,
        inventory_json,
        inventory_sha,
        bundles_json,
        bundles_sha,
        _db_time(promotion.cutoff_at),
        promotion.policy_version,
        promotion.verifier_name,
        promotion.verifier_version,
        promotion.verifier_code_sha256,
        promotion.verifier_config_sha256,
        promotion.status,
        promotion.supersedes_promotion_id,
        _db_time(promotion.recorded_at),
    )


_PROMOTION_COLUMNS = (
    "promotion_id",
    "idempotency_key",
    "scope_key",
    "revision",
    "issuer_id",
    "reporting_entity_id",
    "research_snapshot_id",
    "research_snapshot_sha256",
    "fact_generation_id",
    "fact_projection_seal_sha256",
    "source_inventory_set_json",
    "source_inventory_set_sha256",
    "narrative_bundles_json",
    "narrative_bundles_sha256",
    "cutoff_at",
    "policy_version",
    "verifier_name",
    "verifier_version",
    "verifier_code_sha256",
    "verifier_config_sha256",
    "status",
    "supersedes_promotion_id",
    "recorded_at",
)

_PROMOTION_INSERT_SQL = """
    INSERT INTO ask_retrieval_scope_promotions (
        promotion_id,idempotency_key,scope_key,revision,issuer_id,
        reporting_entity_id,research_snapshot_id,research_snapshot_sha256,
        fact_generation_id,fact_projection_seal_sha256,
        source_inventory_set_json,source_inventory_set_sha256,
        narrative_bundles_json,narrative_bundles_sha256,cutoff_at,
        policy_version,verifier_name,verifier_version,verifier_code_sha256,
        verifier_config_sha256,status,supersedes_promotion_id,recorded_at
    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    ON CONFLICT DO NOTHING
"""

_PROMOTION_SELECT_BY_IDEMPOTENCY_SQL = """
    SELECT promotion_id,idempotency_key,scope_key,revision,issuer_id,
           reporting_entity_id,research_snapshot_id,research_snapshot_sha256,
           fact_generation_id,fact_projection_seal_sha256,
           source_inventory_set_json,source_inventory_set_sha256,
           narrative_bundles_json,narrative_bundles_sha256,cutoff_at,
           policy_version,verifier_name,verifier_version,verifier_code_sha256,
           verifier_config_sha256,status,supersedes_promotion_id,recorded_at
    FROM ask_retrieval_scope_promotions
    WHERE idempotency_key=?
"""


def persist_retrieval_promotion(
    conn: sqlite3.Connection,
    promotion: RetrievalPromotion,
    *,
    runtime: LocalVectorRuntimeConfig | None = None,
) -> RetrievalPromotion:
    """Verify and append one promotion; exact idempotent replay is accepted."""

    promotion = RetrievalPromotion.model_validate(promotion.model_dump())
    values = _promotion_values(promotion)
    with _savepoint(conn, "persist_ask_retrieval_promotion"):
        conn.row_factory = sqlite3.Row
        existing_rows = conn.execute(
            "SELECT * FROM ask_retrieval_scope_promotions "
            "WHERE promotion_id=? OR idempotency_key=?",
            (promotion.promotion_id, promotion.idempotency_key),
        ).fetchall()
        if existing_rows:
            if len(existing_rows) != 1:
                raise ValueError("Ask retrieval promotion identity is split across rows")
            stored = _promotion_from_row(existing_rows[0])
            if _promotion_values(stored) != values:
                raise ValueError("immutable Ask retrieval promotion replay conflict")
            return stored
        verify_retrieval_promotion(conn, promotion, runtime=runtime)
        cursor = conn.execute(_PROMOTION_INSERT_SQL, values)
        if cursor.rowcount == 0:
            row = conn.execute(
                _PROMOTION_SELECT_BY_IDEMPOTENCY_SQL,
                (promotion.idempotency_key,),
            ).fetchone()
            if row is None or tuple(row) != values:
                raise ValueError("immutable Ask retrieval promotion replay conflict")
    return promotion


def _research_request(
    conn: sqlite3.Connection, research_snapshot_id: str
) -> ResearchSnapshotRequest:
    row = conn.execute(
        "SELECT request_json FROM research_snapshot_headers WHERE research_snapshot_id=?",
        (research_snapshot_id,),
    ).fetchone()
    if row is None:
        raise PromotionVerificationError(
            "research_snapshot_not_admitted", "Research Snapshot header is missing"
        )
    try:
        payload = json.loads(str(row[0]))
        return ResearchSnapshotRequest.model_validate(payload)
    except (json.JSONDecodeError, ValueError) as exc:
        raise PromotionVerificationError(
            "research_snapshot_not_admitted", "Research Snapshot request is invalid"
        ) from exc


def _current_inventory_ids(conn: sqlite3.Connection, issuer_id: str) -> tuple[str, ...]:
    try:
        rows = conn.execute(
            "SELECT snapshot_id FROM v_source_inventory_sealed_complete "
            "WHERE issuer_id=? ORDER BY snapshot_id",
            (issuer_id,),
        ).fetchall()
    except sqlite3.Error as exc:
        raise PromotionVerificationError(
            "source_inventory_incomplete", "sealed source inventory view is unavailable"
        ) from exc
    return tuple(str(row[0]) for row in rows)


def verify_retrieval_promotion(
    conn: sqlite3.Connection,
    promotion: RetrievalPromotion,
    *,
    runtime: LocalVectorRuntimeConfig | None = None,
) -> None:
    """Strictly verify a promotion's immutable coordinates and current coverage."""

    actual_identity = (
        promotion.policy_version,
        promotion.verifier_name,
        promotion.verifier_version,
        promotion.verifier_code_sha256,
        promotion.verifier_config_sha256,
    )
    if actual_identity != current_verifier_identity():
        raise PromotionVerificationError(
            "promotion_verifier_mismatch",
            "promotion policy/verifier identity differs from the current verifier artifacts",
        )

    try:
        admission = verify_research_snapshot(conn, promotion.research_snapshot_id)
    except (sqlite3.Error, ValueError) as exc:
        raise PromotionVerificationError(
            "research_snapshot_not_admitted", "Research Snapshot verification failed"
        ) from exc
    receipt = conn.execute(
        "SELECT research_snapshot_sha256 FROM research_snapshot_admission_receipts "
        "WHERE research_snapshot_id=?",
        (promotion.research_snapshot_id,),
    ).fetchone()
    if (
        admission.member_set_sha256 != promotion.research_snapshot_sha256
        or receipt is None
        or str(receipt[0]) != promotion.research_snapshot_sha256
    ):
        raise PromotionVerificationError(
            "research_snapshot_not_admitted",
            "strict Research Snapshot admission receipt is missing or different",
        )
    request = _research_request(conn, promotion.research_snapshot_id)
    expected_bundles = tuple(
        NarrativeBundle(
            corpus_manifest_id=item.corpus_manifest_id,
            lexical_index_run_id=item.lexical_index_run_id,
            vector_index_run_id=item.vector_index_run_id,
            embedding_promotion_id=item.embedding_promotion_id,
        )
        for item in request.corpus_bundles
    )
    if (
        request.canonical_fact_projection_run_id != promotion.fact_generation_id
        or expected_bundles != promotion.narrative_bundles
        or _utc(request.cutoff_at) != _utc(promotion.cutoff_at)
        or request.research_universe.issuer_id != promotion.issuer_id
        or promotion.reporting_entity_id
        not in request.research_universe.reporting_entity_ids
    ):
        raise PromotionVerificationError(
            "promotion_invalid",
            "promotion coordinates differ from its Research Snapshot request",
        )
    projection = conn.execute(
        "SELECT projection_seal_sha256 FROM canonical_fact_projection_seals "
        "WHERE generation_id=?",
        (promotion.fact_generation_id,),
    ).fetchone()
    if projection is None or str(projection[0]) != promotion.fact_projection_seal_sha256:
        raise PromotionVerificationError(
            "fact_projection_unsealed", "canonical fact projection seal is missing"
        )
    if promotion.status == "withdrawn":
        # Withdrawal must remain possible when current coverage or the semantic
        # runtime is unhealthy; the immutable historical coordinates above still
        # have to verify.
        return
    current_inventory = _current_inventory_ids(conn, promotion.issuer_id)
    if not current_inventory:
        raise PromotionVerificationError(
            "source_inventory_incomplete", "issuer has no sealed complete current inventory"
        )
    if current_inventory != promotion.source_inventory_ids:
        raise PromotionVerificationError(
            "promotion_stale", "current source inventory differs from the promotion"
        )
    manifest_inventory = tuple(
        sorted(
            {
                str(row[0])
                for bundle in promotion.narrative_bundles
                for row in conn.execute(
                    "SELECT snapshot_id FROM search_manifest_source_inventories "
                    "WHERE manifest_id=?",
                    (bundle.corpus_manifest_id,),
                ).fetchall()
            }
        )
    )
    if manifest_inventory != promotion.source_inventory_ids:
        raise PromotionVerificationError(
            "promotion_invalid",
            "promoted corpus manifests do not cover the exact inventory set",
        )
    try:
        runtime_config = runtime or LocalVectorRuntimeConfig.from_environment()
    except ValueError as exc:
        raise PromotionVerificationError(
            "semantic_runtime_unavailable", "local semantic runtime configuration is invalid"
        ) from exc
    if runtime_config is None:
        raise PromotionVerificationError(
            "semantic_runtime_unavailable", "local semantic runtime is not configured"
        )
    for bundle in promotion.narrative_bundles:
        if bundle.vector_index_run_id is None or bundle.embedding_promotion_id is None:
            raise PromotionVerificationError(
                "semantic_projection_unsealed", "semantic bundle coordinates are missing"
            )
        try:
            _verify_index_root(
                conn,
                vector_index_run_id=bundle.vector_index_run_id,
                configured_root=runtime_config.index_root,
            )
            ExactSemanticRuntime.from_local_ledger(
                conn,
                vector_index_run_id=bundle.vector_index_run_id,
                embedding_promotion_id=bundle.embedding_promotion_id,
                runtime_root=runtime_config.runtime_root,
            )
        except Exception as exc:
            raise PromotionVerificationError(
                "semantic_runtime_unavailable",
                f"semantic runtime is unavailable for {bundle.corpus_manifest_id}",
            ) from exc


def _verify_index_root(
    conn: sqlite3.Connection,
    *,
    vector_index_run_id: str,
    configured_root: Path,
) -> None:
    row = conn.execute(
        "SELECT storage_uri FROM search_projection_seals "
        "WHERE index_run_id=? AND index_kind='vector'",
        (vector_index_run_id,),
    ).fetchone()
    if row is None:
        raise ValueError("sealed vector projection is missing")
    storage_uri = str(row[0])
    prefix = "lance://"
    suffix = "#evidence_chunks"
    if not storage_uri.startswith(prefix) or not storage_uri.endswith(suffix):
        raise ValueError("sealed vector projection storage URI is invalid")
    dataset_path = Path(storage_uri[len(prefix) : -len(suffix)])
    if not dataset_path.is_absolute() or any(part == ".." for part in dataset_path.parts):
        raise ValueError("sealed vector projection path is unsafe")
    configured = configured_root.resolve()
    projection_root = dataset_path.parent.resolve()
    try:
        projection_root.relative_to(configured)
    except ValueError as exc:
        raise ValueError("sealed vector projection escapes configured index root") from exc


def _promotion_from_row(row: sqlite3.Row) -> RetrievalPromotion:
    inventory_json = str(row["source_inventory_set_json"])
    bundles_json = str(row["narrative_bundles_json"])
    try:
        inventory_payload = json.loads(inventory_json)
        bundle_payload = json.loads(bundles_json)
    except json.JSONDecodeError as exc:
        raise ValueError("Ask retrieval promotion JSON is invalid") from exc
    if (
        canonical_json(inventory_payload) != inventory_json
        or canonical_json(bundle_payload) != bundles_json
        or digest_text(inventory_json) != str(row["source_inventory_set_sha256"])
        or digest_text(bundles_json) != str(row["narrative_bundles_sha256"])
    ):
        raise ValueError("Ask retrieval promotion JSON commitment mismatch")
    promotion = RetrievalPromotion(
        promotion_id=str(row["promotion_id"]),
        idempotency_key=str(row["idempotency_key"]),
        scope_key=str(row["scope_key"]),
        revision=int(row["revision"]),
        issuer_id=str(row["issuer_id"]),
        reporting_entity_id=str(row["reporting_entity_id"]),
        research_snapshot_id=str(row["research_snapshot_id"]),
        research_snapshot_sha256=str(row["research_snapshot_sha256"]),
        fact_generation_id=str(row["fact_generation_id"]),
        fact_projection_seal_sha256=str(row["fact_projection_seal_sha256"]),
        source_inventory_ids=tuple(inventory_payload),
        narrative_bundles=tuple(
            NarrativeBundle.model_validate(item)
            for item in bundle_payload
        ),
        cutoff_at=_datetime(row["cutoff_at"]),
        policy_version=str(row["policy_version"]),
        verifier_name=str(row["verifier_name"]),
        verifier_version=str(row["verifier_version"]),
        verifier_code_sha256=str(row["verifier_code_sha256"]),
        verifier_config_sha256=str(row["verifier_config_sha256"]),
        status=cast(Literal["promoted", "withdrawn"], str(row["status"])),
        supersedes_promotion_id=(
            None
            if row["supersedes_promotion_id"] is None
            else str(row["supersedes_promotion_id"])
        ),
        recorded_at=_datetime(row["recorded_at"]),
    )
    expected = _promotion_payloads(promotion)
    stored = (
        inventory_json,
        str(row["source_inventory_set_sha256"]),
        bundles_json,
        str(row["narrative_bundles_sha256"]),
    )
    if expected != stored:
        raise ValueError("Ask retrieval promotion canonical payload mismatch")
    return promotion


def assess_retrieval_readiness(
    conn: sqlite3.Connection,
    scopes: tuple[RetrievalScope, ...],
    *,
    runtime: LocalVectorRuntimeConfig | None = None,
) -> RetrievalReadiness:
    """Return ready only when every requested issuer is exactly covered."""

    if not scopes:
        return RetrievalReadiness(
            outcome="unavailable",
            reason_code="empty_scope",
            details="no reporting scope was supplied",
        )
    if len({scope.scope_key for scope in scopes}) != len(scopes):
        return RetrievalReadiness(
            outcome="unavailable",
            reason_code="scope_identity_incomplete",
            details="reporting scopes are duplicated or ambiguous",
        )
    conn.row_factory = sqlite3.Row
    ready: list[ReadyRetrievalScope] = []
    for scope in scopes:
        row = conn.execute(
            "SELECT * FROM v_ask_retrieval_scope_current WHERE scope_key=?",
            (scope.scope_key,),
        ).fetchone()
        if row is None:
            return RetrievalReadiness(
                outcome="coverage_incomplete",
                reason_code="promotion_missing",
                details=f"no retrieval promotion exists for {scope.scope_key}",
            )
        try:
            promotion = _promotion_from_row(row)
        except (json.JSONDecodeError, ValueError) as exc:
            return RetrievalReadiness(
                outcome="unavailable",
                reason_code="promotion_invalid",
                details=f"promotion is invalid for {scope.scope_key}: {exc}",
            )
        if (
            promotion.issuer_id != scope.issuer_id
            or promotion.reporting_entity_id != scope.reporting_entity_id
        ):
            return RetrievalReadiness(
                outcome="unavailable",
                reason_code="scope_identity_incomplete",
                details=f"promotion identity differs for {scope.scope_key}",
            )
        if promotion.status != "promoted":
            return RetrievalReadiness(
                outcome="coverage_incomplete",
                reason_code="promotion_withdrawn",
                details=f"retrieval promotion is withdrawn for {scope.scope_key}",
            )
        try:
            verify_retrieval_promotion(conn, promotion, runtime=runtime)
        except PromotionVerificationError as exc:
            outcome: ReadinessOutcome = (
                "coverage_incomplete"
                if exc.reason_code
                in {
                    "promotion_stale",
                    "source_inventory_incomplete",
                    "fact_projection_unsealed",
                    "semantic_projection_unsealed",
                    "semantic_runtime_unavailable",
                }
                else "unavailable"
            )
            return RetrievalReadiness(
                outcome=outcome,
                reason_code=exc.reason_code,
                details=f"{scope.scope_key}: {exc.details}",
            )
        ready.append(ReadyRetrievalScope(scope=scope, promotion=promotion))
    cutoffs = {_utc(item.promotion.cutoff_at) for item in ready}
    if len(cutoffs) != 1:
        return RetrievalReadiness(
            outcome="coverage_incomplete",
            reason_code="incomparable_snapshot_cutoffs",
            details="multi-issuer Ask requires one common research cutoff",
        )
    return RetrievalReadiness(
        outcome="ready",
        reason_code="ready",
        details="all requested reporting scopes are promoted and sealed",
        scopes=tuple(ready),
    )


def build_sealed_retrieval_plan(
    readiness: RetrievalReadiness,
    *,
    request_id: str,
    question: str,
    created_at: datetime,
    candidate_limit: int = 100,
    result_limit: int = 20,
) -> SealedRetrievalPlan:
    if readiness.outcome != "ready" or not readiness.scopes:
        raise ValueError("cannot build retrieval requests from incomplete readiness")
    normalized_question = " ".join(question.split())
    if not normalized_question:
        raise ValueError("question is required")
    requests: list[HeterogeneousRetrievalRequest] = []
    for scope in readiness.scopes:
        seed = canonical_json(
            {
                "promotion_id": scope.promotion.promotion_id,
                "question": normalized_question,
                "request_id": request_id,
                "scope_key": scope.scope.scope_key,
            }
        )
        identity = digest_text(seed)
        requests.append(
            HeterogeneousRetrievalRequest(
                trace_id=f"ask-hetero:{identity}",
                idempotency_key=(
                    f"ask-request:{request_id}:{retrieval_query_sha256(normalized_question)}:"
                    f"{scope.promotion.promotion_id}"
                ),
                research_snapshot_id=scope.promotion.research_snapshot_id,
                fact_generation_id=scope.promotion.fact_generation_id,
                narrative_bundles=scope.promotion.narrative_bundles,
                query_text=normalized_question,
                candidate_limit=candidate_limit,
                result_limit=result_limit,
                filters=RetrievalFilters(
                    reporting_entity_id=scope.scope.reporting_entity_id
                ),
                cutoff_at=scope.promotion.cutoff_at,
                recorded_at=created_at,
            )
        )
    return SealedRetrievalPlan(
        request_id=request_id,
        question=normalized_question,
        scopes=readiness.scopes,
        requests=tuple(requests),
        created_at=created_at,
    )


def execute_sealed_retrieval_plan(
    conn: sqlite3.Connection,
    plan: SealedRetrievalPlan,
    *,
    local_vector_runtime: LocalVectorRuntimeConfig | None = None,
) -> SealedRetrievalExecution:
    """Execute every issuer trace atomically; one failure yields no partial trace set."""

    receipts: list[HeterogeneousRetrievalReceipt] = []
    with _savepoint(conn, "execute_sealed_ask_retrieval"):
        conn.row_factory = sqlite3.Row
        for planned_scope, request in zip(
            plan.scopes, plan.requests, strict=True
        ):
            current_row = conn.execute(
                "SELECT * FROM v_ask_retrieval_scope_current WHERE scope_key=?",
                (planned_scope.scope.scope_key,),
            ).fetchone()
            if current_row is None:
                raise PromotionVerificationError(
                    "promotion_missing",
                    f"retrieval promotion disappeared for {planned_scope.scope.scope_key}",
                )
            try:
                current = _promotion_from_row(current_row)
            except (json.JSONDecodeError, ValueError) as exc:
                raise PromotionVerificationError(
                    "promotion_invalid",
                    f"current promotion is invalid: {exc}",
                ) from exc
            if current != planned_scope.promotion or current.status != "promoted":
                raise PromotionVerificationError(
                    "promotion_stale",
                    f"retrieval promotion changed for {planned_scope.scope.scope_key}",
                )
            verify_retrieval_promotion(
                conn,
                current,
                runtime=local_vector_runtime,
            )
            try:
                receipt = retrieve_heterogeneous(
                    conn,
                    request,
                    local_vector_runtime=local_vector_runtime,
                )
                verified = verify_heterogeneous_retrieval_trace(
                    conn,
                    receipt.trace_id,
                    local_vector_runtime=local_vector_runtime,
                )
            except (HeterogeneousRetrievalError, ValueError) as exc:
                raise PromotionVerificationError(
                    "retrieval_failed",
                    f"sealed heterogeneous retrieval failed: {exc}",
                ) from exc
            if verified.trace_sha256 != receipt.trace_sha256:
                raise PromotionVerificationError(
                    "trace_verification_failed",
                    "retrieval receipt differs from its strict persisted verification",
                )
            receipts.append(verified)
    return SealedRetrievalExecution(plan=plan, receipts=tuple(receipts))


def load_verified_trace_evidence(
    conn: sqlite3.Connection,
    trace_id: str,
    *,
    start_number: int = 1,
    local_vector_runtime: LocalVectorRuntimeConfig | None = None,
) -> tuple[SealedEvidenceItem, ...]:
    """Load prompt/UI DTOs only after strict verification of the persisted trace."""

    receipt = verify_heterogeneous_retrieval_trace(
        conn,
        trace_id,
        local_vector_runtime=local_vector_runtime,
    )
    conn.row_factory = sqlite3.Row
    header = conn.execute(
        "SELECT cutoff_at FROM heterogeneous_retrieval_trace_headers WHERE trace_id=?",
        (trace_id,),
    ).fetchone()
    if header is None:
        raise PromotionVerificationError(
            "trace_verification_failed", "retrieval trace header disappeared"
        )
    items: list[SealedEvidenceItem] = []
    for index, result in enumerate(receipt.ordered_results, start=start_number):
        result_ordinal = _integer(result["result_ordinal"])
        candidate = conn.execute(
            "SELECT candidate.* FROM heterogeneous_retrieval_trace_results result "
            "JOIN heterogeneous_retrieval_trace_candidates candidate "
            "ON candidate.trace_id=result.trace_id "
            "AND candidate.candidate_ordinal=result.candidate_ordinal "
            "WHERE result.trace_id=? AND result.result_ordinal=?",
            (trace_id, result_ordinal),
        ).fetchone()
        if candidate is None:
            raise PromotionVerificationError(
                "trace_verification_failed", "verified result candidate is missing"
            )
        kind = str(candidate["candidate_kind"])
        if kind == "narrative":
            item = _narrative_item(
                conn,
                receipt,
                candidate,
                result,
                n=index,
                cutoff=_datetime(header["cutoff_at"]),
            )
        elif kind == "fact":
            item = _fact_item(
                conn,
                receipt,
                candidate,
                result,
                n=index,
                cutoff=_datetime(header["cutoff_at"]),
            )
        else:
            raise PromotionVerificationError(
                "trace_verification_failed", f"unknown candidate kind {kind!r}"
            )
        items.append(item)
    return tuple(items)


def _document_metadata(
    conn: sqlite3.Connection,
    *,
    document_version_id: str,
) -> sqlite3.Row:
    row = conn.execute(
        "SELECT document.document_version_id,document.issuer_id,document.ticker,"
        "document.document_type,document.form_type,document.period_start,"
        "document.period_end,observation.source_url "
        "FROM evidence_document_versions document "
        "JOIN evidence_source_observations observation "
        "ON observation.observation_id=document.observation_id "
        "WHERE document.document_version_id=?",
        (document_version_id,),
    ).fetchone()
    if row is None:
        raise PromotionVerificationError(
            "trace_verification_failed", "candidate document version is missing"
        )
    return row


def _narrative_item(
    conn: sqlite3.Connection,
    receipt: HeterogeneousRetrievalReceipt,
    candidate: sqlite3.Row,
    result: dict[str, object],
    *,
    n: int,
    cutoff: datetime,
) -> SealedEvidenceItem:
    row = conn.execute(
        "SELECT chunk.text,chunk.evidence_node_id,chunk.char_start,chunk.char_end,"
        "node.node_kind,node.locator_json,run.document_version_id "
        "FROM search_chunks chunk "
        "JOIN evidence_nodes node ON node.node_id=chunk.evidence_node_id "
        "JOIN evidence_extraction_runs run "
        "ON run.extraction_run_id=node.extraction_run_id "
        "WHERE chunk.chunk_id=? AND chunk.content_sha256=?",
        (candidate["candidate_id"], candidate["source_commitment_sha256"]),
    ).fetchone()
    if row is None:
        raise PromotionVerificationError(
            "trace_verification_failed", "narrative source commitment is missing"
        )
    document = _document_metadata(conn, document_version_id=str(row["document_version_id"]))
    locator = (
        {}
        if row["locator_json"] is None
        else cast(dict[str, object], json.loads(str(row["locator_json"])))
    )
    locator.update({"char_start": int(row["char_start"]), "char_end": int(row["char_end"])})
    period = None if document["period_end"] is None else str(document["period_end"])[:10]
    doc_type = str(document["form_type"] or document["document_type"])
    ticker = None if document["ticker"] is None else str(document["ticker"])
    label = " · ".join(part for part in (ticker, doc_type, period) if part)
    version_id = str(document["document_version_id"])
    node_id = str(row["evidence_node_id"])
    return SealedEvidenceItem(
        n=n,
        kind="narrative",
        label=label or doc_type,
        text=str(row["text"]),
        href=f"/source/version/{quote(version_id, safe='')}?node={quote(node_id, safe='')}",
        source_url=str(document["source_url"]),
        ticker=ticker,
        doc_type=doc_type,
        period=period,
        value=None,
        trace_id=receipt.trace_id,
        result_ordinal=_integer(result["result_ordinal"]),
        candidate_id=str(candidate["candidate_id"]),
        source_commitment_sha256=str(candidate["source_commitment_sha256"]),
        research_snapshot_id=receipt.research_snapshot_id,
        as_of_at=cutoff,
        document_version_id=version_id,
        evidence_node_id=node_id,
        node_kind=str(row["node_kind"]),
        locator=locator,
        score=str(result["final_score"]),
    )


def _fact_item(
    conn: sqlite3.Connection,
    receipt: HeterogeneousRetrievalReceipt,
    candidate: sqlite3.Row,
    result: dict[str, object],
    *,
    n: int,
    cutoff: datetime,
) -> SealedEvidenceItem:
    row = conn.execute(
        "SELECT * FROM canonical_fact_projection_entries "
        "WHERE canonical_metric_cell_id=? AND entry_sha256=? "
        "AND change_kind='upsert' ORDER BY recorded_at DESC LIMIT 1",
        (candidate["candidate_id"], candidate["source_commitment_sha256"]),
    ).fetchone()
    if row is None:
        raise PromotionVerificationError(
            "trace_verification_failed", "fact source commitment is missing"
        )
    version_id = str(row["evidence_document_version_id"])
    node_id = str(row["evidence_node_id"])
    document = _document_metadata(conn, document_version_id=version_id)
    locator = cast(dict[str, object], json.loads(str(row["evidence_locator_json"])))
    ticker = None if document["ticker"] is None else str(document["ticker"])
    doc_type = str(document["form_type"] or document["document_type"])
    period = str(row["period_end"])[:10]
    value = None if row["canonical_value"] is None else str(row["canonical_value"])
    metric = str(row["canonical_metric_name"])
    unit = str(row["unit_key"])
    currency = None if row["currency"] is None else str(row["currency"])
    value_text = " ".join(part for part in (value, currency or unit) if part)
    return SealedEvidenceItem(
        n=n,
        kind="fact",
        label=" · ".join(part for part in (ticker, metric, period) if part),
        text=f"{metric}: {value_text} ({period})",
        href=f"/source/version/{quote(version_id, safe='')}?node={quote(node_id, safe='')}",
        source_url=str(document["source_url"]),
        ticker=ticker,
        doc_type=doc_type,
        period=period,
        value=value_text or None,
        trace_id=receipt.trace_id,
        result_ordinal=_integer(result["result_ordinal"]),
        candidate_id=str(candidate["candidate_id"]),
        source_commitment_sha256=str(candidate["source_commitment_sha256"]),
        research_snapshot_id=receipt.research_snapshot_id,
        as_of_at=cutoff,
        document_version_id=version_id,
        evidence_node_id=node_id,
        node_kind="fact",
        locator=locator,
        score=str(result["final_score"]),
    )


__all__ = [
    "ASK_RETRIEVAL_POLICY_VERSION",
    "ASK_RETRIEVAL_VERIFIER_NAME",
    "ASK_RETRIEVAL_VERIFIER_VERSION",
    "PromotionVerificationError",
    "ReadyRetrievalScope",
    "RetrievalPromotion",
    "RetrievalReadiness",
    "RetrievalScope",
    "SealedEvidenceItem",
    "SealedRetrievalExecution",
    "SealedRetrievalPlan",
    "assess_retrieval_readiness",
    "build_sealed_retrieval_plan",
    "current_verifier_identity",
    "execute_sealed_retrieval_plan",
    "load_verified_trace_evidence",
    "persist_retrieval_promotion",
    "verify_retrieval_promotion",
]
