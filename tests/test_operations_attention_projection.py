"""Read-only, safe Operations attention projection contract."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from operations.attention import EvidenceIdentity, EvidenceKind, FindingKind, derive_finding_id
from operations.attention_projection import build_attention_panel_view
from operations.registry import build_operations_registry
from operations.snapshot import collect_operations_snapshot
from pipeline.operations_panel import (
    OperationsPanelView,
    build_operations_panel_view,
    render_operations_panel,
)

NOW = datetime(2026, 8, 24, 19, 0, tzinfo=UTC)


def _finding_id(fingerprint: str, *, kind: FindingKind = FindingKind.RUNTIME_HEALTH) -> str:
    return derive_finding_id(
        owner="scheduler.collect_operations_runtime_observations",
        kind=kind,
        evidence=EvidenceIdentity(
            kind=EvidenceKind.RUNTIME_RECEIPT,
            fingerprint_sha256=fingerprint,
            version="v1",
            reference="operations.runtime.pair.latest.json",
            reference_sha256="c" * 64,
        ),
    )


def _insert(
    conn: sqlite3.Connection,
    *,
    fingerprint: str,
    severity: str = "warning",
    health: str = "degraded",
    lifecycle: str = "open",
    opened_at: datetime = NOW,
    acknowledged_at: datetime | None = None,
    acknowledged_until: datetime | None = None,
    snoozed_until: datetime | None = None,
    updated_at: datetime | None = None,
) -> str:
    finding_id = _finding_id(fingerprint)
    conn.execute(
        """
        INSERT INTO operations_attention_findings(
            finding_id,owner,kind,evidence_kind,evidence_fingerprint_sha256,evidence_version,
            evidence_reference,evidence_reference_sha256,severity,health,lifecycle,opened_at,
            acknowledged_at,acknowledged_until,snoozed_until,updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            finding_id,
            "scheduler.collect_operations_runtime_observations",
            "runtime_health",
            "runtime_receipt",
            fingerprint,
            "v1",
            "operations.runtime.pair.latest.json",
            "c" * 64,
            severity,
            health,
            lifecycle,
            opened_at.isoformat(),
            None if acknowledged_at is None else acknowledged_at.isoformat(),
            None if acknowledged_until is None else acknowledged_until.isoformat(),
            None if snoozed_until is None else snoozed_until.isoformat(),
            (updated_at or opened_at).isoformat(),
        ),
    )
    return finding_id


