"""P5.1 Explore panel + ViewSpec routes on comments_server: the lazy panel
fragment, /api/viewspec/run + /catalog, the /api/views CRUD, and the
saved-view embed fragment.

The DB is built via alembic (stamp the 0078 head, upgrade to head → 0079
creates saved_views), mirroring test_journal_panel.py; the fact tables the
engine reads are raw DDL on top (they live far earlier in the chain than
the stamp point).
"""

from __future__ import annotations

import re
import sqlite3
import sys
from pathlib import Path
from typing import cast

import pytest
from alembic.config import Config
from flask.testing import FlaskClient

from alembic import command
from pipeline.explore_panel import render_explore_panel, render_saved_views_list
from tests.ask_stream_support import fold_sse_response

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "execution"))

import comments_server  # noqa: E402

_PRIOR_HEAD = "0078_stance_scores"

_FACTS_DDL = """
CREATE TABLE documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    source_type TEXT NOT NULL,
    doc_type TEXT NOT NULL,
    file_path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    fetched_at TIMESTAMP NOT NULL,
    fetch_status TEXT NOT NULL,
    raw_bytes_size INTEGER NOT NULL DEFAULT 0,
    source_url TEXT,
    source_quality_tier TEXT NOT NULL DEFAULT 'fmp_normalized'
);
CREATE TABLE financial_facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    period_end TIMESTAMP NOT NULL,
    fiscal_period_type TEXT NOT NULL,
    line_item TEXT NOT NULL,
    value TEXT NOT NULL,
    unit TEXT NOT NULL DEFAULT 'actual',
    source_doc_id INTEGER NOT NULL,
    locator TEXT
);
CREATE TABLE tracked_companies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL DEFAULT 'bhanu',
    ticker TEXT NOT NULL,
    name TEXT NOT NULL,
    list_type TEXT NOT NULL,
    archived_at TIMESTAMP
);
"""


def _build_db(db_path: Path) -> None:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.stamp(cfg, _PRIOR_HEAD)
    command.upgrade(cfg, "head")
    conn = sqlite3.connect(db_path)
    conn.executescript(_FACTS_DDL)
    conn.execute(
        "INSERT INTO documents (id, ticker, source_type, doc_type, file_path, sha256,"
        " fetched_at, fetch_status, source_url) VALUES (1, 'TST', 'fmp',"
        " 'fmp_income_statement', 'f.json', 'a', '2026-01-05 10:00:00', 'ok',"
        " 'https://fmp.example/f.json')"
    )
    for pe, fpt, v in [
        ("2024-12-31 00:00:00", "Q4", 130.0),
        ("2025-03-31 00:00:00", "Q1", 120.0),
        ("2025-06-30 00:00:00", "Q2", 132.0),
        ("2025-09-30 00:00:00", "Q3", 150.0),
        ("2025-12-31 00:00:00", "Q4", 160.0),
    ]:
        conn.execute(
            "INSERT INTO financial_facts (ticker, period_end, fiscal_period_type,"
            " line_item, value, source_doc_id) VALUES ('TST', ?, ?, 'revenue', ?, 1)",
            (pe, fpt, v),
        )
    conn.execute(
        "INSERT INTO tracked_companies (user_id, ticker, name, list_type)"
        " VALUES ('bhanu', 'TST', 'Test Co', 'portfolio')"
    )
    conn.commit()
    conn.close()


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    db = tmp_path / "data" / "portfolio.db"
    db.parent.mkdir(parents=True)
    _build_db(db)
    return db


@pytest.fixture
def client(db_path: Path, tmp_path: Path) -> FlaskClient:
    assert db_path.exists()
    return comments_server.create_app(tmp_path).test_client()


_SPEC = {
    "tickers": ["TST"],
    "metrics": ["fin:revenue"],
    "transform": "level",
    "cadence": "quarterly",
    "periods": 8,
}


# ----------------------------------------------------------------------------
# panel fragment + shell registration
# ----------------------------------------------------------------------------


