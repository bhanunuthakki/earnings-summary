"""S11 Triage surface: the `needs_triage` reader/router in user_state.notes,
the Companies → Triage panel, and the /api/notes/<id>/route disposition.

The comment classifier is closed under no-fit (Instrument Paradigm §1): a
directive it can't map parks at `needs_triage` and mirrors into analyst_notes as
a source='comment' / kind='question' note whose context_json['intent'] ==
'needs_triage'. This panel is the dedicated lens over those parked items. The DB
is built via alembic (stamp a pre-0074 head, upgrade to head), mirroring
test_journal_panel.py.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from datetime import date
from pathlib import Path

import pytest
from flask.testing import FlaskClient

from user_state.notes import (
    ROUTABLE_INTENTS,
    create_note,
    get_note,
    list_triage_notes,
    route_triage_note,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "execution"))

import comments_server  # noqa: E402

_PRIOR_HEAD = "0059_kpi_facts_restatement"


@pytest.fixture
def db_path(tmp_path: Path, migrated_db: Callable[..., Path]) -> Path:
    return migrated_db(tmp_path / "data" / "portfolio.db", stamp=_PRIOR_HEAD)


@pytest.fixture
def client(db_path: Path, tmp_path: Path) -> FlaskClient:
    assert db_path.exists()
    return comments_server.create_app(tmp_path).test_client()


def _park(db_path: Path, *, ticker: str | None, body: str, comment_id: str, **anchor: str) -> int:
    """Seed a parked needs_triage note exactly as sync_store_comments would
    mirror one (source='comment', kind='question', intent watermark in context)."""
    rd = "2026-06-10"
    note = create_note(
        ticker=ticker,
        kind="question",
        body=body,
        anchor_type=anchor.get("anchor_type"),
        anchor_key=anchor.get("anchor_key"),
        source="comment",
        source_ref=f"{(ticker or 'PORTFOLIO')}/{rd}/{comment_id}",
        context={"report_date": rd, "intent": "needs_triage", "tab": anchor.get("tab", "thesis")},
        db_path=db_path,
    )
    return note.id


# ----------------------------------------------------------------------------
# Reader: list_triage_notes
# ----------------------------------------------------------------------------


def test_list_triage_notes_filters_to_the_terminal(db_path: Path) -> None:
    parked = _park(
        db_path, ticker="NU", body="Remove bank peers unless better comps.", comment_id="cmt_a"
    )
    # A plain question note (no triage intent) must NOT appear.
    create_note(
        ticker="NU",
        kind="question",
        body="Ordinary question.",
        source="comment",
        source_ref="NU/2026-06-10/cmt_q",
        context={"report_date": "2026-06-10", "intent": "ask_question"},
        db_path=db_path,
    )
    # A manual note with no context at all must NOT appear.
    create_note(ticker="NU", kind="watch", body="Manual watch.", db_path=db_path)
    triage = list_triage_notes(db_path=db_path)
    assert [n.id for n in triage] == [parked]
    assert triage[0].body == "Remove bank peers unless better comps."


def test_list_triage_notes_excludes_closed(db_path: Path) -> None:
    pid = _park(db_path, ticker="NU", body="x", comment_id="cmt_x")
    assert len(list_triage_notes(db_path=db_path)) == 1
    from user_state.notes import archive_note

    archive_note(pid, db_path=db_path)
    assert list_triage_notes(db_path=db_path) == []


def test_list_triage_notes_degrades_without_schema(tmp_path: Path) -> None:
    # No DB built — a pre-0074 / missing schema yields [], never raises.
    assert list_triage_notes(db_path=tmp_path / "nope.db") == []


# ----------------------------------------------------------------------------
# Router: route_triage_note
# ----------------------------------------------------------------------------


def test_route_triage_note_retypes_and_leaves_queue(db_path: Path) -> None:
    pid = _park(db_path, ticker="NU", body="Drop this stale KPI.", comment_id="cmt_k")
    updated = route_triage_note(pid, intent="drop_kpi", db_path=db_path)
    assert updated is not None
    assert updated.kind == "decision"  # drop_kpi -> decision
    assert (updated.context or {}).get("intent") == "drop_kpi"
    # It is gone from the triage queue (durable note-side write).
    assert list_triage_notes(db_path=db_path) == []


def test_route_triage_note_rejects_non_routable_intent(db_path: Path) -> None:
    pid = _park(db_path, ticker="NU", body="x", comment_id="cmt_y")
    for bad in ("needs_triage", "platform_change", "bogus", ""):
        with pytest.raises(ValueError, match="intent must be one of"):
            route_triage_note(pid, intent=bad, db_path=db_path)


def test_route_triage_note_missing_returns_none(db_path: Path) -> None:
    assert route_triage_note(99999, intent="fix_data", db_path=db_path) is None


# ----------------------------------------------------------------------------
# Panel rendering
# ----------------------------------------------------------------------------


def test_panel_renders_anchor_verbatim_and_dispositions(db_path: Path) -> None:
    from pipeline.triage_panel import render_triage_panel

    _park(
        db_path,
        ticker="NU",
        body="Remove the bank peers unless better fintech comps exist.",
        comment_id="cmt_p",
        anchor_type="peer_comp",
        anchor_key="peer_comp",
    )
    html = render_triage_panel(db_path)
    # One operating band via the S1 kit, not a stacked title/filter band.
    assert "k-toolbar" in html
    assert "Triage" in html
    # Anchor + verbatim text inline.
    assert "peer_comp" in html
    assert "Remove the bank peers unless better fintech comps exist." in html
    # All three dispositions + the drill-in trigger.
    assert 'data-act="route"' in html
    assert 'data-act="resolve"' in html
    assert 'data-act="dismiss"' in html
    assert 'data-act="open"' in html
    # The S4 CCOverlay drill-in surface + every routable intent option.
    assert 'id="triage-drawer"' in html
    assert "k-overlay" in html
    assert "CCOverlay" in html
    for intent in ROUTABLE_INTENTS:
        assert f'value="{intent}"' in html


def test_panel_empty_state(db_path: Path) -> None:
    from pipeline.triage_panel import render_triage_list, render_triage_panel

    assert "Nothing to triage" in render_triage_list(db_path)
    assert "0 open" in render_triage_panel(db_path)


def test_triage_drawer_stays_hidden_until_opened(db_path: Path) -> None:
    """Regression (owner 2026-07-17: "Parked comment cannot be minimized"): the
    drill-in must honour its `hidden` attribute. The kit hides overlays with
    `.k-overlay[hidden]{display:none}` (specificity 0,2,0); a bare
    `#triage-drawer{display:flex}` is an ID rule (1,0,0) that outranks it, so the
    drawer rode open+empty on every load and CCOverlay's close (toggling
    `hidden`) could never dismiss it. The `display` MUST be scoped to
    :not([hidden]) so the hide-when-hidden contract survives the content-sizing
    rule."""
    import re

    from pipeline.triage_panel import render_triage_panel

    html = render_triage_panel(db_path)
    # The element ships closed.
    tag = re.search(r"<div\b[^>]*id=\"triage-drawer\"[^>]*>", html)
    assert tag is not None
    assert " hidden" in tag.group(0)
    # Scan the actual CSS rules, not comment prose: the style block deliberately
    # SPELLS OUT the anti-pattern `#triage-drawer{display:flex}` in a comment, so
    # strip /* … */ first or the rule scan below matches the warning, not a rule.
    css = re.sub(r"/\*.*?\*/", "", html, flags=re.S)
    # `display` only applies when shown…
    assert "#triage-drawer:not([hidden])" in css
    # …and the bare geometry rule must NOT force display (the specificity trap).
    bare = re.search(r"#triage-drawer\s*\{([^}]*)\}", css)
    assert bare is not None
    assert "display" not in bare.group(1)


def test_shell_aliases_triage_into_the_ledger_console() -> None:
    """Phase-5 aggressive IA: Triage is no longer a standalone sub-tab — it
    composes into the single Review → Ledger console (reusing the `musings`
    id) and its old #triage deep-link aliases there. The builder's own
    /api/panel/triage route stays live (see test_panel_route_serves_full_and_list)."""
    from pipeline.command_center_shell import (
        _LEGACY_PANEL_REDIRECTS,  # pyright: ignore[reportPrivateUsage]
        render_shell,
    )

    html = render_shell(overview_html="<p>x</p>")
    # No standalone Triage sub-tab anymore.
    assert 'data-tab-target="triage"' not in html
    # The old deep-link aliases to the reused Ledger (musings) console.
    assert _LEGACY_PANEL_REDIRECTS["triage"] == "musings"
    assert "triage:" in html.replace("'", "")  # mirrored in the SHELL_JS REDIRECTS map


# ----------------------------------------------------------------------------
# Server routes
# ----------------------------------------------------------------------------


def test_panel_route_serves_full_and_list(client: FlaskClient, db_path: Path) -> None:
    _park(db_path, ticker="NU", body="Route-served parked comment.", comment_id="cmt_s")
    full = client.get("/api/panel/triage")
    assert full.status_code == 200
    assert b"Route-served parked comment." in full.data
    assert b"k-toolbar" in full.data
    frag = client.get("/api/panel/triage?fragment=list")
    assert frag.status_code == 200
    assert b"Route-served parked comment." in frag.data
    assert b"k-toolbar" not in frag.data  # list-only fragment


def test_route_action_retypes_and_clears(client: FlaskClient, db_path: Path) -> None:
    pid = _park(db_path, ticker="NU", body="Curate the peers.", comment_id="cmt_r")
    resp = client.post(f"/api/notes/{pid}/route", json={"intent": "curate_peers"})
    assert resp.status_code == 200
    assert resp.get_json()["note"]["kind"] == "decision"
    assert list_triage_notes(db_path=db_path) == []
    # A bad intent is a 400, not a silent no-op.
    pid2 = _park(db_path, ticker="NU", body="x", comment_id="cmt_r2")
    assert client.post(f"/api/notes/{pid2}/route", json={"intent": "bogus"}).status_code == 400


def test_route_mirrors_comment_so_resync_does_not_revert(
    client: FlaskClient, db_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The route handler updates the underlying comment (system-of-record); a
    later re-sync of that report must NOT revert the note back to triage."""
    monkeypatch.setenv("ANALYST_NOTES_SYNC", "1")
    import comments

    rd = date(2026, 6, 10)
    comments.append_comment(
        tmp_path,
        "NU",
        rd,
        anchor=comments.Anchor(type="peer_comp", key="peer_comp", tab="thesis"),
        text="Remove the bank peers unless better fintech comps exist.",
        intent="needs_triage",
    )
    triage = list_triage_notes(db_path=db_path)
    assert len(triage) == 1
    note_id = triage[0].id

    routed = client.post(f"/api/notes/{note_id}/route", json={"intent": "curate_peers"})
    assert routed.status_code == 200
    assert list_triage_notes(db_path=db_path) == []
    # The comment (system-of-record) was updated.
    assert comments.list_comments(tmp_path, "NU", rd)[0].intent == "curate_peers"

    # Touch the report (a new comment) → full re-sync; the routed note stays routed.
    comments.append_comment(
        tmp_path,
        "NU",
        rd,
        anchor=comments.Anchor(type="free_text", key="later"),
        text="A later, unrelated comment.",
    )
    assert list_triage_notes(db_path=db_path) == []
    note = get_note(note_id, db_path=db_path)
    assert note is not None and note.kind == "decision"


def test_triage_js_has_no_window_prompt_and_uses_in_place_editor() -> None:
    """Red-team wave B (B9): Triage's Resolve is the in-place row editor
    (textarea + kit Save/Cancel grown beneath the row / in the drawer), never
    a blocking window.prompt. POST contract unchanged."""
    import inspect

    import pipeline.triage_panel as tp

    src = inspect.getsource(tp)
    assert "window.prompt(" not in src
    assert "beginResolve" in src
    assert "tri-resolve-ta" in src
    assert "data-tri-resolve-save" in src and "data-tri-resolve-cancel" in src
    # POST contract unchanged.
    assert "/resolve'" in src
