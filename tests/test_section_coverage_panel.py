"""Governance → Coverage panel (P4.2): the visible counterpart of
hide-don't-stub. Pins the matrix's cell semantics (filled / empty / n-a /
no-report), the list scoping (index members excluded), the suppression
passthrough, and the rendered fragment."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from pipeline.section_coverage_panel import (
    EMPTY,
    FILLED,
    NO_REPORT,
    SUPPRESSED,
    load_section_coverage,
    render_section_coverage_panel,
)


def _seed_db(db: Path) -> None:
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE tracked_companies ("
        " ticker TEXT, list_type TEXT, user_id TEXT, archived_at TEXT)"
    )
    conn.executemany(
        "INSERT INTO tracked_companies (ticker, list_type, user_id, archived_at)"
        " VALUES (?, ?, ?, ?)",
        [
            ("AAA", "portfolio", "u1", None),
            ("BBB", "evaluation", "u1", None),
            ("ZZZ", "index_member", "u1", None),  # excluded: screening universe
            ("OLD", "portfolio", "u1", "2026-01-01"),  # excluded: archived
            ("OTH", "portfolio", "someone-else", None),  # excluded: other user
        ],
    )
    conn.execute("CREATE TABLE macro_sensitivities (ticker TEXT)")
    conn.execute("INSERT INTO macro_sensitivities VALUES ('AAA')")
    conn.execute("CREATE TABLE decisions (ticker TEXT)")
    conn.execute("INSERT INTO decisions VALUES ('AAA')")
    # strategic_targets / customer_concentrations / lease_commitments /
    # management_commitments tables deliberately absent — best-effort = empty.
    conn.commit()
    conn.close()


def _write_sections_json(repo: Path, ticker: str, date_s: str) -> None:
    payload = {
        "tabs": {
            "earnings": {"status": "ok", "full_quarters": [1], "digest_quarters": []},
            "saydo": {"status": "ok", "cards": []},
            "financials": {"status": "ok", "line_items": [1, 2]},
            "thesis_details": {"status": "ok", "kpi_ledger": [1]},
            "news": {"status": "ok", "content_md": "Material events…"},
            "nuance": {"status": "ok", "failure_modes": [], "most_underweighted": None},
            "company_description": {"status": "ok", "elevator_pitch": "A co."},
            "transcripts": {"status": "ok", "quarters": [1, 2, 3]},
        }
    }
    tdir = repo / "output" / "research" / ticker
    tdir.mkdir(parents=True, exist_ok=True)
    (tdir / f"{date_s}_sections.json").write_text(json.dumps(payload), encoding="utf-8")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / "data").mkdir()
    _seed_db(tmp_path / "data" / "portfolio.db")
    _write_sections_json(tmp_path, "AAA", "2026-06-09")
    return tmp_path


def test_matrix_states_from_sidecar_and_db(repo: Path) -> None:
    rows = load_section_coverage(
        db_path=repo / "data" / "portfolio.db", repo_root=repo, user_id="u1"
    )
    by_ticker = {r.ticker: r for r in rows}
    assert set(by_ticker) == {"AAA", "BBB"}  # index member / archived / other user out

    aaa = by_ticker["AAA"]
    assert aaa.report_date == "2026-06-09"
    assert aaa.cells["earnings"] == FILLED
    assert aaa.cells["saydo"] == EMPTY  # status ok but zero cards = empty
    assert aaa.cells["financials"] == FILLED
    assert aaa.cells["news"] == FILLED
    assert aaa.cells["bear"] == EMPTY
    assert aaa.cells["macro"] == FILLED  # DB probe
    assert aaa.cells["decisions"] == FILLED
    assert aaa.cells["strategic_targets"] == EMPTY  # table absent → best-effort empty

    bbb = by_ticker["BBB"]
    assert bbb.report_date is None
    assert bbb.cells["earnings"] == NO_REPORT
    assert bbb.cells["macro"] == EMPTY  # DB probes still answer without a report


def test_suppressed_sections_render_na(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import pipeline.section_coverage_panel as scp

    monkeypatch.setattr(
        scp,
        "suppressed_sections_for_ticker",
        lambda ticker, root: frozenset({"lease_ladder"}) if ticker == "AAA" else frozenset(),
    )
    rows = load_section_coverage(
        db_path=repo / "data" / "portfolio.db", repo_root=repo, user_id="u1"
    )
    aaa = next(r for r in rows if r.ticker == "AAA")
    assert aaa.cells["lease_ladder"] == SUPPRESSED
    assert aaa.gap_count == sum(1 for v in aaa.cells.values() if v == EMPTY)


def test_sort_portfolio_first_then_gaps(repo: Path) -> None:
    rows = load_section_coverage(
        db_path=repo / "data" / "portfolio.db", repo_root=repo, user_id="u1"
    )
    assert [r.list_type for r in rows] == ["portfolio", "evaluation"]


def test_render_fragment_contains_matrix_and_legend(repo: Path) -> None:
    html = render_section_coverage_panel(repo / "data" / "portfolio.db", repo, user_id="u1")
    assert "Section coverage" in html
    assert "AAA" in html and "BBB" in html
    assert "sc-table" in html
    assert "tracked names" in html
    assert "hide-don't-stub" in html
    # Cell glyphs present for both filled and empty states.
    assert "sc-filled" in html and "sc-empty" in html


def test_missing_db_renders_note(tmp_path: Path) -> None:
    html = render_section_coverage_panel(tmp_path / "nope.db", tmp_path, user_id="u1")
    assert "Section coverage" in html
    assert "No tracked names" in html