def test_explore_panel_renders_with_default_universe(db_path: Path) -> None:
    html_out = render_explore_panel(db_path)
    assert 'id="vx-root"' in html_out
    # The portfolio ticker pre-fills the universe and the catalog pickers.
    assert "TST" in html_out
    assert 'value="fin:revenue"' in html_out
    assert "Save view" in html_out


def test_explore_panel_has_searchable_tracked_ticker_picker_and_flexible_window(
    db_path: Path,
) -> None:
    html_out = render_explore_panel(db_path)

    assert 'id="vx-tickers"' in html_out
    assert 'id="vx-periods"' in html_out
    assert 'min="1" max="40"' in html_out
    assert "window.initExplorePanel" in html_out
    assert "work-os-explore-tickers" in html_out


def test_work_os_explore_fragment_is_runtime_free_and_seeded_to_requested_ticker(
    client: FlaskClient,
) -> None:
    response = client.get("/api/panel/explore?fragment=work-os&tickers=TST")

    assert response.status_code == 200
    html_out = response.get_data(as_text=True)
    assert 'id="vx-root"' in html_out
    assert 'id="vx-tickers" name="tickers" value="TST"' in html_out
    assert "<script>" not in html_out


def test_explore_panel_is_copilot_handoff_first(db_path: Path) -> None:
    """Prompt controls lead into Copilot; the deterministic builder remains."""
    html_out = render_explore_panel(db_path)
    assert 'id="ask-thread"' in html_out
    assert 'id="ask-q"' in html_out
    assert "ask-chip" in html_out  # suggestion chips seed the empty thread
    assert 'class="ask-advanced ask-builder-pop"' in html_out
    assert "Numeric questions compile into deterministic SQL views" in html_out
    assert "narrative\n questions use cited lexical evidence" in html_out
    assert "period, unit, and source provenance" in html_out
    for builder_id in ("vx-run", "vx-pick-fin", "vx-save", "vx-tickers"):
        assert f'id="{builder_id}"' in html_out


def test_explore_panel_removes_legacy_answer_card_editor() -> None:
    """Facts & Analytics has no second conversation or answer-card actions."""
    import inspect

    from pipeline import explore_panel

    src = inspect.getsource(explore_panel)
    assert "window.prompt(" not in src
    assert "beginSaveView" not in src
    assert "data-ask-act" not in src


def test_explore_panel_builder_is_a_diy_popover(db_path: Path) -> None:
    """The deterministic builder opens from DIY and retains scored peers."""
    html_out = render_explore_panel(db_path)
    assert 'id="ask-diy"' in html_out
    assert 'id="ask-pop-close"' in html_out
    assert 'id="ask-advanced" hidden' in html_out
    assert "<details" not in html_out  # the fold is gone
    assert "function openBuilder()" in html_out
    assert 'id="vx-peers"' in html_out  # builder action
    assert "/api/peers/" in html_out
    assert "function addPeersToCard" not in html_out


def test_builder_popover_uses_ccoverlay_not_a_bespoke_escape_listener(
    db_path: Path,
) -> None:
    """Law-3 fix: the DIY builder popover used to carry the only remaining
    per-surface document-level Escape listener in the shell — one that
    ALWAYS fired regardless of whatever else (a peek, the palette) was open
    on top of it, outside the CCOverlay priority ladder (PALETTE > PEEK >
    DRAWER > DOCK; src/pipeline/cc_overlay.py, design_language.md §3.1).
    It's now a CCOverlay registration instead, so Escape resolves by the
    shared priority stack like every other overlay."""
    html_out = render_explore_panel(db_path)
    # The bespoke listener is gone — CCOverlay owns Escape now.
    assert "document.addEventListener('keydown'" not in html_out
    assert "window.CCOverlay && builderPop && window.CCOverlay.register(builderPop" in html_out
    # closeId auto-wires the (x) — no separate popClose click listener needed.
    assert "closeId: 'ask-pop-close'" in html_out
    assert "popClose" not in html_out
    # Registered scrimless/non-trapping (a lightweight in-panel popover, not
    # a blocking modal): scrim / focus-trap / restore-focus all explicit
    # false, not left to defaults.
    m = re.search(r"window\.CCOverlay\.register\(builderPop,\s*\{([^}]*)\}", html_out)
    assert m, "builderPop CCOverlay.register() call not found"
    opts = " ".join(m.group(1).split())
    assert "scrim: false" in opts
    assert "trapFocus: false" in opts
    assert "restoreFocus: false" in opts
    # NO priority set — it defaults to 0, the one rung BELOW
    # window.CCOverlay.PRIORITY.DOCK (10), so an open peek/palette/drawer/dock
    # always outranks this popover for Escape; it only claims Escape when
    # nothing higher on the ladder is open. This is the "outside/below the
    # ladder" placement the fix calls for.
    assert "priority" not in opts
    assert "PRIORITY.DOCK" not in opts


