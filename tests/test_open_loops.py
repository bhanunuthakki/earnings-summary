"""The open-loops ritual-debt band — queue counts, doorways, ritual-clear state,
and the never-raises guarantee on thin/pre-migration DBs."""

from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from alembic.config import Config

from alembic import command

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from capture import ingest  # noqa: E402
from capture.matcher import build_roster_index  # noqa: E402
from pipeline.command_center_shell import render_overview_panel  # noqa: E402
from pipeline.open_loops import render_open_loops_band, render_weekly_packet_peek  # noqa: E402

PRIOR_HEAD = "0059_kpi_facts_restatement"

# ``decisions``, ``tracked_companies`` and ``llm_artifacts`` all predate the
# 0059 stamp (db.init_db() territory) — 0130's extension of ``decisions`` is
# _has_table-guarded, so the stamp+upgrade fixture never creates any of the
# three; hand-build the modern shapes the band queries (the test_governor.py /
# test_card_dispositions.py pattern, post-upgrade so no migration conflicts).
_DECISIONS_DDL = """
CREATE TABLE decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker VARCHAR(16),
    recommendation_kind VARCHAR(32) NOT NULL,
    conviction VARCHAR(16),
    outcome_label VARCHAR(16) NOT NULL DEFAULT 'pending',
    decided_by VARCHAR(16) NOT NULL DEFAULT 'advisor',
    scope VARCHAR(16) NOT NULL DEFAULT 'ticker',
    falsifier TEXT,
    size_usd FLOAT,
    user_notes TEXT,
    advice_artifact_id INTEGER,
    made_at DATETIME NOT NULL,
    created_at DATETIME NOT NULL
);
CREATE TABLE tracked_companies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL DEFAULT 'bhanu',
    ticker TEXT NOT NULL,
    name TEXT NOT NULL,
    list_type TEXT NOT NULL,
    added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    archived_at TEXT
);
CREATE TABLE llm_artifacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker VARCHAR(16),
    scope VARCHAR(64) NOT NULL DEFAULT 'ticker',
    purpose VARCHAR(64) NOT NULL,
    content_json TEXT,
    input_sha256 VARCHAR(64) NOT NULL,
    generated_at DATETIME NOT NULL,
    superseded_by_id INTEGER,
    dirty BOOLEAN NOT NULL DEFAULT 0
);
"""


def _cfg(db_path: Path) -> Config:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    db = tmp_path / "loops.db"
    cfg = _cfg(db)
    command.stamp(cfg, PRIOR_HEAD)
    command.upgrade(cfg, "head")
    conn = sqlite3.connect(str(db))
    try:
        conn.executescript(_DECISIONS_DDL)
        conn.commit()
    finally:
        conn.close()
    return db


def test_ritual_clear_on_empty_db(db_path: Path) -> None:
    html = render_open_loops_band(db_path)
    assert "Ritual clear" in html
    assert "cc-open-loops" in html


def _insert_decision_draft(db_path: Path, *, status: str = "awaiting_confirmation") -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "INSERT INTO decision_drafts (source_channel, idempotency_key, original_text, "
            "status, created_at, updated_at) VALUES ('tracker', 'idem:1', 'buy 10 NU', "
            "?, '2026-07-24T08:00:00', '2026-07-24T08:00:00')",
            (status,),
        )
        conn.commit()
    finally:
        conn.close()


def test_pending_confirmation_line_counts_and_doors(db_path: Path) -> None:
    _insert_decision_draft(db_path)
    html = render_open_loops_band(db_path)
    assert "Pending confirmations" in html
    assert 'href="/mobile/inbox"' in html
    assert "Ritual clear" not in html


def test_pending_confirmation_line_ignores_non_awaiting_status(db_path: Path) -> None:
    _insert_decision_draft(db_path, status="dismissed")
    html = render_open_loops_band(db_path)
    assert "Pending confirmations" not in html


def test_pending_confirmation_line_independent_of_senior_partner_brief(db_path: Path) -> None:
    """The 2026-07-25 postmortem: 78 drafts piled up unconfirmed because their
    only doorway lived inside the brief's Today card, which renders nothing
    until a senior_partner_brief artifact exists. This line must show the
    pending count with no such artifact ever generated."""
    _insert_decision_draft(db_path)
    conn = sqlite3.connect(str(db_path))
    try:
        count = conn.execute(
            "SELECT COUNT(*) FROM llm_artifacts WHERE purpose = 'senior_partner_brief'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert count == 0
    html = render_open_loops_band(db_path)
    assert "Pending confirmations" in html


def test_undispositioned_card_line(db_path: Path) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "INSERT INTO tracked_companies (ticker, name, list_type, added_at) "
            "VALUES ('NU', 'Nu Holdings', 'evaluation', '2026-06-01')"
        )
        conn.execute(
            "INSERT INTO llm_artifacts (ticker, scope, purpose, input_sha256, "
            "generated_at) VALUES ('NU', 'ticker', 'investment_decision_card', "
            "'sha', '2026-07-01T00:00:00')"
        )
        conn.commit()
    finally:
        conn.close()
    html = render_open_loops_band(db_path)
    assert "Cards awaiting disposition" in html
    assert 'href="/mobile/inbox"' in html


