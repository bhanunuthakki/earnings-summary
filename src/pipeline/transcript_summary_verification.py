"""Claim audit and zero-call replay for the retained transcript summary.

Qualification belongs to a frozen evaluation receipt. Schema validity, source
substrings, and a successful provider call do not substitute for that receipt.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    TypeAdapter,
    field_validator,
)

from ask import claims
from ask.grounding import EvidenceItem
from db_paths import db_path_context
from llm.anchors import load_bear_anchor, load_ir_anchor, load_thesis_anchor
from llm.cli import (
    execute_prepared_call,
    model_for_subscription_backend,
    prepare_llm_call,
    routing_config_sha256,
)
from llm.resolver import CapabilityProfile, structured_capability_profile
from llm_artifact_store import (
    DocumentProductEnvelope,
    DocumentProductGeneration,
    DocumentProductKey,
    DocumentProductVerificationReceipt,
)
from llm_budget import require_explicit_blocking_budget
from pipeline.transcript_summary import resolve_transcript_summary_source
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite

_CODE_ROOT = Path(__file__).resolve().parents[2]
_QUALIFICATION = _CODE_ROOT / "evals/qualification/transcript_summary_grounding.json"
_CORPUS = _CODE_ROOT / "evals/golden/transcript_summary_grounding.json"
_CONTEXT_CORPUS = _CODE_ROOT / "evals/golden/transcript_summary_grounding_context.json"
_REQUIRED_COVERAGE = frozenset(
    {
        "boolean_context_adversaries",
        "positive_typed_context",
        "full_format_and_batch_boundary",
        "correction_conflict_issuer",
        "owner_context",
        "long_full_source",
    }
)


class TranscriptSummaryVerificationError(ValueError):
    """The summary is draft or blocked, and cannot become current output."""


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def transcript_audit_code_sha256() -> str:
    """Bind the small prompt, parser, corpus, and receipt implementation closure."""
    from llm.codex_backend import codex_wrapper_source_path
    from llm.fleet_policy import policy_source_path

    paths = (
        Path(claims.__file__),
        _CODE_ROOT / "src/llm/structured.py",
        _CODE_ROOT / "src/llm/untrusted.py",
        _CODE_ROOT / "src/pipeline/transcript_summary.py",
        _CODE_ROOT / "src/pipeline/issuer_document_inventory.py",
        _CODE_ROOT / "src/provenance/evidence_native_candidates.py",
        _CODE_ROOT / "src/provenance/fulltext_backfill.py",
        _CODE_ROOT / "src/provenance/fulltext_extractor_identity.py",
        _CODE_ROOT / "src/provenance/immutable_artifact.py",
        _CODE_ROOT / "src/compute/evidence_snapshot.py",
        _CODE_ROOT / "src/evals/ask_citations.py",
        _CODE_ROOT / "src/evals/harness.py",
        _CODE_ROOT / "src/llm/cli.py",
        _CODE_ROOT / "src/llm/resolver.py",
        _CODE_ROOT / "src/llm/model_ladder.py",
        _CODE_ROOT / "src/llm/model_overrides.py",
        _CODE_ROOT / "src/llm/prompt_ab.py",
        _CODE_ROOT / "src/llm/prompt_registry.py",
        _CODE_ROOT / "src/llm/eval_scopes.py",
        _CODE_ROOT / "src/db_paths.py",
        _CODE_ROOT / "src/sqlite_runtime.py",
        _CODE_ROOT / "src/llm/fleet_policy.py",
        _CODE_ROOT / "src/llm/codex_backend.py",
        _CODE_ROOT / "src/llm/prompt_versions.py",
        _CODE_ROOT / "src/llm_budget.py",
        Path(__file__),
    )
    identities = [
        (path.relative_to(_CODE_ROOT).as_posix(), _sha(path.read_bytes())) for path in paths
    ]
    identities.extend(
        (label, _sha(path.read_bytes()))
        for label, path in (
            ("fleet/snippets/llm_policy.py", policy_source_path()),
            ("fleet/snippets/codex_cli.py", codex_wrapper_source_path()),
        )
    )
    return _sha(_json(identities).encode())


def transcript_audit_corpus_sha256() -> str:
    return _sha(_json([_sha(path.read_bytes()) for path in (_CORPUS, _CONTEXT_CORPUS)]).encode())


class _Closed(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class TranscriptAuditQualification(_Closed):
    launch_profile: Literal["manual", "cron"]
    schema_version: Literal["transcript-summary-audit-qualification@1"]
    status: Literal["qualified"]
    purpose: Literal["transcript_summary_claim_audit"]
    model: str
    backend: str
    routing_config_sha256: str
    audit_code_sha256: str
    corpus_sha256: str
    proof_path: str
    proof_sha256: str
    qualified_at: str
    expires_at: str
    scope: Literal["registered_original_transcript_full_context@1"]
    max_source_characters: int = Field(ge=1, le=120_000)
    max_candidate_characters: int = Field(ge=1, le=80_000)
    limitations: list[str]


class TranscriptAuditQualificationSet(_Closed):
    """The two retained launch configurations share one bounded proof owner."""

    schema_version: Literal["transcript-summary-audit-qualification-set@1"]
    profiles: list[TranscriptAuditQualification] = Field(min_length=2, max_length=2)

    @field_validator("profiles")
    @classmethod
    def _distinct_complete_profiles(
        cls, profiles: list[TranscriptAuditQualification]
    ) -> list[TranscriptAuditQualification]:
        routes = {
            (profile.purpose, profile.model, profile.backend, profile.routing_config_sha256)
            for profile in profiles
        }
        scopes = {
            (
                profile.audit_code_sha256,
                profile.corpus_sha256,
                profile.max_source_characters,
                profile.max_candidate_characters,
            )
            for profile in profiles
        }
        if (
            {profile.launch_profile for profile in profiles} != {"manual", "cron"}
            or len(routes) != len(profiles)
            or len(scopes) != 1
        ):
            raise ValueError("audit profiles must have distinct routes and one complete scope")
        return profiles


class TranscriptAuditPhysicalCall(_Closed):
    purpose: Literal["transcript_summary_claim_audit"]
    prompt: str
    response: str
    generation_id: str
    generated_at: str
    prompt_sha256: str
    response_sha256: str
    model: str
    backend: str
    routing_config_sha256: str
    latency_ms: int | None = Field(default=None, ge=0)
    repair: bool | None = None

    @field_validator("generation_id")
    @classmethod
    def _valid_generation_id(cls, value: str) -> str:
        if str(UUID(value)) != value:
            raise ValueError("physical audit generation identity is not a canonical UUID")
        return value

    @field_validator("generated_at")
    @classmethod
    def _aware_generation_time(cls, value: str) -> str:
        if datetime.fromisoformat(value).tzinfo is None:
            raise ValueError("physical audit generation time must include a timezone")
        return value


class _ProofReference(_Closed):
    path: str
    sha256: str


class TranscriptAuditEvaluation(_Closed):
    launch_profile: Literal["manual", "cron"]
    purpose: Literal["transcript_summary_claim_audit"]
    schema_version: Literal["transcript-summary-audit-evaluation@1"]
    status: Literal["passed"]
    model: str
    backend: str
    routing_config_sha256: str
    audit_code_sha256: str
    corpus_sha256: str
    repetitions: Literal[2]
    case_count: int = Field(ge=38)
    false_accepts: Literal[0]
    false_rejects: Literal[0]
    malformed_or_unavailable: Literal[0]
    typed_contexts_checked: Literal[True]
    over_limit_blocked_before_generation: Literal[True]
    coverage: list[str]
    max_source_characters: int
    max_candidate_characters: int
    repeat_proofs: list[_ProofReference] = Field(min_length=2, max_length=2)


class _RepeatCase(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    case_id: str
    passed: Literal[True]
    actual_json: str
    prompt_text: str | None = None
    response_text: str | None = None
    failure_stage: None = None


class _RepeatResult(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    run_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    purpose: Literal["transcript_summary_claim_audit"]
    mode: Literal["live"]
    model: str
    cases: list[_RepeatCase]


class TranscriptAuditRepeat(_Closed):
    launch_profile: Literal["manual", "cron"]
    """One complete default-route run, with raw calls in its case results."""

    schema_version: Literal["transcript-summary-audit-repeat@1"]
    purpose: Literal["transcript_summary_claim_audit"]
    model: str
    backend: Literal["codex"]
    routing_config_sha256: str
    routing_configuration: dict[str, JsonValue]
    audit_code_sha256: str
    corpus_sha256: str
    result: _RepeatResult


class _Verification(_Closed):
    purpose: Literal["transcript_summary_claim_audit"]
    schema_version: Literal["transcript-summary-verification@1"]
    body_sha256: str
    input_sha256: str
    audit_code_sha256: str
    qualification_sha256: str
    corpus_sha256: str
    records: dict[str, JsonValue]
    calls: list[TranscriptAuditPhysicalCall]


class _Input(BaseModel):
    # The strict artifact owner validates the complete input envelope. This
    # adapter reads only the source/context fields it must reconstruct.
    model_config = ConfigDict(extra="ignore", strict=True)
    document: dict[str, JsonValue]
    canonical_issuer_binding: dict[str, JsonValue]
    fiscal_period_selection: dict[str, JsonValue]
    extraction: dict[str, JsonValue]
    consumed_text_sha256: list[str]
    anchors_context: dict[str, str]


class TranscriptSummaryVerifier:
    """Require an actual qualified route and recheck every persisted audit."""

    def __init__(
        self, db_path: Path, state_root: Path, *, proof_state_root: Path | None = None
    ) -> None:
        self.db_path = db_path.resolve(strict=True)
        self.state_root = state_root.resolve(strict=True)
        self.proof_state_root = (proof_state_root or state_root).resolve(strict=True)
        try:
            self.qualification_bytes = _QUALIFICATION.read_bytes()
            bundle = TranscriptAuditQualificationSet.model_validate_json(self.qualification_bytes)
            self.qualifications = tuple(bundle.profiles)
            proof_runs: set[str] = set()
            proof_generations: set[str] = set()
            for qualification in self.qualifications:
                self._check_profile(qualification)
                self._validate_profile_proof(
                    qualification, runs=proof_runs, generations=proof_generations
                )
            selected = prepare_llm_call(
                "Transcript audit route preflight.",
                purpose=claims.DOCUMENT_AUDIT_PURPOSE,
                scope="document_summary",
                db_path=self.db_path,
                capability_profile=structured_capability_profile(),
            )
            self.qualification = self._profile_for_route(
                model_for_subscription_backend(selected.model, selected.backend),
                selected.backend,
                selected.routing_config_sha256,
                selected.purpose,
            )
        except (OSError, ValueError, RuntimeError, ImportError) as exc:
            raise TranscriptSummaryVerificationError(
                "Transcript summary verification has no current qualified route"
            ) from exc

    def _validate_profile_proof(
        self, qualification: TranscriptAuditQualification, *, runs: set[str], generations: set[str]
    ) -> None:
        # The evals package facade also imports transcript consumers.
        from evals.ask_citations import native_transcript_qualification_limits
        from evals.harness import read_transcript_audit_proof

        source_limit, candidate_limit = native_transcript_qualification_limits(
            _CONTEXT_CORPUS,
            source_db_path=self.db_path,
            source_state_root=self.state_root,
            proof_state_root=self.proof_state_root,
        )
        if (
            qualification.max_source_characters > source_limit
            or qualification.max_candidate_characters > candidate_limit
        ):
            raise ValueError("qualification limits exceed measured native positive cases")
        accepted = TranscriptAuditEvaluation.model_validate_json(
            read_transcript_audit_proof(
                self.proof_state_root, qualification.proof_path, qualification.proof_sha256
            )
        )
        for field in (
            "launch_profile",
            "purpose",
            "model",
            "backend",
            "routing_config_sha256",
            "audit_code_sha256",
            "corpus_sha256",
            "max_source_characters",
            "max_candidate_characters",
        ):
            if getattr(accepted, field) != getattr(qualification, field):
                raise ValueError("qualification differs from its accepted evaluation")
        if frozenset(accepted.coverage) != _REQUIRED_COVERAGE:
            raise ValueError("qualification does not cover the retained product")
        if len({(ref.path, ref.sha256) for ref in accepted.repeat_proofs}) != 2:
            raise ValueError("qualification repeats must be distinct")
        for reference in accepted.repeat_proofs:
            repeat = TranscriptAuditRepeat.model_validate_json(
                read_transcript_audit_proof(self.proof_state_root, reference.path, reference.sha256)
            )
            if len(repeat.result.cases) != accepted.case_count:
                raise ValueError("accepted case count differs from its complete repeat")
            self._validate_repeat(repeat, qualification, runs, generations)

    def _validate_repeat(
        self,
        repeat: TranscriptAuditRepeat,
        qualification: TranscriptAuditQualification,
        runs: set[str],
        generations: set[str],
    ) -> None:
        from evals.ask_citations import (
            document_narrative_eval_records,
            grade_document_narrative_case,
        )

        if any(
            getattr(repeat, field) != getattr(qualification, field)
            for field in (
                "launch_profile",
                "purpose",
                "model",
                "backend",
                "routing_config_sha256",
                "audit_code_sha256",
                "corpus_sha256",
            )
        ):
            raise ValueError("repeat differs from its qualified route")
        configuration_sha = hashlib.sha256(
            json.dumps(repeat.routing_configuration, sort_keys=True).encode("utf-8")
        ).hexdigest()
        if configuration_sha != repeat.routing_config_sha256:
            raise ValueError("repeat routing configuration seal changed")
        result = repeat.result
        if (
            result.run_id in runs
            or result.purpose != qualification.purpose
            or result.model != qualification.model
        ):
            raise ValueError("repeat identity is duplicated or uses another purpose/model")
        runs.add(result.run_id)
        records, overlimit = document_narrative_eval_records(
            _CORPUS,
            _CONTEXT_CORPUS,
            source_db_path=self.db_path,
            source_state_root=self.state_root,
            proof_state_root=self.proof_state_root,
        )
        preflight_id = "overlimit_source_preflight_no_call"
        by_id = {case.case_id: case for case in result.cases}
        if len(by_id) != len(result.cases) or set(by_id) != {row[0] for row in records} | {
            preflight_id
        }:
            raise ValueError("repeat does not cover the complete case population exactly once")
        if overlimit is None or overlimit <= 120_000:
            raise ValueError("overlimit case is not configured")
        preflight = by_id[preflight_id]
        if (
            preflight.prompt_text is not None
            or preflight.response_text is not None
            or json.loads(preflight.actual_json)
            != {"preflight_rejected": True, "provider_calls": 0}
        ):
            raise ValueError("overlimit proof must have no provider call")
        try:
            claims.validate_document_audit_corpus(
                [
                    EvidenceItem(
                        n=1,
                        kind="transcript",
                        label="oversize",
                        text="x" * overlimit,
                        doc_id=None,
                        href=None,
                    )
                ]
            )
        except ValueError:
            pass
        else:
            raise ValueError("overlimit case no longer fails before generation")
        for case_id, body, items, expected in records:
            case = by_id[case_id]
            actual = TypeAdapter(dict[str, JsonValue]).validate_json(case.actual_json)
            exchanges = TypeAdapter(list[dict[str, JsonValue]]).validate_python(
                actual.get("exchanges")
            )
            prompts = TypeAdapter(list[str]).validate_json(case.prompt_text or "null")
            responses = TypeAdapter(list[str]).validate_json(case.response_text or "null")
            if not exchanges or len(exchanges) != len(prompts) or len(exchanges) != len(responses):
                raise ValueError("case has incomplete physical calls")
            replay_calls: list[tuple[str, str]] = []
            replay_exchanges: list[dict[str, object]] = []
            for exchange, prompt, response in zip(exchanges, prompts, responses, strict=True):
                call = TranscriptAuditPhysicalCall.model_validate(
                    {**exchange, "response": response}
                )
                if call.prompt != prompt or (
                    call.purpose,
                    call.model,
                    call.backend,
                    call.routing_config_sha256,
                ) != (
                    qualification.purpose,
                    qualification.model,
                    qualification.backend,
                    qualification.routing_config_sha256,
                ):
                    raise ValueError("case physical call differs from its qualified route")
                if call.generation_id in generations:
                    raise ValueError("physical generation identity is duplicated")
                generations.add(call.generation_id)
                if (
                    _sha(prompt.encode()) != call.prompt_sha256
                    or _sha(response.encode()) != call.response_sha256
                ):
                    raise ValueError("case physical call seal changed")
                replay_calls.append((prompt, response))
                replay_exchanges.append(dict(exchange))
            reconstructed = grade_document_narrative_case(
                case_id=case_id,
                body=body,
                items=items,
                expected=expected,
                db_path=self.db_path,
                transport=None,
                exchanges=replay_exchanges,
                replay_calls=replay_calls,
            )
            if (
                not reconstructed.passed
                or json.loads(reconstructed.actual_json or "null") != actual
            ):
                raise ValueError("saved case does not match reconstruction from its actual calls")

    def _check_profile(self, qualification: TranscriptAuditQualification) -> None:
        now = datetime.now(UTC)
        start = datetime.fromisoformat(qualification.qualified_at)
        end = datetime.fromisoformat(qualification.expires_at)
        if start.tzinfo is None or end.tzinfo is None or not start <= now < end:
            raise TranscriptSummaryVerificationError("transcript audit qualification expired")
        try:
            code_sha = transcript_audit_code_sha256()
            corpus_sha = transcript_audit_corpus_sha256()
        except (OSError, RuntimeError, ImportError) as exc:
            raise TranscriptSummaryVerificationError(
                "transcript audit implementation identity is unavailable"
            ) from exc
        if qualification.audit_code_sha256 != code_sha or qualification.corpus_sha256 != corpus_sha:
            raise TranscriptSummaryVerificationError(
                "transcript audit qualification identity changed"
            )

    def _check_qualification(self) -> None:
        self._check_profile(self.qualification)
        if self.qualification.routing_config_sha256 != routing_config_sha256(
            claims.DOCUMENT_AUDIT_PURPOSE
        ):
            raise TranscriptSummaryVerificationError("transcript audit qualification route changed")

    def _profile_for_route(
        self, model: str, backend: str, route_sha: str, purpose: str
    ) -> TranscriptAuditQualification:
        for qualification in self.qualifications:
            if (model, backend, route_sha, purpose) == (
                qualification.model,
                qualification.backend,
                qualification.routing_config_sha256,
                qualification.purpose,
            ):
                self._check_profile(qualification)
                return qualification
        raise TranscriptSummaryVerificationError("transcript audit used an unqualified route")

    def _check_route(self, model: str, backend: str, route_sha: str, purpose: str) -> None:
        self._profile_for_route(model, backend, route_sha, purpose)

    def _check_scope(self, items: list[EvidenceItem], body: str | None = None) -> None:
        claims.validate_document_audit_corpus(items)
        if any(item.kind not in {"transcript", "owner_context"} for item in items):
            raise TranscriptSummaryVerificationError(
                "transcript audit source kind is outside qualification"
            )
        if (
            sum(len(item.text) + len(item.label) for item in items)
            > self.qualification.max_source_characters
        ):
            raise TranscriptSummaryVerificationError(
                "full source exceeds the evaluated audit context"
            )
        if body is not None and len(body) > self.qualification.max_candidate_characters:
            raise TranscriptSummaryVerificationError("summary exceeds the evaluated audit size")

    def _corpus(
        self,
        conn: sqlite3.Connection,
        key: DocumentProductKey,
        input_manifest_json: str,
    ) -> list[EvidenceItem]:
        manifest = _Input.model_validate_json(input_manifest_json)
        source, full_text, source_ref = resolve_transcript_summary_source(
            conn, key.document_version_id, state_root=self.state_root
        )
        ticker = str(source["ticker"])
        with db_path_context(self.db_path):
            anchors = {
                "thesis": load_thesis_anchor(self.state_root, ticker),
                "bear_case": load_bear_anchor(self.state_root, ticker),
                "investor_relations": load_ir_anchor(self.state_root, ticker),
            }
        consumed = full_text
        source_extraction = TypeAdapter(dict[str, JsonValue]).validate_python(source["extractor"])
        proof = {
            "request": source["fiscal_selection_proof"],
            "binding_kind": source["binding_kind"],
            "binding_revision_id": source["binding_revision_id"],
        }
        fiscal = manifest.fiscal_period_selection
        if (
            manifest.document.get("document_version_id") != key.document_version_id
            or manifest.document.get("raw_blob_sha256") != source["blob_sha256"]
            or manifest.consumed_text_sha256 != [_sha(consumed.encode())]
            or manifest.extraction.get("fulltext_sha256") != _sha(full_text.encode())
            or manifest.anchors_context != anchors
            or manifest.canonical_issuer_binding.get("canonical_issuer_id") != key.issuer_id
            or manifest.canonical_issuer_binding.get("recorded_issuer_id")
            != source["recorded_issuer_id"]
            or manifest.canonical_issuer_binding.get("binding_revision_id")
            != source["binding_revision_id"]
            or manifest.canonical_issuer_binding.get("binding_kind") != source["binding_kind"]
            or source["period_start"]
            != (None if key.period_start is None else key.period_start.isoformat())
            or source["period_end"] != key.period_end.isoformat()
            or source["fiscal_year"] != key.fiscal_year
            or source["fiscal_quarter"] != key.fiscal_quarter
            or fiscal.get("selection_proof") != proof
            or fiscal.get("selection_proof_sha256") != _sha(_json(proof).encode())
            or manifest.extraction.get("approved_extraction_run_id")
            != source_extraction.get("approved_extraction_run_id")
            or manifest.extraction.get("extractor_name") != source_extraction.get("name")
            or manifest.extraction.get("extractor_version") != source_extraction.get("code_version")
            or manifest.extraction.get("config_sha256") != source_extraction.get("config_sha256")
        ):
            raise TranscriptSummaryVerificationError("transcript source or anchor context changed")
        items = [
            EvidenceItem(
                n=1,
                kind="transcript",
                label=f"{ticker} fiscal Q{key.fiscal_quarter} {key.fiscal_year}",
                text=consumed,
                doc_id=None,
                href=None,
                source_url=source_ref,
                ticker=ticker,
                doc_type="transcript",
                period=key.period_end.isoformat(),
            )
        ]
        for name, text in anchors.items():
            if text:
                items.append(
                    EvidenceItem(
                        n=len(items) + 1,
                        kind="owner_context",
                        ticker=ticker,
                        label=f"{ticker} owner {name}; assumptions and attributed views",
                        text=text,
                        doc_id=None,
                        href=None,
                    )
                )
        return items

    def preflight(self, key: DocumentProductKey, input_manifest_json: str, /) -> None:
        """Reject unsupported complete source/context before any summary call."""
        self._check_qualification()
        require_explicit_blocking_budget(claims.DOCUMENT_AUDIT_PURPOSE, db_path=self.db_path)
        with connect_sqlite(self.db_path, role=SQLiteConnectionRole.READ_ONLY) as conn:
            items = self._corpus(conn, key, input_manifest_json)
        self._check_scope(items)

    def audit(
        self,
        key: DocumentProductKey,
        generation: DocumentProductGeneration,
        body_bytes: bytes,
    ) -> DocumentProductVerificationReceipt:
        self._check_qualification()
        require_explicit_blocking_budget(claims.DOCUMENT_AUDIT_PURPOSE, db_path=self.db_path)
        with connect_sqlite(self.db_path, role=SQLiteConnectionRole.READ_ONLY) as conn:
            items = self._corpus(conn, key, generation.input_manifest_json)
        self._check_scope(items, body_bytes.decode("utf-8"))
        calls: list[TranscriptAuditPhysicalCall] = []

        def transport(prompt: str, **kwargs: object) -> str:
            requested_profile = kwargs.get("capability_profile")
            if not isinstance(requested_profile, CapabilityProfile):
                raise TranscriptSummaryVerificationError("audit capability profile is missing")
            prepared = prepare_llm_call(
                prompt,
                purpose=claims.DOCUMENT_AUDIT_PURPOSE,
                scope="document_summary",
                capability_profile=requested_profile,
                db_path=self.db_path,
            )
            self._check_route(
                model_for_subscription_backend(prepared.model, prepared.backend),
                prepared.backend,
                prepared.routing_config_sha256,
                prepared.purpose,
            )
            if prepared.prompt != prompt:
                raise TranscriptSummaryVerificationError("audit prompt experiment is not qualified")
            result = execute_prepared_call(replace(prepared, requested_backend=prepared.backend))
            receipt = result.receipt
            self._check_route(
                receipt.model, receipt.backend, receipt.routing_config_sha256, receipt.purpose
            )
            calls.append(
                TranscriptAuditPhysicalCall(
                    purpose=claims.DOCUMENT_AUDIT_PURPOSE,
                    prompt=prepared.prompt,
                    response=result.text,
                    generation_id=receipt.generation_id,
                    generated_at=receipt.generated_at.isoformat(),
                    prompt_sha256=receipt.prompt_sha256,
                    response_sha256=receipt.response_sha256,
                    model=receipt.model,
                    backend=receipt.backend,
                    routing_config_sha256=receipt.routing_config_sha256,
                )
            )
            return result.text

        body = body_bytes.decode("utf-8")
        audit = claims.audit_document_narrative(
            body, items, db_path=self.db_path, transport=transport
        )
        if not audit.records or any(not record.supported for record in audit.records):
            raise TranscriptSummaryVerificationError(
                "transcript summary contains unsupported claims"
            )
        payload = _Verification(
            purpose=claims.DOCUMENT_AUDIT_PURPOSE,
            schema_version="transcript-summary-verification@1",
            body_sha256=_sha(body_bytes),
            input_sha256=generation.input_sha256,
            audit_code_sha256=transcript_audit_code_sha256(),
            qualification_sha256=_sha(self.qualification_bytes),
            corpus_sha256=_sha(_json([asdict(item) for item in items]).encode()),
            records=TypeAdapter(dict[str, JsonValue]).validate_python(audit.raw_payload()),
            calls=calls,
        )
        text = _json(payload.model_dump(mode="json"))
        return DocumentProductVerificationReceipt(text, _sha(text.encode()))

    def __call__(self, conn: sqlite3.Connection, envelope: DocumentProductEnvelope, /) -> None:
        self._check_qualification()
        receipt = envelope.verification
        if _sha(receipt.receipt_json.encode()) != receipt.receipt_sha256:
            raise TranscriptSummaryVerificationError("transcript audit receipt changed")
        payload = _Verification.model_validate_json(receipt.receipt_json)
        items = self._corpus(conn, envelope.key, envelope.generation.input_manifest_json)
        self._check_scope(items, envelope.body_bytes.decode("utf-8"))
        if (
            payload.body_sha256 != _sha(envelope.body_bytes)
            or payload.input_sha256 != envelope.generation.input_sha256
            or payload.audit_code_sha256 != transcript_audit_code_sha256()
            or payload.qualification_sha256 != _sha(self.qualification_bytes)
            or payload.corpus_sha256 != _sha(_json([asdict(item) for item in items]).encode())
            or not payload.calls
        ):
            raise TranscriptSummaryVerificationError(
                "transcript audit input or policy seal changed"
            )
        for call in payload.calls:
            self._check_route(call.model, call.backend, call.routing_config_sha256, call.purpose)
            if (
                _sha(call.prompt.encode()) != call.prompt_sha256
                or _sha(call.response.encode()) != call.response_sha256
            ):
                raise TranscriptSummaryVerificationError(
                    "transcript audit physical call seal changed"
                )
        audit = claims.replay_document_narrative_calls(
            envelope.body_bytes.decode("utf-8"),
            items,
            [(call.prompt, call.response) for call in payload.calls],
        )
        if _json(audit.raw_payload()) != _json(payload.records):
            raise TranscriptSummaryVerificationError(
                "saved claims differ from actual audit responses"
            )
        if not audit.records or any(not record.supported for record in audit.records):
            raise TranscriptSummaryVerificationError("transcript summary is not fully supported")
