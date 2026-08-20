"""PR B — per-ticker command-center drill-down.

Covers the artifact inventory (filesystem), the analysis log (DB, defensive),
the assembler + renderer, and the live /ticker/<t> and /api/ticker/<t>
endpoints. The portfolio-tracker DB is intentionally absent in the tmp repo,
so the position strip exercises its graceful "not connected" path.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "execution"))

from identity import DEFAULT_USER_ID  # noqa: E402
from pipeline.analysis_log import build_analysis_log  # noqa: E402
from pipeline.artifact_inventory import build_artifact_inventory  # noqa: E402
from pipeline.ticker_command_center import (  # noqa: E402
    build_holding_rail,
    build_ticker_command_center,
    render_holding_fragment,
    render_holding_picker_band,
    render_notes_drawer_fragment,
    render_ticker_fragment,
    render_ticker_html,
)
from user_state.notes import create_note, resolve_note  # noqa: E402

_HOLDINGS = {
    "ticker": "NU",
    "name": "Nu Holdings",
    "last_updated": "2026-05-10",
    "thesis": "LatAm digital bank; monetization compounding.",
    "verdict": "Pending",
    "tier_1_kpis": [
        {"name": "ROE", "current": "30%", "status": "ok", "break_condition": "<25% for 2Q"}
    ],
    "break_rules": [
        {
            "kpi_name": "ROE",
            "comparator": "lt",
            "threshold": 25,
            "unit": "percent",
            "narrative": "sub-25 breaks",
        }
    ],
    "thesis_breakers_qualitative": ["ROE drifts below 25%"],
}


def _seed_db(db_path: Path) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE tracked_companies (ticker TEXT, name TEXT, list_type TEXT, archived_at TIMESTAMP);
        CREATE TABLE fmp_endpoint_status (ticker TEXT, last_pulled TIMESTAMP);
        CREATE TABLE transcripts (ticker TEXT, period_end TIMESTAMP);
        CREATE TABLE thesis_evaluations (ticker TEXT, evaluated_at TIMESTAMP, overall_status TEXT);
        CREATE TABLE timeseries_signals (ticker TEXT, severity TEXT, computed_at TIMESTAMP);
        CREATE TABLE alerts (id INTEGER PRIMARY KEY, user_id TEXT, ticker TEXT, trigger_kind TEXT,
                             fired_at TEXT, status TEXT, memo_artifact_id INTEGER,
                             evidence_json TEXT, signature_sha TEXT,
                             dismissed_at TEXT, approved_at TEXT);
        CREATE TABLE queued_actions (id INTEGER PRIMARY KEY, alert_id INTEGER, action_kind TEXT,
                                     payload_json TEXT, status TEXT, created_at TEXT,
                                     applied_at TEXT, cancelled_at TEXT);
        CREATE TABLE analyst_notes (id INTEGER PRIMARY KEY, user_id TEXT, ticker TEXT, kind TEXT,
                                    status TEXT, body TEXT, anchor_type TEXT, anchor_key TEXT,
                                    source TEXT, source_ref TEXT, supersedes_id INTEGER,
                                    resolution_note TEXT, context_json TEXT,
                                    created_at TEXT, updated_at TEXT, resolved_at TEXT);
        CREATE TABLE management_commitments (ticker TEXT, outcome TEXT, evaluated_at TIMESTAMP);
        CREATE TABLE dcf_runs (ticker TEXT, valuation_date TEXT, segment_name TEXT, over_under_pct REAL);
        CREATE TABLE llm_calls (ticker TEXT, purpose TEXT, model TEXT, cost_estimate_usd REAL, called_at TIMESTAMP);
        CREATE TABLE decisions (ticker TEXT, recommendation_kind TEXT, recommendation_value REAL,
                                conviction TEXT, made_at TIMESTAMP, outcome_label TEXT);
        CREATE TABLE brief_provenance_log (ticker TEXT, generated_at TIMESTAMP, trigger TEXT);
        CREATE TABLE disclosure_events (
            id INTEGER PRIMARY KEY, ticker TEXT, event_type TEXT, form TEXT,
            fiscal_year INTEGER, fiscal_period TEXT, canonical_id TEXT,
            subject TEXT, subject_label TEXT, source_doc_id INTEGER,
            evidence_quote TEXT, materiality REAL, verdict TEXT,
            interpretation_md TEXT, status TEXT, created_at TEXT,
            thesis_materiality TEXT, thesis_materiality_rationale TEXT,
            thesis_materiality_judged_at TEXT
        );
        """
    )
    now = datetime.now(UTC).isoformat()
    # alerts.fired_at is naive-UTC by repo convention (triggers write
    # now(UTC).replace(tzinfo=None)); the seed matches what prod rows look like.
    naive_now = datetime.now(UTC).replace(tzinfo=None).isoformat()
    evidence = json.dumps(
        {
            "summary": "ROE inflected down two quarters running",
            "citations": [
                {
                    "kind": "kpi_observation",
                    "locator": "ROE 2026-03-31",
                    "excerpt": "ROE 30% -> 26%",
                }
            ],
        }
    )
    conn.execute("INSERT INTO tracked_companies VALUES ('NU','Nu Holdings','portfolio',NULL)")
    conn.execute("INSERT INTO fmp_endpoint_status VALUES ('NU','2026-05-11T01:02:14')")
    conn.execute("INSERT INTO transcripts VALUES ('NU','2026-03-31')")
    conn.execute("INSERT INTO thesis_evaluations VALUES ('NU','2026-05-18T10:00:00','watch')")
    conn.execute("INSERT INTO timeseries_signals VALUES ('NU','red',?)", (now,))
    conn.execute("INSERT INTO timeseries_signals VALUES ('NU','green',?)", (now,))
    conn.execute(
        "INSERT INTO alerts VALUES (1, ?, 'NU', 'kpi_inflection', ?, 'pending', NULL, ?, "
        "'sig-nu-roe', NULL, NULL)",
        (DEFAULT_USER_ID, naive_now, evidence),
    )
    conn.execute(
        "INSERT INTO queued_actions VALUES (1, 1, 'thesis_update', ?, 'pending', ?, NULL, NULL)",
        (json.dumps({"body": "Tighten the ROE break rule"}), naive_now),
    )
    conn.execute("INSERT INTO management_commitments VALUES ('NU','met','2026-05-01')")
    conn.execute("INSERT INTO dcf_runs VALUES ('NU','2026-05-01',NULL,0.12)")
    conn.execute("INSERT INTO llm_calls VALUES ('NU','bear_case','claude-opus-4-7',0.42,?)", (now,))
    conn.execute(
        "INSERT INTO decisions VALUES ('NU','trim',20.0,'high','2026-05-15T00:00:00','pending')"
    )
    conn.execute("INSERT INTO brief_provenance_log VALUES ('NU',?,'manual')", (now,))
    conn.execute(
        """
        INSERT INTO disclosure_events VALUES
        (1, 'NU', 'item_reworded', '10-Q', 2026, 'Q2', 'risk_factors',
         'credit quality', 'Credit quality', 42,
         'Delinquency formation increased in the youngest vintages.',
         0.92, 'substantive',
         'The wording became more company-specific.', 'new', ?,
         'restricts_measurement', 'Vintage-level delinquency feeds the NPL break rule.', ?)
        """,
        (now, now),
    )
    # High-materiality-float but UNJUDGED — must never elevate (the float is
    # three incommensurable scales; NULL judgment means not elevated).
    conn.execute(
        """
        INSERT INTO disclosure_events VALUES
        (2, 'NU', 'item_added', '10-Q', 2026, 'Q2', 'mdna',
         'unjudged noise', 'Unjudged noise', NULL,
         'Revenuesand Delivery DD&A: Totals presented above.',
         1.0, 'substantive',
         'mangled table scraping', 'new', ?, NULL, NULL, NULL)
        """,
        (now,),
    )
    conn.commit()
    conn.close()


