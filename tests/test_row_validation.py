from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import BaseModel, TypeAdapter

from pipeline.row_validation import RowValidationDriftError, validate_provider_rows


class _Row(BaseModel):
    value: int


def test_isolated_bad_row_is_dumped_without_hiding_valid_rows(tmp_path: Path) -> None:
    rows: list[object] = [{"value": index} for index in range(99)]
    rows.append({"value": "bad"})
    accepted = validate_provider_rows(
        rows,
        TypeAdapter(_Row),
        source="provider-test",
        rejection_dir=tmp_path,
    )
    assert len(accepted) == 99
    payload = json.loads((tmp_path / "provider-test.jsonl").read_text(encoding="utf-8"))
    assert payload["index"] == 99
    assert payload["raw"] == {"value": "bad"}


def test_batch_schema_drift_halts_after_dumping_rejections(tmp_path: Path) -> None:
    rows: list[object] = [{"renamed_value": index} for index in range(20)]
    with pytest.raises(RowValidationDriftError, match="20/20"):
        validate_provider_rows(
            rows,
            TypeAdapter(_Row),
            source="provider-drift",
            rejection_dir=tmp_path,
        )
    assert len((tmp_path / "provider-drift.jsonl").read_text(encoding="utf-8").splitlines()) == 20


def test_invalid_threshold_contract_fails_before_consuming_rows(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="max_drop_rate"):
        validate_provider_rows(
            [],
            TypeAdapter(_Row),
            source="provider-test",
            max_drop_rate=1.1,
            rejection_dir=tmp_path,
        )
