"""Day-one Ledger seeding — deterministic seed.json ingest + theme clustering."""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from pathlib import Path

import pytest

from capture import ingest
from capture.matcher import build_roster_index
from synthesis import insights, seed
from user_state import notes
from user_state.notes import AnalystNoteRow

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PRIOR_HEAD = "0059_kpi_facts_restatement"
ROSTER = build_roster_index(symbols=["NU"], phrases={"nubank": "NU"})

_SEED = {
    "musings": [
        {"ticker": "NU", "body": "Nubank credit cycle still early", "approx_date": "2025-11"}
    ],
    "decisions": [
        {
            "ticker": "MELI",
            "action": "add",
            "rationale": "cheap on FCF after the selloff",
            "conviction": "high",
            "falsifier": "take rate compresses",
            "approx_date": "2025-09",
        }
    ],
    "themes": [
        {
            "slug": "latam-fintech",
            "title": "LatAm fintech",
            "description": "NU + MELI as structural LatAm winners",
            "tickers": ["NU", "MELI"],
        }
    ],
}


@pytest.fixture
def db_path(tmp_path: Path, migrated_db: Callable[..., Path]) -> Path:
    return migrated_db(tmp_path / "ledger.db", stamp=PRIOR_HEAD)


def _write_seed(tmp_path: Path) -> Path:
    p = tmp_path / "seed.json"
    p.write_text(json.dumps(_SEED), encoding="utf-8")
    return p


def test_seed_from_json_ingests_all(db_path: Path, tmp_path: Path) -> None:
    counts = seed.seed_from_json(db_path, _write_seed(tmp_path))
    assert counts["musings"] == 1
    assert counts["decisions"] == 1
    assert counts["themes"] == 1
    musings = notes.list_notes(kind="musing", db_path=db_path)
    assert len(musings) == 1 and musings[0].ticker == "NU"
    decisions = notes.list_notes(kind="decision", db_path=db_path)
    assert any("MELI" in d.body for d in decisions)
    themes = insights.list_insights(kind="theme", db_path=db_path)
    assert len(themes) == 1
    assert themes[0].scope_key == "theme:latam-fintech"
    assert themes[0].meta.get("title") == "LatAm fintech"
    assert themes[0].provenance == "owner"


def test_seed_from_json_is_idempotent(db_path: Path, tmp_path: Path) -> None:
    path = _write_seed(tmp_path)
    seed.seed_from_json(db_path, path)
    counts = seed.seed_from_json(db_path, path)
    assert counts["musings"] == 0
    assert counts["skipped"] >= 2  # musing + decision already present
    assert len(notes.list_notes(kind="musing", db_path=db_path)) == 1


def test_seed_from_json_missing_file(db_path: Path, tmp_path: Path) -> None:
    counts = seed.seed_from_json(db_path, tmp_path / "absent.json")
    assert counts == {"musings": 0, "decisions": 0, "themes": 0, "skipped": 0}


def test_seed_cluster_themes(db_path: Path) -> None:
    ingest.ingest_capture(
        channel="tray", text="Nubank credit cycle worry", roster=ROSTER, db_path=db_path
    )
    ids = sorted(n.id for n in notes.list_notes(kind="musing", db_path=db_path))

    def fake(musings: Sequence[AnalystNoteRow]) -> list[dict[str, object]]:
        return [
            {
                "slug": "credit-cycle",
                "title": "Credit cycle",
                "description": "watching NPL formation",
                "tickers": ["NU"],
                "note_ids": [m.id for m in musings],
            }
        ]

    counts = seed.seed_cluster_themes(db_path, call=fake)
    assert counts["clustered"] == 1
    themes = insights.list_insights(kind="theme", scope_key="theme:credit-cycle", db_path=db_path)
    assert len(themes) == 1
    assert themes[0].source_note_ids == ids