def test_attention_projection_reopens_expired_suppression_and_orders_actionable_findings(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    db_path = migrated_db(tmp_path / "attention.db")
    with sqlite3.connect(db_path) as conn:
        expired = _insert(
            conn,
            fingerprint="a" * 64,
            lifecycle="acknowledged",
            opened_at=NOW - timedelta(hours=3),
            acknowledged_at=NOW - timedelta(hours=2),
            acknowledged_until=NOW - timedelta(minutes=1),
        )
        critical = _insert(
            conn,
            fingerprint="b" * 64,
            severity="critical",
            opened_at=NOW - timedelta(minutes=2),
        )
        _insert(
            conn, fingerprint="d" * 64, lifecycle="snoozed", snoozed_until=NOW + timedelta(hours=1)
        )

        view = build_attention_panel_view(conn, observed_at=NOW)

    assert view.state == "available"
    assert [item.finding_id for item in view.findings[:2]] == [critical, expired]
    assert view.findings[1].lifecycle == "open"
    assert (
        view.findings[1].lifecycle_detail == "Acknowledgement expired; acknowledged policy remains"
    )
    assert {action.action for action in view.findings[1].actions} == {"snooze"}
    assert not view.findings[0].actions
    assert "operations.runtime.pair.latest.json" in view.findings[0].evidence_reference
    assert "/" not in view.findings[0].evidence_reference


def test_attention_projection_orders_same_severity_by_newest_update_and_expired_actions_by_writer_state(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    db_path = migrated_db(tmp_path / "attention.db")
    with sqlite3.connect(db_path) as conn:
        older = _insert(
            conn,
            fingerprint="a" * 64,
            opened_at=NOW - timedelta(hours=2),
            updated_at=NOW - timedelta(hours=1),
        )
        newer = _insert(
            conn,
            fingerprint="b" * 64,
            opened_at=NOW - timedelta(hours=3),
            updated_at=NOW - timedelta(minutes=1),
        )
        expired_snooze = _insert(
            conn,
            fingerprint="d" * 64,
            lifecycle="snoozed",
            opened_at=NOW - timedelta(hours=3),
            snoozed_until=NOW - timedelta(minutes=1),
            updated_at=NOW - timedelta(minutes=2),
        )
        view = build_attention_panel_view(conn, observed_at=NOW)

    assert [item.finding_id for item in view.findings[:3]] == [newer, expired_snooze, older]
    reopened = next(item for item in view.findings if item.finding_id == expired_snooze)
    assert reopened.lifecycle == "open"
    assert reopened.lifecycle_detail == "Snooze expired; snoozed policy remains"
    assert {action.action for action in reopened.actions} == {"acknowledge"}


def test_attention_projection_handles_empty_and_unavailable_without_raw_database_errors(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    db_path = migrated_db(tmp_path / "attention.db")
    with sqlite3.connect(db_path) as conn:
        empty = build_attention_panel_view(conn, observed_at=NOW)
    assert empty.state == "empty"
    assert empty.findings == ()

    with sqlite3.connect(":memory:") as conn:
        unavailable = build_attention_panel_view(conn, observed_at=NOW)
    assert unavailable.state == "unavailable"
    assert unavailable.message == "Attention findings are unavailable."


def test_attention_projection_bounds_the_active_inbox(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    db_path = migrated_db(tmp_path / "attention.db")
    with sqlite3.connect(db_path) as conn:
        for index in range(205):
            timestamp = NOW - timedelta(seconds=index)
            _insert(
                conn,
                fingerprint=f"{index:064x}",
                opened_at=timestamp,
                updated_at=timestamp,
            )
        view = build_attention_panel_view(conn, observed_at=NOW)

    assert len(view.findings) == 200
    assert view.findings[0].evidence_fingerprint_sha256 == f"{0:064x}"


def test_attention_projection_fails_closed_on_posix_windows_unc_or_traversal_references(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    for number, reference in enumerate(
        (
            "/private/receipt.json",
            "C:/private/receipt.json",
            "//server/share/receipt.json",
            "receipts/../secret.json",
            "receipts/private.json",
            "https://example.invalid/receipt.json",
        )
    ):
        db_path = migrated_db(tmp_path / f"unsafe-{number}.db")
        with sqlite3.connect(db_path) as conn:
            finding_id = _finding_id(f"{number:x}" * 64)
            conn.execute(
                """
                INSERT INTO operations_attention_findings(
                    finding_id,owner,kind,evidence_kind,evidence_fingerprint_sha256,evidence_version,
                    evidence_reference,evidence_reference_sha256,severity,health,lifecycle,opened_at,updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    finding_id,
                    "scheduler.collect_operations_runtime_observations",
                    "runtime_health",
                    "runtime_receipt",
                    f"{number:x}" * 64,
                    "v1",
                    reference,
                    "c" * 64,
                    "warning",
                    "degraded",
                    "open",
                    NOW.isoformat(),
                    NOW.isoformat(),
                ),
            )
            view = build_attention_panel_view(conn, observed_at=NOW)
        assert view.state == "unavailable"
        assert reference not in view.message


def test_attention_panel_renders_safe_action_controls_and_conflict_safe_client_contract(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    db_path = migrated_db(tmp_path / "attention.db")
    with sqlite3.connect(db_path) as conn:
        _insert(conn, fingerprint="a" * 64)
        attention = build_attention_panel_view(conn, observed_at=NOW)

    registry = build_operations_registry(Path(__file__).resolve().parents[1])
    with sqlite3.connect(":memory:") as conn:
        snapshot = collect_operations_snapshot(
            registry,
            repo_root=tmp_path,
            conn=conn,
            observed_at=NOW,
        )
    baseline = build_operations_panel_view(registry, snapshot)
    with_attention = build_operations_panel_view(registry, snapshot, attention=attention)
    assert with_attention.attention_count == baseline.attention_count + 1

    html = render_operations_panel(
        OperationsPanelView(
            observed_label="Observed 2026-08-24 19:00 UTC",
            attention_count=1,
            evidence_gap_count=0,
            runtime_summary_tone="warn",
            tasks=(),
            runtime_rows=(),
            attention=attention,
        )
    )

    assert 'id="operations-tab-attention"' in html
    assert 'data-attention-action="acknowledge"' in html
    assert 'data-attention-action="snooze"' in html
    assert "data-attention-snooze-reason" in html
    assert "Temporary action expiry" in html
    assert "evidence_reviewed" in html
    assert "follow_up_scheduled" in html
    assert 'type="datetime-local"' in html
    assert 'role="status" aria-live="polite"' in html
    assert 'role="group" aria-label="Available attention actions"' in html
    assert 'aria-labelledby="attention-heading-0"' in html
    assert "Resolve this healthy finding?" in html
    assert "Read-only declared ownership" not in html
    assert "/api/operations/attention/" in html
    assert "operations-attention-" in html
    assert "Action conflicted. Refreshing current finding…" in html
    assert "return refreshOperations()" in html
    assert "workOsMountHtml(mount, markup, '/api/panel/operations')" in html
    assert "operations.runtime.pair.latest.json" in html
    assert str(db_path) not in html
