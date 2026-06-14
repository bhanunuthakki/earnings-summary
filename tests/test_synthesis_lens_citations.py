# pyright: reportPrivateUsage=false
"""Static-prose citations — the producer + render wiring (Close the Loops L12).

PR1 gave ``render_prose`` the ability to linkify ``[n]`` against a citations
payload. This pins the PR2 wiring that makes the synthesis lenses actually
PRODUCE numbered evidence and PASS a payload, so a lens memo's ``[n]`` markers
resolve to the same source documents the fact cells cite:

1. ``mgmt_credibility_score`` numbers ONLY its source-doc-backed commitments,
   in render order, and stores those doc ids as ordered ``source_doc_ids`` —
   so the memo's ``[n]`` line up 1:1 with the citation payload at render time.
2. ``report.sections.synthesis`` resolves a citing lens's ``source_doc_ids`` to
   ``ui.cite_marks`` chips, gated on the memo actually carrying ``[n]``.
3. the synthesis renderer hands those chips to ``render_prose`` so the static
   prose carries clickable, evidence-resolving cite marks.
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"
sys.path.insert(0, str(SRC))

import pytest  # noqa: E402

from report.models import SectionStatus, SynthesisLensRow, SynthesisSection  # noqa: E402
from report.renderers.workspace_sections.synthesis import _synthesis_tab  # noqa: E402
from report.sections import synthesis as synth_section  # noqa: E402
from synthesis.lenses.mgmt_credibility_score import (  # noqa: E402
    _ctx_mgmt_credibility,
    _format_commitments,
)

_PRED_COLS = (
    "ticker, source_kind, made_at, target_period, prediction_md, outcome, "
    "realized_value, target_value, notes, source_doc_id"
)


def _rows(records: list[dict[str, object]]) -> list[sqlite3.Row]:
    """Build real sqlite3.Row objects (what the loader passes _format_commitments)."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE p (made_at TEXT, target_period TEXT, prediction_md TEXT, "
        "outcome TEXT, realized_value REAL, target_value REAL, notes TEXT, source_doc_id INTEGER)"
    )
    for r in records:
        conn.execute(
            "INSERT INTO p (made_at, target_period, prediction_md, outcome, source_doc_id) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                r.get("made_at"),
                r.get("target_period"),
                r.get("prediction_md"),
                r.get("outcome"),
                r.get("source_doc_id"),
            ),
        )
    return conn.execute("SELECT * FROM p ORDER BY rowid").fetchall()


def _predictions_db(tmp_path: Path, records: list[dict[str, object]]) -> Path:
    """A temp repo_root whose portfolio.db has a predictions table + records."""
    db_dir = tmp_path / "data"
    db_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_dir / "portfolio.db"))
    conn.execute(
        "CREATE TABLE predictions (id INTEGER PRIMARY KEY, ticker TEXT, source_kind TEXT, "
        "made_at TEXT, target_period TEXT, prediction_md TEXT, outcome TEXT, "
        "realized_value REAL, target_value REAL, notes TEXT, source_doc_id INTEGER)"
    )
    for r in records:
        conn.execute(
            f"INSERT INTO predictions ({_PRED_COLS}) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                r.get("ticker", "NVDA"),
                r.get("source_kind", "mgmt_commitment"),
                r.get("made_at"),
                r.get("target_period"),
                r.get("prediction_md"),
                r.get("outcome"),
                r.get("realized_value"),
                r.get("target_value"),
                r.get("notes"),
                r.get("source_doc_id"),
            ),
        )
    conn.commit()
    conn.close()
    return tmp_path


# ---------------------------------------------------------------------------
# 1. _format_commitments — numbers ONLY doc-backed commitments, in order.
# ---------------------------------------------------------------------------


def test_format_commitments_numbers_only_doc_backed_in_order() -> None:
    rows = _rows(
        [
            {
                "made_at": "2025-08-01",
                "outcome": "met",
                "prediction_md": "25% margins",
                "source_doc_id": 42,
            },
            {
                "made_at": "2025-06-01",
                "outcome": "missed",
                "prediction_md": "no doc here",
                "source_doc_id": None,
            },
            {
                "made_at": "2025-04-01",
                "outcome": "mixed",
                "prediction_md": "double rev",
                "source_doc_id": 43,
            },
        ]
    )
    block, doc_ids = _format_commitments(rows)
    # Ordered doc ids skip the un-sourced row; markers number 1, 2 in render order.
    assert doc_ids == [42, 43]
    assert "[1] 2025-08-01" in block
    assert "[2] 2025-04-01" in block
    # The un-sourced commitment carries NO marker (the LLM can't cite a number
    # that wouldn't resolve).
    assert "[3]" not in block
    assert "no doc here" in block
    no_marker_line = next(ln for ln in block.splitlines() if "no doc here" in ln)
    assert "[" not in no_marker_line


def test_format_commitments_empty_is_safe() -> None:
    block, doc_ids = _format_commitments([])
    assert doc_ids == []
    assert block == "(no recent commitments)"


# ---------------------------------------------------------------------------
# 2. _ctx_mgmt_credibility — the loader sets ordered source_doc_ids + the rule.
# ---------------------------------------------------------------------------


def test_loader_emits_ordered_source_docs_and_citation_rule(tmp_path: Path) -> None:
    repo = _predictions_db(
        tmp_path,
        [
            {
                "made_at": "2025-08-01",
                "outcome": "met",
                "prediction_md": "guided 25%",
                "source_doc_id": 42,
            },
            {
                "made_at": "2025-04-01",
                "outcome": "missed",
                "prediction_md": "guided 40%",
                "source_doc_id": 43,
            },
        ],
    )
    ctx = _ctx_mgmt_credibility("NVDA", repo)
    assert ctx is not None
    # made_at DESC → 42 (Aug) before 43 (Apr); markers align with that order.
    assert ctx.source_doc_ids == [42, 43]
    assert "[1]" in ctx.template_kwargs["recent_commitments"]
    assert "[2]" in ctx.template_kwargs["recent_commitments"]
    assert ctx.template_kwargs["citation_rule"].strip() != ""


