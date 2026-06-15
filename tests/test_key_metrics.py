"""The "key metrics" preselect bubble row (directives/key_metrics_picker.md):
the render-side merge (tier-graded baseline + cached LLM picks → bubbles) and
the build-side LLM extraction (vocabulary-validated suggestions + sha-keyed
idempotent cache).

The render-side tests use a minimal hand-rolled DB (only the two columns
``tier_graded_baseline`` reads) so they stay fast and alembic-free; the
build-side tests monkeypatch the vocabulary + the LLM call.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from compute import key_metrics as km_build
from compute.key_metrics import (
    KeyMetricsResult,
    extract_for_ticker,
    suggest_key_metrics,
)
from pipeline.key_metrics import (
    KeyMetricBubble,
    key_metric_bubbles,
    load_llm_picks,
    render_key_metrics_inner,
    tier_graded_baseline,
)

_KPI_DDL = """
CREATE TABLE kpi_definitions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    name TEXT NOT NULL,
    threshold_tier TEXT
);
CREATE TABLE kpi_facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    kpi_definition_id INTEGER NOT NULL,
    value NUMERIC NOT NULL DEFAULT 0
);
"""

_CATALOG: dict[str, list[dict[str, object]]] = {
    "fin": [{"token": "fin:revenue", "label": "revenue", "tickers": 1}],
    "kpi": [
        {"token": "kpi:NIM", "label": "NIM", "tickers": 1},
        {"token": "kpi:NPL", "label": "NPL", "tickers": 1},
        {"token": "kpi:Deposits", "label": "Deposits", "tickers": 1},
    ],
    "seg": [],
}


def _make_db(tmp_path: Path) -> Path:
    """A minimal portfolio.db with two tier-graded KPIs (NIM=tier-1, NPL=tier-2)
    that both carry a fact for NU."""
    db_path = tmp_path / "data" / "portfolio.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.executescript(_KPI_DDL)
    conn.execute(
        "INSERT INTO kpi_definitions (id, ticker, name, threshold_tier) "
        "VALUES (1, 'NU', 'NIM', 'tier_1_break'), (2, 'NU', 'NPL', 'tier_2_monitor'), "
        "(3, 'NU', 'Deposits', NULL)"
    )
    conn.executemany(
        "INSERT INTO kpi_facts (ticker, kpi_definition_id, value) VALUES (?, ?, ?)",
        [("NU", 1, 0.18), ("NU", 2, 0.04), ("NU", 3, 100.0)],
    )
    conn.commit()
    conn.close()
    return db_path


# ---------------------------------------------------------------------------
# render-side: tier baseline + cache read + merge + html
# ---------------------------------------------------------------------------


def test_tier_graded_baseline_orders_tier1_first(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    rows = tier_graded_baseline(db_path, ["NU"])
    # NIM (tier-1) precedes NPL (tier-2); the un-graded Deposits is absent.
    assert rows == [("kpi:NIM", "tier_1_break"), ("kpi:NPL", "tier_2_monitor")]


def test_tier_graded_baseline_tolerates_missing_db(tmp_path: Path) -> None:
    assert tier_graded_baseline(tmp_path / "nope.db", ["NU"]) == []
    # An empty ticker set is a no-op, never a query.
    db_path = _make_db(tmp_path)
    assert tier_graded_baseline(db_path, []) == []


def test_load_llm_picks_reads_cache_and_tolerates_junk(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    cache_dir = db_path.parent / "key_metrics"
    cache_dir.mkdir()
    (cache_dir / "NU.json").write_text(
        json.dumps(
            {
                "ticker": "NU",
                "metrics": [
                    {"token": "kpi:Deposits", "why": "low-cost funding base"},
                    {"token": "fin:revenue", "why": "top line"},
                    {"bad": "no token"},
                ],
            }
        ),
        encoding="utf-8",
    )
    assert load_llm_picks(db_path, "NU") == [
        ("kpi:Deposits", "low-cost funding base"),
        ("fin:revenue", "top line"),
    ]
    # Absent / malformed cache → [] (degrade, never raise).
    assert load_llm_picks(db_path, "MELI") == []
    (cache_dir / "BAD.json").write_text("{not json", encoding="utf-8")
    assert load_llm_picks(db_path, "BAD") == []


def test_key_metric_bubbles_merges_dedupes_and_validates(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    cache_dir = db_path.parent / "key_metrics"
    cache_dir.mkdir()
    (cache_dir / "NU.json").write_text(
        json.dumps(
            {
                "ticker": "NU",
                "metrics": [
                    {"token": "kpi:NIM", "why": "dup of the tier baseline"},
                    {"token": "kpi:Deposits", "why": "low-cost funding base"},
                    {"token": "fin:revenue", "why": "top line"},
                    {"token": "kpi:HALLUCINATED", "why": "not in the catalog"},
                ],
            }
        ),
        encoding="utf-8",
    )
    bubbles = key_metric_bubbles(db_path, ["NU"], _CATALOG)
    tokens = [b.token for b in bubbles]
    # Tier baseline first (NIM, NPL), then LLM picks not already present
    # (Deposits, revenue). NIM is deduped; HALLUCINATED dropped (not in catalog).
    assert tokens == ["kpi:NIM", "kpi:NPL", "kpi:Deposits", "fin:revenue"]
    by_token = {b.token: b for b in bubbles}
    assert by_token["kpi:NIM"].source == "tier"
    assert by_token["kpi:Deposits"].source == "llm"
    assert by_token["kpi:Deposits"].label == "Deposits"
    assert "funding" in by_token["kpi:Deposits"].title


def test_key_metric_bubbles_caps_the_row(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    bubbles = key_metric_bubbles(db_path, ["NU"], _CATALOG, max_bubbles=1)
    assert len(bubbles) == 1
    assert bubbles[0].token == "kpi:NIM"


def test_key_metric_bubbles_baseline_only_without_cache(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    bubbles = key_metric_bubbles(db_path, ["NU"], _CATALOG)
    assert [b.token for b in bubbles] == ["kpi:NIM", "kpi:NPL"]
    assert all(b.source == "tier" for b in bubbles)


def test_render_key_metrics_inner_html() -> None:
    bubbles = [
        KeyMetricBubble("kpi:NIM", "NIM", "tier", "Tier-1 thesis KPI"),
        KeyMetricBubble("kpi:Deposits", "Deposits", "llm", "low-cost funding"),
    ]
    html = render_key_metrics_inner(bubbles, ["NU", "MELI"])
    assert 'class="k-chip km-chip"' in html  # tier chip
    assert 'class="k-chip km-chip k-chip-accent"' in html  # llm chip (accent tone)
    assert 'data-km-token="kpi:NIM"' in html
    assert 'data-km-token="kpi:Deposits"' in html
    assert "Key metrics" in html and "NU, MELI" in html
    assert 'title="low-cost funding"' in html
    # No bubbles → empty string (the container collapses via :empty).
    assert render_key_metrics_inner([], ["NU"]) == ""


# ---------------------------------------------------------------------------
# build-side: vocabulary-validated suggestions + idempotent cache
# ---------------------------------------------------------------------------


def test_suggest_key_metrics_validates_against_vocabulary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vocab = [("kpi:NIM", "NIM"), ("fin:revenue", "revenue")]

    def fake_call(_prompt: str, **_kw: object) -> object:
        return [
            {"token": "kpi:NIM", "why": "core spread"},
            {"token": "kpi:NIM", "why": "duplicate"},
            {"token": "kpi:HALLUCINATED", "why": "not in vocab"},
            {"token": "fin:revenue", "why": "top line"},
            {"why": "missing token"},
        ]

    monkeypatch.setattr(km_build, "call_llm_structured", fake_call)
    out = suggest_key_metrics(
        ticker="NU", name="Nu", business_description="digital bank", vocabulary=vocab
    )
    # Dedup + hallucination drop + schema drop → only the two valid tokens, in order.
    assert [s.token for s in out] == ["kpi:NIM", "fin:revenue"]
    assert out[0].why == "core spread"


def test_suggest_key_metrics_empty_vocab_skips_the_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(_prompt: str, **_kw: object) -> object:
        raise AssertionError("LLM must not be called with an empty vocabulary")

    monkeypatch.setattr(km_build, "call_llm_structured", boom)
    assert (
        suggest_key_metrics(ticker="NU", name="Nu", business_description="x", vocabulary=[]) == []
    )


def test_extract_for_ticker_caches_and_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = tmp_path
    (repo_root / "data").mkdir()
    (repo_root / "data" / "portfolio.db").write_text("", encoding="utf-8")
    vocab = [("kpi:NIM", "NIM"), ("fin:revenue", "revenue")]

    def fake_vocab(_db: Path, _t: str) -> list[tuple[str, str]]:
        return vocab

    monkeypatch.setattr(km_build, "_vocabulary", fake_vocab)

    calls = {"n": 0}

    def fake_call(_prompt: str, **_kw: object) -> object:
        calls["n"] += 1
        return [{"token": "kpi:NIM", "why": "core spread"}]

    monkeypatch.setattr(km_build, "call_llm_structured", fake_call)
    conn = sqlite3.connect(repo_root / "data" / "portfolio.db")
    try:
        first = extract_for_ticker("NU", repo_root, conn)
        assert [m["token"] for m in first.metrics] == ["kpi:NIM"]
        assert calls["n"] == 1
        cache = repo_root / "data" / "key_metrics" / "NU.json"
        assert cache.exists()

        # Same inputs → cache hit, no second LLM call.
        second = extract_for_ticker("NU", repo_root, conn)
        assert calls["n"] == 1
        assert second.metrics == first.metrics

        # --refresh forces a re-call.
        extract_for_ticker("NU", repo_root, conn, refresh=True)
        assert calls["n"] == 2
    finally:
        conn.close()


def test_extract_for_ticker_degrades_on_parse_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from llm.structured import StructuredParseError

    repo_root = tmp_path
    (repo_root / "data").mkdir()
    (repo_root / "data" / "portfolio.db").write_text("", encoding="utf-8")

    def fake_vocab(_db: Path, _t: str) -> list[tuple[str, str]]:
        return [("kpi:NIM", "NIM")]

    monkeypatch.setattr(km_build, "_vocabulary", fake_vocab)

    def boom(_prompt: str, **_kw: object) -> object:
        raise StructuredParseError("bad json twice", raw_head="<<<")

    monkeypatch.setattr(km_build, "call_llm_structured", boom)
    conn = sqlite3.connect(repo_root / "data" / "portfolio.db")
    try:
        result = extract_for_ticker("NU", repo_root, conn)
    finally:
        conn.close()
    assert isinstance(result, KeyMetricsResult)
    assert result.metrics == []
    assert result.skipped_reason is not None
    assert "parse failure" in result.skipped_reason
    # The skip is cached so the degraded state is visible, not re-attempted.
    assert (repo_root / "data" / "key_metrics" / "NU.json").exists()


def test_extract_for_ticker_skips_when_no_vocabulary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = tmp_path
    (repo_root / "data").mkdir()
    (repo_root / "data" / "portfolio.db").write_text("", encoding="utf-8")

    def fake_vocab(_db: Path, _t: str) -> list[tuple[str, str]]:
        return []

    monkeypatch.setattr(km_build, "_vocabulary", fake_vocab)

    def boom(_prompt: str, **_kw: object) -> object:
        raise AssertionError("must not call the LLM with no vocabulary")

    monkeypatch.setattr(km_build, "call_llm_structured", boom)
    conn = sqlite3.connect(repo_root / "data" / "portfolio.db")
    try:
        result = extract_for_ticker("NU", repo_root, conn)
    finally:
        conn.close()
    assert result.metrics == []
    assert result.skipped_reason is not None and "no extracted metrics" in result.skipped_reason
