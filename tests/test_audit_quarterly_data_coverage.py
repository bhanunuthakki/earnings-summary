from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path
from typing import cast

import pytest

from execution import audit_quarterly_data_coverage as audit
from pipeline.data_coverage_dispositions import (
    CoverageArtifactKind,
    CoverageAttempt,
    CoverageAttemptStatus,
    CoverageDispositionStatus,
    DataCoverageDispositionRequest,
    append_data_coverage_disposition,
    policy_config_sha256,
)
from transcripts.acquisition_semantics import TRANSCRIPT_ACQUISITION_POLICY_VERSION

TICKERS = ["BKNG", "BN", "MELI", "META", "NOW", "NU", "NVO", "RBRK", "UBER", "VEEV", "WIX"]


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE tracked_companies "
        "(ticker TEXT,list_type TEXT,archived_at TEXT,fiscal_year_end TEXT)"
    )
    conn.executemany(
        "INSERT INTO tracked_companies VALUES (?,'portfolio',NULL,'12-31')",
        ((ticker,) for ticker in TICKERS),
    )
    return conn


def _evidence(reference: str) -> dict[str, object]:
    return {
        "complete": True,
        "verification_reason_codes": [],
        "evidence_reference": reference,
        "evidence_sha256": "a" * 64,
        "transcript_id": 1,
    }


def _complete_evidence(_conn: sqlite3.Connection, **_kwargs: object) -> dict[str, object]:
    return _evidence("complete")


def _no_disposition(_conn: sqlite3.Connection, **_kwargs: object) -> dict[str, object] | None:
    return None


def test_reporting_season_maps_non_calendar_fye_to_exact_issuer_quarter() -> None:
    assert audit._season_target(  # pyright: ignore[reportPrivateUsage] - direct unit seam
        reporting_year=2026, reporting_quarter=2, fye_month=12
    ) == (
        2026,
        2,
        date(2026, 6, 30),
    )
    assert audit._season_target(  # pyright: ignore[reportPrivateUsage] - direct unit seam
        reporting_year=2026, reporting_quarter=2, fye_month=1
    ) == (
        2027,
        1,
        date(2026, 4, 30),
    )


