"""Post-earnings readout generation and quarter-indexed persistence."""

from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path
from typing import cast

import pytest


def _readout_text(*_args: object, **_kwargs: object) -> str:
    return "readout"


def _no_budget_skip(*_args: object, **_kwargs: object) -> None:
    return None


def _net_retention_text(*_args: object, **_kwargs: object) -> str:
    return "- Net retention: 112%"


def _valuation_live_text(*_args: object, **_kwargs: object) -> str:
    return "live $10"


def _watch_item_text(*_args: object, **_kwargs: object) -> str:
    return "- [watch] Verify"


def _tone_softened_text(*_args: object, **_kwargs: object) -> str:
    return "Tone softened"


def _thesis_anchor_text(*_args: object, **_kwargs: object) -> str:
    return "Thesis anchor"


def _empty_text(*_args: object, **_kwargs: object) -> str:
    return ""


_DDL = """
CREATE TABLE tracked_companies (
    ticker TEXT PRIMARY KEY,
    list_type TEXT NOT NULL,
    archived_at TEXT
);
CREATE TABLE transcripts (
    id INTEGER PRIMARY KEY,
    document_id INTEGER NOT NULL,
    ticker TEXT NOT NULL,
    call_date TEXT,
    fiscal_period_type TEXT,
    period_end TEXT,
    source_url TEXT
);
CREATE TABLE transcript_segments (
    id INTEGER PRIMARY KEY,
    transcript_id INTEGER NOT NULL,
    seq INTEGER NOT NULL,
    speaker TEXT,
    speaker_role TEXT,
    time_code_start TEXT,
    time_code_end TEXT,
    text TEXT NOT NULL
);
CREATE TABLE earnings_surprises (
    ticker TEXT NOT NULL,
    release_date TEXT NOT NULL,
    eps_estimate NUMERIC,
    eps_actual NUMERIC,
    revenue_estimate NUMERIC,
    revenue_actual NUMERIC,
    eps_surprise_pct NUMERIC,
    revenue_surprise_pct NUMERIC
);
CREATE TABLE llm_artifacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT,
    scope TEXT NOT NULL DEFAULT 'ticker',
    purpose TEXT NOT NULL,
    fiscal_period TEXT,
    content_md TEXT,
    content_json TEXT,
    input_sha256 TEXT NOT NULL,
    output_sha256 TEXT,
    model TEXT,
    prompt_version TEXT NOT NULL DEFAULT 'v1',
    generated_at TIMESTAMP NOT NULL,
    expires_at TIMESTAMP,
    superseded_by_id INTEGER,
    dirty INTEGER NOT NULL DEFAULT 0,
    dirty_reason TEXT,
    source_doc_ids TEXT,
    parent_artifact_ids TEXT,
    llm_call_id INTEGER
);
CREATE UNIQUE INDEX ux_llm_artifacts_current
ON llm_artifacts(ticker, purpose, fiscal_period)
WHERE superseded_by_id IS NULL;
"""


def _seed_quarter(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    list_type: str,
    transcript_id: int,
    document_id: int,
    period_end: str,
    fpt: str = "Q2",
) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO tracked_companies(ticker, list_type) VALUES (?, ?)",
        (ticker, list_type),
    )
    conn.execute(
        "INSERT INTO transcripts(id, document_id, ticker, call_date, "
        "fiscal_period_type, period_end, source_url) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            transcript_id,
            document_id,
            ticker,
            "2026-08-04",
            fpt,
            period_end,
            f"https://example.test/{ticker}/{period_end}",
        ),
    )
    conn.execute(
        "INSERT INTO transcript_segments(transcript_id, seq, speaker, speaker_role, "
        "time_code_start, text) VALUES (?, 1, 'CEO', 'executive', '00:01', ?)",
        (transcript_id, f"{ticker} management discussed the reported quarter."),
    )
    conn.execute(
        "INSERT INTO earnings_surprises(ticker, release_date, eps_estimate, eps_actual, "
        "eps_surprise_pct) VALUES (?, '2026-08-04', 1.0, 1.2, 20.0)",
        (ticker,),
    )


@pytest.fixture
def db(tmp_path: Path) -> Path:
    path = tmp_path / "portfolio.db"
    conn = sqlite3.connect(path)
    try:
        conn.executescript(_DDL)
        _seed_quarter(
            conn,
            ticker="WIX",
            list_type="portfolio",
            transcript_id=1,
            document_id=101,
            period_end="2026-06-30",
        )
        _seed_quarter(
            conn,
            ticker="NU",
            list_type="evaluation",
            transcript_id=2,
            document_id=102,
            period_end="2026-06-30",
        )
        conn.commit()
    finally:
        conn.close()
    return path


