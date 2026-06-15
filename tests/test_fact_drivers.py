"""Unit coverage for the DIY-fact → DCF-driver bridge (capture-every-number S6).

The load-bearing piece is :func:`dcf.fact_drivers.convert_to_driver` — the
units/scale converter that turns a stored fact (percent 4.3, $ actual, bps) into
the Dashboard's expected unit (decimal ratio, $M, turns) and REFUSES anything it
can't scale safely. These tests pin every conversion + every refusal, plus the
override-aware value resolution (S2), the input-application, and the durable
fact-lineage provenance write.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from dcf import fact_drivers, redesign
from dcf.fact_drivers import (
    DRIVER_FIELDS_BY_KEY,
    FactDriverError,
    apply_to_inputs,
    convert_to_driver,
    record_driver_provenance,
    resolve_fact_value,
)
from provenance import overrides
from provenance.overrides import OverrideAction
from viewspec.spec import MetricRef

# --------------------------------------------------------------------------- #
# Field registry
# --------------------------------------------------------------------------- #


def test_wacc_is_not_an_injectable_field() -> None:
    """WACC has no input cell (derived from rf/ERP/beta/Kd) — it must never be a
    directly-writable driver (a guardrail, not an oversight)."""
    assert "wacc" not in DRIVER_FIELDS_BY_KEY
    keys = {f.key for f in fact_drivers.DRIVER_FIELDS}
    # The CAPM levers a "set WACC" fact should target instead ARE present.
    assert {"risk_free_rate", "equity_risk_premium", "beta", "cost_of_debt"} <= keys


def test_field_options_are_unique_and_labeled() -> None:
    opts = fact_drivers.driver_field_options()
    assert len(opts) == len(fact_drivers.DRIVER_FIELDS)
    assert len({o["key"] for o in opts}) == len(opts)
    assert all(o["label"] for o in opts)


# --------------------------------------------------------------------------- #
# Converter — proportions (the 4.3-vs-0.043 trap)
# --------------------------------------------------------------------------- #


def test_percent_scales_to_decimal_ratio() -> None:
    """A margin stored as percent 42.0 must become the ratio 0.42 the Dashboard
    expects — NOT 42.0 (the headline bug the whole converter exists to prevent)."""
    field = DRIVER_FIELDS_BY_KEY["near_op_margin"]
    out = convert_to_driver(42.0, "percent", field)
    assert out.value == pytest.approx(0.42)
    assert out.target_unit == "ratio"


def test_ratio_source_is_identity() -> None:
    field = DRIVER_FIELDS_BY_KEY["near_op_margin"]
    assert convert_to_driver(0.42, "ratio", field).value == pytest.approx(0.42)


def test_bps_scales_to_ratio() -> None:
    """A spread/rate stored in bps: 430 bps → 0.043."""
    field = DRIVER_FIELDS_BY_KEY["risk_free_rate"]
    assert convert_to_driver(430.0, "bps", field).value == pytest.approx(0.043)


def test_absent_unit_rejected_for_proportion() -> None:
    """Without a stored unit we cannot tell 4.3 from 0.043 — refuse, don't guess."""
    field = DRIVER_FIELDS_BY_KEY["near_op_margin"]
    with pytest.raises(FactDriverError, match="no stored unit"):
        convert_to_driver(4.3, None, field)


def test_dollar_unit_rejected_for_a_margin() -> None:
    """A $ level offered to a margin field is a category error — cross-family
    conversion is refused, never silently rescaled."""
    field = DRIVER_FIELDS_BY_KEY["near_op_margin"]
    with pytest.raises(FactDriverError, match="incompatible units"):
        convert_to_driver(1000.0, "millions", field)


# --------------------------------------------------------------------------- #
# Converter — money ($M)
# --------------------------------------------------------------------------- #


def test_actual_dollars_scale_to_millions() -> None:
    """Capex stored in raw dollars ($1.5B) → 1500 ($M)."""
    field = DRIVER_FIELDS_BY_KEY["capex_2026_m"]
    out = convert_to_driver(1_500_000_000.0, "actual", field)
    assert out.value == pytest.approx(1500.0)
    assert out.target_unit == "millions"


def test_billions_scale_to_millions() -> None:
    field = DRIVER_FIELDS_BY_KEY["capex_2026_m"]
    assert convert_to_driver(1.5, "billions", field).value == pytest.approx(1500.0)


def test_percent_rejected_for_money_field() -> None:
    field = DRIVER_FIELDS_BY_KEY["capex_2026_m"]
    with pytest.raises(FactDriverError, match="incompatible units"):
        convert_to_driver(12.0, "percent", field)


# --------------------------------------------------------------------------- #
# Converter — RAW (beta, exit multiple in turns)
# --------------------------------------------------------------------------- #


