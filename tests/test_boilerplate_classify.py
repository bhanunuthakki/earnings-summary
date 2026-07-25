"""Tests for the P3 orchestration module (filings.boilerplate_classify).

Weighted toward the failure modes the design doc calls out:

  * confidently-resolved (LOW/HIGH band) events must NEVER reach the LLM —
    token efficiency is a hard requirement, enforced here by asserting
    triage is not called when nothing is ambiguous;
  * an LLM failure must degrade every ambiguous survivor to `unclassified`,
    never a fabricated `boilerplate_update`;
  * a response missing one id's verdict leaves THAT row unclassified while
    resolving the rest;
  * the Filzen 2015 risk-growth override forces a would-be-LOW event into
    the LLM survivor lane instead of a confident boilerplate fast path;
  * persistence is idempotent and keyed by row id, not by (fragile) subject
    string matching.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from filings import boilerplate_classify as bc  # noqa: E402
from filings.boilerplate_triage import BoilerplateVerdict, ItemVerdict, TriageOutcome  # noqa: E402
from filings.models import HardStopError  # noqa: E402

_SCHEMA = """
CREATE TABLE filing_sections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker VARCHAR(16) NOT NULL,
    source VARCHAR(16) NOT NULL,
    source_ref VARCHAR(255) NOT NULL,
    form VARCHAR(8) NOT NULL,
    fiscal_year INTEGER,
    fiscal_period VARCHAR(4) NOT NULL,
    section_key_raw VARCHAR(255) NOT NULL,
    section_stem VARCHAR(255) NOT NULL,
    canonical_id VARCHAR(64),
    title TEXT,
    ordinal INTEGER NOT NULL,
    text TEXT NOT NULL,
    text_sha256 VARCHAR(64) NOT NULL,
    char_len INTEGER NOT NULL,
    extractor_version VARCHAR(32) NOT NULL,
    created_at DATETIME NOT NULL,
    CONSTRAINT uq_filing_sections_key UNIQUE (source, source_ref, section_key_raw, ordinal)
);
CREATE TABLE disclosure_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker VARCHAR(16) NOT NULL,
    event_type VARCHAR(32) NOT NULL,
    fiscal_year INTEGER,
    fiscal_period VARCHAR(4),
    canonical_id VARCHAR(64),
    subject VARCHAR(255) NOT NULL,
    subject_label TEXT,
    prior_excerpt TEXT,
    current_excerpt TEXT,
    evidence_quote TEXT,
    materiality FLOAT,
    verdict VARCHAR(24) NOT NULL DEFAULT 'unclassified',
    interpretation_md TEXT,
    confidence FLOAT,
    detector_version VARCHAR(32) NOT NULL,
    status VARCHAR(16) NOT NULL DEFAULT 'new',
    created_at DATETIME NOT NULL,
    CONSTRAINT uq_disclosure_events UNIQUE
        (ticker, event_type, fiscal_year, fiscal_period, subject, detector_version)
);
"""

_GENERIC_BOILERPLATE = (
    "There can be no assurance that our business will not be harmed. Such risks and "
    "uncertainties could adversely affect our business, financial condition and results "
    "of operations. We cannot guarantee that general economic conditions or competitive "
    "pressures may not adversely impact our future prospects."
)

_FIRM_SPECIFIC = (
    "In March 2025, our subsidiary MercadoPago launched a new credit product in Brazil "
    "and Mexico that increased total payment volume by $412 million, or 18%, compared to "
    "the prior year, following the December 2024 acquisition of KaveDinero."
)

_AMBIGUOUS_TEXT = (
    "Our Berlin office opened in 2023 and competes with Acme Corp for customers in "
    "several European markets, though broader macroeconomic conditions could still "
    "affect our results in ways that are difficult to predict at this time."
)


@pytest.fixture()
def conn() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.executescript(_SCHEMA)
    connection.commit()
    return connection


def _insert_event(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    event_type: str,
    subject: str,
    fiscal_year: int = 2025,
    fiscal_period: str = "Q1",
    canonical_id: str = "risk_factors",
    current_excerpt: str | None = None,
    prior_excerpt: str | None = None,
    evidence_quote: str = "quote",
) -> int:
    cur = conn.execute(
        """
        INSERT INTO disclosure_events
            (ticker, event_type, fiscal_year, fiscal_period, canonical_id, subject,
             subject_label, prior_excerpt, current_excerpt, evidence_quote,
             detector_version, status, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'item_diff_v1', 'new', '2026-01-01T00:00:00')
        """,
        (
            ticker,
            event_type,
            fiscal_year,
            fiscal_period,
            canonical_id,
            subject,
            subject,
            prior_excerpt,
            current_excerpt,
            evidence_quote,
        ),
    )
    conn.commit()
    assert cur.lastrowid is not None
    return int(cur.lastrowid)


# ---------------------------------------------------------------------------
# Deterministic fast paths never call the LLM
# ---------------------------------------------------------------------------


def test_deterministic_boilerplate_never_calls_llm(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    _insert_event(
        conn,
        ticker="AAA",
        event_type="item_added",
        subject="s1",
        current_excerpt=_GENERIC_BOILERPLATE,
    )

    def _boom(*args: object, **kwargs: object) -> object:
        raise AssertionError("triage_events must not be called for a confidently-boilerplate event")

    monkeypatch.setattr(bc, "triage_events", _boom)
    results, degraded = bc.classify_ticker_events(conn, "AAA")
    assert degraded is False
    assert len(results) == 1
    assert results[0].verdict == bc.BOILERPLATE_VERDICT
    assert results[0].llm_used is False


def test_deterministic_substantive_never_calls_llm(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    _insert_event(
        conn, ticker="AAA", event_type="item_added", subject="s1", current_excerpt=_FIRM_SPECIFIC
    )

    def _boom(*args: object, **kwargs: object) -> object:
        raise AssertionError("triage_events must not be called for a confidently-substantive event")

    monkeypatch.setattr(bc, "triage_events", _boom)
    results, degraded = bc.classify_ticker_events(conn, "AAA")
    assert degraded is False
    assert len(results) == 1
    assert results[0].verdict == bc.SUBSTANTIVE_VERDICT
    assert results[0].llm_used is False


# ---------------------------------------------------------------------------
# LLM survivor path
# ---------------------------------------------------------------------------


def test_ambiguous_event_uses_llm_and_persists_verdict(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    event_id = _insert_event(
        conn, ticker="AAA", event_type="item_added", subject="s1", current_excerpt=_AMBIGUOUS_TEXT
    )

    def _fake_triage(ticker: str, candidates: list[object], **kwargs: object) -> TriageOutcome:
        assert ticker == "AAA"
        return TriageOutcome(
            ticker=ticker,
            verdicts={
                event_id: ItemVerdict(
                    event_id=event_id,
                    verdict=BoilerplateVerdict.SUBSTANTIVE,
                    confidence=0.77,
                    rationale="test rationale",
                )
            },
        )

    monkeypatch.setattr(bc, "triage_events", _fake_triage)
    results, degraded = bc.classify_ticker_events(conn, "AAA")
    assert degraded is False
    assert len(results) == 1
    assert results[0].verdict == "substantive"
    assert results[0].confidence == 0.77
    assert results[0].llm_used is True

    bc.write_classifications(conn, results)
    row = conn.execute(
        "SELECT verdict, confidence, interpretation_md FROM disclosure_events WHERE id = ?",
        (event_id,),
    ).fetchone()
    assert row[0] == "substantive"
    assert row[1] == pytest.approx(0.77)
    assert row[2] == "test rationale"


def test_llm_failure_degrades_to_unclassified_never_boilerplate(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    _insert_event(
        conn, ticker="AAA", event_type="item_added", subject="s1", current_excerpt=_AMBIGUOUS_TEXT
    )

    def _fake_triage(ticker: str, candidates: list[object], **kwargs: object) -> TriageOutcome:
        return TriageOutcome(ticker=ticker, degraded=True, degrade_reason="simulated failure")

    monkeypatch.setattr(bc, "triage_events", _fake_triage)
    results, degraded = bc.classify_ticker_events(conn, "AAA")
    assert degraded is True
    assert len(results) == 1
    assert results[0].verdict == bc.UNCLASSIFIED_VERDICT
    assert results[0].verdict != bc.BOILERPLATE_VERDICT


def test_missing_verdict_for_one_id_leaves_it_unclassified(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    id1 = _insert_event(
        conn, ticker="AAA", event_type="item_added", subject="s1", current_excerpt=_AMBIGUOUS_TEXT
    )
    id2 = _insert_event(
        conn,
        ticker="AAA",
        event_type="item_added",
        subject="s2",
        current_excerpt=_AMBIGUOUS_TEXT + " extra words",
    )

    def _fake_triage(ticker: str, candidates: list[object], **kwargs: object) -> TriageOutcome:
        # Only answer for id1 -- id2's verdict is missing from the response.
        return TriageOutcome(
            ticker=ticker,
            verdicts={
                id1: ItemVerdict(
                    event_id=id1,
                    verdict=BoilerplateVerdict.BOILERPLATE_UPDATE,
                    confidence=0.9,
                    rationale="r",
                )
            },
        )

    monkeypatch.setattr(bc, "triage_events", _fake_triage)
    results, degraded = bc.classify_ticker_events(conn, "AAA")
    assert degraded is False
    by_id = {r.event_id: r for r in results}
    assert by_id[id1].verdict == bc.BOILERPLATE_VERDICT
    assert by_id[id2].verdict == bc.UNCLASSIFIED_VERDICT


# ---------------------------------------------------------------------------
# Filzen 2015 risk-growth override
# ---------------------------------------------------------------------------


def test_risk_growth_override_forces_llm_lane(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A would-be-LOW (confident boilerplate) event must be forced into the
    ambiguous/LLM lane when its ticker/period's risk section grew >100 words
    QoQ (Filzen 2015) -- that growth is evidence of real content, not filler."""
    event_id = _insert_event(
        conn,
        ticker="AAA",
        event_type="item_added",
        subject="s1",
        current_excerpt=_GENERIC_BOILERPLATE,
    )
    called = {"n": 0}

    def _fake_triage(ticker: str, candidates: list[object], **kwargs: object) -> TriageOutcome:
        called["n"] += 1
        return TriageOutcome(
            ticker=ticker,
            verdicts={
                event_id: ItemVerdict(
                    event_id=event_id,
                    verdict=BoilerplateVerdict.SUBSTANTIVE,
                    confidence=0.5,
                    rationale="r",
                )
            },
        )

    monkeypatch.setattr(bc, "triage_events", _fake_triage)
    flags: dict[tuple[str, int | None, str | None], bool] = {("AAA", 2025, "Q1"): True}
    results, _degraded = bc.classify_ticker_events(conn, "AAA", risk_growth_flags=flags)
    assert called["n"] == 1
    assert results[0].llm_used is True
    assert results[0].risk_growth_flagged is True


