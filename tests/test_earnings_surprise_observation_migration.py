"""Migration contract for immutable earnings-surprise observations."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

import pytest
from alembic.config import Config

from alembic import command
from earnings_surprise_store import EarningsSurpriseRecordV1, append_observation
from execution.ingest_earnings_surprises import ingest_one_ticker

ROOT = Path(__file__).resolve().parents[1]
REVISION = "0007_add_earnings_surprise_observations"
ACTIVE_HEAD = "0024_add_operations_attention_findings"


def _config(path: Path) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path.as_posix()}")
    return config


def _observation_values() -> tuple[object, ...]:
    digest = "a" * 64
    return (
        digest,
        f"earnings-surprise-observation:{digest}",
        "WIX",
        "2026-08-06",
        "2.1",
        "2.3",
        None,
        None,
        "9.52",
        None,
        None,
        None,
        "fmp_calendar",
        "https://example.com/source",
        "2026-08-06T20:00:00+00:00",
        "data/surprise/WIX_surprises.json",
        0,
        "{}",
        "b" * 64,
        "{}",
        "c" * 64,
        "source_observed",
        "run:test",
        "generation:test",
        "2026-08-06T20:01:00+00:00",
    )


def test_upgrade_creates_immutable_ledgers_and_projection_lineage(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    path = migrated_db(tmp_path / "earnings-observations.db", target="head")

    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
            ACTIVE_HEAD,
        )
        tables = {
            str(row[0])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert {
            "earnings_surprise_observations",
            "earnings_surprise_quarantine",
        } <= tables
        columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(earnings_surprises)")
        }
        assert "source_observation_id" in columns
        values = _observation_values()
        placeholders = ",".join("?" for _ in values)
        connection.execute(
            f"INSERT INTO earnings_surprise_observations VALUES ({placeholders})", values
        )
        connection.commit()
        with pytest.raises(sqlite3.IntegrityError, match="observations are immutable"):
            connection.execute("UPDATE earnings_surprise_observations SET source_name='changed'")
        with pytest.raises(sqlite3.IntegrityError, match="observations are immutable"):
            connection.execute("DELETE FROM earnings_surprise_observations")
        with pytest.raises(sqlite3.IntegrityError, match="requires source observation"):
            connection.execute(
                """
                INSERT INTO earnings_surprises (
                    ticker,release_date,source_name,fetched_at,source_observation_id
                ) VALUES ('NU','2026-08-06','fmp_calendar',
                          '2026-08-06T20:00:00+00:00',NULL)
                """
            )
        connection.execute(
            """
            INSERT INTO earnings_surprises (
                ticker,release_date,eps_estimate,eps_actual,revenue_estimate,
                revenue_actual,eps_surprise_pct,revenue_surprise_pct,
                num_analysts_eps,num_analysts_revenue,source_name,source_url,
                fetched_at,source_observation_id
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "WIX",
                "2026-08-06",
                "2.1",
                "2.3",
                None,
                None,
                "9.52",
                None,
                None,
                None,
                "fmp_calendar",
                "https://example.com/source",
                "2026-08-06T20:00:00+00:00",
                "a" * 64,
            ),
        )
        with pytest.raises(sqlite3.IntegrityError, match="requires source observation"):
            connection.execute("UPDATE earnings_surprises SET source_observation_id=?", ("d" * 64,))


