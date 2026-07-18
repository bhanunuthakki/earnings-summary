"""The /review capacity block (tenet-2 Phase 2, §4 delivery seam 1 of
docs/design/tenet2_advisory_program.md) — build_capacity_context +
render_capacity_lines + their wiring into render_pre_analysis_chat/plain.

Covers: absent-when-unaffirmed (a proposed-only fact contributes nothing),
the dated life-event horizon-window filter (a baby in 2031 doesn't show on a
review run "today"; a 2027 work-break does), the human-capital bucket match
(ticker IS / IS NOT a member), and the byte-identical-when-empty guarantee —
render_pre_analysis_chat/plain must render EXACTLY as they did before Phase 2
when nothing is affirmed.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest

from advisor.position_review import (
    CAPACITY_HORIZON_DAYS,
    CapacityContext,
    PreAnalysis,
    build_capacity_context,
    render_capacity_lines,
    render_pre_analysis_chat,
    render_pre_analysis_plain,
)
from owner_profile.store import affirm_fact, append_fact

_DDL = """
CREATE TABLE owner_profile_facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL DEFAULT 'bhanu',
    category TEXT NOT NULL CHECK (category IN ('capacity','appetite','behavioral')),
    key TEXT NOT NULL,
    value_json TEXT NOT NULL,
    narrative TEXT NOT NULL,
    provenance TEXT NOT NULL CHECK (
        provenance IN ('wealthplan_import','cio_context_import','owner','derived')
    ),
    status TEXT NOT NULL DEFAULT 'proposed' CHECK (status IN ('proposed','affirmed','rejected')),
    affirmed_at TEXT,
    review_horizon_days INTEGER,
    source_detail TEXT,
    created_at TEXT NOT NULL,
    is_latest INTEGER NOT NULL DEFAULT 1,
    superseded_at TEXT,
    superseded_by_id INTEGER
);
CREATE UNIQUE INDEX ux_owner_profile_facts_latest
    ON owner_profile_facts(user_id, category, key) WHERE is_latest = 1;
