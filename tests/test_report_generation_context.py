"""Bounded offline tests for report generation context semantic identity (v1/v2).

The golden v1 digests below were derived from the pre-change
``report.generation_context`` implementation (before any v2 field existed) using
the fixture values defined in this file; they are not regenerated from the new
implementation.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from report.generation_context import (
    DBSnapshotRef,
    ReportCaptureUnavailable,
    ReportDependency,
    ReportDirectoryDependency,
    ReportGenerationContext,
    ReportModelBinding,
)

GOLDEN_V1_SHA256 = "d8b1e734eb0f5decc185113979d2763fc3df601a61ce2de208c852a1235c5b7b"
GOLDEN_V1_MODEL_SHA256 = "7f30e27c6cd7446d953d9d781eed58d40525f90e93cd84a636c418e5cb21b576"
GOLDEN_V1_EVIDENCE_SHA256 = "ad124fbb26f13990a9b210b47c193eb061b610eacfbfdbfb378a71179349483e"

CUTOFF = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)


def _hex(value: int) -> str:
    return f"{value:064x}"


REPORT_DATE = date(2026, 1, 15)
COVERAGE_ROLE = "evaluation"


def _model(row_id: int = 7) -> ReportModelBinding:
    return ReportModelBinding(
        row_id=row_id,
        ticker="NU",
        family="dcf_standard",
        input_sha256=_hex(1),
        workbook_sha256=_hex(2),
        engine_version="engine-1",
        provenance_sha256=_hex(3),
        assumption_snapshot_sha256=_hex(4),
    )


def _dependencies() -> tuple[ReportDependency, ...]:
    return (
        ReportDependency(
            kind="document",
            identity="doc-1",
            sha256=_hex(5),
            owner="immutable",
            path="data/docs/doc-1.md",
        ),
        ReportDependency(
            kind="observation",
            identity="obs-9",
            sha256=_hex(6),
            owner="database",
        ),
    )


def _v1_payload() -> dict[str, Any]:
    return {
        "model": _model().model_dump(mode="json"),
        "cutoff_at": CUTOFF.isoformat(),
        "source_manifest_sha256": _hex(7),
        "evidence_snapshot_id": "snap-1",
        "evidence_member_sha256": _hex(8),
        "dependencies": [item.model_dump(mode="json") for item in _dependencies()],
        "sections": ["full_brief", "thesis"],
        "recipe_versions": [["valuation", "2026-01-01"]],
        "expected_current_artifact_id": "artifact-42",
    }


def _v1() -> ReportGenerationContext:
    payload = _v1_payload()
    payload["model"] = _model()
    payload["dependencies"] = _dependencies()
    payload["sections"] = ("full_brief", "thesis")
    payload["recipe_versions"] = (("valuation", "2026-01-01"),)
    return ReportGenerationContext(**payload)


def _db_snapshot() -> DBSnapshotRef:
    return DBSnapshotRef(schema_sha256=_hex(21), consumed_rows_sha256=_hex(22))


def _directory_dependency(relative_path: str = "data/bear_case/NU") -> ReportDirectoryDependency:
    return ReportDirectoryDependency(
        relative_path=relative_path,
        selector="latest by (run_date, run_id)",
        selected_member="case-a.json",
        membership_sha256=_hex(23),
        producer="sections.bear_case",
        fence="producer-lock",
    )


def _v2_inputs() -> dict[str, Any]:
    return {
        "report_date": REPORT_DATE.isoformat(),
        "coverage_role": COVERAGE_ROLE,
        "source_authority_sha256": _hex(41),
    }


def _v2_payload() -> dict[str, Any]:
    return {
        **_v1_payload(),
        "schema_version": "report_generation.v2",
        "db_snapshot": _db_snapshot().model_dump(mode="json"),
        "directory_dependencies": [_directory_dependency().model_dump(mode="json")],
        **_v2_inputs(),
    }


def _v2() -> ReportGenerationContext:
    return ReportGenerationContext.model_validate(_v2_payload())


class TestV1Compatibility:
    def test_golden_v1_digest_unchanged(self) -> None:
        context = _v1()
        assert context.sha256 == GOLDEN_V1_SHA256
        assert context.model_sha256 == GOLDEN_V1_MODEL_SHA256
        assert context.evidence_sha256 == GOLDEN_V1_EVIDENCE_SHA256

    def test_v1_hash_stable_after_parse_and_reserialize(self) -> None:
        context = _v1()
        parsed = ReportGenerationContext.model_validate_json(context.model_dump_json())
        assert parsed.sha256 == context.sha256 == GOLDEN_V1_SHA256
        reparsed = ReportGenerationContext.model_validate(parsed.model_dump(mode="json"))
        assert reparsed.sha256 == GOLDEN_V1_SHA256

    def test_v1_rejects_nondefault_v2_fields(self) -> None:
        base = _v1().model_dump(mode="json")
        with_db = {**base, "db_snapshot": _db_snapshot().model_dump(mode="json")}
        with pytest.raises(ValidationError):
            ReportGenerationContext.model_validate(with_db)
        with_directories = {
            **base,
            "directory_dependencies": [_directory_dependency().model_dump(mode="json")],
        }
        with pytest.raises(ValidationError):
            ReportGenerationContext.model_validate(with_directories)
        with_unfenced = {
            **base,
            "unfenced_consumed_dependencies": [
                ReportDependency(
                    kind="analysis", identity="a-1", sha256=_hex(9), owner="analysis"
                ).model_dump(mode="json")
            ],
        }
        with pytest.raises(ValidationError):
            ReportGenerationContext.model_validate(with_unfenced)
        with_report_date = {**base, "report_date": REPORT_DATE.isoformat()}
        with pytest.raises(ValidationError):
            ReportGenerationContext.model_validate(with_report_date)
        with_coverage_role = {**base, "coverage_role": COVERAGE_ROLE}
        with pytest.raises(ValidationError):
            ReportGenerationContext.model_validate(with_coverage_role)
        with_authority = {**base, "source_authority_sha256": _hex(41)}
        with pytest.raises(ValidationError):
            ReportGenerationContext.model_validate(with_authority)

    def test_v1_rejects_source_owner(self) -> None:
        base = _v1().model_dump(mode="json")
        base["dependencies"] = [
            ReportDependency(
                kind="contract", identity="feed-1", sha256=_hex(10), owner="source"
            ).model_dump(mode="json")
        ]
        with pytest.raises(ValidationError, match="source-owned"):
            ReportGenerationContext.model_validate(base)

    def test_v2_accepts_source_owner(self) -> None:
        context = ReportGenerationContext.model_validate(
            {
                **_v2_payload(),
                "dependencies": [
                    ReportDependency(
                        kind="contract", identity="feed-1", sha256=_hex(10), owner="source"
                    ).model_dump(mode="json")
                ],
            }
        )
        assert context.dependencies[0].owner == "source"

    def test_default_schema_version_is_v1(self) -> None:
        assert ReportGenerationContext.model_validate(_v1_payload()).schema_version == (
            "report_generation.v1"
        )

    def test_unknown_schema_version_rejected(self) -> None:
        payload = {**_v1_payload(), "schema_version": "report_generation.v3"}
        with pytest.raises(ValidationError):
            ReportGenerationContext.model_validate(payload)


class TestCutoff:
    def test_missing_cutoff_rejected(self) -> None:
        payload = _v1_payload()
        del payload["cutoff_at"]
        with pytest.raises(ValidationError):
            ReportGenerationContext.model_validate(payload)

    def test_none_cutoff_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ReportGenerationContext.model_validate({**_v1_payload(), "cutoff_at": None})

    def test_naive_cutoff_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ReportGenerationContext.model_validate(
                {**_v1_payload(), "cutoff_at": datetime(2026, 1, 15, 12, 0).isoformat()}
            )


class TestV2Requirements:
    def test_v2_requires_db_snapshot(self) -> None:
        payload = dict(_v2_payload())
        del payload["db_snapshot"]
        with pytest.raises(ValidationError, match="db snapshot"):
            ReportGenerationContext.model_validate(payload)

    def test_v2_requires_nonmissing_evidence(self) -> None:
        with pytest.raises(ValidationError, match="evidence"):
            ReportGenerationContext.model_validate(
                {
                    **_v2_payload(),
                    "evidence_snapshot_id": None,
                    "evidence_member_sha256": None,
                }
            )

    def test_v2_rejects_unpaired_evidence(self) -> None:
        with pytest.raises(ValidationError, match="paired"):
            ReportGenerationContext.model_validate(
                {**_v2_payload(), "evidence_member_sha256": None}
            )

    def test_v2_requires_report_date(self) -> None:
        payload = dict(_v2_payload())
        del payload["report_date"]
        with pytest.raises(ValidationError, match="report date"):
            ReportGenerationContext.model_validate(payload)

    def test_v2_requires_coverage_role(self) -> None:
        payload = dict(_v2_payload())
        del payload["coverage_role"]
        with pytest.raises(ValidationError, match="coverage role"):
            ReportGenerationContext.model_validate(payload)

    def test_v2_requires_source_authority(self) -> None:
        payload = dict(_v2_payload())
        del payload["source_authority_sha256"]
        with pytest.raises(ValidationError, match="source authority"):
            ReportGenerationContext.model_validate(payload)

    def test_v2_rejects_unknown_coverage_role(self) -> None:
        with pytest.raises(ValidationError):
            ReportGenerationContext.model_validate({**_v2_payload(), "coverage_role": "watchlist"})

    def test_v2_digest_includes_new_fields(self) -> None:
        context = _v2()
        without = ReportGenerationContext.model_validate(
            {**_v2_payload(), "directory_dependencies": []}
        )
        assert context.sha256 != without.sha256


class TestSemanticRequestKey:
    def test_identical_requests_join(self) -> None:
        assert _v2().semantic_request_key == _v2().semantic_request_key
        first = _v2()
        second = ReportGenerationContext.model_validate_json(first.model_dump_json())
        assert first.semantic_request_key == second.semantic_request_key

    def test_v1_key_fails_closed(self) -> None:
        # A v1 context cannot represent complete request inputs; it must never
        # mint a misleadingly complete key. The exact v1 sha256 remains the
        # supported v1 fingerprint.
        context = _v1()
        with pytest.raises(ValueError, match="cannot mint"):
            _ = context.semantic_request_key
        assert context.sha256 == GOLDEN_V1_SHA256

    def test_cutoff_difference_splits_key_despite_identical_documents(self) -> None:
        payload = _v2_payload()
        other = ReportGenerationContext.model_validate(
            {**payload, "cutoff_at": datetime(2026, 1, 16, 12, 0, tzinfo=UTC).isoformat()}
        )
        assert ReportGenerationContext.model_validate(payload).semantic_request_key != (
            other.semantic_request_key
        )

    def test_diagnostics_outside_context_do_not_affect_key(self) -> None:
        # Review/approval/capture receipts live outside the context; identical
        # contexts captured "later" keep the same key because no capture-time
        # field exists in the context model.
        first = _v2()
        second = ReportGenerationContext.model_validate_json(first.model_dump_json())
        assert first.semantic_request_key == second.semantic_request_key

    def test_model_binding_change_invalidates(self) -> None:
        payload = _v2_payload()
        other = {**payload, "model": _model(row_id=8).model_dump(mode="json")}
        assert ReportGenerationContext.model_validate(payload).semantic_request_key != (
            ReportGenerationContext.model_validate(other).semantic_request_key
        )

    def test_dependency_change_invalidates(self) -> None:
        payload = _v2_payload()
        other = {
            **payload,
            "dependencies": [
                ReportDependency(
                    kind="document",
                    identity="doc-1",
                    sha256=_hex(15),
                    owner="immutable",
                    path="data/docs/doc-1.md",
                ).model_dump(mode="json")
            ],
        }
        assert ReportGenerationContext.model_validate(payload).semantic_request_key != (
            ReportGenerationContext.model_validate(other).semantic_request_key
        )

    def test_dependency_order_is_semantic(self) -> None:
        # Consumed order matters; a reordering must not silently join. The second
        # context reverses ONLY the dependencies of a validated copy of the first.
        first = _v2()
        reversed_payload = first.model_dump(mode="json")
        reversed_payload["dependencies"] = [
            item.model_dump(mode="json") for item in reversed(first.dependencies)
        ]
        reordered = ReportGenerationContext.model_validate(reversed_payload)
        assert first.semantic_request_key != reordered.semantic_request_key

    def test_recipe_change_invalidates(self) -> None:
        payload = _v2_payload()
        other = {**payload, "recipe_versions": [["valuation", "2026-02-02"]]}
        assert ReportGenerationContext.model_validate(payload).semantic_request_key != (
            ReportGenerationContext.model_validate(other).semantic_request_key
        )

    def test_section_change_invalidates(self) -> None:
        payload = _v2_payload()
        other = {**payload, "sections": ["full_brief"]}
        assert ReportGenerationContext.model_validate(payload).semantic_request_key != (
            ReportGenerationContext.model_validate(other).semantic_request_key
        )

    def test_expected_current_change_invalidates(self) -> None:
        payload = _v2_payload()
        other = {**payload, "expected_current_artifact_id": "artifact-43"}
        assert ReportGenerationContext.model_validate(payload).semantic_request_key != (
            ReportGenerationContext.model_validate(other).semantic_request_key
        )

    def test_report_date_change_invalidates(self) -> None:
        payload = _v2_payload()
        other = {**payload, "report_date": date(2026, 2, 1).isoformat()}
        assert ReportGenerationContext.model_validate(payload).semantic_request_key != (
            ReportGenerationContext.model_validate(other).semantic_request_key
        )

    def test_coverage_role_change_invalidates(self) -> None:
        payload = _v2_payload()
        other = {**payload, "coverage_role": "portfolio"}
        assert ReportGenerationContext.model_validate(payload).semantic_request_key != (
            ReportGenerationContext.model_validate(other).semantic_request_key
        )

    def test_source_authority_change_invalidates(self) -> None:
        payload = _v2_payload()
        other = {**payload, "source_authority_sha256": _hex(42)}
        assert ReportGenerationContext.model_validate(payload).semantic_request_key != (
            ReportGenerationContext.model_validate(other).semantic_request_key
        )

    def test_unfenced_selection_flags_change_key(self) -> None:
        # The unfenced-consumed subset is eligibility-relevant and enters the key.
        payload = _v2_payload()
        fenced = ReportGenerationContext.model_validate(payload)
        unfenced = ReportGenerationContext.model_validate(
            {
                **payload,
                "unfenced_consumed_dependencies": [_dependencies()[1].model_dump(mode="json")],
            }
        )
        assert fenced.semantic_request_key != unfenced.semantic_request_key

    def test_directory_membership_change_invalidates(self) -> None:
        first = _v2()
        moved = ReportGenerationContext.model_validate(
            {
                **_v2_payload(),
                "directory_dependencies": [
                    {
                        **_directory_dependency().model_dump(mode="json"),
                        "membership_sha256": _hex(24),
                    }
                ],
            }
        )
        assert first.semantic_request_key != moved.semantic_request_key

    def test_directory_order_is_not_semantic(self) -> None:
        one = ReportGenerationContext.model_validate(
            {
                **_v2_payload(),
                "directory_dependencies": [
                    _directory_dependency("data/bear_case/NU").model_dump(mode="json"),
                    _directory_dependency("data/peer/NU").model_dump(mode="json"),
                ],
            }
        )
        two = ReportGenerationContext.model_validate(
            {
                **_v2_payload(),
                "directory_dependencies": [
                    _directory_dependency("data/peer/NU").model_dump(mode="json"),
                    _directory_dependency("data/bear_case/NU").model_dump(mode="json"),
                ],
            }
        )
        assert one.semantic_request_key == two.semantic_request_key

    def test_db_snapshot_change_invalidates_v2_key(self) -> None:
        payload = _v2_payload()
        other = ReportGenerationContext.model_validate(
            {
                **payload,
                "db_snapshot": DBSnapshotRef(
                    schema_sha256=_hex(31), consumed_rows_sha256=_hex(22)
                ).model_dump(mode="json"),
            }
        )
        base = ReportGenerationContext.model_validate(payload)
        assert other.semantic_request_key != base.semantic_request_key


class TestUnfencedSubset:
    def test_matching_subset_accepted(self) -> None:
        context = ReportGenerationContext.model_validate(
            {
                **_v2_payload(),
                "unfenced_consumed_dependencies": [_dependencies()[1].model_dump(mode="json")],
            }
        )
        assert context.unfenced_consumed_dependencies[0].identity == "obs-9"

    def test_foreign_entry_rejected(self) -> None:
        with pytest.raises(ValidationError, match="consumed dependencies"):
            ReportGenerationContext.model_validate(
                {
                    **_v2_payload(),
                    "unfenced_consumed_dependencies": [
                        ReportDependency(
                            kind="analysis",
                            identity="not-consumed",
                            sha256=_hex(51),
                            owner="analysis",
                        ).model_dump(mode="json")
                    ],
                }
            )

    def test_conflicting_digest_rejected(self) -> None:
        payload = _v2_payload()
        consumed = payload["dependencies"][0]
        with pytest.raises(ValidationError, match="match consumed digests"):
            ReportGenerationContext.model_validate(
                {
                    **payload,
                    "unfenced_consumed_dependencies": [{**consumed, "sha256": _hex(52)}],
                }
            )

    def test_conflicting_owner_rejected(self) -> None:
        payload = _v2_payload()
        consumed = payload["dependencies"][0]
        with pytest.raises(ValidationError, match="match consumed digests"):
            ReportGenerationContext.model_validate(
                {
                    **payload,
                    "unfenced_consumed_dependencies": [{**consumed, "owner": "database"}],
                }
            )

    def test_conflicting_path_rejected(self) -> None:
        payload = _v2_payload()
        consumed = payload["dependencies"][1]
        with pytest.raises(ValidationError, match="match consumed digests"):
            ReportGenerationContext.model_validate(
                {
                    **payload,
                    "unfenced_consumed_dependencies": [{**consumed, "path": "data/other.md"}],
                }
            )


class TestStrictnessAndPaths:
    def test_context_rejects_unknown_fields(self) -> None:
        with pytest.raises(ValidationError):
            ReportGenerationContext.model_validate({**_v1_payload(), "captured_at": "2026-01-01"})

    def test_dependency_absolute_path_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ReportDependency(
                kind="document",
                identity="doc-1",
                sha256=_hex(5),
                owner="immutable",
                path="/etc/passwd",
            )

    def test_dependency_parent_escape_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ReportDependency(
                kind="document",
                identity="doc-1",
                sha256=_hex(5),
                owner="immutable",
                path="../outside.md",
            )

    def test_dependency_colon_path_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ReportDependency(
                kind="document",
                identity="doc-1",
                sha256=_hex(5),
                owner="immutable",
                path="C:/data/doc.md",
            )

    def test_directory_absolute_path_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ReportDirectoryDependency(
                relative_path="/data/bear_case",
                selector="latest",
                membership_sha256=_hex(23),
                producer="p",
                fence="producer-lock",
            )

    def test_directory_parent_escape_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ReportDirectoryDependency(
                relative_path="../data/bear_case",
                selector="latest",
                membership_sha256=_hex(23),
                producer="p",
                fence="producer-lock",
            )

    def test_directory_unknown_fence_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ReportDirectoryDependency.model_validate(
                {
                    "relative_path": "data/bear_case",
                    "selector": "latest",
                    "membership_sha256": _hex(23),
                    "producer": "p",
                    "fence": "maybe",
                }
            )

    def test_directory_duplicate_paths_rejected(self) -> None:
        with pytest.raises(ValidationError, match="unique"):
            ReportGenerationContext.model_validate(
                {
                    **_v2_payload(),
                    "directory_dependencies": [
                        _directory_dependency("data/bear_case/NU").model_dump(mode="json"),
                        _directory_dependency("data/bear_case/NU").model_dump(mode="json"),
                    ],
                }
            )

    def test_duplicate_dependency_identity_rejected(self) -> None:
        payload = _v1_payload()
        dup = ReportDependency(
            kind="document", identity="doc-1", sha256=_hex(5), owner="immutable"
        ).model_dump(mode="json")
        payload["dependencies"] = [dict(dup), dict(dup)]
        with pytest.raises(ValidationError, match="unique"):
            ReportGenerationContext.model_validate(payload)


class TestCaptureUnavailable:
    def test_closed_reasons_accepted(self) -> None:
        for reason in ("selected_context_unavailable", "requested_cutoff_mismatch"):
            assert ReportCaptureUnavailable(reason_code=reason).reason_code == reason

    def test_unknown_reason_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ReportCaptureUnavailable.model_validate(
                {"reason_code": "capture_failed_generic", "requested_cutoff_at": None}
            )

    def test_safe_optional_identity(self) -> None:
        assert (
            ReportCaptureUnavailable(
                reason_code="requested_cutoff_mismatch",
                requested_cutoff_at=CUTOFF,
                evidence_snapshot_id="snap-1",
            ).requested_cutoff_at
            == CUTOFF
        )

    def test_strict_unknown_fields(self) -> None:
        with pytest.raises(ValidationError):
            ReportCaptureUnavailable.model_validate(
                {"reason_code": "selected_context_unavailable", "ready": False}
            )

    def test_naive_requested_cutoff_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ReportCaptureUnavailable.model_validate(
                {
                    "reason_code": "requested_cutoff_mismatch",
                    "requested_cutoff_at": datetime(2026, 1, 15, 12, 0).isoformat(),
                }
            )

    def test_no_ready_or_decision_grade_claim(self) -> None:
        assert not any(
            field in ReportCaptureUnavailable.model_fields
            for field in ("ready", "decision", "grade")
        )
