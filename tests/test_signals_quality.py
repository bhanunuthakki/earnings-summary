"""Tests for the diet LLM quality score (alembic 0150 + src/signals/quality.py
+ the load_diet_signals read-time gate).

The intelligent replacement for the static publisher denylist: a batched Haiku
pass stamps ``signals.quality_score`` at ingest; the reader filters on the
STORED score (guard-safe: a stored-field filter, never a wall-clock decay) and
falls back to the denylist for unscored rows.

All LLM transport is monkeypatched at the module seam
(``signals.quality.call_llm_structured``) — no real spend.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic.config import Config

from alembic import command
from llm.structured import StructuredParseError
from signals import quality as q
from signals.quality import score_unscored_signals
from signals.store import MIN_QUALITY_SCORE, load_diet_signals

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# Hand-built post-0150 shape (0095 columns + quality_score) — unit tests of
# the scorer/read legs don't need the full migration chain.
_SIGNALS_DDL = """
CREATE TABLE signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    signal_type TEXT NOT NULL,
    title TEXT NOT NULL,
    body TEXT,
    url TEXT,
    firm TEXT,
    event_date TEXT,
    published_at TEXT NOT NULL,
    weight REAL NOT NULL DEFAULT 1.0,
    cadence TEXT NOT NULL DEFAULT 'event',
    source_feed TEXT,
    news_id INTEGER,
    created_at TEXT NOT NULL,
    quality_score REAL
)
"""


@pytest.fixture()
def db(tmp_path: Path) -> Path:
    path = tmp_path / "signals.db"
    conn = sqlite3.connect(str(path))
    conn.executescript(_SIGNALS_DDL)
    conn.commit()
    conn.close()
    return path


def _insert_signal(
    path: Path,
    *,
    ticker: str = "NU",
    signal_type: str = "general_news",
    title: str = "A story",
    firm: str | None = "Reuters",
    published_at: str = "2026-07-01 09:00:00",
    weight: float = 0.5,
    quality_score: float | None = None,
) -> int:
    conn = sqlite3.connect(str(path))
    cur = conn.execute(
        "INSERT INTO signals (ticker, signal_type, title, firm, published_at, weight, "
        "cadence, created_at, quality_score) VALUES (?, ?, ?, ?, ?, ?, 'event', ?, ?)",
        (ticker, signal_type, title, firm, published_at, weight, published_at, quality_score),
    )
    conn.commit()
    row_id = int(cur.lastrowid or 0)
    conn.close()
    return row_id


def _stored_scores(path: Path) -> dict[int, float | None]:
    conn = sqlite3.connect(str(path))
    rows = conn.execute("SELECT id, quality_score FROM signals").fetchall()
    conn.close()
    return {int(r[0]): (float(r[1]) if r[1] is not None else None) for r in rows}


def _batch_size_of(prompt: str) -> int:
    """The number of signal lines in one batch prompt — the JSON lines after
    the final 'Signals:' marker (the template body also contains '"id"'
    literals, so a naive substring count over-reads)."""
    tail = prompt.rsplit("Signals:", 1)[1]
    return sum(1 for line in tail.strip().splitlines() if line.strip().startswith("{"))


# ---------------------------------------------------------------------------
# Migration — quality_score lands on a full-chain DB
# ---------------------------------------------------------------------------


def test_migration_adds_quality_score_column(tmp_path: Path) -> None:
    """Full-chain upgrade (same launch point as the diet guard) leaves
    ``signals.quality_score`` present and nullable."""
    db = tmp_path / "mig.db"
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db}")
    command.stamp(cfg, "0059_kpi_facts_restatement")
    command.upgrade(cfg, "head")

    conn = sqlite3.connect(str(db))
    cols = {r[1]: r for r in conn.execute("PRAGMA table_info(signals)").fetchall()}
    conn.close()
    assert "quality_score" in cols
    assert cols["quality_score"][3] == 0  # nullable (notnull flag off)


# ---------------------------------------------------------------------------
# Scorer — batch parse, validation, cap, degradation
# ---------------------------------------------------------------------------


def test_score_unscored_signals_stamps_valid_scores(
    db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    id_a = _insert_signal(db, title="Original scoop", firm="Reuters")
    id_b = _insert_signal(db, title="3 stocks to buy", firm="Zacks", signal_type="consensus_rating")
    id_c = _insert_signal(
        db, signal_type="investor_day", title="Analyst day"
    )  # diet-only: never scored

    seen: dict[str, object] = {}

    def fake_structured(prompt: str, **kwargs: object) -> object:
        seen["prompt"] = prompt
        seen["kwargs"] = kwargs
        return [
            {"id": id_a, "score": 0.9},
            {"id": id_b, "score": 0.15},
            {"id": 424242, "score": 0.5},  # unknown id — dropped
            {"id": id_a, "score": 7.0},  # out of range — dropped (first score kept)
            "garbage",  # not an object — dropped
        ]

    monkeypatch.setattr(q, "call_llm_structured", fake_structured)
    tally = score_unscored_signals(db)
    assert tally["scored"] == 2
    assert tally["deferred_transient"] == 0

    stored = _stored_scores(db)
    assert stored[id_a] == 0.9
    assert stored[id_b] == 0.15
    assert stored[id_c] is None  # investor_day is not a news-backed lane

    kwargs = seen["kwargs"]
    assert isinstance(kwargs, dict)
    assert kwargs["purpose"] == "diet_source_quality"
    assert kwargs["expect"] == "array"
    prompt = seen["prompt"]
    assert isinstance(prompt, str)
    assert "Original scoop" in prompt and "Zacks" in prompt
    assert "Analyst day" not in prompt


def test_score_unscored_signals_batches_and_caps(db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """45 unscored rows, batch_size 20 → 3 calls (20/20/5); max_rows caps the
    run so a large backlog drains over several runs, not one."""
    for i in range(45):
        _insert_signal(db, title=f"story {i}", published_at=f"2026-07-01 09:{i:02d}:00")

    batch_sizes: list[int] = []

    def fake_structured(prompt: str, **kwargs: object) -> object:
        batch_sizes.append(_batch_size_of(prompt))
        return []

    monkeypatch.setattr(q, "call_llm_structured", fake_structured)
    score_unscored_signals(db)
    assert batch_sizes == [20, 20, 5]

    batch_sizes.clear()
    score_unscored_signals(db, max_rows=8, batch_size=4)
    assert batch_sizes == [4, 4]


def test_score_unscored_signals_is_idempotent_over_scored_rows(
    db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rid = _insert_signal(db)

    def scores_once(*args: object, **kwargs: object) -> object:
        return [{"id": rid, "score": 0.8}]

    monkeypatch.setattr(q, "call_llm_structured", scores_once)
    assert score_unscored_signals(db)["scored"] == 1

    def exploding(*args: object, **kwargs: object) -> object:
        raise AssertionError("scored rows must not be re-sent to the LLM")

    monkeypatch.setattr(q, "call_llm_structured", exploding)
    tally = score_unscored_signals(db)
    assert tally["scored"] == 0
    assert _stored_scores(db)[rid] == 0.8


def test_parse_failure_leaves_null_and_does_not_raise(
    db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rid = _insert_signal(db)

    def bad_twice(*args: object, **kwargs: object) -> object:
        raise StructuredParseError("unusable twice", raw_head="?")

    monkeypatch.setattr(q, "call_llm_structured", bad_twice)
    tally = score_unscored_signals(db)
    assert tally["parse_failed"] == 1
    assert _stored_scores(db)[rid] is None  # retried next run


def test_empty_response_leaves_null_and_does_not_raise(
    db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty (or all-invalid) array is a quality miss, not a crash: rows
    stay NULL and the read path's denylist fallback governs them."""
    rid = _insert_signal(db)

    def empty_array(*args: object, **kwargs: object) -> object:
        return []

    monkeypatch.setattr(q, "call_llm_structured", empty_array)
    tally = score_unscored_signals(db)
    assert tally["scored"] == 0
    assert _stored_scores(db)[rid] is None


