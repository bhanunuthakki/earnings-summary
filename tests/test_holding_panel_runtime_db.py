"""The Holding tab renders against the cockpit's resolved runtime DB path.

Owner-approved M2 behavior update (audit ``performance-serving`` F11, contract
VAL-SERVE-006): ``/api/panel/holding`` threads the application's resolved
runtime database path into the holding renderer so the fragment's DB readers
(the 5-minute rereads, position lifecycle, attribution, you-said strip, and
disclosure strip) read the same database every other cockpit surface uses.
The request path must never open or create the forbidden checkout-default
``data/portfolio.db`` location: on a Mac dev host that path trips the
checkout-DB guard and 500s the route against a snapshot-restored bench DB.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import comments_server
import pytest

import sqlite_runtime
from pipeline.ticker_command_center import render_holding_fragment

_RUNTIME_MARK = "runtime-db-path-marker"
_REPO_MARK = "repo-checkout-default-marker"


def _seed_holding_db(db_path: Path, *, rationale: str, interpretation: str) -> None:
    """Minimal substrate for the holding fragment's DB readers.

    Only the two readers with per-DB visible content (the you-said strip and
    the disclosure strip) carry distinguishing markers; every other reader
    degrades to its documented empty state on a thin schema, which is exactly
    what the live bench DB exercises for tickers without decision history.
    The ``decisions`` table carries the post-0130 ``decided_by`` /
    ``rationale_excerpt`` columns the you-said loader requires — a pre-0130
    schema reads as "no decision on file" and would hide the marker.
    """
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE tracked_companies (ticker TEXT, name TEXT, list_type TEXT,
                                        archived_at TIMESTAMP);
        CREATE TABLE decisions (ticker TEXT, recommendation_kind TEXT,
                                recommendation_value REAL, conviction TEXT,
                                made_at TEXT, outcome_label TEXT, decided_by TEXT,
                                rationale_excerpt TEXT, decision_conditions TEXT);
        CREATE TABLE disclosure_events (
            id INTEGER PRIMARY KEY, ticker TEXT, event_type TEXT, form TEXT,
            fiscal_year INTEGER, fiscal_period TEXT, canonical_id TEXT,
            subject TEXT, subject_label TEXT, source_doc_id INTEGER,
            evidence_quote TEXT, materiality REAL, verdict TEXT,
            interpretation_md TEXT, status TEXT, created_at TEXT,
            thesis_materiality TEXT, thesis_materiality_rationale TEXT,
            thesis_materiality_judged_at TEXT);
        INSERT INTO tracked_companies VALUES ('NU', 'Nu Holdings', 'portfolio', NULL);
        """
    )
    conn.execute(
        "INSERT INTO decisions VALUES ('NU', 'buy', 10.0, 'high', "
        "'2026-05-15T00:00:00', 'pending', 'owner', ?, NULL)",
        (rationale,),
    )
    conn.execute(
        "INSERT INTO disclosure_events VALUES (1, 'NU', 'item_reworded', '10-Q', "
        "2026, 'Q2', 'risk_factors', 'credit quality', 'Credit quality', 42, "
        "'Delinquency formation increased in the youngest vintages.', 0.92, "
        "'substantive', ?, 'new', '2026-05-01', 'restricts_measurement', "
        "'Vintage-level delinquency feeds the NPL break rule.', '2026-05-01')",
        (interpretation,),
    )
    conn.commit()
    conn.close()


def _runtime_db(tmp_path: Path) -> Path:
    runtime_db = tmp_path / "runtime" / "portfolio_runtime.db"
    runtime_db.parent.mkdir(parents=True, exist_ok=True)
    _seed_holding_db(
        runtime_db,
        rationale=f"Runtime owner rationale {_RUNTIME_MARK}",
        interpretation=f"Runtime interpretation {_RUNTIME_MARK}",
    )
    return runtime_db


def test_holding_route_reads_the_resolved_runtime_db(tmp_path: Path) -> None:
    """The route hands the renderer the app's resolved DB path. With a decoy
    checkout-default ``data/portfolio.db`` present, the fragment's you-said and
    disclosure readers still reflect the runtime DB, never the decoy."""
    repo = tmp_path
    (repo / "data").mkdir()
    _seed_holding_db(
        repo / "data" / "portfolio.db",
        rationale=f"Decoy checkout-local rationale {_REPO_MARK}",
        interpretation=f"Decoy checkout-local interpretation {_REPO_MARK}",
    )
    runtime_db = _runtime_db(tmp_path)

    app = comments_server.create_app(repo, db_path=runtime_db)
    resp = app.test_client().get("/api/panel/holding?ticker=NU")

    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert _RUNTIME_MARK in body
    assert _REPO_MARK not in body


def test_holding_route_never_opens_the_checkout_default_db(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without a runtime-threaded path the renderer falls back to the
    checkout-default location — the exact attempt that 500s against the bench
    DB on a Mac. The holding request must open only the resolved runtime DB;
    the checkout-default file is never opened or created."""
    runtime_db = _runtime_db(tmp_path)
    app = comments_server.create_app(tmp_path, db_path=runtime_db)

    opened: list[str] = []
    original_guard = sqlite_runtime.reject_forbidden_mac_checkout_database

    def record_and_guard(path: str | os.PathLike[str]) -> None:
        opened.append(os.fspath(path))
        original_guard(path)

    monkeypatch.setattr(sqlite_runtime, "reject_forbidden_mac_checkout_database", record_and_guard)

    resp = app.test_client().get("/api/panel/holding?ticker=NU")

    assert resp.status_code == 200
    assert _RUNTIME_MARK in resp.get_data(as_text=True)
    resolved_opened = {str(Path(path).resolve()) for path in opened}
    assert str(runtime_db.resolve()) in resolved_opened
    checkout_default = str((tmp_path / "data" / "portfolio.db").resolve())
    assert checkout_default not in resolved_opened
    assert not (tmp_path / "data" / "portfolio.db").exists()


def test_render_holding_fragment_reads_explicit_db_path(tmp_path: Path) -> None:
    """The renderer seam itself: an explicit ``db_path`` wins over a present
    checkout-default ``data/portfolio.db`` for every DB-reading section."""
    (tmp_path / "data").mkdir()
    _seed_holding_db(
        tmp_path / "data" / "portfolio.db",
        rationale=f"Decoy checkout-local rationale {_REPO_MARK}",
        interpretation=f"Decoy checkout-local interpretation {_REPO_MARK}",
    )
    runtime_db = _runtime_db(tmp_path)

    frag = render_holding_fragment(tmp_path, "NU", db_path=runtime_db)

    assert _RUNTIME_MARK in frag
    assert _REPO_MARK not in frag
