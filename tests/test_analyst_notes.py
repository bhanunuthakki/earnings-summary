"""Tests for analyst_notes (alembic 0074): store CRUD, the comment
reconciler, the write-path hook in src/comments.py, and the backfill CLI.

Each test gets a fresh tmp SQLite DB stamped at 0072 and upgraded to head,
so 0073 (tenants + canonical 'bhanu') and 0074 (analyst_notes) run exactly
as alembic creates them in production — the store must agree with the real
schema, not a hand-rolled approximation.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest
from alembic.config import Config

from alembic import command

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "execution"))
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from backfill_analyst_notes import backfill  # noqa: E402

import comments  # noqa: E402
from user_state import notes  # noqa: E402

PRIOR_HEAD = "0072_kpi_reporting_cadence"
RD = date(2026, 6, 1)


def _migrate(db: Path) -> None:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db}")
    command.stamp(cfg, PRIOR_HEAD)
    command.upgrade(cfg, "head")


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    db = tmp_path / "notes.db"
    _migrate(db)
    return db


@pytest.fixture
def repo_root(tmp_path: Path) -> Path:
    """A repo root with NO data/portfolio.db — the write-path hook in
    comments.py must no-op so these tests exercise the explicit sync."""
    return tmp_path / "repo"


def _anchor(type_: str = "thesis_lede", key: str = "thesis_lede") -> comments.Anchor:
    return comments.Anchor(type=type_, key=key)  # pyright: ignore[reportArgumentType]


def _add(
    repo_root: Path,
    text: str,
    *,
    intent: str | None = None,
    anchor: comments.Anchor | None = None,
) -> comments.Comment:
    return comments.append_comment(
        repo_root,
        "NU",
        RD,
        anchor=anchor or _anchor(),
        text=text,
        intent=intent,  # pyright: ignore[reportArgumentType]
    )


def _ref(c: comments.Comment) -> str:
    return f"NU/{RD.isoformat()}/{c.id}"


# ----------------------------------------------------------------------------
# store CRUD
# ----------------------------------------------------------------------------


def test_create_and_list_round_trip(db_path: Path) -> None:
    row = notes.create_note(
        ticker="nu",
        kind="watch",
        body="NPL formation vs 90+ bucket lag",
        anchor_type="kpi_ledger_row",
        anchor_key="NPL ratio",
        db_path=db_path,
    )
    assert row.id > 0
    assert row.user_id == "bhanu"
    assert row.ticker == "NU"  # uppercased on write
    assert row.kind == "watch"
    assert row.status == "open"
    assert row.source == "manual"
    assert row.anchor_key == "NPL ratio"

    portfolio_level = notes.create_note(
        ticker=None,
        kind="assumption",
        body="base case assumes Selic glides below 12%",
        db_path=db_path,
    )
    assert portfolio_level.ticker is None

    nu_only = notes.list_notes(ticker="NU", db_path=db_path)
    assert [r.id for r in nu_only] == [row.id]
    everything = notes.list_notes(db_path=db_path)
    assert {r.id for r in everything} == {row.id, portfolio_level.id}


def test_fact_ref_persists_round_trips_and_inherits(db_path: Path) -> None:
    """The 0099 doorway handle is written, decoded, and inherited on supersede,
    so a note re-binds by stable identity even after the KPI's display name
    changes; a text-keyed anchor degrades to None."""
    row = notes.create_note(
        ticker="NU",
        kind="watch",
        body="NIM vs cost of risk",
        anchor_type="kpi_ledger_row",
        anchor_key="Risk-adjusted NIM",
        fact_ref="kpi:NU:42",
        db_path=db_path,
    )
    assert row.fact_ref == "kpi:NU:42"
    fetched = notes.get_note(row.id, db_path=db_path)
    assert fetched is not None
    assert fetched.fact_ref == "kpi:NU:42"
    # No handle → None (the text-keyed anchor degrade path).
    plain = notes.create_note(ticker="NU", kind="observation", body="x", db_path=db_path)
    assert plain.fact_ref is None
    # Supersede carries the handle forward — the replacement is the same datum.
    superseded = notes.supersede_note(row.id, body="NIM below cost-of-risk floor", db_path=db_path)
    assert superseded.fact_ref == "kpi:NU:42"


def test_sync_mirrors_anchor_fact_ref_onto_note(repo_root: Path, db_path: Path) -> None:
    """A comment whose anchor carries a doorway handle mirrors it onto the
    note (analyst_notes.fact_ref); a text-keyed anchor leaves it None."""
    anchored = comments.Anchor(
        type="kpi_ledger_row",
        key="Risk-adjusted NIM",
        fact_ref="kpi:NU:42",
    )  # pyright: ignore[reportArgumentType]
    c = _add(repo_root, "watch NIM vs cost of risk", anchor=anchored)
    plain = _add(repo_root, "broad observation")  # default thesis_lede anchor, no handle
    notes.sync_store_comments(repo_root, ticker="NU", report_date=RD, db_path=db_path)
    rows = {r.source_ref: r for r in notes.list_notes(ticker="NU", db_path=db_path)}
    assert rows[_ref(c)].fact_ref == "kpi:NU:42"
    assert rows[_ref(plain)].fact_ref is None


def test_vocab_validation(db_path: Path) -> None:
    with pytest.raises(ValueError):
        # An out-of-vocabulary kind is rejected. (Was "musing" until The Ledger
        # capture pipeline made that a first-class NOTE_KIND — use a kind that is
        # still genuinely absent from NOTE_KINDS.)
        notes.create_note(ticker="NU", kind="not_a_real_kind", body="x", db_path=db_path)
    with pytest.raises(ValueError):
        notes.create_note(ticker="NU", kind="watch", body="   ", db_path=db_path)
    with pytest.raises(ValueError):
        notes.create_note(ticker="NU", kind="watch", body="x", source="telepathy", db_path=db_path)
    with pytest.raises(ValueError):
        notes.list_notes(status="pending", db_path=db_path)


def test_resolve_archive_and_default_listing(db_path: Path) -> None:
    a = notes.create_note(ticker="NU", kind="watch", body="A", db_path=db_path)
    b = notes.create_note(ticker="NU", kind="question", body="B", db_path=db_path)

    resolved = notes.resolve_note(a.id, resolution_note="answered by Q1 print", db_path=db_path)
    assert resolved is not None
    assert resolved.status == "resolved"
    assert resolved.resolved_at is not None
    assert resolved.resolution_note == "answered by Q1 print"

    archived = notes.archive_note(b.id, db_path=db_path)
    assert archived is not None
    assert archived.status == "archived"

    # Default listing = live notes: resolved stays visible, archived doesn't.
    live = notes.list_notes(ticker="NU", db_path=db_path)
    assert [r.id for r in live] == [a.id]
    assert [r.id for r in notes.list_notes(status="archived", db_path=db_path)] == [b.id]

    assert notes.resolve_note(424242, db_path=db_path) is None
    assert notes.archive_note(424242, db_path=db_path) is None
    assert notes.reclassify_note(424242, kind="watch", db_path=db_path) is None


def test_supersede_chain(db_path: Path) -> None:
    old = notes.create_note(
        ticker="NU",
        kind="assumption",
        body="ARPAC compounds ~15%/yr",
        anchor_type="kpi_ledger_row",
        anchor_key="ARPAC",
        db_path=db_path,
    )
    new = notes.supersede_note(
        old.id, body="ARPAC compounds 10-12%/yr post-Mexico ramp", db_path=db_path
    )
    assert new.supersedes_id == old.id
    assert (new.ticker, new.anchor_type, new.anchor_key) == ("NU", "kpi_ledger_row", "ARPAC")
    assert new.kind == "assumption"  # inherited
    assert new.status == "open"

    old_after = notes.get_note(old.id, db_path=db_path)
    assert old_after is not None
    assert old_after.status == "superseded"

    # Only the replacement is live.
    assert [r.id for r in notes.list_notes(ticker="NU", db_path=db_path)] == [new.id]

    with pytest.raises(LookupError):
        notes.supersede_note(424242, body="x", db_path=db_path)


# ----------------------------------------------------------------------------
# comment reconciliation
# ----------------------------------------------------------------------------


def test_sync_maps_intents_anchors_and_skips_platform(repo_root: Path, db_path: Path) -> None:
    q = _add(repo_root, "what's driving NPL seasonality?", intent="ask_question")
    d = _add(repo_root, "/thesis flag the FGTS regulation")  # keyword -> edit_thesis
    o = _add(repo_root, "margin bridge looks conservative to me")  # None -> observation
    q2 = _add(repo_root, "is the take-rate guidance sandbagged?")  # None + '?' -> question
    p = _add(repo_root, "fix the sidebar layout", intent="platform_change")

    stats = notes.sync_store_comments(repo_root, ticker="NU", report_date=RD, db_path=db_path)
    assert (stats.created, stats.updated, stats.archived, stats.skipped) == (4, 0, 0, 1)

    rows = {r.source_ref: r for r in notes.list_notes(ticker="NU", db_path=db_path)}
    assert rows[_ref(q)].kind == "question"
    assert rows[_ref(d)].kind == "decision"
    assert rows[_ref(d)].body == "flag the FGTS regulation"  # keyword stripped upstream
    assert rows[_ref(o)].kind == "observation"
    assert rows[_ref(q2)].kind == "question"
    assert _ref(p) not in rows  # backlog, not memory

    n = rows[_ref(q)]
    assert n.source == "comment"
    assert n.anchor_type == "thesis_lede"
    assert n.context is not None
    assert n.context["report_date"] == RD.isoformat()
    assert n.context["comment_status"] == "open"


def test_sync_is_idempotent(repo_root: Path, db_path: Path) -> None:
    _add(repo_root, "watch contribution margin into H2")
    notes.sync_store_comments(repo_root, ticker="NU", report_date=RD, db_path=db_path)
    second = notes.sync_store_comments(repo_root, ticker="NU", report_date=RD, db_path=db_path)
    assert (second.created, second.updated, second.archived) == (0, 0, 0)
    assert len(notes.list_notes(ticker="NU", db_path=db_path)) == 1


def test_sync_propagates_comment_resolution(repo_root: Path, db_path: Path) -> None:
    c = _add(repo_root, "why did opex step up this quarter?", intent="ask_question")
    notes.sync_store_comments(repo_root, ticker="NU", report_date=RD, db_path=db_path)

    comments.update_comment(
        repo_root, "NU", RD, c.id, status="addressed", resolution_note="one-off marketing push"
    )
    stats = notes.sync_store_comments(repo_root, ticker="NU", report_date=RD, db_path=db_path)
    assert stats.updated == 1

    n = next(r for r in notes.list_notes(ticker="NU", db_path=db_path) if r.source_ref == _ref(c))
    assert n.status == "resolved"
    assert n.resolved_at is not None
    assert n.resolution_note == "one-off marketing push"


def test_manual_note_edits_survive_resync(repo_root: Path, db_path: Path) -> None:
    """The watermark contract: when the comment side didn't move, a manual
    reclassify/resolve on the note side must not be reverted by re-syncs."""
    _add(repo_root, "keep an eye on Mexico deposit costs")
    notes.sync_store_comments(repo_root, ticker="NU", report_date=RD, db_path=db_path)
    n = notes.list_notes(ticker="NU", db_path=db_path)[0]

    assert notes.reclassify_note(n.id, kind="watch", db_path=db_path) is not None
    assert notes.resolve_note(n.id, db_path=db_path) is not None

    stats = notes.sync_store_comments(repo_root, ticker="NU", report_date=RD, db_path=db_path)
    assert stats.updated == 0
    n_after = notes.get_note(n.id, db_path=db_path)
    assert n_after is not None
    assert n_after.kind == "watch"
    assert n_after.status == "resolved"


def test_deleted_open_comment_archives_note(repo_root: Path, db_path: Path) -> None:
    c = _add(repo_root, "stale thought")
    notes.sync_store_comments(repo_root, ticker="NU", report_date=RD, db_path=db_path)
    assert comments.delete_comment(repo_root, "NU", RD, c.id) is True

    stats = notes.sync_store_comments(repo_root, ticker="NU", report_date=RD, db_path=db_path)
    assert stats.archived == 1
    archived = notes.list_notes(ticker="NU", status="archived", db_path=db_path)
    assert [r.source_ref for r in archived] == [_ref(c)]


def test_clear_addressed_keeps_resolved_note(repo_root: Path, db_path: Path) -> None:
    c = _add(repo_root, "why is FX drag larger than peers?", intent="ask_question")
    notes.sync_store_comments(repo_root, ticker="NU", report_date=RD, db_path=db_path)
    comments.update_comment(repo_root, "NU", RD, c.id, status="addressed")
    notes.sync_store_comments(repo_root, ticker="NU", report_date=RD, db_path=db_path)

    assert comments.clear_addressed(repo_root, "NU", RD) == 1
    stats = notes.sync_store_comments(repo_root, ticker="NU", report_date=RD, db_path=db_path)
    assert stats.archived == 0  # housekeeping must not erase memory

    n = next(r for r in notes.list_notes(ticker="NU", db_path=db_path) if r.source_ref == _ref(c))
    assert n.status == "resolved"


# ----------------------------------------------------------------------------
# write-path hook (src/comments.py -> analyst_notes)
# ----------------------------------------------------------------------------


def test_write_path_hook_syncs_live(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "data").mkdir(parents=True)
    db = repo / "data" / "portfolio.db"
    _migrate(db)

    c = comments.append_comment(repo, "NU", RD, anchor=_anchor(), text="auto-sync me", intent=None)
    rows = notes.list_notes(ticker="NU", db_path=db)
    assert [r.body for r in rows] == ["auto-sync me"]

    comments.update_comment(repo, "NU", RD, c.id, status="dismissed")
    n = notes.get_note(rows[0].id, db_path=db)
    assert n is not None
    assert n.status == "archived"


def test_hook_noops_without_db(tmp_path: Path) -> None:
    repo = tmp_path / "no_db_repo"
    c = comments.append_comment(repo, "NU", RD, anchor=_anchor(), text="no db here")
    assert c.id  # the comment write itself must succeed untouched


# ----------------------------------------------------------------------------
# backfill CLI
# ----------------------------------------------------------------------------


def test_backfill_walks_all_stores_and_is_idempotent(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    db = tmp_path / "backfill.db"
    _migrate(db)

    comments.append_comment(repo, "NU", RD, anchor=_anchor(), text="nu thought")
    comments.append_comment(repo, "MELI", date(2026, 5, 20), anchor=_anchor(), text="meli thought")
    # A non-date filename must be counted + skipped, not crash the walk.
    junk = repo / "data" / "report_comments" / "NU" / "notes.json"
    junk.write_text("{}", encoding="utf-8")

    result = backfill(repo, db, log=False)
    assert result.files == 2
    assert result.bad_files == 1
    assert result.stats is not None
    assert result.stats.created == 2

    again = backfill(repo, db, log=False)
    assert again.stats is not None
    assert (again.stats.created, again.stats.updated) == (0, 0)

    only_nu = backfill(repo, db, only_ticker="nu", log=False)
    assert only_nu.files == 1
