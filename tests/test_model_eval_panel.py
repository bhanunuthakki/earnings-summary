"""Optimizer panel (src/pipeline/model_eval_panel.py, model_eval_loop.md PR4):
the per-purpose 30d cost split (prod vs eval scopes), the (purpose, candidate)
verdict history with CANDIDATE_ERRORED rendered as an infra flag (not a quality
result), active model-pin overrides with the realized-savings estimate, and the
anonymous-purpose call-health alarm.

Pure DB reads — no LLM, no jobs. Mirrors tests/test_evals_panel.py.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pipeline.model_eval_panel import (
    CANDIDATE_ERRORED,
    load_active_overrides,
    load_anon_costs,
    load_candidate_histories,
    load_purpose_costs,
    render_model_eval_panel,
)

# Real ladder ids so estimated_call_usd/model_rank resolve (src/llm/model_ladder.py).
_OPUS = "claude-opus-4-8"  # in 15.0 / out 75.0 $/MTok
_SONNET = "claude-sonnet-4-6"  # in 3.0 / out 15.0 $/MTok
_GEMINI_PRO = "gemini-3.1-pro-preview"
_GEMINI_FLASH = "gemini-2.5-flash"

_DDL = """
CREATE TABLE llm_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    called_at TEXT NOT NULL, purpose TEXT, ticker TEXT, scope TEXT,
    model TEXT, prompt_sha256 TEXT, response_sha256 TEXT,
    prompt_chars INTEGER, response_chars INTEGER, input_tokens INTEGER,
    cache_creation_input_tokens INTEGER, cache_read_input_tokens INTEGER,
    output_tokens INTEGER, elapsed_ms INTEGER, cost_estimate_usd REAL,
    cache_hit INTEGER NOT NULL DEFAULT 0, fallback_used TEXT,
    artifact_id INTEGER, error TEXT, run_id TEXT
);
CREATE TABLE model_eval_verdicts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    purpose TEXT NOT NULL, candidate TEXT NOT NULL, incumbent TEXT NOT NULL,
    verdict TEXT NOT NULL, run_id TEXT NOT NULL, parity_rate REAL,
    judge_agreement REAL, n_cases INTEGER, n_parity INTEGER,
    summary_json TEXT, recorded_at TEXT NOT NULL
);
CREATE TABLE model_pin_overrides (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    purpose TEXT NOT NULL, model TEXT NOT NULL, set_by TEXT NOT NULL,
    set_at TEXT NOT NULL, reason_json TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1
);
"""


def _call(
    conn: sqlite3.Connection,
    *,
    purpose: str | None,
    scope: str | None,
    cost: float,
    when: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
) -> None:
    conn.execute(
        "INSERT INTO llm_calls (called_at, purpose, scope, cost_estimate_usd,"
        " input_tokens, output_tokens) VALUES (?, ?, ?, ?, ?, ?)",
        (when, purpose, scope, cost, input_tokens, output_tokens),
    )


def _verdict(
    conn: sqlite3.Connection,
    *,
    purpose: str,
    candidate: str,
    incumbent: str,
    verdict: str,
    recorded_at: str,
    parity_rate: float | None = None,
    judge_agreement: float | None = None,
    n_cases: int | None = None,
) -> None:
    conn.execute(
        "INSERT INTO model_eval_verdicts (purpose, candidate, incumbent, verdict,"
        " run_id, parity_rate, judge_agreement, n_cases, recorded_at)"
        " VALUES (?, ?, ?, ?, 'rid', ?, ?, ?, ?)",
        (purpose, candidate, incumbent, verdict, parity_rate, judge_agreement, n_cases, recorded_at),
    )


def _seed(db: Path) -> None:
    conn = sqlite3.connect(db)
    conn.executescript(_DDL)
    now = datetime.now(UTC).replace(tzinfo=None)
    iso = now.isoformat()
    ancient = (now - timedelta(days=90)).isoformat()

    # --- per-purpose cost: bear_case has prod (NULL scope) + eval-machinery calls.
    _call(conn, purpose="bear_case", scope=None, cost=0.50, when=iso)
    _call(conn, purpose="bear_case", scope=None, cost=0.30, when=iso)
    _call(conn, purpose="bear_case", scope="model_eval", cost=0.10, when=iso)
    _call(conn, purpose="bear_case", scope="backend_judge", cost=0.05, when=iso)
    _call(conn, purpose="bear_case", scope="eval", cost=0.02, when=iso)

    # --- company_description: the downgraded purpose. Prod token volume drives the
    #     realized-savings estimate; an eval-scope call must NOT enter that volume.
    _call(
        conn, purpose="company_description", scope=None, cost=1.0, when=iso,
        input_tokens=500_000, output_tokens=100_000,
    )
    _call(
        conn, purpose="company_description", scope=None, cost=1.0, when=iso,
        input_tokens=500_000, output_tokens=100_000,
    )
    _call(
        conn, purpose="company_description", scope="model_eval", cost=9.0, when=iso,
        input_tokens=9_000_000, output_tokens=9_000_000,  # would wreck savings if counted
    )

    # --- anonymous-purpose alarm evidence.
    _call(conn, purpose=None, scope=None, cost=5.00, when=iso)  # NULL → alarms
    _call(conn, purpose=None, scope=None, cost=0.50, when=iso)  # same NULL group
    _call(conn, purpose="ghost_purpose", scope=None, cost=2.00, when=iso)  # unregistered → alarms
    _call(conn, purpose="tiny_orphan", scope=None, cost=0.40, when=iso)  # unregistered, below floor
    _call(conn, purpose="lens:five_min_reread", scope=None, cost=3.00, when=iso)  # known family
    _call(conn, purpose=None, scope=None, cost=99.0, when=ancient)  # outside window

    # --- verdicts: a switch, a keep-history, and an errored candidate.
    _verdict(
        conn, purpose="company_description", candidate=_SONNET, incumbent=_OPUS,
        verdict="SWITCH_DOWN", recorded_at="2026-06-28T02:00:00",
        parity_rate=1.0, judge_agreement=1.0, n_cases=4,
    )
    _verdict(
        conn, purpose="bear_case", candidate=_GEMINI_PRO, incumbent=_SONNET,
        verdict="KEEP_INCUMBENT", recorded_at="2026-06-21T02:00:00",
        parity_rate=0.0, n_cases=4,
    )
    _verdict(
        conn, purpose="bear_case", candidate=_GEMINI_PRO, incumbent=_SONNET,
        verdict="KEEP_INCUMBENT", recorded_at="2026-06-28T02:00:00",
        parity_rate=0.25, n_cases=4,
    )
    # viewspec_compile / flash: older HOLD, latest CANDIDATE_ERRORED (infra).
    _verdict(
        conn, purpose="viewspec_compile", candidate=_GEMINI_FLASH, incumbent=_SONNET,
        verdict="HOLD", recorded_at="2026-06-21T02:00:00", n_cases=2,
    )
    _verdict(
        conn, purpose="viewspec_compile", candidate=_GEMINI_FLASH, incumbent=_SONNET,
        verdict=CANDIDATE_ERRORED, recorded_at="2026-06-28T02:00:00", n_cases=2,
    )

    # --- overrides: one active (company_description→Sonnet) + one inactive (ignored).
    conn.execute(
        "INSERT INTO model_pin_overrides (purpose, model, set_by, set_at, reason_json, active)"
        " VALUES ('company_description', ?, 'auto:model_eval_loop', '2026-06-28T03:00:00', '{}', 1)",
        (_SONNET,),
    )
    conn.execute(
        "INSERT INTO model_pin_overrides (purpose, model, set_by, set_at, reason_json, active)"
        " VALUES ('bear_case', ?, 'manual:owner', '2026-05-01T03:00:00', '{}', 0)",
        (_GEMINI_PRO,),
    )
    conn.commit()
    conn.close()


def _conn(db: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    return conn


def test_purpose_costs_split_prod_vs_eval(tmp_path: Path) -> None:
    db = tmp_path / "p.db"
    _seed(db)
    conn = _conn(db)
    try:
        costs = {c.purpose: c for c in load_purpose_costs(conn)}
    finally:
        conn.close()
    bear = costs["bear_case"]
    assert round(bear.prod_cost_usd, 2) == 0.80 and bear.prod_calls == 2
    # model_eval + backend_judge + eval scopes fold into the eval column.
    assert round(bear.eval_cost_usd, 2) == 0.17 and bear.eval_calls == 3
    assert round(bear.total_cost_usd, 2) == 0.97


def test_candidate_histories_group_and_flag_errored(tmp_path: Path) -> None:
    db = tmp_path / "p.db"
    _seed(db)
    conn = _conn(db)
    try:
        hist = {(h.purpose, h.candidate): h for h in load_candidate_histories(conn)}
    finally:
        conn.close()
    # The keep-history pair keeps most-recent-first and is NOT flagged errored.
    bear = hist[("bear_case", _GEMINI_PRO)]
    assert len(bear.rows) == 2
    assert bear.latest.recorded_at == "2026-06-28 02:00" and bear.latest.parity_rate == 0.25
    assert bear.incumbent == _SONNET and not bear.errored
    # The flash pair's LATEST is CANDIDATE_ERRORED → the infra flag.
    flash = hist[("viewspec_compile", _GEMINI_FLASH)]
    assert flash.latest.verdict == CANDIDATE_ERRORED and flash.errored
    # A switch verdict exists for the downgraded purpose.
    assert hist[("company_description", _SONNET)].latest.verdict == "SWITCH_DOWN"


def test_active_overrides_savings_and_incumbent(tmp_path: Path) -> None:
    db = tmp_path / "p.db"
    _seed(db)
    conn = _conn(db)
    try:
        overrides = load_active_overrides(conn)
    finally:
        conn.close()
    # Only the active override surfaces (the inactive bear_case row is ignored).
    assert len(overrides) == 1
    o = overrides[0]
    assert o.purpose == "company_description" and o.model == _SONNET
    assert o.incumbent == _OPUS  # read from the latest verdict for the pair
    # Prod token volume excludes the eval-scope call (else it would be ~18M).
    assert o.prod_input_tokens == 1_000_000 and o.prod_output_tokens == 200_000
    # Opus (15/75) vs Sonnet (3/15) on 1.0M in / 0.2M out = $30 - $6 = $24/mo.
    assert o.monthly_savings_usd is not None
    assert round(o.monthly_savings_usd, 2) == 24.00


def test_override_savings_none_when_model_unranked(tmp_path: Path) -> None:
    db = tmp_path / "p.db"
    conn = sqlite3.connect(db)
    conn.executescript(_DDL)
    now = datetime.now(UTC).replace(tzinfo=None).isoformat()
    # An override onto a model that isn't on the ladder → cost unknown, savings None.
    conn.execute(
        "INSERT INTO model_pin_overrides (purpose, model, set_by, set_at, reason_json, active)"
        " VALUES ('bear_case', 'some-unranked-model', 'auto:model_eval_loop', ?, '{}', 1)",
        (now,),
    )
    conn.execute(
        "INSERT INTO llm_calls (called_at, purpose, cost_estimate_usd, input_tokens, output_tokens)"
        " VALUES (?, 'bear_case', 1.0, 500000, 100000)",
        (now,),
    )
    conn.commit()
    row = _conn(db)
    try:
        overrides = load_active_overrides(row)
    finally:
        row.close()
    assert len(overrides) == 1
    # No verdict → incumbent unknown → savings None (never a bogus negative number).
    assert overrides[0].incumbent is None and overrides[0].monthly_savings_usd is None


def test_anon_alarm_flags_null_and_unregistered_over_floor(tmp_path: Path) -> None:
    db = tmp_path / "p.db"
    _seed(db)
    conn = _conn(db)
    try:
        anon = load_anon_costs(conn)
    finally:
        conn.close()
    by_purpose = {a.purpose: a for a in anon}
    # The NULL group (5.00 + 0.50) and the unregistered ghost_purpose (2.00) alarm.
    assert None in by_purpose and by_purpose[None].cost_usd == 5.50
    assert "ghost_purpose" in by_purpose
    # Costliest first.
    assert anon[0].purpose is None and anon[0].is_null
    # NOT flagged: registered purposes, the lens:* family, below-floor orphans,
    # and the ancient out-of-window NULL row.
    assert "bear_case" not in by_purpose
    assert "company_description" not in by_purpose
    assert "lens:five_min_reread" not in by_purpose
    assert "tiny_orphan" not in by_purpose
    # The window excludes the ancient 99.0 NULL row (else the NULL group ≠ 5.50).


def test_render_full_panel(tmp_path: Path) -> None:
    db = tmp_path / "p.db"
    _seed(db)
    html = render_model_eval_panel(db)
    # Anonymous alarm: a bad-toned well + the NULL/unregistered chips. Exactly
    # two lines alarm (the NULL group + ghost_purpose) — tiny_orphan is below the
    # floor and lens:* is a known family, so neither reaches the alarm banner.
    assert "k-well-bad" in html and "purpose=NULL" in html
    assert "me-alarm-chip" in html and "ghost_purpose" in html
    assert "2 anonymous/unregistered lines" in html
    # Overrides: the realized-savings rollup + the $24/mo per-row estimate.
    assert "24.00/mo" in html
    assert "saved by 1 downgrade" in html
    assert _OPUS in html and _SONNET in html  # incumbent → override
    # Verdict history: the switch pill + the CANDIDATE_ERRORED infra flag (an
    # outline red chip, NOT a filled quality pill).
    assert "switch ↓" in html
    assert "⚠ infra err" in html and "me-infra" in html
    # Per-purpose cost table with the eval split present.
    assert "company_description" in html and "bear_case" in html


def test_render_empty_db_degrades(tmp_path: Path) -> None:
    db = tmp_path / "empty.db"
    sqlite3.connect(db).close()
    html = render_model_eval_panel(db)
    assert "No active overrides" in html
    assert "No sweep verdicts recorded yet" in html
    assert "No LLM calls in the window" in html
    # The alarm degrades to a clean, not-crashed state.
    assert "k-well-ok" in html and "No anonymous or unregistered LLM spend" in html
    missing = render_model_eval_panel(tmp_path / "nope.db")
    assert "No active overrides" in missing


def test_render_escapes_untrusted_model_and_purpose(tmp_path: Path) -> None:
    """Purpose / model / candidate strings are DB text — escaped, never live markup."""
    db = tmp_path / "x.db"
    conn = sqlite3.connect(db)
    conn.executescript(_DDL)
    now = datetime.now(UTC).replace(tzinfo=None).isoformat()
    conn.execute(
        "INSERT INTO model_pin_overrides (purpose, model, set_by, set_at, reason_json, active)"
        " VALUES ('<b>evil</b>', '<i>m</i>', 'auto:model_eval_loop', ?, '{}', 1)",
        (now,),
    )
    conn.execute(
        "INSERT INTO model_eval_verdicts (purpose, candidate, incumbent, verdict, run_id,"
        " recorded_at) VALUES ('<b>evil</b>', '<i>m</i>', '<u>inc</u>', 'HOLD', 'r', ?)",
        (now,),
    )
    conn.commit()
    conn.close()
    html = render_model_eval_panel(db)
    assert "<b>evil</b>" not in html and "&lt;b&gt;evil&lt;/b&gt;" in html
    assert "<i>m</i>" not in html and "&lt;i&gt;m&lt;/i&gt;" in html
    assert "&lt;u&gt;inc&lt;/u&gt;" in html