def test_audit_covers_exact_33_targets_with_deterministic_report_sha(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = _conn()
    monkeypatch.setattr(audit, "_transcript_evidence", _complete_evidence)
    monkeypatch.setattr(audit, "_commitment_scan_evidence", _complete_evidence)
    monkeypatch.setattr(audit, "_surprise_evidence", _complete_evidence)
    monkeypatch.setattr(audit, "_latest_disposition", _no_disposition)
    cutoff = datetime(2026, 9, 5, tzinfo=UTC)

    first, first_rc = audit.audit_cohort(
        conn,
        repo_root=tmp_path,
        fiscal_year=2026,
        fiscal_quarter=2,
        expected_tickers=list(reversed(TICKERS)),
        cutoff_at=cutoff,
    )
    second, second_rc = audit.audit_cohort(
        conn,
        repo_root=tmp_path,
        fiscal_year=2026,
        fiscal_quarter=2,
        expected_tickers=TICKERS,
        cutoff_at=cutoff,
    )

    assert first_rc == second_rc == 0
    assert first == second
    assert first["target_count"] == 33
    assert first["counts"] == {
        "evidence_complete": 33,
        "accepted_truthful_disposition": 0,
        "actionable": 0,
        "missing": 0,
        "invalid": 0,
    }
    report_sha = str(first.pop("report_sha256"))
    canonical = json.dumps(first, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    assert report_sha == hashlib.sha256(canonical.encode()).hexdigest()


def test_false_satisfied_ledger_is_invalid_but_truthful_gap_is_distinct() -> None:
    missing: dict[str, object] = {
        "complete": False,
        "evidence_reference": None,
        "evidence_sha256": None,
    }
    satisfied: dict[str, object] = {
        "valid": True,
        "status": "satisfied",
        "evidence_reference": "fabricated",
        "evidence_sha256": "a" * 64,
        "verification_reason_codes": [],
    }
    truthful: dict[str, object] = {
        **satisfied,
        "status": "repair_evidence_missing",
        "evidence_reference": None,
        "evidence_sha256": None,
    }

    assert (
        audit._classify_artifact(  # pyright: ignore[reportPrivateUsage] - direct unit seam
            missing, satisfied
        )[0]
        == "invalid"
    )
    assert audit._classify_artifact(  # pyright: ignore[reportPrivateUsage] - direct unit seam
        missing, truthful
    ) == (
        "accepted_truthful_disposition",
        [],
    )


def test_rc_one_distinguishes_truthfully_dispositioned_gap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = _conn()
    monkeypatch.setattr(audit, "_transcript_evidence", _complete_evidence)
    monkeypatch.setattr(audit, "_commitment_scan_evidence", _complete_evidence)

    def surprise(_conn: sqlite3.Connection, **kwargs: object) -> dict[str, object]:
        return (
            {
                "complete": False,
                "evidence_reference": None,
                "evidence_sha256": None,
                "verification_reason_codes": ["surprise_observation_projection_missing"],
            }
            if kwargs["ticker"] == "BN"
            else _evidence("e")
        )

    def disposition(_conn: sqlite3.Connection, **kwargs: object) -> dict[str, object] | None:
        if (
            kwargs["ticker"] != "BN"
            or kwargs["artifact_kind"] is not audit.CoverageArtifactKind.EARNINGS_SURPRISE
        ):
            return None
        return {
            "valid": True,
            "status": "provider_coverage_gap",
            "evidence_reference": None,
            "evidence_sha256": None,
            "verification_reason_codes": [],
        }

    monkeypatch.setattr(audit, "_surprise_evidence", surprise)
    monkeypatch.setattr(audit, "_latest_disposition", disposition)

    report, rc = audit.audit_cohort(
        conn,
        repo_root=tmp_path,
        fiscal_year=2026,
        fiscal_quarter=2,
        expected_tickers=TICKERS,
        cutoff_at=datetime(2026, 9, 5, tzinfo=UTC),
    )

    assert rc == 1
    assert report["outcome"] == "closed_with_accounted_dispositions"
    counts = cast(dict[str, int], report["counts"])
    assert counts["accepted_truthful_disposition"] == 1


def test_roster_mismatch_cannot_close(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    conn = _conn()
    monkeypatch.setattr(audit, "_transcript_evidence", _complete_evidence)
    monkeypatch.setattr(audit, "_commitment_scan_evidence", _complete_evidence)
    monkeypatch.setattr(audit, "_surprise_evidence", _complete_evidence)
    monkeypatch.setattr(audit, "_latest_disposition", _no_disposition)

    report, rc = audit.audit_cohort(
        conn,
        repo_root=tmp_path,
        fiscal_year=2026,
        fiscal_quarter=2,
        expected_tickers=[*TICKERS[:-1], "AMZN"],
        cutoff_at=datetime(2026, 9, 5, tzinfo=UTC),
    )

    assert rc == 2
    assert report["roster_matches"] is False
    assert report["target_count"] == 33


def test_latest_disposition_rejects_wrong_target_policy_attempts_and_identities(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    db_path = migrated_db(tmp_path / "portfolio.db")
    attempts_json = "[]"
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute(
            "INSERT INTO data_coverage_dispositions ("
            "disposition_id,idempotency_key,artifact_kind,ticker,fiscal_year,fiscal_quarter,"
            "period_end,status,reason_code,attempts_json,attempts_sha256,policy_name,"
            "policy_version,policy_config_sha256,evidence_reference,evidence_sha256,"
            "operation_id,observed_at,retry_after,revision,supersedes_disposition_id,recorded_at"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "d" * 64,
                "e" * 64,
                "text_transcript",
                "BN",
                2026,
                2,
                "2026-05-31",
                "policy_blocked",
                "transcript_source_policy_denied",
                attempts_json,
                hashlib.sha256(attempts_json.encode()).hexdigest(),
                "stale_transcript_policy",
                "2025-01-01.1",
                "f" * 64,
                None,
                None,
                None,
                "2026-09-01T00:00:00.000000Z",
                None,
                1,
                None,
                "2026-09-01T00:00:01.000000Z",
            ),
        )
        disposition = audit._latest_disposition(  # pyright: ignore[reportPrivateUsage]
            conn,
            ticker="BN",
            fiscal_year=2026,
            fiscal_quarter=2,
            period_end=date(2026, 6, 30),
            artifact_kind=audit.CoverageArtifactKind.TEXT_TRANSCRIPT,
            cutoff_at=datetime(2026, 9, 5, tzinfo=UTC),
        )

    assert disposition is not None
    reasons = cast(list[str], disposition["verification_reason_codes"])
    assert {
        "attempts_semantically_insufficient",
        "disposition_id_mismatch",
        "idempotency_key_mismatch",
        "period_end_mismatch",
        "policy_config_sha256_mismatch",
        "policy_identity_stale",
    } <= set(reasons)
    assert disposition["valid"] is False


def test_latest_disposition_accepts_exact_current_policy_and_canonical_identities(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    db_path = migrated_db(tmp_path / "portfolio.db")
    providers = ("issuer_ir", "roic", "stockanalysis", "tickertrends")
    observed_at = datetime(2026, 9, 1, tzinfo=UTC)
    authorization_keys = audit._transcript_authorization_keys(  # pyright: ignore[reportPrivateUsage]
        ticker="BN",
        fiscal_year=2026,
        fiscal_quarter=2,
        period_end=date(2026, 6, 30),
    )
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        append_data_coverage_disposition(
            conn,
            DataCoverageDispositionRequest(
                artifact_kind=CoverageArtifactKind.TEXT_TRANSCRIPT,
                ticker="BN",
                fiscal_year=2026,
                fiscal_quarter=2,
                period_end=date(2026, 6, 30),
                status=CoverageDispositionStatus.SOURCE_UNAVAILABLE,
                reason_code="authorized_text_transcript_unavailable",
                attempts=(
                    CoverageAttempt(
                        provider="issuer_ir",
                        status=CoverageAttemptStatus.AUTHORIZED_MISS,
                        authorization_key=authorization_keys["issuer_ir"],
                    ),
                    *(
                        CoverageAttempt(
                            provider=provider,
                            status=CoverageAttemptStatus.POLICY_DENIED,
                            authorization_key=authorization_keys[provider],
                        )
                        for provider in providers[1:]
                    ),
                ),
                policy_name="transcript_acquisition",
                policy_version=TRANSCRIPT_ACQUISITION_POLICY_VERSION,
                policy_config_sha256=policy_config_sha256(
                    policy_name="transcript_acquisition",
                    policy_version=TRANSCRIPT_ACQUISITION_POLICY_VERSION,
                    providers=providers,
                ),
                evidence_reference=None,
                evidence_sha256=None,
                observed_at=observed_at,
                retry_after=datetime(2026, 9, 12, tzinfo=UTC),
            ),
            recorded_at=observed_at,
        )
        disposition = audit._latest_disposition(  # pyright: ignore[reportPrivateUsage]
            conn,
            ticker="BN",
            fiscal_year=2026,
            fiscal_quarter=2,
            period_end=date(2026, 6, 30),
            artifact_kind=CoverageArtifactKind.TEXT_TRANSCRIPT,
            cutoff_at=datetime(2026, 9, 5, tzinfo=UTC),
        )

    assert disposition is not None
    assert disposition["verification_reason_codes"] == []
    assert disposition["valid"] is True


def test_repair_disposition_rejects_acquired_attempts() -> None:
    attempts = tuple(
        CoverageAttempt(provider=provider, status=CoverageAttemptStatus.ACQUIRED)
        for provider in ("issuer_ir", "roic", "stockanalysis", "tickertrends")
    )

    assert not audit._attempts_are_sufficient(  # pyright: ignore[reportPrivateUsage]
        artifact_kind=CoverageArtifactKind.TEXT_TRANSCRIPT,
        status=CoverageDispositionStatus.REPAIR_EVIDENCE_MISSING,
        reason_code="canonical_transcript_evidence_missing",
        attempts=attempts,
    )
    assert audit._reason_matches_status(  # pyright: ignore[reportPrivateUsage]
        artifact_kind=CoverageArtifactKind.TEXT_TRANSCRIPT,
        status=CoverageDispositionStatus.SATISFIED,
        reason_code="authorized_processed_transcript_with_segments",
    )


def test_reacquired_canonical_collision_is_valid_but_actionable() -> None:
    attempts = (
        CoverageAttempt(
            provider="issuer_ir",
            status=CoverageAttemptStatus.ACQUIRED,
            authorization_key="transcript:" + "a" * 64,
        ),
        CoverageAttempt(
            provider="canonical_processed_path",
            status=CoverageAttemptStatus.FAILED,
        ),
    )
    reason = "reacquired_transcript_conflicts_with_canonical_bytes"

    assert audit._attempts_are_sufficient(  # pyright: ignore[reportPrivateUsage]
        artifact_kind=CoverageArtifactKind.TEXT_TRANSCRIPT,
        status=CoverageDispositionStatus.OPERATIONAL_ERROR,
        reason_code=reason,
        attempts=attempts,
    )
    assert audit._reason_matches_status(  # pyright: ignore[reportPrivateUsage]
        artifact_kind=CoverageArtifactKind.TEXT_TRANSCRIPT,
        status=CoverageDispositionStatus.OPERATIONAL_ERROR,
        reason_code=reason,
    )
    closure, reasons = audit._classify_artifact(  # pyright: ignore[reportPrivateUsage]
        {"complete": False},
        {"valid": True, "status": "operational_error"},
    )
    assert closure == "actionable"
    assert reasons == ["disposition_not_accepted_for_closure"]


def test_legacy_surprise_projection_is_not_source_complete(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    def seed_legacy(path: Path) -> None:
        with sqlite3.connect(path) as conn:
            conn.execute(
                "INSERT INTO tracked_companies (ticker,name,list_type,fiscal_year_end) "
                "VALUES ('BN','Brookfield','portfolio','12-31')"
            )
            conn.execute(
                "INSERT INTO earnings_surprises ("
                "ticker,release_date,eps_estimate,eps_actual,source_name,fetched_at,ingested_at"
                ") VALUES (?,?,?,?,?,?,?)",
                (
                    "BN",
                    "2026-08-01",
                    "1.0",
                    "1.1",
                    "fmp_calendar",
                    "2026-08-01T20:00:00+00:00",
                    "2026-08-01T20:01:00+00:00",
                ),
            )

    db_path = migrated_db(
        tmp_path / "legacy-surprise.db",
        upgrade_from="0006_add_ask_proposal_approval",
        before_upgrade=seed_legacy,
        target="head",
    )
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        evidence = audit._surprise_evidence(  # pyright: ignore[reportPrivateUsage]
            conn,
            ticker="BN",
            period_end=date(2026, 6, 30),
            cutoff_at=datetime(2026, 9, 5, tzinfo=UTC),
        )

    assert evidence["complete"] is False
    reasons = cast(list[str], evidence["verification_reason_codes"])
    assert "surprise_provenance_not_source_observed" in reasons


def test_cli_audits_real_head_read_only_and_emits_all_obligations(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = migrated_db(tmp_path / "state" / "data" / "portfolio.db")
    with sqlite3.connect(db_path) as conn:
        conn.executemany(
            "INSERT INTO tracked_companies (ticker,name,list_type,fiscal_year_end) "
            "VALUES (?,?, 'portfolio','12-31')",
            ((ticker, ticker) for ticker in TICKERS),
        )
    args = [
        "--repo-root",
        str(tmp_path / "state"),
        "--db",
        str(db_path),
        "--fiscal-year",
        "2026",
        "--fiscal-quarter",
        "2",
        "--cutoff-at",
        "2026-09-05T23:59:59Z",
    ]
    for ticker in TICKERS:
        args.extend(["--ticker", ticker])

    assert audit.main(args) == 2
    report = json.loads(capsys.readouterr().out)
    assert report["target_count"] == 33
    assert len(report["tickers"]) == 11
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM data_coverage_dispositions").fetchone() == (0,)
        assert conn.execute("SELECT COUNT(*) FROM commitment_scan_receipts").fetchone() == (0,)