def _seed_files(repo_root: Path) -> None:
    holdings = repo_root / "micro_thesis" / "holdings"
    holdings.mkdir(parents=True)
    (holdings / "NU.json").write_text(json.dumps(_HOLDINGS), encoding="utf-8")
    research = repo_root / "output" / "research" / "NU"
    research.mkdir(parents=True)
    (research / "2026-05-18_workspace.html").write_text("<html>nu</html>", encoding="utf-8")
    raw = repo_root / "transcripts" / "raw"
    raw.mkdir(parents=True)
    (raw / "NU_Q1_2026.txt").write_text("transcript", encoding="utf-8")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / "data").mkdir()
    _seed_db(tmp_path / "data" / "portfolio.db")
    _seed_files(tmp_path)
    return tmp_path


# ----- artifact inventory -----


def test_artifact_inventory_flags_present_and_absent(repo: Path) -> None:
    arts = {a.label: a for a in build_artifact_inventory(repo, "NU")}
    assert arts["Holdings JSON"].exists is True
    assert arts["Workspace report (HTML)"].exists is True
    assert arts["Raw transcripts"].exists is True
    assert arts["Raw transcripts"].count == 1
    # Something that wasn't seeded:
    assert arts["Bear case"].exists is False
    # Lowercase ticker resolves to the same upper-case paths.
    assert {a.label: a for a in build_artifact_inventory(repo, "nu")}["Holdings JSON"].exists


