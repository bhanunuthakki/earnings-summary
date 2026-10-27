"""The accepted transcript-audit receipt must reconstruct from its raw calls."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from uuid import uuid4

import pytest

from ask import claims
from evals import harness as evaluation_evidence
from evals.ask_citations import document_narrative_eval_records, grade_document_narrative_case
from llm import structured
from pipeline import transcript_summary_verification as verification
from pipeline.transcript_summary_verification import (
    TranscriptAuditQualification,
    TranscriptAuditQualificationSet,
    TranscriptAuditRepeat,
    TranscriptSummaryVerificationError,
    TranscriptSummaryVerifier,
)
from provenance.immutable_artifact import ImmutableArtifactConflictError

_PURPOSE = "transcript_summary_claim_audit"
_MODEL = "gpt-6.1-sol"
_CLAIM = "Revenue was USD 10 million for Q1 2026."
_REQUIRED_COVERAGE = [
    "boolean_context_adversaries",
    "positive_typed_context",
    "full_format_and_batch_boundary",
    "correction_conflict_issuer",
    "owner_context",
    "long_full_source",
]


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _route_configuration(launch_profile: str) -> dict[str, object]:
    return {
        "purpose": _PURPOSE,
        "launch_profile": launch_profile,
        "subscription_route": ["codex"],
        "fixture": "synthetic-proof-reconstruction",
    }


def _route_sha(launch_profile: str) -> str:
    return _sha(json.dumps(_route_configuration(launch_profile), sort_keys=True).encode())


def _response(span_id: str, quote: str, *, period: str = "Q1 2026") -> str:
    return _json(
        {
            "spans": [
                {
                    "span_id": span_id,
                    "quote": quote,
                    "kind": "reported_fact",
                    "supported": True,
                    "context": {
                        "issuer": "SYNTH01",
                        "metric": "revenue",
                        "period": period,
                        "unit": "USD millions",
                        "scope": "consolidated",
                        "basis": "unknown",
                        "actual_vs_forecast": "actual",
                        "comparison": "unknown",
                        "attribution": "company reported",
                    },
                    "support": [{"evidence_n": 1, "excerpt": _CLAIM}],
                    "reason": "The exact source excerpt supports the claim.",
                }
            ]
        }
    )


def _write_corpus(code_root: Path) -> tuple[Path, Path]:
    golden = code_root / "golden.json"
    context = code_root / "context.json"
    golden.write_text(
        _json(
            {
                "cases": [
                    {
                        "case_id": f"base_{index:02d}",
                        "source": _CLAIM,
                        "candidate": _CLAIM,
                        "issuer": "SYNTH01",
                        "expected_supported": True,
                    }
                    for index in range(36)
                ]
            }
        ),
        encoding="utf-8",
    )
    long_source = _CLAIM + " " + ("x" * (119_000 - len(_CLAIM) - 1))
    context.write_text(
        _json(
            {
                "cases": [
                    {
                        "case_id": "long_full_source_and_preflight",
                        "items": [
                            {
                                "n": 1,
                                "kind": "transcript",
                                "label": "SYNTH01 full transcript",
                                "text": long_source,
                            }
                        ],
                        "lines": [{"quote": _CLAIM, "supported": True}],
                        "overlimit_preflight_chars": 120_001,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    return golden, context


def _synthetic_repeat(
    *,
    golden: Path,
    context: Path,
    database: Path,
    run_id: str,
    launch_profile: str,
) -> TranscriptAuditRepeat:
    records, overlimit = document_narrative_eval_records(golden, context)
    assert len(records) == 37
    assert overlimit == 120_001
    corpus_sha = _sha(_json([_sha(golden.read_bytes()), _sha(context.read_bytes())]).encode())
    cases: list[dict[str, object]] = []
    for case_id, body, items, expected in records:
        exchanges: list[dict[str, object]] = []
        response = _response("s1", body)

        def synthetic_transport(prompt: str, **kwargs: object) -> str:
            assert kwargs.get("purpose") == _PURPOSE
            generation_id = str(uuid4())
            exchanges.append(
                {
                    "prompt": prompt,
                    "purpose": _PURPOSE,
                    "generation_id": generation_id,
                    "generated_at": datetime.now(UTC).isoformat(),
                    "prompt_sha256": _sha(prompt.encode()),
                    "response_sha256": _sha(response.encode()),
                    "model": _MODEL,
                    "backend": "codex",
                    "routing_config_sha256": _route_sha(launch_profile),
                    "latency_ms": 1,
                    "repair": False,
                    "response": response,
                }
            )
            return response

        result = grade_document_narrative_case(
            case_id=case_id,
            body=body,
            items=items,
            expected=expected,
            db_path=database,
            transport=synthetic_transport,
            exchanges=exchanges,
        )
        assert result.passed
        cases.append(
            {
                "case_id": result.case_id,
                "passed": result.passed,
                "actual_json": result.actual_json,
                "prompt_text": result.prompt_text,
                "response_text": result.response_text,
                "failure_stage": result.failure_stage,
            }
        )

    cases.append(
        {
            "case_id": "overlimit_source_preflight_no_call",
            "passed": True,
            "actual_json": _json({"preflight_rejected": True, "provider_calls": 0}),
            "prompt_text": None,
            "response_text": None,
            "failure_stage": None,
        }
    )
    return TranscriptAuditRepeat.model_validate(
        {
            "schema_version": "transcript-summary-audit-repeat@1",
            "launch_profile": launch_profile,
            "purpose": _PURPOSE,
            "model": _MODEL,
            "backend": "codex",
            "routing_config_sha256": _route_sha(launch_profile),
            "routing_configuration": _route_configuration(launch_profile),
            "audit_code_sha256": "c" * 64,
            "corpus_sha256": corpus_sha,
            "result": {
                "run_id": run_id,
                "purpose": _PURPOSE,
                "mode": "live",
                "model": _MODEL,
                "cases": cases,
            },
        }
    )


class _ProofFiles:
    def __init__(
        self, root: Path, code_root: Path, golden: Path, context: Path, database: Path
    ) -> None:
        self.root = root
        self.code_root = code_root
        self.proof_root = root / "data/model_evals/transcript_summary_qualification"
        self.proof_root.mkdir(parents=True)
        self.golden = golden
        self.context = context
        self.database = database
        self.repeats = [
            _synthetic_repeat(
                golden=golden,
                context=context,
                database=database,
                run_id=uuid4().hex,
                launch_profile=launch_profile,
            )
            for launch_profile in ("manual", "manual", "cron", "cron")
        ]
        self.repeat_paths = [self.proof_root / f"repeat-{index}.json" for index in range(1, 5)]
        self.evaluation_paths = {
            profile: self.proof_root / f"accepted-{profile}-evaluation.json"
            for profile in ("manual", "cron")
        }
        self.qualification_path = (
            code_root / "evals/qualification/transcript_summary_grounding.json"
        )
        self.qualification_path.parent.mkdir(parents=True)
        self.evaluations: dict[str, dict[str, object]] = {}
        self._write_all()

    def _write_all(self) -> None:
        references_by_profile: dict[str, list[dict[str, str]]] = {
            "manual": [],
            "cron": [],
        }
        for repeat, path in zip(self.repeats, self.repeat_paths, strict=True):
            content = repeat.model_dump_json().encode()
            path.write_bytes(content)
            references_by_profile[repeat.launch_profile].append(
                {"path": path.name, "sha256": _sha(content)}
            )
        corpus_sha = verification.transcript_audit_corpus_sha256()
        bundle_path = self.qualification_path
        existing_profiles: dict[str, dict[str, object]] = {}
        if bundle_path.exists():
            current = TranscriptAuditQualificationSet.model_validate_json(bundle_path.read_bytes())
            existing_profiles = {
                profile.launch_profile: profile.model_dump(mode="json")
                for profile in current.profiles
            }
        profiles: list[TranscriptAuditQualification] = []
        for launch_profile in ("manual", "cron"):
            evaluation_path = self.evaluation_paths[launch_profile]
            if launch_profile in self.evaluations:
                evaluation = self.evaluations[launch_profile]
            else:
                evaluation: dict[str, object] = {
                    "launch_profile": launch_profile,
                    "purpose": _PURPOSE,
                    "schema_version": "transcript-summary-audit-evaluation@1",
                    "status": "passed",
                    "model": _MODEL,
                    "backend": "codex",
                    "routing_config_sha256": _route_sha(launch_profile),
                    "audit_code_sha256": "c" * 64,
                    "corpus_sha256": corpus_sha,
                    "repetitions": 2,
                    "case_count": 38,
                    "false_accepts": 0,
                    "false_rejects": 0,
                    "malformed_or_unavailable": 0,
                    "typed_contexts_checked": True,
                    "over_limit_blocked_before_generation": True,
                    "coverage": _REQUIRED_COVERAGE,
                    "max_source_characters": 120_000,
                    "max_candidate_characters": 80_000,
                    "repeat_proofs": references_by_profile[launch_profile],
                }
                self.evaluations[launch_profile] = evaluation
            evaluation["repeat_proofs"] = references_by_profile[launch_profile]
            evaluation_path.write_text(_json(evaluation), encoding="utf-8")
            evaluation_bytes = evaluation_path.read_bytes()
            previous = existing_profiles.get(launch_profile)
            qualified_at = (
                str(previous["qualified_at"])
                if previous is not None
                else (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
            )
            expires_at = (
                str(previous["expires_at"])
                if previous is not None
                else (datetime.now(UTC) + timedelta(days=1)).isoformat()
            )
            profiles.append(
                TranscriptAuditQualification(
                    schema_version="transcript-summary-audit-qualification@1",
                    launch_profile=launch_profile,
                    status="qualified",
                    purpose=_PURPOSE,
                    model=_MODEL,
                    backend="codex",
                    routing_config_sha256=_route_sha(launch_profile),
                    audit_code_sha256="c" * 64,
                    corpus_sha256=corpus_sha,
                    proof_path=evaluation_path.name,
                    proof_sha256=_sha(evaluation_bytes),
                    qualified_at=qualified_at,
                    expires_at=expires_at,
                    scope="registered_original_transcript_full_context@1",
                    max_source_characters=120_000,
                    max_candidate_characters=80_000,
                    limitations=["Synthetic corpus and transport; not a semantic qualification."],
                )
            )
        bundle = TranscriptAuditQualificationSet(
            schema_version="transcript-summary-audit-qualification-set@1",
            profiles=profiles,
        )
        self.qualification_path.write_text(bundle.model_dump_json(), encoding="utf-8")

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(verification, "_CODE_ROOT", self.code_root)
        monkeypatch.setattr(verification, "_QUALIFICATION", self.qualification_path)
        monkeypatch.setattr(verification, "_CORPUS", self.golden)
        monkeypatch.setattr(verification, "_CONTEXT_CORPUS", self.context)
        monkeypatch.setattr(verification, "transcript_audit_code_sha256", lambda: "c" * 64)
        monkeypatch.setattr(verification, "prepare_llm_call", _prepared_codex_route)
        monkeypatch.setattr(verification, "model_for_subscription_backend", _identity_model)
        monkeypatch.setattr(verification, "routing_config_sha256", _selected_route_hash)

    def rewrite(self) -> None:
        """Refresh proof references after a deliberate in-test mutation."""
        self.repeats = [
            TranscriptAuditRepeat.model_validate_json(path.read_bytes())
            for path in self.repeat_paths
        ]
        self._write_all()

    def refresh_evaluation_and_qualification(self, launch_profile: str = "manual") -> None:
        """Rebind the profile receipt after an intentional evaluation edit."""
        path = self.evaluation_paths[launch_profile]
        self.evaluations[launch_profile] = json.loads(path.read_text(encoding="utf-8"))
        bundle = TranscriptAuditQualificationSet.model_validate_json(
            self.qualification_path.read_bytes()
        )
        profiles = [
            profile.model_copy(update={"proof_sha256": _sha(path.read_bytes())})
            if profile.launch_profile == launch_profile
            else profile
            for profile in bundle.profiles
        ]
        updated = TranscriptAuditQualificationSet(
            schema_version=bundle.schema_version,
            profiles=profiles,
        )
        self.qualification_path.write_text(updated.model_dump_json(), encoding="utf-8")


def _prepared_codex_route(_prompt: str, **kwargs: object) -> object:
    from types import SimpleNamespace

    return SimpleNamespace(
        purpose=kwargs["purpose"],
        model=_MODEL,
        backend="codex",
        routing_config_sha256=_route_sha("manual"),
    )


def _identity_model(model: str, _backend: str) -> str:
    return model


def _selected_route_hash(_purpose: str) -> str:
    return _route_sha("manual")


@pytest.fixture
def proof_files(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> _ProofFiles:
    monkeypatch.setattr(claims, "call_llm_structured", structured.call_llm_structured)
    code_root = tmp_path / "synthetic-code-root"
    code_root.mkdir()
    golden, context = _write_corpus(code_root)
    monkeypatch.setattr(verification, "_CODE_ROOT", code_root)
    monkeypatch.setattr(verification, "_CORPUS", golden)
    monkeypatch.setattr(verification, "_CONTEXT_CORPUS", context)

    # These tests reconstruct synthetic physical-call proofs only. Native
    # source qualification has its own real-helper tests and cannot be issued here.
    def synthetic_limits(*_args: object, **_kwargs: object) -> tuple[int, int]:
        return 120_000, 80_000

    monkeypatch.setattr(
        "evals.ask_citations.native_transcript_qualification_limits",
        synthetic_limits,
    )
    database = migrated_db(tmp_path / "synthetic-authority.db")
    with sqlite3.connect(database) as conn:
        conn.execute(
            "UPDATE llm_budgets SET monthly_cap_usd=100,on_exceed='block' WHERE purpose=?",
            (_PURPOSE,),
        )
    state_root = tmp_path / "synthetic-state-root"
    state_root.mkdir()
    proofs = _ProofFiles(state_root, code_root, golden, context, database)
    proofs.install(monkeypatch)
    return proofs


def _verifier(proofs: _ProofFiles) -> TranscriptSummaryVerifier:
    return TranscriptSummaryVerifier(proofs.database, proofs.root)


def test_accepted_repeat_proofs_reconstruct_all_cases_and_calls(proof_files: _ProofFiles) -> None:
    verifier = _verifier(proof_files)

    assert verifier.qualification.purpose == _PURPOSE
    assert verifier.qualification.launch_profile == "manual"
    assert len(proof_files.repeats) == 4
    assert all(len(repeat.result.cases) == 38 for repeat in proof_files.repeats)


def test_native_population_is_required_before_profile_proof_acceptance(
    proof_files: _ProofFiles, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unavailable(*args: object, **kwargs: object) -> tuple[int, int]:
        raise ValueError("native gold is unavailable")

    monkeypatch.setattr("evals.ask_citations.native_transcript_qualification_limits", unavailable)
    with pytest.raises(TranscriptSummaryVerificationError):
        _verifier(proof_files)


def test_proof_reconstruction_uses_explicit_fixture_root_without_moving_sources(
    proof_files: _ProofFiles, tmp_path: Path
) -> None:
    proof_state = tmp_path / "isolated-proof-state"
    proof_state.mkdir()
    (proof_files.root / "data").rename(proof_state / "data")

    verifier = TranscriptSummaryVerifier(
        proof_files.database, proof_files.root, proof_state_root=proof_state
    )

    assert verifier.state_root == proof_files.root
    assert verifier.proof_state_root == proof_state
    assert verifier.qualification.launch_profile == "manual"
    assert not (proof_files.root / "data").exists()


@pytest.mark.parametrize("limits", [(119_999, 80_000), (120_000, 79_999)])
def test_qualification_cannot_raise_caps_above_native_positive_measurements(
    proof_files: _ProofFiles, monkeypatch: pytest.MonkeyPatch, limits: tuple[int, int]
) -> None:
    def measured_limits(*_args: object, **_kwargs: object) -> tuple[int, int]:
        return limits

    monkeypatch.setattr(
        "evals.ask_citations.native_transcript_qualification_limits",
        measured_limits,
    )
    with pytest.raises(TranscriptSummaryVerificationError) as failure:
        _verifier(proof_files)
    assert isinstance(failure.value.__cause__, ValueError)
    assert "exceed measured" in str(failure.value.__cause__)


def test_proof_reader_accepts_frozen_payload_and_checks_reparse_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_root = tmp_path / "state"
    proof_root = state_root / "data/model_evals/transcript_summary_qualification"
    proof_root.mkdir(parents=True)
    payload = b'{"sealed":true}\n'
    (proof_root / "accepted.json").write_bytes(payload)
    guarded_roots: list[Path] = []
    original_guard = evaluation_evidence.require_no_reparse_points

    def record_guard(path: Path) -> None:
        guarded_roots.append(path)
        original_guard(path)

    monkeypatch.setattr(evaluation_evidence, "require_no_reparse_points", record_guard)

    assert (
        evaluation_evidence.read_transcript_audit_proof(state_root, "accepted.json", _sha(payload))
        == payload
    )
    assert guarded_roots == [proof_root]


def test_proof_reader_rejects_wrong_hash(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    proof_root = state_root / "data/model_evals/transcript_summary_qualification"
    proof_root.mkdir(parents=True)
    (proof_root / "accepted.json").write_bytes(b"sealed")

    with pytest.raises(ValueError, match="proof changed"):
        evaluation_evidence.read_transcript_audit_proof(state_root, "accepted.json", "0" * 64)


@pytest.mark.parametrize(
    "reference",
    [
        "",
        ".",
        "./accepted.json",
        "../outside.json",
        "nested/../outside.json",
        "a\\b.json",
        "/rooted.json",
        "C:/drive.json",
        "//server/share.json",
        "\\\\server\\share.json",
    ],
)
def test_proof_reader_rejects_nonrelative_or_ambiguous_paths(
    tmp_path: Path, reference: str
) -> None:
    state_root = tmp_path / "state"
    (state_root / "data/model_evals/transcript_summary_qualification").mkdir(parents=True)

    with pytest.raises(ValueError, match="reference is invalid"):
        evaluation_evidence.read_transcript_audit_proof(state_root, reference, "a" * 64)


def test_proof_reader_rejects_symlinked_evidence(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    proof_root = state_root / "data/model_evals/transcript_summary_qualification"
    proof_root.mkdir(parents=True)
    outside = tmp_path / "outside.json"
    outside.write_bytes(b"sealed")
    (proof_root / "linked.json").symlink_to(outside)

    with pytest.raises(ImmutableArtifactConflictError, match="reparse point"):
        evaluation_evidence.read_transcript_audit_proof(state_root, "linked.json", _sha(b"sealed"))


@pytest.mark.parametrize(
    "tamper",
    ["duplicate_repeat_reference", "duplicate_run_id", "duplicate_generation_id"],
)
def test_duplicate_proof_identities_are_rejected(proof_files: _ProofFiles, tamper: str) -> None:
    if tamper == "duplicate_repeat_reference":
        references = cast(list[dict[str, str]], proof_files.evaluations["manual"]["repeat_proofs"])
        references[1] = dict(references[0])
        proof_files.evaluation_paths["manual"].write_text(
            _json(proof_files.evaluations["manual"]), encoding="utf-8"
        )
        proof_files.refresh_evaluation_and_qualification()
        with pytest.raises(TranscriptSummaryVerificationError):
            _verifier(proof_files)
        return

    repeats = [json.loads(path.read_text(encoding="utf-8")) for path in proof_files.repeat_paths]
    if tamper == "duplicate_run_id":
        repeats[2]["result"]["run_id"] = repeats[0]["result"]["run_id"]
    else:
        first_case = json.loads(repeats[0]["result"]["cases"][0]["actual_json"])
        second_case = json.loads(repeats[2]["result"]["cases"][0]["actual_json"])
        first_generation = first_case["exchanges"][0]["generation_id"]
        second_case["exchanges"][0]["generation_id"] = first_generation
        repeats[2]["result"]["cases"][0]["actual_json"] = _json(second_case)
    for path, repeat in zip(proof_files.repeat_paths, repeats, strict=True):
        path.write_text(_json(repeat), encoding="utf-8")
    proof_files.rewrite()

    with pytest.raises(TranscriptSummaryVerificationError):
        _verifier(proof_files)


@pytest.mark.parametrize("tamper", ["missing_case", "duplicate_case", "false_case_count"])
def test_incomplete_or_false_case_population_is_rejected(
    proof_files: _ProofFiles, tamper: str
) -> None:
    repeats = [json.loads(path.read_text(encoding="utf-8")) for path in proof_files.repeat_paths]
    cases = repeats[0]["result"]["cases"]
    if tamper == "missing_case":
        cases.pop(0)
    elif tamper == "duplicate_case":
        cases[-1] = dict(cases[0])
    else:
        proof_files.evaluations["manual"]["case_count"] = 39
    for path, repeat in zip(proof_files.repeat_paths, repeats, strict=True):
        path.write_text(_json(repeat), encoding="utf-8")
    proof_files.rewrite()

    with pytest.raises(TranscriptSummaryVerificationError):
        _verifier(proof_files)


@pytest.mark.parametrize(
    "tamper",
    [
        "edited_context",
        "edited_raw_response",
        "wrong_hash",
        "wrong_purpose",
        "changed_configuration",
    ],
)
def test_tampered_call_evidence_is_rejected(proof_files: _ProofFiles, tamper: str) -> None:
    repeat_data = json.loads(proof_files.repeat_paths[0].read_text(encoding="utf-8"))
    case = repeat_data["result"]["cases"][0]
    actual = json.loads(case["actual_json"])
    response_list = json.loads(case["response_text"])
    if tamper == "edited_context":
        response_data = json.loads(response_list[0])
        response_data["spans"][0]["context"]["period"] = "Q2 2026"
        response_list[0] = _json(response_data)
        exchange = actual["exchanges"][0]
        exchange["response_sha256"] = _sha(response_list[0].encode())
        case["response_text"] = _json(response_list)
        case["actual_json"] = _json(actual)
    elif tamper == "edited_raw_response":
        response_list[0] = response_list[0].replace(
            _CLAIM, "Revenue was USD 11 million for Q1 2026."
        )
        actual["exchanges"][0]["response_sha256"] = _sha(response_list[0].encode())
        case["response_text"] = _json(response_list)
        case["actual_json"] = _json(actual)
    elif tamper == "wrong_hash":
        actual["exchanges"][0]["prompt_sha256"] = "0" * 64
        case["actual_json"] = _json(actual)
    elif tamper == "wrong_purpose":
        actual["exchanges"][0]["purpose"] = "ask_claim_grounding"
        case["actual_json"] = _json(actual)
    else:
        repeat_data["routing_configuration"]["subscription_route"] = ["claude"]

    repeat_data["result"]["cases"][0] = case
    proof_files.repeat_paths[0].write_text(_json(repeat_data), encoding="utf-8")
    proof_files.rewrite()

    with pytest.raises(TranscriptSummaryVerificationError):
        _verifier(proof_files)