def test_undispositioned_card_line_clears_once_dispositioned(db_path: Path) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "INSERT INTO tracked_companies (ticker, name, list_type, added_at) "
            "VALUES ('NU', 'Nu Holdings', 'evaluation', '2026-06-01')"
        )
        conn.execute(
            "INSERT INTO llm_artifacts (id, ticker, scope, purpose, input_sha256, "
            "generated_at) VALUES (1, 'NU', 'ticker', 'investment_decision_card', "
            "'sha', '2026-07-01T00:00:00')"
        )
        conn.execute(
            "INSERT INTO decisions (ticker, recommendation_kind, advice_artifact_id, "
            "made_at, created_at) VALUES ('NU', 'watch', 1, '2026-07-02', '2026-07-02')"
        )
        conn.commit()
    finally:
        conn.close()
    assert "Cards awaiting disposition" not in render_open_loops_band(db_path)


def test_reconcile_line_counts_and_doors(db_path: Path) -> None:
    roster = build_roster_index(symbols=["NU"], phrases={})
    result = ingest.ingest_capture(
        channel="tray", text="seed thought", roster=roster, db_path=db_path
    )
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "UPDATE analyst_notes SET source_ref = 'seed:decision:1' WHERE id = ?",
            (result.note_id,),
        )
        conn.commit()
    finally:
        conn.close()
    html = render_open_loops_band(db_path)
    assert "Reconcile" in html
    assert 'href="/#musings"' in html
    assert "Ritual clear" not in html


def test_decision_stub_debt_line(db_path: Path) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "INSERT INTO decisions (ticker, recommendation_kind, decided_by, "
            "made_at, created_at) VALUES ('NU', 'add', 'owner', '2026-06-01', '2026-06-01')"
        )
        conn.commit()
    finally:
        conn.close()
    html = render_open_loops_band(db_path)
    assert "Decisions missing conviction/falsifier" in html
    assert 'href="/#decisions_record"' in html
    assert "oldest" in html  # the aged stub carries its debt age


def test_digest_ping_line(db_path: Path) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "INSERT INTO coach_pings (class_, key, ticker, body, status, source_ref, "
            "created_at, updated_at) VALUES ('falsifier_breach', 'alert:1', 'NU', 'b', "
            "'digest', 'decision:1', '2026-07-01T08:00:00', '2026-07-01T08:00:00')"
        )
        conn.commit()
    finally:
        conn.close()
    html = render_open_loops_band(db_path)
    assert "Coach digest" in html


def test_routed_to_brief_line(db_path: Path) -> None:
    """P2.2: a coach_pings row the governor routed to the Senior Partner
    Brief (status='routed_to_brief') gets its own debt line — distinct from
    'Coach digest', which never sees these rows anymore."""
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "INSERT INTO coach_pings (class_, key, ticker, body, status, source_ref, "
            "created_at, updated_at) VALUES ('profile_drift', 'fact:1', NULL, 'b', "
            "'routed_to_brief', 'fact:1', '2026-07-01T08:00:00', '2026-07-01T08:00:00')"
        )
        conn.commit()
    finally:
        conn.close()
    html = render_open_loops_band(db_path)
    assert "Routed to weekly brief" in html
    assert "Coach digest" not in html


# --------------------------------------------------------------------------- #
# Wave3b Task 1: coach_strip folded in — the ONE count (today's Telegram
# sends) not already covered by the digest/routed-to-brief lines above.
# --------------------------------------------------------------------------- #

_PINNED_NOW = datetime(2026, 7, 10, 12, 0, 0)  # mid-day, far from UTC midnight
_COACH_PING_KEY_SEQ = iter(range(10_000))


def _insert_coach_ping(db_path: Path, *, status: str, created_at: datetime) -> None:
    # coach_pings has UNIQUE(class_, key) — a fresh key per call, else a
    # second insert in the same test collides (real pings key on their own
    # trigger signature; the exact value is irrelevant here).
    key = f"k:{next(_COACH_PING_KEY_SEQ)}"
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "INSERT INTO coach_pings (class_, key, ticker, body, status, source_ref, "
            "created_at, updated_at) VALUES ('falsifier_breach', ?, 'NU', 'b', ?, NULL, ?, ?)",
            (key, status, created_at.isoformat(), created_at.isoformat()),
        )
        conn.commit()
    finally:
        conn.close()