# ----- analysis log -----


def test_analysis_log_summarizes_each_table(repo: Path) -> None:
    conn = sqlite3.connect(str(repo / "data" / "portfolio.db"))
    conn.row_factory = sqlite3.Row
    try:
        log = build_analysis_log(conn, "NU")
    finally:
        conn.close()
    by_name = {r.analysis: r for r in log.rows}
    assert by_name["Thesis evaluation"].summary.startswith("watch")
    assert "red" in by_name["Time-series signals"].summary
    assert by_name["Queued actions"].summary == "1 pending · 1 total"
    assert by_name["DCF valuation"].summary == "over/under +12%"
    assert log.llm_cost_30d_usd == pytest.approx(0.42)
    assert [a.trigger_kind for a in log.recent_alerts] == ["kpi_inflection"]


def test_analysis_log_empty_db_is_safe(tmp_path: Path) -> None:
    db = tmp_path / "empty.db"
    sqlite3.connect(str(db)).close()  # no tables
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        log = build_analysis_log(conn, "NU")
    finally:
        conn.close()
    assert log.rows == []  # nothing present, no crash


# ----- assembler + render -----


def test_build_and_to_dict_round_trips(repo: Path) -> None:
    tcc = build_ticker_command_center(repo, "NU")
    assert tcc.identity.list_type == "portfolio"
    assert tcc.identity.breach_status == "watch"
    assert tcc.thesis.present is True
    assert tcc.thesis.tier1[0].name == "ROE"
    assert tcc.position.available is False  # no portfolio-tracker sibling in tmp
    assert tcc.tracker_url is None
    json.loads(json.dumps(tcc.to_dict()))  # JSON round-trips cleanly


def test_render_has_all_panels(repo: Path) -> None:
    tcc = build_ticker_command_center(repo, "NU")
    html = render_ticker_html(tcc, generated_at=datetime(2026, 6, 1, tzinfo=UTC))
    assert html.startswith("<!doctype html>")
    for marker in ("Analyses run", "Artifacts", "Thesis", "Position", "Recent decisions"):
        assert marker in html
    assert "Open in Portfolio Tracker" not in html  # no guessed tracker endpoint
    assert "localhost:5173" not in html
    assert "/trade-analysis?" not in html


# ----- Holding tab (PR 8): report_date + head/foot-less fragments + embed -----


def test_report_date_derived_from_workspace_artifact(repo: Path) -> None:
    """report_date is parsed from the latest <DATE>_workspace.html filename — the
    (ticker, report_date) key the comment store + chat thread use."""
    tcc = build_ticker_command_center(repo, "NU")
    assert tcc.report_date == "2026-05-18"
    assert tcc.to_dict()["report_date"] == "2026-05-18"


def test_report_date_none_without_brief(tmp_path: Path) -> None:
    (tmp_path / "data").mkdir()
    _seed_db(tmp_path / "data" / "portfolio.db")  # DB but no output/research brief
    tcc = build_ticker_command_center(tmp_path, "NU")
    assert tcc.report_date is None


