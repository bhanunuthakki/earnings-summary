"""Tests for src/report/sections/p3_data.py — read-side data accessors for
the six P3 panels (macro sensitivity, strategic targets, customer
concentration, lease ladder, decision history, say-do verdicts).

Each accessor is best-effort: missing table → empty result. Synthetic
fixtures here build minimal schemas that match the production columns;
the production migrations (0017, 0040, 0045, 0046, 0047, 0053) are the
authoritative source of truth for the column shapes.
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from report.sections.p3_data import (  # noqa: E402
    load_customer_concentrations,
    load_decision_history,
    load_lease_ladder,
    load_macro_sensitivities,
    load_peer_comp,
    load_saydo_verdicts,
    load_strategic_targets,
)


def _empty_db(tmp_path: Path) -> Path:
    db = tmp_path / "portfolio.db"
    sqlite3.connect(str(db)).close()
    return db


def test_all_accessors_return_empty_on_missing_tables(tmp_path: Path) -> None:
    """Synthetic env with an empty DB — every accessor returns empty without
    raising. This is the contract every renderer relies on."""
    db = _empty_db(tmp_path)
    assert load_macro_sensitivities("META", db_path=db) == []
    assert load_strategic_targets("META", db_path=db) == []
    assert load_customer_concentrations("META", db_path=db) == []
    assert load_lease_ladder("META", db_path=db) == []
    hist = load_decision_history("META", db_path=db)
    assert hist.total == 0
    assert hist.rows == []
    assert load_saydo_verdicts("META", db_path=db) == []


def test_macro_sensitivities_ordering_by_abs_beta(tmp_path: Path) -> None:
    db = tmp_path / "portfolio.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        """
        CREATE TABLE macro_sensitivities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            series_id TEXT NOT NULL,
            beta REAL NOT NULL,
            r_squared REAL,
            lookback_window_days INTEGER NOT NULL,
            computed_at TIMESTAMP NOT NULL
        )
        """
    )
    rows = [
        ("GOOG", "fed_funds_rate", 0.3, 0.4, 90, "2026-05-01 00:00:00"),
        ("GOOG", "10y_treasury", -0.65, 0.31, 90, "2026-05-01 00:00:00"),
        ("GOOG", "usd_index", 0.15, 0.20, 90, "2026-05-01 00:00:00"),
    ]
    conn.executemany(
        "INSERT INTO macro_sensitivities (ticker, series_id, beta, r_squared, "
        "lookback_window_days, computed_at) VALUES (?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    conn.close()

    out = load_macro_sensitivities("GOOG", db_path=db)
    assert len(out) == 3
    # Largest |beta| first.
    assert out[0].series_id == "10y_treasury"
    assert out[1].series_id == "fed_funds_rate"
    assert out[2].series_id == "usd_index"


def test_strategic_targets_returns_typed_rows(tmp_path: Path) -> None:
    db = tmp_path / "portfolio.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        """
        CREATE TABLE strategic_targets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            deck_doc_id INTEGER,
            target_kind TEXT NOT NULL,
            target_value REAL,
            target_unit TEXT NOT NULL,
            target_period TEXT NOT NULL,
            target_currency TEXT,
            narrative_excerpt TEXT NOT NULL,
            extracted_at TIMESTAMP NOT NULL,
            confidence REAL NOT NULL DEFAULT 1.0
        )
        """
    )
    conn.execute(
        """
        INSERT INTO strategic_targets
          (ticker, target_kind, target_value, target_unit, target_period,
           target_currency, narrative_excerpt, extracted_at, confidence)
        VALUES ('GOOG', 'revenue', 500.0, 'USD_B', 'FY2027', 'USD',
                'targeting $500B revenue by FY27',
                '2026-05-01 00:00:00', 0.9)
        """
    )
    conn.commit()
    conn.close()

    out = load_strategic_targets("GOOG", db_path=db)
    assert len(out) == 1
    assert out[0].target_kind == "revenue"
    assert out[0].target_value == 500.0
    assert out[0].target_period == "FY2027"


def test_customer_concentrations_filters_below_5pct(tmp_path: Path) -> None:
    """The audit's threshold for "material" concentration is 5%. Sub-5% rows
    are dropped so the renderer surfaces only the meaningful risk lines."""
    db = tmp_path / "portfolio.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        """
        CREATE TABLE customer_concentrations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            fiscal_period TEXT NOT NULL,
            fiscal_period_type TEXT NOT NULL,
            customer_label TEXT NOT NULL,
            customer_entity_id INTEGER,
            pct_of_revenue REAL NOT NULL,
            revenue_amount NUMERIC,
            revenue_currency TEXT,
            source_doc_id INTEGER,
            source_excerpt TEXT,
            extracted_at TIMESTAMP NOT NULL
        )
        """
    )
    conn.executemany(
        "INSERT INTO customer_concentrations (ticker, fiscal_period, "
        "fiscal_period_type, customer_label, pct_of_revenue, extracted_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [
            ("RBRK", "2025", "FY", "Customer A", 0.22, "2026-05-01"),
            ("RBRK", "2025", "FY", "Customer B", 0.15, "2026-05-01"),
            ("RBRK", "2025", "FY", "Customer Z", 0.03, "2026-05-01"),  # below cutoff
        ],
    )
    conn.commit()
    conn.close()

    out = load_customer_concentrations("RBRK", db_path=db)
    assert len(out) == 2  # Customer Z dropped
    assert all(r.pct_of_revenue >= 0.05 for r in out)
    # Highest pct first.
    assert out[0].customer_label == "Customer A"


def test_lease_ladder_orders_by_canonical_year_buckets(tmp_path: Path) -> None:
    db = tmp_path / "portfolio.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        """
        CREATE TABLE lease_commitments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            fiscal_year INTEGER NOT NULL,
            as_of_date DATE NOT NULL,
            filing_doc_id INTEGER,
            lease_type TEXT NOT NULL,
            ladder_year TEXT NOT NULL,
            ladder_calendar_year INTEGER,
            amount NUMERIC NOT NULL,
            currency TEXT NOT NULL DEFAULT 'USD',
            unit TEXT NOT NULL DEFAULT 'millions',
            source_section_key TEXT,
            extracted_at TIMESTAMP NOT NULL
        )
        """
    )
    conn.executemany(
        "INSERT INTO lease_commitments (ticker, fiscal_year, as_of_date, "
        "lease_type, ladder_year, amount, currency, unit, extracted_at) "
        "VALUES ('AMZN', 2025, '2025-12-31', 'operating', ?, ?, 'USD', 'millions', '2026-05-01')",
        [
            ("Thereafter", 50_000),
            ("Y3", 9_000),
            ("Y1", 11_000),
            ("Y5", 7_000),
            ("Y2", 10_500),
            ("Y4", 8_000),
        ],
    )
    conn.commit()
    conn.close()

    out = load_lease_ladder("AMZN", db_path=db)
    assert [r.ladder_year for r in out] == ["Y1", "Y2", "Y3", "Y4", "Y5", "Thereafter"]


def test_decision_history_aggregates_and_computes_win_rate(tmp_path: Path) -> None:
    db = tmp_path / "portfolio.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        """
        CREATE TABLE decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            recommendation_kind TEXT NOT NULL,
            recommendation_value REAL,
            conviction TEXT,
            source_artifact_id INTEGER NOT NULL,
            source_lens TEXT,
            rationale_excerpt TEXT,
            made_at TIMESTAMP NOT NULL,
            user_acted_at TIMESTAMP,
            user_notes TEXT,
            outcome_at TIMESTAMP,
            outcome_status TEXT,
            outcome_pct REAL,
            outcome_notes TEXT,
            created_at TIMESTAMP NOT NULL
        )
        """
    )
    now = datetime(2026, 5, 1)
    conn.executemany(
        "INSERT INTO decisions (ticker, recommendation_kind, conviction, "
        "source_artifact_id, made_at, outcome_pct, outcome_at, created_at) "
        "VALUES ('META', ?, ?, 1, ?, ?, ?, ?)",
        [
            ("ADD", "high", now - timedelta(days=90), 0.08, now, now),  # win
            ("ADD", "medium", now - timedelta(days=60), -0.02, now, now),  # loss
            ("ADD", "high", now - timedelta(days=30), 0.05, now, now),  # win
            (
                "TRIM",
                "low",
                now - timedelta(days=10),
                -0.06,
                now,
                now,
            ),  # win (negative = correct trim)
            ("HOLD", None, now - timedelta(days=5), None, None, now),  # uncounted
        ],
    )
    conn.commit()
    conn.close()

    hist = load_decision_history("META", db_path=db)
    assert hist.total == 5
    assert hist.by_kind == {"ADD": 3, "TRIM": 1, "HOLD": 1}
    # 3 ADDs + 1 TRIM = 4 counted; 3 wins (2 ADD + 1 TRIM).
    assert hist.win_rate_overall is not None
    assert abs(hist.win_rate_overall - 0.75) < 1e-9


def test_saydo_verdicts_returns_typed_rows(tmp_path: Path) -> None:
    db = tmp_path / "portfolio.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        """
        CREATE TABLE management_commitments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            period_made TIMESTAMP NOT NULL,
            transcript_segment_id INTEGER NOT NULL,
            period_target TIMESTAMP NOT NULL,
            kpi_name TEXT NOT NULL,
            comparator TEXT NOT NULL,
            target_value NUMERIC NOT NULL,
            unit TEXT NOT NULL,
            narrative TEXT NOT NULL,
            realized_value NUMERIC,
            realized_doc_id INTEGER,
            outcome TEXT,
            evaluated_at TIMESTAMP,
            created_at TIMESTAMP NOT NULL
        )
        """
    )
    conn.execute(
        """
        INSERT INTO management_commitments
          (ticker, period_made, transcript_segment_id, period_target,
           kpi_name, comparator, target_value, unit, narrative,
           realized_value, outcome, evaluated_at, created_at)
        VALUES ('GOOG', '2025-10-30 00:00:00', 1, '2025-12-31 00:00:00',
                'Cloud revenue growth', '>=', 30.0, 'percent',
                'Cloud growth at the prior quarter pace through year-end',
                32.0, 'met', '2026-01-30 00:00:00', '2025-10-30 00:00:00')
        """
    )
    conn.commit()
    conn.close()

    out = load_saydo_verdicts("GOOG", db_path=db)
    assert len(out) == 1
    assert out[0].kpi_name == "Cloud revenue growth"
    assert out[0].outcome == "met"
    assert out[0].realized_value == 32.0


def test_peer_comp_returns_empty_when_files_missing(tmp_path: Path) -> None:
    """Cold-start eval — no FMP peers JSON yet — returns []."""
    out = load_peer_comp("XYZ", repo_root=tmp_path)
    assert out == []


def test_peer_comp_loads_from_fmp_cache(tmp_path: Path) -> None:
    """Happy path under the P4.2 scored selection: a same-industry peer with
    cached profile + legacy v3 key-metrics keys resolves with full metrics;
    pool members with nothing to score on (no profile, no payload identity)
    are dropped rather than returned as unexplained rows."""
    import json

    fmp = tmp_path / "data" / "historical" / "fmp"
    fmp.mkdir(parents=True)
    # FMP returns a list of {"symbol": "PEER", "peersList": [...]}.
    (fmp / "GOOG_peers.json").write_text(
        json.dumps([{"symbol": "GOOG", "peersList": ["META", "MSFT", "AAPL"]}]),
        encoding="utf-8",
    )
    profile_goog = [
        {
            "companyName": "Alphabet Inc.",
            "sector": "Communication Services",
            "industry": "Internet Content & Information",
            "mktCap": 2_000_000_000_000,
        }
    ]
    profile_meta = [
        {
            "companyName": "Meta Platforms Inc.",
            "sector": "Communication Services",
            "industry": "Internet Content & Information",
            "mktCap": 1_200_000_000_000,
        }
    ]
    (fmp / "GOOG_profile.json").write_text(json.dumps(profile_goog), encoding="utf-8")
    (fmp / "META_profile.json").write_text(json.dumps(profile_meta), encoding="utf-8")
    (fmp / "META_key_metrics_ttm.json").write_text(
        json.dumps(
            [{"revenueTTM": 165_000_000_000, "netIncomePerRevenueTTM": 0.30, "roicTTM": 0.22}]
        ),
        encoding="utf-8",
    )
    out = load_peer_comp("GOOG", repo_root=tmp_path)
    # MSFT / AAPL carry no profile, and the peersList payload shape has no
    # name/cap fallback -> zero affinity -> dropped.
    assert [r.peer_ticker for r in out] == ["META"]
    meta = out[0]
    assert meta.peer_name == "Meta Platforms Inc."
    assert meta.market_cap_usd == 1_200_000_000_000
    assert meta.revenue_ttm_usd == 165_000_000_000
    assert meta.net_margin_ttm == 0.30
    assert meta.roic_ttm == 0.22
    assert "same industry" in meta.match_reasons
    assert "similar scale" in meta.match_reasons


def test_peer_comp_caps_at_max_peers(tmp_path: Path) -> None:
    """FMP peers responses can carry 20+ tickers; cap at max_peers (equal
    scores keep the original pool order as the tiebreak)."""
    import json

    fmp = tmp_path / "data" / "historical" / "fmp"
    fmp.mkdir(parents=True)
    big_peer_list = [f"PR{i:02d}" for i in range(20)]
    (fmp / "GOOG_peers.json").write_text(
        json.dumps([{"symbol": "GOOG", "peersList": big_peer_list}]),
        encoding="utf-8",
    )
    # mktCap present so the rows carry at least one metric — PR7 drops
    # unnamed candidates whose every metric cell would render an em-dash.
    profile = [{"companyName": "X", "sector": "Technology", "industry": "Software", "mktCap": 1e9}]
    (fmp / "GOOG_profile.json").write_text(json.dumps(profile), encoding="utf-8")
    for p in big_peer_list:
        (fmp / f"{p}_profile.json").write_text(json.dumps(profile), encoding="utf-8")
    out = load_peer_comp("GOOG", repo_root=tmp_path, max_peers=4)
    assert len(out) == 4
    assert [r.peer_ticker for r in out] == big_peer_list[:4]


def test_ticker_case_normalized(tmp_path: Path) -> None:
    """All accessors should upper-case the ticker input — the codebase stores
    tickers uppercase but callers often pass mixed case."""
    db = tmp_path / "portfolio.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        """
        CREATE TABLE strategic_targets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            deck_doc_id INTEGER,
            target_kind TEXT NOT NULL,
            target_value REAL,
            target_unit TEXT NOT NULL,
            target_period TEXT NOT NULL,
            target_currency TEXT,
            narrative_excerpt TEXT NOT NULL,
            extracted_at TIMESTAMP NOT NULL,
            confidence REAL NOT NULL DEFAULT 1.0
        )
        """
    )
    conn.execute(
        "INSERT INTO strategic_targets (ticker, target_kind, target_value, "
        "target_unit, target_period, narrative_excerpt, extracted_at, confidence) "
        "VALUES ('META', 'revenue', 200.0, 'USD_B', 'FY2030', 'x', "
        "'2026-05-01 00:00:00', 1.0)"
    )
    conn.commit()
    conn.close()
    # Mixed-case input should still find the row.
    assert len(load_strategic_targets("meta", db_path=db)) == 1
    assert len(load_strategic_targets("Meta", db_path=db)) == 1