def test_raw_beta_taken_as_is() -> None:
    field = DRIVER_FIELDS_BY_KEY["beta"]
    out = convert_to_driver(1.15, "ratio", field)
    assert out.value == pytest.approx(1.15)
    assert out.target_unit is None


def test_raw_exit_multiple_from_ratio() -> None:
    field = DRIVER_FIELDS_BY_KEY["exit_multiple"]
    assert convert_to_driver(12.5, "ratio", field).value == pytest.approx(12.5)


def test_raw_rejects_a_percent_rate() -> None:
    """A percent fact is a RATE — it belongs in a rate field, not used raw as a
    turn-count/beta (which would be a 100× error)."""
    field = DRIVER_FIELDS_BY_KEY["exit_multiple"]
    with pytest.raises(FactDriverError, match="rate"):
        convert_to_driver(12.5, "percent", field)


# --------------------------------------------------------------------------- #
# Converter — sanity bounds + non-finite
# --------------------------------------------------------------------------- #


def test_out_of_bounds_value_rejected() -> None:
    """A 'margin' that converts to 4.2 (e.g. a 420% mis-typed fact) is degenerate
    — rejected before it can reach the model."""
    field = DRIVER_FIELDS_BY_KEY["near_op_margin"]
    with pytest.raises(FactDriverError, match="outside the safe range"):
        convert_to_driver(420.0, "percent", field)  # → 4.2 ratio, above 0.95


def test_negative_margin_within_bounds_is_allowed() -> None:
    """A real loss-making margin (-30%) is a legitimate driver, not degenerate."""
    field = DRIVER_FIELDS_BY_KEY["near_op_margin"]
    assert convert_to_driver(-30.0, "percent", field).value == pytest.approx(-0.3)


def test_non_finite_rejected() -> None:
    field = DRIVER_FIELDS_BY_KEY["near_op_margin"]
    with pytest.raises(FactDriverError, match="not a finite number"):
        convert_to_driver(float("nan"), "ratio", field)


def test_exit_multiple_out_of_bounds_rejected() -> None:
    field = DRIVER_FIELDS_BY_KEY["exit_multiple"]
    with pytest.raises(FactDriverError, match="outside the safe range"):
        convert_to_driver(0.42, "ratio", field)  # a margin mis-mapped to a multiple


# --------------------------------------------------------------------------- #
# apply_to_inputs — frozen in, frozen out
# --------------------------------------------------------------------------- #
_BASE = redesign.RedesignInputs(
    segments=("Cloud", "Devices"),
    base_revenue_by_segment={"Cloud": 600.0, "Devices": 400.0},
    near_growth_by_segment={"Cloud": 0.10, "Devices": 0.05},
    terminal_growth_by_segment={"Cloud": 0.03, "Devices": 0.02},
    near_op_margin=0.20,
    terminal_op_margin=0.25,
    tax_rate=0.24,
    capex_2026_m=60.0,
    terminal_capex_da=1.05,
    da_ratio=0.05,
    consensus_years=5,
    wacc=0.09,
    beta=1.2,
    risk_free_rate=0.043,
    equity_risk_premium=0.045,
    cost_of_debt=0.045,
    terminal_method="Exit multiple",
    terminal_basis="EV/EBITDA",
    exit_multiple=12.0,
    terminal_growth_g=0.03,
    current_price=50.0,
    cash_m=100.0,
    total_debt_m=200.0,
    diluted_shares_m=100.0,
    fx_to_usd=1.0,
)


def test_apply_scalar_replaces_only_that_field() -> None:
    field = DRIVER_FIELDS_BY_KEY["near_op_margin"]
    out = apply_to_inputs(_BASE, field, 0.30)
    assert out.near_op_margin == pytest.approx(0.30)
    assert out.terminal_op_margin == pytest.approx(0.25)  # untouched
    assert _BASE.near_op_margin == pytest.approx(0.20)  # original frozen, unchanged


def test_apply_segment_growth_sets_every_segment() -> None:
    field = DRIVER_FIELDS_BY_KEY["segment_growth_near"]
    out = apply_to_inputs(_BASE, field, 0.18)
    assert out.near_growth_by_segment == {"Cloud": 0.18, "Devices": 0.18}
    assert out.terminal_growth_by_segment == _BASE.terminal_growth_by_segment  # untouched


# --------------------------------------------------------------------------- #
# Value resolution — latest value, honoring fact_overrides (S2)
# --------------------------------------------------------------------------- #
_KPI = "Operating margin"

