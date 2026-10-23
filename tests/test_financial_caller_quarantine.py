"""Financial decision callers use the shared resolved source guard."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import cast

from compute import fmp_derived_kpis as derived
from compute.metrics_engine import io as metrics_io
from compute.metrics_engine.inputs import CanonicalConcept
from models.companies import AccountingStandard
from provenance.financial_fact_resolution import canonical_fact_relation, resolve_fact_row
from provenance.semantic_disposition import SemanticDisposition, SemanticDispositionStore
from report.metrics_view import metrics_rows, ratios_rows
from report.sections import financials
from tests import test_financial_fact_resolution_cutover as cutover
from tests import test_financial_source_semantic_quarantine as quarantine
from tests.test_source_fact_repository import STAMP
from timeseries import loaders

_seed_document = cast(Callable[..., None], getattr(cutover, "_seed_document"))
_insert_financial_fact = cast(Callable[..., int], getattr(cutover, "_insert_financial_fact"))
_disposition = cast(Callable[..., SemanticDisposition], getattr(quarantine, "_disposition"))
_load_quarterly = cast(
    Callable[..., list[dict[str, object]]], getattr(financials, "_load_quarterly")
)
_fetch_quarterly_facts = cast(
    Callable[..., tuple[list[derived.QuarterlyFacts], list[derived.FmpDerivationReason]]],
    getattr(derived, "_fetch_quarterly_facts"),
)
_build_quarter_cells = cast(
    Callable[..., list[metrics_io.QuarterCell]], getattr(metrics_io, "_build_quarter_cells")
)
_financial_series_with_provenance = cast(
    Callable[..., list[loaders.SourcedObservation]],
    getattr(loaders, "_financial_series_with_provenance"),
)


def _resolved_quarter(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO issuer_entities VALUES (?,?,?,?)",
        ("issuer-acme", "issuer:acme", "operating_company", STAMP),
    )
    _seed_document(conn, document_id=1, source_tier="sec_official", value_suffix="SEC")
    fact_ids = [_insert_financial_fact(conn, document_id=1, value="100")]
    for line_item, value in (
        ("operating_income", "30"),
        ("net_income", "20"),
        ("gross_profit", "50"),
    ):
        row = conn.execute(
            "INSERT INTO financial_facts "
            "(ticker, period_end, fiscal_period_type, line_item, value, currency, unit, "
            "source_doc_id, confidence, extracted_by, locator) "
            "VALUES ('ACME', '2026-06-30', 'Q2', ?, ?, 'USD', 'actual', 1, 0.99, "
            "'deterministic:test', ?)",
            (line_item, value, f'{{"json_path":"[0].{line_item}"}}'),
        )
        assert row.lastrowid is not None
        fact_ids.append(int(row.lastrowid))
    cutoff = datetime.now(UTC) + timedelta(minutes=1)
    for fact_id in fact_ids:
        assert (
            resolve_fact_row(
                conn,
                fact_table="financial_facts",
                fact_row_id=fact_id,
                knowledge_cutoff=cutoff,
            )
            is not None
        )


def test_financial_callers_exclude_explicitly_quarantined_source(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    path = migrated_db(tmp_path / "caller-quarantine.db")
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        _resolved_quarter(conn)

        def current_values() -> tuple[
            list[object],
            list[float],
            float | None,
            list[Decimal],
            list[dict[CanonicalConcept, Decimal]],
            list[object],
            list[object],
        ]:
            report_rows = _load_quarterly(conn, "ACME")
            sourced = _financial_series_with_provenance(
                conn, ticker="ACME", line_item="revenue", period_types=("Q2",)
            )
            provenance = loaders.load_financial_fact_provenance(
                "ACME", "revenue", conn=conn, period_types=("Q2",)
            )
            derived, _ = _fetch_quarterly_facts(conn, "ACME")
            cells = _build_quarter_cells(conn, "ACME", AccountingStandard.US_GAAP)
            metric_rows = metrics_rows(conn, "ACME")
            ratio_rows = ratios_rows(conn, "ACME")
            return (
                [row["revenue"] for row in report_rows],
                [row.value for row in sourced],
                None if provenance is None else cast(float, provenance["value"]),
                [row.revenue for row in derived],
                [cell.values for cell in cells],
                [row["revenue"] for row in metric_rows],
                [row["revenue"] for row in ratio_rows],
            )

        before = current_values()
        assert before[0] == [100]
        assert before[1] == [100.0]
        assert before[2] == 100.0
        assert len(before[3]) == 1
        assert len(before[4]) == 1
        assert before[5] == [100]
        assert before[6] == [100]

        at = datetime.now(UTC) + timedelta(hours=1)
        SemanticDispositionStore(conn).persist(
            _disposition(
                "document-version-1",
                revision=1,
                status="quarantined",
                knowledge_at=at,
                recorded_at=at,
            )
        )
        after = current_values()
        assert after[0] == []
        assert after[1] == []
        assert after[2] is None
        assert after[3] == []
        assert len(after[4]) == 1 and after[4][0] == {}
        assert after[5] == []
        assert after[6] == []
        # The old global view remains a raw compatibility surface.
        assert conn.execute("SELECT revenue FROM metrics WHERE ticker='ACME'").fetchone()[0] == 100
    finally:
        conn.close()


def test_metrics_helper_uses_canonical_winner_before_quarantine(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    path = migrated_db(tmp_path / "metrics-canonical-winner.db")
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        _resolved_quarter(conn)
        _seed_document(conn, document_id=2, source_tier="fmp_normalized", value_suffix="FMP")
        later_raw_id = _insert_financial_fact(conn, document_id=2, value="200")
        assert (
            resolve_fact_row(
                conn,
                fact_table="financial_facts",
                fact_row_id=later_raw_id,
                knowledge_cutoff=datetime.now(UTC) + timedelta(minutes=1),
            )
            is not None
        )
        fact_relation = canonical_fact_relation(conn, "financial_facts").sql
        selected = conn.execute(
            f"SELECT value FROM {fact_relation} WHERE ticker='ACME' AND line_item='revenue'"
        ).fetchone()
        assert selected is not None and selected[0] == 100
        assert metrics_rows(conn, "ACME")[0]["revenue"] == selected[0]
        assert conn.execute("SELECT revenue FROM metrics WHERE ticker='ACME'").fetchone()[0] == 200

        at = datetime.now(UTC) + timedelta(hours=1)
        SemanticDispositionStore(conn).persist(
            _disposition(
                "document-version-1",
                revision=1,
                status="quarantined",
                knowledge_at=at,
                recorded_at=at,
            )
        )
        assert metrics_rows(conn, "ACME") == []
    finally:
        conn.close()
