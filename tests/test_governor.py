"""The nag governor — moment collection, freshness gate, caps, auto-mute (W2)."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import pytest
from alembic.config import Config

from alembic import command
from research.governor import (
    DAILY_CAP,
    MUTE_AFTER,
    Moment,
    collect_moments,
    digest_pings,
    get_ping,
    record_dismissal,
    run_governor,
    unmute,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PRIOR_HEAD = "0130_owner_decision_extension"
HEAD = "0131_coach_pings"

_PRE_DDL = """
CREATE TABLE decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker VARCHAR(16),
    recommendation_kind VARCHAR(32) NOT NULL,
    conviction VARCHAR(16),
    decided_by VARCHAR(16) NOT NULL DEFAULT 'advisor',
    scope VARCHAR(16) NOT NULL DEFAULT 'ticker',
    falsifier TEXT,
    size_usd FLOAT,
    user_notes TEXT,
    made_at DATETIME NOT NULL,
    created_at DATETIME NOT NULL
);
CREATE TABLE alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT,
    trigger_kind TEXT NOT NULL,
    fired_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    evidence_json TEXT,
    dismissed_at TEXT
);
CREATE TABLE analyst_notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT,
    kind TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    body TEXT NOT NULL,
    source TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE tracked_companies (ticker TEXT PRIMARY KEY, list_type TEXT NOT NULL);
