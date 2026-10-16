"""Admitted values cannot certify false numbers or unsupported paraphrases."""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Callable, Generator
from decimal import Decimal
from pathlib import Path
from typing import cast

import pytest

from research.memo_claim_support import (
    MemoClaimSupportError,
    MemoReportedValue,
    financial_reader_value,
    verify_reported_memo_claim,
)
from sources.canonical_financial_series import (
    FinancialCadence,
    FinancialConsumerPoint,
    SeriesContinuity,
)
from sources.report_financials import (
    FinancialEvidenceReference,
    read_financial_evidence,
    read_financial_table,
)
from tests import test_report_canonical_financials as canonical


@pytest.fixture
def database(tmp_path: Path, migrated_db: Callable[..., Path]) -> Generator[sqlite3.Connection]:
    factory = cast(
        Callable[..., Generator[sqlite3.Connection]], getattr(canonical.database, "__wrapped__")
    )
    yield from factory(tmp_path, migrated_db)


def admitted(database: sqlite3.Connection, *, eps: bool = False) -> FinancialEvidenceReference:
    canonical.seed_table(
        database,
        [
            (
                "eps_diluted" if eps else "revenue",
                "2025-10-01",
                "2025-12-31",
                "Q4",
                "2.675" if eps else "120000000",
                "USD/shares" if eps else "USD",
            )
        ],
    )
    cell = read_financial_table(database, "SYNTH", as_of=canonical.STAMP).cells[0]
    assert (
        cell.provenance
        and cell.metric_definition_revision_id
        and cell.canonical_resolution_revision_id
    )
    return FinancialEvidenceReference(
        ticker="SYNTH",
        concept=cell.concept,
        canonical_metric_cell_id=cell.canonical_metric_cell_id,
        observation_id=cell.provenance.observation.observation_id,
        canonical_resolution_revision_id=cell.canonical_resolution_revision_id,
        metric_definition_revision_id=cell.metric_definition_revision_id,
        as_of=canonical.STAMP,
    )


def source(database: sqlite3.Connection, node_id: str, text: str) -> None:
    locator = '{"path":"/reported-wording"}'
    database.execute(
        "INSERT INTO evidence_nodes VALUES(?,?,1,'run-1',NULL,NULL,'section',?,?,?,?)",
        (
            node_id,
            node_id,
            text,
            locator,
            hashlib.sha256(locator.encode()).hexdigest(),
            canonical.STAMP,
        ),
    )


def test_admitted_value_reconstructs_exact_narrative_number(database: sqlite3.Connection) -> None:
    value = MemoReportedValue(
        reference=admitted(database), displayed_value="120.0", display_format="number1"
    )
    verify_reported_memo_claim(database, "Revenue was $120.0 million.", values=(value,))
    with pytest.raises(MemoClaimSupportError, match="memo_reported_value_mismatch"):
        verify_reported_memo_claim(
            database,
            "Revenue was $999.0 million.",
            values=(value.model_copy(update={"displayed_value": "999.0"}),),
        )
    with pytest.raises(MemoClaimSupportError, match="memo_reported_value_not_in_passage"):
        verify_reported_memo_claim(database, "Revenue was $999.0 million.", values=(value,))


@pytest.mark.parametrize(
    "passage",
    [
        "Revenue was $120.0 million and earnings were $999.0 million.",
        "Revenue was $120.0 million in 2025.",
        "Revenue was $120.0 million, up 120.0%.",
        "Revenue was $120.0 million and again $120.0 million.",
        "Revenue was $1.20e2 million.",
    ],
)
def test_every_unbound_numeric_occurrence_fails_closed(
    database: sqlite3.Connection, passage: str
) -> None:
    value = MemoReportedValue(
        reference=admitted(database), displayed_value="120.0", display_format="number1"
    )
    with pytest.raises(MemoClaimSupportError):
        verify_reported_memo_claim(database, passage, values=(value,))


def test_explicit_scale_and_decimal_display_reconstruct(database: sqlite3.Connection) -> None:
    value = MemoReportedValue(
        reference=admitted(database),
        displayed_value="120000000.00",
        display_format="number2",
        scale=1000000,
    )
    verify_reported_memo_claim(database, "Revenue was $120000000.00.", values=(value,))
    with pytest.raises(MemoClaimSupportError, match="memo_reported_value_mismatch"):
        verify_reported_memo_claim(
            database, "Revenue was $120000000.00.", values=(value.model_copy(update={"scale": 1}),)
        )


def test_eps_uses_admitted_per_share_scale(database: sqlite3.Connection) -> None:
    value = MemoReportedValue(
        reference=admitted(database, eps=True), displayed_value="2.68", display_format="number2"
    )
    verify_reported_memo_claim(database, "Diluted EPS was $2.68.", values=(value,))


def test_financial_reader_display_reconstructs_existing_eps_rounding(
    database: sqlite3.Connection,
) -> None:
    value = MemoReportedValue(
        reference=admitted(database, eps=True),
        displayed_value="2.67",
        display_format="financial_reader",
    )
    verify_reported_memo_claim(database, "2.67", values=(value,))
    assert financial_reader_value(Decimal("-3"), "revenue") == "(3.0)"
    assert financial_reader_value(Decimal("-3"), "eps_diluted") == "-3.00"


