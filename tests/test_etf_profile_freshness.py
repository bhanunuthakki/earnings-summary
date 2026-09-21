"""Real parser/store/consumer freshness contracts using isolated migrated state."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from etf_sources.ingest import apply_characteristics
from etf_sources.issuer_registry import IssuerCharacteristics
from execution.fetch_etf_data import parse_etf_info
from instrument_store import get_etf_profile, upsert_etf_profile
from pipeline.work_os_evaluation import build_etf_profile_inputs
from research.investment_profile import EtfProfileInputs, derive_etf_label_evidence


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
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    with sqlite3.connect(migrated_db(tmp_path / "test.db")) as conn:
        conn.row_factory = sqlite3.Row
        upsert_etf_profile(
            conn, parse_etf_info({"sector": "Energy", "yield": 3.5}, "VDE", datetime.now(UTC))
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
        profile = parse_etf_info({"sector": "Energy"}, "VDE", capture)
        assert bool(admitted_profile_fields(profile, now=now)) is expected
    profile = parse_etf_info({"sector": "Energy", "asOfDate": "2026-09-19"}, "VDE", now)
    assert admitted_profile_fields(profile, now=now)
    assert not admitted_profile_fields(
        profile.model_copy(update={"sector_label": "Utilities"}), now=now
    )


def test_invalid_source_date_cannot_become_undated_admissible_evidence() -> None:
    from etf_sources.profile_evidence import admitted_profile_fields

    profile = parse_etf_info({"sector": "Energy", "asOfDate": "invalid"}, "VDE", datetime.now(UTC))
    assert not admitted_profile_fields(profile)