def test_render_ticker_fragment_is_one_band(repo: Path) -> None:
    """UX9c: the fragment is ONE ~40px utility band — the search combobox left,
    verdict · freshness dot · report/DCF links · Ops/Notes icons right. The
    Notes button opens the shell's shared drawer (data-cc-notes-open), not a
    holding-local one. The old inline position/thesis/analyses sections stay
    gone (they're the report's own tabs)."""
    tcc = build_ticker_command_center(repo, "NU")
    frag = render_ticker_fragment(tcc)
    assert "<!doctype" not in frag.lower()
    assert "<html" not in frag.lower()
    # The combobox replaces the static ticker/name heading and the old cc-picker.
    assert 'class="cc-combo"' in frag
    assert 'data-current="NU"' in frag
    assert 'href="/reports/NU"' in frag
    assert 'href="/dcf/NU"' in frag
    assert "Portfolio Tracker" not in frag  # no configured tracker UI endpoint
    assert 'data-tcc-drawer="ops"' in frag
    assert "data-cc-notes-open" in frag  # Notes opens the SHARED drawer
    assert 'data-tcc-drawer="notes"' not in frag  # the holding-local one is retired
    assert 'class="cc-fdot' in frag  # freshness dot
    for gone in ("Analyses run", "Artifacts", "Tier-1 KPIs", "kpi-strip"):
        assert gone not in frag


def test_render_holding_fragment_embeds_report(repo: Path) -> None:
    frag = render_holding_fragment(repo, "NU")
    # The full pipeline is carried by an iframe of the workspace report.
    assert 'src="/reports/NU"' in frag
    assert "cc-report-frame" in frag
    # The band's combobox + the Ops drawer (config/meta + the 5-min reread,
    # folded in from its old inline spot in UX9c).
    assert 'class="cc-combo"' in frag
    assert 'data-tcc-panel="ops"' in frag
    assert "Analyses run" in frag
    assert "5-minute reread" in frag
    assert "Per-holding 5-min rereads" in frag
    # The reread is no longer an inline fold above the report.
    assert "tcc-reread-fold" not in frag
    assert "Disclosure drift" in frag
    assert "thesis-materiality gated" in frag
    assert "Delinquency formation increased in the youngest vintages." in frag
    assert "Vintage-level delinquency feeds the NPL break rule." in frag
    assert 'href="/source/42"' in frag
    # The unjudged row (NULL thesis_materiality) must NOT elevate, however
    # high its materiality float — and the strip must say so honestly.
    assert "Unjudged noise" not in frag
    assert "1 awaiting the thesis-materiality judgment" in frag


def test_ticker_renderer_uses_well_title_and_canonical_drawer_head_roles(repo: Path) -> None:
    frag = render_holding_fragment(repo, "NU")

    assert '<h2 class="k-well-title">Disclosure drift</h2>' in frag
    assert "<h2>Disclosure drift</h2>" not in frag
    assert 'class="cc-drawer-head"' in frag
    assert 'class="tcc-drawer-head"' not in frag


def test_render_holding_fragment_no_brief_degrades(tmp_path: Path) -> None:
    (tmp_path / "data").mkdir()
    _seed_db(tmp_path / "data" / "portfolio.db")  # no brief built
    frag = render_holding_fragment(tmp_path, "NU")
    assert "No workspace brief built yet" in frag
    assert "<iframe" not in frag  # nothing to embed


# ----- Holding rail (P1.3): open notes + recent alerts beside the report -----


def test_build_holding_rail_reads_open_notes_and_alerts(repo: Path) -> None:
    db = repo / "data" / "portfolio.db"
    kept = create_note(ticker="NU", kind="watch", body="Watch FX drag on ARPAC", db_path=db)
    answered = create_note(ticker="NU", kind="question", body="NIM dip: mix or rate?", db_path=db)
    resolve_note(answered.id, db_path=db)
    create_note(ticker="MELI", kind="watch", body="Other name's note", db_path=db)

    rail = build_holding_rail(repo, "nu")  # lowercase in → uppercased lookup
    assert rail.notes is not None
    assert [n.id for n in rail.notes] == [kept.id]  # open-only, this ticker only
    assert rail.alerts is not None
    assert len(rail.alerts) == 1
    alert, actions = rail.alerts[0]
    assert alert.trigger_kind == "kpi_inflection"
    assert [qa.action_kind for qa in actions] == ["thesis_update"]