def test_compute_risk_growth_flags(conn: sqlite3.Connection) -> None:
    def _insert_section(ticker: str, fy: int, fp: str, n_words: int) -> None:
        text = " ".join(f"word{i}" for i in range(n_words))
        conn.execute(
            """
            INSERT INTO filing_sections
                (ticker, source, source_ref, form, fiscal_year, fiscal_period,
                 section_key_raw, section_stem, canonical_id, ordinal, text, text_sha256,
                 char_len, extractor_version, created_at)
            VALUES (?, 'edgar_text', ?, '10-K', ?, ?, 'Item 1A', 'item 1a', 'risk_factors', 0, ?, 'x',
                    ?, 'v1', '2026-01-01T00:00:00')
            """,
            (ticker, f"{ticker}-{fy}-{fp}", fy, fp, text, len(text)),
        )

    _insert_section("AAA", 2024, "Q4", 500)
    _insert_section("AAA", 2025, "Q1", 650)  # +150 words -> flagged
    conn.commit()

    flags = bc.compute_risk_growth_flags(conn)
    assert flags.get(("AAA", 2025, "Q1")) is True
    # First stored period has no prior -> never flagged.
    assert flags.get(("AAA", 2024, "Q4")) is None


# ---------------------------------------------------------------------------
# Hard stop
# ---------------------------------------------------------------------------


def test_missing_table_is_hard_stop() -> None:
    conn = sqlite3.connect(":memory:")
    with pytest.raises(HardStopError):
        bc.fetch_item_events(conn, "AAA")