def test_transient_failure_defers_batch_but_continues(
    db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Quota-scheduling rule 3: a transient CLI failure defers THAT batch
    (rows stay NULL, tallied) and the loop continues to the next batch."""
    for i in range(4):
        _insert_signal(db, title=f"story {i}", published_at=f"2026-07-01 09:0{i}:00")

    calls: list[int] = []

    def flaky(prompt: str, **kwargs: object) -> object:
        calls.append(_batch_size_of(prompt))
        if len(calls) == 1:
            raise RuntimeError(
                "Claude CLI failed AND no Gemini fallback configured. "
                "Original Claude error: CalledProcessError: exit 1"
            )
        return []

    monkeypatch.setattr(q, "call_llm_structured", flaky)
    tally = score_unscored_signals(db, batch_size=2)
    assert tally["deferred_transient"] == 1
    assert calls == [2, 2]  # both batches attempted
    assert all(score is None for score in _stored_scores(db).values())


def test_hard_stop_propagates(db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from llm.cli import LLMSetupError

    _insert_signal(db)

    def setup_broken(*args: object, **kwargs: object) -> object:
        raise LLMSetupError("Claude Code CLI ('claude') not found in PATH.")

    monkeypatch.setattr(q, "call_llm_structured", setup_broken)
    with pytest.raises(LLMSetupError):
        score_unscored_signals(db)


def test_pre_0150_db_degrades_without_llm_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No quality_score column (pre-migration): tallied unavailable, zero
    LLM spend."""
    path = tmp_path / "old.db"
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE signals (id INTEGER PRIMARY KEY, signal_type TEXT, title TEXT)")
    conn.commit()
    conn.close()

    def exploding(*args: object, **kwargs: object) -> object:
        raise AssertionError("no LLM call on a pre-0150 DB")

    monkeypatch.setattr(q, "call_llm_structured", exploding)
    tally = score_unscored_signals(path)
    assert tally["db_unavailable"] == 1

    missing = score_unscored_signals(tmp_path / "nope.db")
    assert missing["db_unavailable"] == 1


# ---------------------------------------------------------------------------
# Read path — load_diet_signals filters on the stored score, denylist fallback
# ---------------------------------------------------------------------------


def test_load_diet_signals_filters_on_stored_score_with_denylist_fallback(db: Path) -> None:
    # Scored LOW from a reputable wire — the score governs: dropped.
    _insert_signal(db, title="Recycled recap", firm="Reuters", quality_score=0.1)
    # Scored HIGH from a denylisted publisher — the score governs: kept
    # (the LLM judged the substance; the brand-name denylist is fallback only).
    _insert_signal(db, title="Zacks real scoop", firm="Zacks", quality_score=0.9)
    # Unscored (NULL) + denylisted firm — denylist fallback: dropped.
    _insert_signal(db, title="Zacks farm recap", firm="Zacks Investment Research")
    # Unscored (NULL) + reputable firm — kept.
    _insert_signal(db, title="Bloomberg story", firm="Bloomberg")
    # Scored exactly AT the threshold — kept (>= MIN_QUALITY_SCORE).
    _insert_signal(db, title="Borderline story", firm="X", quality_score=MIN_QUALITY_SCORE)

    titles = {s.title for s in load_diet_signals(db)}
    assert titles == {"Zacks real scoop", "Bloomberg story", "Borderline story"}

    by_title = {s.title: s for s in load_diet_signals(db)}
    assert by_title["Zacks real scoop"].quality_score == 0.9
    assert by_title["Bloomberg story"].quality_score is None


def test_load_diet_signals_pre_0150_db_falls_back_to_denylist(tmp_path: Path) -> None:
    """A DB without the quality_score column (pre-migration / mid-rollout):
    the reader degrades to the legacy select and the denylist governs alone."""
    path = tmp_path / "old.db"
    conn = sqlite3.connect(str(path))
    conn.executescript(_SIGNALS_DDL.replace(",\n    quality_score REAL", ""))
    conn.execute(
        "INSERT INTO signals (ticker, signal_type, title, firm, published_at, weight, "
        "cadence, created_at) VALUES ('NU', 'general_news', 'Real story', 'Reuters', "
        "'2026-07-01 09:00:00', 0.5, 'event', 't')"
    )
    conn.execute(
        "INSERT INTO signals (ticker, signal_type, title, firm, published_at, weight, "
        "cadence, created_at) VALUES ('NU', 'general_news', 'Farm recap', 'Motley Fool', "
        "'2026-07-01 10:00:00', 0.5, 'event', 't')"
    )
    conn.commit()
    conn.close()
    signals = load_diet_signals(path)
    assert [s.title for s in signals] == ["Real story"]
    assert signals[0].quality_score is None
