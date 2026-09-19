"""P5.1 Explore panel + ViewSpec routes on comments_server: the lazy panel
fragment, /api/viewspec/run + /catalog, the /api/views CRUD, and the
saved-view embed fragment.

The DB is built via alembic (stamp the 0078 head, upgrade to head → 0079
creates saved_views), mirroring test_journal_panel.py; the fact tables the
engine reads are raw DDL on top (they live far earlier in the chain than
the stamp point).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import cast

import comments_server
import pytest
from flask.testing import FlaskClient

from pipeline.explore_panel import (
    render_explore_panel,
    render_ranked_workbench,
    render_saved_views_list,
)
from pipeline.research_panel_styles import RESEARCH_PANEL_STYLE
from report.models import CellSource
from tests.ask_stream_support import fold_sse_response
from viewspec.workbench import (
    RankedMetricCandidate,
    RankedMetricRow,
    RankedMetricWorkbench,
    WorkbenchState,
    build_ranked_metric_workbench,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _build_db(
    db_path: Path,
    migrated_db: Callable[..., Path],
    fact_source_document: Callable[[sqlite3.Connection, str], int],
) -> None:
    migrated_db(db_path)
    conn = sqlite3.connect(db_path)
    document_id = fact_source_document(conn, "TST")
    for pe, fpt, v in [
        ("2024-12-31 00:00:00", "Q4", 130.0),
        ("2025-03-31 00:00:00", "Q1", 120.0),
        ("2025-06-30 00:00:00", "Q2", 132.0),
        ("2025-09-30 00:00:00", "Q3", 150.0),
        ("2025-12-31 00:00:00", "Q4", 160.0),
    ]:
        conn.execute(
            "INSERT INTO financial_facts (ticker, period_end, fiscal_period_type, line_item, value, source_doc_id, unit) VALUES ('TST', ?, ?, 'revenue', ?, ?, 'actual')",
            (pe, fpt, v, document_id),
        )
    conn.execute(
        "INSERT INTO tracked_companies (user_id, ticker, name, list_type)"
        " VALUES ('bhanu', 'TST', 'Test Co', 'portfolio')"
    )
    conn.commit()
    conn.close()


@pytest.fixture
def db_path(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    fact_source_document: Callable[[sqlite3.Connection, str], int],
) -> Path:
    db = tmp_path / "data" / "portfolio.db"
    _build_db(db, migrated_db, fact_source_document)
    return db


@pytest.fixture
def client(db_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FlaskClient:
    # This module exercises the legacy panel-routing contract against its
    # intentionally partial pre-grounding fixture.  Grounded-by-default
    # behavior and the current migrated schema are covered separately by
    # test_ask_grounded_default.py.
    monkeypatch.setenv("ASK_RETRIEVAL_MODE", "legacy")
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
    assert "TST" in html_out
    assert 'id="ask-q"' in html_out
    assert "Work with data" in html_out
    assert "Save analysis" in html_out
    assert "/api/viewspec/catalog" in html_out


def test_workbench_grid_keeps_canvas_in_the_live_column_when_rails_are_hidden() -> None:
    """Hidden optional rails must not shift the canvas into a zero-width track."""
    assert ".vx-analysis-canvas { grid-column:3; }" in RESEARCH_PANEL_STYLE
    assert ".vx-fields-rail { grid-column:1; }" in RESEARCH_PANEL_STYLE
    assert ".vx-inspector { grid-column:5; }" in RESEARCH_PANEL_STYLE


def test_explore_panel_keeps_single_ticker_recipes_non_comparative(db_path: Path) -> None:
    html_out = render_explore_panel(db_path)

    assert "TST vs TST" not in html_out
    assert "What changed?" in html_out
    assert "Key risks" in html_out
    assert "Ranked metrics" not in html_out


def test_ranked_workbench_renders_readable_value_trend_and_provenance() -> None:
    html_out = render_ranked_workbench(
        RankedMetricWorkbench(
            state="ready",
            rows=(
                RankedMetricRow(
                    token="fin:operating_cash_flow",
                    label="Operating cash flow",
                    ticker="MELI",
                    rank_source="llm",
                    why="funds investment capacity",
                    value=125.0,
                    prior_value=100.0,
                    change_pct=25.0,
                    unit="millions",
                    as_of="Q2'26",
                    source=CellSource(source="sec_official", doc_id=7),
                ),
            ),
        )
    )

    assert "Ranked metrics" in html_out
    assert "Operating cash flow" in html_out
    assert "125" in html_out and "25.0%" in html_out and "Q2&#x27;26" in html_out
    assert "funds investment capacity" in html_out
    assert "fin:operating_cash_flow" in html_out
    assert "SEC" in html_out


def test_ranked_workbench_drops_unvalidated_render_evidence() -> None:
    html_out = render_ranked_workbench(
        RankedMetricWorkbench(
            state="ready",
            rows=(
                RankedMetricRow(
                    token="fin:operating_cash_flow",
                    label="Operating cash flow",
                    ticker="MELI",
                    rank_source="tier",
                    why="Cash conversion",
                    value=125.0,
                    prior_value=None,
                    change_pct=None,
                    unit="millions",
                    as_of="Q2'26",
                    source=cast(CellSource | None, "legacy source"),
                ),
            ),
        )
    )

    assert "source unavailable" in html_out


def test_ranked_workbench_projects_governed_db_facts_with_current_provenance(
    db_path: Path,
) -> None:
    workbench = build_ranked_metric_workbench(
        db_path,
        ["TST"],
        [RankedMetricCandidate("fin:revenue", "tier", "Core sales signal")],
    )

    assert workbench.state == "ready"
    (row,) = workbench.rows
    assert row.value == 160.0
    assert row.prior_value == 150.0
    assert row.change_pct == pytest.approx(6.67, abs=0.01)
    assert row.as_of == "Q4'25"
    assert row.source is not None
    assert row.source.doc_id == 1
    assert row.source.source_url == "https://example.test/TST/source"

    html_out = render_ranked_workbench(workbench)
    assert "160" in html_out
    assert "6.7%" in html_out
    assert "Q4&#x27;25" in html_out
    assert "FMP" in html_out


@pytest.mark.parametrize("state", ["stale", "unavailable"])
def test_non_ready_ranked_workbench_never_claims_current_facts(
    state: WorkbenchState,
) -> None:
    html_out = render_ranked_workbench(RankedMetricWorkbench(state=state))

    assert "Current governed facts" not in html_out
    assert "vx-workbench-value" not in html_out


def test_explore_panel_has_searchable_tracked_ticker_picker_and_flexible_window(
    db_path: Path,
) -> None:
    html_out = render_explore_panel(db_path)

    assert 'id="vx-tickers" type="hidden" value="TST"' in html_out
    assert 'id="vx-field-search"' in html_out
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
    assert 'id="vx-tickers" type="hidden" value="TST"' in html_out
    assert "<script>" not in html_out


def test_explore_panel_is_narrative_first_with_optional_analytics(db_path: Path) -> None:
    html_out = render_explore_panel(db_path)
    assert ".work-os-explore-title { font-size:var(--fs-display);" in html_out
    assert 'id="ask-thread"' in html_out
    assert 'id="ask-q"' in html_out
    assert "Explore answers in narrative" in html_out
    assert '<dialog class="vx-workbench-page" id="vx-workbench"' in html_out
    assert "← Back to Explore" in html_out
    assert "period, unit, definition, and source provenance" in html_out
    for builder_id in ("vx-run", "vx-field-search", "vx-save", "vx-tickers"):
        assert f'id="{builder_id}"' in html_out


def test_explore_panel_removes_dcf_mutation_and_legacy_copilot_handoff() -> None:
    import inspect

    from pipeline import explore_panel

    src = inspect.getsource(explore_panel)
    assert "Inject as DCF driver" not in src
    assert "/api/dcf/inject" not in src
    assert "DCF relationship" not in src
    assert "openWorkOsCopilot" not in src
    assert "Work with data" in src


def test_explore_workbench_is_fullscreen_and_on_demand(db_path: Path) -> None:
    html_out = render_explore_panel(db_path)
    assert '<dialog class="vx-workbench-page" id="vx-workbench"' in html_out
    assert "function openWorkbench" in html_out
    assert "function closeWorkbench" in html_out
    assert 'id="vx-back"' in html_out
    assert 'class="vx-workbench-compose"' in html_out
    assert ".showModal()" in html_out
    assert "addEventListener('cancel'" in html_out
    assert "workbenchOpener.focus()" in html_out


def test_workbench_preserves_answer_context_peers_and_saved_views(db_path: Path) -> None:
    html_out = render_explore_panel(db_path)
    assert "answer.dataset.question" in html_out
    assert "answer.dataset.contextSpec" in html_out
    assert "context_spec: contextSpec || null" in html_out
    assert "workbenchTickers = Array.isArray(spec.tickers)" in html_out
    assert "function updateWorkbenchTitle()" in html_out
    assert "if (workbenchTickers.length) el('vx-tickers').value" not in html_out
    assert "showWorkbenchError(result.message" in html_out
    assert 'id="vx-workbench-company"' in html_out
    assert "else if (query) compileForWorkbench(query, null)" in html_out
    assert "selected = {};" in html_out
    assert 'id="vx-saved-toggle"' in html_out
    assert 'id="vx-saved-list"' in html_out
    assert 'data-act="load"' in html_out
    assert 'data-act="del"' in html_out


def test_explore_remount_retires_stale_async_owners(db_path: Path) -> None:
    html_out = render_explore_panel(db_path)

    assert "function isCurrent()" in html_out
    assert "if (!isCurrent())" in html_out


def test_workbench_rails_are_adjustable_and_accessible(db_path: Path) -> None:
    html_out = render_explore_panel(db_path)
    assert html_out.count('role="separator"') == 2
    assert 'aria-valuemin="240"' in html_out
    assert 'aria-valuemax="560"' in html_out
    assert "CCState" not in html_out
    assert "localStorage" not in html_out
    assert "handle.addEventListener('keydown'" in html_out


def test_workbench_uses_one_horizontal_searchable_field_band(db_path: Path) -> None:
    html_out = render_explore_panel(db_path)
    assert 'class="vx-fields-band"' in html_out
    assert 'placeholder="Search cached company facts…"' in html_out
    assert 'id="vx-field-catalog"' in html_out
    assert "['fin', 'kpi', 'seg', 'detail']" in html_out
    assert 'id="vx-pick-fin"' not in html_out
    assert "metric.token" in html_out
    assert "replace(/[!'()*]/g" in html_out
    assert 'id="vx-show-all-fields"' in html_out
    assert "Show all '" in html_out
    assert "Could not load the governed field catalog" in html_out


def test_explore_panel_action_buttons_adopt_ccaction(db_path: Path) -> None:
    """CCAction.busy/release/receipt (PR #1092) replaces every bare
    `.disabled = true` / manual textContent-swap action button in this panel:
    compile, run, builder +Peers, inject-to-DCF, add-as-reference, and save."""
    html_out = render_explore_panel(db_path)
    assert html_out.count("CCAction.busy") >= 1
    assert "CCAction.release" in html_out


def test_explore_panel_picker_options_carry_definition_titles(db_path: Path) -> None:
    """Ask v4 definitions: server-rendered picker options get title tooltips
    (the fin glossary here; kpi notes when the table carries them), and the
    JS sets them on catalog reloads too."""
    html_out = render_explore_panel(db_path)
    assert 'id="vx-inspector-definition"' in html_out
    assert "entry.title" in html_out
    assert "data-inspect-metric" in html_out


def test_explore_panel_fields_carry_dynamic_typeahead(db_path: Path) -> None:
    html_out = render_explore_panel(db_path)
    assert 'type="search"' in html_out
    assert "function renderSuggestions" in html_out
    assert "function matches(entry, query)" in html_out
    assert "No matching cached fact" in html_out


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
    assert b"No saved analyses yet" in frag.data


def test_explore_panel_carries_question_suggestion_chips(db_path: Path) -> None:
    html_out = render_explore_panel(db_path)
    assert 'class="explore-suggestions"' in html_out
    assert "data-ask-q" in html_out
    assert "What changed?" in html_out


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
    assert b"cv2-point-label" in res.data
    # The spec object may also arrive bare (no {"spec": ...} wrapper).
    bare = client.post("/api/viewspec/run", json=_SPEC)
    assert bare.status_code == 200


def test_run_endpoint_validates(client: FlaskClient) -> None:
    res = client.post("/api/viewspec/run", json={"spec": {"tickers": [], "metrics": []}})
    assert res.status_code == 400
    err = res.get_json()["error"]
    assert "tickers" in err
    assert "metrics" in err


def test_catalog_endpoint(client: FlaskClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from provenance.financial_fact_resolution import CanonicalFactRelation

    def fake_relation(_conn: sqlite3.Connection, _table: str) -> CanonicalFactRelation:
        return CanonicalFactRelation("financial_facts", "legacy_pre_cutover")

    monkeypatch.setattr(
        "viewspec.engine.canonical_fact_relation",
        fake_relation,
    )
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
    from ask import narrative_transport
    from viewspec import nl_compile
    from viewspec.spec import ViewSpec

    def fake_compile(query: str, **_kw: object) -> nl_compile.NLCompileResult:
        return nl_compile.NLCompileResult(status="ok", spec=ViewSpec.from_dict(_SPEC))

    def fake_llm(prompt: str, *, purpose: str = "ask_answer"):
        yield {"type": "final", "text": "Revenue rose in the latest period."}

    monkeypatch.setattr(nl_compile, "compile_nl_to_viewspec", fake_compile)
    monkeypatch.setattr(narrative_transport, "stream_llm_text", fake_llm)
    res = client.post("/api/ask/stream", json={"query": "/view TST revenue", "tickers": ["TST"]})
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


def test_explore_panel_uses_governed_stream_inline(
    db_path: Path,
) -> None:
    html_out = render_explore_panel(db_path)
    assert "openWorkOsCopilot" not in html_out
    assert "/api/ask/stream" in html_out
