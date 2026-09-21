"""Real captured-feed fixtures exercise admission, revisions and atomic projection."""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from provenance.evidence_ledger import ContentBlob, EvidenceLedger, SourceObservation
from provenance.issuer_registry import (
    AuthoritySurfaceRevision,
    IssuerEntity,
    IssuerRegistry,
    LegacyIssuerBindingRevision,
)
from signals.ir_event_discovery import discover_ir_events
from signals.ir_events import (
    IREventRunResult,
    generate_event_id,
    generate_revision_id,
    record_ir_events_batch,
)

NOW = datetime(2026, 9, 19, 12, tzinfo=UTC)
URL = "https://ir.example.com/events.ics"


def feed(*, day: str = "20261102", status: str = "CONFIRMED", uid: str = "day-2026") -> bytes:
    return f"BEGIN:VCALENDAR\nVERSION:2.0\nBEGIN:VEVENT\nUID:{uid}\nSUMMARY:Investor Day\nCATEGORIES:Investor Day\nDTSTART;VALUE=DATE:{day}\nURL:https://ir.example.com/{uid}\nSTATUS:{status}\nEND:VEVENT\nEND:VCALENDAR".encode()


@pytest.fixture
def event_db(tmp_path: Path, migrated_db: Callable[..., Path]) -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(migrated_db(tmp_path / "events.db"))
    conn.execute("PRAGMA foreign_keys=ON")
    registry = IssuerRegistry(conn)
    registry.persist(
        IssuerEntity(
            issuer_id="issuer-acme",
            idempotency_key="issuer-acme",
            entity_kind="operating_company",
            created_at=NOW,
        )
    )
    registry.persist(
        LegacyIssuerBindingRevision(
            binding_revision_id="binding-1",
            idempotency_key="binding-1",
            recorded_issuer_id="legacy-ticker:ACME",
            revision=1,
            issuer_id="issuer-acme",
            outcome="selected",
            decision_kind="deterministic",
            material_dissent=False,
            effective_at=NOW,
            knowledge_at=NOW,
            recorded_at=NOW,
            reason_code="fixture",
            reason_details=(("ticker", "ACME"),),
        )
    )
    conn.commit()
    yield conn
    conn.close()


def capture(
    conn: sqlite3.Connection,
    tmp_path: Path,
    raw: bytes,
    *,
    revision: int = 1,
    surface: str = "events",
    now: datetime = NOW,
) -> None:
    digest = hashlib.sha256(raw).hexdigest()
    blob = tmp_path / digest
    blob.write_bytes(raw)
    obs = f"obs-{surface}-{revision}"
    ledger = EvidenceLedger(conn)
    ledger.persist(
        ContentBlob(
            sha256=digest,
            byte_size=len(raw),
            media_type="text/calendar",
            storage_uri=blob.as_uri(),
            recorded_at=now,
        )
    )
    ledger.persist(
        SourceObservation(
            observation_id=obs,
            idempotency_key=obs,
            source_kind="ir",
            source_url=URL,
            blob_sha256=digest,
            source_published_at=None,
            filing_at=None,
            accepted_at=None,
            observed_at=now,
            retrieved_at=now,
            retrieval_config_sha256="a" * 64,
            collector_code_version="offline-fixture",
        )
    )
    IssuerRegistry(conn).persist(
        AuthoritySurfaceRevision(
            surface_revision_id=f"{surface}-{revision}",
            idempotency_key=f"{surface}-{revision}",
            issuer_id="issuer-acme",
            surface_key=surface,
            revision=revision,
            surface_kind="ir_events",
            source_url=URL,
            status="verified",
            authority_level="publisher",
            source_observation_id=obs,
            verification_method="fixture_publisher_identity",
            effective_at=now,
            knowledge_at=now,
            recorded_at=now,
            supersedes_surface_revision_id=f"{surface}-{revision - 1}" if revision > 1 else None,
        )
    )
    conn.commit()