_OVERRIDES_DDL = """
CREATE TABLE fact_overrides (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL DEFAULT 'bhanu',
    ticker TEXT NOT NULL,
    period_end TEXT NOT NULL,
    fiscal_period_type TEXT NOT NULL,
    fact_kind TEXT NOT NULL,
    fact_key TEXT NOT NULL,
    action TEXT NOT NULL,
    value NUMERIC,
    unit TEXT,
    value_json TEXT,
    source_doc_type TEXT NOT NULL,
    source_accession TEXT,
    source_exhibit TEXT,
    source_url TEXT,
    source_excerpt TEXT,
    source_doc_id INTEGER,
    status TEXT NOT NULL DEFAULT 'active',
    confidence REAL,
    rationale TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    retired_at TEXT,
    CHECK (fact_kind IN ('financial_fact', 'segment', 'kpi')),
    CHECK (action IN ('replace', 'drop', 'qualify')),
    CHECK (status IN ('active', 'retired'))
);
CREATE UNIQUE INDEX uq_fact_overrides_active ON fact_overrides
    (user_id, ticker, period_end, fiscal_period_type, fact_kind, fact_key)
    WHERE status = 'active';
"""

_SCHEMA_DDL = (
    """
    CREATE TABLE documents (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ticker TEXT NOT NULL,
        source_type TEXT NOT NULL,
        doc_type TEXT NOT NULL,
        source_url TEXT,
        file_path TEXT NOT NULL,
        sha256 TEXT NOT NULL,
        fetched_at TIMESTAMP NOT NULL,
        fetch_status TEXT NOT NULL,
        raw_bytes_size INTEGER NOT NULL,
        source_quality_tier TEXT NOT NULL DEFAULT 'fmp_normalized'
    );
    CREATE TABLE kpi_definitions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ticker TEXT NOT NULL,
        name TEXT NOT NULL
    );
    CREATE TABLE kpi_facts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ticker TEXT NOT NULL,
        period_end TIMESTAMP NOT NULL,
        fiscal_period_type TEXT NOT NULL,
        kpi_definition_id INTEGER NOT NULL,
        value NUMERIC(24, 6) NOT NULL,
        unit TEXT NOT NULL,
        source_doc_id INTEGER NOT NULL
    );
    CREATE TABLE financial_facts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ticker TEXT NOT NULL,
        period_end TIMESTAMP NOT NULL,
        fiscal_period_type TEXT NOT NULL,
        line_item TEXT NOT NULL,
        value NUMERIC(24, 6) NOT NULL,
        unit TEXT NOT NULL DEFAULT 'actual',
        source_doc_id INTEGER NOT NULL
    );
    """
    + _OVERRIDES_DDL
)


@pytest.fixture
def kpi_db(tmp_path: Path) -> Path:
    path = tmp_path / "data" / "portfolio.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.executescript(_SCHEMA_DDL)
    conn.execute(
        "INSERT INTO documents (id, ticker, source_type, doc_type, file_path, sha256, "
        "fetched_at, fetch_status, raw_bytes_size) VALUES "
        "(1, 'NU', 'fmp', 'fmp_key_metrics', 'x', ?, '2026-01-01 00:00:00', 'ok', 1)",
        ("0" * 64,),
    )
    # Two KPI definitions in ONE de-fragmentation group: a sparse bare name and a
    # qualified, richer variant. Resolution must pick the richest for the ticker.
    conn.execute("INSERT INTO kpi_definitions (id, ticker, name) VALUES (1, 'NU', ?)", (_KPI,))
    conn.execute(
        "INSERT INTO kpi_definitions (id, ticker, name) VALUES (2, 'NU', ?)",
        (f"Profitability — consolidated — {_KPI}",),
    )
    # The bare def #1 has ONE stale point; the qualified def #2 has the full,
    # latest series (so the richest-variant resolution must choose #2).
    conn.execute(
        "INSERT INTO kpi_facts (ticker, period_end, fiscal_period_type, kpi_definition_id, "
        "value, unit, source_doc_id) VALUES ('NU', '2024-12-31 00:00:00', 'Q4', 1, 30, 'percent', 1)"
    )
    conn.executemany(
        "INSERT INTO kpi_facts (ticker, period_end, fiscal_period_type, kpi_definition_id, "
        "value, unit, source_doc_id) VALUES ('NU', ?, ?, 2, ?, 'percent', 1)",
        [
            ("2025-03-31 00:00:00", "Q1", 40),
            ("2025-06-30 00:00:00", "Q2", 41),
            ("2025-09-30 00:00:00", "Q3", 42),  # latest
        ],
    )
    conn.commit()
    conn.close()
    return path


def test_resolve_picks_latest_and_richest_variant(kpi_db: Path) -> None:
    """The picked token is the de-fragmented representative ('Operating margin');
    resolution loads THIS ticker's richest variant (the qualified def with the
    full series) and returns its LATEST observation + unit."""
    resolved = resolve_fact_value(
        MetricRef(domain="kpi", key=_KPI),
        ticker="NU",
        repo_root=kpi_db.parent.parent,
        db_path=kpi_db,
    )
    assert resolved.value == pytest.approx(42.0)  # latest (Q3'25) of the rich variant
    assert resolved.unit == "percent"
    assert resolved.period_end == "2025-09-30"
    assert resolved.load_key == f"Profitability — consolidated — {_KPI}"


