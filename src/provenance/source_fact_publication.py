"""Public verification contract for sealed source-fact publications.

The writer, read model, canonical resolver, and future research-snapshot
builder must all use this module.  A stored seal is not trusted by itself:
verification recomputes the header payload, ordered member set, every live
record commitment, deterministic row identities, counts, digests, and clocks.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field

PublicationMemberKind = Literal[
    "fact_cell",
    "fact_observation",
    "observation_relation",
    "derivation_seal",
    "extraction_seal",
    "resolution_revision",
]
PublicationVerificationDisposition = Literal[
    "missing_provenance",
    "quarantined",
]

PUBLICATION_PAYLOAD_VERSION = "source_fact_publication.v1"
RECORD_COMMITMENT_VERSION = "source_fact_record_commitment.v1"
_MEMBER_KINDS: tuple[PublicationMemberKind, ...] = (
    "fact_cell",
    "fact_observation",
    "observation_relation",
    "derivation_seal",
    "extraction_seal",
    "resolution_revision",
)


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SourceFactPublicationMember(_FrozenModel):
    publication_member_id: str = Field(min_length=1, max_length=128)
    idempotency_key: str = Field(min_length=1, max_length=256)
    publication_id: str = Field(min_length=1, max_length=128)
    member_ordinal: int = Field(ge=0)
    record_kind: PublicationMemberKind
    record_id: str = Field(min_length=1, max_length=128)
    record_idempotency_key: str = Field(min_length=1, max_length=256)
    record_commitment_version: str
    record_commitment_sha256: str = Field(min_length=64, max_length=64)
    canonical_member_json: str
    canonical_member_sha256: str = Field(min_length=64, max_length=64)
    recorded_at: datetime


class VerifiedPublicationMember(_FrozenModel):
    member_ordinal: int = Field(ge=0)
    record_kind: PublicationMemberKind
    record_id: str
    record_commitment_sha256: str = Field(min_length=64, max_length=64)
    canonical_member_sha256: str = Field(min_length=64, max_length=64)


class VerifiedSourceFactPublication(_FrozenModel):
    publication_id: str
    publication_seal_id: str
    cutoff: datetime
    created_at: datetime
    recorded_at: datetime
    sealed_at: datetime
    publication_payload_sha256: str = Field(min_length=64, max_length=64)
    member_set_sha256: str = Field(min_length=64, max_length=64)
    members: tuple[VerifiedPublicationMember, ...]

    @property
    def member_count(self) -> int:
        return len(self.members)


class PublicationVerificationError(RuntimeError):
    """A publication is absent, outside the cutoff, or integrity-quarantined."""

    def __init__(
        self,
        reason_code: str,
        *,
        publication_id: str,
        disposition: PublicationVerificationDisposition,
        record_kind: PublicationMemberKind | None = None,
        record_id: str | None = None,
    ) -> None:
        self.reason_code: str = reason_code
        self.publication_id: str = publication_id
        self.disposition: PublicationVerificationDisposition = disposition
        self.record_kind: PublicationMemberKind | None = record_kind
        self.record_id: str | None = record_id
        suffix = "" if record_kind is None or record_id is None else f" ({record_kind}:{record_id})"
        super().__init__(f"{reason_code}: publication {publication_id!r}{suffix} is not admissible")


class PublicationRecordMissingError(ValueError):
    """A publication member references no complete hardened live record."""

    def __init__(
        self,
        record_kind: PublicationMemberKind,
        record_id: str,
    ) -> None:
        self.record_kind = record_kind
        self.record_id = record_id
        super().__init__(f"publication commitment record {record_id} missing")


def canonical_json(value: object) -> str:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def digest_text(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def canonical_time(value: object) -> str:
    return datetime.fromisoformat(str(value)).isoformat()


def publication_payload(
    *,
    publication_id: object,
    idempotency_key: object,
    member_set_sha256: object,
    cell_count: object,
    observation_count: object,
    relation_count: object,
    derivation_seal_count: object,
    extraction_seal_count: object,
    resolution_revision_count: object,
    member_count: object,
    created_at: object,
    recorded_at: object,
) -> str:
    return canonical_json(
        {
            "created_at": canonical_time(created_at),
            "graph_counts": {
                "cell_count": int(str(cell_count)),
                "derivation_seal_count": int(str(derivation_seal_count)),
                "extraction_seal_count": int(str(extraction_seal_count)),
                "member_count": int(str(member_count)),
                "observation_count": int(str(observation_count)),
                "relation_count": int(str(relation_count)),
                "resolution_revision_count": int(str(resolution_revision_count)),
            },
            "idempotency_key": str(idempotency_key),
            "member_set_sha256": str(member_set_sha256),
            "payload_version": PUBLICATION_PAYLOAD_VERSION,
            "publication_id": str(publication_id),
            "recorded_at": canonical_time(recorded_at),
        }
    )


def publication_member_payload(
    *,
    member_ordinal: int,
    record_kind: PublicationMemberKind,
    record_id: str,
    record_idempotency_key: str,
    record_commitment_sha256: str,
) -> str:
    return canonical_json(
        {
            "member_ordinal": member_ordinal,
            "record_commitment_sha256": record_commitment_sha256,
            "record_commitment_version": RECORD_COMMITMENT_VERSION,
            "record_id": record_id,
            "record_idempotency_key": record_idempotency_key,
            "record_kind": record_kind,
        }
    )


def publication_member_id(
    publication_id: str,
    member_ordinal: int,
    record_kind: PublicationMemberKind,
    record_id: str,
) -> str:
    identity = f"{publication_id}|{member_ordinal}|{record_kind}|{record_id}"
    return f"sfpm_{digest_text(identity)}"


def publication_member_idempotency_key(
    publication_idempotency_key: str,
    member_ordinal: int,
    record_kind: PublicationMemberKind,
    record_idempotency_key: str,
) -> str:
    identity = (
        f"{publication_idempotency_key}|{member_ordinal}|{record_kind}|{record_idempotency_key}"
    )
    return f"sfpmk_{digest_text(identity)}"


def publication_seal_id(publication_id: str) -> str:
    return f"sfps_{digest_text(publication_id + '|seal')}"


def publication_seal_idempotency_key(publication_idempotency_key: str) -> str:
    return f"sfpsk_{digest_text(publication_idempotency_key + '|seal')}"


def record_idempotency_key(
    conn: sqlite3.Connection,
    record_kind: PublicationMemberKind,
    record_id: str,
) -> str:
    table, identifier = _record_table(record_kind)
    row = conn.execute(
        f"SELECT idempotency_key FROM {table} WHERE {identifier} = ?",  # nosec B608 -- trusted internal SQL shape; values remain bound
        (record_id,),
    ).fetchone()
    if row is None:
        raise PublicationRecordMissingError(record_kind, record_id)
    return str(row[0])


def record_commitment(
    conn: sqlite3.Connection,
    record_kind: PublicationMemberKind,
    record_id: str,
) -> str:
    bundle = record_bundle(conn, record_kind, record_id)
    return digest_text(
        canonical_json(
            {
                "commitment_version": RECORD_COMMITMENT_VERSION,
                "record": bundle,
                "record_kind": record_kind,
            }
        )
    )


def record_bundle(
    conn: sqlite3.Connection,
    record_kind: PublicationMemberKind,
    record_id: str,
) -> dict[str, object]:
    if record_kind == "fact_cell":
        row = _require_row(
            conn,
            "SELECT cell.fact_cell_id,cell.idempotency_key,"
            "cell.taxonomy_version,cell.fiscal_year,cell.fiscal_period,"
            "cell.effective_at,cell.knowledge_at,cell.recorded_at,"
            "seal.semantic_key_version,seal.semantic_identity_json,"
            "seal.semantic_key_sha256,seal.dimension_set_json,"
            "seal.dimension_set_sha256 "
            "FROM fact_cells_v2 AS cell "
            "JOIN fact_cell_identity_seals_v2 AS seal "
            "ON seal.fact_cell_id = cell.fact_cell_id "
            "WHERE cell.fact_cell_id = ?",
            record_kind,
            record_id,
        )
        return {"cell": row}
    if record_kind == "fact_observation":
        row = _require_row(
            conn,
            "SELECT observation.observation_id,"
            "observation.idempotency_key,payload.payload_version,"
            "payload.canonical_payload_json,"
            "payload.observation_payload_sha256,"
            "anchor.anchor_payload_json,anchor.anchor_payload_sha256 "
            "FROM fact_observations_v2 AS observation "
            "JOIN fact_observation_payload_commitments_v2 AS payload "
            "ON payload.observation_id = observation.observation_id "
            "LEFT JOIN fact_reported_observation_anchors_v2 AS anchor "
            "ON anchor.observation_id = observation.observation_id "
            "WHERE observation.observation_id = ?",
            record_kind,
            record_id,
        )
        return {"observation": row}
    if record_kind == "observation_relation":
        row = _require_row(
            conn,
            "SELECT relation_id,idempotency_key,"
            "subject_observation_id,object_observation_id,"
            "relation_kind,reason_code,reason_details_json,"
            "policy_name,policy_version,policy_config_sha256,"
            "effective_at,knowledge_at,recorded_at "
            "FROM fact_observation_relations_v2 WHERE relation_id = ?",
            record_kind,
            record_id,
        )
        return {"relation": row}
    if record_kind == "derivation_seal":
        row = _require_row(
            conn,
            "SELECT seal.derivation_seal_id,seal.idempotency_key,"
            "seal.output_observation_id,seal.input_count,"
            "seal.canonical_input_digest_sha256,"
            "seal.formula_config_sha256,seal.seal_method,"
            "seal.seal_method_version,seal.effective_at,"
            "seal.knowledge_at,seal.recorded_at,"
            "basis.input_basis,basis.canonical_basis_json,"
            "basis.canonical_basis_sha256 "
            "FROM fact_derivation_seals_v2 AS seal "
            "JOIN fact_derivation_basis_commitments_v2 AS basis "
            "ON basis.derivation_seal_id = seal.derivation_seal_id "
            "WHERE seal.derivation_seal_id = ?",
            record_kind,
            record_id,
        )
        return {"derivation": row}
    if record_kind == "extraction_seal":
        row = _require_row(
            conn,
            "SELECT extraction_seal_id,idempotency_key,"
            "extraction_run_id,expected_node_count,"
            "observed_node_count,reported_fact_count,node_set_json,"
            "node_set_sha256,observation_set_json,"
            "observation_set_sha256,extractor_config_sha256,"
            "extraction_output_sha256,completeness_policy_name,"
            "completeness_policy_version,"
            "completeness_policy_sha256,knowledge_at,recorded_at "
            "FROM fact_extraction_run_completeness_seals_v2 "
            "WHERE extraction_seal_id = ?",
            record_kind,
            record_id,
        )
        return {"extraction": row}
    resolution = _require_row(
        conn,
        "SELECT resolution_revision_id,idempotency_key,"
        "fact_cell_id,revision,status,selected_observation_id,"
        "candidate_set_id,candidate_count,"
        "candidate_set_digest_sha256,policy_name,policy_version,"
        "policy_config_sha256,reason_code,reason_details_json,"
        "effective_at,knowledge_at,recorded_at,"
        "supersedes_resolution_revision_id "
        "FROM fact_resolution_revisions_v2 "
        "WHERE resolution_revision_id = ?",
        record_kind,
        record_id,
    )
    candidates = _rows(
        conn,
        "SELECT candidate_id,idempotency_key,candidate_set_id,"
        "fact_cell_id,observation_id,candidate_ordinal,"
        "eligibility,reason_code,reason_details_json,"
        "candidate_payload_sha256,recorded_at "
        "FROM fact_resolution_candidates_v2 "
        "WHERE candidate_set_id = ? ORDER BY candidate_ordinal",
        str(resolution["candidate_set_id"]),
    )
    return {"candidates": candidates, "resolution": resolution}


def verify_source_fact_publication(
    conn: sqlite3.Connection,
    *,
    publication_id: str,
    cutoff: datetime,
) -> VerifiedSourceFactPublication:
    """Recompute and admit one complete publication at an explicit cutoff."""

    try:
        return _verify_source_fact_publication(
            conn,
            publication_id=publication_id,
            cutoff=cutoff,
        )
    except PublicationVerificationError:
        raise
    except sqlite3.OperationalError as exc:
        raise _verification_error(
            "publication_ledger_unavailable",
            publication_id,
            "missing_provenance",
        ) from exc
    except (KeyError, TypeError, ValueError) as exc:
        raise _verification_error(
            "publication_ledger_malformed",
            publication_id,
            "quarantined",
        ) from exc


def _verify_source_fact_publication(
    conn: sqlite3.Connection,
    *,
    publication_id: str,
    cutoff: datetime,
) -> VerifiedSourceFactPublication:
    bounded_cutoff = _utc(cutoff)
    try:
        header = _fetchone(
            conn,
            "SELECT publication_id,idempotency_key,payload_version,"
            "canonical_publication_payload_json,publication_payload_sha256,"
            "member_set_sha256,cell_count,observation_count,relation_count,"
            "derivation_seal_count,extraction_seal_count,"
            "resolution_revision_count,member_count,created_at,recorded_at "
            "FROM source_fact_publications WHERE publication_id = ?",
            publication_id,
        )
        seal = _fetchone(
            conn,
            "SELECT publication_seal_id,idempotency_key,publication_id,"
            "member_count,canonical_member_set_json,member_set_sha256,"
            "publication_payload_sha256,sealed_at "
            "FROM source_fact_publication_seals WHERE publication_id = ?",
            publication_id,
        )
    except sqlite3.OperationalError as exc:
        raise _verification_error(
            "publication_ledger_unavailable",
            publication_id,
            "missing_provenance",
        ) from exc
    if header is None:
        raise _verification_error(
            "publication_graph_missing",
            publication_id,
            "missing_provenance",
        )
    if seal is None:
        raise _verification_error(
            "publication_graph_unsealed",
            publication_id,
            "missing_provenance",
        )

    try:
        created_at = _datetime(header["created_at"])
        recorded_at = _datetime(header["recorded_at"])
        sealed_at = _datetime(seal["sealed_at"])
    except ValueError as exc:
        raise _verification_error(
            "publication_clock_tampered",
            publication_id,
            "quarantined",
        ) from exc
    if recorded_at < created_at or sealed_at != recorded_at:
        raise _verification_error(
            "publication_seal_clock_tampered",
            publication_id,
            "quarantined",
        )
    if any(clock > bounded_cutoff for clock in (created_at, recorded_at, sealed_at)):
        raise _verification_error(
            "publication_graph_after_cutoff",
            publication_id,
            "missing_provenance",
        )
    if (
        str(seal["publication_id"]) != publication_id
        or str(seal["publication_seal_id"]) != publication_seal_id(publication_id)
        or str(seal["idempotency_key"])
        != publication_seal_idempotency_key(str(header["idempotency_key"]))
    ):
        raise _verification_error(
            "publication_seal_identity_tampered",
            publication_id,
            "quarantined",
        )

    members = _fetchall(
        conn,
        "SELECT publication_member_id,idempotency_key,publication_id,"
        "member_ordinal,record_kind,record_id,record_idempotency_key,"
        "record_commitment_version,record_commitment_sha256,"
        "canonical_member_json,canonical_member_sha256,recorded_at "
        "FROM source_fact_publication_members "
        "WHERE publication_id = ? ORDER BY member_ordinal",
        publication_id,
    )
    expected_count = _integer(header["member_count"])
    if (
        len(members) != expected_count
        or _integer(seal["member_count"]) != expected_count
        or tuple(_integer(member["member_ordinal"]) for member in members)
        != tuple(range(expected_count))
    ):
        raise _verification_error(
            "publication_graph_incomplete",
            publication_id,
            "missing_provenance",
        )

    counts = {
        kind: sum(str(member["record_kind"]) == kind for member in members)
        for kind in _MEMBER_KINDS
    }
    for kind, column in (
        ("fact_cell", "cell_count"),
        ("fact_observation", "observation_count"),
        ("observation_relation", "relation_count"),
        ("derivation_seal", "derivation_seal_count"),
        ("extraction_seal", "extraction_seal_count"),
        ("resolution_revision", "resolution_revision_count"),
    ):
        if counts[kind] != _integer(header[column]):
            raise _verification_error(
                "publication_graph_incomplete",
                publication_id,
                "missing_provenance",
            )

    verified_members: list[VerifiedPublicationMember] = []
    canonical_members: list[dict[str, object]] = []
    for member in members:
        verified, canonical = _verify_member(
            conn,
            publication_id=publication_id,
            publication_idempotency_key=str(header["idempotency_key"]),
            publication_recorded_at=recorded_at,
            cutoff=bounded_cutoff,
            member=member,
        )
        verified_members.append(verified)
        canonical_members.append(canonical)

    member_set_json = canonical_json(canonical_members)
    member_set_sha256 = digest_text(member_set_json)
    if (
        str(header["member_set_sha256"]) != member_set_sha256
        or str(seal["canonical_member_set_json"]) != member_set_json
        or str(seal["member_set_sha256"]) != member_set_sha256
    ):
        raise _verification_error(
            "publication_member_set_tampered",
            publication_id,
            "quarantined",
        )

    payload_json = publication_payload(
        publication_id=publication_id,
        idempotency_key=header["idempotency_key"],
        member_set_sha256=member_set_sha256,
        cell_count=header["cell_count"],
        observation_count=header["observation_count"],
        relation_count=header["relation_count"],
        derivation_seal_count=header["derivation_seal_count"],
        extraction_seal_count=header["extraction_seal_count"],
        resolution_revision_count=header["resolution_revision_count"],
        member_count=expected_count,
        created_at=header["created_at"],
        recorded_at=header["recorded_at"],
    )
    payload_sha256 = digest_text(payload_json)
    if (
        str(header["payload_version"]) != PUBLICATION_PAYLOAD_VERSION
        or str(header["canonical_publication_payload_json"]) != payload_json
        or str(header["publication_payload_sha256"]) != payload_sha256
        or str(seal["publication_payload_sha256"]) != payload_sha256
    ):
        raise _verification_error(
            "publication_payload_tampered",
            publication_id,
            "quarantined",
        )

    return VerifiedSourceFactPublication(
        publication_id=publication_id,
        publication_seal_id=str(seal["publication_seal_id"]),
        cutoff=bounded_cutoff,
        created_at=created_at,
        recorded_at=recorded_at,
        sealed_at=sealed_at,
        publication_payload_sha256=payload_sha256,
        member_set_sha256=member_set_sha256,
        members=tuple(verified_members),
    )


def _verify_member(
    conn: sqlite3.Connection,
    *,
    publication_id: str,
    publication_idempotency_key: str,
    publication_recorded_at: datetime,
    cutoff: datetime,
    member: dict[str, object],
) -> tuple[VerifiedPublicationMember, dict[str, object]]:
    kind_value = str(member["record_kind"])
    if kind_value not in _MEMBER_KINDS:
        raise _verification_error(
            "publication_member_kind_tampered",
            publication_id,
            "quarantined",
        )
    kind = kind_value
    record_id = str(member["record_id"])
    ordinal = _integer(member["member_ordinal"])
    try:
        member_recorded_at = _datetime(member["recorded_at"])
    except ValueError as exc:
        raise _verification_error(
            "publication_member_clock_tampered",
            publication_id,
            "quarantined",
            kind,
            record_id,
        ) from exc
    if member_recorded_at != publication_recorded_at:
        raise _verification_error(
            "publication_member_clock_tampered",
            publication_id,
            "quarantined",
            kind,
            record_id,
        )
    if member_recorded_at > cutoff:
        raise _verification_error(
            "publication_graph_after_cutoff",
            publication_id,
            "missing_provenance",
            kind,
            record_id,
        )

    canonical = publication_member_payload(
        member_ordinal=ordinal,
        record_kind=kind,
        record_id=record_id,
        record_idempotency_key=str(member["record_idempotency_key"]),
        record_commitment_sha256=str(member["record_commitment_sha256"]),
    )
    if (
        str(member["publication_id"]) != publication_id
        or str(member["publication_member_id"])
        != publication_member_id(publication_id, ordinal, kind, record_id)
        or str(member["idempotency_key"])
        != publication_member_idempotency_key(
            publication_idempotency_key,
            ordinal,
            kind,
            str(member["record_idempotency_key"]),
        )
        or str(member["record_commitment_version"]) != RECORD_COMMITMENT_VERSION
        or str(member["canonical_member_json"]) != canonical
        or str(member["canonical_member_sha256"]) != digest_text(canonical)
    ):
        raise _verification_error(
            "publication_member_tampered",
            publication_id,
            "quarantined",
            kind,
            record_id,
        )

    try:
        live_idempotency_key = record_idempotency_key(conn, kind, record_id)
        bundle = record_bundle(conn, kind, record_id)
    except PublicationRecordMissingError as exc:
        raise _verification_error(
            "publication_member_record_missing",
            publication_id,
            "quarantined",
            kind,
            record_id,
        ) from exc
    _require_bundle_before_cutoff(
        bundle,
        cutoff=cutoff,
        publication_id=publication_id,
        record_kind=kind,
        record_id=record_id,
    )
    live_commitment = digest_text(
        canonical_json(
            {
                "commitment_version": RECORD_COMMITMENT_VERSION,
                "record": bundle,
                "record_kind": kind,
            }
        )
    )
    if (
        str(member["record_idempotency_key"]) != live_idempotency_key
        or str(member["record_commitment_sha256"]) != live_commitment
    ):
        raise _verification_error(
            "publication_record_commitment_mismatch",
            publication_id,
            "quarantined",
            kind,
            record_id,
        )
    try:
        canonical_object = json.loads(canonical)
    except json.JSONDecodeError as exc:  # pragma: no cover - canonical_json is total
        raise _verification_error(
            "publication_member_tampered",
            publication_id,
            "quarantined",
            kind,
            record_id,
        ) from exc
    if not isinstance(canonical_object, dict):  # pragma: no cover - fixed schema
        raise _verification_error(
            "publication_member_tampered",
            publication_id,
            "quarantined",
            kind,
            record_id,
        )
    return (
        VerifiedPublicationMember(
            member_ordinal=ordinal,
            record_kind=kind,
            record_id=record_id,
            record_commitment_sha256=live_commitment,
            canonical_member_sha256=digest_text(canonical),
        ),
        cast(dict[str, object], canonical_object),
    )


def _require_bundle_before_cutoff(
    value: object,
    *,
    cutoff: datetime,
    publication_id: str,
    record_kind: PublicationMemberKind,
    record_id: str,
) -> None:
    if isinstance(value, dict):
        mapping = cast(dict[str, object], value)
        for key, item in mapping.items():
            if key in {"knowledge_at", "recorded_at"} and item is not None:
                try:
                    clock = _datetime(item)
                except ValueError as exc:
                    raise _verification_error(
                        "publication_record_clock_tampered",
                        publication_id,
                        "quarantined",
                        record_kind,
                        record_id,
                    ) from exc
                if clock > cutoff:
                    raise _verification_error(
                        "publication_record_after_cutoff",
                        publication_id,
                        "missing_provenance",
                        record_kind,
                        record_id,
                    )
            elif key.endswith("_json") and isinstance(item, str):
                try:
                    nested: object = json.loads(item)
                except json.JSONDecodeError:
                    continue
                _require_bundle_before_cutoff(
                    nested,
                    cutoff=cutoff,
                    publication_id=publication_id,
                    record_kind=record_kind,
                    record_id=record_id,
                )
            else:
                _require_bundle_before_cutoff(
                    item,
                    cutoff=cutoff,
                    publication_id=publication_id,
                    record_kind=record_kind,
                    record_id=record_id,
                )
    elif isinstance(value, list):
        items = cast(list[object], value)
        for item in items:
            _require_bundle_before_cutoff(
                item,
                cutoff=cutoff,
                publication_id=publication_id,
                record_kind=record_kind,
                record_id=record_id,
            )


def _record_table(
    record_kind: PublicationMemberKind,
) -> tuple[str, str]:
    return {
        "fact_cell": ("fact_cells_v2", "fact_cell_id"),
        "fact_observation": ("fact_observations_v2", "observation_id"),
        "observation_relation": (
            "fact_observation_relations_v2",
            "relation_id",
        ),
        "derivation_seal": (
            "fact_derivation_seals_v2",
            "derivation_seal_id",
        ),
        "extraction_seal": (
            "fact_extraction_run_completeness_seals_v2",
            "extraction_seal_id",
        ),
        "resolution_revision": (
            "fact_resolution_revisions_v2",
            "resolution_revision_id",
        ),
    }[record_kind]


def _require_row(
    conn: sqlite3.Connection,
    sql: str,
    record_kind: PublicationMemberKind,
    record_id: str,
) -> dict[str, object]:
    row = _fetchone(conn, sql, record_id)
    if row is None:
        raise PublicationRecordMissingError(record_kind, record_id)
    return row


def _fetchone(
    conn: sqlite3.Connection,
    sql: str,
    identifier: str,
) -> dict[str, object] | None:
    cursor = conn.execute(sql, (identifier,))
    row = cursor.fetchone()
    if row is None:
        return None
    columns = tuple(item[0] for item in cursor.description)
    return dict(zip(columns, tuple(row), strict=True))


def _fetchall(
    conn: sqlite3.Connection,
    sql: str,
    identifier: str,
) -> list[dict[str, object]]:
    cursor = conn.execute(sql, (identifier,))
    columns = tuple(item[0] for item in cursor.description)
    return [dict(zip(columns, tuple(row), strict=True)) for row in cursor.fetchall()]


def _rows(
    conn: sqlite3.Connection,
    sql: str,
    identifier: str,
) -> list[dict[str, object]]:
    return _fetchall(conn, sql, identifier)


def _integer(value: object) -> int:
    if isinstance(value, bool):
        raise ValueError("boolean is not an integer commitment")
    return int(str(value))


def _datetime(value: object) -> datetime:
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid publication clock: {value!r}") from exc
    return _utc(parsed)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _verification_error(
    reason_code: str,
    publication_id: str,
    disposition: PublicationVerificationDisposition,
    record_kind: PublicationMemberKind | None = None,
    record_id: str | None = None,
) -> PublicationVerificationError:
    return PublicationVerificationError(
        reason_code,
        publication_id=publication_id,
        disposition=disposition,
        record_kind=record_kind,
        record_id=record_id,
    )
