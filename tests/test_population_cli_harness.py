# pyright: reportPrivateUsage=false
"""Compact proof for the shared population CLI harness."""

from __future__ import annotations

import argparse
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Protocol

import pytest

from execution import populate_canonical_resolution as canonical
from execution import populate_document_processing as document
from execution import populate_metric_ontology as ontology
from provenance.population_cli_harness import (
    parse_timezone_aware_datetime,
    validate_protected_receipt_path,
)


class ReceiptValidator(Protocol):
    def __call__(
        self, receipt: Path, *, database: Path, protected_receipts: tuple[Path, ...]
    ) -> Path: ...


_PARSERS: tuple[Callable[[str], datetime], ...] = (
    canonical._datetime,
    document._datetime,
    ontology._datetime,
)

_VALIDATORS: tuple[tuple[ReceiptValidator, str], ...] = (
    (
        canonical.validate_receipt_path,
        "canonical receipt aliases a protected artifact",
    ),
    (
        document.validate_receipt_path,
        "document-processing receipt aliases a protected artifact",
    ),
    (
        ontology.validate_receipt_path,
        "ontology receipt aliases a protected artifact",
    ),
)


def test_parsers_match_harness_for_aware_inputs() -> None:
    for parser in _PARSERS:
        assert parser("2026-07-29T00:00:00Z") == parse_timezone_aware_datetime(
            "2026-07-29T00:00:00Z"
        )
        assert parser("2026-07-29T02:00:00+02:00") == parse_timezone_aware_datetime(
            "2026-07-29T02:00:00+02:00"
        )
        assert parser("2026-07-29T00:00:00Z").utcoffset() is not None


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("2026-07-29T00:00:00", "datetime must include a timezone"),
        ("not-a-datetime", "must be an ISO-8601 datetime"),
        ("2026-13-01T00:00:00Z", "must be an ISO-8601 datetime"),
    ],
)
def test_parsers_reject_with_exact_message(value: str, message: str) -> None:
    for parser in (*_PARSERS, parse_timezone_aware_datetime):
        with pytest.raises(argparse.ArgumentTypeError) as excinfo:
            parser(value)
        assert str(excinfo.value) == message


def test_validators_reject_protected_aliases_with_exact_message(
    tmp_path: Path,
) -> None:
    database = tmp_path / "candidate.db"
    prerequisite = tmp_path / "input.json"
    database.write_bytes(b"db")
    prerequisite.write_text("{}", encoding="utf-8")
    for validator, message in _VALIDATORS:
        for alias in (database, Path(f"{database}-wal"), prerequisite):
            with pytest.raises(ValueError) as excinfo:
                validator(alias, database=database, protected_receipts=(prerequisite,))
            assert str(excinfo.value) == message


def test_harness_normalizes_and_covers_all_alias_classes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    database = tmp_path / "candidate.db"
    prerequisite = tmp_path / "input.json"
    database.write_bytes(b"db")
    prerequisite.write_text("{}", encoding="utf-8")
    destination = validate_protected_receipt_path(
        Path("output.json"),
        database=database,
        protected_receipts=(),
        conflict_message="conflict",
    )
    assert destination == tmp_path / "output.json"
    assert destination.is_absolute()
    for alias in (
        database,
        Path(f"{database}-wal"),
        Path(f"{database}-shm"),
        Path(f"{database}-journal"),
        prerequisite,
    ):
        with pytest.raises(ValueError) as excinfo:
            validate_protected_receipt_path(
                alias,
                database=database,
                protected_receipts=(prerequisite,),
                conflict_message="conflict",
            )
        assert str(excinfo.value) == "conflict"