INSERT INTO tracked_companies VALUES ('NU','portfolio');
"""

_NOW = datetime(2026, 7, 10, 12, 0, 0)


def _fake_collect_moments_factory(moments: list[Moment]) -> object:
    """Typed monkeypatch target for ``governor.collect_moments`` — a plain
    ``lambda *a, **kw: ...`` triggers pyright's reportUnknownLambdaType
    against ``monkeypatch.setattr``'s overloads; a named function with
    explicit ``object`` param types does not."""

    def fake_collect(*a: object, **kw: object) -> list[Moment]:
        return list(moments)

    return fake_collect


def _always_fresh(*a: object, **kw: object) -> bool:
    return True


@pytest.fixture
def db(tmp_path: Path) -> Path:
    path = tmp_path / "gov.db"
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(_PRE_DDL)
        conn.commit()
    finally:
        conn.close()
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    command.stamp(cfg, PRIOR_HEAD)
    command.upgrade(cfg, HEAD)
    conn = sqlite3.connect(str(path))
    try:
        # Deliberately minimal 0131 contract fixture, not a production
        # versioned database; guarded stores may enforce their local tables.
        conn.execute("DROP TABLE alembic_version")
        conn.commit()
    finally:
        conn.close()
    return path


def _seed_breach(db: Path, *, falsifier: str = "15-90d NPL >5% for 2Q") -> int:
    conn = sqlite3.connect(str(db))
    try:
        cur = conn.execute(
            "INSERT INTO decisions (ticker, recommendation_kind, decided_by, falsifier, "
            "made_at, created_at) VALUES ('NU','add','owner',?,'2026-03-15','2026-03-15')",
            (falsifier,),
        )
        did = int(cur.lastrowid or 0)
        conn.execute(
            "INSERT INTO alerts (ticker, trigger_kind, fired_at, evidence_json) VALUES "
            "('NU','decision_condition','2026-07-10T08:00:00',?)",
            (json.dumps({"decision_id": did}),),
        )
        conn.commit()
        return did
    finally:
        conn.close()


def _seed_stub(db: Path) -> int:
    conn = sqlite3.connect(str(db))
    try:
        cur = conn.execute(
            "INSERT INTO decisions (ticker, recommendation_kind, decided_by, size_usd, "
            "user_notes, made_at, created_at) VALUES ('NU','initiate','owner',9000,"
            "'retro-net:NU:2026-07-08:buy · unannounced', '2026-07-08','2026-07-08T10:00:00')"
        )
        conn.commit()
        return int(cur.lastrowid or 0)
    finally:
        conn.close()


def test_collect_and_send_falsifier_breach(db: Path) -> None:
    _seed_breach(db)
    sent: list[Moment] = []
    tally = run_governor(db, send_fn=lambda pid, m: sent.append(m) or True, now=_NOW)
    assert tally["sent"] == 1
    assert sent[0].class_ == "falsifier_breach"
    assert "NPL" in sent[0].body and "NU" in sent[0].body
    # A moment is considered exactly once — rerun sees nothing new
    again = run_governor(db, send_fn=lambda pid, m: True, now=_NOW)
    assert again["seen"] == 0


def test_freshness_gate_blocks_stale_and_inferred(db: Path) -> None:
    did = _seed_breach(db, falsifier="Memory cycle rolls over. (inferred)")
    tally = run_governor(db, send_fn=lambda pid, m: True, now=_NOW)
    assert tally["skipped_stale"] == 1 and tally["sent"] == 0

    # Ratify (marker stripped) → a NEW alert moment passes the gate
    conn = sqlite3.connect(str(db))
    try:
        conn.execute("UPDATE decisions SET falsifier='Memory cycle rolls over.' WHERE id=?", (did,))
        conn.execute(
            "INSERT INTO alerts (ticker, trigger_kind, fired_at, evidence_json) VALUES "
            "('NU','decision_condition','2026-07-10T09:00:00',?)",
            (json.dumps({"decision_id": did}),),
        )
        conn.commit()
    finally:
        conn.close()
    tally2 = run_governor(db, send_fn=lambda pid, m: True, now=_NOW)
    assert tally2["sent"] == 1


def test_daily_cap_overflows_to_digest(db: Path) -> None:
    # B9 raised DAILY_CAP to 2 — one more moment than the cap must overflow.
    _seed_breach(db)
    _seed_breach(db, falsifier="a second live breach")
    _seed_stub(db)
    tally = run_governor(db, send_fn=lambda pid, m: True, now=_NOW)
    assert tally["sent"] == DAILY_CAP
    assert tally["digest"] == 1
    assert len(digest_pings(db)) == 1


def test_failed_send_is_retried_on_the_next_run(db: Path) -> None:
    """A delivery FAILURE (send_fn returned False — Telegram down) is the one
    state the once-forever rule exempts: the next run re-attempts the push on
    the SAME row. Once delivered, once-forever reapplies."""
    _seed_breach(db)
    tally = run_governor(db, send_fn=lambda pid, m: False, now=_NOW)
    assert tally["send_failed"] == 1 and tally["sent"] == 0 and tally["digest"] == 0
    assert digest_pings(db) == []  # a failed push is not quietly parked in the digest

    sent: list[Moment] = []
    tally2 = run_governor(db, send_fn=lambda pid, m: sent.append(m) or True, now=_NOW)
    assert tally2["sent"] == 1 and tally2["seen"] == 1
    assert [m.class_ for m in sent] == ["falsifier_breach"]

    conn = sqlite3.connect(str(db))
    try:
        rows = conn.execute("SELECT status FROM coach_pings").fetchall()
    finally:
        conn.close()
    assert [str(r[0]) for r in rows] == ["sent"]  # same UNIQUE row flipped, no duplicate

    tally3 = run_governor(db, send_fn=lambda pid, m: True, now=_NOW)
    assert tally3["seen"] == 0  # delivered → considered exactly once again


def test_no_send_channel_still_parks_in_digest(db: Path) -> None:
    """send_fn=None (dry-run / no bot configured) is NOT a send failure — the
    quiet digest is the delivery surface, and once-forever holds."""
    _seed_breach(db)
    tally = run_governor(db, send_fn=None, now=_NOW)
    assert tally["digest"] == 1 and tally["send_failed"] == 0
    assert len(digest_pings(db)) == 1
    again = run_governor(db, send_fn=lambda pid, m: True, now=_NOW)
    assert again["seen"] == 0  # cap/no-channel overflow is never re-pushed


def test_three_consecutive_dismissals_mute_the_class(db: Path) -> None:
    conn = sqlite3.connect(str(db))
    try:
        for i in range(MUTE_AFTER):
            conn.execute(
                "INSERT INTO coach_pings (class_, key, body, status, source_ref, "
                "created_at, updated_at) VALUES ('intent_followup', ?, 'x', 'sent', "
                "'note:1', '2026-07-09', '2026-07-09')",
                (f"k{i}",),
            )
        conn.commit()
        ids = [int(r[0]) for r in conn.execute("SELECT id FROM coach_pings ORDER BY id")]
    finally:
        conn.close()

    muted_class = None
    for pid in ids:
        recorded, muted = record_dismissal(pid, db_path=db)
        assert recorded
        muted_class = muted or muted_class
    assert muted_class == "intent_followup"

    # Muted class: a fresh open intent gets skipped_muted, never sent
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            "INSERT INTO analyst_notes (kind, status, body, source, created_at, updated_at) "
            "VALUES ('intent','open','LEAP sleeve','capture','2026-06-01','2026-06-01')"
        )
        conn.commit()
    finally:
        conn.close()
    tally = run_governor(db, send_fn=lambda pid, m: True, now=_NOW)
    assert tally["skipped_muted"] == 1 and tally["sent"] == 0

    assert unmute("intent_followup", db_path=db)


def test_three_consecutive_dismissals_of_routed_or_acted_pings_mute_the_class(
    db: Path,
) -> None:
    """P2.2 mute-learning fix: a BRIEF_ROUTED_CLASSES moment is never
    'sent'/'digest' — it is disposed from the brief UI (Today card / mobile
    Inbox / Telegram spb:dismiss_item), so its row is 'routed_to_brief' (not
    yet drained by a compose_brief run) or 'acted' (drained). Before this
    fix, record_dismissal's status filter excluded both, so these four
    classes could never be muted by the owner. Mirrors
    test_three_consecutive_dismissals_mute_the_class but over the two
    P2.2-specific statuses."""
    conn = sqlite3.connect(str(db))
    try:
        # Two already-drained ('acted') rows + one not-yet-drained
        # ('routed_to_brief') row — record_dismissal must accept both.
        for i, status in enumerate(("acted", "acted", "routed_to_brief")):
            conn.execute(
                "INSERT INTO coach_pings (class_, key, body, status, source_ref, "
                "created_at, updated_at) VALUES ('profile_drift', ?, 'x', ?, "
                "'fact:1', '2026-07-09', '2026-07-09')",
                (f"pd{i}", status),
            )
        conn.commit()
        ids = [int(r[0]) for r in conn.execute("SELECT id FROM coach_pings ORDER BY id")]
    finally:
        conn.close()

    muted_class = None
    for pid in ids:
        recorded, muted = record_dismissal(pid, db_path=db)
        assert recorded
        muted_class = muted or muted_class
    assert muted_class == "profile_drift"

    conn = sqlite3.connect(str(db))
    try:
        statuses = [
            str(r[0])
            for r in conn.execute(
                "SELECT status FROM coach_pings WHERE class_ = 'profile_drift' ORDER BY id"
            )
        ]
    finally:
        conn.close()
    assert statuses == ["dismissed", "dismissed", "dismissed"]

    assert unmute("profile_drift", db_path=db)


def test_dismissal_still_rejects_a_ping_still_awaiting_send(db: Path) -> None:
    """A ping that's neither reached the owner (still 'digest', waiting to be
    pushed) nor been drained by the brief cannot be dismissed — only
    sent/digest/routed_to_brief/acted are dismissable statuses; a bare
    'skipped_stale'/'skipped_muted' row (never delivered anywhere) is not."""
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            "INSERT INTO coach_pings (class_, key, body, status, source_ref, "
            "created_at, updated_at) VALUES ('profile_drift', 'pd:x', 'x', "
            "'skipped_stale', 'fact:1', '2026-07-09', '2026-07-09')"
        )
        conn.commit()
        pid = int(conn.execute("SELECT id FROM coach_pings").fetchone()[0])
    finally:
        conn.close()
    recorded, muted = record_dismissal(pid, db_path=db)
    assert recorded is False and muted is None


def test_intent_followup_and_annotation_moments(db: Path) -> None:
    _seed_stub(db)
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            "INSERT INTO analyst_notes (kind, status, body, source, created_at, updated_at) "
            "VALUES ('intent','open','far-OTM LEAP sleeve on the next washout','capture',"
            "'2026-06-01','2026-06-01')"
        )
        conn.commit()
    finally:
        conn.close()
    classes = {m.class_ for m in collect_moments(db, now=_NOW)}
    assert classes == {"retro_annotation", "intent_followup"}


def test_intent_followup_body_points_to_the_ledger_tab_not_ledger_land(db: Path) -> None:
    """/ledger-land does not exist on Telegram — the ping must not tell the
    owner to type a command that will silently vanish."""
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            "INSERT INTO analyst_notes (kind, status, body, source, created_at, updated_at) "
            "VALUES ('intent','open','far-OTM LEAP sleeve on the next washout','capture',"
            "'2026-06-01','2026-06-01')"
        )
        conn.commit()
    finally:
        conn.close()
    moments = [m for m in collect_moments(db, now=_NOW) if m.class_ == "intent_followup"]
    assert moments
    for m in moments:
        assert "/ledger-land" not in m.body
        assert "Ledger tab" in m.body


def test_get_ping_returns_row_or_none(db: Path) -> None:
    _seed_breach(db)
    tally = run_governor(db, send_fn=lambda pid, m: True, now=_NOW)
    assert tally["sent"] == 1
    conn = sqlite3.connect(str(db))
    try:
        ping_id = int(
            conn.execute("SELECT id FROM coach_pings WHERE class_ = 'falsifier_breach'").fetchone()[
                0
            ]
        )
    finally:
        conn.close()
    row = get_ping(ping_id, db_path=db)
    assert row is not None
    assert row.id == ping_id
    assert row.class_ == "falsifier_breach"
    assert row.ticker == "NU"
    assert row.status == "sent"
    assert get_ping(999999, db_path=db) is None


def test_falsifier_breach_ping_gets_the_two_button_keyboard() -> None:
    """execution/run_coach_pings.py's Answer button — a falsifier_breach moment
    with a ticker gets Answer+Dismiss; every other moment class (or a
    falsifier_breach with no ticker) keeps the original Dismiss-only rows."""
    from execution.run_coach_pings import ping_buttons

    breach = Moment(
        class_="falsifier_breach", key="alert:1", ticker="NU", body="x", source_ref="decision:1"
    )
    rows = ping_buttons(7, breach)
    assert rows == [[("Answer: review NU", "cp:review:7"), ("Dismiss", "cp:dismiss:7")]]

    no_ticker = Moment(
        class_="falsifier_breach", key="alert:2", ticker=None, body="x", source_ref="decision:2"
    )
    assert ping_buttons(8, no_ticker) == [[("Dismiss", "cp:dismiss:8")]]

    annotation = Moment(
        class_="retro_annotation", key="annot:1", ticker="NU", body="x", source_ref="decision:1"
    )
    assert ping_buttons(9, annotation) == [[("Dismiss", "cp:dismiss:9")]]

    intent = Moment(
        class_="intent_followup", key="intent:1:0", ticker=None, body="x", source_ref="note:1"
    )
    assert ping_buttons(10, intent) == [[("Dismiss", "cp:dismiss:10")]]


# ---------------------------------------------------------------------------
# tenet_challenge (B5, 2026-07-19 program overhaul)
#
# collect_moments touches decisions/alerts/analyst_notes UNCONDITIONALLY
# (only the falsifier_breach block has its own try/except) — those tables
# are created by migrations 0046/0063/0074, all BELOW 0130 (the `db` fixture
# above's PRIOR_HEAD) and even below 0059. A stamp-past-a-mid-chain-revision
# fixture would silently skip creating them for real, so this fixture instead
# bootstraps the pre-alembic base tables and runs EVERY migration from 0001
# (verbatim pattern: tests/test_decision_journal_view.py) — the only way to
# get a real decisions/alerts/analyst_notes/insight_notes/v_decision_journal
# schema in one DB. These tests get their OWN fixture and never touch the
# `db` fixture other tests in this file rely on.
# ---------------------------------------------------------------------------

_GOV_BOOTSTRAP_DDL = """
CREATE TABLE IF NOT EXISTS tracked_companies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT DEFAULT 'bhanu',
    ticker TEXT NOT NULL,
    name TEXT NOT NULL,
    list_type TEXT NOT NULL CHECK(list_type IN (
        'portfolio', 'watchlist', 'evaluation', 'none', 'etf', 'index_member'
    )),
    added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    sec_validated BOOLEAN DEFAULT 0,
    ir_url TEXT DEFAULT NULL,
    model_url TEXT DEFAULT NULL,
    publishes_release BOOLEAN DEFAULT 0,
    publishes_slides BOOLEAN DEFAULT 0,
    publishes_transcript BOOLEAN DEFAULT 0,
    fmp_data_upto TEXT DEFAULT NULL,
    manual_data_quarters TEXT DEFAULT '[]',
    fmp_data_saved BOOLEAN DEFAULT 0,
    UNIQUE(user_id, ticker)
);
CREATE TABLE IF NOT EXISTS quarterly_artifacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    year INTEGER NOT NULL,
    quarter TEXT NOT NULL,
    has_release_file    BOOLEAN DEFAULT 0,
    has_slides_file     BOOLEAN DEFAULT 0,
    has_transcript_file BOOLEAN DEFAULT 0,
    has_audio_file      BOOLEAN DEFAULT 0,
    step_audio_transcribed BOOLEAN DEFAULT 0,
    step_llm_summarized    BOOLEAN DEFAULT 0,
    step_saydo_analyzed    BOOLEAN DEFAULT 0,
    step_thesis_updated    BOOLEAN DEFAULT 0,
    UNIQUE(ticker, year, quarter)
);
CREATE TABLE IF NOT EXISTS fmp_endpoint_status (
    ticker         TEXT    NOT NULL,
    endpoint       TEXT    NOT NULL,
    period         TEXT    NOT NULL DEFAULT '',
    status         TEXT    NOT NULL,
    http_code      INTEGER,
    record_count   INTEGER,
    earliest_date  TEXT,
    latest_date    TEXT,
    file_path      TEXT,
    file_bytes     INTEGER,
    error_msg      TEXT,
    last_pulled    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (ticker, endpoint, period)
);
"""


@pytest.fixture
def db_head(tmp_path: Path, migrated_db: Callable[..., Path]) -> Path:
    path = tmp_path / "gov_head.db"
    return migrated_db(path)


def _seed_tenet_with_accountability(
    db_head: Path,
    *,
    scope_key: str,
    violated: list[int],
    est_cost_usd: float | None,
    one_liner: str = "verdict",
) -> int:
    from synthesis.tenets import record_tenet

    tenet = record_tenet(body_md=f"belief: {scope_key}", scope_key=scope_key, db_path=db_head)
    conn = sqlite3.connect(str(db_head))
    try:
        meta = {
            "accountability": {
                "as_of_run": "2026-07-09T00:00:00",
                "upheld": [],
                "violated": violated,
                "est_cost_usd": est_cost_usd,
                "one_liner": one_liner,
            }
        }
        conn.execute(
            "UPDATE insight_notes SET meta_json = ? WHERE id = ?", (json.dumps(meta), tenet.id)
        )
        conn.commit()
    finally:
        conn.close()
    return tenet.id


def test_tenet_challenge_moment_from_seeded_meta_highest_cost_wins(db_head: Path) -> None:
    _seed_tenet_with_accountability(
        db_head, scope_key="low-cost", violated=[1], est_cost_usd=-100.0
    )
    high_id = _seed_tenet_with_accountability(
        db_head, scope_key="high-cost", violated=[2, 3], est_cost_usd=-9000.0
    )
    moments = collect_moments(db_head, now=_NOW)
    challenge = [m for m in moments if m.class_ == "tenet_challenge"]
    assert len(challenge) == 1
    m = challenge[0]
    assert m.source_ref == f"tenet:{high_id}"
    iso = _NOW.isocalendar()
    assert m.key == f"tenet_challenge:{high_id}:{iso[0]}-W{iso[1]:02d}"
    assert "violated 2x" in m.body
    assert "9,000" in m.body


def test_tenet_challenge_ties_prefer_most_violations(db_head: Path) -> None:
    _seed_tenet_with_accountability(db_head, scope_key="a", violated=[1], est_cost_usd=-500.0)
    high_v = _seed_tenet_with_accountability(
        db_head, scope_key="b", violated=[1, 2], est_cost_usd=-500.0
    )
    moments = collect_moments(db_head, now=_NOW)
    challenge = [m for m in moments if m.class_ == "tenet_challenge"]
    assert len(challenge) == 1
    assert challenge[0].source_ref == f"tenet:{high_v}"


def test_tenet_challenge_ignores_tenets_with_no_violations(db_head: Path) -> None:
    from synthesis.tenets import record_tenet

    record_tenet(body_md="a clean tenet", scope_key="clean", db_path=db_head)
    _seed_tenet_with_accountability(db_head, scope_key="also-clean", violated=[], est_cost_usd=None)
    moments = collect_moments(db_head, now=_NOW)
    assert [m for m in moments if m.class_ == "tenet_challenge"] == []


def test_tenet_challenge_run_governor_once_per_week(db_head: Path) -> None:
    _seed_tenet_with_accountability(db_head, scope_key="x", violated=[1], est_cost_usd=-100.0)
    tally1 = run_governor(db_head, now=_NOW)
    assert tally1["seen"] == 1
    # No send_fn configured — the ping still lands (quietly) in the digest.
    assert tally1["digest"] == 1

    tally2 = run_governor(db_head, now=_NOW)
    # anti-nag: same class+key (same iso-week) already has a non-send_failed
    # row — the second run must not re-collect it at all.
    assert tally2["seen"] == 0


# ---------------------------------------------------------------------------
# post_mortem (B6, 2026-07-19 program overhaul)
#
# Reuses the db_head fixture above (position_entries is 0088, well below
# head; analyst_notes.position_entry_id is 0093) — never the `db` fixture.
# ---------------------------------------------------------------------------


def _seed_drafted_postmortem(db_head: Path, *, ticker: str = "NU", outcome: str = "broke") -> int:
    """A closed position_entries row drafted via the REAL
    synthesis.exit_postmortem.apply_draft path (not raw SQL) — so the
    llm_draft provenance note lands exactly the way the 18:00 sweep would
    write it."""
    from position_lifecycle import get_entry
    from synthesis.exit_postmortem import Draft, apply_draft

    now = "2026-07-09T00:00:00"
    conn = sqlite3.connect(str(db_head))
    try:
        cur = conn.execute(
            """
            INSERT INTO position_entries
                (user_id, ticker, entry_date, entry_price, exit_date, exit_price,
                 source, created_at, updated_at)
            VALUES ('bhanu', ?, '2026-01-01', 10.0, '2026-06-01', 12.0,
                    'reconciler', ?, ?)
            """,
            (ticker, now, now),
        )
        entry_id = int(cur.lastrowid or 0)
        conn.commit()
    finally:
        conn.close()

    entry = get_entry(entry_id, db_path=db_head)
    assert entry is not None
    draft = Draft(
        exit_reason="Funding costs eroded the margin thesis.",
        lessons="Watch deposit beta earlier.",
        outcome_vs_thesis=outcome,
    )
    assert apply_draft(entry, draft, db_path=db_head) is True
    return entry_id


def test_post_mortem_moment_from_drafted_entry(db_head: Path) -> None:
    entry_id = _seed_drafted_postmortem(db_head, ticker="NU", outcome="broke")
    moments = collect_moments(db_head, now=_NOW)
    pm = [m for m in moments if m.class_ == "post_mortem"]
    assert len(pm) == 1
    m = pm[0]
    assert m.key == f"post_mortem:{entry_id}"
    assert m.ticker == "NU"
    assert m.source_ref == f"position_entry:{entry_id}"
    assert "NU" in m.body
    assert "broke" in m.body


def test_post_mortem_ignores_owner_graded_entries(db_head: Path) -> None:
    # A manually-graded row -- no linked llm_draft provenance note -- must
    # never fire a post_mortem moment.
    now = "2026-07-09T00:00:00"
    conn = sqlite3.connect(str(db_head))
    try:
        conn.execute(
            """
            INSERT INTO position_entries
                (user_id, ticker, entry_date, exit_date, exit_reason, lessons,
                 outcome_vs_thesis, source, created_at, updated_at)
            VALUES ('bhanu', 'WIX', '2026-01-01', '2026-06-01', 'owner reason',
                    'owner lesson', 'played_out', 'reconciler', ?, ?)
            """,
            (now, now),
        )
        conn.commit()
    finally:
        conn.close()
    moments = collect_moments(db_head, now=_NOW)
    assert [m for m in moments if m.class_ == "post_mortem"] == []


def test_post_mortem_run_governor_once_ever(db_head: Path) -> None:
    _seed_drafted_postmortem(db_head, ticker="NU")
    tally1 = run_governor(db_head, now=_NOW)
    assert tally1["seen"] == 1
    # No send_fn configured -- the ping still lands (quietly) in the digest.
    assert tally1["digest"] == 1

    tally2 = run_governor(db_head, now=_NOW)
    # anti-nag: same class+key already has a non-send_failed row -- the
    # second run must not re-collect it at all (post_mortem has no
    # week-bucketing, unlike tenet_challenge -- once-ever, full stop).
    assert tally2["seen"] == 0


# --------------------------------------------------------------------------- #
# B9: priority ordering + raised caps
# --------------------------------------------------------------------------- #


def test_competing_moments_send_in_class_priority_order(
    db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Money-at-risk speaks first, coaching last: with DAILY_CAP=2 and three
    competing moments collected in the WRONG order, the two highest-priority
    classes send and the coaching class lands digest.

    Uses tenet_challenge/intent_followup/post_mortem rather than
    profile_drift/calibration_finding — those two are P2.2
    BRIEF_ROUTED_CLASSES now and never contend for a send/digest slot at all
    (see test_brief_routed_classes_never_send_or_digest below); this test's
    job is purely the priority-ORDER contract for classes still on the
    legacy send path."""
    import research.governor as governor_mod

    fabricated = [
        Moment("intent_followup", "if:1", None, "intent still open", "note:1"),
        Moment("falsifier_breach", "alert:901", "NU", "falsifier broke", "decision:1"),
        Moment("post_mortem", "pm:1", "NU", "post-mortem drafted", "position_entry:1"),
    ]
    monkeypatch.setattr(governor_mod, "collect_moments", _fake_collect_moments_factory(fabricated))
    monkeypatch.setattr(governor_mod, "freshness_ok", _always_fresh)

    sent: list[str] = []
    tally = governor_mod.run_governor(
        db, send_fn=lambda pid, m: sent.append(m.class_) or True, now=_NOW
    )
    assert tally["sent"] == 2 and tally["digest"] == 1
    assert sent == ["falsifier_breach", "post_mortem"]
    assert [str(p[1]) for p in digest_pings(db)] == ["intent_followup"]


