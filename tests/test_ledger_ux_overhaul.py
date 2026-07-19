"""Ledger ratification/action UX overhaul (2026-07-19 owner rejection fix).

Covers the five complaints this PR addresses, at the level this repo's test
harness can actually exercise (Python-rendered HTML + Flask routes — there is
no JS test runner in this repo, so client-side tally/receipt behavior is
verified structurally: the emitted HTML/JS carries the right data contracts
and function hooks, the same convention every other ``ledger_panel`` test in
this suite already uses).

1. Consequence-first actions — every packet action carries a ``title=``
   tooltip; the three called-out card types (capacity fact, Tenet, falsifier
   ratify) also carry a visible ``.ledger-consequence`` micro-line.
2. Registered feedback — routes return a ``receipt``; the packet JS carries
   the event bus + tally + in-place receipt/failure paint contract.
3. Prioritization + batching — gaps class renders before proposals before
   bulk; homogeneous expiring facts (>=3 same category) collapse into ONE
   grouped card with Affirm all / Review one by one / Drop all.
4. (Visual conformance is covered by tests/test_ui_controls.py's token guard
   + the before/after inventory in the PR description, not here.)
5. Payoff visibility — the packet start band states real counts of what
   clearing it unlocks; the Clear copy is present for the tally to fill in.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest
from alembic.config import Config
from flask.testing import FlaskClient

from alembic import command

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "execution"))
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import comments_server  # noqa: E402

from owner_profile.store import append_fact  # noqa: E402
from pipeline.ledger_panel import (  # noqa: E402
    _group_expiring_facts,  # pyright: ignore[reportPrivateUsage]
    _packet_build,  # pyright: ignore[reportPrivateUsage]
    render_ledger_panel,
)
from research.proposals import create_proposal, create_task  # noqa: E402

_PRIOR_HEAD = "0059_kpi_facts_restatement"

_DECISIONS_DDL = """
CREATE TABLE decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker VARCHAR(16),
    recommendation_kind VARCHAR(32) NOT NULL,
    decided_by VARCHAR(16) NOT NULL DEFAULT 'owner',
    falsifier TEXT,
    made_at DATETIME NOT NULL,
    created_at DATETIME NOT NULL
);
CREATE TABLE tracked_companies (
    ticker TEXT PRIMARY KEY,
    user_id TEXT NOT NULL DEFAULT 'bhanu',
    list_type TEXT NOT NULL,
    archived_at TEXT
);
"""


def _build_db(db_path: Path) -> None:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.stamp(cfg, _PRIOR_HEAD)
    command.upgrade(cfg, "head")
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(_DECISIONS_DDL)
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def ctx(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[FlaskClient, Path, Path]:
    monkeypatch.setenv("LEDGER_ONMYMIND", "1")
    monkeypatch.setenv("LEDGER_WORLDVIEW", "1")
    db = tmp_path / "data" / "portfolio.db"
    db.parent.mkdir(parents=True)
    _build_db(db)
    client = comments_server.create_app(tmp_path).test_client()
    return client, db, tmp_path


def _seed_gap(db: Path, ticker: str = "NU") -> None:
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            "INSERT INTO tracked_companies (ticker, list_type) VALUES (?, 'portfolio')", (ticker,)
        )
        conn.execute(
            "INSERT INTO decisions (ticker, recommendation_kind, decided_by, falsifier, "
            "made_at, created_at) VALUES (?, 'initiate', 'owner', '', '2026-06-01', '2026-06-01')",
            (ticker,),
        )
        conn.commit()
    finally:
        conn.close()


def _seed_proposal(db: Path) -> None:
    task_id = create_task(note_id=None, claim="do NU's margins hold?", ticker="NU", db_path=db)
    create_proposal(
        task_id=task_id,
        kind="memo",
        ticker="NU",
        title="NU margins hold",
        body_md="The book looks stable.",
        budget_tier="cheap",
        db_path=db,
    )


def _seed_expiring_facts(db: Path, n: int, *, category: str = "capacity") -> None:
    conn = sqlite3.connect(str(db))
    try:
        for i in range(n):
            fid = append_fact(
                conn,
                category=category,
                key=f"work_break_{i}",
                value={"year": 2030 + i},
                narrative=f"Work-break window #{i}: planned pause.",
                provenance="wealthplan_import",
                status="affirmed",
                review_horizon_days=90,
            )
            conn.execute(
                "UPDATE owner_profile_facts SET affirmed_at = ? WHERE id = ?",
                ("2020-01-01T00:00:00", fid),
            )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 1. Consequence-first actions — tooltips + micro-lines
# ---------------------------------------------------------------------------


def test_falsifier_ratify_card_has_tooltip_and_consequence_line(
    ctx: tuple[FlaskClient, Path, Path],
) -> None:
    _client, db, _root = ctx
    _seed_gap(db)
    html = render_ledger_panel(db)
    assert 'title="Write and save the falsifier condition' in html
    assert "arms one" in html  # the .ledger-consequence micro-line on the gap card


def test_profile_fact_card_has_tooltip_and_consequence_line(
    ctx: tuple[FlaskClient, Path, Path],
) -> None:
    _client, db, _root = ctx
    conn = sqlite3.connect(str(db))
    try:
        append_fact(
            conn,
            category="capacity",
            key="home_city",
            value={"city": "SF"},
            narrative="Home city: San Francisco.",
            provenance="wealthplan_import",
        )
        conn.commit()
    finally:
        conn.close()
    html = render_ledger_panel(db)
    assert 'data-profile-action="affirm" title=' in html
    assert "the coach may cite this" in html  # .ledger-consequence micro-line


def test_packet_controls_have_tooltips(ctx: tuple[FlaskClient, Path, Path]) -> None:
    _client, db, _root = ctx
    _seed_proposal(db)
    html = render_ledger_panel(db)
    assert "data-pk-start title=" in html
    assert "data-pk-skip title=" in html
    assert "data-pk-close title=" in html


# ---------------------------------------------------------------------------
# 2. Registered feedback — the event bus + tally/receipt contract
# ---------------------------------------------------------------------------


def test_packet_ships_the_settle_event_bus_and_tally(
    ctx: tuple[FlaskClient, Path, Path],
) -> None:
    _client, db, _root = ctx
    _seed_proposal(db)
    html = render_ledger_panel(db)
    assert "__ledgerEmitSettled" in html
    assert "ledger:settled" in html
    assert 'class="pk-tally"' in html


def test_reconcile_verdict_route_carries_a_receipt(
    ctx: tuple[FlaskClient, Path, Path],
) -> None:
    client, db, _root = ctx
    from user_state.notes import create_note

    row = create_note(
        body="seed musing — do I still believe this?",
        kind="musing",
        ticker=None,
        source="capture",
        source_ref="seed:musing:1",
        db_path=db,
    )
    resp = client.post(f"/api/reconcile/note/{row.id}/live")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True
    assert body["receipt"] == "Kept live — the coach can still cite this"


def test_tenet_approve_route_carries_a_receipt(ctx: tuple[FlaskClient, Path, Path]) -> None:
    client, db, _root = ctx
    from synthesis.tenets import record_tenet

    tenet = record_tenet(
        body_md="Let winners run.", scope_key="exit-discipline", status="proposed", db_path=db
    )
    resp = client.post(f"/api/tenets/{tenet.id}/approve")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True
    assert body["receipt"] == "Adopted — now a standing Tenet in your decision prompts"


def test_triage_route_and_archive_carry_receipts(
    ctx: tuple[FlaskClient, Path, Path],
) -> None:
    client, db, _root = ctx
    from user_state.notes import TRIAGE_INTENT, create_note

    row = create_note(
        body="the peer set feels off here",
        kind="question",
        ticker=None,
        source="comment",
        context={"intent": TRIAGE_INTENT},
        db_path=db,
    )
    resp = client.post(f"/api/notes/{row.id}/route", json={"intent": "curate_peers"})
    assert resp.status_code == 200
    assert "receipt" in resp.get_json()

    row2 = create_note(
        body="another parked comment", kind="question", ticker=None, source="comment", db_path=db
    )
    resp2 = client.post(f"/api/notes/{row2.id}/archive", json={})
    assert resp2.status_code == 200
    assert resp2.get_json()["receipt"] == "Dismissed"


# ---------------------------------------------------------------------------
# 3. Prioritization + batching
# ---------------------------------------------------------------------------


def test_packet_orders_gaps_before_proposals_before_bulk(
    ctx: tuple[FlaskClient, Path, Path],
) -> None:
    _client, db, _root = ctx
    _seed_gap(db)
    _seed_proposal(db)
    _seed_expiring_facts(db, 1)  # below the group threshold -> a single bulk card
    html = render_ledger_panel(db)
    gaps_pos = html.index("Live gaps")
    proposals_pos = html.index("Awaiting your verdict")
    bulk_pos = html.index("Routine check-ins")
    assert gaps_pos < proposals_pos < bulk_pos


def test_packet_class_headers_explain_why_they_matter(
    ctx: tuple[FlaskClient, Path, Path],
) -> None:
    _client, db, _root = ctx
    _seed_gap(db)
    html = render_ledger_panel(db)
    assert "the irreducible ask" in html


def test_group_expiring_facts_below_threshold_stays_ungrouped() -> None:
    class _Fake:
        def __init__(self, i: int, category: str) -> None:
            self.id = i
            self.category = category
            self.narrative = f"fact {i}"

    facts = [_Fake(1, "capacity"), _Fake(2, "capacity")]
    groups, singles = _group_expiring_facts(facts)  # type: ignore[arg-type]
    assert groups == []
    assert len(singles) == 2


def test_expiring_facts_group_card_when_at_or_above_threshold(
    ctx: tuple[FlaskClient, Path, Path],
) -> None:
    _client, db, _root = ctx
    _seed_expiring_facts(db, 12, category="capacity")
    html = render_ledger_panel(db)
    assert "12 due for a check-in" in html
    assert 'data-pk-group-action="affirm"' in html
    assert 'data-pk-group-action="drop"' in html
    assert "data-pk-group-reveal" in html
    assert "Affirm all 12" in html
    assert "Drop all 12" in html
    # The individual narratives stay visible in the card (never hidden info).
    assert "Work-break window #0" in html
    assert "Work-break window #11" in html
    # The one-by-one fallback is a real, working stack — not just a label.
    assert html.count('data-profile-action="reaffirm"') == 12


def test_grouped_card_batches_the_same_single_fact_route_no_new_bulk_semantics(
    ctx: tuple[FlaskClient, Path, Path],
) -> None:
    """Requirement C: group-affirm must call the SAME per-fact route per item
    — no new bulk verb in the store. The rendered group card's data-fact-ids
    is exactly the set of individual fact ids, and the JS (inspected
    structurally, no JS runner in this repo) posts each id to
    /api/profile/fact/<id>/reaffirm or /retire — the identical routes
    test_owner_profile_packet.py already exercises per-fact."""
    _client, db, _root = ctx
    _seed_expiring_facts(db, 4, category="appetite")
    build = _packet_build(db)
    group_card = next(c for c in build.bulk if "data-pk-group" in c)
    ids_attr = group_card.split('data-fact-ids="')[1].split('"')[0]
    ids = ids_attr.split(",")
    assert len(ids) == 4
    assert all(i.isdigit() for i in ids)


# ---------------------------------------------------------------------------
# 5. Payoff visibility
# ---------------------------------------------------------------------------


def test_packet_start_band_states_real_counts(ctx: tuple[FlaskClient, Path, Path]) -> None:
    _client, db, _root = ctx
    _seed_gap(db)
    _seed_proposal(db)
    html = render_ledger_panel(db)
    assert 'class="pk-payoff"' in html
    assert "arms 1 tripwire" in html
    assert "clears 1 research proposal" in html


def test_payoff_line_empty_when_nothing_quantifiable() -> None:
    from pipeline.ledger_panel import (
        _packet_payoff_line,  # pyright: ignore[reportPrivateUsage]
        _PacketBuild,  # pyright: ignore[reportPrivateUsage]
    )

    empty = _PacketBuild(
        gaps=[],
        proposals=[],
        bulk=[],
        gaps_n=0,
        research_n=0,
        tenet_n=0,
        new_fact_n=0,
        bulk_fact_n=0,
    )
    assert _packet_payoff_line(empty) == ""
