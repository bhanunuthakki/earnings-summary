"""End-to-end tests for the KPI-inflection trigger.

Mirrors ``test_trigger_earnings_tone`` for the LLM-mock plumbing
(``monkeypatch.setattr("triggers.kpi_inflection.call_llm", ...)``) but exercises
the *inverse* contract: the inflection signal is deterministic, so the alert
MUST fire even when the LLM is down — the LLM only adds an optional context
line. Verifies:

  * Protocol conformance + kind/cadence
  * signature_key_evidence keys on (kpi_name, period_end)
  * scan: no registered KPIs → []; recent inflection → 1 candidate;
    stale inflection → []
  * should_fire: registered-threshold crossing vs the un-thresholded
    significant-z fallback
  * build_alert: deterministic memo with the LLM DOWN (alert still fires,
    llm_context_available=False); LLM UP appends a context line and caches it
    (second build is a cache hit, no re-invoke)
  * draft_actions: threshold/breaker action mix vs the bare earnings-prep case
  * full pipeline integration smoke (NU NIM 'below 17')

Value fixtures (``_VALUES_*``) are pinned from a one-off probe of
``timeseries.detect_inflection`` so the detected changepoint index + latest-
point z-score are deterministic regardless of the ruptures backend.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

import pytest

from alerts.store import compute_signature_sha
from triggers import KpiInflectionTrigger, UserStateContext
from triggers.base import AlertDraft, Cadence, ThesisAnchor, Trigger, TriggerCandidate
from user_state.registry import list_kpis, upsert_kpi

# ---------------------------------------------------------------------------
# Pinned value series (see module docstring). detect_inflection keys off the
# value array by index, so the changepoint index is stable across dates.
# ---------------------------------------------------------------------------

# 8 quarters flat ~18, then a 4-quarter decline to 16.4. Changepoint idx=8
# (recent), latest-point z ≈ -2.3.
_VALUES_RECENT_DOWN: list[float] = [
    18.0, 18.1, 17.9, 18.0, 18.1, 17.9, 18.0, 18.0, 17.4, 17.0, 16.7, 16.4,
]
# 8 flat at 18, sharp step to a flat 16.4. Changepoint idx=8 (recent), but the
# latest-point z ≈ -1.2 (< significant) — used for the "mild, no threshold,
# don't fire" path.
_VALUES_RECENT_DOWN_FLAT: list[float] = [18.0] * 8 + [16.4] * 4
# 8 flat at 50, then an escalating ramp to 120. Changepoint idx=8 (recent),
# latest-point z ≈ 3.3 (>= significant) — the un-thresholded fire path.
_VALUES_RECENT_UP_EXTREME: list[float] = [50.0] * 8 + [62.0, 78.0, 98.0, 120.0]
# Regime change at the very start (idx=3) then flat — a stale inflection that
# must NOT produce a candidate.
_VALUES_OLD_INFLECTION: list[float] = [10.0, 10.0, 10.0] + [30.0] * 9

_QUARTER_MONTH_DAY: dict[int, tuple[str, str]] = {
    1: ("03-31", "Q1"),
    2: ("06-30", "Q2"),
    3: ("09-30", "Q3"),
    4: ("12-31", "Q4"),
}


def _quarter_series(values: list[float]) -> list[tuple[str, str, float]]:
    """(period_end_iso, fiscal_period_type, value) ascending, ending 2026-Q1."""
    seq: list[tuple[int, int]] = []
    year, quarter = 2026, 1
    for _ in range(len(values)):
        seq.append((year, quarter))
        quarter -= 1
        if quarter == 0:
            quarter = 4
            year -= 1
    seq.reverse()
    out: list[tuple[str, str, float]] = []
    for (y, q), v in zip(seq, values, strict=True):
        month_day, fpt = _QUARTER_MONTH_DAY[q]
        out.append((f"{y}-{month_day}", fpt, v))
    return out


# ---------------------------------------------------------------------------
# Schema fixture
# ---------------------------------------------------------------------------


def _create_schema(conn: sqlite3.Connection) -> None:
    """Minimal subset of migrations 0004 (kpi_facts) + 0007 (kpi_definitions)
    + 0060 (user_kpi_registry) + the llm_artifacts cache table. No documents
    table, so load_kpi_series takes its max(id) dedup fallback path."""
    _ = conn.execute(
        "CREATE TABLE kpi_definitions ("
        + "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        + "ticker TEXT NOT NULL, name TEXT NOT NULL, unit TEXT"
        + ")"
    )
    _ = conn.execute(
        "CREATE TABLE kpi_facts ("
        + "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        + "ticker TEXT NOT NULL, period_end TEXT NOT NULL, "
        + "fiscal_period_type TEXT NOT NULL, kpi_definition_id INTEGER NOT NULL, "
        + "value REAL NOT NULL, unit TEXT, source_doc_id INTEGER"
        + ")"
    )
    _ = conn.execute(
        "CREATE TABLE user_kpi_registry ("
        + "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        + "user_id TEXT NOT NULL DEFAULT 'bhanu', ticker TEXT NOT NULL, "
        + "kpi_name TEXT NOT NULL, threshold_direction TEXT, threshold_value REAL, "
        + "is_thesis_breaker INTEGER NOT NULL DEFAULT 0, scaffold_source TEXT, "
        + "notes TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL"
        + ")"
    )
    _ = conn.execute(
        "CREATE TABLE llm_artifacts ("
        + "id INTEGER PRIMARY KEY AUTOINCREMENT, ticker TEXT, "
        + "scope TEXT NOT NULL DEFAULT 'ticker', purpose TEXT NOT NULL, "
        + "fiscal_period TEXT, content_md TEXT, content_json TEXT, "
        + "input_sha256 TEXT NOT NULL, output_sha256 TEXT, model TEXT, "
        + "prompt_version TEXT NOT NULL DEFAULT 'v1', generated_at TEXT NOT NULL, "
        + "expires_at TEXT, superseded_by_id INTEGER, dirty INTEGER NOT NULL DEFAULT 0, "
        + "dirty_reason TEXT, source_doc_ids TEXT, parent_artifact_ids TEXT, "
        + "llm_call_id INTEGER"
        + ")"
    )


@pytest.fixture
def db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Create the test DB and route every ``DB_PATH`` consumer to it."""
    path = tmp_path / "test.db"
    conn = sqlite3.connect(str(path))
    try:
        _create_schema(conn)
        _ = conn.execute("PRAGMA journal_mode=WAL")
        conn.commit()
    finally:
        conn.close()
    monkeypatch.setattr("db.DB_PATH", str(path))
    return path


