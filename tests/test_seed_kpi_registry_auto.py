"""Tests for the ``--auto`` mode of scratch/seed_kpi_registry.py.

Covers the two correctness mechanisms and the trust gate (plan §4-§8):

  * name-alignment: a proposed kpi_name not in the fireable catalog is dropped,
    never written (silent-no-fire prevention).
  * adverse-direction: a higher-is-better breaker is written 'below'; a
    lower-is-better breaker is written 'above' (the #1 risk).
  * polarity conflict (table vs LLM) -> review, not auto-written.
  * confidence below the gate -> review.
  * ungrounded breaker -> review (a breaker that could never escalate).
  * idempotent re-run: llm_auto rows overwritten; curated rows preserved;
    --only-new is purely additive.
  * end-to-end: auto-seed NU NIM, then the real KpiInflectionTrigger fires
    AND escalates on the seeded breaker.

``seed_kpi_registry.call_llm`` is mocked so the suite runs offline. The DB
schema is built by hand (no ``documents`` table) so ``load_kpi_series`` takes
its max(id) fallback — the proven pattern from test_trigger_kpi_inflection.py.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from dataclasses import asdict
from pathlib import Path
from typing import cast

import pytest
import yaml

# scratch/ lives at <repo_root>/scratch/; src/ is already on sys.path via the
# tests' conftest. Insert scratch/ so ``seed_kpi_registry`` imports at runtime.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "scratch"))

from seed_kpi_registry import (  # noqa: E402
    AutoSeedSummary,
    auto_seed_ticker,
    main,
)

from tests.kpi_semantic_support import admit_all_kpi_facts  # noqa: E402
from triggers import KpiInflectionTrigger, UserStateContext  # noqa: E402
from user_state import registry  # noqa: E402

# 8 flat quarters ~18 then a 4-quarter decline to 16.4 — a recent down
# inflection that crosses 'below 17'. Pinned from test_trigger_kpi_inflection.py.
_VALUES_RECENT_DOWN: list[float] = [
    18.0,
    18.1,
    17.9,
    18.0,
    18.1,
    17.9,
    18.0,
    18.0,
    17.4,
    17.0,
    16.7,
    16.4,
]

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
# Schema + seed helpers (no documents table -> load_kpi_series max(id) path)
# ---------------------------------------------------------------------------


def _create_schema(conn: sqlite3.Connection) -> None:
    _ = conn.execute(
        "CREATE TABLE kpi_definitions ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "ticker TEXT NOT NULL, name TEXT NOT NULL, unit TEXT)"
    )
    _ = conn.execute(
        "CREATE TABLE kpi_facts ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, ticker TEXT NOT NULL, "
        "period_end TEXT NOT NULL, fiscal_period_type TEXT NOT NULL, "
        "kpi_definition_id INTEGER NOT NULL, value REAL NOT NULL, unit TEXT, "
        "source_doc_id INTEGER)"
    )
    _ = conn.execute(
        "CREATE TABLE user_kpi_registry ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "user_id TEXT NOT NULL DEFAULT 'bhanu', ticker TEXT NOT NULL, "
        "kpi_name TEXT NOT NULL, threshold_direction TEXT, threshold_value REAL, "
        "is_thesis_breaker INTEGER NOT NULL DEFAULT 0, scaffold_source TEXT, "
        "notes TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
    )
    _ = conn.execute(
        "CREATE UNIQUE INDEX uq_user_kpi_registry_user_ticker_kpi "
        "ON user_kpi_registry (user_id, ticker, kpi_name)"
    )
    _ = conn.execute(
        "CREATE TABLE llm_artifacts ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, ticker TEXT, "
        "scope TEXT NOT NULL DEFAULT 'ticker', purpose TEXT NOT NULL, "
        "fiscal_period TEXT, content_md TEXT, content_json TEXT, "
        "input_sha256 TEXT NOT NULL, output_sha256 TEXT, model TEXT, "
        "prompt_version TEXT NOT NULL DEFAULT 'v1', generated_at TEXT NOT NULL, "
        "expires_at TEXT, superseded_by_id INTEGER, dirty INTEGER NOT NULL DEFAULT 0, "
        "dirty_reason TEXT, source_doc_ids TEXT, parent_artifact_ids TEXT, "
        "llm_call_id INTEGER)"
    )
    conn.commit()


def _seed_kpi_facts(
    db_path: Path,
    *,
    ticker: str,
    kpi_name: str,
    values: list[float],
    unit: str = "%",
) -> None:
    """One kpi_definitions row + a quarterly kpi_facts series for that KPI."""
    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.execute(
            "INSERT INTO kpi_definitions (ticker, name, unit) VALUES (?, ?, ?)",
            (ticker, kpi_name, unit),
        )
        kd_id = int(cur.lastrowid or 0)
        for period_end, fpt, value in _quarter_series(values):
            _ = conn.execute(
                "INSERT INTO kpi_facts (ticker, period_end, fiscal_period_type, "
                "kpi_definition_id, value, unit, source_doc_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (ticker, period_end, fpt, kd_id, value, unit, 1),
            )
        admit_all_kpi_facts(conn)
        conn.commit()
    finally:
        conn.close()


def _write_holdings_json(repo_root: Path, ticker: str) -> None:
    holdings_dir = repo_root / "micro_thesis" / "holdings"
    holdings_dir.mkdir(parents=True, exist_ok=True)
    payload: dict[str, object] = {
        "thesis": "Deposit franchise compounding via NIM and customer growth.",
        "key_driver": "NIM trajectory",
        "tier_1_kpis": [{"name": "NIM", "break_condition": "<17%"}],
    }
    (holdings_dir / f"{ticker.upper()}.json").write_text(json.dumps(payload), encoding="utf-8")


def _write_10k_json(fmp_dir: Path, ticker: str, *, year: int = 2024) -> None:
    fmp_dir.mkdir(parents=True, exist_ok=True)
    payload: dict[str, object] = {
        "symbol": ticker.upper(),
        "year": str(year),
        "Management of financial risks": [
            {"Overview": ["NIM below 17% would force a thesis revision."]}
        ],
    }
    (fmp_dir / f"{ticker.upper()}_form_10k_{year}.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "portfolio.db"
    conn = sqlite3.connect(str(path))
    try:
        _create_schema(conn)
    finally:
        conn.close()
    # build_alert resolves db.DB_PATH for its artifact-store cache; point it here.
    monkeypatch.setattr("db.DB_PATH", str(path))
    return path


@pytest.fixture
def repo_root(tmp_path: Path) -> Path:
    (tmp_path / "micro_thesis" / "holdings").mkdir(parents=True, exist_ok=True)
    return tmp_path


@pytest.fixture
def fmp_dir(tmp_path: Path) -> Path:
    p = tmp_path / "data" / "historical" / "fmp"
    p.mkdir(parents=True, exist_ok=True)
    return p


@pytest.fixture
def db_without_registry(tmp_path: Path) -> Path:
    """A DB with the rest of the schema but NO user_kpi_registry table — the
    unmigrated (alembic rev < 0060) case the --auto precondition guards.
    Dropping the table also drops its unique index (sqlite)."""
    path = tmp_path / "portfolio.db"
    conn = sqlite3.connect(str(path))
    try:
        _create_schema(conn)
        _ = conn.execute("DROP TABLE user_kpi_registry")
        conn.commit()
    finally:
        conn.close()
    return path


class _StatefulLLM:
    """Cycles canned responses; reuses the last once exhausted."""

    def __init__(self, responses: list[str]) -> None:
        self.responses = responses
        self.call_count = 0

    def __call__(self, prompt: str, **_kwargs: object) -> str:
        idx = min(self.call_count, len(self.responses) - 1)
        self.call_count += 1
        return self.responses[idx]


def _auto_json(proposals: list[dict[str, object]]) -> str:
    return json.dumps(proposals)


def _prop(
    *,
    kpi_name: str,
    polarity: str = "higher_is_better",
    is_thesis_breaker: bool = True,
    threshold_value: float | None = 17.0,
    threshold_grounded: bool = True,
    confidence: float = 0.9,
) -> dict[str, object]:
    return {
        "kpi_name": kpi_name,
        "polarity": polarity,
        "is_thesis_breaker": is_thesis_breaker,
        "threshold_value": threshold_value,
        "threshold_grounded": threshold_grounded,
        "grounding_citation": "Item 1A Risk Factors",
        "confidence": confidence,
        "rationale": "Grounded in the 10-K risk section.",
    }


def _run(
    db_path: Path,
    repo_root: Path,
    fmp_dir: Path,
    tmp_path: Path,
    *,
    ticker: str = "NU",
    only_new: bool = False,
) -> AutoSeedSummary:
    return auto_seed_ticker(
        ticker=ticker,
        db_path=db_path,
        fmp_dir=fmp_dir,
        repo_root=repo_root,
        review_out=tmp_path / "review" / f"{ticker}.yaml",
        only_new=only_new,
    )


# ---------------------------------------------------------------------------
# Precondition — unmigrated DB (no user_kpi_registry table)
# ---------------------------------------------------------------------------


def test_auto_missing_registry_table_fails_clean_no_llm(
    db_without_registry: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """--auto against a DB without user_kpi_registry (rev < 0060) fails with a
    clear, actionable error and a non-zero exit — without spending an LLM call
    or raising an uncaught traceback. The precondition check in _run_auto fires
    before the ticker loop (and therefore before any Opus call)."""
    llm = _StatefulLLM(["[]"])
    monkeypatch.setattr("seed_kpi_registry.call_llm", llm)

    rc = main(["--auto", "--ticker", "NU", "--db-path", str(db_without_registry)])

    assert rc != 0
    assert llm.call_count == 0
    err = capsys.readouterr().err
    assert "user_kpi_registry table not found" in err
    assert "alembic upgrade head" in err


# ---------------------------------------------------------------------------
# Correctness #1 — name alignment
# ---------------------------------------------------------------------------


def test_name_not_in_catalog_is_dropped_not_written(
    db_path: Path,
    repo_root: Path,
    fmp_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A proposal whose kpi_name isn't a fireable kpi_definitions.name is
    dropped (never written); an in-catalog name is written."""
    _seed_kpi_facts(db_path, ticker="NU", kpi_name="NIM", values=_VALUES_RECENT_DOWN)
    _write_holdings_json(repo_root, "NU")
    _write_10k_json(fmp_dir, "NU")
    # "Net Interest Margin" is NOT in the catalog (only "NIM" has facts).
    monkeypatch.setattr(
        "seed_kpi_registry.call_llm",
        _StatefulLLM([_auto_json([_prop(kpi_name="Net Interest Margin"), _prop(kpi_name="NIM")])]),
    )

    summary = _run(db_path, repo_root, fmp_dir, tmp_path)

    assert summary.dropped == 1
    assert summary.written == 1
    rows = registry.list_kpis(ticker="NU", db_path=db_path)
    assert [r.kpi_name for r in rows] == ["NIM"]