def test_upgrade_backfills_and_links_existing_projection_deterministically(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    def seed_legacy_projection(path: Path) -> None:
        with sqlite3.connect(path) as connection:
            connection.execute(
                """
                INSERT INTO earnings_surprises (
                    ticker,release_date,eps_estimate,eps_actual,revenue_estimate,
                    revenue_actual,eps_surprise_pct,revenue_surprise_pct,
                    num_analysts_eps,num_analysts_revenue,source_name,source_url,
                    fetched_at,ingested_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    "WIX",
                    "2026-08-06",
                    "2.10",
                    "2.30",
                    None,
                    None,
                    "9.52",
                    None,
                    8,
                    None,
                    "fmp_calendar",
                    None,
                    "2026-08-06T20:00:00+00:00",
                    "2026-08-06T20:01:00+00:00",
                ),
            )

    paths = [
        migrated_db(
            tmp_path / f"legacy-{n}.db",
            upgrade_from="0006_add_ask_proposal_approval",
            before_upgrade=seed_legacy_projection,
            target=REVISION,
        )
        for n in range(2)
    ]
    ids: list[str] = []
    for path in paths:
        with sqlite3.connect(path) as connection:
            row = connection.execute(
                """
                SELECT p.source_observation_id,o.observation_id,o.provenance_status,
                       o.ingestion_run_id,o.cache_generation_id,o.raw_payload_json,
                       o.canonical_payload_json
                FROM earnings_surprises p
                JOIN earnings_surprise_observations o
                  ON o.observation_id=p.source_observation_id
                """
            ).fetchone()
            assert row is not None
            assert row[0] == row[1]
            assert row[2] == "legacy_projection_uncertain"
            assert row[3] == "migration:0007"
            assert str(row[4]).startswith("legacy-projection:")
            assert json.loads(str(row[5]))["provenance_status"] == ("legacy_projection_uncertain")
            assert sha256(str(row[6]).encode()).hexdigest() == row[1]
            ids.append(str(row[1]))
    assert ids[0] == ids[1]


def test_projection_trigger_requires_null_safe_equality_for_every_observed_field(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    path = migrated_db(tmp_path / "projection-equality.db", target="head")
    columns = (
        "eps_estimate",
        "eps_actual",
        "revenue_estimate",
        "revenue_actual",
        "eps_surprise_pct",
        "revenue_surprise_pct",
        "num_analysts_eps",
        "num_analysts_revenue",
        "source_name",
        "source_url",
        "fetched_at",
    )
    good = dict(
        zip(
            columns,
            (
                "2.1",
                "2.3",
                None,
                None,
                "9.52",
                None,
                None,
                None,
                "fmp_calendar",
                "https://example.com/source",
                "2026-08-06T20:00:00+00:00",
            ),
            strict=True,
        )
    )
    mismatches: dict[str, object] = {
        "eps_estimate": "99",
        "eps_actual": None,
        "revenue_estimate": "1",
        "revenue_actual": "1",
        "eps_surprise_pct": "99",
        "revenue_surprise_pct": "1",
        "num_analysts_eps": 1,
        "num_analysts_revenue": 1,
        "source_name": "other",
        "source_url": None,
        "fetched_at": "2026-08-06T20:00:01+00:00",
    }
    with sqlite3.connect(path) as connection:
        values = _observation_values()
        connection.execute(
            """
            INSERT INTO earnings_surprise_observations (
                observation_id,idempotency_key,ticker,release_date,eps_estimate,
                eps_actual,revenue_estimate,revenue_actual,eps_surprise_pct,
                revenue_surprise_pct,num_analysts_eps,num_analysts_revenue,
                source_name,source_url,fetched_at,cache_path,record_ordinal,
                raw_payload_json,raw_payload_sha256,canonical_payload_json,
                canonical_payload_sha256,provenance_status,ingestion_run_id,
                cache_generation_id,recorded_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            values,
        )
        for column, bad_value in mismatches.items():
            candidate = {**good, column: bad_value}
            with pytest.raises(sqlite3.IntegrityError, match="requires source observation"):
                connection.execute(
                    f"""
                    INSERT INTO earnings_surprises (
                        ticker,release_date,{",".join(columns)},source_observation_id
                    ) VALUES ({",".join("?" for _ in range(14))})
                    """,
                    ("WIX", "2026-08-06", *(candidate[name] for name in columns), "a" * 64),
                )


def test_ingest_projects_the_exact_observation_clock_lexeme(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    path = migrated_db(tmp_path / "earnings-ingest-clock.db", target="head")
    cache_path = tmp_path / "data" / "surprise" / "WIX_surprises.json"
    cache_path.parent.mkdir(parents=True)
    source_record = {
        "ticker": "WIX",
        "release_date": "2026-08-06",
        "eps_estimate": "2.1",
        "eps_actual": "2.3",
        "revenue_estimate": None,
        "revenue_actual": None,
        "eps_surprise_pct": "9.52",
        "revenue_surprise_pct": None,
        "num_analysts_eps": None,
        "num_analysts_revenue": None,
        "source_name": "fmp_calendar",
        "source_url": "https://example.com/source",
        "fetched_at": "2026-08-06T20:00:00Z",
    }
    cache_path.write_text(
        json.dumps(
            {
                "ticker": "WIX",
                "generated_at": "2026-08-06T20:00:00Z",
                "record_count": 1,
                "records": [source_record],
            }
        ),
        encoding="utf-8",
    )

    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        record = EarningsSurpriseRecordV1.from_source(source_record, ticker_hint="WIX")
        append_observation(
            connection,
            record=record,
            raw_payload=source_record,
            cache_path=str(cache_path),
            record_ordinal=0,
            recorded_at=datetime(2026, 8, 6, 20, 1, tzinfo=UTC),
            ingestion_run_id="test:preexisting-clock",
            cache_generation_id="test:preexisting-generation",
        )
        connection.commit()
        preexisting_clock = connection.execute(
            "SELECT fetched_at FROM earnings_surprise_observations"
        ).fetchone()
        assert preexisting_clock is not None
        assert preexisting_clock["fetched_at"] == "2026-08-06T20:00:00+00:00"

        result = ingest_one_ticker(
            connection,
            cache_path,
            dry_run=False,
            recorded_at=datetime(2026, 8, 6, 20, 1, tzinfo=UTC),
            wall_clock=datetime(2026, 8, 6, 20, 1, tzinfo=UTC),
            ingestion_run_id="test:clock-lexeme",
        )
        connection.commit()
        row = connection.execute(
            """
            SELECT p.fetched_at AS projection_clock,
                   o.fetched_at AS observation_clock
            FROM earnings_surprises AS p
            JOIN earnings_surprise_observations AS o
              ON o.observation_id = p.source_observation_id
            WHERE p.ticker = 'WIX' AND p.release_date = '2026-08-06'
            """
        ).fetchone()

    assert result.errors == []
    assert row is not None
    assert row["projection_clock"] == row["observation_clock"]
    assert row["projection_clock"] == "2026-08-06T20:00:00+00:00"


def test_downgrade_removes_only_earnings_governance_surface(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    path = migrated_db(tmp_path / "earnings-observations-downgrade.db", target="head")
    config = _config(path)
    command.downgrade(config, "0006_add_ask_proposal_approval")

    with sqlite3.connect(path) as connection:
        tables = {
            str(row[0])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert "earnings_surprise_observations" not in tables
        assert "earnings_surprise_quarantine" not in tables
        assert "earnings_surprises" in tables
        columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(earnings_surprises)")
        }
        assert "source_observation_id" not in columns
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
