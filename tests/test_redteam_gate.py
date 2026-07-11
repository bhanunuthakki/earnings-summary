"""Month-close computation + escalation query (src/redteam/gate.py, PR6).

Contract under test (monthly_red_team.md Phase 2): a month is CLOSED when
zero items for its run_key have status open/deferred — deferred items count
as UNRESOLVED for the close, only refuted/accepted/closed count as answered.
Escalation (``escalated_items``) is a separate, cross-run query: every item
currently sitting ``deferred`` (it used its one allowed defer).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from alembic.config import Config

from alembic import command

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from redteam import gate, response, store  # noqa: E402
from redteam.models import RedTeamLLMItem  # noqa: E402

PRIOR_HEAD = "0059_kpi_facts_restatement"


def _cfg(db_path: Path) -> Config:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    db = tmp_path / "redteam_gate.db"
    cfg = _cfg(db)
    command.stamp(cfg, PRIOR_HEAD)
    command.upgrade(cfg, "head")
    return db


def _insert_item(db_path: Path, *, run_key: str, ticker: str | None = "NU") -> int:
    return store.insert_item(
        db_path=db_path,
        run_key=run_key,
        ticker=ticker,
        lens="fx_translation",
        kind="per_name",
        item=RedTeamLLMItem(
            attack_md="Attack.", question_md="Q?", proposed_change_md="Change.", severity="med"
        ),
    )


def test_month_status_none_run_key_is_closed(db_path: Path) -> None:
    ms = gate.month_status(db_path=db_path, run_key=None)
    assert ms.is_closed is True
    assert ms.unresolved_count == 0
    assert ms.run_key is None


def test_month_status_run_with_no_items_is_closed(db_path: Path) -> None:
    ms = gate.month_status(db_path=db_path, run_key="red_team_2026_09")
    assert ms.is_closed is True
    assert ms.unresolved_count == 0


def test_month_status_open_items_keep_month_open(db_path: Path) -> None:
    _insert_item(db_path, run_key="red_team_2026_08")
    _insert_item(db_path, run_key="red_team_2026_08")
    ms = gate.month_status(db_path=db_path, run_key="red_team_2026_08")
    assert ms.is_closed is False
    assert ms.unresolved_count == 2


def test_month_status_deferred_items_count_as_unresolved(db_path: Path) -> None:
    item_id = _insert_item(db_path, run_key="red_team_2026_08")
    response.respond(db_path=db_path, item_id=item_id, action="defer")
    ms = gate.month_status(db_path=db_path, run_key="red_team_2026_08")
    assert ms.is_closed is False
    assert ms.unresolved_count == 1


def test_month_status_closes_once_every_item_is_answered(db_path: Path) -> None:
    a = _insert_item(db_path, run_key="red_team_2026_08")
    b = _insert_item(db_path, run_key="red_team_2026_08", ticker=None)
    response.respond(db_path=db_path, item_id=a, action="refute", response_md="Reasoning.")
    response.respond(db_path=db_path, item_id=b, action="accept")
    ms = gate.month_status(db_path=db_path, run_key="red_team_2026_08")
    assert ms.is_closed is True
    assert ms.unresolved_count == 0


def test_month_status_scoped_to_its_own_run_key(db_path: Path) -> None:
    open_in_aug = _insert_item(db_path, run_key="red_team_2026_08")
    closed_in_jul = _insert_item(db_path, run_key="red_team_2026_07")
    response.respond(db_path=db_path, item_id=closed_in_jul, action="accept")
    assert gate.month_status(db_path=db_path, run_key="red_team_2026_07").is_closed is True
    aug = gate.month_status(db_path=db_path, run_key="red_team_2026_08")
    assert aug.is_closed is False
    assert aug.unresolved_count == 1
    assert open_in_aug  # keeps ruff's "unused variable" quiet about intent


def test_escalated_items_empty_before_any_defer(db_path: Path) -> None:
    _insert_item(db_path, run_key="red_team_2026_08")
    assert gate.escalated_items(db_path=db_path) == []


def test_escalated_items_lists_items_stuck_in_deferred(db_path: Path) -> None:
    item_id = _insert_item(db_path, run_key="red_team_2026_08")
    response.respond(db_path=db_path, item_id=item_id, action="defer")
    escalated = gate.escalated_items(db_path=db_path)
    assert [i.id for i in escalated] == [item_id]
    assert escalated[0].status == "deferred"
    assert escalated[0].defer_count == 1


def test_escalated_items_spans_run_keys(db_path: Path) -> None:
    old = _insert_item(db_path, run_key="red_team_2026_07")
    response.respond(db_path=db_path, item_id=old, action="defer")
    new_run = _insert_item(db_path, run_key="red_team_2026_08")
    escalated = gate.escalated_items(db_path=db_path)
    assert [i.id for i in escalated] == [old]
    assert new_run not in [i.id for i in escalated]


def test_escalated_items_drops_out_once_answered(db_path: Path) -> None:
    item_id = _insert_item(db_path, run_key="red_team_2026_08")
    response.respond(db_path=db_path, item_id=item_id, action="defer")
    assert len(gate.escalated_items(db_path=db_path)) == 1
    response.respond(db_path=db_path, item_id=item_id, action="accept")
    assert gate.escalated_items(db_path=db_path) == []


def test_never_raises_without_schema(tmp_path: Path) -> None:
    import sqlite3

    bare = tmp_path / "bare.db"
    sqlite3.connect(str(bare)).close()
    assert gate.escalated_items(db_path=bare) == []
    ms = gate.month_status(db_path=bare, run_key="red_team_2026_08")
    assert ms.is_closed is True
