"""ETF workup fragment + governed role synthesis + held-ETF ex-self fit.

- pipeline/etf_workup.render_etf_workup: per-block degradation (each block
  shows its missing state with the fix command), full render with seeded
  substrates, None for non-ETF tickers (the route 404s).
- etf_role_synthesis: schema round-trip; generate → persist; the sha
  pre-check SKIPS the LLM entirely when inputs are unchanged; --force
  regenerates; an invalid model payload degrades to an error status (hard
  stops would propagate — pinned by the is_hard_stop seam).
- candidate_fit: a held candidate scores against the EX-SELF book (the
  self-inclusion bias fix) and carries held_weight through the cache.
"""

from __future__ import annotations

import json
import math
import sqlite3
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import etf_role_synthesis as ers  # noqa: E402
from allocation.candidate_fit import BookContext, compute_candidate_fit  # noqa: E402
from instrument_store import upsert_etf_holdings, upsert_etf_profile  # noqa: E402
from models.instruments import EtfHolding, EtfProfile  # noqa: E402
from pipeline.etf_workup import render_etf_workup  # noqa: E402
from report.etf_models import EtfRoleSynthesis  # noqa: E402

_DDL = """
CREATE TABLE tracked_companies (
    ticker TEXT, list_type TEXT, instrument_type TEXT, archived_at TEXT
);
CREATE TABLE etf_profile (
    ticker TEXT PRIMARY KEY,
    name TEXT, issuer TEXT, expense_ratio REAL, aum_usd_m REAL,
    inception_date TEXT, asset_class TEXT, benchmark_index TEXT,
    domicile TEXT, listed_exchange TEXT, distribution_yield REAL,
    description TEXT, sector_label TEXT, nav REAL, price REAL,
    premium_discount_pct REAL,
    pe_ratio REAL, pb_ratio REAL, weighted_avg_mktcap_usd_m REAL,
    characteristics_as_of TEXT, characteristics_source TEXT,
    source TEXT NOT NULL DEFAULT 'fmp',
    profile_fetched_at TIMESTAMP NOT NULL
);
CREATE TABLE etf_holdings (
    ticker TEXT NOT NULL, as_of_date TEXT NOT NULL, constituent_ticker TEXT,
    name TEXT, weight_pct REAL, shares_held REAL, market_value_usd REAL,
    sector TEXT, asset_class TEXT, rank_position INTEGER, country TEXT,
    source TEXT NOT NULL DEFAULT 'fmp', fetched_at TIMESTAMP NOT NULL,
    PRIMARY KEY (ticker, as_of_date, constituent_ticker)
);
CREATE TABLE llm_artifacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT, scope TEXT NOT NULL DEFAULT 'ticker', purpose TEXT NOT NULL,
    fiscal_period TEXT, content_md TEXT, content_json TEXT,
    input_sha256 TEXT NOT NULL, output_sha256 TEXT, model TEXT,
    prompt_version TEXT NOT NULL DEFAULT 'v1',
    generated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    expires_at TIMESTAMP, superseded_by_id INTEGER,
    dirty INTEGER NOT NULL DEFAULT 0, dirty_reason TEXT,
    source_doc_ids TEXT, parent_artifact_ids TEXT, llm_call_id INTEGER
);
"""


@pytest.fixture
def env(tmp_path: Path) -> tuple[sqlite3.Connection, Path, Path]:
    db_path = tmp_path / "portfolio.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.executescript(_DDL)
    conn.execute("INSERT INTO tracked_companies VALUES ('AVDV', 'evaluation', 'etf', NULL)")
    conn.execute("INSERT INTO tracked_companies VALUES ('NU', 'portfolio', 'equity', NULL)")
    conn.commit()
    yield conn, tmp_path, db_path
    conn.close()


