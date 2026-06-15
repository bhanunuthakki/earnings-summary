"""Unit tests for src/provenance/overrides.py — the fact_overrides resolver.

Covers the durable-record API: record/retire/resolve round-trip, the single-active
invariant (record retires the prior active), and apply_segment_overrides for the
three segment shapes (record-level replace, cell-level replace, cell-level drop).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import cast

import pytest
from alembic.config import Config

from alembic import command
from provenance import overrides
from provenance.overrides import OverrideAction

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PRIOR_HEAD = "0110_pass_decision_source"


def _config(db_path: Path) -> Config:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    db = tmp_path / "ov.db"
    cfg = _config(db)
    command.stamp(cfg, PRIOR_HEAD)
    command.upgrade(cfg, "head")  # builds just 0111 (fact_overrides) atop the stamp
    c = sqlite3.connect(str(db))
    c.row_factory = sqlite3.Row
    return c


# GOOG Q4'25 product segments per the SEC 8-K (leaf sum $113,828M).
_GOOG_Q4_PRODUCT = {
    "Google Search & Other": 63073000000,
    "YouTube Advertising Revenue": 11383000000,
    "Google Network": 7828000000,
    "Subscriptions, Platforms, And Devices Revenue": 13578000000,
    "Google Cloud": 17664000000,
    "Other Bets": 370000000,
    "Other Segments": -68000000,
}


def _data(rec: dict[str, object]) -> dict[str, object]:
    """Narrow a segment record's ``data`` field from object -> dict for indexing."""
    d = rec["data"]
    assert isinstance(d, dict)
    return cast("dict[str, object]", d)


def test_record_and_resolve_scalar(conn: sqlite3.Connection) -> None:
    new_id = overrides.record_override(
        conn,
        ticker="goog",
        period_end="2025-12-31",
        fiscal_period_type="Q4",
        fact_kind=overrides.KPI,
        fact_key="Google Cloud revenue growth",
        action=OverrideAction.REPLACE,
        value=48,
        unit="percent",
        source_doc_type="ir_press_release",
        created_by="test",
    )
    assert new_id > 0
    ov = overrides.resolve_scalar(
        conn,
        ticker="GOOG",
        period_end="2025-12-31",
        fiscal_period_type="Q4",
        fact_kind=overrides.KPI,
        fact_key="Google Cloud revenue growth",
    )
    assert ov is not None
    assert ov.value == 48.0
    assert ov.unit == "percent"
    assert ov.action == "replace"
    assert ov.source_doc_type == "ir_press_release"


def test_resolve_miss_returns_none(conn: sqlite3.Connection) -> None:
    assert (
        overrides.resolve_scalar(
            conn,
            ticker="GOOG",
            period_end="2025-12-31",
            fiscal_period_type="Q4",
            fact_kind=overrides.FINANCIAL_FACT,
            fact_key="revenue",
        )
        is None
    )


def test_record_retires_prior_active(conn: sqlite3.Connection) -> None:
    def _rev(value: int) -> int:
        return overrides.record_override(
            conn,
            ticker="GOOG",
            period_end="2025-12-31",
            fiscal_period_type="Q4",
            fact_kind=overrides.FINANCIAL_FACT,
            fact_key="revenue",
            action=OverrideAction.REPLACE,
            source_doc_type="sec_8k",
            created_by="test",
            value=value,
        )

    _rev(100)
    _rev(200)
    # Exactly one active row; it carries the latest value.
    active = overrides.get_active_overrides(conn, ticker="GOOG")
    assert len(active) == 1
    assert active[0].value == 200.0
    # The prior active was retired, not deleted.
    retired = conn.execute("SELECT COUNT(*) FROM fact_overrides WHERE status='retired'").fetchone()[
        0
    ]
    assert retired == 1


def test_record_validates_inputs(conn: sqlite3.Connection) -> None:
    with pytest.raises(ValueError):
        overrides.record_override(
            conn,
            ticker="GOOG",
            period_end="2025-12-31",
            fiscal_period_type="Q4",
            fact_kind="bogus",
            fact_key="revenue",
            action="replace",
            source_doc_type="sec_8k",
            created_by="test",
            value=1,
        )
    with pytest.raises(ValueError):
        overrides.record_override(
            conn,
            ticker="GOOG",
            period_end="2025-12-31",
            fiscal_period_type="Q4",
            fact_kind=overrides.FINANCIAL_FACT,
            fact_key="revenue",
            action="bogus",
            source_doc_type="sec_8k",
            created_by="test",
            value=1,
        )
    # 'replace' with no value/value_json is rejected.
    with pytest.raises(ValueError):
        overrides.record_override(
            conn,
            ticker="GOOG",
            period_end="2025-12-31",
            fiscal_period_type="Q4",
            fact_kind=overrides.FINANCIAL_FACT,
            fact_key="revenue",
            action="replace",
            source_doc_type="sec_8k",
            created_by="test",
        )