# ---------------------------------------------------------------------------
# Correctness #2 — adverse-direction breaker thresholds
# ---------------------------------------------------------------------------


def test_higher_is_better_breaker_written_below(
    db_path: Path,
    repo_root: Path,
    fmp_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _seed_kpi_facts(db_path, ticker="NU", kpi_name="NIM", values=_VALUES_RECENT_DOWN)
    _write_holdings_json(repo_root, "NU")
    _write_10k_json(fmp_dir, "NU")
    monkeypatch.setattr(
        "seed_kpi_registry.call_llm",
        _StatefulLLM(
            [_auto_json([_prop(kpi_name="NIM", polarity="higher_is_better", threshold_value=17.0)])]
        ),
    )

    _ = _run(db_path, repo_root, fmp_dir, tmp_path)

    row = registry.list_kpis(ticker="NU", db_path=db_path)[0]
    assert row.kpi_name == "NIM"
    assert row.threshold_direction == "below"  # adverse of higher-is-better
    assert row.threshold_value == 17.0
    assert row.is_thesis_breaker is True
    assert row.scaffold_source == "llm_auto"


def test_lower_is_better_breaker_written_above(
    db_path: Path,
    repo_root: Path,
    fmp_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _seed_kpi_facts(db_path, ticker="NU", kpi_name="Monthly churn", values=[1.0] * 12, unit="%")
    _write_holdings_json(repo_root, "NU")
    _write_10k_json(fmp_dir, "NU")
    monkeypatch.setattr(
        "seed_kpi_registry.call_llm",
        _StatefulLLM(
            [
                _auto_json(
                    [
                        _prop(
                            kpi_name="Monthly churn",
                            polarity="lower_is_better",
                            threshold_value=5.0,
                        )
                    ]
                )
            ]
        ),
    )

    _ = _run(db_path, repo_root, fmp_dir, tmp_path)

    row = registry.list_kpis(ticker="NU", db_path=db_path)[0]
    assert row.kpi_name == "Monthly churn"
    assert row.threshold_direction == "above"  # adverse of lower-is-better
    assert row.is_thesis_breaker is True


# ---------------------------------------------------------------------------
# Trust gate — polarity conflict / confidence / ungrounded breaker -> review
# ---------------------------------------------------------------------------


def _review_names(review_path: Path) -> list[str]:
    parsed = cast("dict[str, object]", yaml.safe_load(review_path.read_text(encoding="utf-8")))
    proposals = cast("list[dict[str, object]]", parsed["proposals"])
    assert all(p["status"] == "proposed" for p in proposals)
    return [cast("str", p["kpi_name"]) for p in proposals]


def test_polarity_conflict_routes_to_review(
    db_path: Path,
    repo_root: Path,
    fmp_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Table says NIM is higher-is-better; LLM says lower -> never auto-written."""
    _seed_kpi_facts(db_path, ticker="NU", kpi_name="NIM", values=_VALUES_RECENT_DOWN)
    _write_holdings_json(repo_root, "NU")
    _write_10k_json(fmp_dir, "NU")
    monkeypatch.setattr(
        "seed_kpi_registry.call_llm",
        _StatefulLLM([_auto_json([_prop(kpi_name="NIM", polarity="lower_is_better")])]),
    )

    summary = _run(db_path, repo_root, fmp_dir, tmp_path)

    assert summary.written == 0
    assert summary.reviewed == 1
    assert registry.list_kpis(ticker="NU", db_path=db_path) == []
    review_path = tmp_path / "review" / "NU.yaml"
    assert review_path.exists()
    assert _review_names(review_path) == ["NIM"]


def test_low_confidence_routes_to_review(
    db_path: Path,
    repo_root: Path,
    fmp_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A KPI with no table entry and confidence below the gate -> review."""
    _seed_kpi_facts(
        db_path, ticker="NU", kpi_name="Widget volume", values=[100.0] * 12, unit="count"
    )
    _write_holdings_json(repo_root, "NU")
    _write_10k_json(fmp_dir, "NU")
    monkeypatch.setattr(
        "seed_kpi_registry.call_llm",
        _StatefulLLM(
            [
                _auto_json(
                    [
                        _prop(
                            kpi_name="Widget volume",
                            is_thesis_breaker=False,
                            threshold_value=None,
                            threshold_grounded=False,
                            confidence=0.5,
                        )
                    ]
                )
            ]
        ),
    )

    summary = _run(db_path, repo_root, fmp_dir, tmp_path)

    assert summary.written == 0
    assert summary.reviewed == 1
    assert registry.list_kpis(ticker="NU", db_path=db_path) == []


def test_ungrounded_breaker_routes_to_review(
    db_path: Path,
    repo_root: Path,
    fmp_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A breaker without a grounded, non-null threshold can never escalate ->
    routed to review, not written as a breaker."""
    _seed_kpi_facts(db_path, ticker="NU", kpi_name="NIM", values=_VALUES_RECENT_DOWN)
    _write_holdings_json(repo_root, "NU")
    _write_10k_json(fmp_dir, "NU")
    monkeypatch.setattr(
        "seed_kpi_registry.call_llm",
        _StatefulLLM(
            [
                _auto_json(
                    [
                        _prop(
                            kpi_name="NIM",
                            is_thesis_breaker=True,
                            threshold_value=None,
                            threshold_grounded=False,
                        )
                    ]
                )
            ]
        ),
    )

    summary = _run(db_path, repo_root, fmp_dir, tmp_path)

    assert summary.written == 0
    assert summary.reviewed == 1
    assert registry.list_kpis(ticker="NU", db_path=db_path) == []


# ---------------------------------------------------------------------------
# Idempotency / re-run
# ---------------------------------------------------------------------------


def test_rerun_overwrites_llm_auto_but_preserves_curated(
    db_path: Path,
    repo_root: Path,
    fmp_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """An existing llm_auto row is refreshed; a manual row on the same key is
    preserved untouched."""
    _seed_kpi_facts(db_path, ticker="NU", kpi_name="NIM", values=_VALUES_RECENT_DOWN)
    _seed_kpi_facts(
        db_path, ticker="NU", kpi_name="Total customers", values=[100.0] * 12, unit="count"
    )
    _write_holdings_json(repo_root, "NU")
    _write_10k_json(fmp_dir, "NU")

    # Pre-existing rows: NIM auto-written previously (threshold 18), Total
    # customers hand-curated (threshold 100).
    auto_before = registry.upsert_kpi(
        ticker="NU",
        kpi_name="NIM",
        threshold_direction="below",
        threshold_value=18.0,
        is_thesis_breaker=True,
        scaffold_source="llm_auto",
        db_path=db_path,
    )
    _ = registry.upsert_kpi(
        ticker="NU",
        kpi_name="Total customers",
        threshold_direction="below",
        threshold_value=100.0,
        is_thesis_breaker=True,
        scaffold_source="manual",
        db_path=db_path,
    )

    monkeypatch.setattr(
        "seed_kpi_registry.call_llm",
        _StatefulLLM(
            [
                _auto_json(
                    [
                        _prop(kpi_name="NIM", threshold_value=17.0),
                        _prop(kpi_name="Total customers", threshold_value=90.0),
                    ]
                )
            ]
        ),
    )

    summary = _run(db_path, repo_root, fmp_dir, tmp_path)

    assert summary.overwritten == 1
    assert summary.preserved == 1
    assert summary.written == 0

    by_name = {r.kpi_name: r for r in registry.list_kpis(ticker="NU", db_path=db_path)}
    # NIM refreshed in place (same row id, new threshold), still llm_auto.
    assert by_name["NIM"].id == auto_before.id
    assert by_name["NIM"].threshold_value == 17.0
    assert by_name["NIM"].scaffold_source == "llm_auto"
    # Curated row untouched.
    assert by_name["Total customers"].threshold_value == 100.0
    assert by_name["Total customers"].scaffold_source == "manual"


def test_only_new_skips_existing_llm_auto(
    db_path: Path,
    repo_root: Path,
    fmp_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """--only-new leaves an existing llm_auto row untouched (purely additive)."""
    _seed_kpi_facts(db_path, ticker="NU", kpi_name="NIM", values=_VALUES_RECENT_DOWN)
    _write_holdings_json(repo_root, "NU")
    _write_10k_json(fmp_dir, "NU")
    _ = registry.upsert_kpi(
        ticker="NU",
        kpi_name="NIM",
        threshold_direction="below",
        threshold_value=18.0,
        is_thesis_breaker=True,
        scaffold_source="llm_auto",
        db_path=db_path,
    )
    monkeypatch.setattr(
        "seed_kpi_registry.call_llm",
        _StatefulLLM([_auto_json([_prop(kpi_name="NIM", threshold_value=17.0)])]),
    )

    summary = _run(db_path, repo_root, fmp_dir, tmp_path, only_new=True)

    assert summary.skipped_only_new == 1
    assert summary.overwritten == 0
    row = registry.list_kpis(ticker="NU", db_path=db_path)[0]
    assert row.threshold_value == 18.0  # unchanged


# ---------------------------------------------------------------------------
# End-to-end — auto-seed then the real trigger fires + escalates
# ---------------------------------------------------------------------------


def test_end_to_end_auto_seed_makes_trigger_escalate(
    db_path: Path,
    repo_root: Path,
    fmp_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Auto-seed NU NIM as a grounded higher-is-better breaker, then drive the
    real KpiInflectionTrigger: it must detect the down-inflection, see the
    'below 17' cross, and escalate (THESIS-BREAKER CROSSED + the 3 actions)."""
    _seed_kpi_facts(db_path, ticker="NU", kpi_name="NIM", values=_VALUES_RECENT_DOWN)
    _write_holdings_json(repo_root, "NU")
    _write_10k_json(fmp_dir, "NU")
    monkeypatch.setattr(
        "seed_kpi_registry.call_llm",
        _StatefulLLM([_auto_json([_prop(kpi_name="NIM", threshold_value=17.0, confidence=0.95)])]),
    )

    _ = _run(db_path, repo_root, fmp_dir, tmp_path)

    seeded = registry.list_kpis(ticker="NU", db_path=db_path)[0]
    assert seeded.threshold_direction == "below"
    assert seeded.is_thesis_breaker is True

    # The trigger's optional context line is best-effort; force it down so the
    # deterministic escalated memo is what we assert on.
    def _raise_llm(*_a: object, **_k: object) -> str:
        raise RuntimeError("LLM down for test")

    monkeypatch.setattr("triggers.kpi_inflection.call_llm", _raise_llm)

    trigger = KpiInflectionTrigger()
    conn = sqlite3.connect(str(db_path))
    try:
        candidates = trigger.scan("NU", conn)
    finally:
        conn.close()
    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.evidence["threshold_crossed"] is True

    rows = registry.list_kpis(user_id="bhanu", ticker="NU", db_path=db_path)
    state = UserStateContext(
        registered_kpis=[asdict(r) for r in rows],
        sizing_intents=[],
        recent_dismissed_signatures=set(),
    )
    assert trigger.should_fire(candidate, state) is True

    alert = trigger.build_alert(candidate, None)
    assert alert.memo_text is not None
    assert "THESIS-BREAKER CROSSED" in alert.memo_text

    actions = trigger.draft_actions(alert, candidate)
    assert sorted(a.action_kind for a in actions) == [
        "earnings_prep_append",
        "sizing_update",
        "thesis_update",
    ]