def test_brief_routed_classes_never_send_or_digest(
    db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P2.2 ownership rule (personal_investment_partner_prd.md §9.1/§3.3): the
    four moment classes calibration_finding/capacity_breach/
    life_event_checkpoint/profile_drift stop delivering as standalone
    pings — a fresh, unmuted moment in one of these classes lands
    status='routed_to_brief' regardless of DAILY_CAP/WEEKLY_CAP, send_fn is
    NEVER called for it, and it does not consume a cap slot (a sibling
    falsifier_breach collected alongside it still sends normally)."""
    import research.governor as governor_mod
    from research.governor import BRIEF_ROUTED_CLASSES

    assert {
        "calibration_finding",
        "capacity_breach",
        "life_event_checkpoint",
        "profile_drift",
    } == BRIEF_ROUTED_CLASSES

    fabricated = [
        Moment("calibration_finding", "cal:1", None, "cohort under bar", "calibration:x"),
        Moment("capacity_breach", "cap:1", None, "cap breached", "alert:1"),
        Moment("life_event_checkpoint", "life:1", None, "life event window", "fact:1"),
        Moment("profile_drift", "pd:1", None, "profile drifted", "fact:2"),
        Moment("falsifier_breach", "alert:901", "NU", "falsifier broke", "decision:1"),
    ]
    monkeypatch.setattr(governor_mod, "collect_moments", _fake_collect_moments_factory(fabricated))
    monkeypatch.setattr(governor_mod, "freshness_ok", _always_fresh)

    called: list[str] = []
    tally = governor_mod.run_governor(
        db, send_fn=lambda pid, m: called.append(m.class_) or True, now=_NOW
    )
    assert tally["routed_to_brief"] == 4
    assert tally["sent"] == 1 and tally["digest"] == 0
    assert called == ["falsifier_breach"]  # send_fn never invoked for the routed four

    conn = sqlite3.connect(str(db))
    try:
        rows = dict(
            conn.execute(
                "SELECT class_, status FROM coach_pings WHERE class_ != 'falsifier_breach'"
            )
        )
    finally:
        conn.close()
    assert set(rows.values()) == {"routed_to_brief"}

    # A once-forever moment: rerunning sees nothing new for the routed rows.
    again = governor_mod.run_governor(db, send_fn=lambda pid, m: True, now=_NOW)
    assert again["seen"] == 0


def test_pending_routed_to_brief_and_mark_briefed(
    db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The brief's drain contract: pending_routed_to_brief lists undrained
    routed rows oldest-first; mark_pings_briefed flips them to 'acted' and is
    idempotent (a second call touches nothing)."""
    import research.governor as governor_mod
    from research.governor import mark_pings_briefed, pending_routed_to_brief

    fabricated = [Moment("profile_drift", "pd:2", None, "profile drifted again", "fact:3")]
    monkeypatch.setattr(governor_mod, "collect_moments", _fake_collect_moments_factory(fabricated))
    monkeypatch.setattr(governor_mod, "freshness_ok", _always_fresh)
    governor_mod.run_governor(db, send_fn=None, now=_NOW)

    pending = pending_routed_to_brief(db)
    assert len(pending) == 1 and pending[0].class_ == "profile_drift"

    n = mark_pings_briefed([p.id for p in pending], db_path=db)
    assert n == 1
    assert pending_routed_to_brief(db) == []

    again = mark_pings_briefed([pending[0].id], db_path=db)
    assert again == 0  # already 'acted' — idempotent no-op


def test_weekly_cap_is_eight(db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """WEEKLY_CAP 3 -> 8 (B9): the 9th send of the week digests even on a
    fresh day. Seed 8 already-sent rows spread earlier in the same week."""
    import sqlite3 as _sqlite3

    import research.governor as governor_mod

    conn = _sqlite3.connect(str(db))
    try:
        for i in range(8):
            # Inside the rolling 7-day window (week_cut = _NOW - 7d =
            # 07-03T12:00) but outside the 1-day window (day_cut 07-09T12:00).
            stamp = f"2026-07-0{4 + (i % 6)}T09:00:00"
            conn.execute(
                "INSERT INTO coach_pings (class_, key, ticker, body, status, source_ref, "
                "created_at, updated_at) VALUES ('intent_followup', ?, NULL, 'x', 'sent', "
                "NULL, ?, ?)",
                (f"wk:{i}", stamp, stamp),
            )
        conn.commit()
    finally:
        conn.close()
    fabricated = [Moment("falsifier_breach", "alert:902", "NU", "b", "decision:2")]
    monkeypatch.setattr(governor_mod, "collect_moments", _fake_collect_moments_factory(fabricated))
    monkeypatch.setattr(governor_mod, "freshness_ok", _always_fresh)
    tally = governor_mod.run_governor(db, send_fn=lambda pid, m: True, now=_NOW)
    assert tally["sent"] == 0 and tally["digest"] == 1


def test_class_priority_covers_every_class() -> None:
    """A class missing from CLASS_PRIORITY silently sorts last — force every
    registered class to claim an explicit rank."""
    from research.governor import CLASS_PRIORITY, CLASSES

    assert set(CLASSES) == set(CLASS_PRIORITY)