@pytest.fixture
def fixture_conn(db_path: Path) -> Iterator[sqlite3.Connection]:
    """Yield a connection on the fixture DB (used as scan()'s ``db`` arg)."""
    conn = sqlite3.connect(str(db_path))
    try:
        yield conn
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


def _seed_kpi_facts(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    kpi_name: str,
    values: list[float],
    unit: str = "%",
) -> None:
    """Insert a kpi_definitions row + a quarterly kpi_facts series."""
    cur = conn.execute(
        "INSERT INTO kpi_definitions (ticker, name, unit) VALUES (?, ?, ?)",
        (ticker, kpi_name, unit),
    )
    kd_id = cur.lastrowid
    for period_end, fpt, value in _quarter_series(values):
        _ = conn.execute(
            "INSERT INTO kpi_facts "
            + "(ticker, period_end, fiscal_period_type, kpi_definition_id, value, unit, source_doc_id) "
            + "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (ticker, period_end, fpt, kd_id, value, unit, 1),
        )
    conn.commit()


def _register_kpi(
    db_path: Path,
    *,
    ticker: str,
    kpi_name: str,
    threshold_direction: str | None = None,
    threshold_value: float | None = None,
    is_thesis_breaker: bool = False,
) -> None:
    """Register a load-bearing KPI via the real CRUD path."""
    _ = upsert_kpi(
        user_id="bhanu",
        ticker=ticker,
        kpi_name=kpi_name,
        threshold_direction=threshold_direction,
        threshold_value=threshold_value,
        is_thesis_breaker=is_thesis_breaker,
        scaffold_source="manual",
        db_path=db_path,
    )


def _user_state_from_db(db_path: Path, ticker: str) -> UserStateContext:
    """Compose UserStateContext exactly as the morning driver does."""
    rows = list_kpis(user_id="bhanu", ticker=ticker, db_path=db_path)
    return UserStateContext(
        registered_kpis=[asdict(r) for r in rows],
        sizing_intents=[],
        recent_dismissed_signatures=set(),
    )


# ---------------------------------------------------------------------------
# In-memory candidate / alert builders (for the unit-level policy tests)
# ---------------------------------------------------------------------------


