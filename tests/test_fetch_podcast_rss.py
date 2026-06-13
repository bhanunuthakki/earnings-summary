"""Tests for ``execution/fetch_podcast_rss.py`` — hermetic, no network.

All HTTP traffic is blocked: tests inject ``fetch_url`` directly into
``run()`` (the same seam that the network uses in production). Tests exercise:

* ``_parse_rss`` — pubDate filtering, URL extraction (link vs enclosure),
  description truncation, malformed XML degradation.
* ``_match_text`` — word-boundary company + investor matching, case-insensitivity,
  short-name exclusion, dedup within one call.
* ``run()`` end-to-end — company hit writes signal; investor hit writes signal
  with source_key as ticker; no match → nothing written; old episode → nothing
  written; re-poll same URL → idempotent (SKIPPED, not second INSERT); feed
  HTTP error → skip feed, continue; malformed XML → zero episodes, no crash.
"""

from __future__ import annotations

import sqlite3
import textwrap
from datetime import UTC, datetime
from pathlib import Path

import execution.fetch_podcast_rss as rss
from tests._signals_fixtures import signals_only

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 6, 13, 12, 0, 0, tzinfo=UTC)
_RECENT = "Fri, 13 Jun 2026 08:00:00 +0000"
_OLD = "Fri, 01 Jan 2026 00:00:00 +0000"

_SHOW = ("Test Show", "https://example.com/feed.rss")


def _rss_bytes(
    title: str = "Episode 1",
    *,
    url: str = "https://example.com/ep1",
    description: str = "",
    pub_date: str = _RECENT,
) -> bytes:
    """Minimal valid RSS XML with one item."""
    body = textwrap.dedent(f"""\
        <?xml version="1.0" encoding="UTF-8"?>
        <rss version="2.0">
          <channel>
            <title>Test Show</title>
            <item>
              <title>{title}</title>
              <link>{url}</link>
              <description>{description}</description>
              <pubDate>{pub_date}</pubDate>
            </item>
          </channel>
        </rss>
    """)
    return body.encode()