def test_resolve_honors_company_doc_override(kpi_db: Path) -> None:
    """An active company-doc `replace` on the latest period wins over the FMP row
    (S2) — the resolved value tracks the override, durably."""
    conn = sqlite3.connect(str(kpi_db))
    conn.row_factory = sqlite3.Row
    overrides.record_override(
        conn,
        ticker="NU",
        period_end="2025-09-30",
        fiscal_period_type="Q3",
        fact_kind=overrides.KPI,
        fact_key=f"Profitability — consolidated — {_KPI}",
        action=OverrideAction.REPLACE,
        value=47,
        unit="percent",
        source_doc_type="ir_press_release",
        created_by="test",
    )
    conn.commit()
    conn.close()
    resolved = resolve_fact_value(
        MetricRef(domain="kpi", key=_KPI),
        ticker="NU",
        repo_root=kpi_db.parent.parent,
        db_path=kpi_db,
    )
    assert resolved.value == pytest.approx(47.0)  # overridden, not 42


def test_resolve_override_only_fact(kpi_db: Path) -> None:
    """A company-doc figure FMP never carried (no base row) is still resolvable —
    pickable since S5, so injection must not come back empty."""
    conn = sqlite3.connect(str(kpi_db))
    conn.row_factory = sqlite3.Row
    overrides.record_override(
        conn,
        ticker="NU",
        period_end="2025-09-30",
        fiscal_period_type="Q3",
        fact_kind=overrides.FINANCIAL_FACT,
        fact_key="adjusted_roe",
        action=OverrideAction.REPLACE,
        value=19,
        unit="percent",
        source_doc_type="sec_8k",
        created_by="test",
    )
    conn.commit()
    conn.close()
    resolved = resolve_fact_value(
        MetricRef(domain="fin", key="adjusted_roe"),
        ticker="NU",
        repo_root=kpi_db.parent.parent,
        db_path=kpi_db,
    )
    assert resolved.value == pytest.approx(19.0)
    assert resolved.source == "sec_8k"
    assert resolved.fact_id is None


def test_resolve_no_observations_raises(kpi_db: Path) -> None:
    with pytest.raises(FactDriverError, match="no observations"):
        resolve_fact_value(
            MetricRef(domain="kpi", key="Nonexistent metric"),
            ticker="NU",
            repo_root=kpi_db.parent.parent,
            db_path=kpi_db,
        )


def test_resolve_then_convert_end_to_end(kpi_db: Path) -> None:
    """The full DIY path on the value side: a 42% KPI resolves and converts into
    the 0.42 ratio the margin driver wants."""
    resolved = resolve_fact_value(
        MetricRef(domain="kpi", key=_KPI),
        ticker="NU",
        repo_root=kpi_db.parent.parent,
        db_path=kpi_db,
    )
    out = convert_to_driver(resolved.value, resolved.unit, DRIVER_FIELDS_BY_KEY["near_op_margin"])
    assert out.value == pytest.approx(0.42)


# --------------------------------------------------------------------------- #
# Durable fact-lineage provenance (S6 #5)
# --------------------------------------------------------------------------- #


def test_record_driver_provenance_adds_and_survives(tmp_path: Path) -> None:
    path = tmp_path / "NU.json"
    path.write_text(
        json.dumps({"redesign": {"near_term_op_margin": 0.42, "set_by": "opus-4.8"}}),
        encoding="utf-8",
    )
    ok = record_driver_provenance(
        path,
        field_key="near_op_margin",
        payload={"metric": "kpi:Operating margin", "fact_id": 99, "raw_value": 42.0},
    )
    assert ok is True
    data = json.loads(path.read_text(encoding="utf-8"))
    prov = data["redesign"]["driver_provenance"]["near_op_margin"]
    assert prov["fact_id"] == 99
    assert prov["metric"] == "kpi:Operating margin"
    assert "injected_on" in prov
    # The known numeric key is untouched (sync-compatible: additive only).
    assert data["redesign"]["near_term_op_margin"] == 0.42


def test_record_driver_provenance_missing_file_is_soft(tmp_path: Path) -> None:
    assert record_driver_provenance(tmp_path / "absent.json", field_key="beta", payload={}) is False


def test_record_driver_provenance_no_redesign_block_is_soft(tmp_path: Path) -> None:
    path = tmp_path / "NU.json"
    path.write_text(json.dumps({"other": 1}), encoding="utf-8")
    assert record_driver_provenance(path, field_key="beta", payload={}) is False
