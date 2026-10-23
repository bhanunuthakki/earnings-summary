"""The nag governor — moment collection, freshness gate, caps, auto-mute (W2)."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from research.governor import (
    MUTE_AFTER,
    Moment,
    collect_moments,
    digest_pings,
    get_ping,
    record_dismissal,
    run_governor,
    unmute,
)

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
def db(tmp_path: Path, migrated_db: Callable[[Path], Path]) -> Path:
    path = migrated_db(tmp_path / "gov.db")
    with sqlite3.connect(path) as conn:
        conn.execute(
            "INSERT INTO tracked_companies (ticker, name, list_type) VALUES ('NU', 'Nu', 'portfolio')"
        )
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
            "INSERT INTO alerts (ticker, trigger_kind, fired_at, evidence_json, signature_sha) VALUES "
            "('NU','decision_condition','2026-07-10T08:00:00',?,?)",
            (json.dumps({"decision_id": did}), f"fixture-breach-{did}"),
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


def test_collect_and_digest_falsifier_breach(db: Path) -> None:
    _seed_breach(db)
    tally = run_governor(db, now=_NOW)
    assert tally["digest"] == 1
    finding = digest_pings(db)[0]
    assert finding[1] == "falsifier_breach"
    assert "NPL" in str(finding[2]) and "NU" in str(finding[2])
    # A moment is considered exactly once — rerun sees nothing new
    again = run_governor(db, now=_NOW)
    assert again["seen"] == 0


def test_freshness_gate_blocks_stale_and_inferred(db: Path) -> None:
    did = _seed_breach(db, falsifier="Memory cycle rolls over. (inferred)")
    tally = run_governor(db, now=_NOW)
    assert tally["skipped_stale"] == 1 and tally["sent"] == 0

    # Ratify (marker stripped) → a NEW alert moment passes the gate
    conn = sqlite3.connect(str(db))
    try:
        conn.execute("UPDATE decisions SET falsifier='Memory cycle rolls over.' WHERE id=?", (did,))
        conn.execute(
            "INSERT INTO alerts (ticker, trigger_kind, fired_at, evidence_json, signature_sha) VALUES "
            "('NU','decision_condition','2026-07-10T09:00:00',?,?)",
            (json.dumps({"decision_id": did}), f"fixture-ratified-{did}"),
        )
        conn.commit()
    finally:
        conn.close()
    tally2 = run_governor(db, now=_NOW)
    assert tally2["digest"] == 1


def test_all_fresh_findings_enter_digest(db: Path) -> None:
    _seed_breach(db)
    _seed_breach(db, falsifier="a second live breach")
    _seed_stub(db)
    tally = run_governor(db, now=_NOW)
    assert tally["sent"] == 0 and tally["digest"] == 3
    assert len(digest_pings(db)) == 3


def test_historical_failed_delivery_enters_digest_on_the_same_row(db: Path) -> None:
    _seed_breach(db)
    run_governor(db, now=_NOW)
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE coach_pings SET status='send_failed'")
        before = conn.execute("SELECT * FROM coach_pings").fetchone()
    assert before is not None
    tally = run_governor(db, now=_NOW)
    assert tally == {
        "sent": 0,
        "digest": 1,
        "skipped_muted": 0,
        "skipped_stale": 0,
        "send_failed": 0,
        "seen": 1,
    }
    with sqlite3.connect(db) as conn:
        after = conn.execute("SELECT * FROM coach_pings").fetchone()
        columns = [str(row[1]) for row in conn.execute("PRAGMA table_info(coach_pings)")]
    expected = list(before)
    expected[columns.index("status")] = "digest"
    assert after == tuple(expected)
    assert run_governor(db, now=_NOW)["seen"] == 0


def test_acknowledged_linked_episode_suppresses_send_failed_retry(
    db_head: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import research.governor as governor_mod
    from compute.thesis_episode_attention import acknowledge_episode

    episode_id = "episode-governor-ack"
    moment = Moment("falsifier_breach", "alert:902", "WIX", "review", "decision:2")
    with sqlite3.connect(db_head) as connection:
        stamp = datetime(2026, 7, 10, 12, 0, tzinfo=UTC).isoformat()
        connection.execute(
            "INSERT INTO thesis_evaluation_episodes "
            "(episode_id,ticker,fingerprint_policy_version,semantic_input_json,"
            "semantic_input_sha256,evaluator_semantic_version,result_sha256,"
            "overall_status,provenance_completeness,first_evaluated_at,last_seen_at,"
            "last_checked_at,duplicate_run_count,rule_evaluations_json,created_at) "
            "VALUES (?, 'WIX','legacy_v0','{}',?,'legacy/v0',?,'warn','partial',"
            "?,?,?,0,'[]',?)",
            (episode_id, "a" * 64, "b" * 64, stamp, stamp, stamp, stamp),
        )
        connection.execute(
            "INSERT INTO coach_pings "
            '(class_,"key",ticker,body,status,source_ref,created_at,updated_at,'
            "thesis_evaluation_episode_id,review_cycle_id) "
            "VALUES (?,?,?,?,?,?,?,?,?,'initial')",
            (
                moment.class_,
                moment.key,
                moment.ticker,
                moment.body,
                "send_failed",
                moment.source_ref,
                stamp,
                stamp,
                episode_id,
            ),
        )
        acknowledge_episode(
            connection,
            episode_id,
            acknowledged_at=datetime(2026, 7, 10, 12, 1, tzinfo=UTC),
        )
        # Model a late failed-send callback arriving after acknowledgement.
        connection.execute(
            "UPDATE coach_pings SET status='send_failed' WHERE thesis_evaluation_episode_id=?",
            (episode_id,),
        )
        connection.commit()

    monkeypatch.setattr(
        governor_mod,
        "collect_moments",
        _fake_collect_moments_factory([moment]),
    )
    monkeypatch.setattr(governor_mod, "freshness_ok", _always_fresh)
    tally = run_governor(db_head, now=_NOW)

    assert tally["sent"] == 0
    with sqlite3.connect(db_head) as connection:
        assert connection.execute(
            "SELECT status FROM coach_pings WHERE thesis_evaluation_episode_id=?",
            (episode_id,),
        ).fetchone() == ("acted",)


def test_acknowledgement_during_freshness_gate_preserves_owner_state(
    db_head: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An owner action between the attention and freshness gates wins the retry CAS."""
    import research.governor as governor_mod
    from compute.thesis_episode_attention import acknowledge_episode

    episode_id = "episode-governor-gate-race"
    moment = Moment("falsifier_breach", "alert:904", "WIX", "review", "decision:4")
    with sqlite3.connect(db_head) as connection:
        stamp = datetime(2026, 7, 10, 12, 0, tzinfo=UTC).isoformat()
        connection.execute(
            "INSERT INTO thesis_evaluation_episodes "
            "(episode_id,ticker,fingerprint_policy_version,semantic_input_json,"
            "semantic_input_sha256,evaluator_semantic_version,result_sha256,"
            "overall_status,provenance_completeness,first_evaluated_at,last_seen_at,"
            "last_checked_at,duplicate_run_count,rule_evaluations_json,created_at) "
            "VALUES (?, 'WIX','legacy_v0','{}',?,'legacy/v0',?,'warn','partial',"
            "?,?,?,0,'[]',?)",
            (episode_id, "e" * 64, "f" * 64, stamp, stamp, stamp, stamp),
        )
        connection.execute(
            "INSERT INTO coach_pings "
            '(class_,"key",ticker,body,status,source_ref,created_at,updated_at,'
            "thesis_evaluation_episode_id,review_cycle_id) "
            "VALUES (?,?,?,?,'send_failed',?,?,?,?,'initial')",
            (
                moment.class_,
                moment.key,
                moment.ticker,
                moment.body,
                moment.source_ref,
                stamp,
                stamp,
                episode_id,
            ),
        )
        connection.commit()

    def acknowledge_then_pass(*_a: object, **_kw: object) -> bool:
        with sqlite3.connect(db_head) as connection:
            acknowledge_episode(
                connection,
                episode_id,
                acknowledged_at=datetime(2026, 7, 10, 12, 1, tzinfo=UTC),
            )
            connection.commit()
        return True

    monkeypatch.setattr(governor_mod, "collect_moments", _fake_collect_moments_factory([moment]))
    monkeypatch.setattr(governor_mod, "freshness_ok", acknowledge_then_pass)
    run_governor(db_head, now=_NOW)
    with sqlite3.connect(db_head) as connection:
        assert connection.execute(
            "SELECT status FROM coach_pings WHERE thesis_evaluation_episode_id=?",
            (episode_id,),
        ).fetchone() == ("acted",)


