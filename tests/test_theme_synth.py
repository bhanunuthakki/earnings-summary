"""Per-holding stance synthesis — grounding gate, incrementality, degrade."""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from pathlib import Path

import pytest

from capture import ingest
from capture.matcher import build_roster_index
from synthesis import insights, theme_synth
from user_state import notes
from user_state.notes import AnalystNoteRow

PRIOR_HEAD = "0059_kpi_facts_restatement"
ROSTER = build_roster_index(symbols=["NU", "MELI"], phrases={"nubank": "NU"})


@pytest.fixture
def db_path(tmp_path: Path, migrated_db: Callable[..., Path]) -> Path:
    return migrated_db(tmp_path / "ledger.db", stamp=PRIOR_HEAD)


def _seed_nu_musings(db_path: Path) -> None:
    ingest.ingest_capture(
        channel="tray", text="Nubank NPL formation worries me", roster=ROSTER, db_path=db_path
    )
    ingest.ingest_capture(
        channel="tray", text="Nubank credit cycle still looks early", roster=ROSTER, db_path=db_path
    )


def test_synthesis_records_grounded_stance(db_path: Path) -> None:
    _seed_nu_musings(db_path)
    nu_ids = sorted(n.id for n in notes.list_notes(kind="musing", ticker="NU", db_path=db_path))

    def fake(ticker: str, musings: Sequence[AnalystNoteRow]) -> tuple[str, list[int]]:
        return ("Constructive on NU; credit cycle early.", [m.id for m in musings])

    counts = theme_synth.run_synthesis(db_path, call=fake)
    assert counts["scopes"] == 1
    assert counts["synthesized"] == 1
    stances = insights.list_insights(kind="stance", scope_key="NU", db_path=db_path)
    assert len(stances) == 1
    assert "Constructive" in stances[0].body_md
    assert stances[0].source_note_ids == nu_ids


def test_synthesis_skips_unchanged_scope(db_path: Path) -> None:
    _seed_nu_musings(db_path)

    def fake(ticker: str, musings: Sequence[AnalystNoteRow]) -> tuple[str, list[int]]:
        return ("stance", [musings[0].id])

    theme_synth.run_synthesis(db_path, call=fake)
    counts = theme_synth.run_synthesis(db_path, call=fake)
    assert counts["skipped_unchanged"] == 1
    assert counts["synthesized"] == 0


def test_synthesis_rejects_hallucinated_citation(db_path: Path) -> None:
    _seed_nu_musings(db_path)

    def fake(ticker: str, musings: Sequence[AnalystNoteRow]) -> tuple[str, list[int]]:
        return ("I claim you said X", [999999])  # not among the input musings

    counts = theme_synth.run_synthesis(db_path, call=fake)
    assert counts["skipped_groundless"] == 1
    assert counts["synthesized"] == 0
    assert insights.list_insights(kind="stance", scope_key="NU", db_path=db_path) == []


def test_synthesis_degrades_only_for_structured_parse_failure(
    db_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    from llm.structured import StructuredParseError

    _seed_nu_musings(db_path)
    leak_marker = "fixture-credential-material"

    def boom(ticker: str, musings: Sequence[AnalystNoteRow]) -> tuple[str, list[int]]:
        raise StructuredParseError(f"unusable response https://example.test?apikey={leak_marker}")

    with caplog.at_level(logging.WARNING, logger="synthesis.theme_synth"):
        counts = theme_synth.run_synthesis(db_path, call=boom)
    assert counts["failed"] == 1
    assert counts["synthesized"] == 0
    assert "theme_synthesis_scope_degraded" in caplog.text
    assert leak_marker not in caplog.text


def test_synthesis_reraises_unexpected_programming_error(db_path: Path) -> None:
    _seed_nu_musings(db_path)

    def boom(ticker: str, musings: Sequence[AnalystNoteRow]) -> tuple[str, list[int]]:
        raise RuntimeError("unexpected invariant failure")

    with pytest.raises(RuntimeError, match="unexpected invariant"):
        theme_synth.run_synthesis(db_path, call=boom)


def test_synthesis_re_runs_after_new_musing(db_path: Path) -> None:
    _seed_nu_musings(db_path)

    def fake_v1(ticker: str, musings: Sequence[AnalystNoteRow]) -> tuple[str, list[int]]:
        return ("stance v1", [musings[0].id])

    theme_synth.run_synthesis(db_path, call=fake_v1)
    ingest.ingest_capture(
        channel="tray", text="Nubank guidance reassures me", roster=ROSTER, db_path=db_path
    )

    def fake_v2(ticker: str, musings: Sequence[AnalystNoteRow]) -> tuple[str, list[int]]:
        return ("stance v2", [musings[-1].id])

    counts = theme_synth.run_synthesis(db_path, call=fake_v2)
    assert counts["synthesized"] == 1
    current = insights.list_insights(kind="stance", scope_key="NU", db_path=db_path)
    assert current[0].body_md == "stance v2"
