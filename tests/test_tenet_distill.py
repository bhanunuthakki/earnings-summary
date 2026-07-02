"""The Worldview distiller (P2) — owner-flagged musings → proposed Tenets.

Deterministic $0 triage (only flagged, not-yet-distilled musings reach the model),
citation grounding, tension detection, dedup on re-run, and degrade-safety — all
with an injected call (no CLI).
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest
from alembic.config import Config

from alembic import command
from synthesis.tenet_distill import ProposedTenet, run_tenet_distill
from synthesis.tenets import list_tenets, record_tenet
from user_state.notes import AnalystNoteRow, create_note

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


def _flagged(db_path: Path, text: str, ladder: str = "saved") -> int:
    return create_note(
        ticker=None,
        kind="musing",
        body=text,
        source="capture",
        context={"ledger": "musing", "ladder": ladder},
        db_path=db_path,
    ).id


def _unflagged(db_path: Path, text: str) -> int:
    return create_note(
        ticker=None,
        kind="musing",
        body=text,
        source="capture",
        context={"ledger": "musing"},
        db_path=db_path,
    ).id


def _propose_all(musings: Sequence[AnalystNoteRow]) -> list[ProposedTenet]:
    return [
        {
            "tenet": "I sell my winners too early.",
            "scope_key": "exit-discipline",
            "citations": [m.id for m in musings],
        }
    ]


def test_distill_proposes_from_flagged_only(db_path: Path) -> None:
    a = _flagged(db_path, "sold NVDA too early, still regret it")
    b = _flagged(db_path, "trimmed MELI at the first double; it ran 3x more", ladder="incorporated")
    _unflagged(db_path, "the macro tape feels off")  # not flagged → not a candidate
    counts = run_tenet_distill(db_path, call=_propose_all)
    assert counts["candidates"] == 2
    assert counts["proposed"] == 1
    props = list_tenets(status="proposed", db_path=db_path)
    assert len(props) == 1
    assert set(props[0].source_note_ids) == {a, b}
    assert props[0].status == "proposed"
    # nothing became current without approval
    assert list_tenets(status="current", db_path=db_path) == []


def test_distill_zero_cost_when_nothing_flagged(db_path: Path) -> None:
    _unflagged(db_path, "just thinking out loud")
    calls = {"n": 0}

    def spy(musings: Sequence[AnalystNoteRow]) -> list[ProposedTenet]:
        calls["n"] += 1
        return []

    counts = run_tenet_distill(db_path, call=spy)
    assert counts["candidates"] == 0
    assert calls["n"] == 0  # deterministic $0 short-circuit — the model never ran


def test_distill_skips_groundless(db_path: Path) -> None:
    _flagged(db_path, "a flagged thought")

    def groundless(musings: Sequence[AnalystNoteRow]) -> list[ProposedTenet]:
        return [{"tenet": "a belief with no support", "scope_key": "foo", "citations": []}]

    counts = run_tenet_distill(db_path, call=groundless)
    assert counts["proposed"] == 0
    assert counts["skipped_groundless"] == 1


def test_distill_drops_foreign_citations(db_path: Path) -> None:
    a = _flagged(db_path, "a flagged thought")

    def call(musings: Sequence[AnalystNoteRow]) -> list[ProposedTenet]:
        return [{"tenet": "belief", "scope_key": "s", "citations": [a, 999999]}]

    run_tenet_distill(db_path, call=call)
    props = list_tenets(status="proposed", db_path=db_path)
    assert props[0].source_note_ids == [a]  # the id it wasn't given is dropped


def test_distill_flags_tension_with_standing_tenet(db_path: Path) -> None:
    live = record_tenet(
        body_md="Let winning theses run.", scope_key="exit-discipline", db_path=db_path
    )
    a = _flagged(db_path, "maybe I should trim winners after a double")

    def call(musings: Sequence[AnalystNoteRow]) -> list[ProposedTenet]:
        return [
            {"tenet": "Trim winners on a double.", "scope_key": "exit-discipline", "citations": [a]}
        ]

    counts = run_tenet_distill(db_path, call=call)
    assert counts["tensions"] == 1
    props = list_tenets(status="proposed", db_path=db_path)
    assert props[0].meta.get("tensions") == [live.id]


def test_distill_dedups_already_cited_on_rerun(db_path: Path) -> None:
    _flagged(db_path, "a flagged thought")
    run_tenet_distill(db_path, call=_propose_all)  # proposes, citing the musing
    # second run: the musing is now cited by a proposed Tenet → no longer a candidate
    counts = run_tenet_distill(db_path, call=_propose_all)
    assert counts["candidates"] == 0


def test_distill_degrades_on_failure(db_path: Path) -> None:
    _flagged(db_path, "a flagged thought")

    def boom(musings: Sequence[AnalystNoteRow]) -> list[ProposedTenet]:
        raise RuntimeError("llm down")

    counts = run_tenet_distill(db_path, call=boom)  # never raises
    assert counts["proposed"] == 0
    assert list_tenets(status="proposed", db_path=db_path) == []


def test_distill_handles_none_result(db_path: Path) -> None:
    _flagged(db_path, "a flagged thought")
    counts = run_tenet_distill(db_path, call=lambda musings: None)
    assert counts["proposed"] == 0
