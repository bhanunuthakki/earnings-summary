"""Strict transport boundary for retained canonical financial evidence."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from sources.report_financials import FinancialEvidenceReference


def _payload() -> dict[str, object]:
    return {
        "ticker": "SYNTH",
        "concept": "revenue",
        "canonical_metric_cell_id": "cell-1",
        "observation_id": "observation-1",
        "canonical_resolution_revision_id": "resolution-1",
        "metric_definition_revision_id": "definition-1",
        "as_of": datetime(2025, 3, 31, tzinfo=UTC),
    }


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("ticker", ""),
        ("ticker", "synth"),
        ("ticker", "S" * 33),
        ("ticker", "SYNTH/OTHER"),
        ("ticker", 123),
        ("concept", "unknown"),
        ("observation_id", ""),
        ("observation_id", " \n "),
        ("observation_id", "o" * 129),
        ("observation_id", 123),
        ("observation_id", True),
        ("canonical_metric_cell_id", ""),
        ("canonical_resolution_revision_id", "r" * 129),
        ("metric_definition_revision_id", None),
        ("as_of", datetime(2025, 3, 31)),
        ("as_of", datetime.now(UTC) + timedelta(days=1)),
        ("as_of", 1),
        ("as_of", "2025-03-31T00:00:00Z"),
        ("unexpected", "value"),
    ],
)
def test_reference_rejects_untrusted_python_fields(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        FinancialEvidenceReference.model_validate({**_payload(), field: value})


@pytest.mark.parametrize("as_of", ["2025-03-31T00:00:00", "2999-01-01T00:00:00Z", "1", 1, None])
def test_reference_rejects_invalid_json_cutoffs(as_of: object) -> None:
    payload = {**_payload(), "as_of": as_of}
    with pytest.raises(ValidationError):
        FinancialEvidenceReference.model_validate_json(json.dumps(payload))


def test_reference_normalizes_aware_cutoff_and_roundtrips_json() -> None:
    original = datetime(2025, 3, 31, 7, tzinfo=timezone(timedelta(hours=7)))
    reference = FinancialEvidenceReference.model_validate({**_payload(), "as_of": original})
    assert reference.as_of == datetime(2025, 3, 31, tzinfo=UTC)
    assert reference.as_of.tzinfo is UTC
    assert FinancialEvidenceReference.model_validate_json(reference.model_dump_json()) == reference
    with pytest.raises(ValidationError, match="frozen"):
        reference.observation_id = "changed"
