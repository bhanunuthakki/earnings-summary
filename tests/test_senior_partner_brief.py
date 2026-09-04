# pyright: reportPrivateUsage=false
"""Tests for src/advisor/senior_partner_brief.py — the P2.2 governed
composition pipeline (PRD §9.1).

The deterministic-gathering layer runs FOR REAL against a hand-built SQLite
DB (mirrors tests/test_governor.py / tests/test_open_loops.py's pattern of a
minimal table subset rather than a full alembic head) — every ``_gather_*``
helper degrades to empty on a missing table, so a test only needs to create
the tables its scenario actually exercises. ``call_llm_structured`` is
monkeypatched at the module seam per the repo's never-spend convention — no
real LLM call anywhere in this file.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from typing import cast

import pytest

import advisor.senior_partner_brief as spb
from llm.cli import LLMBudgetExceeded

_NOW = datetime(2026, 7, 20, 9, 0, 0)

_BASE_DDL = """
CREATE TABLE llm_artifacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker VARCHAR(16),
    scope VARCHAR(64) NOT NULL DEFAULT 'ticker',
    purpose VARCHAR(64) NOT NULL,
    fiscal_period VARCHAR(10),
    content_md TEXT,
    content_json TEXT,
    input_sha256 VARCHAR(64) NOT NULL,
    output_sha256 VARCHAR(64),
    model VARCHAR(64),
    prompt_version VARCHAR(32) NOT NULL DEFAULT 'v1',
    generated_at DATETIME NOT NULL,
    expires_at DATETIME,
    superseded_by_id INTEGER,
    dirty BOOLEAN NOT NULL DEFAULT 0,
    dirty_reason VARCHAR(128),
    source_doc_ids TEXT,
    parent_artifact_ids TEXT,
    llm_call_id INTEGER
);
CREATE TABLE coach_pings (
    id INTEGER PRIMARY KEY,
    class_ VARCHAR(32) NOT NULL,
    key VARCHAR(128) NOT NULL,
    ticker VARCHAR(16),
    body TEXT NOT NULL,
    status VARCHAR(16) NOT NULL,
    source_ref VARCHAR(128),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker VARCHAR(16),
    recommendation_kind VARCHAR(32) NOT NULL,
    decided_by VARCHAR(16) NOT NULL DEFAULT 'advisor',
    created_at DATETIME NOT NULL
);
CREATE TABLE coach_mutes (
    class_ VARCHAR(32) PRIMARY KEY,
    muted_at TEXT NOT NULL,
    reason TEXT
);
"""


def _make_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(_BASE_DDL)
    conn.commit()
    conn.close()
    return db_path


def _seed_routed_ping(
    db_path: Path, *, class_: str = "profile_drift", ticker: str | None = None
) -> int:
    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.execute(
            "INSERT INTO coach_pings (class_, key, ticker, body, status, source_ref, "
            "created_at, updated_at) VALUES (?, 'k:1', ?, 'body', 'routed_to_brief', 'fact:1', "
            "'2026-07-20T09:00:00', '2026-07-20T09:00:00')",
            (class_, ticker),
        )
        conn.commit()
        return int(cur.lastrowid or 0)
    finally:
        conn.close()


def _seed_portfolio_shift(db_path: Path) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "INSERT INTO decisions (ticker, recommendation_kind, decided_by, created_at) "
            "VALUES ('NU', 'trim', 'owner', '2026-07-18T09:00:00')"
        )
        conn.commit()
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("dirty", "expires_at"),
    [(1, None), (0, "2026-07-19T09:00:00+00:00")],
)
def test_compose_excludes_dirty_or_expired_recommendation_from_prompt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    dirty: int,
    expires_at: str | None,
) -> None:
    db_path = _make_db(tmp_path)
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO llm_artifacts "
        "(scope, purpose, content_json, input_sha256, generated_at, expires_at, dirty) "
        "VALUES ('portfolio', 'incremental_dollar_recommendation', ?, 'sha', ?, ?, ?)",
        (
            '{"status":"ready","central_hypothesis":"stale advice"}',
            "2026-07-18T09:00:00+00:00",
            expires_at,
            dirty,
        ),
    )
    conn.commit()
    conn.close()

    prompts: list[str] = []

    def capture_prompt(prompt: str, **_kwargs: object) -> dict[str, object]:
        prompts.append(prompt)
        return _valid_llm_payload(n_action=0)

    monkeypatch.setattr(spb, "call_llm_structured", capture_prompt)

    spb.compose_brief(db_path, tmp_path, now=_NOW)

    assert prompts
    assert "stale advice" not in prompts[0]


def _fake_call_factory(payload: dict[str, object]) -> object:
    def fake_call(prompt: str, **kw: object) -> object:
        return dict(payload)

    return fake_call


def _raising_call_factory(exc: Exception) -> object:
    def fake_call(prompt: str, **kw: object) -> object:
        raise exc

    return fake_call


def _valid_llm_payload(
    *, n_action: int = 1, source_refs: list[str] | None = None
) -> dict[str, object]:
    items: list[dict[str, object]] = [
        {
            "title": f"item {i}",
            "body": "body text",
            "disposition": "action_requested",
            "effort_estimate": "quick",
            "ticker": None,
            "source_refs": source_refs or [],
        }
        for i in range(n_action)
    ]
    return {
        "what_changed": items,
        "highest_priority_decision": None,
        "capital_use": None,
        "assumption_challenge": None,
        "decision_revisit": None,
        "active_week_explanation": "",
    }


# --------------------------------------------------------------------------- #
# Deterministic validation invariants (PRD §9.1)
# --------------------------------------------------------------------------- #


def test_validate_notification_policy_allows_up_to_three_normal_week() -> None:
    payload = _valid_llm_payload(n_action=3)
    payload.update(
        {
            "as_of": "x",
            "iso_year": 2026,
            "iso_week": 30,
            "input_sha": "sha",
            "is_active_week": False,
        }
    )
    brief = spb.SeniorPartnerBrief.model_validate(payload)
    assert brief.validate_notification_policy() == []


def test_validate_notification_policy_rejects_four_in_normal_week() -> None:
    payload = _valid_llm_payload(n_action=4)
    payload.update(
        {
            "as_of": "x",
            "iso_year": 2026,
            "iso_week": 30,
            "input_sha": "sha",
            "is_active_week": False,
        }
    )
    brief = spb.SeniorPartnerBrief.model_validate(payload)
    reasons = brief.validate_notification_policy()
    assert reasons and "4 action_requested" in reasons[0]


def test_validate_notification_policy_active_week_needs_explanation() -> None:
    payload = _valid_llm_payload(n_action=4)
    payload.update(
        {
            "as_of": "x",
            "iso_year": 2026,
            "iso_week": 30,
            "input_sha": "sha",
            "is_active_week": True,
            "active_week_explanation": "",
        }
    )
    brief = spb.SeniorPartnerBrief.model_validate(payload)
    assert brief.validate_notification_policy() != []

    payload["active_week_explanation"] = "earnings cluster this week"
    brief2 = spb.SeniorPartnerBrief.model_validate(payload)
    assert brief2.validate_notification_policy() == []


def test_validate_grounding_rejects_invented_ref() -> None:
    payload = _valid_llm_payload(n_action=1, source_refs=["not_a_real_ref"])
    payload.update({"as_of": "x", "iso_year": 2026, "iso_week": 30, "input_sha": "sha"})
    brief = spb.SeniorPartnerBrief.model_validate(payload)
    reasons = brief.validate_grounding(allowed_refs={"risk_snapshot:2026-07-01"})
    assert reasons and "not_a_real_ref" in reasons[0]


def test_validate_grounding_accepts_allowed_ref() -> None:
    payload = _valid_llm_payload(n_action=1, source_refs=["risk_snapshot:2026-07-01"])
    payload.update({"as_of": "x", "iso_year": 2026, "iso_week": 30, "input_sha": "sha"})
    brief = spb.SeniorPartnerBrief.model_validate(payload)
    assert brief.validate_grounding(allowed_refs={"risk_snapshot:2026-07-01"}) == []


# --------------------------------------------------------------------------- #
# compose_brief orchestration
# --------------------------------------------------------------------------- #


def test_compose_brief_llm_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = _make_db(tmp_path)
    monkeypatch.setattr(
        spb, "call_llm_structured", _fake_call_factory(_valid_llm_payload(n_action=1))
    )
    result = spb.compose_brief(db_path, tmp_path, now=_NOW)
    assert result.selection_mode == "llm"
    assert result.artifact_id is not None
    assert result.cache_hit is False
    assert len(result.brief.action_requested_items()) == 1
    assert result.brief.is_active_week is False  # nothing seeded — no active signal

    # Same-week re-run with the SAME gathered inputs is a cache hit — no
    # re-spend (call_llm_structured raises if invoked a second time here).
    monkeypatch.setattr(
        spb,
        "call_llm_structured",
        _raising_call_factory(AssertionError("must not re-spend on cache hit")),
    )
    result2 = spb.compose_brief(db_path, tmp_path, now=_NOW)
    assert result2.cache_hit is True
    assert result2.artifact_id == result.artifact_id


def test_compose_brief_exceeds_cap_in_normal_week_falls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = _make_db(tmp_path)
    monkeypatch.setattr(
        spb, "call_llm_structured", _fake_call_factory(_valid_llm_payload(n_action=4))
    )
    result = spb.compose_brief(db_path, tmp_path, now=_NOW)
    assert result.selection_mode == "deterministic_fallback"
    assert any("rejected" in d for d in result.degraded_reasons)
    # The fallback never claims action_requested without a governed judgment.
    assert result.brief.action_requested_items() == []


def test_compose_brief_active_week_with_explanation_allows_more_than_three(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = _make_db(tmp_path)
    _seed_portfolio_shift(db_path)  # an owner 'trim' inside the 7-day window -> active week

    def _payload(*a: object, **kw: object) -> dict[str, object]:
        payload = _valid_llm_payload(n_action=4)
        payload["active_week_explanation"] = (
            "earnings cluster this week — more context, not more pings"
        )
        return payload

    monkeypatch.setattr(spb, "call_llm_structured", _payload)
    result = spb.compose_brief(db_path, tmp_path, now=_NOW)
    assert result.brief.is_active_week is True
    assert result.selection_mode == "llm"
    assert len(result.brief.action_requested_items()) == 4
    assert result.brief.active_week_explanation


def test_compose_brief_budget_exceeded_labeled_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = _make_db(tmp_path)

    def _raise(*a: object, **kw: object) -> object:
        raise LLMBudgetExceeded("senior_partner_brief over budget")

    monkeypatch.setattr(spb, "call_llm_structured", _raise)
    result = spb.compose_brief(db_path, tmp_path, now=_NOW)
    assert result.selection_mode == "deterministic_fallback"
    assert any("budget exceeded" in d for d in result.degraded_reasons)
    # Every item is context_only — never a synthesized action without a
    # governed judgment behind it.
    for item in result.brief.all_items():
        assert item.disposition != "action_requested"


def test_compose_brief_deterministic_fallback_is_explicitly_labeled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fallback content itself names 'mechanical' / 'no LLM synthesis' —
    never presented as if a governed judgment ran (PRD: 'no synthesized
    confidence'). Also proves a routed governor moment resurfaces in the
    fallback digest rather than silently dropping."""
    db_path = _make_db(tmp_path)
    _seed_routed_ping(db_path, class_="profile_drift")

    def _raise(*a: object, **kw: object) -> object:
        raise RuntimeError("transient network blip")

    monkeypatch.setattr(spb, "call_llm_structured", _raise)
    result = spb.compose_brief(db_path, tmp_path, now=_NOW)
    assert result.selection_mode == "deterministic_fallback"
    rendered = spb.render_markdown(result.brief)
    assert "mechanical" in rendered.lower()
    assert "profile_drift" in rendered
    assert "transient LLM failure" in " ".join(result.degraded_reasons)


