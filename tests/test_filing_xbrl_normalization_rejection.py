from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

from provenance.filing_xbrl_fact_adapter import (
    FilingXbrlExtractionIdentity,
    FilingXbrlFactAdapter,
    FilingXbrlNormalizationRejection,
    FilingXbrlNormalizedOutput,
    FilingXbrlSubjectIdentity,
    QuarantinedFilingXbrlDisposition,
)

STAMP = datetime(2026, 7, 28, tzinfo=UTC)


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def test_normalization_rejection_receives_one_terminal_disposition() -> None:
    raw_json = _canonical({"contextRef": None, "name": "us-gaap:Revenue", "value": "10"})
    rejection = FilingXbrlNormalizationRejection(
        ordinal=0,
        evidence_node_id="node-1",
        canonical_raw_fact_json=raw_json,
        raw_fact_sha256=_sha(raw_json),
        source_entry_sha256="a" * 64,
        source_locator_sha256="b" * 64,
        reason_code="missing_context",
        detail="source fact has no contextRef",
        knowledge_at=STAMP,
        recorded_at=STAMP,
    )
    extraction = FilingXbrlExtractionIdentity(
        document_version_id="document-1",
        extraction_run_id="run-1",
        extractor_name="filing-native-xbrl",
        extractor_code_version="v1",
        extractor_config_sha256="c" * 64,
        extraction_input_sha256="d" * 64,
        extraction_output_sha256="0" * 64,
        expected_evidence_node_count=1,
        knowledge_at=STAMP,
        recorded_at=STAMP,
    )
    output = FilingXbrlNormalizedOutput.with_computed_digest(
        extraction=extraction,
        subject=FilingXbrlSubjectIdentity(
            reporting_entity_id="reporting-1",
            selected_subject_binding_revision_id="binding-1",
        ),
        entries=(),
        rejections=(rejection,),
    )

    adapted = FilingXbrlFactAdapter().adapt(output)

    assert adapted.total_count == 1
    assert adapted.published_count == 0
    assert adapted.quarantined_count == 1
    disposition = adapted.dispositions[0]
    assert isinstance(disposition, QuarantinedFilingXbrlDisposition)
    assert disposition.reason == "normalization_rejected"
    assert adapted.entry_commitments[0].source_entry_sha256 == "a" * 64
