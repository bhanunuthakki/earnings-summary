"""The Worldview Tenet store (P2) — CRUD over insight_notes kind='tenet'.

Owner-typed Tenets are current immediately; machine-distilled land proposed;
approval promotes + supersedes the standing belief; revision rides the supersede
chain on a shared scope_key; tension is the same-scope overlap.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic.config import Config

from alembic import command
from synthesis import tenets
from synthesis.insights import get_insight

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PRIOR_HEAD = "0059_kpi_facts_restatement"


def _cfg(db_path: Path) -> Config:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    db = tmp_path / "ledger.db"
    cfg = _cfg(db)
    command.stamp(cfg, PRIOR_HEAD)
    command.upgrade(cfg, "head")
    return db


def test_owner_tenet_is_current_immediately(db_path: Path) -> None:
    t = tenets.record_tenet(
        body_md="I sell my winners too early.", provenance="owner", db_path=db_path
    )
    assert t.status == "current"
    assert t.kind == "tenet"
    assert t.scope_key.startswith("tenet:")
    assert [x.scope_key for x in tenets.list_tenets(status="current", db_path=db_path)] == [
        t.scope_key
    ]


def test_scope_key_derivation_and_reuse(db_path: Path) -> None:
    a = tenets.record_tenet(
        body_md="Concentrate into the highest-conviction names.", db_path=db_path
    )
    # explicit scope reuse revises the standing Tenet (supersede chain)
    b = tenets.record_tenet(
        body_md="Concentrate hard, but keep one hedge.", scope_key=a.scope_key, db_path=db_path
    )
    assert b.scope_key == a.scope_key
    current = tenets.list_tenets(status="current", db_path=db_path)
    assert [c.id for c in current] == [b.id]  # only the revision is current
    old = get_insight(a.id, db_path=db_path)
    assert old is not None and old.status == "superseded"


def test_proposed_does_not_supersede_current(db_path: Path) -> None:
    live = tenets.record_tenet(
        body_md="Let winning theses run.", scope_key="exit-discipline", db_path=db_path
    )
    prop = tenets.record_tenet(
        body_md="Trim winners on a doubling.",
        scope_key="exit-discipline",
        status="proposed",
        source_note_ids=[1],
        tension_with=live.id,
        db_path=db_path,
    )
    # the standing belief stands; the proposal waits
    assert prop.status == "proposed"
    assert [c.id for c in tenets.list_tenets(status="current", db_path=db_path)] == [live.id]
    assert prop.meta.get("tensions") == [live.id]


def test_approve_promotes_and_supersedes(db_path: Path) -> None:
    live = tenets.record_tenet(
        body_md="Let winners run.", scope_key="exit-discipline", db_path=db_path
    )
    prop = tenets.record_tenet(
        body_md="Trim winners on a doubling.",
        scope_key="exit-discipline",
        status="proposed",
        source_note_ids=[1],
        db_path=db_path,
    )
    approved = tenets.approve_tenet(prop.id, db_path=db_path)
    assert approved is not None and approved.status == "current"
    # the standing belief is superseded; only the approved revision is current
    assert get_insight(live.id, db_path=db_path).status == "superseded"  # type: ignore[union-attr]
    assert [c.id for c in tenets.list_tenets(status="current", db_path=db_path)] == [prop.id]


def test_approve_non_proposed_is_noop(db_path: Path) -> None:
    live = tenets.record_tenet(body_md="Circle of competence first.", db_path=db_path)
    assert tenets.approve_tenet(live.id, db_path=db_path) is None  # already current
    assert tenets.approve_tenet(999999, db_path=db_path) is None  # missing


def test_reject_retires_proposed(db_path: Path) -> None:
    prop = tenets.record_tenet(
        body_md="Chase momentum.", status="proposed", source_note_ids=[1], db_path=db_path
    )
    assert tenets.reject_tenet(prop.id, db_path=db_path) is True
    assert get_insight(prop.id, db_path=db_path).status == "rejected"  # type: ignore[union-attr]
    # idempotent — a second reject finds nothing proposed
    assert tenets.reject_tenet(prop.id, db_path=db_path) is False
    assert tenets.list_tenets(status="current", db_path=db_path) == []


def test_supersede_tenet_revises(db_path: Path) -> None:
    a = tenets.record_tenet(body_md="Never average down.", db_path=db_path)
    b = tenets.supersede_tenet(
        a.id, body_md="Average down only on a re-underwritten thesis.", db_path=db_path
    )
    assert b is not None and b.scope_key == a.scope_key and b.status == "current"
    assert get_insight(a.id, db_path=db_path).status == "superseded"  # type: ignore[union-attr]


def test_empty_body_rejected(db_path: Path) -> None:
    with pytest.raises(ValueError, match="non-empty"):
        tenets.record_tenet(body_md="   ", db_path=db_path)


def test_current_tenet_for_scope(db_path: Path) -> None:
    assert tenets.current_tenet_for_scope("tenet:exit-discipline", db_path=db_path) is None
    t = tenets.record_tenet(
        body_md="Let winners run.", scope_key="exit-discipline", db_path=db_path
    )
    found = tenets.current_tenet_for_scope("tenet:exit-discipline", db_path=db_path)
    assert found is not None and found.id == t.id