def test_saved_view_handoff_actually_opens_the_builder(db_path: Path) -> None:
    """Regression: the saved-view palette handoff used to set
    `fold.open = true` on `#ask-advanced` — a plain <div>, not a <details> —
    a dead assignment that never showed the popover (openBuilder() was the
    real show call, used everywhere else). The CCOverlay conversion's
    consumePaletteView() now calls openBuilder() directly."""
    html_out = render_explore_panel(db_path)
    assert "fold.open = true" not in html_out
    consume = html_out[html_out.index("function consumePaletteView") :]
    consume = consume[: consume.index("\n  window.addEventListener('cc-view-id'")]
    assert "openBuilder();" in consume


def test_explore_panel_action_buttons_adopt_ccaction(db_path: Path) -> None:
    """CCAction.busy/release/receipt (PR #1092) replaces every bare
    `.disabled = true` / manual textContent-swap action button in this panel:
    compile, run, builder +Peers, inject-to-DCF, add-as-reference, and save."""
    html_out = render_explore_panel(db_path)
    assert html_out.count("CCAction.busy") >= 6
    assert "CCAction.release" in html_out


def test_explore_panel_picker_options_carry_definition_titles(db_path: Path) -> None:
    """Ask v4 definitions: server-rendered picker options get title tooltips
    (the fin glossary here; kpi notes when the table carries them), and the
    JS sets them on catalog reloads too."""
    html_out = render_explore_panel(db_path)
    assert "FMP-normalized income-statement top line" in html_out  # fin:revenue
    assert "opt.title = e.title" in html_out


def test_explore_panel_pickers_carry_type_ahead_search(db_path: Path) -> None:
    """S5: each picker gets a type-ahead filter + a count readout so a huge
    per-ticker fact list stays usable, and selection is tracked in a token map
    (not the live <option> flags) so a pick survives being filtered out."""
    html_out = render_explore_panel(db_path)
    for dom_id in ("vx-pick-fin", "vx-pick-kpi", "vx-pick-seg"):
        assert f'id="{dom_id}-q"' in html_out  # search input
        assert f'id="{dom_id}-count"' in html_out  # count readout
    assert 'type="search"' in html_out
    assert "function renderPicker" in html_out
    assert "function syncSelection" in html_out
    assert "sel._selected" in html_out  # the map is the source of truth


def test_explore_panel_removes_legacy_dock_thread_handoff(db_path: Path) -> None:
    """The retired dock cannot recreate a second conversation in Explore."""
    html_out = render_explore_panel(db_path)
    assert "CCState.getJSON('askThread')" not in html_out
    assert "function consumeDockThread()" not in html_out


def test_explore_panel_route_and_views_fragment(client: FlaskClient) -> None:
    page = client.get("/api/panel/explore")
    assert page.status_code == 200
    assert b'id="vx-root"' in page.data
    frag = client.get("/api/panel/explore?fragment=views")
    assert frag.status_code == 200
    assert b"No saved views yet" in frag.data


def test_explore_panel_carries_keymetrics_bubble_row(db_path: Path) -> None:
    """The key-metrics preselect bubble row + its inline JS (the new feature):
    the container the JS targets, the prefix-routing toggle, and the refresh-on-
    ticker-change fetch (directives/key_metrics_picker.md)."""
    html_out = render_explore_panel(db_path)
    assert 'id="vx-keymetrics"' in html_out
    # The chip handler routes a token to the right <select> by domain prefix
    # and the row re-fetches itself on a ticker change.
    assert "function kmToggleToken" in html_out
    assert "function refreshKeyMetrics" in html_out
    assert "fragment: 'keymetrics'" in html_out
    assert "data-km-token" in html_out  # the delegated click reads the token