def _make_candidate(
    *,
    ticker: str = "NU",
    kpi_name: str = "NIM",
    period_end: str = "2026-03-31",
    prev_value: float = 18.0,
    curr_value: float = 16.4,
    zscore: float | None = -2.3,
    unit: str | None = "%",
    inflection_direction: str = "down",
    threshold_crossed: bool = False,
    registered_threshold_direction: str | None = None,
    registered_threshold_value: float | None = None,
    is_thesis_breaker: bool = False,
) -> TriggerCandidate:
    evidence: dict[str, object] = {
        "kpi_name": kpi_name,
        "period_end": period_end,
        "prev_value": prev_value,
        "curr_value": curr_value,
        "zscore": zscore,
        "inflection_direction": inflection_direction,
        "unit": unit,
        "inflection_period": "2025-06-30",
        "inflection_magnitude": 2.0,
        "registered_threshold_direction": registered_threshold_direction,
        "registered_threshold_value": registered_threshold_value,
        "is_thesis_breaker": is_thesis_breaker,
        "threshold_crossed": threshold_crossed,
        "series_snapshot": [{"period_end": period_end, "value": curr_value}],
    }
    return TriggerCandidate(
        ticker=ticker,
        kind="kpi_inflection",
        key=f"{ticker}:{kpi_name}:{period_end}",
        evidence=evidence,
        computed_at=datetime.now(UTC).replace(tzinfo=None),
    )


def _make_alert(*, ticker: str = "NU", memo_text: str = "memo") -> AlertDraft:
    return AlertDraft(
        trigger_kind="kpi_inflection",
        ticker=ticker,
        fired_at=datetime.now(UTC).replace(tzinfo=None),
        evidence_json="{}",
        signature_sha="0" * 64,
        memo_text=memo_text,
    )


def _reg_dict(
    kpi_name: str,
    threshold_direction: str | None,
    threshold_value: float | None,
    *,
    is_thesis_breaker: bool = False,
) -> dict[str, object]:
    return {
        "kpi_name": kpi_name,
        "threshold_direction": threshold_direction,
        "threshold_value": threshold_value,
        "is_thesis_breaker": is_thesis_breaker,
    }


class _StatefulLLM:
    """Tracks call count + returns canned plain-text context in sequence."""

    def __init__(self, responses: list[str]) -> None:
        self.responses = responses
        self.call_count = 0
        self.prompts: list[str] = []

    def __call__(self, prompt: str, **_kwargs: object) -> str:
        self.prompts.append(prompt)
        idx = min(self.call_count, len(self.responses) - 1)
        self.call_count += 1
        return self.responses[idx]


def _raise_llm(*_args: object, **_kwargs: object) -> str:
    raise RuntimeError("LLM unavailable for test")


# ---------------------------------------------------------------------------
# Protocol / signature
# ---------------------------------------------------------------------------


def test_protocol_conformance_and_metadata() -> None:
    trigger = KpiInflectionTrigger()
    assert isinstance(trigger, Trigger)
    assert trigger.kind == "kpi_inflection"
    assert trigger.cadence == Cadence.DAILY


def test_signature_key_evidence_keys_on_kpi_and_period() -> None:
    candidate = _make_candidate(kpi_name="NIM", period_end="2026-03-31")
    key_evidence = KpiInflectionTrigger().signature_key_evidence(candidate)
    assert dict(key_evidence) == {"kpi_name": "NIM", "period_end": "2026-03-31"}


# ---------------------------------------------------------------------------
# scan
# ---------------------------------------------------------------------------


def test_scan_no_registered_kpis_returns_empty(
    fixture_conn: sqlite3.Connection,
) -> None:
    # Facts exist, but nothing is registered → no candidates.
    _seed_kpi_facts(fixture_conn, ticker="NU", kpi_name="NIM", values=_VALUES_RECENT_DOWN)
    assert KpiInflectionTrigger().scan("NU", fixture_conn) == []