def test_holding_fragment_consolidates_notes_into_shared_drawer(repo: Path) -> None:
    """UX9c: the holding fragment no longer inlines its own notes/alerts drawer —
    the band's ✎ button opens the shell's SHARED drawer (data-cc-notes-open),
    and the report is full-width. The rich notes+alerts rendering lives in the
    shared drawer fragment (asserted below + by the PR1 notes-drawer tests)."""
    db = repo / "data" / "portfolio.db"
    create_note(ticker="NU", kind="watch", body="Watch FX drag on ARPAC next print", db_path=db)

    frag = render_holding_fragment(repo, "NU")
    assert "data-cc-notes-open" in frag
    assert 'class="tcc-report-main"' in frag
    assert 'src="/reports/NU"' in frag
    # The holding fragment itself no longer carries the notes/alerts rail panels
    # (the "Recent alerts" h3 in the Ops analyses log is a different thing).
    assert 'data-tcc-panel="notes"' not in frag
    assert "Open notes" not in frag
    assert 'class="alert-card ' not in frag  # the rich note-rail alert cards are gone
    assert "Watch FX drag on ARPAC next print" not in frag  # note body not inlined


def test_shared_notes_drawer_surfaces_notes_and_alerts(repo: Path) -> None:
    """The ticker-scoped shared drawer (UX9b) carries the open notes + the
    feed alert-card shape (evidence drawer collapsed, memo, queued action,
    feed deep link) that the holding rail used to render beside the report."""
    db = repo / "data" / "portfolio.db"
    create_note(
        ticker="NU",
        kind="watch",
        body="Watch FX drag on ARPAC next print",
        anchor_type="kpi",
        anchor_key="earnings.arpac",
        db_path=db,
    )
    answered = create_note(ticker="NU", kind="question", body="NIM dip: mix or rate?", db_path=db)
    resolve_note(answered.id, db_path=db)

    frag = render_notes_drawer_fragment(repo, "NU")
    assert "Open notes" in frag
    assert "Watch FX drag on ARPAC next print" in frag
    assert "nk-watch" in frag
    assert "earnings.arpac" in frag
    assert "NIM dip: mix or rate?" not in frag  # resolved note excluded
    assert "Recent alerts" in frag
    assert 'class="alert-card k-card k-card-stack"' in frag
    assert "ROE inflected down two quarters running" in frag
    assert "Tighten the ROE break rule" in frag
    assert '<details class="evidence-drawer">' in frag
    assert '<details open class="evidence-drawer">' not in frag
    assert 'href="/feed?ticker=NU"' in frag


def test_shared_drawer_empty_states(repo: Path) -> None:
    """No notes / no alerts (for a ticker with none) render the none-yet
    states, not the substrate-unavailable ones."""
    frag = render_notes_drawer_fragment(repo, "MELI")  # seeded DB, nothing for MELI
    assert "No open notes on this name" in frag
    assert "No alerts fired on this name yet" in frag


def test_shared_drawer_degrades_without_substrate(tmp_path: Path) -> None:
    """A DB without the analyst_notes / alerts tables (pre-migration) degrades
    to the unavailable-state per source — never an exception."""
    (tmp_path / "data").mkdir()
    conn = sqlite3.connect(str(tmp_path / "data" / "portfolio.db"))
    conn.execute(
        "CREATE TABLE tracked_companies (ticker TEXT, name TEXT, list_type TEXT, "
        "archived_at TIMESTAMP)"
    )
    conn.commit()
    conn.close()
    frag = render_notes_drawer_fragment(tmp_path, "NU")
    assert "Notes substrate unavailable" in frag
    assert "Alerts substrate unavailable" in frag

    rail = build_holding_rail(tmp_path, "NU")
    assert rail.notes is None
    assert rail.alerts is None


