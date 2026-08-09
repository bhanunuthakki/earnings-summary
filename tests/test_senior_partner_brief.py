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
