"""Round-trip + contract tests for the ``0065_news`` migration.

Two things are proven here:

  * **Round trip** — ``0065_news`` upgrade creates ``news`` with the six
    trigger-read columns plus ingestion bookkeeping; downgrade drops it and
    lands back at ``0064_queued_actions``; the inspector guard makes a second
    upgrade a no-op; upgrade→downgrade→upgrade is column-stable.

  * **Schema-matches-contract (the payoff guard)** — the schema is built via
    the *migration* (not the inline test fixture the trigger suite uses), then
    ``MaterialNewsTrigger.scan()`` runs against it with ``call_llm`` mocked to
    score one story material. A candidate is emitted, proving the migration
    satisfies the sensor's column contract verbatim — the whole point of the
    table existing.

Hermetic: a tmp_path-backed SQLite DB is stamped at the prior head and only the
new migration runs (mirrors ``test_alembic_personal_cio_migrations.py``); no
real LLM is called.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic.config import Config

from alembic import command
from triggers import MaterialNewsTrigger

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PRIOR_HEAD = "0064_queued_actions"
NEWS_HEAD = "0065_news"

# The six columns the trigger reads (src/triggers/material_news.py _NEWS_*).
_CONTRACT_COLUMNS = ("id", "ticker", "headline", "url", "published_at", "snippet")


def _build_config(db_path: Path) -> Config:
    """Alembic Config rooted at the repo's alembic.ini, sqlalchemy.url -> tmp DB."""
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


def _table_names(db_path: Path) -> set[str]:
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    finally:
        conn.close()
    return {str(r[0]) for r in rows}


def _columns(db_path: Path, table: str) -> list[str]:
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    finally:
        conn.close()
    return [str(r[1]) for r in rows]


def _index_names(db_path: Path, table: str) -> set[str]:
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(f"PRAGMA index_list({table})").fetchall()
    finally:
        conn.close()
    return {str(r[1]) for r in rows}


def _alembic_head(db_path: Path) -> str | None:
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute("SELECT version_num FROM alembic_version").fetchone()
    finally:
        conn.close()
    return None if row is None else str(row[0])


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    """Fresh tmp SQLite DB stamped at ``0064_queued_actions`` (the prior head).

    Stamping (vs. running every prior migration) is correct because ``0065_news``
    references no pre-0064 table — it is a standalone create.
    """
    db = tmp_path / "news_migration.db"
    cfg = _build_config(db)
    command.stamp(cfg, PRIOR_HEAD)
    return db


# ---------------------------------------------------------------------------
# Round trip
# ---------------------------------------------------------------------------


def test_upgrade_creates_news_with_contract_columns(db_path: Path) -> None:
    cfg = _build_config(db_path)
    command.upgrade(cfg, NEWS_HEAD)

    assert "news" in _table_names(db_path)
    assert _alembic_head(db_path) == NEWS_HEAD

    cols = _columns(db_path, "news")
    # The six trigger-read columns must be present and exactly named.
    for name in _CONTRACT_COLUMNS:
        assert name in cols, f"contract column {name!r} missing from migrated news table"
    # Ingestion bookkeeping is present but invisible to the trigger's SELECT.
    for name in ("source", "source_feed", "fetched_at"):
        assert name in cols, f"bookkeeping column {name!r} missing"

    assert "ix_news_ticker_published" in _index_names(db_path, "news")


def test_news_unique_ticker_url(db_path: Path) -> None:
    """UNIQUE (ticker, url): a duplicate (ticker, url) raises; the same url under
    a different ticker is allowed (syndication)."""
    cfg = _build_config(db_path)
    command.upgrade(cfg, NEWS_HEAD)

    conn = sqlite3.connect(str(db_path))
    try:
        _insert_news(conn, ticker="BN", url="https://news.example/x")
        # same (ticker, url) -> violates UNIQUE
        with pytest.raises(sqlite3.IntegrityError):
            _insert_news(conn, ticker="BN", url="https://news.example/x")
        # same url, different ticker -> allowed
        _insert_news(conn, ticker="GOOG", url="https://news.example/x")
        rows = conn.execute("SELECT COUNT(*) FROM news").fetchone()
        assert rows is not None
        assert rows[0] == 2
    finally:
        conn.close()


def test_downgrade_removes_news(db_path: Path) -> None:
    cfg = _build_config(db_path)
    command.upgrade(cfg, NEWS_HEAD)
    assert "news" in _table_names(db_path)

    command.downgrade(cfg, PRIOR_HEAD)

    assert "news" not in _table_names(db_path)
    assert _alembic_head(db_path) == PRIOR_HEAD


def test_upgrade_downgrade_upgrade_is_idempotent(db_path: Path) -> None:
    """A full round trip leaves the column set indistinguishable from the first
    upgrade — a forgotten Column() in a future recreate path would fail here."""
    cfg = _build_config(db_path)
    command.upgrade(cfg, NEWS_HEAD)
    cols_first = _columns(db_path, "news")

    command.downgrade(cfg, PRIOR_HEAD)
    assert "news" not in _table_names(db_path)

    command.upgrade(cfg, NEWS_HEAD)
    cols_second = _columns(db_path, "news")

    assert cols_first == cols_second
    assert _alembic_head(db_path) == NEWS_HEAD