def ingest(conn: sqlite3.Connection, *, now: datetime = NOW, dry: bool = False) -> IREventRunResult:
    discovery = discover_ir_events(conn, ("ACME",), now=now)
    return record_ir_events_batch(
        conn,
        discovery.events,
        attempts=discovery.attempts,
        tickers=discovery.tickers,
        now=now,
        mode="dry_run" if dry else "apply",
    )


def test_event_id_and_revision_id_are_deterministic() -> None:
    assert generate_event_id("a", "investor_day", "one") == generate_event_id(
        "a", "investor_day", "one"
    )
    assert generate_event_id("a", "investor_day", "one") != generate_event_id(
        "a", "investor_day", "two"
    )
    assert generate_revision_id("a", {"date": "2026-10-01"}, "obs") != generate_revision_id(
        "a", {"date": "2026-10-02"}, "obs"
    )


def test_lifecycle_preserves_revisions_and_replays(
    event_db: sqlite3.Connection, tmp_path: Path
) -> None:
    capture(event_db, tmp_path, feed())
    assert ingest(event_db).inserted == 1
    assert ingest(event_db).replayed == 1
    capture(event_db, tmp_path, feed(day="20261103"), revision=2, now=NOW + timedelta(hours=1))
    assert ingest(event_db, now=NOW + timedelta(hours=1)).superseded == 1
    assert event_db.execute("SELECT event_date,cadence FROM signals").fetchall() == [
        ("2026-11-03", "scheduled")
    ]
    capture(
        event_db,
        tmp_path,
        feed(day="20261103", status="CANCELLED"),
        revision=3,
        now=NOW + timedelta(hours=2),
    )
    assert ingest(event_db, now=NOW + timedelta(hours=2)).cancelled == 1
    assert event_db.execute("SELECT COUNT(*) FROM signals").fetchone() == (0,)
    assert event_db.execute(
        "SELECT revision,supersedes_revision_id FROM ir_event_revisions ORDER BY revision"
    ).fetchall()[0] == (1, None)
    assert event_db.execute("SELECT COUNT(*) FROM ir_event_revisions").fetchone() == (3,)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        event_db.execute("DELETE FROM ir_event_revisions")
    event_db.rollback()


def test_two_distinct_same_day_events_survive(event_db: sqlite3.Connection, tmp_path: Path) -> None:
    # Construct two full VEVENT components in one exhausted calendar.
    raw = feed().replace(b"END:VCALENDAR", feed(uid="different").split(b"VERSION:2.0\n")[1])
    capture(event_db, tmp_path, raw)
    assert ingest(event_db).inserted == 2
    assert event_db.execute("SELECT COUNT(DISTINCT ir_event_id) FROM signals").fetchone() == (2,)


def test_dry_run_is_read_only_and_forged_observation_rejected(
    event_db: sqlite3.Connection, tmp_path: Path
) -> None:
    capture(event_db, tmp_path, feed())
    assert ingest(event_db, dry=True).inserted == 1
    assert event_db.execute("SELECT COUNT(*) FROM ir_event_runs").fetchone() == (0,)
    assert event_db.execute("SELECT COUNT(*) FROM ir_event_revisions").fetchone() == (0,)
    observation = discover_ir_events(event_db, ("ACME",), now=NOW).events[0]
    with pytest.raises(ValueError, match="publisher capture"):
        record_ir_events_batch(
            event_db, [observation.model_copy(update={"title": "Fabricated"})], now=NOW
        )


def test_failure_rolls_back_full_batch(event_db: sqlite3.Connection, tmp_path: Path) -> None:
    raw = feed().replace(b"END:VCALENDAR", feed(uid="different").split(b"VERSION:2.0\n")[1])
    capture(event_db, tmp_path, raw)
    event_db.execute(
        "CREATE TRIGGER fail_second BEFORE INSERT ON signals WHEN NEW.url LIKE '%different' BEGIN SELECT RAISE(ABORT,'injected failure'); END"
    )
    with pytest.raises(sqlite3.IntegrityError, match="injected failure"):
        ingest(event_db)
    assert event_db.execute("SELECT COUNT(*) FROM signals").fetchone() == (0,)
    assert event_db.execute("SELECT COUNT(*) FROM ir_event_revisions").fetchone() == (0,)
    assert event_db.execute("SELECT COUNT(*) FROM ir_event_runs").fetchone() == (0,)