def test_financial_reader_sign_and_scale_remain_exact(database: sqlite3.Connection) -> None:
    reference = admitted(database)
    value = MemoReportedValue(
        reference=reference, displayed_value="120.0", display_format="financial_reader"
    )
    with pytest.raises(MemoClaimSupportError, match="memo_reported_value_not_in_passage"):
        verify_reported_memo_claim(database, "(120.0)", values=(value,))
    with pytest.raises(MemoClaimSupportError, match="memo_reader_scale_mismatch"):
        verify_reported_memo_claim(
            database, "120.0", values=(value.model_copy(update={"scale": 1000000}),)
        )


def test_wrong_or_unadmitted_reference_cannot_support_number(database: sqlite3.Connection) -> None:
    reference = admitted(database).model_copy(update={"observation_id": "missing-observation"})
    value = MemoReportedValue(
        reference=reference, displayed_value="120.0", display_format="number1"
    )
    with pytest.raises(MemoClaimSupportError, match="memo_reported_value_not_admitted"):
        verify_reported_memo_claim(database, "Revenue was $120.0 million.", values=(value,))


def test_source_wording_requires_complete_exact_reconstruction(
    database: sqlite3.Connection,
) -> None:
    source(database, "first", "Management expects revenue to increase in 2027, subject to demand.")
    verify_reported_memo_claim(
        database, "Management expects revenue to increase in 2027,\n subject to demand.", ("first",)
    )
    for passage in (
        "Management expects revenue to increase in 2027.",
        "Management promised revenue will increase in 2027, subject to demand.",
        "Management expects revenue to increase in 2028, subject to demand.",
    ):
        with pytest.raises(MemoClaimSupportError, match="memo_reported_passage_not_reconstructed"):
            verify_reported_memo_claim(database, passage, ("first",))


def test_source_nodes_reconstruct_in_order_without_duplicate_members(
    database: sqlite3.Connection,
) -> None:
    source(database, "first", "Sales increased.")
    source(database, "second", "Margins remained under pressure.")
    verify_reported_memo_claim(
        database, "Sales increased. Margins remained under pressure.", ("first", "second")
    )
    with pytest.raises(MemoClaimSupportError, match="memo_reported_passage_not_reconstructed"):
        verify_reported_memo_claim(
            database, "Sales increased. Margins remained under pressure.", ("second", "first")
        )
    with pytest.raises(MemoClaimSupportError, match="memo_reported_node_membership_duplicate"):
        verify_reported_memo_claim(
            database, "Sales increased. Sales increased.", ("first", "first")
        )


def test_source_quote_cannot_hide_a_conflicting_declared_value(
    database: sqlite3.Connection,
) -> None:
    value = MemoReportedValue(
        reference=admitted(database), displayed_value="120.0", display_format="number1"
    )
    source(database, "quote", "Management reported revenue of $999.0 million.")
    with pytest.raises(MemoClaimSupportError, match="memo_reported_value_not_in_passage"):
        verify_reported_memo_claim(
            database, "Management reported revenue of $999.0 million.", ("quote",), (value,)
        )


def test_bare_source_attachment_and_empty_support_fail(database: sqlite3.Connection) -> None:
    with pytest.raises(MemoClaimSupportError, match="memo_reported_source_text_unavailable"):
        verify_reported_memo_claim(database, "An unsupported statement.", ("missing",))
    with pytest.raises(MemoClaimSupportError, match="memo_reported_claim_support_missing"):
        verify_reported_memo_claim(database, "An unsupported statement.")


def test_calculation_cannot_hide_a_false_reported_operand(database: sqlite3.Connection) -> None:
    value = MemoReportedValue(
        reference=admitted(database), displayed_value="120.0", display_format="number1"
    )
    verify_reported_memo_claim(
        database,
        "Revenue was 120.0 and the calculated result was 0.0.",
        values=(value,),
        calculated_values=("0.0",),
    )
    with pytest.raises(MemoClaimSupportError, match="memo_reported_value_mismatch"):
        verify_reported_memo_claim(
            database,
            "Revenue was 999.0 and the calculated result was 0.0.",
            values=(value.model_copy(update={"displayed_value": "999.0"}),),
            calculated_values=("0.0",),
        )


def test_native_series_value_cannot_use_memo_table_display_contract(
    database: sqlite3.Connection,
) -> None:
    reference = admitted(database).model_copy(
        update={
            "reader_kind": "series",
            "cadence": FinancialCadence.QUARTERLY,
            "continuity": SeriesContinuity.WINDOWED,
        }
    )
    point = read_financial_evidence(database, reference)
    assert isinstance(point, FinancialConsumerPoint)
    assert point.observation.value == Decimal("120000000")
    value = MemoReportedValue(
        reference=reference, displayed_value="120.0", display_format="number1"
    )
    with pytest.raises(MemoClaimSupportError, match="memo_financial_series_reference_unsupported"):
        verify_reported_memo_claim(database, "Revenue was $120.0 million.", values=(value,))