def test_upgrade_noop_when_news_already_exists(db_path: Path) -> None:
    """The inspector guard: if ``news`` already exists, upgrade returns early
    without raising (a hand-created table is not clobbered)."""
    conn = sqlite3.connect(str(db_path))
    try:
        # Pre-create a divergent `news` table; the guard must leave it intact.
        _ = conn.execute("CREATE TABLE news (id INTEGER PRIMARY KEY, marker TEXT)")
        conn.commit()
    finally:
        conn.close()

    cfg = _build_config(db_path)
    command.upgrade(cfg, NEWS_HEAD)  # must not raise

    assert "marker" in _columns(db_path, "news"), "inspector guard clobbered an existing table"
    assert _alembic_head(db_path) == NEWS_HEAD


# ---------------------------------------------------------------------------
# Schema-matches-contract — the payoff guard
# ---------------------------------------------------------------------------


def _insert_news(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    url: str,
    headline: str = "A headline",
    published_at: datetime | None = None,
    snippet: str | None = None,
) -> int:
    """Insert one row through the *migrated* schema. ``fetched_at`` is NOT NULL
    with no server default, so the persistence layer (and this test) must supply
    it — proving the migration's bookkeeping contract."""
    now = datetime.now(UTC).replace(tzinfo=None)
    pub = (published_at or now).strftime("%Y-%m-%d %H:%M:%S")
    fetched = now.strftime("%Y-%m-%d %H:%M:%S")
    cur = conn.execute(
        "INSERT INTO news (ticker, headline, url, published_at, snippet, fetched_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (ticker, headline, url, pub, snippet, fetched),
    )
    conn.commit()
    return int(cur.lastrowid) if cur.lastrowid is not None else 0


def _anchor_stub(*_args: object, **_kwargs: object) -> str:
    """Stand-in for load_thesis_anchor — hermetic, no real-repo holdings read.
    The empty string exercises the prompt's generic-framing fallback."""
    return ""


class _StatefulLLM:
    """Canned-response call_llm stand-in tracking call count (mirrors the
    trigger suite's helper)."""

    def __init__(self, responses: list[str]) -> None:
        self.responses = responses
        self.call_count = 0

    def __call__(self, prompt: str, **_kwargs: object) -> str:
        idx = min(self.call_count, len(self.responses) - 1)
        self.call_count += 1
        return self.responses[idx]


def test_migrated_schema_satisfies_trigger_contract(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Build ``news`` via the migration, seed two recent stories, and run
    ``scan()`` with a mocked classifier: exactly the material story becomes a
    candidate. This is the reason the table exists — the migration's columns
    feed the trigger verbatim.

    No ``llm_artifacts`` table is created: the trigger's classification cache
    degrades gracefully when that table is absent (``read_current``/``upsert``
    return None/no-op), so the scan still classifies — and the test stays
    hermetic and free of cross-connection sqlite locking. Cache behavior is
    covered separately by ``test_trigger_material_news.py``."""
    cfg = _build_config(db_path)
    command.upgrade(cfg, NEWS_HEAD)

    conn = sqlite3.connect(str(db_path))
    try:
        now = datetime.now(UTC).replace(tzinfo=None)
        _insert_news(
            conn,
            ticker="BN",
            headline="Acme acquires Beta for $2B",
            url="https://news.example/1",
            published_at=now - timedelta(hours=1),
            snippet="A major acquisition reshaping the segment.",
        )
        _insert_news(
            conn,
            ticker="BN",
            headline="CEO sells routine shares under 10b5-1 plan",
            url="https://news.example/2",
            published_at=now - timedelta(hours=2),
        )

        # v3 classification shape: event_type rides every entry (a missing one
        # fails closed — see triggers.material_news._scores_from_payload).
        payload = json.dumps(
            [
                {
                    "news_index": 0,
                    "event_type": "primary",
                    "relevance": 0.90,
                    "why_material": "Material M&A",
                },
                {
                    "news_index": 1,
                    "event_type": "commentary",
                    "relevance": 0.10,
                    "why_material": "Routine insider sale",
                },
            ]
        )
        mock = _StatefulLLM([payload])
        monkeypatch.setattr("triggers.material_news.call_llm", mock)
        monkeypatch.setattr("triggers.material_news.load_thesis_anchor", _anchor_stub)

        candidates = MaterialNewsTrigger().scan("BN", conn)

        assert mock.call_count == 1  # one batched classification call
        assert len(candidates) == 1
        cand = candidates[0]
        assert cand.ticker == "BN"
        assert cand.evidence["headline"] == "Acme acquires Beta for $2B"
        assert isinstance(cand.evidence["relevance_score"], float)
        assert cand.evidence["relevance_score"] >= 0.7
    finally:
        conn.close()
