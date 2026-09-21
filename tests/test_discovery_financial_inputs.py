"""Discovery canonical financial calculations and separate market provenance."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from discovery.screens import canonical_ticker_metrics
from provenance.metric_ontology import CanonicalMetricDefinitionRevision, MetricOntology
from sources.discovery_financial_inputs import (
    calculate_financial_inputs,
    comparable_quarter_window,
    free_cash_flow_yield,
    read_financial_inputs,
)
from sources.discovery_financials import GrowthFactReference, read_financial_history
from sources.discovery_market import read_market_context
from tests.test_canonical_growth_screen import STAMP, seed_growth_graph


def seed_market_context(
    conn: sqlite3.Connection,
    directory: Path,
    *,
    ticker: str = "WIX",
    currency: str = "USD",
    price: object = 25,
    recorded_at: datetime = STAMP,
) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{ticker}_profile.json"
    body = json.dumps(
        [
            {
                "symbol": ticker,
                "currency": currency,
                "marketCap": 500,
                "price": price,
                "companyName": "Synthetic",
                "sector": "Technology",
                "industry": "Software",
                "isActivelyTrading": True,
            }
        ]
    ).encode()
    path.write_bytes(body)
    sha = hashlib.sha256(body).hexdigest()
    stamp = STAMP.isoformat()
    conn.execute(
        "INSERT INTO evidence_content_blobs VALUES (?,?,?,?,?)",
        (sha, len(body), "application/json", path.as_uri(), stamp),
    )
    conn.execute(
        "INSERT INTO evidence_source_observations(observation_id,idempotency_key,source_kind,source_url,blob_sha256,observed_at,retrieved_at,retrieval_config_sha256,collector_code_version) VALUES ('profile-source','profile-source','fmp','https://example.invalid/profile',?,?,?,?,'fixture')",
        (sha, stamp, stamp, "1" * 64),
    )
    conn.execute(
        "INSERT INTO evidence_document_versions(document_version_id,document_key,version_sequence,observation_id,blob_sha256,issuer_id,ticker,document_type,form_type,language,recorded_at) VALUES ('profile-document','profile-document',1,'profile-source',?,'issuer-1',?,'fmp_profile','profile','en',?)",
        (sha, ticker, recorded_at.isoformat()),
    )
    conn.commit()


def test_financial_and_market_provenance_remain_separate(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    db = migrated_db(tmp_path / "fixture.db")
    with sqlite3.connect(db) as conn:
        seed_growth_graph(conn, tmp_path, include_screen_financials=True)
        source_dir = tmp_path / "data" / "historical" / "fmp"
        seed_market_context(conn, source_dir)
        financials = read_financial_inputs(conn, "WIX", as_of=STAMP.date())
        assert str(financials.revenue_yoy.value) == "0.3"
        assert len(financials.revenue_yoy.references) == 5
        assert financials.operating_margin_ttm.status == "available"
        assert financials.free_cash_flow_ttm.value == 40
        assert financials.roic_ttm.status == "unavailable"
        market = read_market_context(conn, source_dir, "WIX", as_of=STAMP.date())
        assert market.authority == "captured_provider_market_snapshot"
        assert free_cash_flow_yield(financials, market).value is not None
        assert str(free_cash_flow_yield(financials, market).value) == "0.08"
        metrics, evidence = canonical_ticker_metrics(
            conn, source_dir, "WIX", None, as_of=STAMP.date()
        )
        assert metrics.rev_yoy == 0.3
        assert metrics.fcf_yield_ttm == 0.08
        assert metrics.roic_ttm is None
        assert evidence["market"] != evidence["financials"]
        mismatch = market.model_copy(update={"currency": "DKK"})
        assert free_cash_flow_yield(financials, mismatch).reasons == (
            "financial_market_currency_mismatch",
        )


def test_unregistered_raw_market_file_cannot_supply_currency_or_cap(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    (tmp_path / "WIX_profile.json").write_text(
        '[{"symbol":"WIX","currency":"USD","marketCap":500}]'
    )
    with sqlite3.connect(migrated_db(tmp_path / "fixture.db")) as conn:
        observed = read_market_context(conn, tmp_path, "WIX", as_of=STAMP.date())
    assert observed.status == "unavailable"
    assert observed.currency is None
    assert observed.market_cap is None


def test_actual_need_rank_and_discovery_persist_canonical_financial_evidence(
    tmp_path: Path, migrated_db: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    from discovery.store import get_candidate_by_ticker, upsert_candidate
    from execution import run_discovery

    db = migrated_db(tmp_path / "fixture.db")
    with sqlite3.connect(db) as conn:
        seed_growth_graph(conn, tmp_path, include_screen_financials=True)
        seed_market_context(conn, tmp_path / "data" / "historical" / "fmp")
    upsert_candidate(ticker="WIX", name="Synthetic", score=1, evidence=[], db_path=db)

    def no_book(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(run_discovery, "_assemble_book_context_safe", no_book)
    run_discovery.discover(tmp_path, db_path=db, include_adjacency=False)
    candidate = get_candidate_by_ticker("WIX", db_path=db)
    assert candidate is not None and candidate.score_json is not None
    financial = candidate.score_json["financial_coverage"]
    assert isinstance(financial, dict)
    assert financial["financials"] is not None
    rank = candidate.score_json["need_rank"]
    assert isinstance(rank, dict)
    assert rank["financial_evidence"] == financial
    assert rank["garp"] > 0
    assert "ROIC definition unavailable" in rank["garp_reason"]


@pytest.mark.parametrize("in_universe", [True, False])
def test_unavailable_candidate_refresh_removes_stale_claim_and_preserves_other_owner(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    in_universe: bool,
) -> None:
    from discovery.store import (
        SignalWrite,
        get_candidate_by_ticker,
        list_signals,
        replace_signals,
        set_status,
        upsert_candidate,
    )
    from execution import run_discovery

    db = migrated_db(tmp_path / "fixture.db")
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO tracked_companies(ticker,name,list_type,instrument_type) VALUES ('EMPTY','Synthetic','index_member','equity')"
        )
    row = upsert_candidate(
        ticker="EMPTY",
        name="Synthetic",
        score=2.9,
        evidence=[
            {"source": "screen:growth_inflection", "detail": "old growth"},
            {"source": "investor_13f:retained", "detail": "other owner"},
        ],
        db_path=db,
    )
    set_status(row.id, "dismissed", db_path=db)
    if not in_universe:
        with sqlite3.connect(db) as conn:
            conn.execute("DELETE FROM tracked_companies WHERE ticker='EMPTY'")
    replace_signals(
        [
            SignalWrite(
                ticker="EMPTY",
                signal_class="screen",
                source_key="growth_inflection",
                weight=1,
                raw_strength=1,
                observed_at=STAMP.isoformat(),
            ),
            SignalWrite(
                ticker="EMPTY",
                signal_class="investor_13f",
                source_key="retained",
                weight=0.4,
                raw_strength=1,
                observed_at=STAMP.isoformat(),
            ),
        ],
        classes=("screen", "investor_13f"),
        db_path=db,
    )

    def no_book(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(run_discovery, "_assemble_book_context_safe", no_book)
    run_discovery.discover(tmp_path, db_path=db, include_adjacency=False)
    refreshed = get_candidate_by_ticker("EMPTY", db_path=db)
    assert refreshed is not None and refreshed.status == "dismissed"
    assert 0 < refreshed.score < 0.41
    assert all(item["source"] != "screen:growth_inflection" for item in refreshed.evidence)
    assert any(item["source"] == "investor_13f:retained" for item in refreshed.evidence)
    assert [item.signal_class for item in list_signals("EMPTY", db_path=db)] == ["investor_13f"]


def test_candidate_and_signal_refresh_roll_back_together(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    from discovery.store import (
        CandidateWrite,
        SignalWrite,
        get_candidate_by_ticker,
        persist_discovery_refresh,
        upsert_candidate,
    )

    db = migrated_db(tmp_path / "fixture.db")
    upsert_candidate(ticker="WIX", name="Synthetic", score=9, evidence=[{"old": True}], db_path=db)
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TRIGGER fixture_signal_failure BEFORE INSERT ON discovery_signals BEGIN SELECT RAISE(ABORT,'fixture signal failure'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="fixture signal failure"):
        persist_discovery_refresh(
            [CandidateWrite(ticker="WIX", name="Changed", score=0, evidence=[], score_json={})],
            [
                SignalWrite(
                    ticker="WIX",
                    signal_class="screen",
                    source_key="fixture",
                    weight=1,
                    raw_strength=1,
                    observed_at=STAMP.isoformat(),
                )
            ],
            classes=("screen",),
            db_path=db,
        )
    row = get_candidate_by_ticker("WIX", db_path=db)
    assert row is not None and row.score == 9 and row.evidence == [{"old": True}]


@pytest.mark.parametrize("break_kind", ["missing", "definition", "currency", "gap", "unresolved"])
def test_incomplete_or_incomparable_cash_never_joins_canonical_series(
    tmp_path: Path, migrated_db: Callable[..., Path], break_kind: str
) -> None:
    from datetime import timedelta

    from sources.discovery_financial_inputs import calculate_financial_inputs
    from sources.discovery_financials import read_financial_history

    with sqlite3.connect(migrated_db(tmp_path / "fixture.db")) as conn:
        seed_growth_graph(conn, tmp_path, include_screen_financials=True)
        history = read_financial_history(conn, "WIX", as_of=STAMP.date())
    cash = max(
        (item for item in history.references if item.concept == "free_cash_flow"),
        key=lambda item: item.period_end,
    )
    refs = history.references
    if break_kind == "missing":
        refs = tuple(item for item in refs if item != cash)
    elif break_kind == "unresolved":
        history = history.model_copy(update={"unresolved": (("free_cash_flow", cash.period_end),)})
    else:
        update: dict[str, object] = {}
        if break_kind == "definition":
            update["metric_id"] = "different-definition"
        elif break_kind == "currency":
            update["currency"] = "DKK"
        else:
            update["period_start"] = cash.period_start + timedelta(days=5)
        refs = tuple(item.model_copy(update=update) if item == cash else item for item in refs)
    result = calculate_financial_inputs(history.model_copy(update={"references": refs}))
    assert result.free_cash_flow_ttm.status == "unavailable"
    assert result.free_cash_flow_ttm.value is None
    # An unrelated valid revenue series remains usable with its own provenance.
    assert result.revenue_yoy.status == "available"


def test_market_cutoff_and_byte_identity_fail_closed(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    from datetime import timedelta

    with sqlite3.connect(migrated_db(tmp_path / "fixture.db")) as conn:
        seed_growth_graph(conn, tmp_path)
        source_dir = tmp_path / "market"
        seed_market_context(conn, source_dir)
        assert (
            read_market_context(
                conn, source_dir, "WIX", as_of=STAMP.date() - timedelta(days=1)
            ).status
            == "unavailable"
        )
        path = source_dir / "WIX_profile.json"
        path.write_text(path.read_text().replace("500", "600"))
        assert (
            read_market_context(conn, source_dir, "WIX", as_of=STAMP.date()).status == "unavailable"
        )


def test_shadow_receipt_never_promotes_numerical_match_to_definition_parity(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    from sources.discovery_financial_inputs import financial_parity_receipt

    with sqlite3.connect(migrated_db(tmp_path / "fixture.db")) as conn:
        seed_growth_graph(conn, tmp_path, include_screen_financials=True)
        seed_market_context(conn, tmp_path / "market")
        financials = read_financial_inputs(conn, "WIX", as_of=STAMP.date())
        market = read_market_context(conn, tmp_path / "market", "WIX", as_of=STAMP.date())
    receipt = financial_parity_receipt(
        financials,
        free_cash_flow_yield(financials, market),
        legacy_values=(0.3, 0.5, 0.08),
        legacy_source_hashes={"profile": market.source_payload_sha256 or ""},
    )
    assert receipt["definition_parity"] == "UNVERIFIED"
    fields = receipt["fields"]
    assert isinstance(fields, dict)
    assert fields["revenue_yoy"]["status"] == "NUMERICAL_MATCH"
    assert fields["operating_margin_ttm"]["status"] == "NUMERICAL_DIVERGENCE"
    assert fields["free_cash_flow_yield"]["status"] == "NUMERICAL_MATCH"


def test_cash_value_usd_liquidity_floor_rejects_other_currencies(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    from dataclasses import replace

    from discovery.screens import SCREENS

    with sqlite3.connect(migrated_db(tmp_path / "fixture.db")) as conn:
        seed_growth_graph(conn, tmp_path, include_screen_financials=True)
        seed_market_context(conn, tmp_path / "market")
        metrics, _evidence = canonical_ticker_metrics(
            conn, tmp_path / "market", "WIX", None, as_of=STAMP.date()
        )
    # This tests only the existing numeric gate with an explicitly supplied quality leg;
    # production ROIC remains unavailable until a definition is approved.
    supplied = replace(metrics, roic_ttm=0.1, market_cap=5e9)
    assert SCREENS["fcf_value"](supplied) is not None
    assert SCREENS["fcf_value"](replace(supplied, market_cap_currency="DKK")) is None


@pytest.mark.parametrize("compatible", [True, False])
def test_metric_revision_compatibility_and_historic_reads(
    tmp_path: Path, migrated_db: Callable[..., Path], compatible: bool
) -> None:
    from datetime import timedelta

    from provenance.metric_ontology import MetricOntology
    from sources.discovery_financials import read_growth_financials

    with sqlite3.connect(migrated_db(tmp_path / "fixture.db")) as conn:
        seed_growth_graph(conn, tmp_path, include_screen_financials=True)
        original = read_growth_financials(conn, "WIX", as_of=STAMP.date())
        assert original.status == "available"
        revenue = next(item for item in original.references if item.concept == "revenue")
        ontology = MetricOntology(conn)
        definition = ontology.metric_definition_as_known(revenue.metric_id, STAMP)
        assert definition is not None
        later = STAMP + timedelta(days=1)
        changes: dict[str, object] = {
            "metric_definition_revision_id": "revision-2",
            "idempotency_key": "revision-2",
            "revision": 2,
            "supersedes_metric_definition_revision_id": definition.metric_definition_revision_id,
            "effective_at": later,
            "knowledge_at": later,
            "recorded_at": later,
            "aliases": (*definition.aliases, "additional-display-alias"),
        }
        if not compatible:
            changes["definition_text"] = "Different economic meaning without comparability proof"
        ontology.persist_metric_definition(definition.model_copy(update=changes))
        assert read_growth_financials(conn, "WIX", as_of=STAMP.date()) == original
        current_growth = read_growth_financials(conn, "WIX", as_of=later.date())
        current_financial = read_financial_inputs(conn, "WIX", as_of=later.date())
        if compatible:
            assert current_growth.status == "available"
            assert current_financial.revenue_yoy.status == "available"
            assert any(
                item.metric_definition_revision_id == "revision-2"
                for item in current_growth.references
            )
        else:
            assert current_growth.status != "available"
            assert current_financial.revenue_yoy.status == "unavailable"
            assert current_financial.free_cash_flow_ttm.status == "available"


def test_market_freshness_reuses_exact_tracked_tier_policy(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    from datetime import timedelta

    from pipeline.cadence_policy import cadence_hours

    with sqlite3.connect(migrated_db(tmp_path / "fixture.db")) as conn:
        seed_growth_graph(conn, tmp_path, include_screen_financials=True)
        seed_market_context(conn, tmp_path / "market")
        financials = read_financial_inputs(conn, "WIX", as_of=STAMP.date())
        fresh = read_market_context(conn, tmp_path / "market", "WIX", as_of=STAMP.date())
        assert fresh.status == "available" and fresh.freshness_status == "fresh"
        conn.execute("UPDATE tracked_companies SET list_type='portfolio' WHERE ticker='WIX'")
        stale = read_market_context(
            conn, tmp_path / "market", "WIX", as_of=STAMP.date() + timedelta(days=1)
        )
        assert stale.freshness_limit_hours == cadence_hours("portfolio", "time_sensitive")
        assert stale.status == "degraded" and stale.freshness_status == "stale"
        assert (
            stale.market_cap == 500 and stale.source_payload_sha256 == fresh.source_payload_sha256
        )
        assert free_cash_flow_yield(financials, stale).status == "unavailable"
        conn.execute("DELETE FROM tracked_companies WHERE ticker='WIX'")
        unknown = read_market_context(conn, tmp_path / "market", "WIX", as_of=STAMP.date())
        assert unknown.status == "degraded" and unknown.freshness_status == "unverified"


def test_refresh_bulk_read_uses_one_connection_and_preserves_all_statuses(
    tmp_path: Path, migrated_db: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    from discovery import store

    db = migrated_db(tmp_path / "fixture.db")
    for ticker in ("ONE", "TWO"):
        row = store.upsert_candidate(ticker=ticker, name=ticker, score=1, evidence=[], db_path=db)
        store.set_status(row.id, "dismissed" if ticker == "ONE" else "built", db_path=db)
    store.upsert_candidate(
        ticker="OTHER", name="Other user", score=9, evidence=[], user_id="other", db_path=db
    )
    original_open = store.open_conn
    opens: list[object] = []
    queries: list[str] = []

    def traced_open(path: Path | str | None = None) -> sqlite3.Connection:
        connection = original_open(path)
        opens.append(path)
        connection.set_trace_callback(queries.append)
        return connection

    monkeypatch.setattr(store, "open_conn", traced_open)
    candidates, _signals = store.load_discovery_refresh_state(db_path=db)
    assert {key: row.status for key, row in candidates.items()} == {
        "ONE": "dismissed",
        "TWO": "built",
    }
    assert len(opens) == 1
    assert len([query for query in queries if query.startswith("SELECT")]) == 2


def test_explicit_checkout_default_override_is_prohibited() -> None:
    import db_paths
    from execution import run_discovery

    checkout = Path(db_paths.__file__).resolve().parents[1]
    with pytest.raises(RuntimeError, match="checkout-default"):
        run_discovery.discover(checkout, db_path=checkout / "data" / "portfolio.db")


def test_market_document_not_known_at_cutoff_cannot_backdate_availability(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    from datetime import timedelta

    with sqlite3.connect(migrated_db(tmp_path / "fixture.db")) as conn:
        seed_growth_graph(conn, tmp_path)
        seed_market_context(conn, tmp_path / "market", recorded_at=STAMP + timedelta(days=1))
        assert (
            read_market_context(conn, tmp_path / "market", "WIX", as_of=STAMP.date()).status
            == "unavailable"
        )


@pytest.mark.parametrize("price", [-1, "NaN", "Infinity", True, [], {"unexpected": "shape"}])
def test_market_context_keeps_valid_cap_when_profile_price_is_invalid(
    tmp_path: Path, migrated_db: Callable[..., Path], price: object
) -> None:
    with sqlite3.connect(migrated_db(tmp_path / "fixture.db")) as conn:
        seed_growth_graph(conn, tmp_path)
        seed_market_context(conn, tmp_path / "market", price=price)
        result = read_market_context(conn, tmp_path / "market", "WIX", as_of=STAMP)
    assert result.market_cap == 500
    assert result.price is None
    assert "current_price_unavailable" in result.reason_codes


def test_market_context_uses_exact_intraday_cutoff_and_rejects_naive_datetime(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    with sqlite3.connect(migrated_db(tmp_path / "fixture.db")) as conn:
        seed_growth_graph(conn, tmp_path)
        seed_market_context(conn, tmp_path / "market", price=25)
        assert (
            read_market_context(
                conn, tmp_path / "market", "WIX", as_of=STAMP - timedelta(seconds=1)
            ).status
            == "unavailable"
        )
        assert read_market_context(conn, tmp_path / "market", "WIX", as_of=STAMP).price == 25
        with pytest.raises(ValueError, match="timezone-aware"):
            read_market_context(conn, tmp_path / "market", "WIX", as_of=STAMP.replace(tzinfo=None))


@pytest.mark.parametrize(
    ("quarter_days", "available"),
    [
        ((71, 71, 71, 71), False),
        ((106, 106, 106, 106), False),
        ((91, 91, 91, 92), True),
        ((91, 91, 91, 98), True),
    ],
)
def test_four_quarters_must_cover_a_supported_reporting_year(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    quarter_days: tuple[int, int, int, int],
    available: bool,
) -> None:
    with sqlite3.connect(migrated_db(tmp_path / "fixture.db")) as conn:
        seed_growth_graph(conn, tmp_path, include_screen_financials=True)
        history = read_financial_history(conn, "WIX", as_of=STAMP.date())
    # Exercise the pure calculation contract with retained source references;
    # exact source publication is covered by the reader integration fixtures.
    originals = sorted(
        (item for item in history.references if item.concept == "free_cash_flow"),
        key=lambda item: item.period_end,
    )[-4:]
    assert len(originals) == 4
    start = STAMP.date() - timedelta(days=sum(quarter_days))
    periods: list[GrowthFactReference] = []
    for item, days in zip(originals, quarter_days, strict=True):
        end = start + timedelta(days=days - 1)
        periods.append(item.model_copy(update={"period_start": start, "period_end": end}))
        start = end + timedelta(days=1)
    history = history.model_copy(update={"references": tuple(periods), "unresolved": ()})
    window = comparable_quarter_window(history, "free_cash_flow", 4)
    cash = calculate_financial_inputs(history).free_cash_flow_ttm
    if available:
        assert not isinstance(window, str)
        assert cash.status == "available" and cash.value == 40
    else:
        assert window == "four_quarter_duration_not_annual"
        assert cash.value is None and cash.reasons == (window,)


@pytest.mark.parametrize("caller_transaction", [False, True])
def test_financial_history_uses_one_snapshot_during_definition_append(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    caller_transaction: bool,
) -> None:
    database = migrated_db(tmp_path / "fixture.db")
    with sqlite3.connect(database) as conn:
        seed_growth_graph(conn, tmp_path)
        conn.execute("PRAGMA journal_mode=WAL")
        initial = read_financial_history(conn, "WIX", as_of=STAMP.date(), concepts=("revenue",))
        assert len(initial.references) > 1 and not initial.unresolved
        original = MetricOntology.metric_definition_as_known
        calls = 0
        appended = False
        later = STAMP + timedelta(hours=1)

        def observe(
            self: MetricOntology,
            metric_id: str,
            cutoff: datetime,
        ) -> CanonicalMetricDefinitionRevision | None:
            nonlocal calls, appended
            definition = original(self, metric_id, cutoff)
            calls += 1
            if calls == 2:
                assert definition is not None
                writer = sqlite3.connect(database)
                try:
                    MetricOntology(writer).persist_metric_definition(
                        definition.model_copy(
                            update={
                                "metric_definition_revision_id": "concurrent-discovery-definition",
                                "idempotency_key": "concurrent-discovery-definition",
                                "revision": 2,
                                "supersedes_metric_definition_revision_id": definition.metric_definition_revision_id,
                                "definition_text": "A new incompatible synthetic reporting scope",
                                "effective_at": later,
                                "knowledge_at": later,
                                "recorded_at": later,
                            }
                        )
                    )
                    writer.commit()
                    appended = True
                finally:
                    writer.close()
            return definition

        monkeypatch.setattr(MetricOntology, "metric_definition_as_known", observe)
        if caller_transaction:
            conn.execute("BEGIN")
        factory = conn.row_factory
        current = read_financial_history(conn, "WIX", as_of=STAMP.date(), concepts=("revenue",))
        assert appended and current == initial
        assert conn.in_transaction is caller_transaction and conn.row_factory is factory
        if caller_transaction:
            conn.rollback()
        subsequent = read_financial_history(conn, "WIX", as_of=STAMP.date(), concepts=("revenue",))
        assert not subsequent.references and subsequent.unresolved