# --------------------------------------------------------------------------- #
# Governor drain (P2.2 ownership rule)
# --------------------------------------------------------------------------- #


def test_compose_brief_drains_routed_governor_pings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A calibration_finding/profile_drift/etc. moment the governor routed to
    coach_pings.status='routed_to_brief' is drained (flipped to 'acted')
    after a successful compose — never re-drained on the next run — and lands
    as a section 4/5 candidate rather than sending as a standalone ping."""
    db_path = _make_db(tmp_path)
    ping_id = _seed_routed_ping(db_path, class_="profile_drift")

    from research.governor import pending_routed_to_brief

    assert len(pending_routed_to_brief(db_path)) == 1

    def _payload(*a: object, **kw: object) -> dict[str, object]:
        return {
            "what_changed": [],
            "highest_priority_decision": None,
            "capital_use": None,
            "assumption_challenge": {
                "title": "Profile drift resurfaced",
                "body": "the moment the governor routed here",
                "disposition": "context_only",
                "effort_estimate": None,
                "ticker": None,
                "source_refs": [f"coach_ping:{ping_id}"],
            },
            "decision_revisit": None,
            "active_week_explanation": "",
        }

    monkeypatch.setattr(spb, "call_llm_structured", _payload)
    result = spb.compose_brief(db_path, tmp_path, now=_NOW)
    assert result.brief.assumption_challenge is not None
    assert result.brief.routed_ping_ids == [ping_id]
    assert pending_routed_to_brief(db_path) == []  # drained


def test_muted_class_does_not_reappear_in_next_composed_brief(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The full P2.2 mute-learning loop, closing with the ownership rule: a
    class the owner dismissed 3 times (via ``dismiss_routed_moment`` — the
    SAME action core the Today card / mobile Inbox / Telegram callback all
    use) is muted; the governor's EXISTING mute gate (checked before the
    BRIEF_ROUTED_CLASSES routing branch in run_governor) then stops
    routing NEW moments of that class at all, so it never resurfaces in a
    later composed brief — governor mute still gates admission even though
    delivery moved to the brief."""
    db_path = _make_db(tmp_path)

    # Simulate 3 prior weeks' already-drained ('acted') profile_drift
    # moments, each dismissed by the owner via the per-item action core.
    conn = sqlite3.connect(str(db_path))
    try:
        for i in range(3):
            conn.execute(
                "INSERT INTO coach_pings (class_, key, ticker, body, status, source_ref, "
                "created_at, updated_at) VALUES ('profile_drift', ?, NULL, 'x', 'acted', "
                "'fact:1', '2026-07-06T09:00:00', '2026-07-06T09:00:00')",
                (f"pd:{i}",),
            )
        conn.commit()
        ping_ids = [int(r[0]) for r in conn.execute("SELECT id FROM coach_pings ORDER BY id")]
    finally:
        conn.close()

    from advisor.senior_partner_brief import dismiss_routed_moment

    muted_class = None
    for pid in ping_ids:
        recorded, muted = dismiss_routed_moment(pid, db_path=db_path)
        assert recorded
        muted_class = muted or muted_class
    assert muted_class == "profile_drift"

    # A FRESH profile_drift moment (new key — the underlying condition is
    # still active) must now land 'skipped_muted', never 'routed_to_brief'.
    import research.governor as governor_mod
    from research.governor import Moment, pending_routed_to_brief

    fresh_moment = [Moment("profile_drift", "pd:fresh", None, "still drifting", "fact:2")]

    def _fake_collect(*a: object, **kw: object) -> list[Moment]:
        return list(fresh_moment)

    def _always_fresh(*a: object, **kw: object) -> bool:
        return True

    monkeypatch.setattr(governor_mod, "collect_moments", _fake_collect)
    monkeypatch.setattr(governor_mod, "freshness_ok", _always_fresh)
    tally = governor_mod.run_governor(db_path, send_fn=None, now=_NOW)
    assert tally["skipped_muted"] == 1
    assert tally["routed_to_brief"] == 0
    assert pending_routed_to_brief(db_path) == []

    # compose_brief has nothing to drain — the muted class never resurfaces.
    monkeypatch.setattr(
        spb, "call_llm_structured", _fake_call_factory(_valid_llm_payload(n_action=0))
    )
    result = spb.compose_brief(db_path, tmp_path, now=_NOW)
    assert result.brief.routed_ping_ids == []
    assert result.brief.assumption_challenge is None
    assert result.brief.decision_revisit is None


