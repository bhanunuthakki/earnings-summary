"""Materialize bounded owner-approved IR candidates from sealed observations."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import urllib.parse
from collections import Counter
from contextlib import suppress
from datetime import date, datetime
from pathlib import Path
from typing import cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from ir_pipeline.approved_ir_observation_capture import (
    ApprovedIrObservationBundle,
    SealedObservationArtifact,
    load_approved_ir_observation_bundle,
)
from models.documents import DocType
from pipeline.approved_ir_catalog import (
    ApprovedIrCatalog,
    CatalogDisposition,
    IrCatalogEntry,
    IrCatalogError,
    IrSourceObservation,
    build_catalog,
)
from pipeline.approved_ir_rubrik import (
    load_rubrik_row_observations,
    parse_rubrik_quarter_rows,
)
from pipeline.approved_ir_wix import (
    load_wix_rendered_observations,
    parse_wix_visible_quarters,
)
from pipeline.ir_approval_store import (
    EvidenceReference,
    IrApprovalError,
    IrCandidate,
    IrCandidateRequest,
    get_candidate_by_request_id,
    persist_candidate,
)
from pipeline.source_policy import AdapterKey, issuer_policy
from provenance.evidence_ledger import ContentBlob, EvidenceLedger, SourceObservation
from provenance.evidence_links import BlobLocationObservation, EvidenceLinkLedger
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite

_MAX_BUNDLE_BYTES = 50_000_000
_COLLECTOR_VERSION = "approved-ir-observation-candidate@1"
_APPROVED_PERIOD_SHAPE: dict[AdapterKey, dict[date, tuple[DocType, ...]]] = {
    AdapterKey.WIX_VISIBLE_QUARTER: {
        date(2026, 6, 30): (
            DocType.IR_PRESS_RELEASE,
            DocType.IR_PRESENTATION,
            DocType.IR_INVESTOR_UPDATE,
            DocType.IR_TRANSCRIPT,
        ),
        date(2026, 3, 31): (
            DocType.IR_PRESS_RELEASE,
            DocType.IR_PRESENTATION,
            DocType.IR_INVESTOR_UPDATE,
            DocType.IR_TRANSCRIPT,
        ),
        date(2025, 12, 31): (
            DocType.IR_PRESS_RELEASE,
            DocType.IR_PRESENTATION,
            DocType.IR_INVESTOR_UPDATE,
            DocType.IR_TRANSCRIPT,
        ),
        date(2025, 9, 30): (
            DocType.IR_PRESS_RELEASE,
            DocType.IR_PRESENTATION,
            DocType.IR_INVESTOR_UPDATE,
            DocType.IR_TRANSCRIPT,
        ),
        date(2025, 6, 30): (
            DocType.IR_PRESS_RELEASE,
            DocType.IR_PRESENTATION,
            DocType.IR_INVESTOR_UPDATE,
            DocType.IR_TRANSCRIPT,
        ),
    },
    AdapterKey.RUBRIK_QUARTER_TABLE: {
        date(2026, 4, 30): (DocType.IR_PRESS_RELEASE, DocType.IR_PRESENTATION),
        date(2026, 1, 31): (DocType.IR_PRESS_RELEASE, DocType.IR_PRESENTATION),
        date(2025, 10, 31): (DocType.IR_PRESS_RELEASE, DocType.IR_PRESENTATION),
        date(2025, 7, 31): (DocType.IR_PRESS_RELEASE, DocType.IR_PRESENTATION),
        date(2025, 4, 30): (DocType.IR_PRESS_RELEASE, DocType.IR_PRESENTATION),
    },
}


class IrCandidateCallerError(ValueError):
    """The sealed artifact cannot safely materialize the approved scope."""


class _FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class IrCandidateCallerRequest(_FrozenModel):
    issuer_identifier: str = Field(min_length=1, max_length=128)
    recorded_by: str = Field(min_length=1, max_length=256)
    recorded_at: datetime
    reason: str = Field(min_length=1, max_length=4096)

    @field_validator("issuer_identifier", "recorded_by", "reason")
    @classmethod
    def _non_blank(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("value cannot be blank")
        return normalized

    @field_validator("recorded_at")
    @classmethod
    def _naive_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is not None:
            raise ValueError("recorded_at must use the repository naive-UTC convention")
        return value


class PlannedIrCandidate(_FrozenModel):
    request_id: str
    quarter_end: date
    title: str
    url: str
    disposition: CatalogDisposition
    doc_type: DocType
    observation_key: str
    observation_raw_sha256: str
    evidence_locator: str
    evidence: tuple[EvidenceReference, ...]


class IrCandidatePlan(_FrozenModel):
    issuer_id: str
    ticker: str
    adapter_key: AdapterKey
    bundle_input_sha256: str
    bundle_sha256: str
    catalog_sha256: str
    reported_quarters: tuple[date, ...]
    excluded_webcast_count: int = Field(ge=0)
    excluded_out_of_scope_count: int = Field(ge=0)
    sec_handoff_count: int = Field(ge=0)
    candidates: tuple[PlannedIrCandidate, ...]
    request: IrCandidateCallerRequest
    catalog: ApprovedIrCatalog = Field(exclude=True)
    bundle: ApprovedIrObservationBundle = Field(exclude=True)

    @property
    def candidate_count(self) -> int:
        return len(self.candidates)


class IrCandidateApplyReceipt(_FrozenModel):
    issuer_id: str
    ticker: str
    catalog_sha256: str
    bundle_input_sha256: str
    created: int = Field(ge=0)
    replayed: int = Field(ge=0)
    total: int = Field(ge=0)
    evidence_records_created: int = Field(ge=0)
    candidate_ids: tuple[str, ...]


def plan_ir_candidates(
    sealed_bundle_bytes: bytes,
    request: IrCandidateCallerRequest,
) -> IrCandidatePlan:
    """Verify one collector-sealed bundle and build its exact bounded write plan."""

    request = IrCandidateCallerRequest.model_validate(request.model_dump())
    if not sealed_bundle_bytes:
        raise IrCandidateCallerError("IR observation bundle is empty")
    if len(sealed_bundle_bytes) > _MAX_BUNDLE_BYTES:
        raise IrCandidateCallerError("IR observation bundle exceeds the caller size limit")
    try:
        bundle = load_approved_ir_observation_bundle(sealed_bundle_bytes)
        policy = issuer_policy(request.issuer_identifier)
        bundle_policy = issuer_policy(bundle.issuer_identifier)
        if bundle_policy.issuer_id != policy.issuer_id:
            raise IrCandidateCallerError("sealed bundle issuer does not match the request")
        if bundle.authority_url != policy.ir.authority_url:
            raise IrCandidateCallerError("sealed bundle authority does not match issuer policy")
        observations_text = bundle.normalized_observations_bytes.decode("utf-8")
        if policy.ir.adapter_key is AdapterKey.WIX_VISIBLE_QUARTER:
            observations = load_wix_rendered_observations(observations_text)
            parsed = parse_wix_visible_quarters(observations, policy=policy)
        elif policy.ir.adapter_key is AdapterKey.RUBRIK_QUARTER_TABLE:
            observations = load_rubrik_row_observations(observations_text)
            parsed = parse_rubrik_quarter_rows(observations, policy=policy)
        else:  # pragma: no cover - closed registry currently has two adapters
            raise IrCandidateCallerError("issuer has no production IR candidate adapter")
        catalog = build_catalog(policy, parsed)
    except IrCandidateCallerError:
        raise
    except (IrCatalogError, ValidationError, UnicodeDecodeError, ValueError) as exc:
        raise IrCandidateCallerError(f"sealed IR observation failed validation: {exc}") from None

    approved_period_shape = _APPROVED_PERIOD_SHAPE[policy.ir.adapter_key]
    if set(catalog.reported_quarters) != set(approved_period_shape):
        raise IrCandidateCallerError(
            "IR bundle does not contain the exact approved reporting periods"
        )
    rendered_by_key = _rendered_artifacts_by_key(bundle)
    observation_by_key = {item.observation_key: item for item in catalog.observations}
    admitted: list[tuple[IrCatalogEntry, IrSourceObservation, SealedObservationArtifact]] = []
    excluded_out_of_scope = 0
    sec_handoffs = 0
    for entry in catalog.entries:
        if entry.disposition is CatalogDisposition.SEC_HANDOFF:
            sec_handoffs += 1
            continue
        expected_types = approved_period_shape.get(entry.quarter_end, ())
        if entry.doc_type not in expected_types:
            excluded_out_of_scope += 1
            continue
        observation = observation_by_key.get(entry.observation_key)
        artifact = rendered_by_key.get(entry.observation_key)
        if observation is None or artifact is None:
            raise IrCandidateCallerError("catalog candidate lost its sealed source observation")
        if observation.raw_sha256 != artifact.sha256:
            raise IrCandidateCallerError("catalog observation hash does not match sealed bytes")
        _validate_candidate_link_proof(entry, artifact)
        admitted.append((entry, observation, artifact))

    actual_shape: dict[date, Counter[DocType]] = {
        quarter: Counter() for quarter in approved_period_shape
    }
    for entry, _observation, _artifact in admitted:
        if entry.doc_type is None:
            raise IrCandidateCallerError("approved IR candidate has no document type")
        actual_shape[entry.quarter_end][entry.doc_type] += 1
    if any(
        actual_shape[quarter] != Counter(expected)
        for quarter, expected in approved_period_shape.items()
    ):
        raise IrCandidateCallerError(
            "IR bundle does not match the approved per-period document shape"
        )
    urls = [entry.url for entry, _observation, _artifact in admitted]
    if len(urls) != len(set(urls)):
        raise IrCandidateCallerError("IR bundle reuses one candidate URL across reporting periods")

    bundle_input_sha256 = hashlib.sha256(sealed_bundle_bytes).hexdigest()
    catalog_bytes = catalog.canonical_json().encode("utf-8")
    catalog_sha256 = hashlib.sha256(catalog_bytes).hexdigest()
    ticker = policy.ticker_aliases[0]
    candidates = tuple(
        _planned_candidate(
            entry,
            observation,
            artifact,
            issuer_id=policy.issuer_id,
            catalog_sha256=catalog_sha256,
        )
        for entry, observation, artifact in admitted
    )
    return IrCandidatePlan(
        issuer_id=policy.issuer_id,
        ticker=ticker,
        adapter_key=policy.ir.adapter_key,
        bundle_input_sha256=bundle_input_sha256,
        bundle_sha256=bundle.bundle_sha256,
        catalog_sha256=catalog_sha256,
        reported_quarters=catalog.reported_quarters,
        excluded_webcast_count=parsed.excluded_webcast_count,
        excluded_out_of_scope_count=excluded_out_of_scope,
        sec_handoff_count=sec_handoffs,
        candidates=candidates,
        request=request,
        catalog=catalog,
        bundle=bundle,
    )


def apply_ir_candidate_plan(
    db_path: Path,
    blob_root: Path,
    sealed_bundle_bytes: bytes,
    plan: IrCandidatePlan,
) -> IrCandidateApplyReceipt:
    """Persist sealed evidence and every candidate as one rollback-safe unit."""

    if hashlib.sha256(sealed_bundle_bytes).hexdigest() != plan.bundle_input_sha256:
        raise IrCandidateCallerError("apply bundle bytes differ from the reviewed plan")
    verified = plan_ir_candidates(sealed_bundle_bytes, plan.request)
    if _public_plan_payload(verified) != _public_plan_payload(plan):
        raise IrCandidateCallerError("apply plan differs from the verified sealed bundle")

    connection = connect_sqlite(db_path, role=SQLiteConnectionRole.WRITER, schema_preflight=True)
    created = 0
    replayed = 0
    evidence_created = 0
    candidates: list[IrCandidate] = []
    new_blob_paths: list[Path] = []
    try:
        connection.execute("BEGIN IMMEDIATE")
        evidence_created = _persist_plan_evidence(
            connection,
            blob_root,
            sealed_bundle_bytes,
            plan,
            new_blob_paths,
        )
        for item in plan.candidates:
            existing = get_candidate_by_request_id(connection, item.request_id)
            recorded_at = plan.request.recorded_at
            if existing is not None:
                _verify_replay_metadata(existing, plan, item)
                recorded_at = existing.recorded_at
            result = persist_candidate(
                connection,
                IrCandidateRequest(
                    request_id=item.request_id,
                    ticker=plan.ticker,
                    catalog=plan.catalog,
                    candidate_url=item.url,
                    recorded_by=plan.request.recorded_by,
                    recorded_at=recorded_at,
                    reason=plan.request.reason,
                    evidence=item.evidence,
                ),
            )
            if result.outcome == "created":
                created += 1
            else:
                replayed += 1
            candidates.append(result.candidate)
        connection.commit()
    except IrCandidateCallerError:
        connection.rollback()
        _remove_new_blobs(new_blob_paths, blob_root)
        raise
    except (IrApprovalError, ValueError) as exc:
        connection.rollback()
        _remove_new_blobs(new_blob_paths, blob_root)
        raise IrCandidateCallerError(str(exc)) from None
    except Exception:
        connection.rollback()
        _remove_new_blobs(new_blob_paths, blob_root)
        raise
    finally:
        connection.close()
    return IrCandidateApplyReceipt(
        issuer_id=plan.issuer_id,
        ticker=plan.ticker,
        catalog_sha256=plan.catalog_sha256,
        bundle_input_sha256=plan.bundle_input_sha256,
        created=created,
        replayed=replayed,
        total=len(candidates),
        evidence_records_created=evidence_created,
        candidate_ids=tuple(candidate.candidate_id for candidate in candidates),
    )


def load_ir_observation_artifact(path: Path) -> bytes:
    """Read one bounded local sealed bundle without following a network locator."""

    try:
        with path.open("rb") as handle:
            artifact_bytes = handle.read(_MAX_BUNDLE_BYTES + 1)
    except OSError as exc:
        raise IrCandidateCallerError("IR observation bundle is unavailable") from exc
    if len(artifact_bytes) > _MAX_BUNDLE_BYTES:
        raise IrCandidateCallerError("IR observation bundle exceeds the caller size limit")
    return artifact_bytes


def _rendered_artifacts_by_key(
    bundle: ApprovedIrObservationBundle,
) -> dict[str, SealedObservationArtifact]:
    return {
        artifact.observation_key: artifact
        for artifact in bundle.artifacts
        if str(artifact.role) in {"rendered_state", "ObservationArtifactRole.RENDERED_STATE"}
    }


def _planned_candidate(
    entry: IrCatalogEntry,
    observation: IrSourceObservation,
    artifact: SealedObservationArtifact,
    *,
    issuer_id: str,
    catalog_sha256: str,
) -> PlannedIrCandidate:
    if entry.doc_type is None:
        raise IrCandidateCallerError("approved IR candidate has no document type")
    request_hash = _digest(catalog_sha256, entry.url)
    artifact_observation_id = _artifact_observation_id(artifact)
    catalog_observation_id = f"ir-catalog:{catalog_sha256}"
    return PlannedIrCandidate(
        request_id=f"ir-candidate:{request_hash}",
        quarter_end=entry.quarter_end,
        title=entry.title,
        url=entry.url,
        disposition=entry.disposition,
        doc_type=entry.doc_type,
        observation_key=entry.observation_key,
        observation_raw_sha256=observation.raw_sha256,
        evidence_locator=entry.evidence_locator,
        evidence=(
            EvidenceReference(
                evidence_id=artifact_observation_id,
                locator=(
                    f"evidence://source-observation/{artifact_observation_id}"
                    f"#{entry.evidence_locator}"
                ),
                content_sha256=artifact.sha256,
            ),
            EvidenceReference(
                evidence_id=catalog_observation_id,
                locator=f"evidence://source-observation/{catalog_observation_id}",
                content_sha256=catalog_sha256,
            ),
        ),
    )


def _persist_plan_evidence(
    connection: object,
    blob_root: Path,
    sealed_bundle_bytes: bytes,
    plan: IrCandidatePlan,
    new_paths: list[Path],
) -> int:
    # sqlite3.Connection is intentionally imported through the runtime boundary;
    # validate the concrete protocol at the only private persistence entrypoint.
    import sqlite3

    if not isinstance(connection, sqlite3.Connection):
        raise TypeError("IR evidence persistence requires sqlite3.Connection")
    ledger = EvidenceLedger(connection)
    links = EvidenceLinkLedger(connection)
    records_created = 0
    records: list[tuple[str, bytes, str, str, datetime, datetime]] = [
        (
            f"ir-bundle:{plan.bundle_input_sha256}",
            sealed_bundle_bytes,
            "application/vnd.earnings-summary.ir-observation-bundle+json",
            plan.catalog.authority_url,
            plan.bundle.captured_at,
            plan.bundle.captured_at,
        ),
        (
            f"ir-catalog:{plan.catalog_sha256}",
            plan.catalog.canonical_json().encode("utf-8"),
            "application/vnd.earnings-summary.approved-ir-catalog+json",
            plan.catalog.authority_url,
            plan.bundle.captured_at,
            plan.bundle.captured_at,
        ),
    ]
    records.extend(
        (
            _artifact_observation_id(artifact),
            artifact.content_bytes,
            artifact.media_type,
            artifact.source_url,
            artifact.observed_at,
            artifact.retrieved_at,
        )
        for artifact in plan.bundle.artifacts
    )
    policy_sha256 = issuer_policy(plan.issuer_id).policy_sha256
    for observation_id, content, media_type, source_url, observed_at, retrieved_at in records:
        digest = hashlib.sha256(content).hexdigest()
        existing_blob = connection.execute(
            "SELECT byte_size,media_type,storage_uri,recorded_at "
            "FROM evidence_content_blobs WHERE sha256=?",
            (digest,),
        ).fetchone()
        if existing_blob is not None and (
            int(existing_blob[0]) != len(content) or str(existing_blob[1]) != media_type
        ):
            raise IrCandidateCallerError("canonical observation blob metadata conflicts")
        if existing_blob is None:
            storage_path, was_created = _write_content_addressed(blob_root, digest, content)
            if was_created:
                new_paths.append(storage_path)
            storage_uri = storage_path.resolve().as_uri()
        else:
            storage_uri = str(existing_blob[2])
            _verify_durable_blob(storage_uri, digest, content)
        blob = ContentBlob(
            sha256=digest,
            byte_size=len(content) if existing_blob is None else int(existing_blob[0]),
            media_type=media_type if existing_blob is None else str(existing_blob[1]),
            storage_uri=storage_uri,
            recorded_at=(
                retrieved_at
                if existing_blob is None
                else datetime.fromisoformat(str(existing_blob[3]))
            ),
        )
        records_created += int(ledger.persist(blob).created)
        existing_observation = connection.execute(
            "SELECT source_kind,source_url,blob_sha256,observed_at,retrieved_at,"
            "retrieval_config_sha256,collector_code_version "
            "FROM evidence_source_observations WHERE observation_id=?",
            (observation_id,),
        ).fetchone()
        if existing_observation is not None and tuple(existing_observation[:3]) != (
            "approved_ir_observation",
            source_url,
            digest,
        ):
            raise IrCandidateCallerError("canonical observation identity conflicts")
        persisted_observed_at = (
            observed_at
            if existing_observation is None
            else datetime.fromisoformat(str(existing_observation[3]))
        )
        persisted_retrieved_at = (
            retrieved_at
            if existing_observation is None
            else datetime.fromisoformat(str(existing_observation[4]))
        )
        if existing_observation is not None and tuple(existing_observation[5:]) != (
            policy_sha256,
            _COLLECTOR_VERSION,
        ):
            raise IrCandidateCallerError("canonical observation policy identity conflicts")
        records_created += int(
            ledger.persist(
                SourceObservation(
                    observation_id=observation_id,
                    idempotency_key=observation_id,
                    source_kind="approved_ir_observation",
                    source_url=source_url,
                    blob_sha256=digest,
                    source_published_at=None,
                    filing_at=None,
                    accepted_at=None,
                    observed_at=persisted_observed_at,
                    retrieved_at=persisted_retrieved_at,
                    retrieval_config_sha256=policy_sha256,
                    collector_code_version=_COLLECTOR_VERSION,
                )
            ).created
        )
        location_id = f"ir-observation-loc:{_digest(observation_id, digest)}"
        existing_location = connection.execute(
            "SELECT storage_uri,verified_at,recorded_at FROM "
            "evidence_blob_location_observations WHERE location_observation_id=?",
            (location_id,),
        ).fetchone()
        location_uri = storage_uri if existing_location is None else str(existing_location[0])
        location_verified_at = (
            persisted_retrieved_at
            if existing_location is None
            else datetime.fromisoformat(str(existing_location[1]))
        )
        location_recorded_at = (
            persisted_retrieved_at
            if existing_location is None
            else datetime.fromisoformat(str(existing_location[2]))
        )
        records_created += int(
            links.persist_location(
                BlobLocationObservation(
                    location_observation_id=location_id,
                    idempotency_key=location_id,
                    blob_sha256=digest,
                    storage_uri=location_uri,
                    location_kind="local",
                    availability_state="present",
                    location_sequence=1,
                    verified_at=location_verified_at,
                    verified_byte_size=len(content),
                    verified_sha256=digest,
                    recorded_at=location_recorded_at,
                )
            ).created
        )
    return records_created


def _write_content_addressed(blob_root: Path, digest: str, content: bytes) -> tuple[Path, bool]:
    target = blob_root / digest[:2] / digest
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        if target.read_bytes() != content:
            raise IrCandidateCallerError("content-addressed IR blob conflicts with stored bytes")
        return target, False
    descriptor, temporary_name = tempfile.mkstemp(prefix=".ir-observation-", dir=target.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        if hashlib.sha256(temporary.read_bytes()).hexdigest() != digest:
            raise IrCandidateCallerError("written IR evidence blob failed hash verification")
        temporary.replace(target)
    except Exception:
        if temporary.exists():
            temporary.unlink()
        raise
    return target, True


def _verify_durable_blob(storage_uri: str, digest: str, expected: bytes) -> None:
    parsed = urllib.parse.urlsplit(storage_uri)
    if parsed.scheme != "file" or parsed.netloc not in {"", "localhost"}:
        raise IrCandidateCallerError("canonical IR evidence has no verifiable local storage")
    decoded = urllib.parse.unquote(parsed.path)
    if len(decoded) >= 3 and decoded[0] == "/" and decoded[2] == ":":
        decoded = decoded[1:]
    path = Path(decoded)
    try:
        content = path.read_bytes()
    except OSError:
        raise IrCandidateCallerError("canonical IR evidence blob is missing") from None
    if content != expected or hashlib.sha256(content).hexdigest() != digest:
        raise IrCandidateCallerError("canonical IR evidence blob is corrupt")


def _validate_candidate_link_proof(
    entry: IrCatalogEntry,
    artifact: SealedObservationArtifact,
) -> None:
    try:
        proof_value = json.loads(artifact.content_bytes)
    except (UnicodeError, json.JSONDecodeError):
        raise IrCandidateCallerError("candidate rendered link proof is invalid") from None
    proof = cast("dict[str, object]", proof_value) if isinstance(proof_value, dict) else {}
    state_value = proof.get("visible_state")
    state = cast("dict[str, object]", state_value) if isinstance(state_value, dict) else {}
    links_value = state.get("links")
    links = cast("list[object]", links_value) if isinstance(links_value, list) else []
    expected = {
        "title": entry.title,
        "url": entry.url,
        "evidence_locator": entry.evidence_locator,
    }
    proven = False
    for link_value in links:
        if not isinstance(link_value, dict):
            continue
        link = cast("dict[str, object]", link_value)
        if all(link.get(key) == value for key, value in expected.items()):
            proven = True
            break
    if not proven:
        raise IrCandidateCallerError(
            "candidate URL/title/locator is not proven by exact rendered evidence"
        )


def _remove_new_blobs(paths: list[Path], blob_root: Path) -> None:
    resolved_root = blob_root.resolve()
    for path in reversed(paths):
        with suppress(OSError):
            path.unlink(missing_ok=True)
        parent = path.parent
        while parent.resolve() != resolved_root and resolved_root in parent.resolve().parents:
            try:
                parent.rmdir()
            except OSError:
                break
            parent = parent.parent


def _verify_replay_metadata(
    existing: IrCandidate,
    plan: IrCandidatePlan,
    item: PlannedIrCandidate,
) -> None:
    if existing.recorded_by != plan.request.recorded_by:
        raise IrCandidateCallerError("candidate replay recorded_by changed")
    if existing.reason != plan.request.reason:
        raise IrCandidateCallerError("candidate replay reason changed")
    if existing.evidence != item.evidence:
        raise IrCandidateCallerError("candidate replay evidence changed")


def _public_plan_payload(plan: IrCandidatePlan) -> dict[str, object]:
    payload = plan.model_dump(mode="json", exclude={"catalog", "bundle"})
    request = payload.get("request")
    if isinstance(request, dict):
        request.pop("recorded_at", None)
    return payload


def _artifact_observation_id(artifact: SealedObservationArtifact) -> str:
    return f"ir-artifact:{_digest(artifact.artifact_id, artifact.sha256)}"


def _digest(*parts: str) -> str:
    return hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()


__all__ = [
    "IrCandidateApplyReceipt",
    "IrCandidateCallerError",
    "IrCandidateCallerRequest",
    "IrCandidatePlan",
    "PlannedIrCandidate",
    "apply_ir_candidate_plan",
    "load_ir_observation_artifact",
    "plan_ir_candidates",
]