def _seed_full(conn: sqlite3.Connection, repo: Path, db_path: Path) -> None:
    upsert_etf_profile(
        conn,
        EtfProfile(
            ticker="AVDV",
            name="Avantis Intl Small Cap Value ETF",
            issuer="Avantis",
            expense_ratio=0.0036,
            aum_usd_m=9500.0,
            pb_ratio=1.1,
            characteristics_as_of=date(2026, 6, 30),
            characteristics_source="issuer:test",
            source="nport",
            profile_fetched_at=datetime(2026, 7, 10),
        ),
    )
    upsert_etf_holdings(
        conn,
        "AVDV",
        date(2026, 5, 31),
        [
            EtfHolding(
                ticker="AVDV",
                as_of_date=date(2026, 5, 31),
                constituent_ticker="NU",
                name="Nu",
                weight_pct=0.03,
                country="BR",
                source="nport",
                fetched_at=datetime(2026, 7, 10),
            ),
            EtfHolding(
                ticker="AVDV",
                as_of_date=date(2026, 5, 31),
                constituent_ticker="7203",
                name="Toyota",
                weight_pct=0.6,
                country="JP",
                source="nport",
                fetched_at=datetime(2026, 7, 10),
            ),
        ],
    )
    conn.commit()
    (repo / "data").mkdir(exist_ok=True)
    (repo / "data" / "portfolio_weights.json").write_text(
        json.dumps({"computed_at": "x", "weights": {"NU": 1.0}}), encoding="utf-8"
    )
    (repo / "data" / "etf_score.json").write_text(
        json.dumps(
            {
                "computed_at": "x",
                "scores": {},
                "loadings": {
                    "AVDV": [{"key": "value", "beta": 0.62, "r_squared": 0.41, "n_obs": 252}]
                },
                "whatif": {
                    "AVDV": {
                        "0.03": {
                            "vol_before_ann": 0.14,
                            "vol_after_ann": 0.138,
                            "sharpe_before": 0.82,
                            "sharpe_after": 0.831,
                            "sharpe_delta_bps": 110.0,
                            "prices_through": "2026-07-09",
                            "obs": 252,
                            "degraded": [],
                        }
                    }
                },
            }
        ),
        encoding="utf-8",
    )


_SYNTHESIS = {
    "role_summary": "AVDV would be the book's international small-cap value sleeve.",
    "what_it_adds": ["intl small-cap value exposure", "JP/EU developed exposure"],
    "overlap_caution": None,
    "verdict": "diversifier",
    "suggested_weight_band": "3-5%",
    "watch_items": ["value loading decay"],
}


def test_workup_renders_all_blocks(env: tuple[sqlite3.Connection, Path, Path]) -> None:
    conn, repo, db_path = env
    _seed_full(conn, repo, db_path)
    # Seed the role artifact directly through the store (no LLM).
    from llm_artifact_store import UpsertRequest, upsert

    upsert(
        UpsertRequest(
            ticker="AVDV",
            purpose="etf_role_synthesis",
            scope="etf",
            content_json=_SYNTHESIS,
            prompt_version="v1",
            cache_inputs=["seed"],
        ),
        db_path=db_path,
    )
    html = render_etf_workup(conn, repo, db_path, "avdv")
    assert html is not None
    assert "ER 36bp" in html and "P/B 1.1" in html
    assert "issuer:test as of 2026-06-30" in html
    assert "value" in html and "+0.62" in html
    assert "3% of the fund's weight is in names the book already owns" in html
    assert "JP 60%" in html and "intl 63%" in html
    assert "+0.820" in html and "+0.831" in html
    assert "+110bp" in html
    assert "international small-cap value sleeve" in html
    assert "diversifier" in html and "suggested 3-5%" in html


def test_workup_degrades_per_block(env: tuple[sqlite3.Connection, Path, Path]) -> None:
    conn, repo, db_path = env
    html = render_etf_workup(conn, repo, db_path, "AVDV")
    assert html is not None
    assert "No profile on file" in html and "fetch_etf_published_data.py --ticker AVDV" in html
    assert "No qualifying style legs" in html
    assert "weights cache empty" in html
    assert "Not precomputed yet" in html
    assert "Not generated yet" in html and "build_etf_workup.py --ticker AVDV" in html
    # Non-ETF → None (route 404s).
    assert render_etf_workup(conn, repo, db_path, "NU") is None
    assert render_etf_workup(conn, repo, db_path, "ZZZQ") is None


# ---------------------------------------------------------------------------
# Role synthesis generation (sha-gated, schema-validated)
# ---------------------------------------------------------------------------