def test_scan_recent_inflection_emits_one_candidate(
    fixture_conn: sqlite3.Connection, db_path: Path
) -> None:
    _seed_kpi_facts(fixture_conn, ticker="NU", kpi_name="NIM", values=_VALUES_RECENT_DOWN)
    _register_kpi(
        db_path, ticker="NU", kpi_name="NIM", threshold_direction="below", threshold_value=17.0
    )
    candidates = KpiInflectionTrigger().scan("NU", fixture_conn)
    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.kind == "kpi_inflection"
    assert candidate.key == "NU:NIM:2026-03-31"
    ev = candidate.evidence
    assert ev["kpi_name"] == "NIM"
    assert ev["period_end"] == "2026-03-31"
    assert ev["curr_value"] == pytest.approx(16.4)
    assert ev["prev_value"] == pytest.approx(18.0)
    assert ev["inflection_direction"] == "down"
    assert ev["unit"] == "%"
    assert ev["threshold_crossed"] is True
    assert ev["registered_threshold_direction"] == "below"
    assert ev["registered_threshold_value"] == pytest.approx(17.0)
    assert isinstance(ev["series_snapshot"], list) and ev["series_snapshot"]


def test_scan_stale_inflection_returns_empty(
    fixture_conn: sqlite3.Connection, db_path: Path
) -> None:
    # Regime changed at idx=3 then went flat — not a *recent* inflection.
    _seed_kpi_facts(fixture_conn, ticker="NU", kpi_name="ARPAC", values=_VALUES_OLD_INFLECTION)
    _register_kpi(db_path, ticker="NU", kpi_name="ARPAC")
    assert KpiInflectionTrigger().scan("NU", fixture_conn) == []


def test_scan_missing_tables_returns_empty(
    fixture_conn: sqlite3.Connection, db_path: Path
) -> None:
    # Registered, but no kpi_facts/kpi_definitions rows for this KPI → []
    _register_kpi(db_path, ticker="NU", kpi_name="DEPOSIT_COST")
    assert KpiInflectionTrigger().scan("NU", fixture_conn) == []


# ---------------------------------------------------------------------------
# should_fire
# ---------------------------------------------------------------------------


def test_should_fire_true_when_below_threshold_crossed() -> None:
    candidate = _make_candidate(curr_value=16.4)
    state = UserStateContext(
        registered_kpis=[_reg_dict("NIM", "below", 17.0)],
        sizing_intents=[],
        recent_dismissed_signatures=set(),
    )
    assert KpiInflectionTrigger().should_fire(candidate, state) is True


def test_should_fire_false_threshold_not_crossed_and_z_below_significant() -> None:
    # 17.5 does not breach 'below 17'; z is mild → no fire (does not fall
    # through to the z-score path because a threshold is registered).
    candidate = _make_candidate(curr_value=17.5, zscore=-1.2)
    state = UserStateContext(
        registered_kpis=[_reg_dict("NIM", "below", 17.0)],
        sizing_intents=[],
        recent_dismissed_signatures=set(),
    )
    assert KpiInflectionTrigger().should_fire(candidate, state) is False


def test_should_fire_true_no_threshold_but_significant_z() -> None:
    candidate = _make_candidate(kpi_name="GMV", curr_value=120.0, zscore=3.28, unit=None)
    state = UserStateContext(
        registered_kpis=[_reg_dict("GMV", None, None)],
        sizing_intents=[],
        recent_dismissed_signatures=set(),
    )
    assert KpiInflectionTrigger().should_fire(candidate, state) is True


def test_should_fire_false_no_threshold_and_mild_z() -> None:
    candidate = _make_candidate(kpi_name="GMV", curr_value=16.4, zscore=-1.21, unit=None)
    state = UserStateContext(
        registered_kpis=[_reg_dict("GMV", None, None)],
        sizing_intents=[],
        recent_dismissed_signatures=set(),
    )
    assert KpiInflectionTrigger().should_fire(candidate, state) is False


# ---------------------------------------------------------------------------
# build_alert — deterministic memo first; LLM context is best-effort
# ---------------------------------------------------------------------------


