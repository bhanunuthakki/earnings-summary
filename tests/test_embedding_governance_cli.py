from __future__ import annotations

import argparse
from datetime import UTC, datetime
from pathlib import Path

import pytest

from execution.evaluate_embedding_models import _write_atomic
from execution.promote_embedding_model import _approved_at


def test_evaluation_artifact_write_is_exact_and_replaces_atomically(
    tmp_path: Path,
) -> None:
    output = tmp_path / "nested" / "evaluation.json"

    _write_atomic(output, '{"revision":1}')
    assert output.read_bytes() == b'{"revision":1}\n'

    _write_atomic(output, '{"revision":2}')
    assert output.read_bytes() == b'{"revision":2}\n'
    assert list(output.parent.glob(".*.tmp")) == []


def test_owner_approval_timestamp_is_explicit_and_normalized_to_utc() -> None:
    assert _approved_at("2026-07-28T01:02:03-07:00") == datetime(2026, 7, 28, 8, 2, 3, tzinfo=UTC)
    assert _approved_at("2026-07-28T08:02:03Z") == datetime(2026, 7, 28, 8, 2, 3, tzinfo=UTC)

    with pytest.raises(argparse.ArgumentTypeError, match="timezone"):
        _approved_at("2026-07-28T08:02:03")
