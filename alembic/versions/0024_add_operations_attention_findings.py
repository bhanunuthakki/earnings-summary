"""Persist bounded, evidence-led Operations attention lifecycle state.

Revision ID: 0024_add_operations_attention_findings
Revises: 0023_add_price_action_sensor_state
"""

from __future__ import annotations

from alembic import op

revision = "0024_add_operations_attention_findings"
down_revision = "0023_add_price_action_sensor_state"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE operations_attention_findings (
            finding_id TEXT PRIMARY KEY NOT NULL,
            owner TEXT NOT NULL,
            kind TEXT NOT NULL CHECK(kind IN
                ('runtime_health','schema_compatibility','data_integrity','repair_required')),
            evidence_kind TEXT NOT NULL CHECK(evidence_kind IN
                ('runtime_receipt','schema_observation','integrity_audit','repair_receipt')),
            evidence_fingerprint_sha256 TEXT NOT NULL,
            evidence_version TEXT NOT NULL,
            evidence_reference TEXT NOT NULL,
            evidence_reference_sha256 TEXT NOT NULL,
            severity TEXT NOT NULL CHECK(severity IN ('info','warning','critical')),
            health TEXT NOT NULL CHECK(health IN ('healthy','degraded','invalid','unavailable')),
            lifecycle TEXT NOT NULL CHECK(lifecycle IN
                ('open','acknowledged','snoozed','resolved','superseded')),
            opened_at TEXT NOT NULL,
            acknowledged_at TEXT,
            acknowledged_until TEXT,
            snoozed_until TEXT,
            resolved_at TEXT,
            superseded_by_finding_id TEXT REFERENCES operations_attention_findings(finding_id),
            updated_at TEXT NOT NULL,
            CHECK(length(finding_id)=85 AND substr(finding_id,1,21)='operations-attention:'
                AND substr(finding_id,22) NOT GLOB '*[^0-9a-f]*'),
            CHECK(length(owner) BETWEEN 1 AND 128 AND owner NOT GLOB '*[^A-Za-z0-9_.:-]*'),
            CHECK(length(evidence_fingerprint_sha256)=64
                AND evidence_fingerprint_sha256 NOT GLOB '*[^0-9a-f]*'),
            CHECK(length(evidence_version) BETWEEN 1 AND 128
                AND evidence_version NOT GLOB '*[^A-Za-z0-9_.:-]*'),
            CHECK(length(evidence_reference) BETWEEN 1 AND 256
                AND evidence_reference NOT GLOB '*[^A-Za-z0-9_.:/-]*'),
            CHECK(length(evidence_reference_sha256)=64
                AND evidence_reference_sha256 NOT GLOB '*[^0-9a-f]*'),
            CHECK(datetime(opened_at) IS NOT NULL AND datetime(updated_at) IS NOT NULL
                AND datetime(updated_at)>=datetime(opened_at)),
            CHECK(acknowledged_at IS NULL OR datetime(acknowledged_at) IS NOT NULL),
            CHECK(acknowledged_until IS NULL OR datetime(acknowledged_until) IS NOT NULL),
            CHECK(snoozed_until IS NULL OR datetime(snoozed_until) IS NOT NULL),
            CHECK(resolved_at IS NULL OR datetime(resolved_at) IS NOT NULL),
            CHECK((lifecycle='acknowledged') =
                (acknowledged_at IS NOT NULL AND acknowledged_until IS NOT NULL)),
            CHECK(acknowledged_at IS NULL OR datetime(acknowledged_at)>=datetime(opened_at)),
            CHECK(acknowledged_until IS NULL OR datetime(acknowledged_until)>datetime(acknowledged_at)),
            CHECK((lifecycle='snoozed') = (snoozed_until IS NOT NULL)),
            CHECK(snoozed_until IS NULL OR datetime(snoozed_until)>datetime(updated_at)),
            CHECK((lifecycle='resolved') = (resolved_at IS NOT NULL)),
            CHECK(resolved_at IS NULL OR datetime(resolved_at)>=datetime(opened_at)),
            CHECK((lifecycle='superseded') = (superseded_by_finding_id IS NOT NULL))
        )
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_operations_attention_findings_identity ON "
        "operations_attention_findings(owner,kind,evidence_fingerprint_sha256,evidence_version)"
    )
    op.execute(
        "CREATE INDEX ix_operations_attention_findings_attention ON "
        "operations_attention_findings(lifecycle,severity,health,updated_at DESC)"
    )
    op.execute(
        """
        CREATE TABLE operations_attention_lifecycle_events (
            event_id TEXT PRIMARY KEY NOT NULL,
            finding_id TEXT NOT NULL REFERENCES operations_attention_findings(finding_id),
            event_kind TEXT NOT NULL CHECK(event_kind IN
                ('detected','acknowledge','snooze','expire_acknowledgement','expire_snooze','resolve','supersede')),
            from_lifecycle TEXT CHECK(from_lifecycle IN
                ('open','acknowledged','snoozed','resolved','superseded')),
            to_lifecycle TEXT NOT NULL CHECK(to_lifecycle IN
                ('open','acknowledged','snoozed','resolved','superseded')),
            evidence_fingerprint_sha256 TEXT NOT NULL,
            occurred_at TEXT NOT NULL,
            receipt_sha256 TEXT NOT NULL,
            CHECK(length(event_id)=91 AND substr(event_id,1,27)='operations-attention-event:'),
            CHECK(length(evidence_fingerprint_sha256)=64
                AND evidence_fingerprint_sha256 NOT GLOB '*[^0-9a-f]*'),
            CHECK(length(receipt_sha256)=64 AND receipt_sha256 NOT GLOB '*[^0-9a-f]*'),
            CHECK(datetime(occurred_at) IS NOT NULL),
            CHECK((event_kind='detected' AND from_lifecycle IS NULL AND to_lifecycle='open')
                OR (event_kind<>'detected' AND from_lifecycle IS NOT NULL)),
            CHECK((event_kind='acknowledge') =
                (from_lifecycle IN ('open','snoozed') AND to_lifecycle='acknowledged')),
            CHECK((event_kind='snooze') =
                (from_lifecycle IN ('open','acknowledged') AND to_lifecycle='snoozed')),
            CHECK((event_kind='expire_acknowledgement') =
                (from_lifecycle='acknowledged' AND to_lifecycle='open')),
            CHECK((event_kind='expire_snooze') = (from_lifecycle='snoozed' AND to_lifecycle='open')),
            CHECK((event_kind='resolve') =
                (from_lifecycle IN ('open','acknowledged','snoozed') AND to_lifecycle='resolved')),
            CHECK((event_kind='supersede') =
                (from_lifecycle IN ('open','acknowledged','snoozed') AND to_lifecycle='superseded'))
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_operations_attention_lifecycle_events_finding_occurred ON "
        "operations_attention_lifecycle_events(finding_id,occurred_at DESC)"
    )
    op.execute(
        """
        CREATE TABLE operations_attention_action_receipts (
            receipt_id TEXT PRIMARY KEY NOT NULL,
            idempotency_key_sha256 TEXT NOT NULL UNIQUE,
            finding_id TEXT NOT NULL REFERENCES operations_attention_findings(finding_id),
            lifecycle_event_id TEXT REFERENCES operations_attention_lifecycle_events(event_id),
            actor TEXT NOT NULL,
            action TEXT NOT NULL CHECK(action IN ('acknowledge','snooze','resolve','supersede')),
            result_lifecycle TEXT NOT NULL CHECK(result_lifecycle IN
                ('acknowledged','snoozed','resolved','superseded')),
            occurred_at TEXT NOT NULL,
            request_sha256 TEXT NOT NULL,
            reason_code TEXT CHECK(reason_code IN
                ('evidence_reviewed','follow_up_scheduled','investigation_in_progress','maintenance_window')),
            reason_reference_sha256 TEXT,
            acknowledged_until TEXT,
            snoozed_until TEXT,
            result_state TEXT NOT NULL CHECK(result_state IN ('applied','rejected')),
            failure_code TEXT CHECK(failure_code IN
                ('prohibited_suppression','invalid_transition','invalid_expiry','validation_failed','conflict')),
            failure_sha256 TEXT,
            CHECK(length(receipt_id)=92 AND substr(receipt_id,1,28)='operations-attention-action:'),
            CHECK(length(idempotency_key_sha256)=64 AND idempotency_key_sha256 NOT GLOB '*[^0-9a-f]*'),
            CHECK(length(actor) BETWEEN 1 AND 128 AND actor NOT GLOB '*[^A-Za-z0-9_.:-]*'),
            CHECK(length(request_sha256)=64 AND request_sha256 NOT GLOB '*[^0-9a-f]*'),
            CHECK(reason_reference_sha256 IS NULL OR
                (length(reason_reference_sha256)=64 AND reason_reference_sha256 NOT GLOB '*[^0-9a-f]*')),
            CHECK(failure_sha256 IS NULL OR
                (length(failure_sha256)=64 AND failure_sha256 NOT GLOB '*[^0-9a-f]*')),
            CHECK(datetime(occurred_at) IS NOT NULL),
            CHECK(acknowledged_until IS NULL OR datetime(acknowledged_until)>datetime(occurred_at)),
            CHECK(snoozed_until IS NULL OR datetime(snoozed_until)>datetime(occurred_at)),
            CHECK(result_state<>'applied' OR
                ((action IN ('acknowledge','snooze')) = (reason_code IS NOT NULL))),
            CHECK(result_state<>'applied' OR
                ((action IN ('acknowledge','snooze')) = (reason_reference_sha256 IS NOT NULL))),
            CHECK(result_state<>'applied' OR action<>'acknowledge' OR reason_code='evidence_reviewed'),
            CHECK(result_state<>'applied' OR action<>'snooze' OR reason_code IN
                ('follow_up_scheduled','investigation_in_progress','maintenance_window')),
            CHECK(result_state<>'applied' OR
                ((action='acknowledge') = (acknowledged_until IS NOT NULL))),
            CHECK(result_state<>'applied' OR ((action='snooze') = (snoozed_until IS NOT NULL))),
            CHECK(result_state<>'rejected' OR
                (reason_code IS NULL AND reason_reference_sha256 IS NULL
                    AND acknowledged_until IS NULL AND snoozed_until IS NULL)),
            CHECK((result_state='applied') =
                (lifecycle_event_id IS NOT NULL AND failure_code IS NULL AND failure_sha256 IS NULL)),
            CHECK((result_state='rejected') =
                (lifecycle_event_id IS NULL AND failure_code IS NOT NULL AND failure_sha256 IS NOT NULL)),
            CHECK((action='acknowledge') = (result_lifecycle='acknowledged')),
            CHECK((action='snooze') = (result_lifecycle='snoozed')),
            CHECK((action='resolve') = (result_lifecycle='resolved')),
            CHECK((action='supersede') = (result_lifecycle='superseded'))
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_operations_attention_action_receipts_finding_occurred ON "
        "operations_attention_action_receipts(finding_id,occurred_at DESC)"
    )
    op.execute(
        "CREATE TRIGGER trg_operations_attention_findings_identity_immutable "
        "BEFORE UPDATE OF finding_id,owner,kind,evidence_kind,evidence_fingerprint_sha256,evidence_version,"
        "evidence_reference,evidence_reference_sha256 ON operations_attention_findings "
        "WHEN NEW.finding_id<>OLD.finding_id OR NEW.owner<>OLD.owner OR NEW.kind<>OLD.kind "
        "OR NEW.evidence_kind<>OLD.evidence_kind "
        "OR NEW.evidence_fingerprint_sha256<>OLD.evidence_fingerprint_sha256 "
        "OR NEW.evidence_version<>OLD.evidence_version OR NEW.evidence_reference<>OLD.evidence_reference "
        "OR NEW.evidence_reference_sha256<>OLD.evidence_reference_sha256 BEGIN "
        "SELECT RAISE(ABORT, 'operations attention canonical identity is immutable'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_operations_attention_findings_opened_at_immutable "
        "BEFORE UPDATE OF opened_at ON operations_attention_findings WHEN NEW.opened_at<>OLD.opened_at "
        "BEGIN SELECT RAISE(ABORT, 'operations attention canonical identity is immutable'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_operations_attention_lifecycle_events_after_open "
        "BEFORE INSERT ON operations_attention_lifecycle_events WHEN EXISTS ("
        "SELECT 1 FROM operations_attention_findings finding WHERE finding.finding_id=NEW.finding_id "
        "AND datetime(NEW.occurred_at)<datetime(finding.opened_at)) BEGIN "
        "SELECT RAISE(ABORT, 'attention lifecycle event predates finding'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_operations_attention_lifecycle_events_evidence_matches_finding "
        "BEFORE INSERT ON operations_attention_lifecycle_events WHEN NOT EXISTS ("
        "SELECT 1 FROM operations_attention_findings finding WHERE finding.finding_id=NEW.finding_id "
        "AND finding.evidence_fingerprint_sha256=NEW.evidence_fingerprint_sha256) BEGIN "
        "SELECT RAISE(ABORT, 'attention lifecycle evidence fingerprint does not match finding'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_operations_attention_lifecycle_events_source_lifecycle_matches "
        "BEFORE INSERT ON operations_attention_lifecycle_events WHEN NEW.event_kind<>'detected' "
        "AND NOT EXISTS (SELECT 1 FROM operations_attention_findings finding "
        "WHERE finding.finding_id=NEW.finding_id AND finding.lifecycle=NEW.from_lifecycle) BEGIN "
        "SELECT RAISE(ABORT, 'attention lifecycle event source lifecycle does not match finding'); END"
    )
    op.execute(
        """
        CREATE TABLE operations_attention_repair_references (
            repair_reference_id TEXT PRIMARY KEY NOT NULL,
            finding_id TEXT NOT NULL REFERENCES operations_attention_findings(finding_id),
            reference_kind TEXT NOT NULL CHECK(reference_kind IN
                ('runbook','repair_receipt','replay_receipt','manual_reference')),
            reference_label TEXT NOT NULL,
            reference_sha256 TEXT NOT NULL,
            recorded_at TEXT NOT NULL,
            CHECK(length(repair_reference_id)=92
                AND substr(repair_reference_id,1,28)='operations-attention-repair:'),
            CHECK(length(reference_label) BETWEEN 1 AND 256
                AND reference_label NOT GLOB '*[^A-Za-z0-9_.:/-]*'),
            CHECK(length(reference_sha256)=64 AND reference_sha256 NOT GLOB '*[^0-9a-f]*'),
            CHECK(datetime(recorded_at) IS NOT NULL)
        )
        """
    )
    op.execute(
        "CREATE TRIGGER trg_operations_attention_action_receipts_event_agreement "
        "BEFORE INSERT ON operations_attention_action_receipts WHEN NEW.result_state='applied' "
        "AND NOT EXISTS (SELECT 1 FROM operations_attention_lifecycle_events event "
        "WHERE event.event_id=NEW.lifecycle_event_id AND event.finding_id=NEW.finding_id "
        "AND event.event_kind=NEW.action AND event.to_lifecycle=NEW.result_lifecycle) BEGIN "
        "SELECT RAISE(ABORT, 'attention action receipt event does not match finding/action/result'); END"
    )
    op.execute(
        "CREATE INDEX ix_operations_attention_repair_references_finding_recorded ON "
        "operations_attention_repair_references(finding_id,recorded_at DESC)"
    )
    for table in (
        "operations_attention_lifecycle_events",
        "operations_attention_action_receipts",
        "operations_attention_repair_references",
    ):
        op.execute(
            f"CREATE TRIGGER trg_{table}_no_update BEFORE UPDATE ON {table} BEGIN "
            f"SELECT RAISE(ABORT, '{table} append-only'); END"
        )
        op.execute(
            f"CREATE TRIGGER trg_{table}_no_delete BEFORE DELETE ON {table} BEGIN "
            f"SELECT RAISE(ABORT, '{table} append-only'); END"
        )
    op.execute(
        "CREATE TRIGGER trg_operations_attention_findings_no_delete BEFORE DELETE ON "
        "operations_attention_findings BEGIN "
        "SELECT RAISE(ABORT, 'operations attention findings cannot be deleted'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_operations_attention_findings_no_silent_suppression "
        "BEFORE UPDATE OF lifecycle ON operations_attention_findings "
        "WHEN NEW.lifecycle IN ('acknowledged','snoozed') AND "
        "(NEW.severity='critical' OR NEW.health IN ('invalid','unavailable')) BEGIN "
        "SELECT RAISE(ABORT, 'critical, invalid, or unavailable findings cannot be suppressed'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_operations_attention_findings_transition_matrix "
        "BEFORE UPDATE OF lifecycle ON operations_attention_findings WHEN NOT ("
        "(OLD.lifecycle='open' AND NEW.lifecycle IN ('acknowledged','snoozed','resolved','superseded')) OR "
        "(OLD.lifecycle='acknowledged' AND NEW.lifecycle IN ('open','snoozed','resolved','superseded')) OR "
        "(OLD.lifecycle='snoozed' AND NEW.lifecycle IN ('open','acknowledged','resolved','superseded'))"
        ") BEGIN SELECT RAISE(ABORT, 'operations attention lifecycle transition is not allowed'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_operations_attention_findings_resolution_requires_healthy_evidence "
        "BEFORE UPDATE OF lifecycle ON operations_attention_findings "
        "WHEN NEW.lifecycle='resolved' AND NEW.health<>'healthy' BEGIN "
        "SELECT RAISE(ABORT, 'findings require healthy evidence before resolution'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_operations_attention_action_receipts_no_silent_suppression "
        "BEFORE INSERT ON operations_attention_action_receipts "
        "WHEN NEW.result_state='applied' AND NEW.action IN ('acknowledge','snooze') AND EXISTS ("
        "SELECT 1 FROM operations_attention_findings finding WHERE finding.finding_id=NEW.finding_id "
        "AND (finding.severity='critical' OR finding.health IN ('invalid','unavailable'))"
        ") BEGIN SELECT RAISE(ABORT, 'critical, invalid, or unavailable findings cannot be suppressed'); END"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_operations_attention_action_receipts_event_agreement")
    op.execute("DROP TRIGGER IF EXISTS trg_operations_attention_lifecycle_events_after_open")
    op.execute(
        "DROP TRIGGER IF EXISTS trg_operations_attention_lifecycle_events_evidence_matches_finding"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_operations_attention_lifecycle_events_source_lifecycle_matches"
    )
    op.execute("DROP TRIGGER IF EXISTS trg_operations_attention_findings_identity_immutable")
    op.execute("DROP TRIGGER IF EXISTS trg_operations_attention_findings_opened_at_immutable")
    op.execute(
        "DROP TRIGGER IF EXISTS trg_operations_attention_action_receipts_no_silent_suppression"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS "
        "trg_operations_attention_findings_resolution_requires_healthy_evidence"
    )
    op.execute("DROP TRIGGER IF EXISTS trg_operations_attention_findings_no_silent_suppression")
    op.execute("DROP TRIGGER IF EXISTS trg_operations_attention_findings_transition_matrix")
    op.execute("DROP TRIGGER IF EXISTS trg_operations_attention_findings_no_delete")
    for table in (
        "operations_attention_repair_references",
        "operations_attention_action_receipts",
        "operations_attention_lifecycle_events",
    ):
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_no_delete")
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_no_update")
    op.execute("DROP INDEX IF EXISTS ix_operations_attention_repair_references_finding_recorded")
    op.execute("DROP TABLE IF EXISTS operations_attention_repair_references")
    op.execute("DROP INDEX IF EXISTS ix_operations_attention_action_receipts_finding_occurred")
    op.execute("DROP TABLE IF EXISTS operations_attention_action_receipts")
    op.execute("DROP INDEX IF EXISTS ix_operations_attention_lifecycle_events_finding_occurred")
    op.execute("DROP TABLE IF EXISTS operations_attention_lifecycle_events")
    op.execute("DROP INDEX IF EXISTS ix_operations_attention_findings_attention")
    op.execute("DROP INDEX IF EXISTS uq_operations_attention_findings_identity")
    op.execute("DROP TABLE IF EXISTS operations_attention_findings")
