"""Identity and append-only persistence tests for the patent-source fetcher."""

from __future__ import annotations

import importlib.util
import sys
from datetime import datetime
from pathlib import Path
from types import ModuleType

import pytest

from models.patents import ExtensionStatus, FetchSource, Jurisdiction, PatentRecord

ROOT = Path(__file__).resolve().parents[1]


def _load_fetcher() -> ModuleType:
    path = ROOT / "execution" / "fetch_drug_patent_status.py"
    spec = importlib.util.spec_from_file_location("fetch_drug_patent_status_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def fetcher(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    module = _load_fetcher()
    monkeypatch.setattr(module, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(module, "OUT_DIR", tmp_path / ".tmp" / "nvo_patents")
    return module


def _record(*, pulled_at: datetime, expiry: str = "2031-12-05") -> PatentRecord:
    return PatentRecord(
        molecule="semaglutide",
        jurisdiction=Jurisdiction.US,
        patent_number="123456",
        title="Ozempic",
        expiry_date=datetime.fromisoformat(expiry).date(),
        extension_status=ExtensionStatus.ORIGINAL,
        source=FetchSource.FDA_ORANGE_BOOK,
        source_url="https://example.test/orange-book/123456",
        pulled_at=pulled_at,
    )


def test_retry_across_dates_reuses_stable_observation_but_records_new_attempt(
    fetcher: ModuleType,
) -> None:
    first_path, first = fetcher.write_outputs(
        "semaglutide",
        [Jurisdiction.US],
        [_record(pulled_at=datetime(2026, 8, 25, 8, 0))],
        attempt_identity="attempt-1",
        observed_at=datetime(2026, 8, 25, 8, 0),
    )
    first_bytes = first_path.read_bytes()

    retry_path, retry = fetcher.write_outputs(
        "semaglutide",
        [Jurisdiction.US],
        [_record(pulled_at=datetime(2026, 8, 26, 8, 0))],
        attempt_identity="attempt-2",
        observed_at=datetime(2026, 8, 26, 8, 0),
    )

    assert retry.logical_idempotency_key == first.logical_idempotency_key
    assert retry.content_identity == first.content_identity
    assert retry.observation_version == first.observation_version
    assert retry_path == first_path
    assert retry_path.read_bytes() == first_bytes
    assert first.attempt_identity != retry.attempt_identity
    assert first.disposition == "created"
    assert retry.disposition == "replayed"
    attempt_receipts = sorted((first_path.parents[1] / "attempts").glob("*.json"))
    assert [path.stem for path in attempt_receipts] == ["attempt-1", "attempt-2"]


def test_changed_same_day_observation_is_appended_without_overwriting_prior_bytes(
    fetcher: ModuleType,
) -> None:
    observed_at = datetime(2026, 8, 25, 8, 0)
    first_path, first = fetcher.write_outputs(
        "semaglutide",
        [Jurisdiction.US],
        [_record(pulled_at=observed_at, expiry="2031-12-05")],
        attempt_identity="attempt-a",
        observed_at=observed_at,
    )
    first_bytes = first_path.read_bytes()

    changed_path, changed = fetcher.write_outputs(
        "semaglutide",
        [Jurisdiction.US],
        [_record(pulled_at=observed_at, expiry="2032-06-01")],
        attempt_identity="attempt-b",
        observed_at=observed_at,
    )

    assert changed.logical_idempotency_key == first.logical_idempotency_key
    assert changed.content_identity != first.content_identity
    assert changed.observation_version != first.observation_version
    assert changed_path != first_path
    assert first_path.read_bytes() == first_bytes
    assert changed_path.exists()
    assert changed.disposition == "created"
    assert len(list((first_path.parent).glob("*.json"))) == 2