# --------------------------------------------------------------------------- #
# §11.4 privacy — Telegram surface
# --------------------------------------------------------------------------- #


def test_telegram_text_omits_dollar_totals() -> None:
    distinctive_total = "$4,827,193.42"
    brief = spb.SeniorPartnerBrief(
        as_of="2026-07-20T09:00:00",
        iso_year=2026,
        iso_week=30,
        input_sha="sha",
        highest_priority_decision=spb.BriefItem(
            title="Trim NVO",
            body=f"Book total is {distinctive_total} — trim to reduce concentration.",
            disposition="action_requested",
            effort_estimate="moderate",
            ticker="NVO",
        ),
        what_changed=[
            spb.BriefItem(
                title=f"Net worth crossed {distinctive_total}",
                body="context",
                disposition="context_only",
            )
        ],
    )
    text = spb.build_telegram_text(brief)
    assert distinctive_total not in text
    assert "[amount omitted]" in text
    assert "Trim NVO" in text  # ticker/action/rationale still present


def test_telegram_keyboard_links_review_to_private_mobile_inbox() -> None:
    brief = spb.SeniorPartnerBrief(as_of="x", iso_year=2026, iso_week=30, input_sha="sha")
    kb = spb.build_telegram_keyboard(
        brief,
        artifact_id=2104,
        inbox_url="https://desktop.example.ts.net/mobile/inbox",
    )
    rows = cast("list[list[dict[str, object]]]", kb["inline_keyboard"])
    buttons = [btn for row in rows for btn in row]
    assert buttons == [
        {"text": "Why?", "callback_data": "spb:why:2104"},
        {
            "text": "Review in Inbox",
            "url": "https://desktop.example.ts.net/mobile/inbox",
        },
        {"text": "Defer", "callback_data": "spb:defer:2104"},
        {"text": "Dismiss", "callback_data": "spb:dismiss:2104"},
    ]


