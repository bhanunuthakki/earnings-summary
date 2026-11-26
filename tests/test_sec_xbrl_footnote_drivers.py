"""Tests for MBI-driven SEC CompanyFacts footnote & dilution TAG_LADDERS (Item 2A)."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from pipeline import sec_xbrl
from pipeline.restatement_detector import insert_with_restatement_detection
from pipeline.sec_xbrl import (
    TAG_LADDERS,
    FetchedCompanyFacts,
    ingest_for_ticker,
    select_facts_from_companyfacts,
)
from provenance.issuer_registry import (
    IdentifierAssertion,
    IdentifierResolution,
    IssuerEntity,
    IssuerRegistry,
    identifier_candidate_digest,
)

ACC_10K = "0001326801-25-000010"
CIK_META = "0001326801"
STAMP = datetime(2026, 2, 1, 12, 0, tzinfo=UTC)


def _instant_entry(
    end: str, val: float, *, fp: str = "FY", form: str = "10-K"
) -> dict[str, object]:
    return {
        "end": end,
        "val": val,
        "accn": ACC_10K,
        "fy": 2025,
        "fp": fp,
        "form": form,
        "filed": "2026-01-30",
    }


def _duration_entry(
    start: str, end: str, val: float, *, fp: str = "FY", form: str = "10-K"
) -> dict[str, object]:
    return {
        "start": start,
        "end": end,
        "val": val,
        "accn": ACC_10K,
        "fy": 2025,
        "fp": fp,
        "form": form,
        "filed": "2026-01-30",
    }


def _concept(unit_key: str, entries: list[dict[str, object]]) -> dict[str, object]:
    return {"label": "Concept", "description": "Test concept", "units": {unit_key: entries}}


def _sample_companyfacts_payload() -> dict[str, object]:
    return {
        "cik": 1326801,
        "entityName": "Meta Platforms, Inc.",
        "facts": {
            "us-gaap": {
                "ConstructionInProgressGross": _concept(
                    "USD", [_instant_entry("2025-12-31", 90_000_000_000)]
                ),
                "UnconditionalPurchaseObligation": _concept(
                    "USD", [_instant_entry("2025-12-31", 121_000_000_000)]
                ),
                "PurchaseObligation": _concept(
                    # Lower-priority rung: must lose to UnconditionalPurchaseObligation.
                    "USD",
                    [_instant_entry("2025-12-31", 999_000_000)],
                ),
                "PurchaseObligationDueInNextTwelveMonths": _concept(
                    "USD", [_instant_entry("2025-12-31", 32_000_000_000)]
                ),
                "RevenueRemainingPerformanceObligation": _concept(
                    "USD", [_instant_entry("2025-12-31", 45_000_000_000)]
                ),
                "EmployeeServiceShareBasedCompensationNonvestedAwardsTotalCompensationCostNotYetRecognized": _concept(
                    "USD", [_instant_entry("2025-12-31", 28_500_000_000)]
                ),
                "ShareBasedCompensationArrangementByShareBasedPaymentAwardEquityInstrumentsOtherThanOptionsNonvestedNumber": _concept(
                    "shares", [_instant_entry("2025-12-31", 110_000_000)]
                ),
                "ShareBasedCompensationArrangementByShareBasedPaymentAwardEquityInstrumentsOtherThanOptionsGrantsInPeriod": _concept(
                    "shares", [_duration_entry("2025-01-01", "2025-12-31", 34_000_000)]
                ),
                "ShareBasedCompensationArrangementByShareBasedPaymentAwardEquityInstrumentsOtherThanOptionsVestedInPeriod": _concept(
                    "shares", [_duration_entry("2025-01-01", "2025-12-31", 29_000_000)]
                ),
                "PaymentsRelatedToTaxWithholdingForShareBasedCompensation": _concept(
                    "USD", [_duration_entry("2025-01-01", "2025-12-31", 7_200_000_000)]
                ),
            },
            "dei": {
                "EntityCommonStockSharesOutstanding": _concept(
                    "shares", [_instant_entry("2025-12-31", 2_520_000_000)]
                )
            },
        },
    }


def _seed_meta_issuer(conn: sqlite3.Connection) -> None:
    registry = IssuerRegistry(conn)
    registry.persist(
        IssuerEntity(
            issuer_id="issuer-meta",
            idempotency_key="issuer-meta",
            entity_kind="operating_company",
            created_at=STAMP,
        )
    )
    assertion = IdentifierAssertion(
        assertion_id=f"assertion-{CIK_META}",
        idempotency_key=f"assertion-{CIK_META}",
        issuer_id="issuer-meta",
        identifier_type="sec_cik",
        identifier_value=CIK_META,
        normalized_value=CIK_META,
        authority="manual",
        source_observation_id=None,
        effective_at=STAMP,
        knowledge_at=STAMP,
        recorded_at=STAMP,
    )
    registry.persist(assertion)
    registry.persist(
        IdentifierResolution(
            resolution_id=f"resolution-{CIK_META}",
            idempotency_key=f"resolution-{CIK_META}",
            resolution_key=f"sec_cik:{CIK_META}",
            revision=1,
            outcome="selected",
            selected_assertion_id=assertion.assertion_id,
            candidate_digest_sha256=identifier_candidate_digest((assertion,)),
            policy_name="test",
            policy_version="1",
            policy_config_sha256=hashlib.sha256(b"test-policy").hexdigest(),
            material_dissent=False,
            reason_code="manual_test_identity",
            reason_details=(("source", "test"),),
            effective_at=STAMP,
            knowledge_at=STAMP,
            recorded_at=STAMP,
            supersedes_resolution_id=None,
        )
    )
    conn.execute(
        "INSERT INTO tracked_companies (ticker, name, list_type) VALUES ('META', 'Meta', 'evaluation')"
    )
    conn.commit()


def test_mbi_footnote_and_dilution_ladders_registered_with_expected_kinds_and_signs() -> None:
    by_item = {ladder.line_item: ladder for ladder in TAG_LADDERS}
    expected_items = {
        "construction_in_progress": ("monetary", 1),
        "unconditional_purchase_obligations": ("monetary", 1),
        "purchase_obligations_due_next_12m": ("monetary", 1),
        "remaining_performance_obligations": ("monetary", 1),
        "unrecognized_sbc_expense": ("monetary", 1),
        "unvested_rsu_shares": ("shares", 1),
        "rsu_granted_shares": ("shares", 1),
        "rsu_vested_shares": ("shares", 1),
        "cash_taxes_paid_rsu_withholding": ("monetary", -1),
        "common_shares_outstanding": ("shares", 1),
    }
    for name, (kind, sign) in expected_items.items():
        assert name in by_item
        assert by_item[name].kind == kind
        assert by_item[name].sign == sign


def test_select_and_persist_all_ten_mbi_driver_ladders(
    migrated_db: Callable[..., Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _sample_companyfacts_payload()
    selection = select_facts_from_companyfacts(
        payload, ticker="META", supported_accessions={ACC_10K}
    )
    selected_by_item = {ladder.line_item: facts for ladder, facts in selection.ladders}

    # Instant FYE items dual-write FY and Q4; unconditional_purchase_obligations picks rung 0.
    po_fy = [
        f
        for f in selected_by_item["unconditional_purchase_obligations"]
        if f.fiscal_period_type == "FY"
    ]
    assert len(po_fy) == 1
    assert po_fy[0].value == Decimal("121000000000")
    assert len(selected_by_item["unconditional_purchase_obligations"]) == 2  # FY + Q4

    # Outflow withholding carries sign=-1.
    withhold = selected_by_item["cash_taxes_paid_rsu_withholding"]
    assert len(withhold) == 1
    assert withhold[0].value == Decimal("-7200000000")

    # dei fallback works for common_shares_outstanding with COUNT unit and None currency.
    shares = [
        f for f in selected_by_item["common_shares_outstanding"] if f.fiscal_period_type == "FY"
    ]
    assert len(shares) == 1
    assert shares[0].value == Decimal("2520000000")
    assert shares[0].unit == "count"
    assert shares[0].currency is None

    db_path = migrated_db(tmp_path / "sec_xbrl_drivers.db")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        _seed_meta_issuer(conn)
        monkeypatch.setitem(sec_xbrl.CIK_MAP, "META", CIK_META)
        body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        stats = ingest_for_ticker(
            conn,
            ticker="META",
            project_root=tmp_path,
            local_payload=FetchedCompanyFacts(
                source_url=f"https://data.sec.gov/api/xbrl/companyfacts/CIK{CIK_META}.json",
                raw_body=body,
                observed_at=STAMP - timedelta(seconds=1),
                retrieved_at=STAMP,
            ),
        )
        # 7 instant items * 2 (FY+Q4) + 3 duration items * 1 (FY) = 17 rows.
        assert stats.facts_inserted == 17

        rows = conn.execute(
            "SELECT line_item, fiscal_period_type, value, unit, currency, locator "
            "FROM financial_facts WHERE ticker = 'META' AND fiscal_period_type = 'FY'"
        ).fetchall()
        by_line = {str(r["line_item"]): r for r in rows}
        assert Decimal(str(by_line["construction_in_progress"]["value"])) == Decimal("90000000000")
        assert Decimal(str(by_line["remaining_performance_obligations"]["value"])) == Decimal(
            "45000000000"
        )
        assert Decimal(str(by_line["unrecognized_sbc_expense"]["value"])) == Decimal("28500000000")
        assert Decimal(str(by_line["rsu_granted_shares"]["value"])) == Decimal("34000000")
        assert by_line["rsu_granted_shares"]["unit"] == "count"
        assert by_line["rsu_granted_shares"]["currency"] is None

        locator = json.loads(str(by_line["common_shares_outstanding"]["locator"]))
        assert locator["table_cell"]["table_title"] == "dei"
        assert locator["table_cell"]["row_label"] == "EntityCommonStockSharesOutstanding"
        assert (
            locator["json_path"] == "facts.dei.EntityCommonStockSharesOutstanding.units.shares[0]"
        )
    finally:
        conn.close()


def test_negative_share_count_rejected_by_plausibility_guard(
    migrated_db: Callable[..., Path], tmp_path: Path
) -> None:
    db_path = migrated_db(tmp_path / "neg_shares.db")
    conn = sqlite3.connect(db_path)
    try:
        with pytest.raises(ValueError, match="Negative share count implausible"):
            insert_with_restatement_detection(
                conn,
                ticker="META",
                period_end=datetime(2025, 12, 31),
                fiscal_period_type="FY",
                line_item="unvested_rsu_shares",
                value=Decimal("-5"),
                currency=None,
                unit="count",
                source_doc_id=1,
                extracted_by="sec_xbrl",
            )
    finally:
        conn.close()
