"""Owner-facing semantic episodes over immutable raw thesis evaluations.

The raw ``thesis_evaluations`` relation remains execution history.  This module
stores one owner-facing episode for one material semantic input and severity,
plus one idempotent check receipt for every forward evaluator run.  It performs
no commit: callers own the surrounding transaction and writer lock.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_POLICY_VERSION = "forward_v1"
_EPISODE_PREFIX = "thesis-evaluation-episode:"
_CHECK_PREFIX = "thesis-evaluation-check:"


class EpisodeStoreError(ValueError):
    """Base error for semantic episode validation and persistence."""


class EpisodeIdempotencyConflictError(EpisodeStoreError):
    """A ticker/run identity was reused for different canonical inputs."""


class EpisodeNondeterminismError(EpisodeStoreError):
    """Identical semantic inputs produced incompatible deterministic output."""


class EpisodeSeverity(StrEnum):
    OK = "ok"
    WARN = "warn"
    BREACH = "breach"
    UNRESOLVED = "unresolved"


class ProvenanceCompleteness(StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"


class _FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _validate_sha256(value: str) -> str:
    if _SHA256.fullmatch(value) is None:
        raise ValueError("must be a lowercase SHA-256")
    return value


def _validate_aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("must be timezone-aware")
    return value


def _canonical_json(value: JsonValue) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


class SemanticRuleInput(_FrozenModel):
    """One normalized hard or soft rule definition, excluding its outcome."""

    rule_id: str = Field(min_length=1, max_length=160)
    definition: dict[str, JsonValue]


class AcceptedObservationInput(_FrozenModel):
    """One accepted observation that actually participated in the verdict."""

    metric_identity: str = Field(min_length=1, max_length=256)
    period_end: str = Field(min_length=4, max_length=40)
    observed_value: str = Field(min_length=1, max_length=128)
    accepted_value: str = Field(min_length=1, max_length=128)
    unit: str = Field(min_length=1, max_length=80)
    currency: str | None = Field(default=None, min_length=1, max_length=16)
    material_source_semantics: tuple[str, ...] = Field(min_length=1, max_length=32)
    restatement_semantics: str = Field(min_length=1, max_length=128)


class ForwardSemanticInput(_FrozenModel):
    """Complete deterministic input to the forward semantic fingerprint.

    Provenance row IDs, execution times, run IDs, UI state, and prior evaluation
    rows have no field here by design, so they cannot accidentally perturb the
    owner-facing episode identity.
    """

    ticker: str = Field(min_length=1, max_length=32)
    thesis_content_sha256: str
    ruleset_version: str = Field(min_length=1, max_length=128)
    evaluator_semantic_version: str = Field(min_length=1, max_length=128)
    hard_rules: tuple[SemanticRuleInput, ...]
    soft_rules: tuple[SemanticRuleInput, ...]
    accepted_observations: tuple[AcceptedObservationInput, ...]

    _thesis_hash = field_validator("thesis_content_sha256")(_validate_sha256)

    @field_validator("ticker")
    @classmethod
    def _uppercase_ticker(cls, value: str) -> str:
        ticker = value.strip().upper()
        if not ticker:
            raise ValueError("ticker cannot be blank")
        return ticker

    def _rules_payload(self) -> dict[str, JsonValue]:
        def normalized(rules: tuple[SemanticRuleInput, ...]) -> list[JsonValue]:
            rows = [rule.model_dump(mode="json") for rule in rules]
            return sorted(rows, key=_canonical_json)

        return {
            "ruleset_version": self.ruleset_version,
            "hard_rules": normalized(self.hard_rules),
            "soft_rules": normalized(self.soft_rules),
        }

    def canonical_payload(self) -> dict[str, JsonValue]:
        observations: list[JsonValue] = [
            observation.model_dump(mode="json") for observation in self.accepted_observations
        ]
        observations.sort(key=_canonical_json)
        return {
            "fingerprint_policy_version": _POLICY_VERSION,
            "ticker": self.ticker,
            "thesis_content_sha256": self.thesis_content_sha256,
            "rules": self._rules_payload(),
            "accepted_observations": observations,
            "evaluator_semantic_version": self.evaluator_semantic_version,
        }

    @property
    def ruleset_sha256(self) -> str:
        return _sha256(_canonical_json(self._rules_payload()))

    @property
    def semantic_input_sha256(self) -> str:
        return _sha256(_canonical_json(self.canonical_payload()))


class EpisodeCheckInput(_FrozenModel):
    """One forward evaluator execution and its episode-level projection."""

    run_id: str = Field(min_length=1, max_length=256)
    checked_at: datetime
    evidence_as_of: datetime | None
    severity: EpisodeSeverity
    provenance_completeness: ProvenanceCompleteness
    rule_evaluations: tuple[dict[str, JsonValue], ...]
    soft_rule_results: tuple[dict[str, JsonValue], ...] | None = None
    raw_evaluation_id: int | None = Field(default=None, ge=1)

    _checked_at = field_validator("checked_at")(_validate_aware)

    @field_validator("evidence_as_of")
    @classmethod
    def _aware_evidence_as_of(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _validate_aware(value)


class EpisodeWriteResult(_FrozenModel):
    episode_id: str
    check_id: str
    created: bool
    deduplicated: bool
    replayed: bool = False


@dataclass(frozen=True)
class EpisodeHistorySource:
    """Trusted relation and clock columns for old and episode schemas."""

    relation: str
    first_seen_column: str
    latest_checked_column: str


def _episode_id(*, ticker: str, semantic_sha256: str, severity: EpisodeSeverity) -> str:
    key = "\n".join((_POLICY_VERSION, ticker, semantic_sha256, severity.value))
    return f"{_EPISODE_PREFIX}{_sha256(key)}"


def forward_episode_id(*, semantic: ForwardSemanticInput, severity: EpisodeSeverity) -> str:
    """Return the deterministic identity used by ``record_forward_episode``.

    Callers use this read-only helper to decide whether the first raw execution
    row must be inserted as the episode anchor.  The store still rechecks the
    semantic uniqueness and result determinism inside the write transaction.
    """

    return _episode_id(
        ticker=semantic.ticker,
        semantic_sha256=semantic.semantic_input_sha256,
        severity=severity,
    )


def _check_id(*, ticker: str, run_id: str) -> str:
    key = "\n".join((ticker, run_id))
    return f"{_CHECK_PREFIX}{_sha256(key)}"


def _projection_json(rows: tuple[dict[str, JsonValue], ...] | None) -> str | None:
    if rows is None:
        return None
    normalized: list[JsonValue] = sorted(
        (dict(row) for row in rows),
        key=_canonical_json,
    )
    return _canonical_json(normalized)


def _receipt_sha256(
    *,
    episode_id: str,
    semantic: ForwardSemanticInput,
    check: EpisodeCheckInput,
    rule_json: str,
    soft_json: str | None,
    result_sha256: str,
) -> str:
    payload: dict[str, JsonValue] = {
        "episode_id": episode_id,
        "ticker": semantic.ticker,
        "run_id": check.run_id,
        "checked_at": check.checked_at.isoformat(),
        "evidence_as_of": (
            None if check.evidence_as_of is None else check.evidence_as_of.isoformat()
        ),
        "semantic_input_sha256": semantic.semantic_input_sha256,
        "ruleset_sha256": semantic.ruleset_sha256,
        "result_sha256": result_sha256,
        "severity": check.severity.value,
        "provenance_completeness": check.provenance_completeness.value,
        "rule_evaluations_json": rule_json,
        "soft_rule_results_json": soft_json,
    }
    return _sha256(_canonical_json(payload))


def _existing_episode_result(
    connection: sqlite3.Connection,
    *,
    episode_id: str,
    check_id: str,
    replayed: bool,
) -> EpisodeWriteResult:
    row = connection.execute(
        "SELECT outcome FROM thesis_evaluation_episode_check_receipts WHERE receipt_id=?",
        (check_id,),
    ).fetchone()
    if row is None:
        raise EpisodeStoreError("episode check receipt disappeared during write")
    return EpisodeWriteResult(
        episode_id=episode_id,
        check_id=check_id,
        created=False,
        deduplicated=str(row[0]) == "deduplicated_no_change",
        replayed=replayed,
    )


def record_forward_episode(
    connection: sqlite3.Connection,
    *,
    semantic: ForwardSemanticInput,
    check: EpisodeCheckInput,
) -> EpisodeWriteResult:
    """Record or recheck one semantic episode without appending a raw row.

    The same ``ticker``/``run_id`` and exact receipt is an idempotent replay.
    Reusing that identity for different inputs raises a conflict.  Identical
    semantic inputs producing another severity or projection fail loudly as
    evaluator nondeterminism.  The caller owns commit/rollback.
    """

    episode_id = forward_episode_id(semantic=semantic, severity=check.severity)
    check_id = _check_id(ticker=semantic.ticker, run_id=check.run_id)
    rule_json = _projection_json(check.rule_evaluations)
    if rule_json is None:
        raise EpisodeStoreError("rule_evaluations cannot be null")
    soft_json = _projection_json(check.soft_rule_results)
    result_payload: dict[str, JsonValue] = {
        "severity": check.severity.value,
        "provenance_completeness": check.provenance_completeness.value,
        "evidence_as_of": (
            None if check.evidence_as_of is None else check.evidence_as_of.isoformat()
        ),
        "rule_evaluations_json": rule_json,
        "soft_rule_results_json": soft_json,
    }
    result_sha256 = _sha256(_canonical_json(result_payload))
    receipt_sha256 = _receipt_sha256(
        episode_id=episode_id,
        semantic=semantic,
        check=check,
        rule_json=rule_json,
        soft_json=soft_json,
        result_sha256=result_sha256,
    )

    existing_check = connection.execute(
        "SELECT receipt_id,episode_id,receipt_sha256 FROM "
        "thesis_evaluation_episode_check_receipts "
        "WHERE ticker=? AND run_id=?",
        (semantic.ticker, check.run_id),
    ).fetchone()
    if existing_check is not None:
        if (
            str(existing_check[0]) != check_id
            or str(existing_check[1]) != episode_id
            or str(existing_check[2]) != receipt_sha256
        ):
            raise EpisodeIdempotencyConflictError(
                "ticker/run_id was reused for a different thesis evaluation receipt"
            )
        return _existing_episode_result(
            connection,
            episode_id=episode_id,
            check_id=check_id,
            replayed=True,
        )

    semantic_collision = connection.execute(
        "SELECT episode_id,overall_status FROM thesis_evaluation_episodes "
        "WHERE ticker=? AND fingerprint_policy_version=? AND semantic_input_sha256=?",
        (semantic.ticker, _POLICY_VERSION, semantic.semantic_input_sha256),
    ).fetchone()
    if semantic_collision is not None and str(semantic_collision[1]) != check.severity.value:
        raise EpisodeNondeterminismError("identical semantic input produced a different severity")

    existing = connection.execute(
        "SELECT episode_id,evidence_as_of,rule_evaluations_json,soft_rule_results_json,"
        "result_sha256,"
        "provenance_completeness FROM thesis_evaluation_episodes WHERE episode_id=?",
        (episode_id,),
    ).fetchone()
    checked_at = check.checked_at.isoformat()
    evidence_as_of = None if check.evidence_as_of is None else check.evidence_as_of.isoformat()
    created = existing is None
    if existing is None:
        if check.raw_evaluation_id is None:
            raise EpisodeStoreError("a new forward episode requires one raw evaluation anchor")
        try:
            connection.execute(
                "INSERT INTO thesis_evaluation_episodes "
                "(episode_id,ticker,fingerprint_policy_version,semantic_input_json,"
                "semantic_input_sha256,thesis_content_sha256,ruleset_sha256,"
                "evaluator_semantic_version,result_sha256,overall_status,"
                "provenance_completeness,evidence_as_of,first_evaluated_at,last_seen_at,"
                "last_checked_at,duplicate_run_count,rule_evaluations_json,"
                "soft_rule_results_json,first_run_id,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,?,?,?,?)",
                (
                    episode_id,
                    semantic.ticker,
                    _POLICY_VERSION,
                    _canonical_json(semantic.canonical_payload()),
                    semantic.semantic_input_sha256,
                    semantic.thesis_content_sha256,
                    semantic.ruleset_sha256,
                    semantic.evaluator_semantic_version,
                    result_sha256,
                    check.severity.value,
                    check.provenance_completeness.value,
                    evidence_as_of,
                    checked_at,
                    checked_at,
                    checked_at,
                    rule_json,
                    soft_json,
                    check.run_id,
                    checked_at,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise EpisodeStoreError("episode insert conflicted with durable state") from exc
    else:
        if (
            existing[1] != evidence_as_of
            or str(existing[2]) != rule_json
            or existing[3] != soft_json
            or str(existing[4]) != result_sha256
            or str(existing[5]) != check.provenance_completeness.value
        ):
            raise EpisodeNondeterminismError(
                "identical semantic input and severity produced different evidence projection"
            )
        connection.execute(
            "UPDATE thesis_evaluation_episodes SET "
            "last_seen_at=MAX(last_seen_at,?),last_checked_at=MAX(last_checked_at,?),"
            "duplicate_run_count=duplicate_run_count+1 "
            "WHERE episode_id=?",
            (checked_at, checked_at, episode_id),
        )

    if check.raw_evaluation_id is not None:
        raw = connection.execute(
            "SELECT ticker,overall_status,run_id FROM thesis_evaluations WHERE id=?",
            (check.raw_evaluation_id,),
        ).fetchone()
        if raw is None:
            raise EpisodeStoreError("raw_evaluation_id does not exist")
        if str(raw[0]).strip().upper() != semantic.ticker or str(raw[1]) != check.severity.value:
            raise EpisodeStoreError("raw_evaluation_id does not match episode ticker and severity")
        if raw[2] is not None and str(raw[2]) != check.run_id:
            raise EpisodeStoreError("raw_evaluation_id does not match episode run_id")
        existing_member = connection.execute(
            "SELECT episode_id FROM thesis_evaluation_episode_members WHERE evaluation_id=?",
            (check.raw_evaluation_id,),
        ).fetchone()
        if existing_member is not None and str(existing_member[0]) != episode_id:
            raise EpisodeIdempotencyConflictError(
                "raw_evaluation_id is already mapped to another episode"
            )
        if existing_member is None:
            member_ordinal = int(
                connection.execute(
                    "SELECT COUNT(*)+1 FROM thesis_evaluation_episode_members WHERE episode_id=?",
                    (episode_id,),
                ).fetchone()[0]
            )
            connection.execute(
                "INSERT INTO thesis_evaluation_episode_members "
                "(episode_id,evaluation_id,membership_role,member_ordinal,recorded_at) "
                "VALUES (?,?,?,?,?)",
                (
                    episode_id,
                    check.raw_evaluation_id,
                    "anchor" if member_ordinal == 1 else "duplicate",
                    member_ordinal,
                    checked_at,
                ),
            )

    try:
        connection.execute(
            "INSERT INTO thesis_evaluation_episode_check_receipts "
            "(receipt_id,idempotency_key_sha256,episode_id,ticker,run_id,checked_at,"
            "outcome,semantic_input_sha256,result_sha256,receipt_sha256) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                check_id,
                _sha256(f"{semantic.ticker}\n{check.run_id}"),
                episode_id,
                semantic.ticker,
                check.run_id,
                checked_at,
                "created" if created else "deduplicated_no_change",
                semantic.semantic_input_sha256,
                result_sha256,
                receipt_sha256,
            ),
        )
    except sqlite3.IntegrityError as exc:
        raise EpisodeIdempotencyConflictError("run_id receipt insert conflicted") from exc
    return EpisodeWriteResult(
        episode_id=episode_id,
        check_id=check_id,
        created=created,
        deduplicated=not created,
    )


def episode_history_relation(connection: sqlite3.Connection) -> str:
    """Return the episode history view, falling back only on pre-0014 schemas."""

    row = connection.execute(
        "SELECT type FROM sqlite_master WHERE name='v_thesis_evaluation_history'"
    ).fetchone()
    if row is not None and str(row[0]) == "view":
        return "v_thesis_evaluation_history"
    raw = connection.execute(
        "SELECT type FROM sqlite_master WHERE name='thesis_evaluations'"
    ).fetchone()
    if raw is not None and str(raw[0]) == "table":
        return "thesis_evaluations"
    raise EpisodeStoreError("no thesis evaluation history relation is available")


def episode_history_source(connection: sqlite3.Connection) -> EpisodeHistorySource:
    """Return the closed read model used by every owner-facing consumer."""

    relation = episode_history_relation(connection)
    return EpisodeHistorySource(
        relation=relation,
        first_seen_column="evaluated_at",
        latest_checked_column=(
            "last_checked_at" if relation == "v_thesis_evaluation_history" else "evaluated_at"
        ),
    )


__all__ = [
    "AcceptedObservationInput",
    "EpisodeCheckInput",
    "EpisodeHistorySource",
    "EpisodeIdempotencyConflictError",
    "EpisodeNondeterminismError",
    "EpisodeSeverity",
    "EpisodeStoreError",
    "EpisodeWriteResult",
    "ForwardSemanticInput",
    "ProvenanceCompleteness",
    "SemanticRuleInput",
    "episode_history_relation",
    "episode_history_source",
    "forward_episode_id",
    "record_forward_episode",
]