def test_keymetrics_fragment_route(client: FlaskClient) -> None:
    """``?fragment=keymetrics`` is a 200 HTML fragment. TST has no tier-graded
    KPIs or LLM cache, so the row is empty (the container collapses) — the merge
    logic itself is covered in test_key_metrics.py."""
    res = client.get("/api/panel/explore?fragment=keymetrics&tickers=TST")
    assert res.status_code == 200
    assert res.mimetype == "text/html"
    assert res.data == b""


# ----------------------------------------------------------------------------
# /api/viewspec/*
# ----------------------------------------------------------------------------


def test_run_endpoint_returns_fragment(client: FlaskClient) -> None:
    res = client.post("/api/viewspec/run", json={"spec": _SPEC})
    assert res.status_code == 200
    assert b"vx-matrix" in res.data
    assert b"Q4'25" in res.data.replace(b"&#x27;", b"'")
    # The spec object may also arrive bare (no {"spec": ...} wrapper).
    bare = client.post("/api/viewspec/run", json=_SPEC)
    assert bare.status_code == 200


def test_run_endpoint_validates(client: FlaskClient) -> None:
    res = client.post("/api/viewspec/run", json={"spec": {"tickers": [], "metrics": []}})
    assert res.status_code == 400
    err = res.get_json()["error"]
    assert "tickers" in err
    assert "metrics" in err


def test_catalog_endpoint(client: FlaskClient) -> None:
    res = client.get("/api/viewspec/catalog?tickers=TST")
    assert res.status_code == 200
    body = res.get_json()
    entry = next(e for e in body["fin"] if e["token"] == "fin:revenue")
    assert entry["label"] == "revenue"
    assert entry["tickers"] == 1
    assert "top line" in entry["title"]  # definition tooltip (Ask v4)
    assert body["kpi"] == []


# ----------------------------------------------------------------------------
# /api/views CRUD + embed fragment
# ----------------------------------------------------------------------------


def test_views_crud_and_embed(client: FlaskClient, db_path: Path) -> None:
    created = client.post("/api/views", json={"name": "Rev pivot", "spec": _SPEC})
    assert created.status_code == 201
    view = created.get_json()["view"]
    assert view["name"] == "Rev pivot"
    assert view["spec"]["tickers"] == ["TST"]

    listed = client.get("/api/views")
    assert [v["name"] for v in listed.get_json()["views"]] == ["Rev pivot"]

    # Upsert: same name replaces the spec, no second row.
    spec2 = dict(_SPEC, transform="yoy")
    again = client.post("/api/views", json={"name": "Rev pivot", "spec": spec2})
    assert again.status_code == 201
    assert again.get_json()["view"]["id"] == view["id"]
    assert len(client.get("/api/views").get_json()["views"]) == 1

    # The embed hook renders the stored view; ?chart=0 drops the SVG.
    frag = client.get(f"/api/views/{view['id']}/fragment")
    assert frag.status_code == 200
    assert b"vx-matrix" in frag.data
    no_chart = client.get(f"/api/views/{view['id']}/fragment?chart=0")
    assert b"<svg" not in no_chart.data

    # Saved chips render for the panel strip.
    strip = render_saved_views_list(db_path)
    assert "Rev pivot" in strip
    assert "data-spec=" in strip

    deleted = client.delete(f"/api/views/{view['id']}")
    assert deleted.status_code == 200
    assert client.delete(f"/api/views/{view['id']}").status_code == 404
    assert client.get(f"/api/views/{view['id']}/fragment").status_code == 404


def test_views_post_validates(client: FlaskClient) -> None:
    assert client.post("/api/views", json={"spec": _SPEC}).status_code == 400
    bad = client.post("/api/views", json={"name": "x", "spec": {"tickers": ["A"]}})
    assert bad.status_code == 400
    assert "metrics" in bad.get_json()["error"]


