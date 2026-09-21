"""Current valuation consumes canonical LTM denominators and separate market evidence."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from compute.valuation_basis import extract_for_ticker, load
from provenance.metric_ontology import MetricOntology
from report.render_clock import fixed_render_clock
from sources.discovery_financials import (
    CanonicalFinancialHistory,
    GrowthFactReference,
    read_financial_history,
)
from sources.discovery_market import DiscoveryMarketContext
from sources.valuation_inputs import ValuationInputs, read_valuation_inputs
from tests.test_canonical_growth_screen import STAMP, seed_growth_graph
from tests.test_discovery_financial_inputs import seed_market_context


def seed_ltm(
    conn: sqlite3.Connection, root: Path, multiple: str, *, canonical: bool = True
) -> None:
    seed_growth_graph(
        conn, root, include_screen_financials=canonical, include_peer_financials=canonical
    )
    seed_market_context(conn, root / "data/historical/fmp")
    conn.execute("UPDATE tracked_companies SET list_type='portfolio' WHERE ticker='WIX'")
    conn.commit()
    path = root / "micro_thesis/holdings/WIX.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"valuation_multiple_override": multiple, "thesis": "Fixture"}))
    raw = root / "data/historical/fmp/WIX_key_metrics_quarterly.json"
    raw.write_text('[{"date":"2026-03-31","peRatio":999,"priceToFreeCashFlowsRatio":888}]')


@pytest.mark.parametrize("multiple,expected", [("P/E (LTM)", 500 / 60), ("P/FCF", 12.5)])
def test_actual_canonical_current_ratio_does_not_reuse_provider_ratio(
    tmp_path: Path, migrated_db: Callable[..., Path], multiple: str, expected: float
) -> None:
    with (
        sqlite3.connect(migrated_db(tmp_path / "fixture.db")) as conn,
        fixed_render_clock(STAMP.date()),
    ):
        seed_ltm(conn, tmp_path, multiple)
        result = extract_for_ticker("WIX", tmp_path, conn)
        assert result.current_value == pytest.approx(expected)
        assert result.current_basis == "canonical_reported_ltm"
        assert result.historical_median is None and result.rich_cheap_verdict is None
        assert result.source_context["decision_grade"] is False
        assert result.source_context["acquisition_completeness"] == "unverified"
        facts = CanonicalFinancialHistory.model_validate(
            result.source_context["canonical_financials"]
        )
        assert facts.as_of == STAMP.date() and facts.ticker == "WIX"
        concept = "net_income" if multiple == "P/E (LTM)" else "free_cash_flow"
        refs = [item for item in facts.references if item.concept == concept]
        assert len(refs) == 4
        assert len({item.reporting_entity_id for item in refs}) == 1
        assert all(
            item.currency == "USD"
            and item.canonical_resolution_revision_id
            and item.observation_payload_sha256
            for item in refs
        )
        market = DiscoveryMarketContext.model_validate(result.source_context["market"])
        assert market.ticker == "WIX" and market.name == "Synthetic"
        assert market.captured_at == STAMP and market.freshness_status == "fresh"
        assert market.document_version_id and market.source_payload_sha256
        assert result.current_method == f"captured_market_cap / sum_4_contiguous_quarter_{concept}"
        assert result.current_period_end == STAMP.date().isoformat()


def test_missing_canonical_financials_never_fall_back_to_raw_ratios(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    with (
        sqlite3.connect(migrated_db(tmp_path / "fixture.db")) as conn,
        fixed_render_clock(STAMP.date()),
    ):
        seed_ltm(conn, tmp_path, "P/E (LTM)", canonical=False)
        result = extract_for_ticker("WIX", tmp_path, conn)
        assert result.current_value is None
        assert result.current_unavailable_reason
        assert result.history[0].value == 999
        assert result.history[0].basis == "provider_reported_ratio_unmigrated"
        assert result.rich_cheap_verdict is None


def test_supported_owner_ratio_does_not_require_legacy_keymetrics(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    with (
        sqlite3.connect(migrated_db(tmp_path / "fixture.db")) as conn,
        fixed_render_clock(STAMP.date()),
    ):
        seed_ltm(conn, tmp_path, "P/FCF")
        (tmp_path / "data/historical/fmp/WIX_key_metrics_quarterly.json").unlink()
        result = extract_for_ticker("WIX", tmp_path, conn)
        assert result.current_value == 12.5
        assert result.history == []


def test_database_only_semantic_revision_invalidates_current_ratio_cache(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    with (
        sqlite3.connect(migrated_db(tmp_path / "fixture.db")) as conn,
        fixed_render_clock(STAMP.date()),
    ):
        seed_ltm(conn, tmp_path, "P/E (LTM)")
        before = extract_for_ticker("WIX", tmp_path, conn)
        paths = sorted((tmp_path / "data/historical/fmp").glob("*.json"))
        raw_before = [path.read_bytes() for path in paths]
        history = read_financial_history(conn, "WIX", as_of=STAMP.date(), concepts=("net_income",))
        ontology = MetricOntology(conn)
        definition = ontology.metric_definition_as_known(history.references[0].metric_id, STAMP)
        assert definition is not None
        later = STAMP + timedelta(hours=1)
        ontology.persist_metric_definition(
            definition.model_copy(
                update={
                    "revision": 2,
                    "metric_definition_revision_id": "changed-net-definition",
                    "idempotency_key": "changed-net-definition",
                    "supersedes_metric_definition_revision_id": definition.metric_definition_revision_id,
                    "definition_text": "Different scope without compatibility proof",
                    "effective_at": later,
                    "knowledge_at": later,
                    "recorded_at": later,
                }
            )
        )
        cached = load(tmp_path, "WIX", conn=conn)
        assert cached is not None
        assert cached.skipped_reason == "valuation_cache_rebuild_required_input_identity_changed"
        after = extract_for_ticker("WIX", tmp_path, conn)
        assert before.current_value is not None and after.current_value is None
        assert before.cache_sha256 != after.cache_sha256
        assert [path.read_bytes() for path in paths] == raw_before


def test_invalid_canonical_windows_and_market_context_fail_closed(
    tmp_path: Path, migrated_db: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    with (
        sqlite3.connect(migrated_db(tmp_path / "fixture.db")) as conn,
        fixed_render_clock(STAMP.date()),
    ):
        seed_ltm(conn, tmp_path, "P/FCF")
        admitted = read_valuation_inputs(tmp_path, "WIX", as_of=STAMP.date(), conn=conn)
        history = admitted.canonical_financials
        cash = max(
            (item for item in history.references if item.concept == "free_cash_flow"),
            key=lambda item: item.period_end,
        )
        changes: tuple[dict[str, object], ...] = (
            {"metric_id": "different-definition"},
            {"reporting_entity_id": "different-entity"},
            {"currency": "DKK"},
            {"period_start": cash.period_start + timedelta(days=1)},
            {"period_start": cash.period_start - timedelta(days=1)},
            {"value": Decimal(-100)},
        )
        invalid_inputs = [
            replace(
                admitted,
                canonical_financials=history.model_copy(
                    update={
                        "references": tuple(
                            item.model_copy(update=update) if item == cash else item
                            for item in history.references
                        )
                    }
                ),
            )
            for update in changes
        ]
        invalid_inputs.extend(
            (
                replace(admitted, market=admitted.market.model_copy(update={"currency": "DKK"})),
                replace(
                    admitted,
                    market=admitted.market.model_copy(
                        update={"status": "unavailable", "freshness_status": "stale"}
                    ),
                ),
            )
        )
        for selected in invalid_inputs:

            def read_selected(
                root: Path, ticker: str, *, as_of: date, conn: sqlite3.Connection | None
            ) -> ValuationInputs:
                return selected

            monkeypatch.setattr("compute.valuation_basis.read_valuation_inputs", read_selected)
            (tmp_path / "data/valuation_basis/WIX.json").unlink(missing_ok=True)
            result = extract_for_ticker("WIX", tmp_path, conn)
            assert result.current_value is None
            assert result.current_unavailable_reason
            assert result.historical_median is None


@pytest.mark.parametrize("day_offset", [-1, 30])
def test_actual_market_capture_rejects_future_or_stale_current_ratio(
    tmp_path: Path, migrated_db: Callable[..., Path], day_offset: int
) -> None:
    with sqlite3.connect(migrated_db(tmp_path / "fixture.db")) as conn:
        seed_ltm(conn, tmp_path, "P/FCF")
        with fixed_render_clock(STAMP.date() + timedelta(days=day_offset)):
            result = extract_for_ticker("WIX", tmp_path, conn)
        assert result.current_value is None
        assert result.current_unavailable_reason
        assert result.source_context["decision_grade"] is False


@pytest.mark.parametrize(
    "durations,available",
    [((71, 71, 71, 71), False), ((90, 91, 92, 92), True), ((91, 91, 91, 98), True)],
)
def test_ltm_requires_annual_coverage_including_fiscal_53_week_year(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    durations: tuple[int, int, int, int],
    available: bool,
) -> None:
    with (
        sqlite3.connect(migrated_db(tmp_path / "fixture.db")) as conn,
        fixed_render_clock(STAMP.date()),
    ):
        seed_ltm(conn, tmp_path, "P/E (LTM)")
        admitted = read_valuation_inputs(tmp_path, "WIX", as_of=STAMP.date(), conn=conn)
        refs = sorted(
            (
                item
                for item in admitted.canonical_financials.references
                if item.concept == "net_income"
            ),
            key=lambda item: item.period_end,
        )
        start = date(2026, 3, 31) - timedelta(days=sum(durations) - 1)
        replacements: list[GrowthFactReference] = []
        for item, duration in zip(refs, durations, strict=True):
            end = start + timedelta(days=duration - 1)
            replacements.append(item.model_copy(update={"period_start": start, "period_end": end}))
            start = end + timedelta(days=1)
        inputs = replace(
            admitted,
            canonical_financials=admitted.canonical_financials.model_copy(
                update={"references": tuple(replacements)}
            ),
        )

        def read_selected(
            root: Path, ticker: str, *, as_of: date, conn: sqlite3.Connection | None
        ) -> ValuationInputs:
            return inputs

        monkeypatch.setattr("compute.valuation_basis.read_valuation_inputs", read_selected)
        result = extract_for_ticker("WIX", tmp_path, conn)
        if available:
            assert result.current_value == pytest.approx(500 / 60)
            assert result.current_unavailable_reason is None
        else:
            assert result.current_value is None
            assert result.current_unavailable_reason == "four_quarter_duration_not_annual"