def test_telegram_keyboard_refuses_delivery_without_private_url() -> None:
    brief = spb.SeniorPartnerBrief(as_of="x", iso_year=2026, iso_week=30, input_sha="sha")
    with pytest.raises(ValueError, match="private mobile Inbox URL is required"):
        spb.build_telegram_keyboard(brief, artifact_id=2104, inbox_url="")


def test_private_mobile_inbox_url_reads_service_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "private_mobile_base_url"
    config_path.write_text("https://desktop.example.ts.net\n", encoding="utf-8")
    monkeypatch.delenv("EARNINGS_SUMMARY_PRIVATE_BASE_URL", raising=False)
    monkeypatch.setattr(spb, "_PRIVATE_BASE_URL_PATH", config_path)

    assert spb.private_mobile_inbox_url() == "https://desktop.example.ts.net/mobile/inbox"


@pytest.mark.parametrize(
    "configured",
    [
        "http://desktop.example.ts.net",
        "https://desktop.example.ts.net/mobile/inbox",
        "https://desktop.example.ts.net?next=other",
    ],
)
def test_private_mobile_inbox_url_rejects_insecure_or_non_origin_base(configured: str) -> None:
    assert spb.private_mobile_inbox_url(configured) is None


# --------------------------------------------------------------------------- #
# render_markdown — five distinct sections
# --------------------------------------------------------------------------- #