def test_holding_rail_missing_db_is_unavailable(tmp_path: Path) -> None:
    rail = build_holding_rail(tmp_path, "NU")  # no data/portfolio.db at all
    assert rail.notes is None
    assert rail.alerts is None
    assert rail.brief_provenance is None


# ----- Shared ✎ Notes drawer fragment (UX9b): quick-add + open notes -----


def test_notes_drawer_fragment_global(repo: Path) -> None:
    """Without a ticker scope: quick-add (all five kinds, empty ticker box)
    above the newest open notes book-wide — plus the Journal hand-off link.
    No alerts section (that's the ticker-scoped extra)."""
    db = repo / "data" / "portfolio.db"
    create_note(ticker="NU", kind="watch", body="Watch FX drag on ARPAC", db_path=db)
    create_note(ticker="MELI", kind="question", body="Ads take-rate trajectory?", db_path=db)

    frag = render_notes_drawer_fragment(repo, None)
    assert "Quick note" in frag
    for kind in ("question", "decision", "watch", "assumption", "observation"):
        assert f'value="{kind}"' in frag
    # PR3: unchecked-by-default routing still targets /api/notes — the fetch
    # URL is now a variable (musing toggle can redirect it), so pin the
    # literal that supplies the default rather than the old fetch(' call.
    assert "'/api/notes'" in frag
    assert "ccReloadNotesDrawer" in frag
    # Both names' open notes show (book-wide scope), no per-name alerts panel.
    assert "Watch FX drag on ARPAC" in frag
    assert "Ads take-rate trajectory?" in frag
    assert "Recent alerts" not in frag
    assert 'href="/#journal"' in frag


def test_notes_drawer_quick_add_carries_musing_toggle(repo: Path) -> None:
    """PR3: the quick-add fragment gains a 'musing' checkbox (default
    unchecked — unchanged /api/notes journal-note behavior) plus the
    data-musing-endpoint attribute the client JS reads to route the SAME text
    to the Ledger capture spine (/api/capture/text) instead, when checked."""
    frag = render_notes_drawer_fragment(repo, None)
    assert 'class="qn-musing"' in frag
    assert 'data-musing-endpoint="/api/capture/text"' in frag
    # Unchecked by default — no "checked" attribute on the checkbox.
    assert '<input type="checkbox" class="qn-musing">' in frag
    # The routing itself is client-side; the JS reads the toggle + endpoint
    # attribute (getAttribute) rather than hardcoding a second POST target.
    assert "toCaptureSpine" in frag
    assert "getAttribute('data-musing-endpoint')" in frag
    # Existing /api/notes path is untouched (still the unchecked default).
    assert "'/api/notes'" in frag


def test_notes_drawer_fragment_ticker_scoped(repo: Path) -> None:
    """With the Holding tab's ticker: the quick-add pre-fills it, the list
    narrows to that name, and the name's recent alerts ride along — the
    content the holding page's PR4 Notes drawer used to carry."""
    db = repo / "data" / "portfolio.db"
    create_note(ticker="NU", kind="watch", body="Watch FX drag on ARPAC", db_path=db)
    create_note(ticker="MELI", kind="question", body="Ads take-rate trajectory?", db_path=db)

    frag = render_notes_drawer_fragment(repo, "nu")  # lowercase in → uppercased
    assert 'value="NU"' in frag  # quick-add pre-filled
    assert "Watch FX drag on ARPAC" in frag
    assert "Ads take-rate trajectory?" not in frag
    assert "Recent alerts" in frag
    assert "ROE inflected down two quarters running" in frag  # seeded alert


def test_notes_drawer_fragment_missing_db_degrades(tmp_path: Path) -> None:
    """No DB at all: the quick-add still renders (the POST will surface its
    own error) and the list shows the substrate-unavailable state."""
    frag = render_notes_drawer_fragment(tmp_path, None)
    assert "Quick note" in frag
    assert "Notes substrate unavailable" in frag


# ----- Search-first holding combobox (UX9c) -----