def test_loader_without_source_docs_stays_uncited(tmp_path: Path) -> None:
    repo = _predictions_db(
        tmp_path,
        [
            {
                "made_at": "2025-08-01",
                "outcome": "met",
                "prediction_md": "guided 25%",
                "source_doc_id": None,
            }
        ],
    )
    ctx = _ctx_mgmt_credibility("NVDA", repo)
    assert ctx is not None
    assert ctx.source_doc_ids == []
    assert ctx.template_kwargs["citation_rule"] == ""
    assert "[1]" not in ctx.template_kwargs["recent_commitments"]


# ---------------------------------------------------------------------------
# 3. _citations_for_source_docs — doc ids → chip payloads.
# ---------------------------------------------------------------------------


def test_citations_resolver_builds_aligned_chips() -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE documents (id INTEGER PRIMARY KEY, doc_type TEXT, "
        "period_end TEXT, source_url TEXT)"
    )
    conn.execute("INSERT INTO documents VALUES (42, '10-K', '2024-12-31', 'https://sec.gov/x')")
    # doc 99 deliberately absent → a link-less slot that keeps the numbering.
    items = synth_section._citations_for_source_docs(conn, [42, 99])
    assert items[0]["n"] == 1
    assert items[0]["kind"] == "10-K"
    assert items[0]["label"] == "10-K · 2024-12-31"
    assert items[0]["href"] == "/source/42"
    assert items[0]["source_url"] == "https://sec.gov/x"
    assert items[1]["n"] == 2
    assert items[1]["label"] == "source document"  # missing row → generic label
    assert items[1]["href"] == "/source/99"


# ---------------------------------------------------------------------------
# 4. report.sections.synthesis.build — the gate (only a citing memo lights up).
# ---------------------------------------------------------------------------


def _documents_db(tmp_path: Path) -> Path:
    db_dir = tmp_path / "data"
    db_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_dir / "portfolio.db"))
    conn.execute(
        "CREATE TABLE documents (id INTEGER PRIMARY KEY, doc_type TEXT, "
        "period_end TEXT, source_url TEXT)"
    )
    conn.execute("INSERT INTO documents VALUES (42, '10-Q', '2025-09-30', NULL)")
    conn.execute("INSERT INTO documents VALUES (43, 'transcript', '2025-06-30', NULL)")
    conn.commit()
    conn.close()
    return tmp_path


def _fake_artifact(content_md: str, source_doc_ids: list[int]) -> SimpleNamespace:
    return SimpleNamespace(
        content_md=content_md,
        model="claude-sonnet-4-6",
        generated_at=datetime(2026, 6, 1, tzinfo=UTC),
        dirty=False,
        source_doc_ids=source_doc_ids,
    )


@pytest.mark.parametrize(
    ("content", "doc_ids", "expect_cites"),
    [
        ("Guided 25% [1] then missed [2].", [42, 43], True),  # cites → payload built
        ("A plain memo with no markers.", [42, 43], False),  # no [n] → gated off
        ("Has [1] marker but no source docs.", [], False),  # no docs → nothing to resolve
    ],
)
def test_build_gate_only_lights_up_a_citing_memo(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    content: str,
    doc_ids: list[int],
    expect_cites: bool,
) -> None:
    repo = _documents_db(tmp_path)
    art = _fake_artifact(content, doc_ids)

    def fake_read_current(*, ticker: str, purpose: str, scope: str, db_path: Path) -> object:
        return art if purpose == "lens:mgmt_credibility_score" else None

    monkeypatch.setattr("llm_artifact_store.read_current", fake_read_current)

    section = synth_section.build("NVDA", repo)
    row = next(r for r in section.lenses if r.name == "mgmt_credibility_score")
    if expect_cites:
        assert len(row.citations) == 2
        assert row.citations[0]["n"] == 1
        assert row.citations[0]["href"] == "/source/42"
    else:
        assert row.citations == ()


# ---------------------------------------------------------------------------
# 5. The synthesis renderer linkifies a citing lens's prose end-to-end.
# ---------------------------------------------------------------------------


def test_synthesis_tab_renders_clickable_cite_marks() -> None:
    row = SynthesisLensRow(
        name="mgmt_credibility_score",
        content_md="Management guided to 25% margins [1] and missed.",
        citations=({"n": 1, "kind": "10-Q", "label": "10-Q · 2025-09-30", "href": "/source/42"},),
    )
    section = SynthesisSection(status=SectionStatus.OK, ticker="NVDA", lenses=[row])
    body = StringIO()
    _synthesis_tab(body, section)
    html = body.getvalue()
    assert 'class="cite-wrap"' in html
    assert 'href="/source/42"' in html
    assert "10-Q · 2025-09-30" in html


def test_synthesis_tab_uncited_lens_is_unchanged() -> None:
    row = SynthesisLensRow(
        name="bull_case",
        content_md="A normal memo, no [1] resolution because citations are empty.",
    )
    section = SynthesisSection(status=SectionStatus.OK, ticker="NVDA", lenses=[row])
    body = StringIO()
    _synthesis_tab(body, section)
    html = body.getvalue()
    assert "cite-wrap" not in html
    assert "[1]" in html  # marker stays literal text