def test_render_markdown_five_distinct_sections() -> None:
    brief = spb.SeniorPartnerBrief(as_of="x", iso_year=2026, iso_week=30, input_sha="sha")
    rendered = spb.render_markdown(brief)
    for heading in (
        "1. What changed that matters",
        "2. Highest-priority portfolio decision",
        "3. Best current use of incremental capital",
        "4. An assumption or behavioral pattern worth challenging",
        "5. A prior Owner Decision worth revisiting",
    ):
        assert heading in rendered
    # Empty sections carry a distinct "nothing" phrase, not one repeated blob.
    assert rendered.count("(nothing material this week)") <= 1


# --------------------------------------------------------------------------- #
# Shared direct-SQL connection behavior
# --------------------------------------------------------------------------- #


def _seed_direct_sql_tables(db_path: Path) -> None:
    conn: sqlite3.Connection = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS tracked_companies "
            "(ticker TEXT PRIMARY KEY, list_type TEXT NOT NULL)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS weekly_packet_runs "
            "(id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "iso_year INTEGER NOT NULL, iso_week INTEGER NOT NULL)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS weekly_packet_items "
            "(id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER NOT NULL, "
            "item_kind TEXT NOT NULL, ticker TEXT, title TEXT NOT NULL, "
            "verdict TEXT, order_index INTEGER NOT NULL DEFAULT 0)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS expected_earnings "
            "(ticker TEXT NOT NULL, expected_date TEXT NOT NULL)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS _decision_journal_base "
            "(decision_id INTEGER PRIMARY KEY, ticker TEXT, "
            "recommendation_kind TEXT NOT NULL, made_at TEXT NOT NULL, "
            "falsifier TEXT, decided_by TEXT NOT NULL, "
            "advice_preceded INTEGER NOT NULL, process_quality TEXT)"
        )
        conn.execute(
            "CREATE VIEW IF NOT EXISTS v_decision_journal AS SELECT "
            "decision_id, ticker, recommendation_kind, made_at, falsifier, "
            "decided_by, advice_preceded, process_quality "
            "FROM _decision_journal_base"
        )
        conn.execute(
            "INSERT OR REPLACE INTO tracked_companies (ticker, list_type) VALUES "
            "('AAA', 'evaluation'), ('BBB', 'evaluation'), "
            "('NVO', 'portfolio'), ('NU', 'portfolio')"
        )
        conn.execute(
            "INSERT INTO llm_artifacts "
            "(ticker, scope, purpose, content_json, input_sha256, generated_at, "
            "superseded_by_id) VALUES "
            "('AAA', 'ticker', 'investment_decision_card', "
            "'{\"suggested_disposition\":\"buy\"}', 'sha-card-aaa', "
            "'2026-07-19T09:00:00', NULL)"
        )
        conn.execute(
            "INSERT INTO llm_artifacts "
            "(ticker, scope, purpose, content_json, input_sha256, generated_at, "
            "superseded_by_id) VALUES "
            "('BBB', 'ticker', 'investment_decision_card', "
            "'{\"suggested_disposition\":\"watch\"}', 'sha-card-bbb', "
            "'2026-07-18T09:00:00', NULL)"
        )
        iso_year: int
        iso_week: int
        iso_year, iso_week, _ = _NOW.isocalendar()
        cur: sqlite3.Cursor = conn.execute(
            "INSERT INTO weekly_packet_runs (iso_year, iso_week) VALUES (?, ?)",
            (iso_year, iso_week),
        )
        run_id: int = int(cur.lastrowid or 0)
        conn.execute(
            "INSERT INTO weekly_packet_items "
            "(run_id, item_kind, ticker, title, verdict, order_index) VALUES "
            "(?, 'news', 'NVO', 'stub packet item', NULL, 0)",
            (run_id,),
        )
        conn.execute(
            "INSERT INTO expected_earnings (ticker, expected_date) VALUES "
            "('NVO', '2026-07-22'), ('NU', '2026-07-23')"
        )
        conn.execute(
            "INSERT INTO decisions (ticker, recommendation_kind, decided_by, "
            "created_at) VALUES ('NU', 'trim', 'owner', '2026-07-18T09:00:00')"
        )
        conn.execute(
            "INSERT OR REPLACE INTO _decision_journal_base "
            "(decision_id, ticker, recommendation_kind, made_at, falsifier, "
            "decided_by, advice_preceded, process_quality) VALUES "
            "(9001, 'NVO', 'trim', '2026-07-19T09:00:00', "
            "'price drops 20%', 'owner', 1, NULL)"
        )
        conn.commit()
    finally:
        conn.close()