# ----------------------------------------------------------------------------
# /api/ask/stream — one Ask-thread turn through the unified engine
# ----------------------------------------------------------------------------


def test_ask_endpoint_compiles_runs_and_renders(
    client: FlaskClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """compile (mocked) → execute → fragment, one round trip; the previous
    spec rides through as refine context."""
    from viewspec import nl_compile
    from viewspec.spec import ViewSpec

    seen: dict[str, object] = {}

    def fake_compile(
        query: str,
        *,
        db_path: Path,
        context_tickers: list[str] | None = None,
        context_spec: dict[str, object] | None = None,
        run_id: str | None = None,
    ) -> nl_compile.NLCompileResult:
        seen["query"] = query
        seen["context_spec"] = context_spec
        return nl_compile.NLCompileResult(status="ok", spec=ViewSpec.from_dict(_SPEC))

    monkeypatch.setattr(nl_compile, "compile_nl_to_viewspec", fake_compile)
    res = client.post(
        "/api/ask/stream",
        json={"query": "TST revenue", "tickers": ["TST"], "context_spec": {"tickers": ["TST"]}},
    )
    assert res.status_code == 200
    body = fold_sse_response(res.get_data(as_text=True))
    assert body["status"] == "ok"
    assert body["kind"] == "view"
    assert seen["query"] == "TST revenue"
    assert seen["context_spec"] == {"tickers": ["TST"]}
    fragment = body["fragment"]
    assert isinstance(fragment, str)
    assert "vx-matrix" in fragment
    spec = body["spec"]
    assert isinstance(spec, dict)
    assert cast("dict[str, object]", spec)["tickers"] == ["TST"]
    message = body["message"]
    assert isinstance(message, str)
    assert "series" in message


def test_ask_stream_redacts_forced_view_compile_failure(
    client: FlaskClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The sole SSE path keeps provider/compiler details behind its generic
    correlated error boundary, including a forced local view compile."""
    from viewspec import nl_compile

    def fake_compile(query: str, **_kw: object) -> nl_compile.NLCompileResult:
        return nl_compile.NLCompileResult(status="error", message="no matching metric token")

    monkeypatch.setattr(nl_compile, "compile_nl_to_viewspec", fake_compile)
    res = client.post("/api/ask/stream", json={"query": "/view garbage"})
    assert res.status_code == 200  # tri-state payload, never a 500
    body = fold_sse_response(res.get_data(as_text=True))
    assert body["status"] == "error"
    assert body["message"] == "chat stream failed; retry the request"
    assert "fragment" not in body


def test_ask_endpoint_answers_narrative_questions(
    client: FlaskClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-metric question routes to the narrative path with the PORTFOLIO
    context pack attached (the one-brain/two-entry-points seam)."""
    from ask import narrative_transport

    prompts: list[str] = []

    def fake_llm(prompt: str, *, purpose: str = "ask_answer"):
        prompts.append(prompt)
        yield {"type": "delta", "text": "prose"}
        yield {"type": "final", "text": "a researched answer"}

    monkeypatch.setattr(narrative_transport, "stream_llm_text", fake_llm)
    res = client.post(
        "/api/ask/stream",
        json={
            "query": "what's the bear case here?",
            "history": [{"role": "user", "text": "earlier question"}],
        },
    )
    assert res.status_code == 200
    body = fold_sse_response(res.get_data(as_text=True))
    assert body["status"] == "ok"
    assert body["kind"] == "narrative"
    assert body["text"] == "a researched answer"
    # The portfolio pack's system context + client history reached the LLM.
    assert "portfolio research assistant" in prompts[0]
    assert "portfolio: TST" in prompts[0]
    assert "[USER] earlier question" in prompts[0]


def test_ask_endpoint_data_question_falls_back_to_narrative(
    client: FlaskClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A data-shaped question whose compile fails still gets answered in
    prose, with the note surfaced in the payload."""
    from ask import narrative_transport
    from viewspec import nl_compile

    def fake_compile(query: str, **_kw: object) -> nl_compile.NLCompileResult:
        return nl_compile.NLCompileResult(status="error", message="nope")

    def fake_llm(prompt: str, *, purpose: str = "ask_answer"):
        yield {"type": "final", "text": "prose fallback"}

    monkeypatch.setattr(nl_compile, "compile_nl_to_viewspec", fake_compile)
    monkeypatch.setattr(narrative_transport, "stream_llm_text", fake_llm)
    res = client.post("/api/ask/stream", json={"query": "TST revenue growth, last 8 quarters"})
    body = fold_sse_response(res.get_data(as_text=True))
    assert body["status"] == "ok"
    assert body["kind"] == "narrative"
    assert body["text"] == "prose fallback"
    note = body["note"]
    assert isinstance(note, str)
    assert "prose" in note


def test_ask_endpoint_runs_commands(client: FlaskClient) -> None:
    """Deterministic commands work from the Ask tab too — no LLM."""
    res = client.post("/api/ask/stream", json={"query": "/help"})
    assert res.status_code == 200
    body = fold_sse_response(res.get_data(as_text=True))
    assert body["status"] == "ok"
    assert body["kind"] == "command"
    text = body["text"]
    assert isinstance(text, str)
    assert "/discovery" in text


def test_ask_endpoint_requires_query(client: FlaskClient) -> None:
    assert client.post("/api/ask/stream", json={}).status_code == 400


# ----------------------------------------------------------------------------
# /api/ask/stream — the SSE sibling (Ask v2 live progress)
# ----------------------------------------------------------------------------


def test_ask_stream_endpoint_streams_data_frames(
    client: FlaskClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The SSE framing keeps stage frames first (the panel's
    busy line), then the fragment and the final message line."""
    from viewspec import nl_compile
    from viewspec.spec import ViewSpec

    def fake_compile(query: str, **_kw: object) -> nl_compile.NLCompileResult:
        return nl_compile.NLCompileResult(status="ok", spec=ViewSpec.from_dict(_SPEC))

    monkeypatch.setattr(nl_compile, "compile_nl_to_viewspec", fake_compile)
    res = client.post("/api/ask/stream", json={"query": "TST revenue", "tickers": ["TST"]})
    assert res.mimetype == "text/event-stream"
    body = res.get_data(as_text=True)
    assert '"stage": "compiling"' in body
    assert '"stage": "running"' in body
    assert '"type": "fragment"' in body
    assert "vx-matrix" in body
    assert '"type": "final"' in body


def test_ask_stream_endpoint_streams_narrative_deltas(
    client: FlaskClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Narrative turns stream their prose incrementally over the same SSE
    channel (the whole point of the streaming sibling)."""
    from ask import narrative_transport

    def fake_llm(prompt: str, *, purpose: str = "ask_answer"):
        yield {"type": "delta", "text": "chunk one "}
        yield {"type": "delta", "text": "chunk two"}
        yield {"type": "final", "text": "chunk one chunk two"}

    monkeypatch.setattr(narrative_transport, "stream_llm_text", fake_llm)
    res = client.post("/api/ask/stream", json={"query": "what should I look at next?"})
    assert res.mimetype == "text/event-stream"
    body = res.get_data(as_text=True)
    assert '"stage": "answering"' in body
    assert "chunk one" in body
    assert '"route": "narrative"' in body


def test_ask_stream_endpoint_requires_query(client: FlaskClient) -> None:
    assert client.post("/api/ask/stream", json={}).status_code == 400
    assert client.open("/api/ask/stream", method="OPTIONS").status_code == 204


def test_explore_panel_hands_research_to_copilot_and_consumes_palette_query(
    db_path: Path,
) -> None:
    """The panel opens Work OS Copilot and still consumes palette handoff."""
    html_out = render_explore_panel(db_path)
    assert "openWorkOsCopilot" in html_out
    assert "/api/ask/stream" not in html_out
    assert "CCState.get('askQ')" in html_out
    assert "'cc-ask-q'" in html_out  # the event registration
    assert "consumePaletteQuery" in html_out
