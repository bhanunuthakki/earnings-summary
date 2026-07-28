"""Piece 2 — competitive-mention extractor over RBRK earnings transcripts."""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from competitive.transcript_mentions import (  # noqa: E402
    count_mentions,
    extract_for_ticker,
)

from ._competitive_fixtures import kpi_conn  # noqa: E402


# --------------------------------------------------------------------------- #
# Pure counter
# --------------------------------------------------------------------------- #
def test_counts_displacement_of_legacy() -> None:
    text = (
        "We displaced a legacy backup vendor at a large bank this quarter. "
        "The customer ripped and replaced their incumbent solution. "
        "Revenue grew nicely and margins expanded."  # not a displacement sentence
    )
    counts = count_mentions(text)
    assert counts.displacement == 2
    assert counts.displacement_examples  # captured evidence snippets


def test_displacement_requires_verb_and_legacy_object() -> None:
    # A displacement verb with NO legacy/competitor object does not count.
    text = "We replaced our CFO this quarter. We had a great quarter overall."
    assert count_mentions(text).displacement == 0
    # Explicit phrase counts on its own.
    assert (
        count_mentions("Another competitive displacement in financial services.").displacement == 1
    )


def test_counts_large_and_million_dollar_wins() -> None:
    text = (
        "We closed a $3 million expansion with a Fortune 500 logo. "
        "We also signed a seven-figure deal in EMEA. "
        "A smaller $400,000 renewal also closed."  # under $1M -> not a large win
    )
    counts = count_mentions(text)
    assert counts.large_win == 2  # $3M+Fortune 500 sentence, and seven-figure sentence


def test_sub_million_dollar_not_counted_as_large_win() -> None:
    assert count_mentions("A modest $500,000 deal closed.").large_win == 0
    assert count_mentions("We landed a $1 million deal.").large_win == 1


def test_counts_named_competitor_mentions_with_aliases() -> None:
    text = (
        "Customers are switching from Cohesity and Veeam. "
        "We also see Dell PowerProtect and Commvault in deals, plus Druva and Veritas."
    )
    counts = count_mentions(text)
    # Cohesity, Veritas(->Cohesity), Veeam, Dell PowerProtect(->Dell, one match),
    # Commvault, Druva = 6 vendor references.
    assert counts.named_competitor == 6
    assert counts.vendor_breakdown["Cohesity"] == 2  # Cohesity + Veritas
    assert counts.vendor_breakdown["Dell"] == 1  # "Dell PowerProtect" counts once


def test_empty_text_is_all_zero() -> None:
    counts = count_mentions("")
    assert (counts.displacement, counts.large_win, counts.named_competitor) == (0, 0, 0)


# --------------------------------------------------------------------------- #
# On-disk scan + persist (RBRK Jan fiscal-year-end mapping)
# --------------------------------------------------------------------------- #
def _write_transcript(transcripts_root: Path, name: str, body: str) -> None:
    d = transcripts_root / "raw"
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(body, encoding="utf-8")


def test_extract_for_ticker_writes_quarterly_counts(tmp_path: Path) -> None:
    transcripts_root = tmp_path / "transcripts"
    _write_transcript(
        transcripts_root,
        "RBRK_Q4_2026.txt",
        "We displaced a legacy Cohesity deployment and closed a $5 million Fortune 100 win. "
        "Veeam also came up on the call.",
    )
    conn = kpi_conn()
    result = extract_for_ticker(conn, tmp_path, "RBRK", transcripts_root=transcripts_root)

    assert len(result.quarters) == 1
    q = result.quarters[0]
    assert q.quarter == "Q4"
    assert q.period_end == "2026-01-31"  # RBRK Jan FYE: Q4 FY2026 ends Jan 31 2026
    assert q.counts.displacement == 1
    assert q.counts.large_win == 1
    assert q.counts.named_competitor == 2  # Cohesity + Veeam
    assert q.inserted == 3  # one fact per signal metric

    rows = conn.execute(
        "SELECT d.name, f.value, f.unit, f.fiscal_period_type, f.period_end "
        "FROM kpi_facts f JOIN kpi_definitions d ON d.id = f.kpi_definition_id "
        "WHERE f.ticker = 'RBRK' ORDER BY d.name"
    ).fetchall()
    by_name = {r["name"]: r for r in rows}
    assert set(by_name) == {
        "Competitive displacement-of-legacy mentions (count)",
        "Large-deal / >$1M-logo win mentions (count)",
        "Named-competitor mentions — Cohesity/Veeam/Dell (count)",
    }
    for r in rows:
        assert r["unit"] == "count"
        assert r["fiscal_period_type"] == "Q4"
        assert str(r["period_end"]).startswith("2026-01-31")
    assert float(by_name["Named-competitor mentions — Cohesity/Veeam/Dell (count)"]["value"]) == 2.0
    assert [
        row[0]
        for row in conn.execute(
            "SELECT status FROM ingestion_runs WHERE directive='extract_competitive_mentions'"
        ).fetchall()
    ] == ["ok"]


def test_extract_processed_wins_over_raw(tmp_path: Path) -> None:
    transcripts_root = tmp_path / "transcripts"
    _write_transcript(transcripts_root, "RBRK_Q1_2026.txt", "Veeam mentioned once in raw.")
    proc = transcripts_root / "processed"
    proc.mkdir(parents=True, exist_ok=True)
    (proc / "RBRK_Q1_2026.txt").write_text(
        "Cohesity and Veeam and Dell all mentioned in processed.", encoding="utf-8"
    )
    conn = kpi_conn()
    result = extract_for_ticker(conn, tmp_path, "RBRK", transcripts_root=transcripts_root)
    assert len(result.quarters) == 1
    # processed copy wins -> 3 vendor mentions, not the raw's 1
    assert result.quarters[0].counts.named_competitor == 3


def test_extract_is_idempotent(tmp_path: Path) -> None:
    transcripts_root = tmp_path / "transcripts"
    _write_transcript(transcripts_root, "RBRK_Q4_2026.txt", "We displaced legacy Cohesity.")
    conn = kpi_conn()
    extract_for_ticker(conn, tmp_path, "RBRK", transcripts_root=transcripts_root)
    extract_for_ticker(conn, tmp_path, "RBRK", transcripts_root=transcripts_root)
    n = conn.execute("SELECT COUNT(*) FROM kpi_facts WHERE ticker = 'RBRK'").fetchone()[0]
    assert n == 3  # second run is a no-op (same provenance key)


def test_extract_no_transcripts_is_noop(tmp_path: Path) -> None:
    conn = kpi_conn()
    result = extract_for_ticker(conn, tmp_path, "RBRK", transcripts_root=tmp_path / "transcripts")
    assert result.quarters == []