@pytest.mark.parametrize("populated", [True, False])
def test_direct_helpers_shared_connection_matches_owned(tmp_path: Path, populated: bool) -> None:
    db_path: Path = _make_db(tmp_path)
    if populated:
        _seed_direct_sql_tables(db_path)
    shared: sqlite3.Connection = sqlite3.connect(str(db_path))
    shared.row_factory = sqlite3.Row
    try:
        cards_owned: spb._CardsInput = spb._gather_cards(db_path, now=_NOW)
        cards_shared: spb._CardsInput = spb._gather_cards(db_path, now=_NOW, conn=shared)
        assert cards_owned == cards_shared
        packet_owned: spb._PacketInput = spb._gather_packet_items(db_path, now=_NOW)
        packet_shared: spb._PacketInput = spb._gather_packet_items(db_path, now=_NOW, conn=shared)
        assert packet_owned == packet_shared
        prior_owned: spb._PriorDecisionInput = spb._gather_prior_decision(db_path)
        prior_shared: spb._PriorDecisionInput = spb._gather_prior_decision(db_path, conn=shared)
        assert prior_owned == prior_shared
        earnings_owned: int = spb._earnings_cluster_count(db_path, now=_NOW)
        earnings_shared: int = spb._earnings_cluster_count(db_path, now=_NOW, conn=shared)
        assert earnings_owned == earnings_shared
        shift_owned: int = spb._portfolio_shift_count(db_path, now=_NOW)
        shift_shared: int = spb._portfolio_shift_count(db_path, now=_NOW, conn=shared)
        assert shift_owned == shift_shared
        fresh_count: int = sum(1 for line in cards_owned.lines if "(fresh," in line)
        active_owned: tuple[bool, list[str]] = spb._detect_active_week(
            db_path, now=_NOW, fresh_card_count=fresh_count
        )
        active_shared: tuple[bool, list[str]] = spb._detect_active_week(
            db_path, now=_NOW, fresh_card_count=fresh_count, conn=shared
        )
        assert active_owned == active_shared
        probe: sqlite3.Row | None = shared.execute("SELECT 1 AS one").fetchone()
        assert probe is not None and int(probe["one"]) == 1
        if populated:
            assert len(cards_owned.lines) == 2
            assert all("(fresh," in line for line in cards_owned.lines)
            assert len(packet_owned.lines) == 1
            assert prior_owned.line is not None
            assert prior_owned.ref == "decision:9001"
            assert earnings_owned == 2
            assert shift_owned == 1
            assert active_owned[0] is True
        else:
            assert cards_owned.lines == []
            assert cards_owned.refs == []
            assert packet_owned.lines == []
            assert prior_owned.line is None
            assert earnings_owned == 0
            assert shift_owned == 0
            assert active_owned == (False, [])
    finally:
        shared.close()