def _setup_db(
    tmp_path: Path,
    *,
    company_rows: list[tuple[str, str]] | None = None,
) -> Path:
    """Create a minimal DB with signals + optional tracked companies.

    ``signals_only`` runs the full migration chain 0094→0102, which includes
    migration 0098 that creates and seeds ``discovery_sources`` (27 investors
    including appaloosa / altimeter / tiger_global etc.). ``tracked_companies``
    is an init_db bootstrap table (not in alembic), so it is created here.

    ``company_rows`` = [(ticker, name), ...].
    """
    db_path = tmp_path / "test.db"
    signals_only(db_path)
    conn = sqlite3.connect(str(db_path))
    try:
        # tracked_companies is a bootstrap table (not in alembic migrations).
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tracked_companies (
                ticker TEXT NOT NULL,
                name TEXT NOT NULL,
                archived_at TEXT
            )
            """
        )
        if company_rows:
            conn.executemany(
                "INSERT INTO tracked_companies (ticker, name) VALUES (?, ?)",
                company_rows,
            )
        conn.commit()
    finally:
        conn.close()
    return db_path


def _count_signals(db_path: Path, signal_type: str = "media_appearance") -> int:
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM signals WHERE signal_type = ?", (signal_type,)
        ).fetchone()[0]
    finally:
        conn.close()


def _signal_ticker(db_path: Path) -> str:
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute(
            "SELECT ticker FROM signals WHERE signal_type = 'media_appearance' LIMIT 1"
        ).fetchone()[0]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# _parse_rss unit tests
# ---------------------------------------------------------------------------


def test_parse_rss_returns_recent_episode() -> None:
    episodes = rss.parse_rss(_rss_bytes(), cutoff_ts=_NOW.timestamp() - 86400)
    assert len(episodes) == 1
    assert episodes[0]["title"] == "Episode 1"
    assert episodes[0]["url"] == "https://example.com/ep1"


def test_parse_rss_filters_old_episodes() -> None:
    episodes = rss.parse_rss(_rss_bytes(pub_date=_OLD), cutoff_ts=_NOW.timestamp() - 86400)
    assert episodes == []


def test_parse_rss_no_pub_date_included() -> None:
    xml = b"""<?xml version="1.0"?>
    <rss><channel><item>
      <title>Undated</title><link>https://x.com/ep</link>
    </item></channel></rss>"""
    episodes = rss.parse_rss(xml, cutoff_ts=_NOW.timestamp())
    assert len(episodes) == 1  # no date → included (cannot confirm it is old)


def test_parse_rss_truncates_description() -> None:
    long_desc = "x" * 1000
    episodes = rss.parse_rss(_rss_bytes(description=long_desc), cutoff_ts=0.0)
    assert len(episodes[0]["description"] or "") == rss._DESC_CHARS  # type: ignore[arg-type]  # pyright: ignore[reportPrivateUsage]


def test_parse_rss_uses_enclosure_url_when_no_link() -> None:
    xml = textwrap.dedent("""\
        <?xml version="1.0"?>
        <rss><channel><item>
          <title>Ep</title>
          <enclosure url="https://audio.example.com/ep.mp3" type="audio/mpeg"/>
          <pubDate>Fri, 13 Jun 2026 10:00:00 +0000</pubDate>
        </item></channel></rss>
    """).encode()
    episodes = rss.parse_rss(xml, cutoff_ts=0.0)
    assert len(episodes) == 1
    assert episodes[0]["url"] == "https://audio.example.com/ep.mp3"


def test_parse_rss_skips_items_without_url() -> None:
    xml = b"""<?xml version="1.0"?>
    <rss><channel><item><title>No URL</title></item></channel></rss>"""
    assert rss.parse_rss(xml, cutoff_ts=0.0) == []


def test_parse_rss_malformed_xml_returns_empty() -> None:
    assert rss.parse_rss(b"<not valid xml<<<", cutoff_ts=0.0) == []


# ---------------------------------------------------------------------------
# _match_text unit tests
# ---------------------------------------------------------------------------

_COMPANY_MAP = {"apple": "AAPL", "nu holdings": "NU"}
_INVESTOR_MAP = {"appaloosa": "appaloosa", "tiger global": "tiger_global"}


def test_match_company_by_name() -> None:
    matches = rss.match_entities("Apple Q2 earnings beat", _COMPANY_MAP, {})
    assert ("AAPL", "company") in matches


def test_match_is_case_insensitive() -> None:
    matches = rss.match_entities("APPLE crushes estimates", _COMPANY_MAP, {})
    assert ("AAPL", "company") in matches


def test_no_partial_word_match() -> None:
    # "apple" should not match "applesauce"
    matches = rss.match_entities("applesauce company review", _COMPANY_MAP, {})
    assert not any(t == "AAPL" for t, _ in matches)


def test_match_investor_by_display_name() -> None:
    matches = rss.match_entities("Appaloosa's Tepper on macro", {}, _INVESTOR_MAP)
    assert ("appaloosa", "investor") in matches


def test_match_dedup_per_entity_within_call() -> None:
    matches = rss.match_entities("Apple Apple Apple", _COMPANY_MAP, {})
    assert len([m for m in matches if m[0] == "AAPL"]) == 1


def test_no_match_returns_empty() -> None:
    matches = rss.match_entities("Unrelated podcast about cooking", _COMPANY_MAP, _INVESTOR_MAP)
    assert matches == []


# ---------------------------------------------------------------------------
# run() end-to-end tests
# ---------------------------------------------------------------------------


def test_run_writes_company_signal(tmp_path: Path) -> None:
    db = _setup_db(tmp_path, company_rows=[("AAPL", "Apple")])
    rss.run(
        db_path=db,
        days=7,
        fetch_url=lambda _url: _rss_bytes(title="Apple Q2 results", url="https://x.com/ep1"),
        feeds=(_SHOW,),
        now=_NOW,
    )
    assert _count_signals(db) == 1
    assert _signal_ticker(db) == "AAPL"


def test_run_writes_investor_signal_with_source_key_as_ticker(tmp_path: Path) -> None:
    # "Appaloosa (Tepper)" is seeded by migration 0098 with source_key="appaloosa".
    # Word-boundary match on "appaloosa" fires against the seeded display_name.
    db = _setup_db(tmp_path)
    rss.run(
        db_path=db,
        days=7,
        fetch_url=lambda _url: _rss_bytes(title="Appaloosa macro thesis", url="https://x.com/ep2"),
        feeds=(_SHOW,),
        now=_NOW,
    )
    assert _count_signals(db) == 1
    assert _signal_ticker(db) == "APPALOOSA"  # record_media_appearance uppercases ticker


def test_run_no_match_writes_nothing(tmp_path: Path) -> None:
    db = _setup_db(tmp_path, company_rows=[("AAPL", "Apple")])
    rss.run(
        db_path=db,
        days=7,
        fetch_url=lambda _url: _rss_bytes(title="Unrelated cooking show", url="https://x.com/ep3"),
        feeds=(_SHOW,),
        now=_NOW,
    )
    assert _count_signals(db) == 0


def test_run_old_episode_writes_nothing(tmp_path: Path) -> None:
    db = _setup_db(tmp_path, company_rows=[("AAPL", "Apple")])
    rss.run(
        db_path=db,
        days=7,
        fetch_url=lambda _url: _rss_bytes(
            title="Apple Q1 results", url="https://x.com/ep4", pub_date=_OLD
        ),
        feeds=(_SHOW,),
        now=_NOW,
    )
    assert _count_signals(db) == 0


def test_run_idempotent_on_repoll(tmp_path: Path) -> None:
    db = _setup_db(tmp_path, company_rows=[("AAPL", "Apple")])
    for _ in range(3):
        rss.run(
            db_path=db,
            days=7,
            fetch_url=lambda _url: _rss_bytes(title="Apple earnings", url="https://x.com/ep5"),
            feeds=(_SHOW,),
            now=_NOW,
        )
    assert _count_signals(db) == 1  # dedup: same (ticker, url) written only once


def test_run_feed_http_error_skips_feed(tmp_path: Path) -> None:
    db = _setup_db(tmp_path, company_rows=[("AAPL", "Apple")])

    def _fail(url: str) -> bytes:
        raise OSError("connection refused")

    summary = rss.run(db_path=db, days=7, fetch_url=_fail, feeds=(_SHOW,), now=_NOW)
    assert summary["feeds"] == 0
    assert _count_signals(db) == 0


def test_run_malformed_xml_no_crash(tmp_path: Path) -> None:
    db = _setup_db(tmp_path, company_rows=[("AAPL", "Apple")])
    rss.run(
        db_path=db,
        days=7,
        fetch_url=lambda _url: b"<broken<<<xml",
        feeds=(_SHOW,),
        now=_NOW,
    )
    assert _count_signals(db) == 0


def test_run_multiple_matches_in_one_episode(tmp_path: Path) -> None:
    db = _setup_db(
        tmp_path,
        company_rows=[("AAPL", "Apple"), ("NU", "Nu Holdings")],
    )
    rss.run(
        db_path=db,
        days=7,
        fetch_url=lambda _url: _rss_bytes(
            title="Apple and Nu Holdings both soar", url="https://x.com/ep6"
        ),
        feeds=(_SHOW,),
        now=_NOW,
    )
    assert _count_signals(db) == 2


def test_run_summary_counts(tmp_path: Path) -> None:
    db = _setup_db(tmp_path, company_rows=[("AAPL", "Apple")])
    summary = rss.run(
        db_path=db,
        days=7,
        fetch_url=lambda _url: _rss_bytes(title="Apple results", url="https://x.com/ep7"),
        feeds=(_SHOW,),
        now=_NOW,
    )
    assert summary["feeds"] == 1
    assert summary["episodes"] == 1
    assert summary["written"] == 1


def test_run_inactive_investor_not_matched(tmp_path: Path) -> None:
    # Seed appaloosa is active by default; deactivate it and verify no signal.
    db = _setup_db(tmp_path)
    conn = sqlite3.connect(str(db))
    try:
        conn.execute("UPDATE discovery_sources SET active = 0 WHERE source_key = 'appaloosa'")
        conn.commit()
    finally:
        conn.close()

    rss.run(
        db_path=db,
        days=7,
        fetch_url=lambda _url: _rss_bytes(title="Appaloosa macro thesis", url="https://x.com/ep8"),
        feeds=(_SHOW,),
        now=_NOW,
    )
    assert _count_signals(db) == 0


def test_run_missing_discovery_sources_degrades(tmp_path: Path) -> None:
    """If discovery_sources table is absent, investor matching is skipped (no crash)."""
    db = _setup_db(tmp_path, company_rows=[("AAPL", "Apple")])
    conn = sqlite3.connect(str(db))
    try:
        conn.execute("DROP TABLE IF EXISTS discovery_sources")
        conn.commit()
    finally:
        conn.close()

    rss.run(
        db_path=db,
        days=7,
        fetch_url=lambda _url: _rss_bytes(title="Apple results", url="https://x.com/ep9"),
        feeds=(_SHOW,),
        now=_NOW,
    )
    assert _count_signals(db) == 1  # company match still works
