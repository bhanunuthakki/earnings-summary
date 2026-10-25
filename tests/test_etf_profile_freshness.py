"""Real parser/store/consumer freshness contracts using isolated migrated state."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from etf_sources.ingest import apply_characteristics
from etf_sources.issuer_registry import IssuerCharacteristics
from execution.fetch_etf_data import ingest_live, parse_etf_holdings, parse_etf_info
from instrument_store import get_etf_profile, upsert_etf_profile
from models.instruments import EtfProfile
from net.client import (
    FMP_CLIENT,
    HttpCallError,
    HttpErrorKind,
    HttpJsonResponse,
    HttpResponseCapture,
    JsonShape,
    JsonValue,
)
from pipeline.work_os_evaluation import build_etf_profile_inputs
from research.investment_profile import EtfProfileInputs, derive_etf_label_evidence


def _captured_response(payload: JsonValue, url: str) -> HttpJsonResponse:
    return HttpJsonResponse(
        200,
        payload,
        HttpResponseCapture(
            body=json.dumps(payload).encode(),
            received_at=datetime.now(UTC),
            capture_id=str(uuid4()),
            origin="https://financialmodelingprep.com",
            path=url.removeprefix("https://financialmodelingprep.com"),
        ),
    )


def _policy_link(profile: EtfProfile) -> EtfProfile:
    """Synthetic complete links isolate the existing age/value policy assertions."""
    return profile.model_copy(
        update={
            "field_evidence": {
                key: evidence.model_copy(
                    update={
                        "document_id": 1,
                        "document_version_id": "synthetic-document",
                        "observation_id": "synthetic-observation",
                        "content_sha256": hashlib.sha256(b"synthetic").hexdigest(),
                        "source_url": "https://financialmodelingprep.com/stable/etf/info?symbol=VDE",
                        "json_pointer": "/sector",
                        "parser_version": "synthetic-policy-test.v1",
                    }
                )
                for key, evidence in profile.field_evidence.items()
            }
        }
    )


def _inputs(conn: sqlite3.Connection) -> EtfProfileInputs:
    return build_etf_profile_inputs(
        conn,
        ticker="VDE",
        fit=None,
        sharpe_delta_bps=None,
        loadings_cache={},
        whatif_cache={},
        warnings=set(),
    )


def test_old_profile_does_not_authorize_current_labels(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    with sqlite3.connect(migrated_db(tmp_path / "test.db")) as conn:
        conn.row_factory = sqlite3.Row
        profile = parse_etf_info(
            {"sector": "Energy", "yield": 3.5}, "VDE", datetime(2000, 1, 1, tzinfo=UTC)
        )
        upsert_etf_profile(conn, profile)
        assert not derive_etf_label_evidence(_inputs(conn))


def test_partial_overlay_does_not_renew_old_profile_fields(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    with sqlite3.connect(migrated_db(tmp_path / "test.db")) as conn:
        conn.row_factory = sqlite3.Row
        old = datetime(2000, 1, 1, tzinfo=UTC)
        upsert_etf_profile(conn, parse_etf_info({"sector": "Energy", "yield": 3.5}, "VDE", old))
        apply_characteristics(
            conn, "VDE", IssuerCharacteristics(source="issuer:test", expense_ratio=0.001)
        )
        stored = get_etf_profile(conn, "VDE")
        assert stored is not None and stored.profile_fetched_at == old
        assert not derive_etf_label_evidence(_inputs(conn))


def test_fresh_capture_roundtrip_labels_disclose_unknown_publication(
    tmp_path: Path, migrated_db: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    with sqlite3.connect(migrated_db(tmp_path / "test.db")) as conn:
        conn.row_factory = sqlite3.Row

        def wire(url: str, **_kwargs: object) -> HttpJsonResponse:
            payload: JsonValue = (
                {"symbol": "VDE", "sector": "Energy", "yield": 3.5} if url.endswith("/info") else []
            )
            return _captured_response(payload, url)

        monkeypatch.setattr(FMP_CLIENT, "get_url_json", wire)
        monkeypatch.setenv("FMP_API_KEY", "synthetic-key")
        ingest_live(conn, "VDE", tmp_path)

        warnings: set[str] = set()
        inputs = build_etf_profile_inputs(
            conn,
            ticker="VDE",
            fit=None,
            sharpe_delta_bps=None,
            loadings_cache={},
            whatif_cache={},
            warnings=warnings,
        )
        labels = derive_etf_label_evidence(inputs)
        assert {label.value for label in labels} == {
            "income",
            "thematic_exposure",
            "tactical_cyclical",
        }
        assert "etf_profile_publication_date_unknown" in warnings
        assert inputs.profile_field_evidence["distribution_yield"]["source"] == "fmp"
        assert inputs.profile_field_evidence["distribution_yield"]["source_as_of"] is None
        assert (
            inputs.profile_field_evidence["distribution_yield"]["currency"] == "recently_captured"
        )
        assert all(label.evidence["profile_capture_evidence"] for label in labels.values())


def test_recapture_historical_source_keeps_currency_warning(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    with sqlite3.connect(migrated_db(tmp_path / "test.db")) as conn:
        conn.row_factory = sqlite3.Row
        upsert_etf_profile(
            conn,
            parse_etf_info(
                {"sector": "Energy", "yield": 3.5, "asOfDate": "2000-01-01"},
                "VDE",
                datetime.now(UTC),
            ),
        )
        warnings: set[str] = set()
        inputs = build_etf_profile_inputs(
            conn,
            ticker="VDE",
            fit=None,
            sharpe_delta_bps=None,
            loadings_cache={},
            whatif_cache={},
            warnings=warnings,
        )
        assert not derive_etf_label_evidence(inputs)
        assert "etf_profile_source_currency_unverified" in warnings


def test_legacy_profile_is_not_backfilled_with_invented_capture_proof(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    with sqlite3.connect(migrated_db(tmp_path / "test.db")) as conn:
        conn.row_factory = sqlite3.Row
        profile = parse_etf_info({"sector": "Energy"}, "VDE", datetime.now(UTC))
        upsert_etf_profile(conn, profile.model_copy(update={"field_evidence": {}}))
        assert not derive_etf_label_evidence(_inputs(conn))


def test_fresh_partial_overlay_only_admits_its_published_fields(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    with sqlite3.connect(migrated_db(tmp_path / "test.db")) as conn:
        conn.row_factory = sqlite3.Row
        upsert_etf_profile(
            conn,
            parse_etf_info(
                {"sector": "Energy", "yield": 3.5}, "VDE", datetime(2000, 1, 1, tzinfo=UTC)
            ),
        )
        apply_characteristics(
            conn, "VDE", IssuerCharacteristics(source="issuer:test", distribution_yield=0.04)
        )
        labels = derive_etf_label_evidence(_inputs(conn))
        assert {label.value for label in labels} == {"income"}


def test_cache_replay_mtime_is_not_capture_proof(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    from execution.fetch_etf_data import ingest_from_cache

    (tmp_path / "VDE_etf_info.json").write_text('{"sector":"Energy","yield":3.5}')
    (tmp_path / "VDE_etf_holdings.json").write_text("[]")
    with sqlite3.connect(migrated_db(tmp_path / "test.db")) as conn:
        conn.row_factory = sqlite3.Row
        ingest_from_cache(conn, "VDE", fmp_dir=tmp_path)
        assert not derive_etf_label_evidence(_inputs(conn))


def test_capture_admission_bounds_and_value_identity() -> None:
    from datetime import timedelta

    from etf_sources.profile_evidence import admitted_profile_fields

    now = datetime(2026, 9, 19, tzinfo=UTC)
    for capture, expected in (
        (now - timedelta(days=7), True),
        (now - timedelta(days=7, seconds=1), False),
        (now + timedelta(seconds=1), False),
        (now.replace(tzinfo=None), False),
    ):
        profile = _policy_link(parse_etf_info({"sector": "Energy"}, "VDE", capture))
        assert bool(admitted_profile_fields(profile, now=now)) is expected
    profile = _policy_link(
        parse_etf_info({"sector": "Energy", "asOfDate": "2026-09-19"}, "VDE", now)
    )
    assert admitted_profile_fields(profile, now=now)
    assert not admitted_profile_fields(
        profile.model_copy(update={"sector_label": "Utilities"}), now=now
    )


def test_invalid_source_date_cannot_become_undated_admissible_evidence() -> None:
    from etf_sources.profile_evidence import admitted_profile_fields

    profile = parse_etf_info({"sector": "Energy", "asOfDate": "invalid"}, "VDE", datetime.now(UTC))
    assert not admitted_profile_fields(profile)


@pytest.mark.parametrize("primary", [0, "0"])
def test_etf_parsers_accept_zero(primary: object) -> None:
    fetched_at = datetime(2024, 5, 1, tzinfo=UTC)
    info = parse_etf_info(
        {"aum": primary, "assetsUnderManagement": 1000000, "yield": primary, "dividendYield": 5},
        "AAA",
        fetched_at,
    )
    holdings = parse_etf_holdings(
        [{"weightPercentage": primary, "weight": 7, "sharesNumber": primary, "shares": 99}],
        "AAA",
        fetched_at.date(),
        fetched_at,
    )
    assert (
        info.aum_usd_m,
        info.distribution_yield,
        holdings[0].weight_pct,
        holdings[0].shares_held,
    ) == (0.0, 0.0, 0.0, 0.0)


@pytest.mark.parametrize(
    "bad",
    [None, "", "bad", False, True, float("nan"), float("inf"), float("-inf"), "NaN", 10**400],
)
def test_etf_parsers_fallback_for_invalid(bad: object) -> None:
    fetched_at = datetime(2024, 5, 1, tzinfo=UTC)
    info = parse_etf_info(
        {
            "aum": bad,
            "assetsUnderManagement": 1000000,
            "yield": bad,
            "dividendYield": 5,
            "nav": bad,
            "price": bad,
        },
        "AAA",
        fetched_at,
    )
    holdings = parse_etf_holdings(
        [{"weightPercentage": bad, "weight": "7%", "sharesNumber": bad, "shares": 99}],
        "AAA",
        fetched_at.date(),
        fetched_at,
    )
    assert (
        info.aum_usd_m,
        info.distribution_yield,
        holdings[0].weight_pct,
        holdings[0].shares_held,
    ) == (1.0, 0.05, 0.07, 99.0)
    assert info.nav is None
    assert info.price is None


def test_ingest_live_positive_two_calls(
    tmp_path: Path, migrated_db: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    def wire(url: str, **kwargs: object) -> HttpJsonResponse:
        calls.append((url, kwargs))
        if url.endswith("/stable/etf/info"):
            return _captured_response({"symbol": "ZZZ", "sector": "Energy"}, url)
        return _captured_response([], url)

    monkeypatch.setattr(FMP_CLIENT, "get_url_json", wire)
    monkeypatch.setenv("FMP_API_KEY", "synthetic-key")
    with sqlite3.connect(migrated_db(tmp_path / "test.db")) as conn:
        conn.row_factory = sqlite3.Row
        profile, count = ingest_live(conn, "ZZZ", tmp_path / "fmp")
    assert profile.ticker == "ZZZ"
    assert count == 0
    assert len(calls) == 2
    assert [c[0] for c in calls] == [
        "https://financialmodelingprep.com/stable/etf/info",
        "https://financialmodelingprep.com/stable/etf/holdings",
    ]
    for _, kw in calls:
        assert kw["params"] == {"symbol": "ZZZ"}
        assert kw["api_key"] == "synthetic-key"
        assert kw["expected"] is JsonShape.ANY
        assert kw["timeout"] == (10, 60)
        assert kw["retain_capture"] is True


def test_ingest_live_network_error_no_fallback(
    tmp_path: Path, migrated_db: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    def wire(url: str, **kwargs: object) -> HttpJsonResponse:
        calls.append(url)
        raise HttpCallError(
            kind=HttpErrorKind.NETWORK,
            message="failure?apikey=synthetic-key",
            retryable=True,
        )

    monkeypatch.setattr(FMP_CLIENT, "get_url_json", wire)
    monkeypatch.setenv("FMP_API_KEY", "synthetic-key")
    fmp_dir = tmp_path / "fmp"
    with sqlite3.connect(migrated_db(tmp_path / "test.db")) as conn:
        conn.row_factory = sqlite3.Row
        with pytest.raises(RuntimeError) as excinfo:
            ingest_live(conn, "ZZZ", fmp_dir)
    assert len(calls) == 1
    assert "synthetic-key" not in str(excinfo.value)
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__suppress_context__ is True
    assert not fmp_dir.exists()