def test_empty_unsupported_failed_stale_are_distinct(
    event_db: sqlite3.Connection, tmp_path: Path
) -> None:
    assert ingest(event_db).status == "error"
    capture(event_db, tmp_path, b"BEGIN:VCALENDAR\nVERSION:2.0\nEND:VCALENDAR")
    assert ingest(event_db).status == "empty"
    stale = ingest(event_db, now=NOW + timedelta(hours=37))
    assert stale.status == "error"
    assert stale.attempts[0].status == "stale"
    capture(
        event_db, tmp_path, b"<html>not a calendar</html>", revision=2, now=NOW + timedelta(hours=1)
    )
    failed = ingest(event_db, now=NOW + timedelta(hours=1))
    assert failed.status == "error"
    assert failed.attempts[0].status == "contract_error"


def test_absence_never_cancels_prior_event(event_db: sqlite3.Connection, tmp_path: Path) -> None:
    capture(event_db, tmp_path, feed())
    ingest(event_db)
    capture(
        event_db,
        tmp_path,
        b"BEGIN:VCALENDAR\nVERSION:2.0\nEND:VCALENDAR",
        revision=2,
        now=NOW + timedelta(hours=1),
    )
    assert ingest(event_db, now=NOW + timedelta(hours=1)).status == "empty"
    assert event_db.execute("SELECT COUNT(*) FROM signals").fetchone() == (1,)


def test_same_tier_disagreement_retained_without_projection(
    event_db: sqlite3.Connection, tmp_path: Path
) -> None:
    capture(event_db, tmp_path, feed())
    capture(event_db, tmp_path, feed(day="20261103"), surface="second")
    result = ingest(event_db)
    assert result.status == "partial"
    assert result.conflicts == 1
    assert event_db.execute("SELECT COUNT(*) FROM ir_event_revisions").fetchone() == (2,)
    assert event_db.execute("SELECT COUNT(*) FROM signals").fetchone() == (0,)


def test_admission_rejects_past_and_far_future(
    event_db: sqlite3.Connection, tmp_path: Path
) -> None:
    capture(event_db, tmp_path, feed(day="20260918"))
    assert ingest(event_db).dispositions[0].reason_code == "past_date"
    capture(event_db, tmp_path, feed(day="20301103"), revision=2, now=NOW + timedelta(hours=1))
    result = ingest(event_db, now=NOW + timedelta(hours=1))
    assert result.status == "partial"
    assert result.dispositions[0].reason_code == "date_beyond_548d_ceiling"


def test_tampered_capture_and_unbound_ticker_fail_closed(
    event_db: sqlite3.Connection, tmp_path: Path
) -> None:
    raw = feed()
    capture(event_db, tmp_path, raw)
    (tmp_path / hashlib.sha256(raw).hexdigest()).write_bytes(b"changed")
    result = discover_ir_events(event_db, ("ACME", "OTHER"), now=NOW)
    assert not result.events
    assert [attempt.status for attempt in result.attempts] == ["contract_error", "unsupported"]


def test_forward_coverage_does_not_extend_capture_freshness(
    event_db: sqlite3.Connection, tmp_path: Path
) -> None:
    from signals.ir_events import ir_calendar_coverage

    event_db.execute(
        "INSERT INTO tracked_companies(ticker,name,list_type) VALUES('ACME','Acme','portfolio')"
    )
    event_db.commit()
    capture(event_db, tmp_path, feed())
    ingest(event_db, now=NOW + timedelta(hours=35))
    assert ir_calendar_coverage(event_db, now=NOW + timedelta(hours=35))[0] == "fresh"
    assert ir_calendar_coverage(event_db, now=NOW + timedelta(hours=37))[0] == "stale"
    event_db.execute(
        "INSERT INTO tracked_companies(ticker,name,list_type) VALUES('OTHER','Other','portfolio')"
    )
    event_db.commit()
    assert ir_calendar_coverage(event_db, now=NOW)[0] == "unavailable"