def test_gather_inputs_uses_single_shared_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path: Path = _make_db(tmp_path)
    _seed_direct_sql_tables(db_path)

    def _stub_recommendation(db_path_arg: Path) -> spb._RecommendationInput:
        return spb._RecommendationInput("[rec:stub] stub", "rec:stub")

    def _stub_risk(db_path_arg: Path) -> spb._RiskInput:
        return spb._RiskInput("[risk:stub] stub", "risk:stub")

    def _stub_wealth(db_path_arg: Path) -> spb._WealthInput:
        return spb._WealthInput("[wealth:stub] stub", "wealth:stub")

    def _stub_routed(db_path_arg: Path) -> list[spb._RoutedMomentLine]:
        return [
            spb._RoutedMomentLine(
                ping_id=1,
                class_="profile_drift",
                ticker=None,
                ref="coach_ping:1",
                line="[coach_ping:1] profile_drift",
            )
        ]

    def _stub_proposals(db_path_arg: Path) -> list[str]:
        return ["[research_proposal:7] stub proposal"]

    def _stub_anchors(repo_root_arg: Path) -> str:
        return "stub anchors"

    monkeypatch.setattr(spb, "_gather_recommendation", _stub_recommendation)
    monkeypatch.setattr(spb, "_gather_risk", _stub_risk)
    monkeypatch.setattr(spb, "_gather_wealth", _stub_wealth)
    monkeypatch.setattr(spb, "_gather_routed_moments", _stub_routed)
    monkeypatch.setattr(spb, "_gather_proposals", _stub_proposals)
    monkeypatch.setattr(spb, "_gather_anchors", _stub_anchors)

    real_ro_conn = spb._ro_conn
    opened: list[sqlite3.Connection] = []

    def _counting_ro(db_path_arg: Path) -> sqlite3.Connection | None:
        conn = real_ro_conn(db_path_arg)
        if conn is not None:
            opened.append(conn)
        return conn

    monkeypatch.setattr(spb, "_ro_conn", _counting_ro)
    inputs: spb._Inputs = spb._gather_inputs(db_path, tmp_path, now=_NOW)
    assert len(opened) == 1
    with pytest.raises(sqlite3.ProgrammingError):
        opened[0].execute("SELECT 1")
    assert inputs.recommendation.ref == "rec:stub"
    assert inputs.risk.ref == "risk:stub"
    assert inputs.wealth.ref == "wealth:stub"
    assert len(inputs.cards.lines) == 2
    assert len(inputs.packet.lines) == 1
    assert inputs.prior_decision.ref == "decision:9001"
    assert inputs.prior_decision.line is not None
    assert inputs.is_active_week is True
    assert inputs.active_week_reasons != []
    assert "rec:stub" in inputs.allowed_refs
    assert "coach_ping:1" in inputs.allowed_refs
    assert "research_proposal:7" in inputs.allowed_refs
    assert any(ref.startswith("decision:") for ref in inputs.allowed_refs)