def test_coach_sent_today_line(db_path: Path) -> None:
    _insert_coach_ping(db_path, status="sent", created_at=_PINNED_NOW)
    html = render_open_loops_band(db_path, now=_PINNED_NOW)
    assert "Coach sent today" in html


def test_coach_sent_today_line_excludes_earlier_days(db_path: Path) -> None:
    _insert_coach_ping(db_path, status="sent", created_at=_PINNED_NOW - timedelta(days=1))
    html = render_open_loops_band(db_path, now=_PINNED_NOW)
    assert "Coach sent today" not in html


def test_coach_sent_today_line_ignores_digest_and_dismissed(db_path: Path) -> None:
    """'sent' is disjoint from 'digest'/'routed_to_brief'/'dismissed' — this
    line must never double-count what the sibling lines already cover."""
    _insert_coach_ping(db_path, status="digest", created_at=_PINNED_NOW)
    _insert_coach_ping(db_path, status="dismissed", created_at=_PINNED_NOW)
    html = render_open_loops_band(db_path, now=_PINNED_NOW)
    assert "Coach sent today" not in html


# --------------------------------------------------------------------------- #
# Wave3b Task 3: the Sunday-packet state line + its read-only peek.
# --------------------------------------------------------------------------- #


def _insert_packet_run(
    db_path: Path, *, status: str, total_items: int, now: datetime
) -> int:
    iso_year, iso_week, _ = now.isocalendar()
    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.execute(
            "INSERT INTO weekly_packet_runs (iso_year, iso_week, status, total_items, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (iso_year, iso_week, status, total_items, now.isoformat(), now.isoformat()),
        )
        conn.commit()
        return int(cur.lastrowid or 0)
    finally:
        conn.close()


def _insert_packet_item(
    db_path: Path,
    *,
    run_id: int,
    order_index: int,
    title: str,
    verdict: str | None,
    now: datetime,
) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "INSERT INTO weekly_packet_items (run_id, item_kind, ref_id, ticker, title, "
            "order_index, created_at, verdict) VALUES (?, 'reconcile_note', ?, NULL, ?, ?, ?, ?)",
            (run_id, order_index + 1, title, order_index, now.isoformat(), verdict),
        )
        conn.commit()
    finally:
        conn.close()


def test_packet_line_open_run_shows_answered_of_total(db_path: Path) -> None:
    run_id = _insert_packet_run(db_path, status="open", total_items=3, now=_PINNED_NOW)
    _insert_packet_item(db_path, run_id=run_id, order_index=0, title="a", verdict="accept", now=_PINNED_NOW)
    _insert_packet_item(db_path, run_id=run_id, order_index=1, title="b", verdict=None, now=_PINNED_NOW)
    _insert_packet_item(db_path, run_id=run_id, order_index=2, title="c", verdict=None, now=_PINNED_NOW)

    html = render_open_loops_band(db_path, now=_PINNED_NOW)
    assert "Sunday packet · 1 of 3 answered · finish" in html
    assert 'data-peek-url="/api/peek/weekly-packet"' in html
    assert 'href="/#musings"' in html


def test_packet_line_dark_when_clear(db_path: Path) -> None:
    _insert_packet_run(db_path, status="clear", total_items=0, now=_PINNED_NOW)
    assert "Sunday packet" not in render_open_loops_band(db_path, now=_PINNED_NOW)


def test_packet_line_dark_when_complete(db_path: Path) -> None:
    run_id = _insert_packet_run(db_path, status="complete", total_items=2, now=_PINNED_NOW)
    _insert_packet_item(
        db_path, run_id=run_id, order_index=0, title="a", verdict="accept", now=_PINNED_NOW
    )
    _insert_packet_item(
        db_path, run_id=run_id, order_index=1, title="b", verdict="drop", now=_PINNED_NOW
    )
    assert "Sunday packet" not in render_open_loops_band(db_path, now=_PINNED_NOW)


def test_packet_line_dark_when_no_run_this_week(db_path: Path) -> None:
    assert "Sunday packet" not in render_open_loops_band(db_path, now=_PINNED_NOW)


def test_packet_line_ignores_a_prior_weeks_leftover_run(db_path: Path) -> None:
    """A previous week's still-open run must never bleed into this week's
    line — only the CURRENT ISO week's row counts (weekly_packet.py's own
    docstring: unresolved items resurface in a FRESH row next week)."""
    last_week = _PINNED_NOW - timedelta(days=7)
    run_id = _insert_packet_run(db_path, status="open", total_items=1, now=last_week)
    _insert_packet_item(
        db_path, run_id=run_id, order_index=0, title="stale", verdict=None, now=last_week
    )
    assert "Sunday packet" not in render_open_loops_band(db_path, now=_PINNED_NOW)