def test_build_alert_fires_when_llm_down(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """LLM raises → the alert STILL fires with the deterministic factual memo;
    llm_context_available is False. This is the core contract."""
    monkeypatch.setattr("triggers.kpi_inflection.call_llm", _raise_llm)
    candidate = _make_candidate(
        threshold_crossed=True,
        registered_threshold_direction="below",
        registered_threshold_value=17.0,
    )
    alert = KpiInflectionTrigger().build_alert(candidate, None)

    assert alert.trigger_kind == "kpi_inflection"
    assert alert.ticker == "NU"
    assert alert.memo_text is not None
    # The factual core carries the user's own numbers + KPI name.
    assert "NIM" in alert.memo_text
    assert "16.4" in alert.memo_text
    assert "18" in alert.memo_text

    import json

    ev = json.loads(alert.evidence_json)
    assert ev["llm_context_available"] is False
    assert ev["threshold_crossed"] is True
    assert ev["series_snapshot"]
    expected_sig = compute_signature_sha(
        "kpi_inflection", "NU", {"kpi_name": "NIM", "period_end": "2026-03-31"}
    )
    assert alert.signature_sha == expected_sig


def test_build_alert_appends_llm_context_and_caches(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """LLM succeeds → context line appended; a second build is a cache hit and
    does NOT re-invoke the LLM."""
    context_line = "NIM compression squeezes the spread that funds NU's loan growth."
    mock = _StatefulLLM([context_line])
    monkeypatch.setattr("triggers.kpi_inflection.call_llm", mock)
    candidate = _make_candidate()

    first = KpiInflectionTrigger().build_alert(candidate, None)
    assert mock.call_count == 1
    assert first.memo_text is not None
    assert context_line in first.memo_text

    import json

    ev1 = json.loads(first.evidence_json)
    assert ev1["llm_context_available"] is True

    # Second build — same candidate, same inputs → artifact-store cache hit.
    second = KpiInflectionTrigger().build_alert(candidate, None)
    assert mock.call_count == 1, "cache miss — call_llm was re-invoked"
    assert second.memo_text == first.memo_text


def test_build_alert_empty_llm_response_degrades_to_factual(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A blank LLM response is treated as no context (not appended)."""
    monkeypatch.setattr("triggers.kpi_inflection.call_llm", _StatefulLLM(["   "]))
    candidate = _make_candidate()
    alert = KpiInflectionTrigger().build_alert(candidate, None)

    import json

    ev = json.loads(alert.evidence_json)
    assert ev["llm_context_available"] is False
    assert alert.memo_text is not None and alert.memo_text.endswith(".")


# ---------------------------------------------------------------------------
# build_alert — thesis-breaker cross escalation (slice 4 / PR-N14)
# ---------------------------------------------------------------------------


def _breaker_cross_candidate() -> TriggerCandidate:
    """A NIM 'below 17' breaker that crossed: is_thesis_breaker AND
    threshold_crossed both True → the escalation predicate fires."""
    return _make_candidate(
        threshold_crossed=True,
        registered_threshold_direction="below",
        registered_threshold_value=17.0,
        is_thesis_breaker=True,
    )


def test_build_alert_breaker_cross_escalates(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A thesis-breaker cross swaps the factual memo for the escalated
    'THESIS-BREAKER CROSSED ... Reconsider your thesis' memo and sets
    is_thesis_breaker_cross in the serialized evidence. LLM up → the optional
    context line still appends after the escalated core."""
    context_line = "Funding-cost pressure threatens the deposit-franchise thesis."
    monkeypatch.setattr("triggers.kpi_inflection.call_llm", _StatefulLLM([context_line]))
    alert = KpiInflectionTrigger().build_alert(_breaker_cross_candidate(), None)

    assert alert.memo_text is not None
    assert "THESIS-BREAKER CROSSED" in alert.memo_text
    assert "Reconsider your thesis" in alert.memo_text
    assert "NIM" in alert.memo_text and "16.4" in alert.memo_text
    assert context_line in alert.memo_text

    import json

    ev = json.loads(alert.evidence_json)
    assert ev["is_thesis_breaker_cross"] is True
    assert ev["llm_context_available"] is True


def test_build_alert_non_breaker_cross_stays_factual(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """REGRESSION GUARD: a non-breaker threshold cross keeps the existing
    factual memo verbatim — no escalation tag — and is_thesis_breaker_cross is
    False. Proves the non-breaker framing is unchanged by slice 4."""
    monkeypatch.setattr("triggers.kpi_inflection.call_llm", _raise_llm)
    candidate = _make_candidate(
        threshold_crossed=True,
        registered_threshold_direction="below",
        registered_threshold_value=17.0,
        is_thesis_breaker=False,
    )
    alert = KpiInflectionTrigger().build_alert(candidate, None)

    assert alert.memo_text is not None
    assert "THESIS-BREAKER CROSSED" not in alert.memo_text
    assert "Reconsider your thesis" not in alert.memo_text
    # Existing factual form: names the KPI, the move, and the crossed threshold.
    assert alert.memo_text.startswith("NU NIM inflected down")
    assert "Crossed your registered 'below 17' threshold." in alert.memo_text

    import json

    ev = json.loads(alert.evidence_json)
    assert ev["is_thesis_breaker_cross"] is False
    assert ev["llm_context_available"] is False


def test_build_alert_breaker_cross_fires_when_llm_down(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CONTRACT for breakers: the escalated memo is deterministic, so a
    thesis-breaker cross still fires the full escalation when the LLM raises;
    llm_context_available is False and the dedup signature is unchanged
    (escalation is presentation only)."""
    monkeypatch.setattr("triggers.kpi_inflection.call_llm", _raise_llm)
    alert = KpiInflectionTrigger().build_alert(_breaker_cross_candidate(), None)

    assert alert.memo_text is not None
    assert "THESIS-BREAKER CROSSED" in alert.memo_text
    assert "Reconsider your thesis" in alert.memo_text

    import json

    ev = json.loads(alert.evidence_json)
    assert ev["llm_context_available"] is False
    assert ev["is_thesis_breaker_cross"] is True
    expected_sig = compute_signature_sha(
        "kpi_inflection", "NU", {"kpi_name": "NIM", "period_end": "2026-03-31"}
    )
    assert alert.signature_sha == expected_sig


def test_build_alert_breaker_without_cross_not_escalated(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Escalation requires an ACTUAL cross, not merely a breaker-flagged KPI: a
    breaker KPI that inflected without breaching its threshold reads as a normal
    inflection (no escalation tag, flag False) and drafts no thesis/sizing
    action."""
    monkeypatch.setattr("triggers.kpi_inflection.call_llm", _raise_llm)
    candidate = _make_candidate(
        curr_value=17.5,  # 17.5 does not breach 'below 17'
        threshold_crossed=False,
        registered_threshold_direction="below",
        registered_threshold_value=17.0,
        is_thesis_breaker=True,
    )
    trigger = KpiInflectionTrigger()
    alert = trigger.build_alert(candidate, None)

    assert alert.memo_text is not None
    assert "THESIS-BREAKER CROSSED" not in alert.memo_text

    import json

    ev = json.loads(alert.evidence_json)
    assert ev["is_thesis_breaker_cross"] is False

    # Treated as a normal inflection — only the earnings-prep action.
    actions = trigger.draft_actions(alert, candidate)
    assert [a.action_kind for a in actions] == ["earnings_prep_append"]


# ---------------------------------------------------------------------------
# draft_actions
# ---------------------------------------------------------------------------


def test_draft_actions_threshold_breaker_full_mix() -> None:
    candidate = _make_candidate(
        threshold_crossed=True,
        registered_threshold_direction="below",
        registered_threshold_value=17.0,
        is_thesis_breaker=True,
    )
    alert = _make_alert(memo_text="NU NIM inflected down ... 'below 17' threshold.")
    actions = KpiInflectionTrigger().draft_actions(alert, candidate)

    kinds = sorted(a.action_kind for a in actions)
    assert kinds == ["earnings_prep_append", "sizing_update", "thesis_update"]
    by_kind = {a.action_kind: a.payload for a in actions}
    assert by_kind["earnings_prep_append"]["kpi_name"] == "NIM"
    assert by_kind["thesis_update"]["kpi_name"] == "NIM"
    assert by_kind["sizing_update"]["intent_kind"] == "sizing_note"
    assert by_kind["sizing_update"]["kpi_name"] == "NIM"


def test_draft_actions_threshold_crossed_not_breaker_has_no_sizing() -> None:
    candidate = _make_candidate(
        threshold_crossed=True,
        registered_threshold_direction="below",
        registered_threshold_value=17.0,
        is_thesis_breaker=False,
    )
    alert = _make_alert()
    actions = KpiInflectionTrigger().draft_actions(alert, candidate)
    kinds = sorted(a.action_kind for a in actions)
    assert kinds == ["earnings_prep_append", "thesis_update"]


def test_draft_actions_mild_inflection_only_earnings_prep() -> None:
    candidate = _make_candidate(
        kpi_name="GMV", inflection_direction="up", threshold_crossed=False, is_thesis_breaker=False
    )
    alert = _make_alert()
    actions = KpiInflectionTrigger().draft_actions(alert, candidate)
    assert [a.action_kind for a in actions] == ["earnings_prep_append"]
    assert actions[0].payload["kpi_name"] == "GMV"


def test_draft_actions_breaker_cross_reconsider_framed() -> None:
    """A thesis-breaker cross → reconsider-framed thesis_update carrying
    thesis_breaker=True, plus the sizing_update and earnings_prep."""
    candidate = _make_candidate(
        threshold_crossed=True,
        registered_threshold_direction="below",
        registered_threshold_value=17.0,
        is_thesis_breaker=True,
    )
    alert = _make_alert(
        memo_text="THESIS-BREAKER CROSSED - NU NIM inflected down ... Reconsider your thesis on NU."
    )
    actions = KpiInflectionTrigger().draft_actions(alert, candidate)

    kinds = sorted(a.action_kind for a in actions)
    assert kinds == ["earnings_prep_append", "sizing_update", "thesis_update"]
    by_kind = {a.action_kind: a.payload for a in actions}
    assert "RECONSIDER THESIS" in by_kind["thesis_update"]["body"]
    assert by_kind["thesis_update"]["thesis_breaker"] is True
    assert by_kind["thesis_update"]["kpi_name"] == "NIM"
    assert by_kind["sizing_update"]["intent_kind"] == "sizing_note"


def test_draft_actions_non_breaker_cross_not_reconsider() -> None:
    """REGRESSION GUARD: a non-breaker threshold cross keeps the existing
    factual thesis_update body (no 'RECONSIDER'), flags thesis_breaker=False,
    and adds no sizing_update."""
    candidate = _make_candidate(
        threshold_crossed=True,
        registered_threshold_direction="below",
        registered_threshold_value=17.0,
        is_thesis_breaker=False,
    )
    alert = _make_alert(memo_text="NU NIM inflected down from 18% to 16.4%.")
    actions = KpiInflectionTrigger().draft_actions(alert, candidate)

    kinds = sorted(a.action_kind for a in actions)
    assert kinds == ["earnings_prep_append", "thesis_update"]
    by_kind = {a.action_kind: a.payload for a in actions}
    assert "RECONSIDER" not in by_kind["thesis_update"]["body"]
    assert "crossed your" in by_kind["thesis_update"]["body"]
    assert by_kind["thesis_update"]["thesis_breaker"] is False


# ---------------------------------------------------------------------------
# Full pipeline integration smoke
# ---------------------------------------------------------------------------


def test_full_pipeline_integration_smoke(
    fixture_conn: sqlite3.Connection, db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """NU NIM registered 'below 17' + thesis-breaker; kpi_facts show NIM
    inflecting down to 16.4 → scan → should_fire → build_alert → draft_actions
    returns the expected shape."""
    _seed_kpi_facts(fixture_conn, ticker="NU", kpi_name="NIM", values=_VALUES_RECENT_DOWN, unit="%")
    _register_kpi(
        db_path,
        ticker="NU",
        kpi_name="NIM",
        threshold_direction="below",
        threshold_value=17.0,
        is_thesis_breaker=True,
    )
    monkeypatch.setattr(
        "triggers.kpi_inflection.call_llm",
        _StatefulLLM(["Funding-cost pressure threatens the deposit-franchise thesis."]),
    )

    trigger = KpiInflectionTrigger()
    candidates = trigger.scan("NU", fixture_conn)
    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.evidence["threshold_crossed"] is True

    state = _user_state_from_db(db_path, "NU")
    assert trigger.should_fire(candidate, state) is True

    alert = trigger.build_alert(candidate, None)
    assert alert.ticker == "NU"
    assert alert.memo_text is not None and "16.4" in alert.memo_text

    actions = trigger.draft_actions(alert, candidate)
    kinds = sorted(a.action_kind for a in actions)
    assert kinds == ["earnings_prep_append", "sizing_update", "thesis_update"]


def test_build_alert_accepts_structured_anchor_argument(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Protocol's ThesisAnchor arg is accepted (and ignored — the markdown
    anchor is loaded from disk); passing one must not change the outcome."""
    monkeypatch.setattr("triggers.kpi_inflection.call_llm", _raise_llm)
    candidate = _make_candidate()
    anchor = ThesisAnchor(
        ticker="NU",
        thesis_statement="Deposit franchise compounding.",
        key_driver="NIM",
        tier_1_kpis=[],
        business_model_rules=[],
    )
    alert = KpiInflectionTrigger().build_alert(candidate, anchor)
    assert alert.trigger_kind == "kpi_inflection"
    assert alert.memo_text is not None