def test_general_calendar_verification_proves_projection_or_reports_drift(
    event_db: sqlite3.Connection, tmp_path: Path
) -> None:
    from execution.verify_calendars import audit_calendars

    event_db.execute(
        "INSERT INTO tracked_companies(ticker,name,list_type) VALUES('ACME','Acme','portfolio')"
    )
    event_db.commit()
    capture(event_db, tmp_path, feed())
    ingest(event_db)
    db = Path(str(event_db.execute("PRAGMA database_list").fetchone()[2]))
    result = audit_calendars(db, today=NOW.date(), now=NOW)
    assert result.forward_events_count == 1
    assert result.forward_freshness == "fresh"
    assert result.integrity_pass, result.issues
    event_db.execute("UPDATE signals SET title='fabricated'")
    event_db.commit()
    failed = audit_calendars(db, today=NOW.date(), now=NOW)
    assert not failed.integrity_pass
    assert any("differs from retained" in issue for issue in failed.issues)


def test_replaying_conflict_cannot_turn_receipt_complete(
    event_db: sqlite3.Connection, tmp_path: Path
) -> None:
    capture(event_db, tmp_path, feed())
    capture(event_db, tmp_path, feed(day="20261103"), surface="second")
    assert ingest(event_db).status == "partial"
    assert ingest(event_db).status == "partial"