def test_weekly_packet_peek_renders_pending_and_answered_items(db_path: Path) -> None:
    run_id = _insert_packet_run(db_path, status="open", total_items=2, now=_PINNED_NOW)
    _insert_packet_item(
        db_path, run_id=run_id, order_index=0, title="ratified note", verdict="accept", now=_PINNED_NOW
    )
    _insert_packet_item(
        db_path, run_id=run_id, order_index=1, title="still pending", verdict=None, now=_PINNED_NOW
    )
    html = render_weekly_packet_peek(db_path, now=_PINNED_NOW)
    assert "still pending" in html and "pending" in html
    assert "ratified note" in html and "accept" in html
    assert "given on Telegram" in html


def test_weekly_packet_peek_no_run_this_week(db_path: Path) -> None:
    html = render_weekly_packet_peek(db_path, now=_PINNED_NOW)
    assert "No Sunday packet run yet" in html


def test_weekly_packet_peek_never_raises_on_missing_table(tmp_path: Path) -> None:
    bare = tmp_path / "bare.db"
    sqlite3.connect(str(bare)).close()
    html = render_weekly_packet_peek(bare, now=_PINNED_NOW)
    assert "unavailable" in html


def test_proposed_tenets_flag_gated(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from synthesis.tenets import record_tenet

    record_tenet(body_md="Let winners run.", status="proposed", db_path=db_path)
    monkeypatch.delenv("LEDGER_WORLDVIEW", raising=False)
    assert "Tenets proposed" not in render_open_loops_band(db_path)
    monkeypatch.setenv("LEDGER_WORLDVIEW", "1")
    assert "Tenets proposed" in render_open_loops_band(db_path)


def test_never_raises_without_schema(tmp_path: Path) -> None:
    bare = tmp_path / "bare.db"
    sqlite3.connect(str(bare)).close()  # a DB with no tables at all
    html = render_open_loops_band(bare)
    assert "Ritual clear" in html


def test_red_team_escalation_banner_absent_with_no_deferred_items(db_path: Path) -> None:
    html = render_open_loops_band(db_path)
    assert "escalated" not in html
    assert "k-well-bad" not in html


def test_red_team_escalation_banner_renders_on_second_defer(db_path: Path) -> None:
    from redteam import response, store
    from redteam.models import RedTeamLLMItem

    item_id = store.insert_item(
        db_path=db_path,
        run_key="red_team_2026_08",
        ticker="NU",
        lens="fx_translation",
        kind="per_name",
        item=RedTeamLLMItem(
            attack_md="Attack.", question_md="Q?", proposed_change_md="Change.", severity="high"
        ),
    )
    response.respond(db_path=db_path, item_id=item_id, action="defer")
    html = render_open_loops_band(db_path)
    assert "Red Team: 1 item escalated" in html
    assert "k-well" in html
    assert "k-well-bad" in html
    assert 'href="/#red_team"' in html


def test_red_team_escalation_banner_clears_once_answered(db_path: Path) -> None:
    from redteam import response, store
    from redteam.models import RedTeamLLMItem

    item_id = store.insert_item(
        db_path=db_path,
        run_key="red_team_2026_08",
        ticker="NU",
        lens="fx_translation",
        kind="per_name",
        item=RedTeamLLMItem(
            attack_md="Attack.", question_md="Q?", proposed_change_md="Change.", severity="high"
        ),
    )
    response.respond(db_path=db_path, item_id=item_id, action="defer")
    assert "escalated" in render_open_loops_band(db_path)
    response.respond(db_path=db_path, item_id=item_id, action="accept")
    assert "escalated" not in render_open_loops_band(db_path)


def test_overview_panel_prepends_band() -> None:
    html = render_overview_panel({}, None, open_loops_html='<div class="cc-open-loops">BAND</div>')
    assert "BAND" in html
    assert html.index("BAND") < html.index("cc-cockpit-live")
    # Without the band, no open-loops markup is rendered in the DOCUMENT BODY
    # — TODAY_BANDS_JS (navigation_ia §4 PR3, always inlined) reuses the
    # ``.cc-open-loops``/``.cc-ol-line`` classes at runtime for its own
    # "Continue where you left off" line, so its JS *source* legitimately
    # contains the literal substring; strip that one known script before
    # asserting the body itself stayed band-free.
    from pipeline.command_center_shell import TODAY_BANDS_JS

    bare = render_overview_panel({}, None).replace(f"<script>{TODAY_BANDS_JS}</script>", "")
    assert "cc-open-loops" not in bare
