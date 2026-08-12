"""Add durable FMP provider recovery state and work backlog.

Revision ID: 0008_add_fmp_recovery
Revises: 0007_add_earnings_surprise_observations
Create Date: 2026-08-11
"""

from __future__ import annotations

from alembic import op

revision = "0008_add_fmp_recovery"
down_revision = "0007_add_earnings_surprise_observations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS provider_circuit_state (
            provider TEXT PRIMARY KEY,
            state TEXT NOT NULL,
            revision INTEGER NOT NULL DEFAULT 0,
            consecutive_failures INTEGER NOT NULL DEFAULT 0,
            consecutive_rate_limits INTEGER NOT NULL DEFAULT 0,
            transient_failure_threshold INTEGER NOT NULL,
            rate_limit_threshold INTEGER NOT NULL,
            retry_delay_seconds INTEGER NOT NULL,
            probe_delay_seconds INTEGER NOT NULL,
            auth_probe_delay_seconds INTEGER NOT NULL,
            rate_limit_probe_delay_seconds INTEGER NOT NULL,
            opened_at TEXT,
            next_probe_at TEXT,
            probe_work_id TEXT,
            probe_lease_token TEXT,
            probe_lease_expires_at TEXT,
            last_reason_code TEXT,
            last_success_at TEXT,
            last_probe_at TEXT,
            updated_at TEXT NOT NULL,
            CHECK(length(provider) BETWEEN 1 AND 32),
            CHECK(state IN ('CLOSED','OPEN','HALF_OPEN')),
            CHECK(revision >= 0),
            CHECK(consecutive_failures >= 0),
            CHECK(consecutive_rate_limits >= 0),
            CHECK(transient_failure_threshold BETWEEN 1 AND 100),
            CHECK(rate_limit_threshold BETWEEN 1 AND 100),
            CHECK(retry_delay_seconds BETWEEN 0 AND 86400),
            CHECK(probe_delay_seconds BETWEEN 1 AND 604800),
            CHECK(auth_probe_delay_seconds BETWEEN 1 AND 604800),
            CHECK(rate_limit_probe_delay_seconds BETWEEN 1 AND 604800),
            CHECK(
                (state = 'CLOSED' AND opened_at IS NULL AND next_probe_at IS NULL)
                OR (state IN ('OPEN','HALF_OPEN') AND opened_at IS NOT NULL
                    AND next_probe_at IS NOT NULL)
            ),
            CHECK(
                (state = 'HALF_OPEN' AND probe_work_id IS NOT NULL
                    AND probe_lease_token IS NOT NULL
                    AND probe_lease_expires_at IS NOT NULL)
                OR (state != 'HALF_OPEN' AND probe_work_id IS NULL
                    AND probe_lease_token IS NULL
                    AND probe_lease_expires_at IS NULL)
            )
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS fmp_work_backlog (
            work_id TEXT PRIMARY KEY,
            provider TEXT NOT NULL DEFAULT 'fmp',
            ticker TEXT NOT NULL,
            coverage_role TEXT NOT NULL,
            artifact_kind TEXT NOT NULL,
            endpoint_key TEXT NOT NULL,
            period_key TEXT NOT NULL,
            cache_generation_id TEXT NOT NULL,
            policy_sha256 TEXT NOT NULL,
            requested INTEGER NOT NULL DEFAULT 0,
            owner_request_id TEXT,
            priority INTEGER NOT NULL,
            state TEXT NOT NULL,
            available_at TEXT NOT NULL,
            lease_owner TEXT,
            lease_token TEXT,
            lease_run_id TEXT,
            lease_mode TEXT,
            lease_expires_at TEXT,
            resolution_source TEXT,
            attempt_count INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            satisfied_at TEXT,
            terminal_reason_code TEXT,
            FOREIGN KEY(provider) REFERENCES provider_circuit_state(provider),
            UNIQUE(
                provider,ticker,artifact_kind,endpoint_key,period_key,
                cache_generation_id,policy_sha256
            ),
            CHECK(length(work_id) = 64 AND work_id GLOB '[0-9a-f]*'),
            CHECK(provider = 'fmp'),
            CHECK(length(ticker) BETWEEN 1 AND 16 AND ticker = upper(ticker)),
            CHECK(coverage_role IN ('portfolio','evaluation','index_member')),
            CHECK(artifact_kind = 'financial_fact'),
            CHECK(length(endpoint_key) BETWEEN 1 AND 96),
            CHECK(length(period_key) BETWEEN 1 AND 96),
            CHECK(length(cache_generation_id) BETWEEN 1 AND 256),
            CHECK(length(policy_sha256) = 64 AND policy_sha256 GLOB '[0-9a-f]*'),
            CHECK(requested IN (0,1)),
            CHECK(
                (coverage_role = 'evaluation' AND requested = 1
                    AND owner_request_id IS NOT NULL)
                OR coverage_role != 'evaluation'
            ),
            CHECK(priority IN (100,200,300)),
            CHECK(state IN ('PENDING','LEASED','SATISFIED','TERMINAL')),
            CHECK(attempt_count >= 0),
            CHECK(
                (state = 'LEASED' AND lease_owner IS NOT NULL
                    AND lease_token IS NOT NULL AND lease_run_id IS NOT NULL
                    AND lease_mode IS NOT NULL AND lease_expires_at IS NOT NULL)
                OR (state != 'LEASED' AND lease_owner IS NULL
                    AND lease_token IS NULL AND lease_run_id IS NULL
                    AND lease_mode IS NULL AND lease_expires_at IS NULL)
            ),
            CHECK(
                (state = 'SATISFIED' AND resolution_source IS NOT NULL
                    AND satisfied_at IS NOT NULL)
                OR (state != 'SATISFIED' AND resolution_source IS NULL
                    AND satisfied_at IS NULL)
            ),
            CHECK(
                (state = 'TERMINAL' AND terminal_reason_code IS NOT NULL)
                OR (state != 'TERMINAL' AND terminal_reason_code IS NULL)
            )
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_fmp_work_backlog_recoverable "
        "ON fmp_work_backlog(state, available_at, priority DESC, created_at)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_fmp_work_backlog_ticker_state "
        "ON fmp_work_backlog(ticker, state, updated_at)"
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS fmp_work_attempts (
            attempt_id TEXT PRIMARY KEY,
            work_id TEXT NOT NULL,
            run_id TEXT NOT NULL,
            execution_mode TEXT NOT NULL,
            outcome_code TEXT NOT NULL,
            http_status INTEGER,
            retry_after_at TEXT,
            corpus_generation_id TEXT,
            corpus_content_sha256 TEXT,
            corpus_captured_at TEXT,
            fmp_snapshot_content_sha256 TEXT,
            fmp_snapshot_captured_at TEXT,
            resolution_source TEXT,
            resolution_policy_sha256 TEXT,
            resolution_endpoint_key TEXT,
            resolution_period_key TEXT,
            resolution_concept_keys_json TEXT,
            resolution_evidence_fresh_at TEXT,
            resolution_source_authorized INTEGER,
            resolution_has_disagreement INTEGER,
            coverage_proof_sha256 TEXT,
            evidence_ids_json TEXT,
            fact_ids_json TEXT,
            observed_at TEXT NOT NULL,
            recorded_at TEXT NOT NULL,
            FOREIGN KEY(work_id) REFERENCES fmp_work_backlog(work_id),
            UNIQUE(work_id, run_id),
            CHECK(length(attempt_id) = 64 AND attempt_id GLOB '[0-9a-f]*'),
            CHECK(length(run_id) BETWEEN 1 AND 256),
            CHECK(execution_mode IN ('LIVE','PROBE','CORPUS','ALTERNATIVE','RECONCILE')),
            CHECK(http_status IS NULL OR http_status BETWEEN 100 AND 599),
            CHECK(corpus_content_sha256 IS NULL OR length(corpus_content_sha256) = 64),
            CHECK(fmp_snapshot_content_sha256 IS NULL
                  OR length(fmp_snapshot_content_sha256) = 64),
            CHECK(resolution_policy_sha256 IS NULL
                  OR length(resolution_policy_sha256) = 64),
            CHECK(resolution_concept_keys_json IS NULL OR (
                json_valid(resolution_concept_keys_json)
                AND json_type(resolution_concept_keys_json) = 'array'
            )),
            CHECK(resolution_source_authorized IS NULL
                  OR resolution_source_authorized IN (0,1)),
            CHECK(resolution_has_disagreement IS NULL
                  OR resolution_has_disagreement IN (0,1)),
            CHECK(coverage_proof_sha256 IS NULL OR length(coverage_proof_sha256) = 64),
            CHECK(evidence_ids_json IS NULL OR (
                json_valid(evidence_ids_json) AND json_type(evidence_ids_json) = 'array'
            )),
            CHECK(fact_ids_json IS NULL OR (
                json_valid(fact_ids_json) AND json_type(fact_ids_json) = 'array'
            ))
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_fmp_work_attempts_work_recorded "
        "ON fmp_work_attempts(work_id, recorded_at)"
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS fmp_recovery_events (
            event_id TEXT PRIMARY KEY,
            provider TEXT NOT NULL,
            work_id TEXT,
            attempt_id TEXT,
            event_type TEXT NOT NULL,
            reason_code TEXT,
            state_from TEXT,
            state_to TEXT,
            circuit_revision INTEGER,
            recorded_at TEXT NOT NULL,
            FOREIGN KEY(provider) REFERENCES provider_circuit_state(provider),
            FOREIGN KEY(work_id) REFERENCES fmp_work_backlog(work_id),
            FOREIGN KEY(attempt_id) REFERENCES fmp_work_attempts(attempt_id),
            CHECK(length(event_id) = 64 AND event_id GLOB '[0-9a-f]*'),
            CHECK(length(event_type) BETWEEN 1 AND 64),
            CHECK(circuit_revision IS NULL OR circuit_revision >= 0)
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_fmp_recovery_events_provider_recorded "
        "ON fmp_recovery_events(provider, recorded_at)"
    )
    for table, label in (
        ("fmp_work_attempts", "FMP work attempts"),
        ("fmp_recovery_events", "FMP recovery events"),
    ):
        op.execute(
            f"""
            CREATE TRIGGER IF NOT EXISTS trg_{table}_no_update
            BEFORE UPDATE ON {table}
            BEGIN
                SELECT RAISE(ABORT, '{label} are immutable');
            END
            """
        )
        op.execute(
            f"""
            CREATE TRIGGER IF NOT EXISTS trg_{table}_no_delete
            BEFORE DELETE ON {table}
            BEGIN
                SELECT RAISE(ABORT, '{label} are immutable');
            END
            """
        )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_fmp_recovery_events_no_delete")
    op.execute("DROP TRIGGER IF EXISTS trg_fmp_recovery_events_no_update")
    op.execute("DROP TRIGGER IF EXISTS trg_fmp_work_attempts_no_delete")
    op.execute("DROP TRIGGER IF EXISTS trg_fmp_work_attempts_no_update")
    op.execute("DROP INDEX IF EXISTS ix_fmp_recovery_events_provider_recorded")
    op.execute("DROP TABLE IF EXISTS fmp_recovery_events")
    op.execute("DROP INDEX IF EXISTS ix_fmp_work_attempts_work_recorded")
    op.execute("DROP TABLE IF EXISTS fmp_work_attempts")
    op.execute("DROP INDEX IF EXISTS ix_fmp_work_backlog_ticker_state")
    op.execute("DROP INDEX IF EXISTS ix_fmp_work_backlog_recoverable")
    op.execute("DROP TABLE IF EXISTS fmp_work_backlog")
    op.execute("DROP TABLE IF EXISTS provider_circuit_state")