def test_apply_segment_record_level_replace(conn: sqlite3.Connection) -> None:
    overrides.record_override(
        conn,
        ticker="GOOG",
        period_end="2025-12-31",
        fiscal_period_type="Q4",
        fact_kind=overrides.SEGMENT,
        fact_key=overrides.segment_record_key("product"),
        action=OverrideAction.REPLACE,
        value_json=dict(_GOOG_Q4_PRODUCT),
        source_doc_type="sec_8k",
        source_accession="0001652044-26-000012",
        created_by="test",
    )
    # Simulate a re-fetched BAD FMP record: inflated GCP, spurious bucket, missing Subscriptions.
    bad: list[dict[str, object]] = [
        {
            "symbol": "GOOG",
            "period": "Q4",
            "date": "2025-12-31",
            "data": {
                "Google Cloud": 20941000000,
                "Google Inc.": 12870000000,
                "Google Search & Other": 63073000000,
            },
        },
        {
            "symbol": "GOOG",
            "period": "Q3",
            "date": "2025-09-30",
            "data": {"Google Cloud": 15157000000},
        },
    ]
    out = overrides.apply_segment_overrides(conn, ticker="GOOG", dim_type="product", records=bad)
    q4 = next(r for r in out if r["date"] == "2025-12-31")
    assert _data(q4) == _GOOG_Q4_PRODUCT  # whole dict reconstructed from the 8-K
    assert _data(q4)["Google Cloud"] == 17664000000
    assert "Google Inc." not in _data(q4)
    # Q3 (no override) is untouched, and the input list is not mutated.
    q3 = next(r for r in out if r["date"] == "2025-09-30")
    assert _data(q3) == {"Google Cloud": 15157000000}
    assert _data(bad[0])["Google Cloud"] == 20941000000


def test_apply_segment_cell_replace_and_drop(conn: sqlite3.Connection) -> None:
    overrides.record_override(
        conn,
        ticker="GOOG",
        period_end="2025-12-31",
        fiscal_period_type="Q4",
        fact_kind=overrides.SEGMENT,
        fact_key=overrides.segment_cell_key("product", "Google Cloud"),
        action=OverrideAction.REPLACE,
        value=17664000000,
        source_doc_type="sec_8k",
        created_by="test",
    )
    overrides.record_override(
        conn,
        ticker="GOOG",
        period_end="2025-12-31",
        fiscal_period_type="Q4",
        fact_kind=overrides.SEGMENT,
        fact_key=overrides.segment_cell_key("product", "Google Inc."),
        action=OverrideAction.DROP,
        source_doc_type="sec_8k",
        created_by="test",
    )
    bad: list[dict[str, object]] = [
        {
            "symbol": "GOOG",
            "period": "Q4",
            "date": "2025-12-31",
            "data": {
                "Google Cloud": 20941000000,
                "Google Inc.": 12870000000,
                "Google Network": 7828000000,
            },
        }
    ]
    out = overrides.apply_segment_overrides(conn, ticker="GOOG", dim_type="product", records=bad)
    data = _data(out[0])
    assert data["Google Cloud"] == 17664000000  # replaced
    assert "Google Inc." not in data  # dropped
    assert data["Google Network"] == 7828000000  # untouched


def test_overridden_segment_periods(conn: sqlite3.Connection) -> None:
    overrides.record_override(
        conn,
        ticker="GOOG",
        period_end="2025-12-31",
        fiscal_period_type="Q4",
        fact_kind=overrides.SEGMENT,
        fact_key=overrides.segment_record_key("product"),
        action=OverrideAction.REPLACE,
        value_json=dict(_GOOG_Q4_PRODUCT),
        source_doc_type="sec_8k",
        created_by="test",
    )
    assert overrides.overridden_segment_periods(conn, ticker="GOOG", dim_type="product") == {
        "2025-12-31"
    }
    assert overrides.overridden_segment_periods(conn, ticker="GOOG", dim_type="geography") == set()


def test_apply_segment_no_override_is_passthrough(conn: sqlite3.Connection) -> None:
    recs: list[dict[str, object]] = [
        {"symbol": "META", "period": "Q4", "date": "2025-12-31", "data": {"Family of Apps": 1}}
    ]
    out = overrides.apply_segment_overrides(conn, ticker="META", dim_type="product", records=recs)
    assert out == recs
    assert out is not recs  # a fresh list, never the caller's


def test_retire_override(conn: sqlite3.Connection) -> None:
    overrides.record_override(
        conn,
        ticker="GOOG",
        period_end="2025-12-31",
        fiscal_period_type="Q4",
        fact_kind=overrides.FINANCIAL_FACT,
        fact_key="revenue",
        action=OverrideAction.REPLACE,
        value=100,
        source_doc_type="sec_8k",
        created_by="test",
    )
    assert overrides.retire_override(
        conn,
        ticker="GOOG",
        period_end="2025-12-31",
        fiscal_period_type="Q4",
        fact_kind=overrides.FINANCIAL_FACT,
        fact_key="revenue",
    )
    assert not overrides.get_active_overrides(conn, ticker="GOOG")
