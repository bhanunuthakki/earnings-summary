"""Actual peer panel and route read governed membership and canonical facts."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from datetime import date, timedelta
from io import StringIO
from pathlib import Path

import pytest

from compute.comparable_set_reader import read_frozen_comparable_set
from compute.comparable_sets import METHOD_VERSION, comparable_set_id
from report.render_clock import fixed_render_clock
from report.renderers.workspace_sections.eval_screen import _peer_comp_panel
from report.sections.p3_data import load_peer_comp
from tests.test_canonical_growth_screen import STAMP, seed_growth_graph
from tests.test_discovery_financial_inputs import seed_market_context


def seed_membership(
    conn: sqlite3.Connection, *, members: tuple[str, ...] = ("WIX",), as_of: date = STAMP.date()
) -> None:
    source_id = comparable_set_id("SUBJECT", METHOD_VERSION)
    conn.execute(
        "INSERT INTO comparable_sets(comparable_set_id,ticker,method_version,resolved_at,metric_class,method_flags,source_summary) VALUES (?,?,?,?,?,?,?)",
        (source_id, "SUBJECT", METHOD_VERSION, as_of.isoformat(), "operating", "{}", "{}"),
    )
    conn.executemany(
        "INSERT INTO comparable_set_members(comparable_set_id,member_ticker,membership_reason,context_only,valid_from) VALUES (?,?,?,0,?)",
        [(source_id, peer, "industry_seed", as_of.isoformat()) for peer in members],
    )
    conn.commit()


def seed_peer_fixture(conn: sqlite3.Connection, root: Path) -> None:
    seed_growth_graph(conn, root, include_peer_financials=True)
    seed_market_context(conn, root / "data" / "historical" / "fmp")
    seed_membership(conn)
    # Raw vendor candidates and false values cannot alter governed membership or facts.
    raw = root / "data" / "historical" / "fmp"
    (raw / "SUBJECT_peers.json").write_text('[{"symbol":"RAWONLY","companyName":"Unadmitted"}]')
    (raw / "WIX_income_statement_quarterly.json").write_text(json.dumps([{"revenue": 999}] * 4))


def test_actual_peer_projection_persists_full_source_evidence_in_render(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    with sqlite3.connect(migrated_db(tmp_path / "fixture.db")) as conn:
        seed_peer_fixture(conn, tmp_path)
        conn.row_factory = None
        with fixed_render_clock(STAMP.date()):
            rows = load_peer_comp("SUBJECT", repo_root=tmp_path, conn=conn)
        assert conn.row_factory is None
        assert conn.execute("SELECT 1").fetchone() == (1,)
    assert [row.peer_ticker for row in rows] == ["WIX"]
    row = rows[0]
    assert row.revenue_ttm_usd == 475
    assert row.net_margin_ttm == pytest.approx(60 / 475)
    assert row.market_cap_usd == 500 and row.roic_ttm is None
    assert row.source_evidence is not None
    manifest = json.dumps(row.source_evidence)
    assert "canonical_resolution_revision_id" in manifest
    assert "NUMERICAL_DIVERGENCE" in manifest and "UNVERIFIED" in manifest
    rendered = StringIO()
    _peer_comp_panel(rendered, rows)
    html = rendered.getvalue()
    assert "Sources and coverage" in html and "ROIC unavailable" in html
    assert "canonical_resolution_revision_id" in html and "snapshot_sha256" in html
    assert "$475" in html and "12.6%" in html


def test_raw_only_unavailable_is_distinct_from_valid_empty_frozen_set(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    with sqlite3.connect(migrated_db(tmp_path / "fixture.db")) as conn:
        with pytest.raises(ValueError, match="membership_unavailable"):
            load_peer_comp("SUBJECT", repo_root=tmp_path, conn=conn)
        seed_membership(conn, members=())
        assert load_peer_comp("SUBJECT", repo_root=tmp_path, conn=conn) == []


def test_frozen_selection_rejects_future_resolution_and_future_members(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    with sqlite3.connect(migrated_db(tmp_path / "fixture.db")) as conn:
        seed_membership(conn)
        assert (
            read_frozen_comparable_set(conn, "SUBJECT", as_of=STAMP.date() - timedelta(days=1))
            is None
        )
        future = (STAMP.date() + timedelta(days=1)).isoformat()
        source_id = comparable_set_id("SUBJECT", METHOD_VERSION)
        conn.execute(
            "INSERT INTO comparable_set_members(comparable_set_id,member_ticker,membership_reason,context_only,valid_from) VALUES (?,?,?,0,?)",
            (source_id, "FUTURE", "industry_seed", future),
        )
        conn.execute(
            "INSERT INTO comparable_set_members(comparable_set_id,member_ticker,membership_reason,context_only,valid_from) VALUES (?,?,?,1,?)",
            (source_id, "CONTEXT", "pinned_override", STAMP.date().isoformat()),
        )
        observed = read_frozen_comparable_set(conn, "SUBJECT", as_of=STAMP.date())
        assert observed is not None and observed.members == (("WIX", "industry_seed"),)
        conn.execute(
            "UPDATE comparable_set_members SET valid_to=? WHERE member_ticker='WIX'", (future,)
        )
        later = read_frozen_comparable_set(conn, "SUBJECT", as_of=date.fromisoformat(future))
        assert later is not None and later.members == (("FUTURE", "industry_seed"),)
        # Historical valid-time remains explicit; no later-resolution reconstruction is invented.
        assert read_frozen_comparable_set(conn, "SUBJECT", as_of=STAMP.date()) == observed


def test_owner_exclusions_and_hide_override_do_not_add_raw_members(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    with sqlite3.connect(migrated_db(tmp_path / "fixture.db")) as conn:
        seed_peer_fixture(conn, tmp_path)
        thesis = tmp_path / "micro_thesis" / "holdings" / "SUBJECT.json"
        thesis.parent.mkdir(parents=True)
        thesis.write_text(
            json.dumps({"peer_exclude": ["WIX"], "competitive_watchlist": ["RAWONLY"]})
        )
        with fixed_render_clock(STAMP.date()):
            assert load_peer_comp("SUBJECT", repo_root=tmp_path, conn=conn) == []
        thesis.write_text(
            json.dumps({"peers_section_override": {"action": "hide", "min_quality_peers": 2}})
        )
        with fixed_render_clock(STAMP.date()):
            assert load_peer_comp("SUBJECT", repo_root=tmp_path, conn=conn) == []


def test_actual_peers_api_uses_explicit_request_database(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    from execution.comments_server import create_app

    db = migrated_db(tmp_path / "fixture.db")
    with sqlite3.connect(db) as conn:
        seed_peer_fixture(conn, tmp_path)
    app = create_app(repo_root=tmp_path, db_path=db)
    with fixed_render_clock(STAMP.date()):
        response = app.test_client().get("/api/peers/SUBJECT")
    assert response.status_code == 200
    payload: object = response.get_json()
    assert isinstance(payload, dict)
    assert payload["peers"] == [
        {"ticker": "WIX", "name": "Synthetic", "reasons": ["industry seed"]}
    ]
    assert not (tmp_path / "data" / "portfolio.db").exists()


@pytest.mark.parametrize("semantic_break", [False, True])
def test_missing_or_changed_net_income_cannot_be_supplied_by_raw_cache(
    tmp_path: Path, migrated_db: Callable[..., Path], semantic_break: bool
) -> None:
    from provenance.metric_ontology import MetricOntology
    from sources.discovery_financials import read_financial_history

    db = migrated_db(tmp_path / "fixture.db")
    with sqlite3.connect(db) as conn:
        seed_growth_graph(conn, tmp_path, include_peer_financials=semantic_break)
        seed_market_context(conn, tmp_path / "data" / "historical" / "fmp")
        seed_membership(conn)
        root = tmp_path / "data" / "historical" / "fmp"
        (root / "WIX_financial_ratios_ttm.json").write_text('[{"netProfitMarginTTM":0.99}]')
        cutoff = STAMP.date()
        if semantic_break:
            history = read_financial_history(conn, "WIX", as_of=cutoff, concepts=("net_income",))
            metric_id = history.references[0].metric_id
            ontology = MetricOntology(conn)
            definition = ontology.metric_definition_as_known(metric_id, STAMP)
            assert definition is not None
            later = STAMP + timedelta(days=1)
            ontology.persist_metric_definition(
                definition.model_copy(
                    update={
                        "revision": 2,
                        "metric_definition_revision_id": "changed-net-definition",
                        "idempotency_key": "changed-net-definition",
                        "supersedes_metric_definition_revision_id": definition.metric_definition_revision_id,
                        "definition_text": "Different scope without compatibility proof",
                        "effective_at": later,
                        "knowledge_at": later,
                        "recorded_at": later,
                    }
                )
            )
            cutoff = later.date()
        with fixed_render_clock(cutoff):
            rows = load_peer_comp("SUBJECT", repo_root=tmp_path, conn=conn)
        assert len(rows) == 1
        assert rows[0].revenue_ttm_usd == 475 and rows[0].net_margin_ttm is None
        assert rows[0].source_evidence is not None
        assert "net margin unavailable" in " ".join(rows[0].coverage_notes).lower()
        conn.commit()


def test_owner_name_pin_preserved_only_within_governed_membership(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    with sqlite3.connect(migrated_db(tmp_path / "fixture.db")) as conn:
        seed_peer_fixture(conn, tmp_path)
        thesis = tmp_path / "micro_thesis" / "holdings" / "SUBJECT.json"
        thesis.parent.mkdir(parents=True)
        thesis.write_text(
            json.dumps(
                {
                    "competitive_watchlist": ["Synthetic", "RAWONLY"],
                    "peers_section_override": {"action": "hide", "min_quality_peers": 1},
                }
            )
        )
        with fixed_render_clock(STAMP.date()):
            rows = load_peer_comp("SUBJECT", repo_root=tmp_path, conn=conn)
        assert [row.peer_ticker for row in rows] == ["WIX"]
        assert "named rival" in rows[0].match_reasons
        assert rows[0].source_evidence is not None and "owner_context" in rows[0].source_evidence


def test_nonempty_governed_membership_retains_unavailable_row(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    with sqlite3.connect(migrated_db(tmp_path / "fixture.db")) as conn:
        seed_membership(conn)
        with fixed_render_clock(STAMP.date()):
            rows = load_peer_comp("SUBJECT", repo_root=tmp_path, conn=conn)
        assert len(rows) == 1 and rows[0].peer_ticker == "WIX"
        row = rows[0]
        assert row.market_cap_usd is None and row.revenue_ttm_usd is None
        assert row.net_margin_ttm is None and row.roic_ttm is None
        assert row.coverage_notes and row.source_evidence is not None
        manifest = json.dumps(row.source_evidence)
        assert "current_only_not_historical_as_known" in manifest
        rendered = StringIO()
        _peer_comp_panel(rendered, rows)
        assert "WIX" in rendered.getvalue() and "Unavailable" in rendered.getvalue()
