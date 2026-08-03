"""Tests for the user_state CRUD modules: registry, sizing, ledger.

Each test gets a fresh tmp_path SQLite DB stamped at 0059 and upgraded to
head (0064). That way the 5 Personal CIO tables exist exactly as alembic
creates them in production; the CRUD modules must agree with the actual
schema, not a hand-rolled approximation.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from user_state import ledger, registry, sizing

PRIOR_HEAD = "0059_kpi_facts_restatement"


@pytest.fixture
def db_path(tmp_path: Path, migrated_db: Callable[..., Path]) -> Path:
    """tmp_path SQLite stamped at 0059, then upgraded to head — the 5
    Personal CIO tables exist after this fixture runs."""
    return migrated_db(tmp_path / "user_state.db", stamp=PRIOR_HEAD)


# ----------------------------------------------------------------------------
# registry
# ----------------------------------------------------------------------------


def test_registry_upsert_round_trip(db_path: Path) -> None:
    row = registry.upsert_kpi(
        ticker="NU",
        kpi_name="monthly_active_customers",
        threshold_direction="above",
        threshold_value=100_000_000.0,
        is_thesis_breaker=True,
        scaffold_source="seeded_from_10k",
        notes="hit by Q4 print",
        db_path=db_path,
    )

    assert row.id > 0
    assert row.user_id == "bhanu"
    assert row.ticker == "NU"
    assert row.kpi_name == "monthly_active_customers"
    assert row.threshold_direction == "above"
    assert row.threshold_value == 100_000_000.0
    assert row.is_thesis_breaker is True
    assert row.scaffold_source == "seeded_from_10k"
    assert row.notes == "hit by Q4 print"

    all_rows = registry.list_kpis(db_path=db_path)
    assert [r.id for r in all_rows] == [row.id]


def test_registry_upsert_is_idempotent_on_natural_key(db_path: Path) -> None:
    """Repeated upsert on the same (user_id, ticker, kpi_name) must UPDATE,
    not INSERT a duplicate. The unique index ``uq_user_kpi_registry_user_
    ticker_kpi`` would raise IntegrityError on a second INSERT — the CRUD's
    update-branch handles this transparently."""
    first = registry.upsert_kpi(
        ticker="META",
        kpi_name="dau",
        is_thesis_breaker=False,
        db_path=db_path,
    )
    second = registry.upsert_kpi(
        ticker="META",
        kpi_name="dau",
        threshold_direction="below",
        threshold_value=3_000_000_000.0,
        is_thesis_breaker=True,
        notes="updated",
        db_path=db_path,
    )

    assert first.id == second.id
    assert second.threshold_direction == "below"
    assert second.is_thesis_breaker is True
    assert second.notes == "updated"
    assert second.created_at == first.created_at
    assert second.updated_at >= first.updated_at

    all_rows = registry.list_kpis(ticker="META", db_path=db_path)
    assert len(all_rows) == 1
    assert all_rows[0].id == first.id


def test_registry_list_kpis_filters_by_ticker(db_path: Path) -> None:
    registry.upsert_kpi(ticker="META", kpi_name="dau", db_path=db_path)
    registry.upsert_kpi(ticker="META", kpi_name="reels_minutes", db_path=db_path)
    registry.upsert_kpi(ticker="GOOG", kpi_name="cloud_op_margin", db_path=db_path)

    meta_only = registry.list_kpis(ticker="META", db_path=db_path)
    assert {r.kpi_name for r in meta_only} == {"dau", "reels_minutes"}

    all_three = registry.list_kpis(db_path=db_path)
    assert len(all_three) == 3


def test_registry_get_thesis_breakers_filters_to_breakers(db_path: Path) -> None:
    registry.upsert_kpi(
        ticker="NU",
        kpi_name="net_interest_margin",
        is_thesis_breaker=True,
        db_path=db_path,
    )
    registry.upsert_kpi(
        ticker="NU",
        kpi_name="active_customers",
        is_thesis_breaker=False,
        db_path=db_path,
    )
    registry.upsert_kpi(
        ticker="MELI",
        kpi_name="take_rate",
        is_thesis_breaker=True,
        db_path=db_path,
    )

    breakers = registry.get_thesis_breakers(db_path=db_path)
    assert {(b.ticker, b.kpi_name) for b in breakers} == {
        ("NU", "net_interest_margin"),
        ("MELI", "take_rate"),
    }

    nu_breakers = registry.get_thesis_breakers(ticker="NU", db_path=db_path)
    assert [b.kpi_name for b in nu_breakers] == ["net_interest_margin"]


def test_registry_delete_kpi_removes_row(db_path: Path) -> None:
    row = registry.upsert_kpi(ticker="WIX", kpi_name="self_creators", db_path=db_path)
    assert registry.delete_kpi(row.id, db_path=db_path) is True
    assert registry.delete_kpi(row.id, db_path=db_path) is False
    assert registry.list_kpis(db_path=db_path) == []


# ----------------------------------------------------------------------------
# sizing
# ----------------------------------------------------------------------------


def test_sizing_append_intent_creates_new_row_each_time(db_path: Path) -> None:
    """Sizing intents are append-only: three append_intent calls = three rows
    in the table, even with the same (ticker, intent_kind)."""
    first = sizing.append_intent(
        ticker="GOOG",
        intent_kind="target_pct",
        intent_value=0.07,
        narrative="initial pre-Q4 size",
        db_path=db_path,
    )
    second = sizing.append_intent(
        ticker="GOOG",
        intent_kind="target_pct",
        intent_value=0.05,
        narrative="trimmed after Cloud guide-down",
        db_path=db_path,
    )
    third = sizing.append_intent(
        ticker="GOOG",
        intent_kind="max_pct",
        intent_value=0.10,
        db_path=db_path,
    )

    rows = sizing.list_intents(ticker="GOOG", db_path=db_path)
    assert {r.id for r in rows} == {first.id, second.id, third.id}
    assert len(rows) == 3


def test_sizing_latest_intent_returns_most_recent_of_kind(db_path: Path) -> None:
    """latest_intent must return the chronologically most recent of an
    intent_kind for that ticker — verify with three writes and check that
    the third is returned, not the first or second."""
    sizing.append_intent(
        ticker="GOOG",
        intent_kind="target_pct",
        intent_value=0.07,
        db_path=db_path,
    )
    sizing.append_intent(
        ticker="GOOG",
        intent_kind="target_pct",
        intent_value=0.05,
        db_path=db_path,
    )
    third = sizing.append_intent(
        ticker="GOOG",
        intent_kind="target_pct",
        intent_value=0.04,
        db_path=db_path,
    )

    latest = sizing.latest_intent(
        ticker="GOOG",
        intent_kind="target_pct",
        db_path=db_path,
    )
    assert latest is not None
    assert latest.id == third.id
    assert latest.intent_value == 0.04


def test_sizing_latest_intent_kind_scoped(db_path: Path) -> None:
    """A `target_pct` intent must not satisfy a `max_pct` lookup."""
    sizing.append_intent(
        ticker="MELI",
        intent_kind="target_pct",
        intent_value=0.05,
        db_path=db_path,
    )
    assert sizing.latest_intent(ticker="MELI", intent_kind="max_pct", db_path=db_path) is None
    assert sizing.latest_intent(ticker="OTHER", intent_kind="target_pct", db_path=db_path) is None


def test_sizing_list_intents_returns_newest_first(db_path: Path) -> None:
    a = sizing.append_intent(
        ticker="NVO",
        intent_kind="sizing_note",
        narrative="initial entry",
        db_path=db_path,
    )
    b = sizing.append_intent(
        ticker="NVO",
        intent_kind="sizing_note",
        narrative="second",
        db_path=db_path,
    )

    rows = sizing.list_intents(db_path=db_path)
    assert [r.id for r in rows] == [b.id, a.id]


# ----------------------------------------------------------------------------
# ledger
# ----------------------------------------------------------------------------


def test_ledger_append_entry_round_trip(db_path: Path) -> None:
    row = ledger.append_entry(
        ticker="GOOG",
        entry_kind="thesis_update",
        body="Cloud margin inflection confirmed by Q4 print.",
        source_alert_id=42,
        db_path=db_path,
    )
    assert row.id > 0
    assert row.ticker == "GOOG"
    assert row.entry_kind == "thesis_update"
    assert row.body == "Cloud margin inflection confirmed by Q4 print."
    assert row.source_alert_id == 42
    # created_at and accepted_at must agree — the spec says the row IS the
    # act of acceptance.
    assert row.created_at == row.accepted_at


def test_ledger_no_update_or_delete_in_public_api() -> None:
    """The ledger is intentionally append-only — no update/delete API
    exists in the module. This guards against a future contributor adding
    one without thinking through the audit-trail implications."""
    public = {name for name in dir(ledger) if not name.startswith("_")}
    forbidden = {
        name
        for name in public
        if name.startswith(("update_", "delete_", "remove_", "edit_", "amend_"))
    }
    assert not forbidden, f"ledger gained mutation/delete API: {forbidden}"


def test_ledger_list_entries_filters_by_entry_kind(db_path: Path) -> None:
    ledger.append_entry(
        ticker="META",
        entry_kind="thesis_update",
        body="A",
        db_path=db_path,
    )
    ledger.append_entry(
        ticker="META",
        entry_kind="bear_append",
        body="B",
        db_path=db_path,
    )
    ledger.append_entry(
        ticker="META",
        entry_kind="earnings_prep_note",
        body="C",
        db_path=db_path,
    )

    all_meta = ledger.list_entries(ticker="META", db_path=db_path)
    assert {e.body for e in all_meta} == {"A", "B", "C"}

    bears = ledger.list_entries(
        ticker="META",
        entry_kind="bear_append",
        db_path=db_path,
    )
    assert [e.body for e in bears] == ["B"]


def test_ledger_list_entries_newest_first(db_path: Path) -> None:
    a = ledger.append_entry(
        ticker="NU",
        entry_kind="thesis_update",
        body="first",
        db_path=db_path,
    )
    b = ledger.append_entry(
        ticker="NU",
        entry_kind="thesis_update",
        body="second",
        db_path=db_path,
    )
    rows = ledger.list_entries(ticker="NU", db_path=db_path)
    assert [r.id for r in rows] == [b.id, a.id]


def test_ledger_list_entries_respects_limit(db_path: Path) -> None:
    for i in range(5):
        ledger.append_entry(
            ticker="WIX",
            entry_kind="thesis_update",
            body=f"note {i}",
            db_path=db_path,
        )
    truncated = ledger.list_entries(ticker="WIX", limit=2, db_path=db_path)
    assert len(truncated) == 2
