"""Add semantic thesis-evaluation episodes without deleting raw history.

Revision ID: 0014_add_thesis_evaluation_episodes
Revises: 0013_add_readme_update_budgets
Create Date: 2026-08-14
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa

from alembic import op

revision = "0014_add_thesis_evaluation_episodes"
down_revision = "0013_add_readme_update_budgets"
branch_labels = None
depends_on = None

_LEGACY_POLICY = "legacy_v0"
_EPISODE_PREFIX = "thesis-evaluation-episode:"


def _canonical_json(value: object) -> str:
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True
    )


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _normalized(value: object) -> object:
    """Normalize stored legacy payloads without claiming unavailable provenance."""

    if isinstance(value, dict):
        return {
            str(key): _normalized(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
            if str(key) != "evaluated_at"
        }
    if isinstance(value, list):
        items = [_normalized(item) for item in value]
        return sorted(items, key=_canonical_json)
    return value


def _json_array(raw: object, *, evaluation_id: int, column: str) -> list[object]:
    if raw is None and column == "soft_rule_results_json":
        return []
    try:
        value = json.loads(str(raw))
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"thesis evaluation {evaluation_id} has invalid {column}") from exc
    if not isinstance(value, list):
        raise RuntimeError(f"thesis evaluation {evaluation_id} has non-array {column}")
    return value


def _periods(value: object) -> Iterable[str]:
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"period_end", "last_period"} and isinstance(item, str):
                yield item
            yield from _periods(item)
    elif isinstance(value, list):
        for item in value:
            yield from _periods(item)


def _legacy_payload(row: sa.Row[Any]) -> tuple[str, str, str | None]:
    evaluation_id = int(row.id)
    hard = _normalized(
        _json_array(
            row.rule_evaluations_json,
            evaluation_id=evaluation_id,
            column="rule_evaluations_json",
        )
    )
    soft = _normalized(
        _json_array(
            row.soft_rule_results_json,
            evaluation_id=evaluation_id,
            column="soft_rule_results_json",
        )
    )
    semantic = {
        "fingerprint_policy_version": _LEGACY_POLICY,
        "ticker": str(row.ticker).strip().upper(),
        "overall_status": str(row.overall_status),
        "rule_evaluations": hard,
        "soft_rule_results": soft,
    }
    canonical = _canonical_json(semantic)
    periods = sorted(set(_periods(semantic)))
    evidence_as_of = periods[-1] if periods else None
    result = {
        "overall_status": str(row.overall_status),
        "rule_evaluations": hard,
        "soft_rule_results": soft,
        "provenance_completeness": "partial",
    }
    return canonical, _sha256(_canonical_json(result)), evidence_as_of


def _backfill(bind: sa.Connection) -> None:
    rows = (
        bind.execute(
            sa.text(
                "SELECT id,ticker,evaluated_at,overall_status,rule_evaluations_json,"
                "soft_rule_results_json,run_id FROM thesis_evaluations "
                "ORDER BY UPPER(ticker),datetime(evaluated_at),id"
            )
        )
        .mappings()
        .all()
    )
    groups: dict[tuple[str, str], list[tuple[sa.RowMapping, str, str, str | None]]] = defaultdict(
        list
    )
    for row in rows:
        canonical, result_sha256, evidence_as_of = _legacy_payload(row)
        ticker = str(row["ticker"]).strip().upper()
        semantic_sha256 = _sha256(canonical)
        groups[(ticker, semantic_sha256)].append((row, canonical, result_sha256, evidence_as_of))

    for (ticker, semantic_sha256), members in sorted(groups.items()):
        first = members[0][0]
        last = members[-1][0]
        canonical = members[0][1]
        result_sha256 = members[0][2]
        evidence_values = [item[3] for item in members if item[3] is not None]
        evidence_as_of = max(evidence_values) if evidence_values else None
        status = str(first["overall_status"])
        episode_id = _EPISODE_PREFIX + _sha256(
            "\n".join((_LEGACY_POLICY, ticker, semantic_sha256, status))
        )
        first_at = str(first["evaluated_at"])
        last_at = str(last["evaluated_at"])
        bind.execute(
            sa.text(
                "INSERT INTO thesis_evaluation_episodes "
                "(episode_id,ticker,fingerprint_policy_version,semantic_input_json,"
                "semantic_input_sha256,thesis_content_sha256,ruleset_sha256,"
                "evaluator_semantic_version,result_sha256,overall_status,"
                "provenance_completeness,evidence_as_of,first_evaluated_at,last_seen_at,"
                "last_checked_at,duplicate_run_count,rule_evaluations_json,"
                "soft_rule_results_json,first_run_id,created_at) "
                "VALUES (:episode_id,:ticker,:policy,:semantic_json,:semantic_sha,NULL,NULL,"
                ":evaluator_version,:result_sha,:status,'partial',:evidence_as_of,"
                ":first_at,:last_at,:last_at,:duplicates,:hard,:soft,:first_run_id,:created_at)"
            ),
            {
                "episode_id": episode_id,
                "ticker": ticker,
                "policy": _LEGACY_POLICY,
                "semantic_json": canonical,
                "semantic_sha": semantic_sha256,
                "evaluator_version": _LEGACY_POLICY,
                "result_sha": result_sha256,
                "status": status,
                "evidence_as_of": evidence_as_of,
                "first_at": first_at,
                "last_at": last_at,
                "duplicates": len(members) - 1,
                "hard": _canonical_json(
                    _normalized(
                        _json_array(
                            first["rule_evaluations_json"],
                            evaluation_id=int(first["id"]),
                            column="rule_evaluations_json",
                        )
                    )
                ),
                "soft": _canonical_json(
                    _normalized(
                        _json_array(
                            first["soft_rule_results_json"],
                            evaluation_id=int(first["id"]),
                            column="soft_rule_results_json",
                        )
                    )
                ),
                "first_run_id": first["run_id"],
                "created_at": datetime.now(UTC).isoformat(),
            },
        )
        for ordinal, (row, _, _, _) in enumerate(members, start=1):
            bind.execute(
                sa.text(
                    "INSERT INTO thesis_evaluation_episode_members "
                    "(episode_id,evaluation_id,membership_role,member_ordinal,recorded_at) "
                    "VALUES (:episode_id,:evaluation_id,:role,:ordinal,:recorded_at)"
                ),
                {
                    "episode_id": episode_id,
                    "evaluation_id": int(row["id"]),
                    "role": "anchor" if ordinal == 1 else "duplicate",
                    "ordinal": ordinal,
                    "recorded_at": str(row["evaluated_at"]),
                },
            )


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE thesis_evaluation_episodes (
            episode_id TEXT PRIMARY KEY NOT NULL,
            ticker TEXT NOT NULL,
            fingerprint_policy_version TEXT NOT NULL
                CHECK(fingerprint_policy_version IN ('forward_v1','legacy_v0')),
            semantic_input_json TEXT NOT NULL
                CHECK(json_valid(semantic_input_json)=1 AND json_type(semantic_input_json)='object'),
            semantic_input_sha256 TEXT NOT NULL
                CHECK(length(semantic_input_sha256)=64 AND semantic_input_sha256 NOT GLOB '*[^0-9a-f]*'),
            thesis_content_sha256 TEXT
                CHECK(thesis_content_sha256 IS NULL OR (length(thesis_content_sha256)=64 AND thesis_content_sha256 NOT GLOB '*[^0-9a-f]*')),
            ruleset_sha256 TEXT
                CHECK(ruleset_sha256 IS NULL OR (length(ruleset_sha256)=64 AND ruleset_sha256 NOT GLOB '*[^0-9a-f]*')),
            evaluator_semantic_version TEXT NOT NULL,
            result_sha256 TEXT NOT NULL
                CHECK(length(result_sha256)=64 AND result_sha256 NOT GLOB '*[^0-9a-f]*'),
            overall_status TEXT NOT NULL CHECK(overall_status IN ('ok','warn','breach','unresolved')),
            provenance_completeness TEXT NOT NULL CHECK(provenance_completeness IN ('complete','partial')),
            evidence_as_of TEXT,
            first_evaluated_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            last_checked_at TEXT NOT NULL,
            duplicate_run_count INTEGER NOT NULL DEFAULT 0 CHECK(duplicate_run_count>=0),
            rule_evaluations_json TEXT NOT NULL CHECK(json_valid(rule_evaluations_json)=1 AND json_type(rule_evaluations_json)='array'),
            soft_rule_results_json TEXT CHECK(soft_rule_results_json IS NULL OR (json_valid(soft_rule_results_json)=1 AND json_type(soft_rule_results_json)='array')),
            first_run_id TEXT,
            created_at TEXT NOT NULL,
            UNIQUE(ticker,fingerprint_policy_version,semantic_input_sha256),
            FOREIGN KEY(first_run_id) REFERENCES ingestion_runs(run_id),
            CHECK(datetime(first_evaluated_at) IS NOT NULL AND datetime(last_seen_at) IS NOT NULL AND datetime(last_checked_at) IS NOT NULL),
            CHECK(datetime(first_evaluated_at)<=datetime(last_seen_at) AND datetime(last_seen_at)<=datetime(last_checked_at)),
            CHECK((fingerprint_policy_version='legacy_v0' AND provenance_completeness='partial' AND thesis_content_sha256 IS NULL AND ruleset_sha256 IS NULL) OR fingerprint_policy_version='forward_v1')
        )
        """
    )
    op.execute(
        """
        CREATE TABLE thesis_evaluation_episode_members (
            episode_id TEXT NOT NULL,
            evaluation_id INTEGER NOT NULL UNIQUE,
            membership_role TEXT NOT NULL CHECK(membership_role IN ('anchor','duplicate')),
            member_ordinal INTEGER NOT NULL CHECK(member_ordinal>=1),
            recorded_at TEXT NOT NULL,
            PRIMARY KEY(episode_id,evaluation_id),
            UNIQUE(episode_id,member_ordinal),
            FOREIGN KEY(episode_id) REFERENCES thesis_evaluation_episodes(episode_id),
            FOREIGN KEY(evaluation_id) REFERENCES thesis_evaluations(id)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE thesis_evaluation_episode_check_receipts (
            receipt_id TEXT PRIMARY KEY NOT NULL,
            idempotency_key_sha256 TEXT NOT NULL UNIQUE
                CHECK(length(idempotency_key_sha256)=64 AND idempotency_key_sha256 NOT GLOB '*[^0-9a-f]*'),
            episode_id TEXT NOT NULL,
            ticker TEXT NOT NULL,
            run_id TEXT NOT NULL,
            checked_at TEXT NOT NULL,
            outcome TEXT NOT NULL CHECK(outcome IN ('created','deduplicated_no_change')),
            semantic_input_sha256 TEXT NOT NULL,
            result_sha256 TEXT NOT NULL,
            receipt_sha256 TEXT NOT NULL UNIQUE,
            FOREIGN KEY(episode_id) REFERENCES thesis_evaluation_episodes(episode_id),
            FOREIGN KEY(run_id) REFERENCES ingestion_runs(run_id),
            UNIQUE(run_id,episode_id),
            CHECK(datetime(checked_at) IS NOT NULL)
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_thesis_evaluation_episodes_ticker_latest ON thesis_evaluation_episodes(ticker,last_checked_at DESC)"
    )
    op.execute(
        "CREATE INDEX ix_thesis_evaluation_episode_members_episode ON thesis_evaluation_episode_members(episode_id,member_ordinal)"
    )
    op.execute(
        "CREATE INDEX ix_thesis_evaluation_episode_receipts_episode ON thesis_evaluation_episode_check_receipts(episode_id,checked_at)"
    )

    _backfill(op.get_bind())

    op.execute(
        """
        CREATE VIEW v_thesis_evaluation_history AS
        SELECT
            anchor.evaluation_id AS id,
            episode.ticker AS ticker,
            episode.first_evaluated_at AS evaluated_at,
            episode.overall_status AS overall_status,
            episode.rule_evaluations_json AS rule_evaluations_json,
            episode.first_run_id AS run_id,
            episode.soft_rule_results_json AS soft_rule_results_json,
            episode.episode_id AS episode_id,
            episode.fingerprint_policy_version AS fingerprint_policy_version,
            episode.evidence_as_of AS evidence_as_of,
            episode.last_seen_at AS last_seen_at,
            episode.last_checked_at AS last_checked_at,
            episode.duplicate_run_count AS duplicate_run_count,
            episode.duplicate_run_count + 1 AS occurrence_count,
            episode.provenance_completeness AS provenance_completeness
        FROM thesis_evaluation_episodes AS episode
        JOIN thesis_evaluation_episode_members AS anchor
          ON anchor.episode_id=episode.episode_id AND anchor.membership_role='anchor'
        """
    )
    for table in (
        "thesis_evaluation_episode_members",
        "thesis_evaluation_episode_check_receipts",
    ):
        op.execute(
            f"CREATE TRIGGER trg_{table}_no_update BEFORE UPDATE ON {table} "
            f"BEGIN SELECT RAISE(ABORT, '{table} append-only'); END"
        )
        op.execute(
            f"CREATE TRIGGER trg_{table}_no_delete BEFORE DELETE ON {table} "
            f"BEGIN SELECT RAISE(ABORT, '{table} append-only'); END"
        )


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS v_thesis_evaluation_history")
    for table in (
        "thesis_evaluation_episode_members",
        "thesis_evaluation_episode_check_receipts",
    ):
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_no_update")
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_no_delete")
    op.execute("DROP TABLE IF EXISTS thesis_evaluation_episode_check_receipts")
    op.execute("DROP TABLE IF EXISTS thesis_evaluation_episode_members")
    op.execute("DROP TABLE IF EXISTS thesis_evaluation_episodes")