def test_general_calendar_render_keeps_rows_and_distinguishes_verified_empty(
    event_db: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pipeline.diet_panel import render_diet_panel
    from signals.store import ForwardAgendaResult, load_forward_agenda_result

    event_db.execute(
        "INSERT INTO tracked_companies(ticker,name,list_type) VALUES('ACME','Acme','portfolio')"
    )
    event_db.commit()
    capture(event_db, tmp_path, b"BEGIN:VCALENDAR\nVERSION:2.0\nEND:VCALENDAR")
    ingest(event_db)
    db = Path(str(event_db.execute("PRAGMA database_list").fetchone()[2]))
    empty = load_forward_agenda_result(db, on_or_after=NOW.date(), now=NOW)
    assert empty.freshness == "fresh"
    agenda = empty

    def fixed_agenda(*_args: object, **_kwargs: object) -> ForwardAgendaResult:
        return agenda

    monkeypatch.setattr("pipeline.diet_panel.load_forward_agenda_result", fixed_agenda)
    empty_html = render_diet_panel(db, today=NOW.date())
    assert 'data-calendar-state="empty"' in empty_html
    (tmp_path / "ir-forward-empty.html").write_text(empty_html)
    capture(event_db, tmp_path, feed(), revision=2, now=NOW + timedelta(hours=1))
    ingest(event_db, now=NOW + timedelta(hours=1))
    stale = load_forward_agenda_result(db, on_or_after=NOW.date(), now=NOW + timedelta(hours=38))
    agenda = stale
    html = render_diet_panel(db, today=NOW.date())
    (tmp_path / "ir-forward-stale.html").write_text(html)
    assert stale.freshness == "stale"
    assert 'data-calendar-state="incomplete"' in html
    assert "Investor Day" in html
    assert "https://ir.example.com/day-2026" in html


def test_legacy_writer_cannot_bypass_governed_history(event_db: sqlite3.Connection) -> None:
    from signals.store import record_investor_day

    with pytest.raises(RuntimeError, match="record_ir_events_batch"):
        record_investor_day(event_db, "ACME", NOW.date(), "Unproven event")


def test_real_cli_discovers_input_without_network(
    event_db: sqlite3.Connection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import json

    from execution import ingest_ir_events

    class FixtureClock:
        @staticmethod
        def now(_zone: object) -> datetime:
            return NOW

    monkeypatch.setattr(ingest_ir_events, "datetime", FixtureClock)
    event_db.execute(
        "INSERT INTO tracked_companies(ticker,name,list_type) VALUES('ACME','Acme','portfolio')"
    )
    event_db.commit()
    capture(event_db, tmp_path, feed())
    db = Path(str(event_db.execute("PRAGMA database_list").fetchone()[2]))
    assert ingest_ir_events.main(["--db", str(db), "--dry-run", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "complete"
    assert payload["events"][0]["ticker"] == "ACME"
    assert payload["events"][0]["source_observation_id"] == "obs-events-1"
    assert event_db.execute("SELECT COUNT(*) FROM signals").fetchone() == (0,)

    monkeypatch.setenv("IR_EVENTS_APPLY_ENABLED", "1")
    assert ingest_ir_events.main(["--db", str(db), "--apply", "--json"]) == 0
    applied = json.loads(capsys.readouterr().out)
    assert applied["inserted"] == 1
    assert event_db.execute("SELECT COUNT(*) FROM ir_event_runs").fetchone() == (1,)
    assert not db.with_name(db.name + ".write.lock").exists()


def test_forged_empty_success_and_omitted_events_are_rejected(
    event_db: sqlite3.Connection, tmp_path: Path
) -> None:
    capture(event_db, tmp_path, feed())
    discovered = discover_ir_events(event_db, ("ACME",), now=NOW)
    with pytest.raises(ValueError, match="reconcile"):
        record_ir_events_batch(
            event_db, [], attempts=discovered.attempts, tickers=("ACME",), now=NOW
        )
    with pytest.raises(ValueError, match="reconcile"):
        record_ir_events_batch(
            event_db,
            discovered.events,
            attempts=[
                discovered.attempts[0].model_copy(update={"observed_at": NOW + timedelta(hours=1)})
            ],
            tickers=("ACME",),
            now=NOW,
        )


def test_empty_capture_does_not_resolve_prior_authority_conflict(
    event_db: sqlite3.Connection, tmp_path: Path
) -> None:
    capture(event_db, tmp_path, feed())
    capture(event_db, tmp_path, feed(day="20261103"), surface="second")
    assert ingest(event_db).status == "partial"
    empty = b"BEGIN:VCALENDAR\nVERSION:2.0\nEND:VCALENDAR"
    capture(event_db, tmp_path, empty, revision=2, now=NOW + timedelta(hours=1))
    capture(event_db, tmp_path, empty, revision=2, surface="second", now=NOW + timedelta(hours=1))
    assert ingest(event_db, now=NOW + timedelta(hours=1)).status == "partial"


def test_upgrade_preserves_legacy_calendar_rows(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    def seed(path: Path) -> None:
        with sqlite3.connect(path) as conn:
            conn.execute(
                "INSERT INTO signals(ticker,signal_type,title,event_date,published_at,created_at,cadence) VALUES('ACME','investor_day','Legacy event','2026-11-02','2026-09-01','2026-09-01','scheduled')"
            )

    db = migrated_db(
        tmp_path / "upgrade.db", upgrade_from="0040_fmp_watchlist_recovery", before_upgrade=seed
    )
    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT ticker,title,ir_event_id,ir_event_revision_id FROM signals"
        ).fetchall() == [("ACME", "Legacy event", None, None)]
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_credential_bearing_event_url_is_not_retained_in_receipt(
    event_db: sqlite3.Connection, tmp_path: Path
) -> None:
    raw = feed().replace(
        b"URL:https://ir.example.com/day-2026",
        b"URL:https://ir.example.com/day-2026?token=fixture-sensitive",
    )
    capture(event_db, tmp_path, raw)
    result = ingest(event_db)
    assert result.status == "error"
    assert not result.events
    assert "fixture-sensitive" not in result.model_dump_json()