"""

_TODAY = date(2026, 7, 17)


@pytest.fixture()
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    c = sqlite3.connect(str(tmp_path / "p.db"))
    c.row_factory = sqlite3.Row
    c.executescript(_DDL)
    c.commit()
    yield c
    c.close()


def _affirm(conn: sqlite3.Connection, **kwargs: object) -> int:
    fact_id = append_fact(conn, status="proposed", **kwargs)  # type: ignore[arg-type]
    affirm_fact(conn, fact_id)
    conn.commit()
    return fact_id


# --------------------------------------------------------------------------- #
# build_capacity_context — absence, cash buffer + tax buckets
# --------------------------------------------------------------------------- #


def test_empty_conn_yields_all_empty_context(conn: sqlite3.Connection) -> None:
    ctx = build_capacity_context(conn, "META", today=_TODAY)
    assert ctx.is_empty()
    assert ctx == CapacityContext()


def test_proposed_only_fact_contributes_nothing(conn: sqlite3.Connection) -> None:
    """A fact staged but NOT affirmed must never condition the block (§7.1
    gated assertion) — the common case today (25 capacity facts, 0 affirmed)."""
    append_fact(
        conn,
        category="capacity",
        key="cash_buffer_months",
        value={"months": 6.0},
        narrative="Target cash buffer: 6 months of spend.",
        provenance="wealthplan_import",
        status="proposed",
    )
    ctx = build_capacity_context(conn, "META", today=_TODAY)
    assert ctx.is_empty()


def test_cash_buffer_and_tax_bucket_render(conn: sqlite3.Connection) -> None:
    _affirm(
        conn,
        category="capacity",
        key="cash_buffer_months",
        value={"months": 9.0},
        narrative="Target cash buffer: 9 months of spend.",
        provenance="wealthplan_import",
    )
    _affirm(
        conn,
        category="capacity",
        key="tax_bucket_balances",
        value={"balances": {"taxable": 100000.0, "roth": 50000.0}, "as_of": "2026-06-30"},
        narrative="Tax-bucket balances as of 2026-06-30.",
        provenance="wealthplan_import",
    )
    ctx = build_capacity_context(conn, "META", today=_TODAY)
    assert ctx.cash_buffer_months == 9.0
    assert ctx.tax_bucket_total_usd == pytest.approx(150000.0)
    assert ctx.tax_bucket_as_of == "2026-06-30"
    assert not ctx.is_empty()


# --------------------------------------------------------------------------- #
# dated life-event horizon-window filter
# --------------------------------------------------------------------------- #


def test_life_event_far_in_future_does_not_show(conn: sqlite3.Connection) -> None:
    """A baby due 2031 is well outside the 24mo lookahead from 2026-07-17."""
    _affirm(
        conn,
        category="capacity",
        key="life_event.baby_2031_03_01",
        value={"kind": "baby", "label": "Baby due", "date": "2031-03-01"},
        narrative="Baby due: 2031-03-01.",
        provenance="wealthplan_import",
    )
    ctx = build_capacity_context(conn, "META", today=_TODAY)
    assert ctx.upcoming_life_events == ()


def test_life_event_within_horizon_shows(conn: sqlite3.Connection) -> None:
    """A work-break starting 2027-06-01 IS within 24mo of 2026-07-17."""
    _affirm(
        conn,
        category="capacity",
        key="life_event.work_break_person_a_2027_06_01",
        value={
            "kind": "work_break",
            "label": "Career break",
            "date": "2027-06-01",
            "end_date": "2027-12-01",
            "person": "person_a",
        },
        narrative="Career break (person_a): 2027-06-01 through 2027-12-01.",
        provenance="wealthplan_import",
    )
    ctx = build_capacity_context(conn, "META", today=_TODAY)
    assert len(ctx.upcoming_life_events) == 1
    line = ctx.upcoming_life_events[0]
    assert "Career break" in line and "person_a" in line and "2027-06-01" in line
    assert "2027-12-01" in line  # end_date window


def test_life_event_exactly_at_horizon_boundary_shows(conn: sqlite3.Connection) -> None:
    boundary = _TODAY.toordinal() + CAPACITY_HORIZON_DAYS
    boundary_date = date.fromordinal(boundary)
    _affirm(
        conn,
        category="capacity",
        key="life_event.move_boundary",
        value={"kind": "move", "label": "Move to Austin", "date": boundary_date.isoformat()},
        narrative="Move.",
        provenance="wealthplan_import",
    )
    ctx = build_capacity_context(conn, "META", today=_TODAY)
    assert len(ctx.upcoming_life_events) == 1


def test_parent_care_event_never_crashes_and_is_skipped(conn: sqlite3.Connection) -> None:
    """ParentCareWindow facts are age-keyed, not date-keyed — LifeEventFact
    validation fails on them (no `date` field), and they're silently skipped
    rather than crashing the block."""
    _affirm(
        conn,
        category="capacity",
        key="life_event.parent_care_70_80",
        value={"label": "Parent care", "start_age": 70, "end_age": 80},
        narrative="Parent care: active from age 70 to age 80.",
        provenance="wealthplan_import",
    )
    ctx = build_capacity_context(conn, "META", today=_TODAY)
    assert ctx.upcoming_life_events == ()
    assert ctx.is_empty()


# --------------------------------------------------------------------------- #
# human-capital bucket membership
# --------------------------------------------------------------------------- #


def test_human_capital_bucket_match_shows_note(conn: sqlite3.Connection) -> None:
    _affirm(
        conn,
        category="capacity",
        key="human_capital.big_tech_ads",
        value={"cap_pct": 15.0, "members": ["META", "GOOG", "GOOGL", "AMZN"]},
        narrative="Human-capital bucket 'big_tech_ads': cap 15%.",
        provenance="cio_context_import",
    )
    ctx = build_capacity_context(conn, "META", today=_TODAY)
    assert ctx.human_capital_note is not None
    assert "big_tech_ads" in ctx.human_capital_note
    assert "15%" in ctx.human_capital_note


def test_human_capital_bucket_non_member_no_note(conn: sqlite3.Connection) -> None:
    _affirm(
        conn,
        category="capacity",
        key="human_capital.big_tech_ads",
        value={"cap_pct": 15.0, "members": ["META", "GOOG", "GOOGL", "AMZN"]},
        narrative="Human-capital bucket 'big_tech_ads': cap 15%.",
        provenance="cio_context_import",
    )
    ctx = build_capacity_context(conn, "RBRK", today=_TODAY)
    assert ctx.human_capital_note is None
    assert ctx.is_empty()


# --------------------------------------------------------------------------- #
# render_capacity_lines
# --------------------------------------------------------------------------- #


def test_render_capacity_lines_none_and_empty_both_yield_no_lines() -> None:
    assert render_capacity_lines(None) == []
    assert render_capacity_lines(CapacityContext()) == []


def test_render_capacity_lines_full_context() -> None:
    ctx = CapacityContext(
        cash_buffer_months=9.0,
        tax_bucket_total_usd=150000.0,
        tax_bucket_as_of="2026-06-30",
        upcoming_life_events=("Career break (person_a): 2027-06-01 through 2027-12-01",),
        human_capital_note="this position stacks on your income's big_tech_ads bucket (cap 15%)",
        wealthplan_cash_need_note="elevated (work break)",
        exit_quality_note="prior realized exit on META beat holding by ~$1,000",
    )
    lines = render_capacity_lines(ctx)
    text = "\n".join(lines)
    assert "9 months" in text
    assert "$150,000" in text and "2026-06-30" in text
    assert "Career break" in text
    assert "big_tech_ads" in text
    assert "elevated (work break)" in text
    assert "beat holding" in text


# --------------------------------------------------------------------------- #
# byte-identical-when-empty: render_pre_analysis_chat/plain
# --------------------------------------------------------------------------- #

_PRE_DEFAULT = PreAnalysis(
    ticker="RBRK",
    weight_pct=10.0,
    weight_source="materialized",
    market_value_usd=None,
    unrealized_pnl_usd=None,
    target_weight_pct=None,
    target_band=None,
    weight_vs_band="no_band",
    conviction_1_5=None,
    concentration_flag=False,
    thesis_present=True,
    verdict_label="Intact",
    key_driver="Net new subscription ARR cadence",
    break_rule_status="intact",
    tripped_rules=(),
    dcf_gap_pct=-10.0,
    npv_per_share=91.09,
    dcf_live_price=80.35,
    dcf_date="2026-06-15",
    at_price=None,
    mos_bar=0.30,
    valuation_verdict="fair",
    conviction_encoded=True,
    has_stance=True,
    has_decision_note=True,
    is_index_instrument=False,
)


def test_chat_render_byte_identical_capacity_none_vs_empty_context() -> None:
    pre_none = _PRE_DEFAULT
    pre_empty = replace(_PRE_DEFAULT, capacity=CapacityContext())
    assert render_pre_analysis_chat(pre_none) == render_pre_analysis_chat(pre_empty)


def test_plain_render_byte_identical_capacity_none_vs_empty_context() -> None:
    pre_none = _PRE_DEFAULT
    pre_empty = replace(_PRE_DEFAULT, capacity=CapacityContext())
    assert render_pre_analysis_plain(pre_none) == render_pre_analysis_plain(pre_empty)


def test_chat_render_no_capacity_lines_when_empty() -> None:
    out = render_pre_analysis_chat(_PRE_DEFAULT)
    assert "Capacity" not in out
    assert "Life event" not in out
    assert "Human-capital" not in out


def test_chat_render_capacity_lines_appear_between_tax_and_mechanical() -> None:
    from advisor.position_tax import PositionTaxView

    tax = PositionTaxView(
        available=True,
        reason=None,
        approximate=False,
        approx_reasons=(),
        eval_price=300.0,
        taxable_mv_usd=90_000.0,
        taxable_pct_of_position=100.0,
        sheltered_mv_usd=0.0,
        sheltered_note=None,
        st_unrealized_usd=100.0,
        lt_unrealized_usd=200.0,
        trim=None,
        footnote="",
    )
    capacity = CapacityContext(cash_buffer_months=9.0)
    pre = replace(_PRE_DEFAULT, tax=tax, capacity=capacity)
    out = render_pre_analysis_chat(pre)
    assert out.index("- Tax:") < out.index("- Capacity:") < out.index("- Mechanical read:")
