"""Shared held-candidate and portfolio diversification math."""

from __future__ import annotations

import json
import math
import sqlite3
import sys
from collections.abc import Callable
from contextlib import closing
from datetime import date, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from allocation.candidate_fit import BookContext, compute_candidate_fit

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


def test_held_weight_round_trips_cache(
    tmp_path: Path, migrated_db: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    import candidate_fit_cache as cfc
    from allocation.candidate_fit import CandidateFit

    repo = tmp_path
    db_path = migrated_db(tmp_path / "data/portfolio.db")
    fit = CandidateFit(ticker="EQTY", factors=[], fit=1.0, why="", partial=False, held_weight=0.031)

    def book(*_args: object, **_kwargs: object) -> BookContext:
        return BookContext(weights={})

    def fits(*_args: object, **_kwargs: object) -> dict[str, CandidateFit]:
        return {"EQTY": fit}

    monkeypatch.setattr(cfc, "assemble_book_context", book)
    monkeypatch.setattr(cfc, "compute_candidate_fit", fits)
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute(
            "INSERT INTO tracked_companies(ticker,name,list_type,instrument_type) "
            "VALUES('EQTY','Synthetic company','evaluation','equity')"
        )
        conn.commit()
        cfc.materialize_candidate_fit(conn, repo, db_path=db_path)
    back = cfc.read_materialized_candidate_fit(repo)["EQTY"]
    assert back.held_weight == pytest.approx(0.031)