def test_generate_persists_and_sha_gates(
    env: tuple[sqlite3.Connection, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    conn, repo, db_path = env
    _seed_full(conn, repo, db_path)
    calls: list[str] = []

    def fake_structured(prompt: str, **kw: object) -> object:
        calls.append(str(kw.get("purpose")))
        return dict(_SYNTHESIS)

    monkeypatch.setattr("llm.structured.call_llm_structured", fake_structured)
    artifact_id, status = ers.generate_role_synthesis(conn, repo, db_path, "AVDV")
    assert status == "generated" and artifact_id is not None
    assert calls == ["etf_role_synthesis"]
    decoded = ers.read_role_synthesis(db_path, "AVDV")
    assert decoded is not None and decoded.verdict == "diversifier"

    # Unchanged inputs → NO second LLM call at all.
    artifact_id2, status2 = ers.generate_role_synthesis(conn, repo, db_path, "AVDV")
    assert status2 == "unchanged" and artifact_id2 == artifact_id
    assert calls == ["etf_role_synthesis"]

    # --force regenerates.
    _id3, status3 = ers.generate_role_synthesis(conn, repo, db_path, "AVDV", force=True)
    assert status3 == "generated"
    assert calls == ["etf_role_synthesis", "etf_role_synthesis"]


def test_generate_invalid_output_degrades(
    env: tuple[sqlite3.Connection, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    conn, repo, db_path = env
    monkeypatch.setattr(
        "llm.structured.call_llm_structured",
        lambda *a, **k: {"role_summary": "x", "verdict": "moon"},  # bad enum
    )
    artifact_id, status = ers.generate_role_synthesis(conn, repo, db_path, "AVDV")
    assert artifact_id is None and status.startswith("error:")
    assert ers.read_role_synthesis(db_path, "AVDV") is None


def test_role_synthesis_schema_bounds() -> None:
    ok = EtfRoleSynthesis.model_validate(_SYNTHESIS)
    assert ok.suggested_weight_band == "3-5%"
    with pytest.raises(ValueError):
        EtfRoleSynthesis.model_validate({**_SYNTHESIS, "verdict": "yolo"})
    with pytest.raises(ValueError):
        EtfRoleSynthesis.model_validate({**_SYNTHESIS, "what_it_adds": ["a"] * 5})


# ---------------------------------------------------------------------------
# Held candidate → ex-self book (self-inclusion bias fix)
# ---------------------------------------------------------------------------

_START = date(2024, 1, 2)


def _chart(repo: Path, ticker: str, returns: list[float]) -> None:
    fmp = repo / "data" / "historical" / "fmp"
    fmp.mkdir(parents=True, exist_ok=True)
    price, rows = 100.0, [{"date": _START.isoformat(), "adjClose": 100.0}]
    for i, r in enumerate(returns, start=1):
        price *= math.exp(r)
        rows.append({"date": (_START + timedelta(days=i)).isoformat(), "adjClose": price})
    (fmp / f"{ticker}_price_chart_10y_div_adj.json").write_text(json.dumps(rows), encoding="utf-8")


def test_held_candidate_scores_ex_self(tmp_path: Path) -> None:
    """CAND is 50% of the book and the exact mirror of AAA + BBB. Against the
    SELF-INCLUDED book its correlation would be degenerate/positive; against
    the ex-self book it is -1 → the diversification leg reads the hedge
    (×1.20) and held_weight is carried."""
    market = [0.01 * math.sin(i / 5.0) + 0.0004 for i in range(200)]
    _chart(tmp_path, "AAA", market)
    _chart(tmp_path, "BBB", list(market))
    _chart(tmp_path, "CAND", [-m for m in market])
    book = BookContext(
        weights={"AAA": 0.25, "BBB": 0.25, "CAND": 0.5},
        sharpe=1.0,
        risk_free_annual=0.02,
    )
    fit = compute_candidate_fit(tmp_path, ["CAND"], book)["CAND"]
    assert fit.held_weight == pytest.approx(0.5)
    divers = {f.key: f for f in fit.factors}["divers"]
    assert not divers.missing
    assert divers.multiplier == pytest.approx(1.20)  # corr -1 vs the EX-SELF book
    # An unheld candidate keeps held_weight unset.
    unheld_book = BookContext(weights={"AAA": 0.5, "BBB": 0.5}, sharpe=1.0, risk_free_annual=0.02)
    assert compute_candidate_fit(tmp_path, ["CAND"], unheld_book)["CAND"].held_weight is None


def test_held_weight_round_trips_cache(tmp_path: Path) -> None:
    import candidate_fit_cache as cfc
    from allocation.candidate_fit import CandidateFit

    fit = CandidateFit(ticker="AVDV", factors=[], fit=1.0, why="", partial=False, held_weight=0.031)
    blob = cfc._fit_to_json(fit)  # pyright: ignore[reportPrivateUsage]
    back = cfc._fit_from_json("AVDV", blob)  # pyright: ignore[reportPrivateUsage]
    assert back is not None and back.held_weight == pytest.approx(0.031)