def test_linked_attention_defect_fails_visibly_and_keeps_retry_state(
    db_head: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import compute.thesis_episode_attention as attention_mod
    import research.governor as governor_mod

    episode_id = "episode-governor-defect"
    moment = Moment("falsifier_breach", "alert:904", "WIX", "review", "decision:4")
    with sqlite3.connect(db_head) as connection:
        stamp = datetime(2026, 7, 10, 12, 0, tzinfo=UTC).isoformat()
        connection.execute(
            "INSERT INTO coach_pings "
            '(class_,"key",ticker,body,status,source_ref,created_at,updated_at,'
            "thesis_evaluation_episode_id,review_cycle_id) "
            "VALUES (?,?,?,?,?,?,?,?,?,'initial')",
            (
                moment.class_,
                moment.key,
                moment.ticker,
                moment.body,
                "send_failed",
                moment.source_ref,
                stamp,
                stamp,
                episode_id,
            ),
        )
        connection.commit()

    monkeypatch.setattr(
        governor_mod,
        "collect_moments",
        _fake_collect_moments_factory([moment]),
    )

    def broken_attention(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("attention decoder defect")

    monkeypatch.setattr(attention_mod, "get_attention", broken_attention)

    with pytest.raises(RuntimeError, match="attention decoder defect"):
        run_governor(db_head, now=_NOW)

    with sqlite3.connect(db_head) as connection:
        assert connection.execute(
            "SELECT status FROM coach_pings WHERE thesis_evaluation_episode_id=?",
            (episode_id,),
        ).fetchone() == ("send_failed",)


def test_linked_partial_attention_schema_fails_visibly_and_keeps_retry_state(
    db_head: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import research.governor as governor_mod

    episode_id = "episode-governor-partial-schema"
    moment = Moment("falsifier_breach", "alert:905", "WIX", "review", "decision:5")
    with sqlite3.connect(db_head) as connection:
        stamp = datetime(2026, 7, 10, 12, 0, tzinfo=UTC).isoformat()
        connection.execute(
            "INSERT INTO coach_pings "
            '(class_,"key",ticker,body,status,source_ref,created_at,updated_at,'
            "thesis_evaluation_episode_id,review_cycle_id) "
            "VALUES (?,?,?,?,?,?,?,?,?,'initial')",
            (
                moment.class_,
                moment.key,
                moment.ticker,
                moment.body,
                "send_failed",
                moment.source_ref,
                stamp,
                stamp,
                episode_id,
            ),
        )
        connection.execute(
            "ALTER TABLE thesis_evaluation_episodes DROP COLUMN acknowledgement_note"
        )
        connection.commit()

    monkeypatch.setattr(
        governor_mod,
        "collect_moments",
        _fake_collect_moments_factory([moment]),
    )

    with pytest.raises(RuntimeError, match="incomplete thesis episode attention schema"):
        run_governor(db_head, now=_NOW)

    with sqlite3.connect(db_head) as connection:
        assert connection.execute(
            "SELECT status FROM coach_pings WHERE thesis_evaluation_episode_id=?",
            (episode_id,),
        ).fetchone() == ("send_failed",)


def test_digest_delivery_is_idempotent(db: Path) -> None:
    """The quiet digest is the delivery surface, and once-forever holds."""
    _seed_breach(db)
    tally = run_governor(db, now=_NOW)
    assert tally["digest"] == 1 and tally["send_failed"] == 0
    assert len(digest_pings(db)) == 1
    again = run_governor(db, now=_NOW)
    assert again["seen"] == 0  # cap/no-channel overflow is never re-pushed


def _only_ping_status(db: Path) -> str:
    with sqlite3.connect(db) as conn:
        rows = conn.execute("SELECT status FROM coach_pings").fetchall()
    assert len(rows) == 1
    return str(rows[0][0])


def test_expired_claim_is_swept_even_when_its_moment_is_no_longer_collected(
    db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import research.governor as governor_mod

    _seed_breach(db)
    run_governor(db, now=_NOW)
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE coach_pings SET status='sending'")
    monkeypatch.setattr(governor_mod, "collect_moments", _fake_collect_moments_factory([]))
    run_governor(db, now=_NOW + timedelta(minutes=10))
    assert _only_ping_status(db) == "sending"
    run_governor(db, now=_NOW + timedelta(hours=1))
    assert _only_ping_status(db) == "send_failed"


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
    tally = run_governor(db, now=_NOW)
    assert tally["skipped_muted"] == 1 and tally["sent"] == 0

    assert unmute("intent_followup", db_path=db)


def test_three_consecutive_dismissals_of_routed_or_acted_pings_mute_the_class(
    db: Path,
) -> None:
    """Legacy brief-routed rows remain dismissable after product retirement."""
    conn = sqlite3.connect(str(db))
    try:
        # Historical terminal and routed rows remain valid owner-control inputs.
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
    tally = run_governor(db, now=_NOW)
    assert tally["digest"] == 1
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
    assert row.status == "digest"
    assert get_ping(999999, db_path=db) is None


# ---------------------------------------------------------------------------
# tenet_challenge (B5, 2026-07-19 program overhaul)
#
# These integration cases use the same cached current schema as the other governor tests.


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
    # The finding is retained in the quiet digest.
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
    # The finding is retained in the quiet digest.
    assert tally1["digest"] == 1

    tally2 = run_governor(db_head, now=_NOW)
    # anti-nag: same class+key already has a non-send_failed row -- the
    # second run must not re-collect it at all (post_mortem has no
    # week-bucketing, unlike tenet_challenge -- once-ever, full stop).
    assert tally2["seen"] == 0


# --------------------------------------------------------------------------- #
# Stable finding priority and historical delivery rows
# --------------------------------------------------------------------------- #


def test_findings_enter_digest_in_class_priority_order(
    db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Preserve class priority when findings are collected in another order."""
    import research.governor as governor_mod

    fabricated = [
        Moment("intent_followup", "if:1", None, "intent still open", "note:1"),
        Moment("falsifier_breach", "alert:901", "NU", "falsifier broke", "decision:1"),
        Moment("post_mortem", "pm:1", "NU", "post-mortem drafted", "position_entry:1"),
    ]
    monkeypatch.setattr(governor_mod, "collect_moments", _fake_collect_moments_factory(fabricated))
    monkeypatch.setattr(governor_mod, "freshness_ok", _always_fresh)

    tally = governor_mod.run_governor(db, now=_NOW)
    assert tally["sent"] == 0 and tally["digest"] == 3
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT class_ FROM coach_pings ORDER BY id").fetchall() == [
            ("falsifier_breach",),
            ("post_mortem",),
            ("intent_followup",),
        ]


def test_historical_sent_rows_do_not_change_digest_delivery(
    db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep sent history intact when past delivery caps have already been reached."""
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
    tally = governor_mod.run_governor(db, now=_NOW)
    assert tally["sent"] == 0 and tally["digest"] == 1
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM coach_pings WHERE status='sent'").fetchone() == (
            8,
        )


def test_class_priority_covers_every_class() -> None:
    """A class missing from CLASS_PRIORITY silently sorts last — force every
    registered class to claim an explicit rank."""
    from research.governor import CLASS_PRIORITY, CLASSES

    assert set(CLASSES) == set(CLASS_PRIORITY)


def test_quiet_findings_are_written_directly_to_digest(
    db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import research.governor as governor_mod
    from user_state._db import open_conn

    moment = Moment("calibration_finding", "direct-digest", None, "finding", "test:1")
    monkeypatch.setattr(governor_mod, "collect_moments", _fake_collect_moments_factory([moment]))
    monkeypatch.setattr(governor_mod, "freshness_ok", _always_fresh)
    statements: list[str] = []

    def traced_conn(path: Path | str | None) -> sqlite3.Connection:
        conn = open_conn(path)
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(governor_mod, "open_conn", traced_conn)
    tally = run_governor(db, now=_NOW)
    assert tally == {
        "sent": 0,
        "digest": 1,
        "skipped_muted": 0,
        "skipped_stale": 0,
        "send_failed": 0,
        "seen": 1,
    }
    assert not any(
        "COUNT(*)" in sql or ("'sending'" in sql and "INSERT" in sql) for sql in statements
    )
    assert sum("UPDATE coach_pings" in sql for sql in statements) == 1  # historical lease sweep


@pytest.mark.parametrize(
    ("prior_status", "age_minutes", "expected_status", "seen"),
    [
        ("sending", 14, "sending", 0),
        ("sending", 15, "digest", 1),
        ("send_failed", 60, "digest", 1),
        ("sent", 60, "sent", 0),
        ("acted", 60, "acted", 0),
        ("dismissed", 60, "dismissed", 0),
        ("digest", 60, "digest", 0),
    ],
)
def test_historical_delivery_state_retains_full_row_except_recovery_fields(
    db: Path,
    monkeypatch: pytest.MonkeyPatch,
    prior_status: str,
    age_minutes: int,
    expected_status: str,
    seen: int,
) -> None:
    import research.governor as governor_mod

    moment = Moment("calibration_finding", "historical", None, "current body", "test:current")
    before_stamp = (_NOW - timedelta(minutes=age_minutes)).isoformat()
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO coach_pings "
            "(class_,key,ticker,body,status,source_ref,created_at,updated_at,telegram_message_id) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (
                moment.class_,
                moment.key,
                "NU",
                "historical body",
                prior_status,
                "test:historical",
                before_stamp,
                before_stamp,
                77,
            ),
        )
        before = conn.execute("SELECT * FROM coach_pings").fetchone()
        columns = [str(row[1]) for row in conn.execute("PRAGMA table_info(coach_pings)")]
    assert before is not None
    monkeypatch.setattr(governor_mod, "collect_moments", _fake_collect_moments_factory([moment]))
    monkeypatch.setattr(governor_mod, "freshness_ok", _always_fresh)
    tally = run_governor(db, now=_NOW)
    assert tally == {
        "sent": 0,
        "digest": seen,
        "skipped_muted": 0,
        "skipped_stale": 0,
        "send_failed": 0,
        "seen": seen,
    }
    expected = list(before)
    expected[columns.index("status")] = expected_status
    if seen:
        expected[columns.index("updated_at")] = _NOW.isoformat()
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT * FROM coach_pings").fetchall() == [tuple(expected)]