def test_scheduled_generation_is_portfolio_only_and_idempotent(
    db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import earnings_readout
    from llm_artifact_store import read_current

    calls: list[str] = []
    monkeypatch.setattr(
        earnings_readout,
        "call_llm",
        lambda prompt, **kwargs: calls.append(str(kwargs["ticker"])) or "# Persisted readout",
    )
    monkeypatch.setattr(earnings_readout, "should_skip_for_budget", lambda *a, **k: None)

    first = earnings_readout.generate_all(db, db.parent, today=date(2026, 8, 4))
    second = earnings_readout.generate_all(db, db.parent, today=date(2026, 8, 4))

    assert first[earnings_readout.GENERATED] == 1
    assert second[earnings_readout.CACHE_HIT] == 1
    assert calls == ["WIX"]
    assert (
        read_current(
            ticker="NU",
            purpose=earnings_readout.PURPOSE,
            fiscal_period="2026-06-30",
            db_path=db,
        )
        is None
    )


def test_evaluation_name_generates_only_when_explicitly_requested(
    db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import earnings_readout
    from llm_artifact_store import read_current

    calls: list[str] = []
    monkeypatch.setattr(
        earnings_readout,
        "call_llm",
        lambda prompt, **kwargs: calls.append(str(kwargs["ticker"])) or "# NU readout",
    )
    monkeypatch.setattr(earnings_readout, "should_skip_for_budget", lambda *a, **k: None)

    outcome = earnings_readout.generate_for_ticker(db, db.parent, "NU")

    assert outcome.status == earnings_readout.GENERATED
    assert outcome.fiscal_period == "2026-06-30"
    assert calls == ["NU"]
    artifact = read_current(
        ticker="NU",
        purpose=earnings_readout.PURPOSE,
        fiscal_period="2026-06-30",
        db_path=db,
    )
    assert artifact is not None
    assert artifact.source_doc_ids == [102]


def test_readout_persists_ordered_context_manifest_and_marks_missing_identity(
    db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import earnings_readout
    from llm_artifact_store import read_current

    monkeypatch.setattr(earnings_readout, "call_llm", _readout_text)
    monkeypatch.setattr(earnings_readout, "should_skip_for_budget", _no_budget_skip)
    monkeypatch.setattr(earnings_readout, "kpi_text", _net_retention_text)
    monkeypatch.setattr(earnings_readout, "valuation_text", _valuation_live_text)
    monkeypatch.setattr(earnings_readout, "watch_items_text", _watch_item_text)
    monkeypatch.setattr(earnings_readout, "tone_text", _tone_softened_text)
    monkeypatch.setattr(earnings_readout, "compose_anchor_block", _thesis_anchor_text)

    assert earnings_readout.generate_for_ticker(db, db.parent, "NU").status == "generated"
    artifact = read_current(
        ticker="NU",
        purpose=earnings_readout.PURPOSE,
        fiscal_period="2026-06-30",
        db_path=db,
    )

    assert artifact is not None
    assert isinstance(artifact.content_json, dict)
    manifest = cast(dict[str, object], artifact.content_json)
    assert manifest["schema_version"] == "post_earnings_readout_context@2"
    assert manifest["grounding_status"] == "partial"
    raw_blocks = manifest["blocks"]
    assert isinstance(raw_blocks, list)
    blocks = cast(list[dict[str, object]], raw_blocks)
    assert [block["kind"] for block in blocks] == [
        "reported_quarter_identity",
        "actuals_vs_consensus",
        "tracked_kpi_moves",
        "thesis_break_rules_prior_context",
        "open_watch_items_questions",
        "call_tone_change",
        "current_valuation_stance",
        "earnings_call_transcript",
    ]
    assert all(block["content"] for block in blocks)
    assert all(block["content_status"] == "present" for block in blocks)
    transcript = blocks[-1]
    transcript_source = transcript["source"]
    assert isinstance(transcript_source, dict)
    typed_transcript_source = cast(dict[str, object], transcript_source)
    assert typed_transcript_source["source_doc_id"] == 102
    assert typed_transcript_source["identity_status"] == "present"
    block_sources = [block["source"] for block in blocks[:-1]]
    assert all(isinstance(source, dict) for source in block_sources)
    typed_block_sources = cast(list[dict[str, object]], block_sources)
    assert any(source["identity_status"] == "missing" for source in typed_block_sources)
    assert artifact.source_doc_ids == [102]


def test_readout_manifest_preserves_empty_blocks_and_fails_grounding_closed(
    db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import earnings_readout
    from llm_artifact_store import read_current

    monkeypatch.setattr(earnings_readout, "call_llm", _readout_text)
    monkeypatch.setattr(earnings_readout, "should_skip_for_budget", _no_budget_skip)
    monkeypatch.setattr(earnings_readout, "_surprise_text", _empty_text)
    monkeypatch.setattr(earnings_readout, "kpi_text", _empty_text)
    monkeypatch.setattr(earnings_readout, "valuation_text", _empty_text)
    monkeypatch.setattr(earnings_readout, "watch_items_text", _empty_text)
    monkeypatch.setattr(earnings_readout, "tone_text", _empty_text)
    monkeypatch.setattr(earnings_readout, "compose_anchor_block", _empty_text)

    assert earnings_readout.generate_for_ticker(db, db.parent, "NU").status == "generated"
    artifact = read_current(
        ticker="NU",
        purpose=earnings_readout.PURPOSE,
        fiscal_period="2026-06-30",
        db_path=db,
    )

    assert artifact is not None
    manifest = cast(dict[str, object], artifact.content_json)
    assert isinstance(manifest, dict)
    assert manifest["schema_version"] == "post_earnings_readout_context@2"
    assert manifest["grounding_status"] == "partial"
    raw_blocks = manifest["blocks"]
    assert isinstance(raw_blocks, list)
    blocks = cast(list[dict[str, object]], raw_blocks)
    assert len(blocks) == 8
    empty_kinds = {str(block["kind"]) for block in blocks if block["content_status"] == "missing"}
    assert empty_kinds == {
        "actuals_vs_consensus",
        "tracked_kpi_moves",
        "thesis_break_rules_prior_context",
        "open_watch_items_questions",
        "call_tone_change",
        "current_valuation_stance",
    }
    missing_source_identities = manifest["missing_source_identities"]
    assert isinstance(missing_source_identities, list)
    assert empty_kinds <= set(cast(list[str], missing_source_identities))


def test_legacy_readout_without_context_manifest_still_reads(db: Path) -> None:
    from llm_artifact_store import read_current

    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "INSERT INTO llm_artifacts "
            "(ticker, purpose, fiscal_period, content_md, input_sha256, prompt_version, generated_at) "
            "VALUES ('NU', 'post_earnings_readout', '2026-03-31', 'legacy', ?, 'v1', ?) ",
            ("a" * 64, "2026-08-01T00:00:00+00:00"),
        )
        conn.commit()
    finally:
        conn.close()

    artifact = read_current(
        ticker="NU",
        purpose="post_earnings_readout",
        fiscal_period="2026-03-31",
        db_path=db,
    )
    assert artifact is not None
    assert artifact.content_md == "legacy"
    assert artifact.content_json is None


def test_new_reported_quarter_creates_a_distinct_current_artifact(
    db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import earnings_readout
    from llm_artifact_store import quarter_index

    monkeypatch.setattr(earnings_readout, "call_llm", lambda *a, **k: "readout")
    monkeypatch.setattr(earnings_readout, "should_skip_for_budget", lambda *a, **k: None)
    assert earnings_readout.generate_for_ticker(db, db.parent, "WIX").status == "generated"

    conn = sqlite3.connect(db)
    try:
        _seed_quarter(
            conn,
            ticker="WIX",
            list_type="portfolio",
            transcript_id=3,
            document_id=103,
            period_end="2026-09-30",
            fpt="Q3",
        )
        conn.commit()
    finally:
        conn.close()

    assert earnings_readout.generate_for_ticker(db, db.parent, "WIX").status == "generated"
    periods = {
        artifact.fiscal_period
        for artifact in quarter_index(ticker="WIX", purpose=earnings_readout.PURPOSE, db_path=db)
    }
    assert periods == {"2026-06-30", "2026-09-30"}


def test_budget_skip_prevents_on_request_token_burn(
    db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import earnings_readout

    calls: list[str] = []
    monkeypatch.setattr(
        earnings_readout,
        "call_llm",
        lambda *a, **k: calls.append("burn") or "readout",
    )
    monkeypatch.setattr(earnings_readout, "should_skip_for_budget", lambda *a, **k: object())

    outcome = earnings_readout.generate_for_ticker(db, db.parent, "NU")

    assert outcome.status == earnings_readout.BUDGET_SKIPPED
    assert calls == []