def test_holding_picker_band_is_search_first(repo: Path) -> None:
    """The no-ticker band: a combobox (empty current) + a hint, plus its wiring.
    Navigation is the same #holding=<T> hash the old cc-picker drove."""
    band = render_holding_picker_band(repo)
    assert 'class="cc-combo"' in band
    assert 'data-current=""' in band
    assert 'role="combobox"' in band
    assert "Search a ticker or name to open a holding." in band
    # The widget fetches the shared ticker source and navigates by hash.
    assert "/api/tickers" in band
    assert "'#holding=' + encodeURIComponent" in band
    # Arrow/Enter keyboard selection is wired.
    assert "ArrowDown" in band
    assert "ArrowUp" in band
    # Results are relevance-ranked, current research lists win tie-breaks,
    # and the first result is immediately Enter-selectable.
    assert "function matchScore" in band
    assert "listPriority(a.ticker) - listPriority(b.ticker)" in band
    assert ".slice(0, 12)" in band
    assert "if (resetSelection) sel = matches.length ? 0 : -1" in band
    assert "aria-activedescendant" in band
    # Focus and input share one in-flight fetch; the late focus result renders
    # the current input value instead of overwriting typed results with "all".
    assert "if (loading) return loading" in band
    assert "render(input.value, true)" in band


def test_holding_band_combobox_prefills_current_ticker(repo: Path) -> None:
    """A loaded holding's band combobox carries the bare ticker as its VALUE
    (mono), the company name as the separate muted overlay span (the canonical
    two-part label — never "T · Name" as one string), and data-current (so
    re-selecting the same name is a no-op)."""
    tcc = build_ticker_command_center(repo, "NU")
    frag = render_ticker_fragment(tcc)
    assert 'data-current="NU"' in frag
    assert 'value="NU"' in frag
    assert '<span class="cc-combo-name" title="Nu Holdings">Nu Holdings</span>' in frag
    assert "NU · Nu Holdings" not in frag


def test_holding_panel_endpoint(client) -> None:
    # No ticker → the search combobox band (UX9c), not a 404 or a dropdown stub.
    empty = client.get("/api/panel/holding")
    assert empty.status_code == 200
    empty_body = empty.get_data(as_text=True)
    assert 'class="cc-combo"' in empty_body
    assert "Search a ticker or name to open a holding." in empty_body
    assert "Pick a holding from the dropdown" not in empty_body
    # With a ticker → head/foot-less fragment: the band (combobox + Ops/Notes)
    # over the embedded report at full width. Notes ride in the shared drawer.
    resp = client.get("/api/panel/holding?ticker=NU")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "<!doctype" not in body.lower()
    assert 'class="cc-combo"' in body
    assert 'data-current="NU"' in body
    assert 'src="/reports/NU"' in body
    assert "data-cc-notes-open" in body  # Notes button → shared drawer
    assert 'data-tcc-panel="ops"' in body


def test_notes_drawer_panel_endpoint(client) -> None:
    plain = client.get("/api/panel/notes_drawer")
    assert plain.status_code == 200
    body = plain.get_data(as_text=True)
    assert "<!doctype" not in body.lower()
    assert "Quick note" in body
    scoped = client.get("/api/panel/notes_drawer?ticker=NU")
    assert scoped.status_code == 200
    scoped_body = scoped.get_data(as_text=True)
    assert 'value="NU"' in scoped_body
    assert "Recent alerts" in scoped_body


# ----- live endpoints -----


@pytest.fixture
def client(repo: Path):
    import comments_server

    return comments_server.create_app(repo).test_client()


def test_ticker_api_returns_json(client) -> None:
    resp = client.get("/api/ticker/NU")
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["identity"]["ticker"] == "NU"
    assert payload["thesis"]["present"] is True
    assert payload["tracker_url"] is None


def test_ticker_page_redirects_to_shell(client) -> None:
    """/ticker/<t> is folded into the shell — it now 302-redirects to the
    Holding drill-down deep link (ticker uppercased)."""
    resp = client.get("/ticker/nu")  # lowercase in → uppercased in the deep link
    assert resp.status_code == 302
    assert resp.headers["Location"] == "/?screen=company-desk&ticker=NU"
